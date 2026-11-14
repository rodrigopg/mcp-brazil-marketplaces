"""
MCP server para marketplaces brasileiros.

Tools disponíveis:
- olx_buscar_anuncios / olx_detalhe_anuncio — OLX Brasil (scraping __NEXT_DATA__)
- ml_buscar_anuncios — Mercado Livre Brasil (scraping HTML via UA Googlebot)
"""

import asyncio
import json
import logging
import os
import random
import re
import uuid
from datetime import datetime
from enum import Enum
from html import unescape
from typing import Literal
from urllib.parse import quote, urlparse

import httpx
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger("mcp_brazil_marketplaces")


def _env(name: str, default: str = "") -> str:
    """Lê env preferindo MCP_BR_<NAME>, com fallback ao legado OLX_MCP_<NAME>."""
    return os.getenv(f"MCP_BR_{name}", os.getenv(f"OLX_MCP_{name}", default))


_log_level = _env("LOG_LEVEL", "WARNING").upper()
if _log_level in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
    logging.basicConfig(level=_log_level)
    logger.setLevel(_log_level)

# ---------------------------------------------------------------------------
# Feature flags via env
# ---------------------------------------------------------------------------

DISABLE_JINA = _env("DISABLE_JINA", "0").lower() in ("1", "true", "yes")
ALLOWED_OLX_HOSTS = (".olx.com.br",)

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

BASE_URL = "https://www.olx.com.br"

# Pool de perfis de navegador para rotação anti-bloqueio
BROWSER_PROFILES = [
    {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Sec-Ch-Ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
        "Sec-Ch-Ua-Platform": '"macOS"',
        "Sec-Ch-Ua-Mobile": "?0",
    },
    {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        "Sec-Ch-Ua": '"Google Chrome";v="130", "Chromium";v="130", "Not?A_Brand";v="99"',
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Sec-Ch-Ua-Mobile": "?0",
    },
    {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
        "Sec-Ch-Ua": '"Chromium";v="129", "Not=A?Brand";v="8", "Google Chrome";v="129"',
        "Sec-Ch-Ua-Platform": '"Linux"',
        "Sec-Ch-Ua-Mobile": "?0",
    },
    {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:131.0) Gecko/20100101 Firefox/131.0",
        "Sec-Ch-Ua": None,
        "Sec-Ch-Ua-Platform": None,
        "Sec-Ch-Ua-Mobile": None,
    },
    {
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Mobile/15E148 Safari/604.1",
        "Sec-Ch-Ua": None,
        "Sec-Ch-Ua-Platform": None,
        "Sec-Ch-Ua-Mobile": None,
    },
]


def _build_headers(
    profile: dict, referer: str = "https://www.google.com/", same_origin: bool = False
) -> dict:
    """Monta headers realistas a partir de um perfil de browser."""
    h = {
        "User-Agent": profile["User-Agent"],
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
        "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept-Encoding": "gzip, deflate, br, zstd",
        "Referer": referer,
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin" if same_origin else "cross-site",
        "Sec-Fetch-User": "?1",
        "DNT": "1",
        "Priority": "u=0, i",
    }
    if profile.get("Sec-Ch-Ua"):
        h["Sec-Ch-Ua"] = profile["Sec-Ch-Ua"]
        h["Sec-Ch-Ua-Platform"] = profile["Sec-Ch-Ua-Platform"]
        h["Sec-Ch-Ua-Mobile"] = profile["Sec-Ch-Ua-Mobile"]
    return h


def _env_float(name: str, default: float, lo: float, hi: float) -> float:
    """Lê env (MCP_BR_<NAME>, fallback OLX_MCP_<NAME>) como float com clamp."""
    raw = _env(name)
    if not raw:
        return default
    try:
        v = float(raw)
    except ValueError:
        logger.warning("Env %s inválido (%r), usando default %s", name, raw, default)
        return default
    return max(lo, min(hi, v))


def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        v = int(raw)
    except ValueError:
        logger.warning("Env %s inválido (%r), usando default %s", name, raw, default)
        return default
    return max(lo, min(hi, v))


REQUEST_TIMEOUT = _env_float("REQUEST_TIMEOUT", 25.0, 1.0, 300.0)
HTTP2 = True  # Obrigatório: OLX retorna 403 em HTTP/1.1
MAX_RETRIES = _env_int("MAX_RETRIES", 4, 0, 20)
WARMUP_PROBABILITY = _env_float("WARMUP_PROBABILITY", 0.7, 0.0, 1.0)

# Rate limit por host: protege IP do operador contra loops do LLM que
# fariam queimar o IP em <30s. Semáforo limita concorrência;
# `_HOST_MIN_GAP` impõe atraso mínimo entre chamadas ao mesmo host.
RATE_LIMIT_CONCURRENCY = _env_int("RATE_LIMIT_CONCURRENCY", 2, 1, 16)
RATE_LIMIT_MIN_GAP = _env_float("RATE_LIMIT_MIN_GAP", 0.5, 0.0, 30.0)

_rate_semaphore = asyncio.Semaphore(RATE_LIMIT_CONCURRENCY)
_host_last_request: dict[str, float] = {}
_host_lock = asyncio.Lock()


async def _rate_limit(host: str) -> None:
    """Adquire slot global + força gap mínimo por host.

    Não é contador de janela deslizante — é um throttle simples
    suficiente para evitar que um LLM em loop dispare 100 requests/s.
    Operadores podem desabilitar via MCP_BR_RATE_LIMIT_MIN_GAP=0.
    """
    await _rate_semaphore.acquire()
    if RATE_LIMIT_MIN_GAP <= 0:
        return
    async with _host_lock:
        now = asyncio.get_event_loop().time()
        prev = _host_last_request.get(host, 0.0)
        wait = RATE_LIMIT_MIN_GAP - (now - prev)
        if wait > 0:
            await asyncio.sleep(wait)
        _host_last_request[host] = asyncio.get_event_loop().time()


def _rate_release() -> None:
    _rate_semaphore.release()


class _RateGate:
    """Context manager async: rate-limit por host com release garantido."""

    def __init__(self, url: str) -> None:
        self.host = _host_of(url)

    async def __aenter__(self) -> "_RateGate":
        await _rate_limit(self.host)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        _rate_release()


def _host_of(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""


ESTADOS = {
    "ac",
    "al",
    "ap",
    "am",
    "ba",
    "ce",
    "df",
    "es",
    "go",
    "ma",
    "mt",
    "ms",
    "mg",
    "pa",
    "pb",
    "pr",
    "pe",
    "pi",
    "rj",
    "rn",
    "rs",
    "ro",
    "rr",
    "sc",
    "sp",
    "se",
    "to",
}

# ---------------------------------------------------------------------------
# Inicialização
# ---------------------------------------------------------------------------

mcp = FastMCP("mcp_brazil_marketplaces")


# ---------------------------------------------------------------------------
# Modelos de entrada
# ---------------------------------------------------------------------------


class OrdenarPor(str, Enum):
    RELEVANCIA = "relevance"
    PRECO_MENOR = "price"
    MAIS_RECENTE = "date"


class BuscarAnunciosInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, validate_assignment=True, extra="forbid")

    query: str = Field(
        ...,
        description="Termo de busca. Ex: 'notebook', 'iphone 13', 'sofá 3 lugares'",
        min_length=1,
        max_length=200,
    )
    estado: str | None = Field(
        default=None,
        description=(
            "Sigla do estado brasileiro em minúsculas. Ex: 'sp', 'go', 'rj'. "
            "Se omitido, busca em todo o Brasil."
        ),
        min_length=2,
        max_length=2,
    )
    categoria: str | None = Field(
        default=None,
        description=(
            "Slug de categoria da OLX. Ex: 'informatica-e-acessorios', 'celulares', "
            "'imoveis', 'veiculos-e-pecas', 'eletrodomesticos'. "
            "Se omitido, busca em todas as categorias."
        ),
    )
    preco_min: int | None = Field(
        default=None,
        description="Preço mínimo em reais (inteiro). Ex: 500",
        ge=0,
    )
    preco_max: int | None = Field(
        default=None,
        description="Preço máximo em reais (inteiro). Ex: 3000",
        ge=0,
    )
    ordenar: OrdenarPor = Field(
        default=OrdenarPor.RELEVANCIA,
        description="Ordenação: 'relevance' (relevância), 'price' (menor preço), 'date' (mais recente)",
    )
    pagina: int = Field(
        default=1,
        description="Página de resultados (começa em 1)",
        ge=1,
        le=50,
    )
    excluir: list[str] | None = Field(
        default=None,
        description=(
            "Palavras a excluir (case-insensitive, no título). Ex: ['capa','caixa','carregador','película']"
        ),
        max_length=20,
    )
    queries: list[str] | None = Field(
        default=None,
        description="Buscas extras além de `query` (fan-out, máx 5 no total). Resultados deduplicados por id.",
        max_length=4,
    )
    estados: list[str] | None = Field(
        default=None,
        description="Estados extras além de `estado` (fan-out, máx 5 no total). Ex: ['go','df']",
        max_length=4,
    )


class DetalheAnuncioInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, validate_assignment=True, extra="forbid")

    url: str = Field(
        ...,
        description=(
            "URL completa do anúncio na OLX. "
            "Ex: 'https://sp.olx.com.br/sao-paulo-e-regiao/informatica/notebooks/notebook-abc-1234567890'"
        ),
        min_length=20,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _validar_url_olx(url: str) -> str:
    """Garante que a URL é http(s) e aponta para um host *.olx.com.br.

    Previne SSRF — sem isso, um caller poderia forçar requests para
    169.254.169.254 (metadata cloud), localhost, ou hosts internos.
    """
    try:
        p = urlparse(url)
    except Exception as e:
        raise ValueError(f"URL inválida: {e}") from None

    if p.scheme not in ("http", "https"):
        raise ValueError(f"Esquema não permitido: {p.scheme!r}. Use http(s).")
    host = (p.hostname or "").lower()
    if not host:
        raise ValueError("URL sem hostname.")
    if not any(host == h.lstrip(".") or host.endswith(h) for h in ALLOWED_OLX_HOSTS):
        raise ValueError(f"Hostname não permitido: {host!r}. Apenas *.olx.com.br é aceito.")
    return url


def _build_search_url(params: BuscarAnunciosInput) -> str:
    """Monta a URL de busca da OLX com os filtros fornecidos."""
    estado = params.estado.lower() if params.estado else None
    if estado and estado not in ESTADOS:
        raise ValueError(f"Estado inválido: '{params.estado}'. Use siglas como 'sp', 'go', 'rj'.")

    # Path base
    regiao = f"estado-{estado}" if estado else "brasil"
    categoria = f"/{params.categoria}" if params.categoria else ""
    path = f"{BASE_URL}/{regiao}{categoria}"

    # Query string
    qp: dict[str, str] = {
        "q": params.query,
    }
    if params.pagina > 1:
        qp["o"] = str(params.pagina)

    if params.ordenar == OrdenarPor.PRECO_MENOR:
        qp["sp"] = "1"
    elif params.ordenar == OrdenarPor.MAIS_RECENTE:
        qp["sp"] = "2"

    if params.preco_min is not None:
        qp["ps"] = str(params.preco_min)
    if params.preco_max is not None:
        qp["pe"] = str(params.preco_max)

    qs = "&".join(f"{k}={v}" for k, v in qp.items())
    return f"{path}?{qs}"


MAX_HTML_BYTES = 8 * 1024 * 1024  # 8 MB — páginas reais OLX ficam <2MB
MAX_NEXT_DATA_BYTES = 5 * 1024 * 1024  # 5 MB — blob JSON #20

# Regex sem `.*?` em re.DOTALL — evita catastrophic backtracking em HTML
# adversarial. `[^<]*` é linear porque exclui `<`, garantindo que a
# engine não revisite caracteres já consumidos.
_NEXT_DATA_RE = re.compile(r'id="__NEXT_DATA__"[^>]{0,500}>([^<]+)</script>')


def _extract_next_data(html: str) -> dict:
    """Extrai o JSON embutido no __NEXT_DATA__ da página.

    Defesas:
    - HTML hard-cap (`MAX_HTML_BYTES`) p/ limitar trabalho da regex.
    - Regex linear (`[^<]+`) em vez de `.*?` p/ evitar ReDoS (#19).
    - JSON blob hard-cap (`MAX_NEXT_DATA_BYTES`) p/ evitar OOM (#20).
    """
    if len(html) > MAX_HTML_BYTES:
        html = html[:MAX_HTML_BYTES]

    match = _NEXT_DATA_RE.search(html)
    if not match:
        raise ValueError(
            "Não foi possível encontrar dados estruturados na página. A OLX pode estar bloqueando a requisição."
        )

    blob = match.group(1)
    if len(blob) > MAX_NEXT_DATA_BYTES:
        raise ValueError(f"Payload __NEXT_DATA__ excede limite ({len(blob)} > {MAX_NEXT_DATA_BYTES} bytes).")
    return json.loads(blob)


_INITIAL_DATA_TAG_RE = re.compile(r'<script[^>]{0,3000}id="initial-data"[^>]{0,3000}>')
_DATA_JSON_RE = re.compile(r'data-json="([^"]*)"')
_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]{0,500}>")


def _extract_initial_data(html: str) -> dict:
    """Extrai o `.ad` de `<script id="initial-data" data-json="...">` (páginas de detalhe OLX).

    Mesmas defesas de `_extract_next_data`: cap de HTML, regex linear, cap do blob.
    """
    if len(html) > MAX_HTML_BYTES:
        html = html[:MAX_HTML_BYTES]
    tag = _INITIAL_DATA_TAG_RE.search(html)
    attr = _DATA_JSON_RE.search(tag.group(0)) if tag else None
    if not attr:
        raise ValueError("initial-data não encontrado na página de detalhe.")
    blob = unescape(attr.group(1))
    if len(blob) > MAX_NEXT_DATA_BYTES:
        raise ValueError(f"Payload initial-data excede limite ({len(blob)} > {MAX_NEXT_DATA_BYTES} bytes).")
    ad = _safe_dict(json.loads(blob)).get("ad")
    if not isinstance(ad, dict) or not ad:
        raise ValueError("initial-data sem campo 'ad'.")
    return ad


def _parse_olx_ad(ad: dict, url: str) -> dict:
    """Normaliza o `.ad` do initial-data. Campos crus e descrição ficam separados
    (anúncios se contradizem — o caller compara)."""
    loc = _safe_dict(ad.get("location"))
    body = _TAG_RE.sub("", _BR_RE.sub("\n", _safe_str(ad.get("body"), 20000) or "")).strip()
    props = [
        {
            "nome": _safe_str(_safe_dict(x).get("label")) or _safe_str(_safe_dict(x).get("name")),
            "chave": _safe_str(_safe_dict(x).get("name")),
            "valor": _safe_str(_safe_dict(x).get("value")),
        }
        for x in _safe_list(ad.get("properties"))[:_PROPS_MAX]
    ]
    imgs = [
        _safe_str(_safe_dict(i).get("original"))
        for i in _safe_list(ad.get("images"))[:_LIST_MAX]
        if _safe_str(_safe_dict(i).get("original"))
    ]
    ts = ad.get("origListTime")
    return {
        "fonte": "olx",
        "status": "ok",
        "id": ad.get("listId"),
        "titulo": _safe_str(ad.get("subject")),
        "preco": _safe_str(ad.get("priceValue")),
        "preco_antigo": _safe_str(ad.get("oldPrice")),
        "descricao": body,
        "estado": _safe_str(loc.get("uf")),
        "municipio": _safe_str(loc.get("municipality")),
        "bairro": _safe_str(loc.get("neighbourhood")),
        "vendedor": _safe_str(_safe_dict(ad.get("user")).get("name")),
        "profissional": bool(ad.get("professionalAd")),
        "data": _format_timestamp(ts) if isinstance(ts, int) else None,
        "imagens": imgs,
        "propriedades_raw": props,
        "propriedades": {p["nome"]: p["valor"] for p in props if p["nome"]},
        "url": url,
    }


def _format_timestamp(ts: int) -> str:
    """Converte Unix timestamp para data legível."""
    try:
        return datetime.fromtimestamp(ts).strftime("%d/%m/%Y %H:%M")
    except Exception:
        return str(ts)


_STR_MAX = 500
_LIST_MAX = 50
_PROPS_MAX = 80


def _safe_str(v, max_len: int = _STR_MAX) -> str | None:
    """Coage para string com limite de tamanho; rejeita não-escalares."""
    if v is None:
        return None
    if isinstance(v, str):
        return v[:max_len]
    if isinstance(v, (int, float, bool)):
        return str(v)[:max_len]
    return None  # dict/list em campo string = payload malicioso, descarta


def _safe_dict(v) -> dict:
    return v if isinstance(v, dict) else {}


def _safe_list(v) -> list:
    return v if isinstance(v, list) else []


_SPEC_CHIP_RE = re.compile(r"\bM([1-5])(?:\s?(Pro|Max|Ultra))?\b", re.IGNORECASE)
_SPEC_RAM_RE = re.compile(r"\b(\d{1,3})\s?gb\s*(?:de\s*)?(?:ram|mem[oó]ria)\b", re.IGNORECASE)
_SPEC_SSD_RE = re.compile(
    r"\b(\d{1,4})\s?(gb|tb)(?=\s*(?:de\s*)?ssd\b)|\bssd\s*(?:de\s*)?(\d{1,4})\s?(gb|tb)\b", re.IGNORECASE
)
_SPEC_PAIR_RE = re.compile(r"\b(\d{1,2})\s?(?:gb)?\s?/\s?(\d{3,4})\s?(?:gb)?\b", re.IGNORECASE)


_SPEC_GB_RE = re.compile(r"\b(\d{1,3})\s?gb\b", re.IGNORECASE)
_RAM_SIZES = {8, 16, 18, 24, 32, 36, 48, 64, 96, 128}


def _extract_specs(text: str) -> dict:
    """Chip/RAM/SSD inferidos do texto (título). Heurística — o anúncio pode se contradizer."""
    text = text[:2000]
    out: dict = {}
    if m := _SPEC_CHIP_RE.search(text):
        out["chip"] = f"M{m.group(1)}" + (f" {m.group(2).title()}" if m.group(2) else "")
    if m := _SPEC_RAM_RE.search(text):
        out["ram_gb"] = int(m.group(1))
    # "24GB SSD 512GB": as duas formas casam; o SSD é o maior candidato
    ssd = [
        int(m.group(1) or m.group(3)) * (1000 if (m.group(2) or m.group(4)).lower() == "tb" else 1)
        for m in _SPEC_SSD_RE.finditer(text)
    ]
    if ssd:
        out["ssd_gb"] = max(ssd)
    if m := _SPEC_PAIR_RE.search(text):
        out.setdefault("ram_gb", int(m.group(1)))
        out.setdefault("ssd_gb", int(m.group(2)))
    if "ram_gb" not in out:  # "24GB 256GB SSD": o único GB típico de RAM que sobra
        ram = [
            int(n) for n in _SPEC_GB_RE.findall(text) if int(n) in _RAM_SIZES and int(n) != out.get("ssd_gb")
        ]
        if len(set(ram)) == 1:
            out["ram_gb"] = ram[0]
    return out


_CARD_END_RE = re.compile(r"</section>")
_CARD_HREF_RE = re.compile(r'<a[^>]{0,500}?href="([^"]{1,600})"')
_CARD_ID_RE = re.compile(r"-(\d{6,})$")
_CARD_TEXT = r"((?:[^<]|</?(?:span|strong|b|small)\b[^>]{0,100}>){0,300})"


def _card_field_re(cls: str) -> re.Pattern:
    return re.compile(rf'class="(?:[^"]{{0,200}}\s)?{cls}(?:\s[^"]{{0,200}})?"[^>]{{0,300}}>' + _CARD_TEXT)


_CARD_FIELDS = {
    "titulo": _card_field_re("olx-adcard__title"),
    "preco": _card_field_re("olx-adcard__price"),
    "preco_antigo": _card_field_re("olx-adcard__old-price"),
    "localizacao": _card_field_re("olx-adcard__location"),
    "data": _card_field_re("olx-adcard__date"),
    "vendedor": _card_field_re("Ir9Wnq_transactionalSellerName"),
}


def _parse_olx_cards(html: str) -> list[dict]:
    """Cards da busca (`section.olx-adcard`) quando não há __NEXT_DATA__. Regex lineares, caps de tamanho."""
    if len(html) > MAX_HTML_BYTES:
        html = html[:MAX_HTML_BYTES]
    out: list[dict] = []
    for chunk in html.split("<section")[1:201]:
        if "olx-adcard" not in chunk[:300]:
            continue
        chunk = chunk[:20000]
        end = _CARD_END_RE.search(chunk)
        chunk = chunk[: end.start()] if end else chunk
        href = _CARD_HREF_RE.search(chunk)
        url = unescape(href.group(1)).split("?")[0] if href else ""
        try:
            _validar_url_olx(url)
        except ValueError:
            continue
        ad: dict = {"url": url}
        m = _CARD_ID_RE.search(url)
        ad["id"] = int(m.group(1)) if m else None
        for key, rx in _CARD_FIELDS.items():
            f = rx.search(chunk)
            ad[key] = _safe_str(" ".join(unescape(_TAG_RE.sub("", f.group(1))).split())) if f else None
        out.append(ad)
    return out


def _format_ad_summary(ad: dict) -> dict:
    """Normaliza um anúncio da listagem aplicando coerção defensiva.

    Site comprometido pode injetar JSON malicioso no __NEXT_DATA__
    (lists onde se espera string, dicts gigantes, etc.). Cada campo
    passa por `_safe_*` antes de chegar ao LLM (issue #29).
    """
    if not isinstance(ad, dict):
        return {}

    preco_raw = _safe_str(ad.get("priceValue")) or _safe_str(ad.get("price")) or ""
    loc = _safe_dict(ad.get("locationDetails"))
    images = _safe_list(ad.get("images"))[:_LIST_MAX]
    primeira_img = _safe_dict(images[0] if images else {}).get("original")

    propriedades_raw = _safe_list(ad.get("properties"))[:_PROPS_MAX]
    propriedades = {}
    for p in propriedades_raw:
        if not isinstance(p, dict):
            continue
        label = _safe_str(p.get("label"), max_len=100)
        value = _safe_str(p.get("value"))
        if label and value:
            propriedades[label] = value

    date_raw = ad.get("date")
    if isinstance(date_raw, int):
        data = _format_timestamp(date_raw)
    elif isinstance(date_raw, str):
        data = date_raw[:_STR_MAX]
    else:
        data = None

    list_id = ad.get("listId")
    if not isinstance(list_id, (int, str)):
        list_id = None

    municipality = _safe_str(loc.get("municipality")) or ""
    uf = _safe_str(loc.get("uf")) or ""
    localizacao = _safe_str(ad.get("location")) or f"{municipality} - {uf}".strip(" -")

    return {
        "id": list_id,
        "titulo": _safe_str(ad.get("subject")) or _safe_str(ad.get("title")),
        "preco": preco_raw,
        "categoria": _safe_str(ad.get("categoryName")) or _safe_str(ad.get("category")),
        "localizacao": localizacao,
        "bairro": _safe_str(loc.get("neighbourhood")),
        "data": data,
        "url": _safe_str(ad.get("friendlyUrl")) or _safe_str(ad.get("url")),
        "imagem": _safe_str(primeira_img) if primeira_img else None,
        "profissional": bool(ad.get("professionalAd", False)),
        "entrega_olx": bool(_safe_dict(ad.get("olxDelivery")).get("enabled", False)),
        "propriedades": propriedades,
    }


async def _fetch_with_evasion(url: str, referer_override: str | None = None) -> str:
    """
    GET com técnicas anti-bloqueio:
    - Rotação de perfil de browser (UA + sec-ch-ua coerentes)
    - Warm-up opcional na homepage para captar cookies (bm_*, ak_bmsc, etc)
    - Retry com backoff exponencial + jitter
    - Troca de perfil a cada falha 403/429
    - Fallback final: Google Web Cache
    Levanta httpx.HTTPStatusError se todas tentativas falharem.
    """
    last_exc: Exception | None = None

    async with _RateGate(url):
        for attempt in range(MAX_RETRIES):
            profile = random.choice(BROWSER_PROFILES)
            cookies = httpx.Cookies()

            try:
                async with httpx.AsyncClient(
                    follow_redirects=True,
                    timeout=REQUEST_TIMEOUT,
                    http2=HTTP2,
                    cookies=cookies,
                ) as client:
                    if attempt == 0 or random.random() < WARMUP_PROBABILITY:
                        try:
                            warm_headers = _build_headers(
                                profile, referer="https://www.google.com/", same_origin=False
                            )
                            await client.get(BASE_URL + "/", headers=warm_headers)
                            await asyncio.sleep(random.uniform(0.4, 1.2))
                        except Exception:
                            pass

                    ref = referer_override or (
                        BASE_URL + "/" if random.random() < 0.5 else "https://www.google.com/"
                    )
                    same_origin = ref.startswith(BASE_URL)
                    headers = _build_headers(profile, referer=ref, same_origin=same_origin)

                    resp = await client.get(url, headers=headers)
                    if resp.status_code in (403, 429):
                        last_exc = httpx.HTTPStatusError(
                            f"status {resp.status_code}", request=resp.request, response=resp
                        )
                        logger.debug("retry %s: status %s p/ %s", attempt, resp.status_code, url)
                        await asyncio.sleep((2**attempt) + random.uniform(0.3, 1.5))
                        continue
                    resp.raise_for_status()
                    return resp.text
            except httpx.HTTPStatusError as e:
                last_exc = e
                if e.response.status_code not in (403, 429, 503):
                    raise
                await asyncio.sleep((2**attempt) + random.uniform(0.3, 1.5))
            except (httpx.TimeoutException, httpx.TransportError) as e:
                last_exc = e
                await asyncio.sleep((2**attempt) + random.uniform(0.2, 0.8))

    if last_exc:
        raise last_exc
    raise RuntimeError("Falha ao buscar URL após retries.")


async def _fetch_with_retries(url: str, headers: dict, max_retries: int | None = None) -> str:
    """Fetch genérico com backoff exponencial + rate limit. Usado por ML / detail."""
    retries = max_retries if max_retries is not None else MAX_RETRIES
    last_exc: Exception | None = None
    async with _RateGate(url):
        for attempt in range(max(1, retries)):
            try:
                async with httpx.AsyncClient(
                    follow_redirects=True, timeout=REQUEST_TIMEOUT, http2=HTTP2
                ) as client:
                    resp = await client.get(url, headers=headers)
                    if resp.status_code in (403, 429, 503):
                        last_exc = httpx.HTTPStatusError(
                            f"status {resp.status_code}", request=resp.request, response=resp
                        )
                        logger.debug("retry %s: status %s p/ %s", attempt, resp.status_code, url)
                        await asyncio.sleep((2**attempt) + random.uniform(0.2, 1.0))
                        continue
                    resp.raise_for_status()
                    return resp.text
            except (httpx.TimeoutException, httpx.TransportError) as e:
                last_exc = e
                await asyncio.sleep((2**attempt) + random.uniform(0.2, 0.8))
    if last_exc:
        raise last_exc
    raise RuntimeError("Falha ao buscar URL após retries.")


async def _fetch_via_jina(url: str) -> str:
    """Fallback usando r.jina.ai como proxy reader. Retorna markdown."""
    proxy_url = f"https://r.jina.ai/{url}"
    logger.info("jina_fallback url=%s", url)
    async with _RateGate(proxy_url):
        async with httpx.AsyncClient(follow_redirects=True, timeout=REQUEST_TIMEOUT) as client:
            resp = await client.get(
                proxy_url,
                headers={"Accept": "text/markdown", "X-Return-Format": "markdown", "X-No-Cache": "true"},
            )
            resp.raise_for_status()
            return resp.text


def _parse_search_markdown(md: str, url_busca: str) -> dict:
    """Parser de fallback que extrai anúncios do markdown do r.jina.ai."""
    anuncios: list[dict] = []

    # Corta seção "Você pode gostar" para não misturar recomendações com busca real
    cut = re.search(r"##\s*Você pode gostar", md)
    md_busca = md[: cut.start()] if cut else md

    # Total: "1 - N de TOTAL resultados"
    total = 0
    mt = re.search(r"de\s+(\d+)\s+resultados?", md_busca)
    if mt:
        total = int(mt.group(1))
    md = md_busca

    # Cada anúncio aparece como: "## [Titulo](URL ...)" seguido de bloco até "Adicionar aos favoritos"
    pattern = re.compile(
        r'## \[([^\]]+)\]\((https?://[^\s)"]+)[^)]*\)\s*(.*?)Adicionar aos favoritos',
        re.DOTALL,
    )
    seen_ids: set[str] = set()
    for m in pattern.finditer(md):
        titulo = m.group(1).strip()
        link = m.group(2).strip()
        bloco = m.group(3)

        # Pular top_ads / anúncios patrocinados fora da busca real
        if "top_ads" in link:
            continue

        idm = re.search(r"/(\d{8,})(?:\?|$|-)", link)
        ad_id = idm.group(1) if idm else link
        if ad_id in seen_ids:
            continue
        seen_ids.add(ad_id)

        preco_m = re.search(r"R\$\s*([\d\.\,]+)", bloco)
        preco = f"R$ {preco_m.group(1)}" if preco_m else None

        # Localização: linha contendo cidade (heurística - antes de data)
        loc = None
        for ln in bloco.split("\n"):
            ln = ln.strip()
            if not ln or ln.startswith(
                ("![", "*", "Slide", "Ir para", "Entrega", "Pague", "Parcelamento", "em até", "Garantia")
            ):
                continue
            if re.match(r"^\d+\s+de\s+\w+", ln) or re.match(r"^\d{2}/\d{2}/\d{4}", ln):
                continue
            if "R$" in ln or "###" in ln:
                continue
            loc = ln
            break

        data_m = re.search(r"(\d+\s+de\s+\w+,\s*\d+:\d+|\d{2}/\d{2}/\d{4},?\s*\d+:\d+)", bloco)
        data = data_m.group(1) if data_m else None

        img_m = re.search(r"!\[[^\]]*\]\((https://img\.olx\.com\.br/[^\)]+)\)", bloco)
        imagem = img_m.group(1) if img_m else None

        anuncios.append(
            {
                "id": int(ad_id) if ad_id.isdigit() else ad_id,
                "titulo": titulo,
                "preco": preco,
                "localizacao": loc,
                "data": data,
                "url": link,
                "imagem": imagem,
                "_fonte": "jina_markdown",
            }
        )

    return {
        "total": total or len(anuncios),
        "pagina": 1,
        "por_pagina": len(anuncios),
        "url_busca": url_busca,
        "anuncios": anuncios,
        "_fonte": "jina_proxy_fallback",
    }


def _handle_http_error(e: Exception) -> str:
    """Formata erros HTTP de forma padronizada.

    Mensagens conhecidas (HTTP status, timeout, validação) são seguras:
    não contêm caminhos internos nem detalhes da stack. Para qualquer
    exceção desconhecida, geramos um correlation ID, logamos a stack
    completa internamente, e devolvemos apenas o ID ao caller — evita
    leak de paths, tokens e estado de runtime (issue #21).
    """
    if isinstance(e, httpx.HTTPStatusError):
        code = e.response.status_code
        if code == 404:
            return "Erro: página não encontrada (404). Verifique a URL ou os filtros informados."
        if code == 403:
            return "Erro: acesso negado (403). A OLX pode estar bloqueando requisições automatizadas."
        if code == 429:
            return "Erro: muitas requisições (429). Aguarde alguns segundos antes de tentar novamente."
        # body da resposta pode conter HTML longo — não propagar
        return f"Erro HTTP {code}: resposta inesperada do servidor."
    if isinstance(e, httpx.TimeoutException):
        return "Erro: timeout na requisição. Tente novamente."
    if isinstance(e, ValueError):
        return f"Erro de validação: {e}"
    err_id = uuid.uuid4().hex[:8]
    logger.exception("Erro inesperado [%s]: %s", err_id, e)
    return f"Erro inesperado (id={err_id}). Consulte os logs do servidor para detalhes."


def _status_of(e: Exception) -> str:
    """Status por fonte p/ exceções de rede: blocked | removido | erro."""
    if isinstance(e, httpx.HTTPStatusError):
        if e.response.status_code in (403, 429, 503):
            return "blocked"
        if e.response.status_code in (404, 410):
            return "removido"
    return "erro"


def _erro(msg: str, fonte: str, status: str = "erro", **extra) -> dict:
    """Envelope de erro padrão: toda resposta tem `fonte` + `status` (ok|blocked|requer_token|removido|layout|erro)."""
    return {"fonte": fonte, "status": status, "erro": msg, **extra}


# ---------------------------------------------------------------------------
# Ferramentas
# ---------------------------------------------------------------------------


async def _olx_search_once(params: BuscarAnunciosInput) -> dict:
    """Uma busca (1 query × 1 estado). Retorna dict; erros viram {"erro": ...}."""
    try:
        url = _build_search_url(params)
    except ValueError as e:
        return _erro(str(e), "olx")

    logger.info(
        "olx_search query=%r estado=%s categoria=%s pagina=%s",
        params.query,
        params.estado,
        params.categoria,
        params.pagina,
    )
    html = None
    try:
        html = await _fetch_with_evasion(url)
    except Exception as e0:
        if DISABLE_JINA:
            return _erro(
                "Erro: acesso negado e fallback Jina desabilitado (MCP_BR_DISABLE_JINA=1).",
                "olx",
                _status_of(e0),
            )
        # Fallback markdown via r.jina.ai
        try:
            md = await _fetch_via_jina(url)
            result = _parse_search_markdown(md, url)
            result["fonte"] = "olx_jina"
            result["status"] = "ok"
            return result
        except Exception as e:
            return _erro(
                _handle_http_error(e), "olx", _status_of(e0) if _status_of(e0) != "erro" else _status_of(e)
            )

    try:
        props = _extract_next_data(html)["props"]["pageProps"]
        anuncios = [_format_ad_summary(ad) for ad in props.get("ads", []) if ad.get("listId")]
        total = props.get("totalOfAds", 0)
    except Exception:
        # OLX removeu __NEXT_DATA__ da busca: lê os cards `section.olx-adcard`
        anuncios = _parse_olx_cards(html)
        total = len(anuncios)
        props = {}
        if not anuncios:
            return _erro(
                "Layout da busca OLX não reconhecido (sem __NEXT_DATA__ nem cards olx-adcard).",
                "olx",
                "layout",
            )
    for a in anuncios:
        a["fonte"] = "olx"

    result = {
        "fonte": "olx",
        "total": total,
        "pagina": props.get("pageIndex", params.pagina),
        "por_pagina": props.get("pageSize", len(anuncios)),
        "url_busca": url,
        "anuncios": anuncios,
    }
    return result


@mcp.tool(
    name="olx_buscar_anuncios",
    annotations={
        "title": "Buscar Anúncios na OLX Brasil",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def olx_buscar_anuncios(params: BuscarAnunciosInput) -> str:
    """Busca anúncios públicos na OLX Brasil com filtros de texto, estado, categoria, preço e ordenação.

    Retorna lista paginada de anúncios com título, preço, localização, data, URL e propriedades extras.

    Args:
        params (BuscarAnunciosInput):
            - query (str): Termo de busca obrigatório.
            - estado (Optional[str]): Sigla do estado ('sp', 'go', etc.). Padrão: todo Brasil.
            - categoria (Optional[str]): Slug de categoria OLX. Ex: 'informatica-e-acessorios'.
            - preco_min (Optional[int]): Preço mínimo em reais.
            - preco_max (Optional[int]): Preço máximo em reais.
            - ordenar (str): 'relevance' | 'price' | 'date'. Padrão: 'relevance'.
            - pagina (int): Número da página (1–50). Padrão: 1.
            - excluir (Optional[list[str]]): palavras a excluir do título (acessórios etc.).
            - queries / estados (Optional[list[str]]): fan-out; resultados deduplicados por id.

    Returns:
        str: JSON com campos:
            - total (int): Total de anúncios encontrados.
            - pagina (int): Página atual.
            - por_pagina (int): Anúncios por página.
            - url_busca (str): URL utilizada na busca.
            - anuncios (list): Lista de anúncios com id, titulo, preco, categoria,
              localizacao, bairro, data, url, imagem, profissional, entrega_olx, propriedades,
              specs (chip/ram/ssd inferidos do título) e preco_cartao=true (preço da listagem
              pode estar defasado: confirme com olx_detalhe_anuncio).
            - fontes (list): status por (query, estado): ok | erro.
    """
    queries = list(dict.fromkeys([params.query, *(params.queries or [])]))[:5]
    estados = list(dict.fromkeys([params.estado, *(params.estados or [])]))[:5]
    combos = [(q, e) for q in queries for e in estados]
    excluir = [x.lower() for x in (params.excluir or []) if x.strip()]

    anuncios: list[dict] = []
    vistos: set = set()
    fontes: list[dict] = []
    total = 0
    url_busca = None
    for q, e in combos:
        r = await _olx_search_once(params.model_copy(update={"query": q, "estado": e}))
        if "erro" in r:
            fontes.append({"query": q, "estado": e, "status": r.get("status", "erro"), "erro": r["erro"]})
            continue
        fontes.append({"query": q, "estado": e, "status": "ok", "fonte": r.get("fonte")})
        total += r.get("total") or 0
        url_busca = url_busca or r.get("url_busca")
        for a in r["anuncios"]:
            if a.get("id") in vistos:
                continue
            vistos.add(a.get("id"))
            a["preco_cartao"] = True  # preço do card pode estar defasado; confirmar em olx_detalhe_anuncio
            a["specs"] = _extract_specs(f"{a.get('titulo') or ''} {a.get('url') or ''}")
            if excluir and any(x in (a.get("titulo") or "").lower() for x in excluir):
                continue
            anuncios.append(a)

    falhas = [f for f in fontes if f["status"] != "ok"]
    if len(falhas) == len(fontes):
        return json.dumps(_erro(falhas[0]["erro"], "olx", falhas[0]["status"]), ensure_ascii=False)
    result = {
        "fonte": "olx",
        "status": "parcial" if falhas else "ok",
        "total": total,
        "pagina": params.pagina,
        "por_pagina": len(anuncios),
        "url_busca": url_busca,
        "anuncios": anuncios,
        "fontes": fontes,
        "avisos": [
            "preco_cartao: preços da listagem podem estar defasados; confirme com olx_detalhe_anuncio."
        ],
    }
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool(
    name="olx_detalhe_anuncio",
    annotations={
        "title": "Obter Detalhes de Anúncio OLX",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def olx_detalhe_anuncio(params: DetalheAnuncioInput) -> str:
    """Obtém detalhes completos de um anúncio específico da OLX a partir da URL.

    Args:
        params (DetalheAnuncioInput):
            - url (str): URL completa do anúncio na OLX.

    Returns:
        str: JSON com campos:
            - id (int): ID do anúncio (list_id).
            - titulo (str): Título do anúncio.
            - descricao (str): Descrição completa (HTML removido).
            - preco (str): Preço formatado.
            - categoria (str): Categoria principal.
            - subcategoria (str): Subcategoria.
            - estado (str): UF do anúncio.
            - municipio (str): Cidade.
            - bairro (str): Bairro.
            - vendedor (str): Nome do vendedor.
            - profissional (bool): Se é anunciante profissional.
            - data (str): Data de publicação formatada.
            - imagens (list[str]): Lista de URLs das imagens.
            - propriedades (dict): Atributos específicos da categoria.
            - url (str): URL canônica do anúncio.
    """
    # SSRF guard: somente *.olx.com.br é permitido
    try:
        _validar_url_olx(params.url)
    except ValueError as e:
        return json.dumps(_erro(f"Erro de validação: {e}", "olx"), ensure_ascii=False)

    used_jina = False
    try:
        html = await _fetch_with_evasion(params.url)
    except Exception as e0:
        if _status_of(e0) == "removido":  # 404/410: anúncio saiu do ar
            return json.dumps({"fonte": "olx", "status": "removido", "url": params.url}, ensure_ascii=False)
        if DISABLE_JINA:
            return json.dumps(
                _erro(
                    "Erro: acesso negado e fallback Jina desabilitado (MCP_BR_DISABLE_JINA=1).",
                    "olx",
                    _status_of(e0),
                ),
                ensure_ascii=False,
            )
        try:
            html = await _fetch_via_jina(params.url)
            used_jina = True
        except Exception as e:
            st = _status_of(e0) if _status_of(e0) != "erro" else _status_of(e)
            return json.dumps(_erro(_handle_http_error(e), "olx", st), ensure_ascii=False)

    # Se vier markdown do jina, parse simples
    if used_jina:
        try:
            titulo_m = re.search(r"^Title:\s*(.+)$", html, re.MULTILINE)
            preco_m = re.search(r"R\$\s*([\d\.\,]+)", html)
            desc_m = re.search(r"##\s*Descrição\s*(.+?)(?:##|\Z)", html, re.DOTALL | re.IGNORECASE)
            loc_m = re.search(r"##\s*Localiza[çc][ãa]o\s*(.+?)(?:##|\Z)", html, re.DOTALL | re.IGNORECASE)
            imgs = list(dict.fromkeys(re.findall(r"https://img\.olx\.com\.br/[^\s\)]+", html)))
            result = {
                "fonte": "olx_jina",
                "status": "ok",
                "titulo": (titulo_m.group(1).strip() if titulo_m else None),
                "preco": (f"R$ {preco_m.group(1)}" if preco_m else None),
                "descricao": (desc_m.group(1).strip()[:2000] if desc_m else None),
                "localizacao": (loc_m.group(1).strip()[:300] if loc_m else None),
                "imagens": imgs[:10],
                "url": params.url,
            }
            return json.dumps(result, ensure_ascii=False, indent=2)
        except Exception as e:
            err_id = uuid.uuid4().hex[:8]
            logger.exception("Falha ao parsear markdown [%s]: %s", err_id, e)
            return json.dumps(
                _erro(f"Falha ao parsear markdown (id={err_id}).", "olx_jina", "layout"), ensure_ascii=False
            )

    try:
        ad = _extract_initial_data(html)
        return json.dumps(_parse_olx_ad(ad, params.url), ensure_ascii=False, indent=2)
    except ValueError as e:
        return json.dumps(
            _erro(f"Não foi possível extrair dados do anúncio: {e}", "olx", "layout"), ensure_ascii=False
        )
    except Exception as e:
        err_id = uuid.uuid4().hex[:8]
        logger.exception("Falha ao processar anúncio [%s]: %s", err_id, e)
        return json.dumps(_erro(f"Falha ao processar anúncio (id={err_id}).", "olx"), ensure_ascii=False)


class VerificarAnunciosInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, validate_assignment=True, extra="forbid")

    urls: list[str] = Field(
        ..., description="URLs de anúncios OLX a verificar (máx 20).", min_length=1, max_length=20
    )


async def _olx_liveness(url: str) -> dict:
    """Status de um anúncio: ativo | removido (404/410) | blocked (403/429/503) | erro. Sem evasão."""
    try:
        _validar_url_olx(url)
    except ValueError as e:
        return {"url": url, "status": "erro", "erro": str(e)}
    try:
        async with _RateGate(url):
            async with httpx.AsyncClient(follow_redirects=True, timeout=REQUEST_TIMEOUT, http2=HTTP2) as c:
                code = (await c.get(url, headers={"User-Agent": "mcp-brazil-marketplaces"})).status_code
    except (httpx.TimeoutException, httpx.TransportError):
        return {"url": url, "status": "erro", "erro": "timeout/rede"}
    if code in (404, 410):
        return {"url": url, "status": "removido"}
    if code in (403, 429, 503):
        return {
            "url": url,
            "status": "blocked",
            "dica": "OLX bloqueou esta checagem (IP/cliente automatizado). Confirme abrindo a URL no navegador.",
        }
    return {"url": url, "status": "ativo" if code == 200 else "erro", "http": code}


@mcp.tool(
    name="olx_verificar_anuncios",
    annotations={
        "title": "Verificar se anúncios OLX continuam no ar",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def olx_verificar_anuncios(params: VerificarAnunciosInput) -> str:
    """Checa em lote se URLs de anúncios OLX ainda estão ativas (ativo/removido/blocked)."""
    results = await asyncio.gather(*[_olx_liveness(u) for u in params.urls])
    return json.dumps(
        {"fonte": "olx", "total": len(results), "anuncios": results}, ensure_ascii=False, indent=2
    )


# ---------------------------------------------------------------------------
# Mercado Livre — busca via scraping (Googlebot UA bypassa micro-landing)
# ---------------------------------------------------------------------------

ML_BASE = "https://lista.mercadolivre.com.br"

# UA Googlebot bypassa o micro-landing anti-bot do ML. Operadores que
# considerem o spoof inaceitável (risco ético/legal) podem sobrescrever
# via MCP_BR_ML_USER_AGENT — nesse caso o ML geralmente devolve a
# página de challenge e a tool retorna lista vazia, mas sem spoof.
_ML_DEFAULT_UA = "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"
ML_HEADERS = {
    "User-Agent": _env("ML_USER_AGENT") or _ML_DEFAULT_UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
}


class BuscarMLInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, validate_assignment=True, extra="forbid")

    query: str = Field(..., min_length=1, max_length=200, description="Termo de busca.")
    preco_min: int | None = Field(default=None, ge=0, description="Preço mínimo em R$.")
    preco_max: int | None = Field(default=None, ge=0, description="Preço máximo em R$.")
    estado: str | None = Field(
        default=None,
        min_length=2,
        max_length=2,
        description=(
            "AVISO: heurística best-effort. ML raramente expõe localização "
            "nos cards de listagem, então este filtro frequentemente retorna "
            "lista vazia mesmo quando há resultados no estado. Para resultados "
            "confiáveis por região, use ml_buscar_anuncios sem este campo e "
            "filtre pelo título/descrição do anúncio."
        ),
    )
    condicao: str | None = Field(
        default=None,
        description=(
            "'novo' ou 'usado'. AVISO: ML ignora o filtro via URL, então é "
            "aplicado pós-scraping por heurística no título do anúncio."
        ),
    )
    pagina: int = Field(default=1, ge=1, le=20)
    ordenar: OrdenarPor = Field(
        default=OrdenarPor.RELEVANCIA,
        description="'relevance' ou 'price' (menor preço). 'date' não suportado no ML.",
    )


def _build_ml_url(p: BuscarMLInput) -> tuple[str, list[str]]:
    """Monta URL de busca ML. Retorna (url, avisos)."""
    avisos: list[str] = []
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", p.query.strip().lower()).strip("-")
    base = f"{ML_BASE}/{slug}"
    filters = []
    if p.preco_min is not None or p.preco_max is not None:
        lo = p.preco_min if p.preco_min is not None else 0
        hi = p.preco_max if p.preco_max is not None else 0
        filters.append(f"_PriceRange_{lo}-{hi if hi else '*'}")
    if p.condicao:
        cond = {"novo": "2230284", "usado": "2230581"}.get(p.condicao.lower())
        if cond:
            # ML não respeita ITEM_CONDITION via slug em /lista — aplicamos
            # como filtro pós-scraping no título do anúncio.
            avisos.append("Filtro 'condicao' aplicado pós-scraping (heurística por título).")
    if p.pagina > 1:
        filters.append(f"_Desde_{(p.pagina - 1) * 50 + 1}")
    if p.ordenar == OrdenarPor.PRECO_MENOR:
        filters.append("_OrderId_PRICE_NoIndex_True")
    if filters:
        base += "".join(filters)
    return base, avisos


def _parse_ml_html(html: str) -> list[dict]:
    """Extrai cards de produto do HTML do Mercado Livre."""
    anuncios = []
    cards = re.findall(
        r'<(?:li|div)[^>]*class="[^"]*(?:ui-search-layout__item|poly-card)[^"]*".*?</(?:li|div)>',
        html,
        re.DOTALL,
    )
    for card in cards:
        title_m = re.search(
            r'class="poly-component__title[^"]*"[^>]*>([^<]+)<',
            card,
        )
        if not title_m:
            continue
        titulo = title_m.group(1).strip()

        link_m = re.search(
            r'href="(https?://(?:produto|articulo|www)\.mercado(?:livre|libre)\.com[^"]+)"',
            card,
        )
        link = link_m.group(1).replace("&amp;", "&").split("#")[0] if link_m else None

        preco_int = re.search(r"andes-money-amount__fraction[^>]*>([\d\.]+)<", card)
        preco_cents = re.search(r"andes-money-amount__cents[^>]*>([\d]+)<", card)
        preco = None
        if preco_int:
            val = preco_int.group(1).replace(".", "")
            preco = f"R$ {val}" + (f",{preco_cents.group(1)}" if preco_cents else "")

        # Frete
        frete = "Frete grátis" if "Frete grátis" in card or "frete grátis" in card else None

        # Local (raro nos cards de busca ML; geralmente só na pdp)
        loc_m = re.search(r"poly-component__location[^>]*>([^<]+)<", card)
        loc = loc_m.group(1).strip() if loc_m else None

        # Vendedor
        sel_m = re.search(r"poly-component__seller[^>]*>([^<]+)<", card)
        seller = sel_m.group(1).strip() if sel_m else None

        # Img
        img_m = re.search(r'<img[^>]+(?:src|data-src)="(https://http2\.mlstatic\.com/[^"]+)"', card)
        imagem = img_m.group(1) if img_m else None

        # Atributos (RAM, armazenamento etc) - poly-attributes_list
        attrs = re.findall(r"poly-attributes_list__item[^>]*>([^<]+)<", card)

        # ID extraído do MLB-<digits> na URL — campo comum a OLX/ML
        ad_id: str | int | None = None
        if link:
            m_id = re.search(r"MLB-?(\d+)", link)
            if m_id:
                ad_id = int(m_id.group(1))

        anuncios.append(
            {
                # campos comuns (schema unificado)
                "id": ad_id,
                "titulo": titulo,
                "preco": preco,
                "localizacao": loc,
                "data": None,  # ML não expõe data nos cards
                "url": link,
                "imagem": imagem,
                # campos específicos ML
                "frete": frete,
                "vendedor": seller,
                "atributos": [a.strip() for a in attrs if a.strip()],
            }
        )
    return anuncios


# ---------------------------------------------------------------------------
# Mercado Livre — API oficial (token OAuth)
# ---------------------------------------------------------------------------
ML_API = "https://api.mercadolibre.com"
_ML_TOKEN_STATE: dict = {"access": None, "refresh": None, "loaded": False}
_ML_SETUP_STEPS = [
    "1. Registre um app em https://developers.mercadolivre.com.br/ (Minhas aplicações).",
    "2. Autorize o app com sua conta (fluxo OAuth authorization_code) e obtenha access_token + refresh_token.",
    "3. Exporte MCP_BR_ML_ACCESS_TOKEN=<access_token> (vale ~6h).",
    "4. Para renovar sozinho: MCP_BR_ML_CLIENT_ID, MCP_BR_ML_CLIENT_SECRET, MCP_BR_ML_REFRESH_TOKEN "
    "e MCP_BR_ML_TOKEN_FILE=<caminho> (o refresh token é de uso único; o arquivo guarda o novo).",
]


def _ml_requer_token(motivo: str = "A API pública de busca do Mercado Livre exige um token OAuth.") -> str:
    return json.dumps(
        {"fonte": "ml", "status": "requer_token", "erro": motivo, "passos": _ML_SETUP_STEPS},
        ensure_ascii=False,
        indent=2,
    )


def _ml_load_tokens() -> None:
    if _ML_TOKEN_STATE["loaded"]:
        return
    _ML_TOKEN_STATE["loaded"] = True
    _ML_TOKEN_STATE["access"] = _env("ML_ACCESS_TOKEN") or None
    _ML_TOKEN_STATE["refresh"] = _env("ML_REFRESH_TOKEN") or None
    path = _env("ML_TOKEN_FILE")
    if path and os.path.exists(path):
        try:
            with open(path) as f:
                saved = json.load(f)
            _ML_TOKEN_STATE["access"] = _safe_str(saved.get("access_token")) or _ML_TOKEN_STATE["access"]
            _ML_TOKEN_STATE["refresh"] = _safe_str(saved.get("refresh_token")) or _ML_TOKEN_STATE["refresh"]
        except (OSError, ValueError):
            logger.warning("ML_TOKEN_FILE ilegível; ignorando.")


async def _ml_request(method: str, url: str, **kw) -> httpx.Response:
    """Único ponto de rede da API ML (rate-limited). Os testes substituem esta função."""
    async with _RateGate(url):
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            return await client.request(method, url, **kw)


async def _ml_refresh() -> bool:
    """Renova o access token com o refresh token (uso único: persiste o novo em ML_TOKEN_FILE)."""
    cid, secret, refresh = _env("ML_CLIENT_ID"), _env("ML_CLIENT_SECRET"), _ML_TOKEN_STATE["refresh"]
    if not (cid and secret and refresh):
        return False
    resp = await _ml_request(
        "POST",
        f"{ML_API}/oauth/token",
        data={
            "grant_type": "refresh_token",
            "client_id": cid,
            "client_secret": secret,
            "refresh_token": refresh,
        },
        headers={"Accept": "application/json"},
    )
    if resp.status_code != 200:
        return False
    data = _safe_dict(resp.json())
    if not _safe_str(data.get("access_token")):
        return False
    _ML_TOKEN_STATE["access"] = _safe_str(data["access_token"])
    _ML_TOKEN_STATE["refresh"] = _safe_str(data.get("refresh_token")) or refresh
    path = _env("ML_TOKEN_FILE")
    if path:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(
                {"access_token": _ML_TOKEN_STATE["access"], "refresh_token": _ML_TOKEN_STATE["refresh"]}, f
            )
    return True


async def _ml_api_get(path: str, params: dict | None = None) -> tuple[str, object]:
    """GET autenticado na API ML. Retorna (status, dados|mensagem).

    status: ok | requer_token | blocked | removido | erro. Sem token ⇒ requer_token (nunca tenta evadir).
    """
    _ml_load_tokens()
    if not _ML_TOKEN_STATE["access"] and not await _ml_refresh():
        return "requer_token", None
    for tentativa in (0, 1):
        resp = await _ml_request(
            "GET",
            f"{ML_API}{path}",
            params=params,
            headers={"Authorization": f"Bearer {_ML_TOKEN_STATE['access']}", "Accept": "application/json"},
        )
        if resp.status_code == 200:
            return "ok", resp.json()
        if resp.status_code == 401 and tentativa == 0 and await _ml_refresh():
            continue
        if resp.status_code == 401:
            return "requer_token", "Token inválido ou expirado."
        if resp.status_code == 403:
            return "blocked", "Mercado Livre recusou (403): token sem permissão ou IP bloqueado."
        if resp.status_code in (404, 410):
            return "removido", None
        return "erro", f"HTTP {resp.status_code}"
    return "erro", "falha ao autenticar"


def _ml_attrs(item: dict) -> dict:
    out = {}
    for a in _safe_list(item.get("attributes"))[:_PROPS_MAX]:
        a = _safe_dict(a)
        nome, valor = _safe_str(a.get("name"), 100), _safe_str(a.get("value_name"))
        if nome and valor:
            out[nome] = valor
    return out


def _ml_spec_ram_gb(item: dict) -> int | None:
    """RAM do spec (verdade). O título mente: 'Capacidade total do módulo de memória RAM'."""
    for a in _safe_list(item.get("attributes")):
        a = _safe_dict(a)
        if "ram" in (_safe_str(a.get("name"), 100) or "").lower() or a.get("id") in ("RAM_MEMORY", "RAM"):
            m = re.search(r"\d+", _safe_str(a.get("value_name")) or "")
            if m:
                return int(m.group())
    return None


def _ml_modelo(item: dict) -> str | None:
    for a in _safe_list(item.get("attributes")):
        a = _safe_dict(a)
        if a.get("id") == "ALPHANUMERIC_MODEL":
            return _safe_str(a.get("value_name"), 100)
    return None


def _brl(v) -> str | None:
    """1234.5 -> 'R$ 1.234,50'."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return f"R$ {v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _ml_normalize(item: dict) -> dict:
    """Item da API ML → schema unificado + campos ML. Expõe spec x título lado a lado."""
    item = _safe_dict(item)
    ml_id = _safe_str(item.get("id")) or ""
    m = re.search(r"MLB-?(\d+)", ml_id)
    preco = item.get("price")
    titulo = _safe_str(item.get("title"))
    ram_spec = _ml_spec_ram_gb(item)
    ram_titulo = _extract_specs(titulo or "").get("ram_gb")
    seller = _safe_dict(item.get("seller"))
    addr = _safe_dict(item.get("address"))
    out = {
        "fonte": "ml",
        "id": int(m.group(1)) if m else None,
        "titulo": titulo,
        "preco": _brl(preco),
        "localizacao": ", ".join(
            x for x in (_safe_str(addr.get("city_name")), _safe_str(addr.get("state_name"))) if x
        )
        or None,
        "data": None,
        "url": _safe_str(item.get("permalink")),
        "imagem": _safe_str(item.get("thumbnail")),
        "condicao": _safe_str(item.get("condition")),
        "frete": "Frete grátis" if _safe_dict(item.get("shipping")).get("free_shipping") else None,
        "vendedor": _safe_str(seller.get("nickname")),
        "vendas": item.get("sold_quantity") if isinstance(item.get("sold_quantity"), int) else None,
        "atributos": _ml_attrs(item),
        "ram_gb_spec": ram_spec,
        "modelo_alfanumerico": _ml_modelo(item),
    }
    if ram_spec and ram_titulo and ram_spec != ram_titulo:
        out["aviso_ram"] = f"Título diz {ram_titulo}GB de RAM, mas o spec diz {ram_spec}GB (vale o spec)."
    return out


@mcp.tool(
    name="ml_buscar_anuncios",
    annotations={
        "title": "Buscar Anúncios no Mercado Livre Brasil",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def ml_buscar_anuncios(params: BuscarMLInput) -> str:
    """Busca anúncios no Mercado Livre Brasil.

    Args:
        params (BuscarMLInput):
            - query (str): Termo obrigatório.
            - preco_min/preco_max (Optional[int]): Faixa de preço em reais.
            - estado (Optional[str]): Filtro pós-scraping por sigla UF (heurística).
            - condicao (Optional[str]): 'novo' ou 'usado'.
            - pagina (int): Página (50 itens por página).

    Returns:
        str: JSON com lista de anúncios (titulo, preco, frete, atributos, url, imagem).
    """
    url, avisos = _build_ml_url(params)  # url web: só referência para o humano
    logger.info("ml_search query=%r condicao=%s pagina=%s", params.query, params.condicao, params.pagina)
    q: dict = {"q": params.query, "limit": 50, "offset": (params.pagina - 1) * 50}
    if params.preco_min is not None or params.preco_max is not None:
        q["price"] = f"{params.preco_min or 0}-{params.preco_max if params.preco_max is not None else '*'}"
    if params.condicao and params.condicao.lower() in ("novo", "usado"):
        q["ITEM_CONDITION"] = {"novo": "2230284", "usado": "2230581"}[params.condicao.lower()]
    if params.ordenar == OrdenarPor.PRECO_MENOR:
        q["sort"] = "price_asc"
    try:
        status, data = await _ml_api_get("/sites/MLB/search", q)
    except Exception as e:
        return json.dumps(
            {"fonte": "ml", "status": "erro", "erro": _handle_http_error(e)}, ensure_ascii=False
        )
    if status == "requer_token":
        return _ml_requer_token(data or "A API pública de busca do Mercado Livre exige um token OAuth.")
    if status != "ok":
        return json.dumps(
            {"fonte": "ml", "status": status, "erro": data, "url_busca": url}, ensure_ascii=False
        )
    d = _safe_dict(data)
    anuncios = [_ml_normalize(i) for i in _safe_list(d.get("results"))[:50]]
    if params.estado:
        uf = params.estado.upper()
        anuncios = [a for a in anuncios if a.get("localizacao") and uf in a["localizacao"].upper()]
    return json.dumps(
        {
            "fonte": "ml",
            "status": "ok",
            "total": _safe_dict(d.get("paging")).get("total", len(anuncios)),
            "pagina": params.pagina,
            "por_pagina": len(anuncios),
            "url_busca": url,
            "avisos": avisos,
            "anuncios": anuncios,
        },
        ensure_ascii=False,
        indent=2,
    )


# ---------------------------------------------------------------------------
# Mercado Livre — detalhe
# ---------------------------------------------------------------------------

_ALLOWED_ML_HOSTS = (
    ".mercadolivre.com.br",
    ".mercadolibre.com",
)


def _validar_url_ml(url: str) -> str:
    """SSRF guard para Mercado Livre."""
    try:
        p = urlparse(url)
    except Exception as e:
        raise ValueError(f"URL inválida: {e}") from None
    if p.scheme not in ("http", "https"):
        raise ValueError(f"Esquema não permitido: {p.scheme!r}. Use http(s).")
    host = (p.hostname or "").lower()
    if not host:
        raise ValueError("URL sem hostname.")
    if not any(host.endswith(h) for h in _ALLOWED_ML_HOSTS):
        raise ValueError(f"Hostname não permitido: {host!r}. Apenas *.mercadolivre.com.br/mercadolibre.com.")
    return url


class DetalheMLInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, validate_assignment=True, extra="forbid")
    url: str = Field(
        ...,
        min_length=20,
        description=(
            "URL completa do anúncio no Mercado Livre. Ex: 'https://produto.mercadolivre.com.br/MLB-1234-...'"
        ),
    )


@mcp.tool(
    name="ml_detalhe_anuncio",
    annotations={
        "title": "Obter Detalhes de Anúncio no Mercado Livre",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def ml_detalhe_anuncio(params: DetalheMLInput) -> str:
    """Obtém detalhes de um anúncio do Mercado Livre a partir da URL.

    Args:
        params (DetalheMLInput):
            - url (str): URL completa do anúncio.

    Returns:
        str: JSON com campos comuns (id, titulo, preco, url, fonte) + descricao,
        imagens, vendedor. Em caso de erro: {"erro": "..."}.
    """
    try:
        _validar_url_ml(params.url)
    except ValueError as e:
        return json.dumps(_erro(f"Erro de validação: {e}", "ml"), ensure_ascii=False)

    logger.info("ml_detail url=%s", params.url)
    m = re.search(r"MLB-?(\d+)", params.url)
    if not m:
        return json.dumps(_erro("Não foi possível extrair o id MLB da URL.", "ml"), ensure_ascii=False)
    item_id = f"MLB{m.group(1)}"
    try:
        status, item = await _ml_api_get(f"/items/{item_id}")
        if status == "requer_token":
            return _ml_requer_token(item or "A API do Mercado Livre exige um token OAuth.")
        if status != "ok":
            return json.dumps(
                {"fonte": "ml", "status": status, "erro": item, "url": params.url}, ensure_ascii=False
            )
        item = _safe_dict(item)
        d_status, desc = await _ml_api_get(f"/items/{item_id}/description")
        seller = {}
        if item.get("seller_id"):
            u_status, u = await _ml_api_get(f"/users/{item['seller_id']}")
            seller = _safe_dict(u) if u_status == "ok" else {}
    except Exception as e:
        return json.dumps(
            {"fonte": "ml", "status": "erro", "erro": _handle_http_error(e)}, ensure_ascii=False
        )
    item["seller"] = {"nickname": seller.get("nickname")}
    out = _ml_normalize(item)
    out.update(
        {
            "status": "ok",
            "descricao": (_safe_str(_safe_dict(desc).get("plain_text"), 2000) if d_status == "ok" else None),
            "garantia": _safe_str(item.get("warranty")),
            "vendedor_vendas": _safe_dict(
                _safe_dict(seller.get("seller_reputation")).get("transactions")
            ).get("completed"),
            "imagens": [
                _safe_str(_safe_dict(x).get("secure_url")) for x in _safe_list(item.get("pictures"))[:10]
            ],
            "url": params.url,
        }
    )
    return json.dumps(out, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Agregadores (Zoom / Buscapé) — única rota legítima p/ Magalu, Casas Bahia, Ponto, Amazon
# (esses sites bloqueiam IPs de datacenter; não tentamos burlar). Páginas públicas, UA honesto.
# ---------------------------------------------------------------------------
_AGG_BASE = {"zoom": "https://www.zoom.com.br", "buscape": "https://www.buscape.com.br"}
_AGG_UA = "mcp-brazil-marketplaces (+https://github.com/rodrigopg/mcp-brazil-marketplaces)"
_SPEC_COMPARE = (("chip", "chip"), ("ram_gb", "RAM (GB)"), ("ssd_gb", "SSD (GB)"))


class BuscarOfertasInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, validate_assignment=True, extra="forbid")

    query: str = Field(..., min_length=1, max_length=200, description="Ex: 'macbook air m4 24gb'.")
    fonte: Literal["zoom", "buscape"] = Field(default="zoom", description="Agregador a consultar.")
    preco_min: int | None = Field(default=None, ge=0)
    preco_max: int | None = Field(default=None, ge=0)
    limite_produtos: int = Field(
        default=3, ge=1, le=5, description="Quantos produtos do catálogo detalhar com ofertas."
    )


async def _agg_get(url: str, fonte: str) -> tuple[str, str | None]:
    """GET único, UA honesto, sem retry/evasão. Retorna (status, html|None): ok | blocked | removido | erro."""
    host = urlparse(_AGG_BASE[fonte]).hostname or ""
    try:
        async with _RateGate(url):
            async with httpx.AsyncClient(follow_redirects=True, timeout=REQUEST_TIMEOUT) as c:
                r = await c.get(url, headers={"User-Agent": _AGG_UA, "Accept": "text/html"})
    except (httpx.TimeoutException, httpx.TransportError):
        return "erro", None
    if (r.url.host or "") != host:
        return "erro", None  # redirecionou para fora do agregador (SSRF)
    if r.status_code in (403, 429, 503):
        return "blocked", None
    if r.status_code in (404, 410):
        return "removido", None
    if r.status_code != 200:
        return "erro", None
    return "ok", r.text[:MAX_HTML_BYTES]


def _agg_redux(html: str) -> dict:
    return _safe_dict(_safe_dict(_extract_next_data(html).get("props")).get("initialReduxState"))


def _agg_divergencias(nome_catalogo: str, nome_oferta: str) -> list[str]:
    """Títulos de catálogo são mal mapeados (oferta 'M2' sob catálogo 'M4'): compara chip/RAM/SSD."""
    sc, so = _extract_specs(nome_catalogo), _extract_specs(nome_oferta)
    return [
        f"{rot}: catálogo {sc[k]}, oferta {so[k]}"
        for k, rot in _SPEC_COMPARE
        if k in sc and k in so and sc[k] != so[k]
    ]


def _agg_ofertas(redux: dict, nome_catalogo: str) -> list[dict]:
    out = []
    for o in _safe_list(_safe_dict(redux.get("offers")).get("offerList"))[:_LIST_MAX]:
        o = _safe_dict(o)
        nome = _safe_str(o.get("name")) or ""
        div = _agg_divergencias(nome_catalogo, nome)
        out.append(
            {
                "loja": _safe_str(o.get("sellerName"), 100),
                "preco": _brl(o.get("price")),
                "nome_oferta": nome,  # sempre ao lado do título do catálogo
                "marketplace": bool(o.get("sellerIsMarketplace")),
                "parcelas": o.get("numParcels") if isinstance(o.get("numParcels"), int) else None,
                "specs_oferta": _extract_specs(nome),
                "titulo_divergente": bool(div),
                "avisos": div,
            }
        )
    unicos = {(o["loja"], o["preco"], o["nome_oferta"]): o for o in out}.values()  # mesma loja listada 2x
    return sorted(unicos, key=lambda x: (x["preco"] is None, _brl_num(x["preco"])))


def _brl_num(v: str | None) -> float:
    return float(v.replace("R$", "").replace(".", "").replace(",", ".")) if v else 0.0


@mcp.tool(
    name="agregador_buscar_ofertas",
    annotations={
        "title": "Comparar ofertas de lojas via Zoom/Buscapé",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def agregador_buscar_ofertas(params: BuscarOfertasInput) -> str:
    """Busca produtos no Zoom ou Buscapé e lista a oferta de cada loja (Magalu, Amazon, Casas Bahia...).

    Rota legítima para lojas que bloqueiam este servidor. Cada oferta traz o próprio nome
    (`nome_oferta`) ao lado do título do catálogo: catálogos misturam modelos, então confira
    `titulo_divergente`/`avisos` antes de confiar no preço. Preços são de vitrine: confirme na loja.

    Returns:
        JSON: fonte, status (ok | blocked | removido | erro | layout), produtos[] com
        titulo_catalogo, preco_catalogo, url, lojas, ofertas[] e status por produto.
    """
    fonte = params.fonte
    base = _AGG_BASE[fonte]
    logger.info("agregador query=%r fonte=%s", params.query, fonte)
    status, html = await _agg_get(f"{base}/search?q={quote(params.query)}", fonte)
    if status != "ok":
        dica = (
            "O agregador bloqueou a consulta; abra a busca no navegador."
            if status == "blocked"
            else "Falha ao consultar o agregador."
        )
        return json.dumps({"fonte": fonte, "status": status, "erro": dica}, ensure_ascii=False)
    try:
        hits = _safe_list(_safe_dict(_agg_redux(html).get("hits")).get("hits"))
    except ValueError:
        return json.dumps(
            {"fonte": fonte, "status": "layout", "erro": "Página sem dados estruturados (layout mudou)."},
            ensure_ascii=False,
        )
    produtos = []
    for h in hits:
        h = _safe_dict(h)
        preco, nome, path = h.get("price"), _safe_str(h.get("name")) or "", _safe_str(h.get("url")) or ""
        if not path.startswith("/") or h.get("type", "product") != "product":
            continue
        if isinstance(preco, (int, float)) and (
            (params.preco_min is not None and preco < params.preco_min)
            or (params.preco_max is not None and preco > params.preco_max)
        ):
            continue
        produtos.append(
            {
                "titulo_catalogo": nome,
                "preco_catalogo": _brl(preco),
                "specs_catalogo": _extract_specs(nome),
                "lojas": [
                    _safe_str(_safe_dict(m).get("name"), 100) for m in _safe_list(h.get("merchants"))[:10]
                ],
                "url": base + path.split("?")[0],
            }
        )
    for prod in produtos[: params.limite_produtos]:
        p_status, p_html = await _agg_get(prod["url"], fonte)
        prod["status"] = p_status
        if p_status != "ok":
            continue
        try:
            prod["ofertas"] = _agg_ofertas(_agg_redux(p_html), prod["titulo_catalogo"])
        except ValueError:
            prod["status"] = "layout"
    return json.dumps(
        {
            "fonte": fonte,
            "status": "ok",
            "total_catalogo": len(produtos),
            "produtos": produtos[: params.limite_produtos],
            "avisos": [
                "Preços de vitrine do agregador; confirme na loja. Compare nome_oferta com titulo_catalogo."
            ],
        },
        ensure_ascii=False,
        indent=2,
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Entry point para `mcp-brazil-marketplaces` console_script e `python -m mcp_brazil_marketplaces`."""
    mcp.run()


if __name__ == "__main__":
    main()
