# Changelog

## 0.1.0 — 2026-11-16
- Initial release: `page_audit`, `ai_visibility`, `sentiment`, `full_analysis` tools and matching prompts.
- Agent Plugin v1 package (`plugin.json` / `mcp.json` / `skills/geo-analyzer-mcp`): install with
  `hermes plugins install clance1/geo-analyzer-mcp`; the server launches from the installed plugin
  directory via `uvx`, and the shipped skill teaches the agent the run → poll → report workflow.
- Run history (SQLite + per-run Markdown/HTML/JSON), trends, run comparison, alerts, webhooks.
- stdio and Streamable HTTP transports, bearer auth, REST hooks, CLI for cron/CI, Docker image.
- Page audit: throttled per-bot checks with 429 retry; Cloudflare/Vercel/Akamai challenge detection;
  content checks suppressed when a challenge page is served; compares visible text to visible text
  (fixes the old raw-HTML-vs-rendered-text parity bug).
