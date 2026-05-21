# Política de segurança

## Reportando uma vulnerabilidade

Se você encontrou uma vulnerabilidade de segurança neste projeto, **não abra uma issue pública**. Em vez disso:

1. Use o [GitHub Security Advisories](https://github.com/rodrigopg/olx-mcp/security/advisories/new) (**preferencial**) — canal privado integrado ao repositório.
2. Ou envie e-mail para **rodrigo.pgoncalves@totvs.com.br** com o assunto `[security] olx-mcp`.

Inclua no report:

- Descrição da vulnerabilidade
- Passos para reproduzir
- Impacto estimado (DoS, SSRF, RCE, leak, etc.)
- Versão afetada do `olx-mcp`
- Sugestão de correção, se houver

## Tempo de resposta

- **Acknowledgment:** até 5 dias úteis
- **Triagem inicial:** até 10 dias úteis
- **Patch:** depende da severidade; críticas em até 14 dias

## Escopo

Vulnerabilidades em escopo:

- SSRF, RCE, path traversal no servidor MCP
- Injeção em parâmetros de tools
- ReDoS, OOM, ou DoS via input malicioso
- Vazamento de credenciais ou dados sensíveis
- Falhas de validação em URLs aceitas pelas tools

**Fora de escopo:**

- Bloqueios anti-bot da OLX/Mercado Livre (essa é parte do design)
- Scraping retornar dados desatualizados ou parciais
- Issues em dependências (reporte upstream — `mcp`, `httpx`, `pydantic`)

## Versões suportadas

Apenas a versão `MAJOR.MINOR` mais recente recebe patches de segurança. Atualize antes de reportar.

## Disclosure

Após o patch publicado, o report é divulgado via GitHub Security Advisory com crédito ao reporter (a menos que seja solicitado anonimato).
