# Roadmap

Roteiro vivo do projeto. Cada item é uma issue rastreável; milestones organizam por release. Veja status atualizado em [milestones do GitHub](https://github.com/rodrigopg/mcp-brazil-marketplaces/milestones).

## Como ler

- **v0.4 — Discoverability**: ganhar visibilidade externa
- **v0.5 — Mais marketplaces**: justificar o nome plural
- **v0.6 — Engenharia/observability**: robustez e operações
- **v1.0 — Maturidade**: features ricas + docs completas
- **future — Ideias**: candidatos sem prazo definido

Itens prefixados `[R-N]` no título da issue.

## v0.4 — Discoverability

Objetivo: package descoberto organicamente por usuários MCP.

- [#41 R-1](https://github.com/rodrigopg/mcp-brazil-marketplaces/issues/41) — Submeter ao MCP registry (mcp.so, Smithery.ai, modelcontextprotocol/servers)
- [#42 R-2](https://github.com/rodrigopg/mcp-brazil-marketplaces/issues/42) — Demo/blog post + Reddit /r/LocalLLaMA + Twitter
- [#43 R-3](https://github.com/rodrigopg/mcp-brazil-marketplaces/issues/43) — Vídeo demo curto (2min)
- [#44 R-4](https://github.com/rodrigopg/mcp-brazil-marketplaces/issues/44) — README hero GIF
- [#67 R-27](https://github.com/rodrigopg/mcp-brazil-marketplaces/issues/67) — Diretório `examples/` com receitas

## v0.5 — Mais marketplaces

Objetivo: cobrir 3+ marketplaces brasileiros, justificando o nome.

- [#45 R-5](https://github.com/rodrigopg/mcp-brazil-marketplaces/issues/45) — Adicionar Enjoei
- [#46 R-6](https://github.com/rodrigopg/mcp-brazil-marketplaces/issues/46) — Adicionar Shopee BR
- [#47 R-7](https://github.com/rodrigopg/mcp-brazil-marketplaces/issues/47) — Adicionar Magalu / Americanas
- [#49 R-9](https://github.com/rodrigopg/mcp-brazil-marketplaces/issues/49) — Tool `comparar_precos` cross-marketplace

## v0.6 — Engenharia/observability

Objetivo: rodar em produção sem surpresas; detectar regressões cedo.

- [#52 R-12](https://github.com/rodrigopg/mcp-brazil-marketplaces/issues/52) — Smoke test pós-release
- [#53 R-13](https://github.com/rodrigopg/mcp-brazil-marketplaces/issues/53) — Job de integração no CI (semanal + workflow_dispatch)
- [#54 R-14](https://github.com/rodrigopg/mcp-brazil-marketplaces/issues/54) — Dependabot/Renovate
- [#55 R-15](https://github.com/rodrigopg/mcp-brazil-marketplaces/issues/55) — Coverage badge + threshold
- [#58 R-18](https://github.com/rodrigopg/mcp-brazil-marketplaces/issues/58) — Endpoint `/metrics` Prometheus opcional
- [#59 R-19](https://github.com/rodrigopg/mcp-brazil-marketplaces/issues/59) — Schema diff detector
- [#63 R-23](https://github.com/rodrigopg/mcp-brazil-marketplaces/issues/63) — Rotacionar tokens entire-account
- [#64 R-24](https://github.com/rodrigopg/mcp-brazil-marketplaces/issues/64) — SAST no CI (Bandit + Semgrep)
- [#65 R-25](https://github.com/rodrigopg/mcp-brazil-marketplaces/issues/65) — SBOM em cada release

## v1.0 — Maturidade

Objetivo: doc completa, features-flagship, código pronto para divulgação ampla.

- [#50 R-10](https://github.com/rodrigopg/mcp-brazil-marketplaces/issues/50) — Filtros avançados (distância lat/lon, data range, condição)
- [#57 R-17](https://github.com/rodrigopg/mcp-brazil-marketplaces/issues/57) — Cache em disco/Redis opcional
- [#62 R-22](https://github.com/rodrigopg/mcp-brazil-marketplaces/issues/62) — Fuzz tests com payloads malformados
- [#66 R-26](https://github.com/rodrigopg/mcp-brazil-marketplaces/issues/66) — Site de documentação (MkDocs Material + ReadTheDocs)
- [#69 R-29](https://github.com/rodrigopg/mcp-brazil-marketplaces/issues/69) — CONTRIBUTING.md

## future — Ideias

Itens válidos sem prazo definido; promover a milestone quando justificar.

- [#48 R-8](https://github.com/rodrigopg/mcp-brazil-marketplaces/issues/48) — Adicionar Amazon BR (anti-bot pesado)
- [#51 R-11](https://github.com/rodrigopg/mcp-brazil-marketplaces/issues/51) — Tool `monitorar_anuncio` com webhook
- [#56 R-16](https://github.com/rodrigopg/mcp-brazil-marketplaces/issues/56) — Fallback Pyodide/WASM para parser
- [#60 R-20](https://github.com/rodrigopg/mcp-brazil-marketplaces/issues/60) — Telemetria opt-in
- [#61 R-21](https://github.com/rodrigopg/mcp-brazil-marketplaces/issues/61) — Detecção de anúncios duplicados cross-marketplace
- [#68 R-28](https://github.com/rodrigopg/mcp-brazil-marketplaces/issues/68) — Tutorial em vídeo "Como criar seu próprio MCP em 30min"
- [#70 R-30](https://github.com/rodrigopg/mcp-brazil-marketplaces/issues/70) — GitHub Sponsors
- [#71 R-31](https://github.com/rodrigopg/mcp-brazil-marketplaces/issues/71) — Versão Pro com features avançadas

## Contribuindo

Qualquer item desta lista é candidato a contribuição. Veja `CONTRIBUTING.md` (em construção, [#69](https://github.com/rodrigopg/mcp-brazil-marketplaces/issues/69)) e o fluxo obrigatório em `CLAUDE.md`: **toda mudança = issue + teste + lint verde**.

Sugestões novas? Abra uma issue com o prefixo `[R-N+1]` (próximo número livre) e proponha milestone.
