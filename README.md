# GEO Analyzer MCP

A self-hosted [Model Context Protocol](https://modelcontextprotocol.io) server for **Generative Engine Optimization**: measure how a company shows up in ChatGPT, Claude, Gemini (and optionally Perplexity), and get concrete fixes.

| MCP prompt | Tool | What it does |
|---|---|---|
| `/page-audit` | `page_audit` | Tests a URL the way AI crawlers see it: robots.txt and real HTTP access for 16 AI user-agents, WAF/bot-challenge detection (Cloudflare, Vercel, Akamai), content parity, JS-only content, indexability, JSON-LD, sitemap, `llms.txt`. Score 0-100 + copy-paste fixes. |
| `/ai-visibility` | `ai_visibility` | Runs your preset queries **and stable variants** through each engine with web search on. Stores full answer text + native citations. Reports mention / citation / recommendation rate, rank vs competitors, share of voice, most-cited domains, citation-outreach opportunities, and lost queries. |
| `/sentiment` | `sentiment` | Asks engines directly about the company ("How is {company} at {service}?", reputation and head-to-head prompts). Scores recognition, sentiment −100..100, aspects (quality, price, timeline…), praise, concerns, and claims that contradict your ground-truth facts. |
| `/full-analysis` | `full_analysis` | All three, plus a GEO score (0-100), deltas vs the previous run, alerts, a prioritized action plan, campaign guidance and a Markdown/HTML report. |

Those are MCP prompt names, not Claude Code slash commands. In Claude Code they appear in the `/`
menu as `/geo:page-audit (MCP)` and run as `/mcp__geo__page-audit`; in Hermes they are listed by
`mcp__geo__list_prompts`. Every prompt maps 1:1 onto the tool of the same name.

Supporting tools: `get_job`, `get_run`, `list_runs`, `get_trends`, `compare_runs`, `list_projects`, `get_project`, `server_status`.

Every run is persisted (SQLite index + `report.md`, `report.html`, `data.json`), so agents can talk about **trends**, not one-off answers.

---

## Contents

- [Quick start](#quick-start)
- [Install as a Hermes Agent Plugin](#install-as-a-hermes-agent-plugin)
- [Configuration](#configuration)
- [Project presets](#project-presets)
- [Connect a client](#connect-a-client)
- [Automation (cron, CI, agents, webhooks)](#automation)
- [Self-hosting over HTTP (Docker + Cloudflare Tunnel)](#self-hosting-over-http)
- [What the numbers mean](#what-the-numbers-mean)
- [Configuration reference](#configuration-reference)
- [Security](#security)
- [Development](#development)
- [License](#license)

---

## Quick start

Requires Python 3.11+.

```bash
pip install "git+https://github.com/clance1/geo-analyzer-mcp"   # or: pip install -e . from a clone
geo-mcp init        # writes ~/.geo-mcp/projects.yaml and ~/.geo-mcp/.env
# add ANTHROPIC_API_KEY / OPENAI_API_KEY / GOOGLE_API_KEY to ~/.geo-mcp/.env
geo-mcp doctor      # shows which engines are configured
geo-mcp run page-audit --project local-construction --format md
```

The package is **not published on PyPI** — install it from git (or a clone) as shown above.

Prefer not to install anything? `uvx` runs the same CLI straight from git — prefix any command with
`uvx --from git+https://github.com/clance1/geo-analyzer-mcp geo-mcp`:

```bash
uvx --from git+https://github.com/clance1/geo-analyzer-mcp geo-mcp doctor
```

`local-construction` is a placeholder preset — `init` copies it verbatim. It points at
`https://www.localconstruction.example.com`, a reserved example domain that does not resolve, so
nothing is audited until you edit it. Replace the slug, company, website, queries and competitors
with your own in `~/.geo-mcp/projects.yaml` before you run anything for real.

Any subset of keys works; engines without a key are skipped with a warning. The page audit needs no keys at all.

Optional JS rendering for the page audit (detects content that only appears after JavaScript):

```bash
pip install "geo-analyzer-mcp[render] @ git+https://github.com/clance1/geo-analyzer-mcp" && playwright install chromium
```

## Install as a Hermes Agent Plugin

```bash
hermes plugins install clance1/geo-analyzer-mcp
hermes plugins enable geo-analyzer-mcp
```

The plugin launches the server with `uvx` and registers the MCP server as `geo`, so the tools show
up as `mcp__geo__page_audit`, `mcp__geo__ai_visibility`, … It also ships the `geo-analyzer-mcp`
skill, which teaches the agent the list-projects → run → poll → report workflow and the `dry_run`
cost guard.

`uvx` builds the **installed plugin directory** into a throwaway environment on first run and
downloads the package's Python dependencies from PyPI, so the code that runs is exactly the commit
Hermes installed — for a catalog install, the reviewed, pinned commit.

Portable packages install **disabled** — `hermes plugins enable` turns the plugin on (`hermes plugins list`
shows the exact key). An interactive `hermes plugins install` offers to enable it right away; decline
and run `hermes plugins enable` yourself. Requires [`uvx`](https://docs.astral.sh/uv/) on `PATH`. The
plugin ships no credentials: keys and presets come from `~/.geo-mcp/` (next section).

The plugin runs the server for you but does not install the `geo-mcp` command, so `geo-mcp init` is
not on your `PATH`. Write `~/.geo-mcp/projects.yaml` and `~/.geo-mcp/.env` by hand, or run `init`
once from a clone (`uvx --from git+https://github.com/clance1/geo-analyzer-mcp geo-mcp init`).

## Configuration

```bash
geo-mcp init        # writes ~/.geo-mcp/projects.yaml and ~/.geo-mcp/.env
```

- **`~/.geo-mcp/.env`** — engine API keys: `ANTHROPIC_API_KEY` (Claude), `OPENAI_API_KEY` (ChatGPT),
  `GOOGLE_API_KEY` (Gemini), optionally `PERPLEXITY_API_KEY`. Any subset works; engines without a
  key are skipped with a warning. The page audit needs no keys at all.
- **`~/.geo-mcp/projects.yaml`** — one preset per company: website, aliases, queries, competitors,
  core services, ground-truth facts and alert thresholds. Copy the shipped example
  [`config/projects.yaml`](config/projects.yaml) into this file and edit **your copy** — the
  shipped file says not to edit it in place. The Docker image is the exception: it mounts
  `config/projects.yaml` and sets `GEO_MCP_PROJECTS` to it, so edit that file for container runs.

`geo-mcp doctor` prints which engines are configured. Every other setting is an environment
variable — see [Configuration reference](#configuration-reference).

## Project presets

`projects.yaml` holds one entry per company. Only `company` and `website` are required.

```yaml
projects:
  local-construction:
    company: Local Construction Co.
    website: https://www.localconstruction.example.com
    aliases: ["Local Construction"]
    location: Springfield
    core_services: [General contracting, Kitchen remodeling, Bathroom remodeling]
    accolades: []                 # awards the company has won
    facts:                      # ground truth used to flag inaccurate AI claims
      - Service area is Springfield.
    competitors:
      - name: Example Builders Inc.
        website: https://www.example-builders.example.com
      - https://www.another-contractor.example.com  # shorthand works too
    generic_terms: ["construction company", "general contractor"]   # never count these as a brand mention
    queries:
      - Best general contractor Springfield
      - query: Kitchen remodeling contractor Springfield
        variants: ["home renovation companies near me"]   # optional: pin your own variants
    variants_per_query: 2       # generated once, cached, reused → like-for-like trends
    pages: []                    # extra URLs for page audits
    engines: [claude, chatgpt, gemini]
    alerts: { visibility_drop_pts: 10, sentiment_drop_pts: 15, min_ai_bots_allowed: 6 }
```

Presets are resolved in this order:

1. `GEO_MCP_PROJECTS` — an **override**, not a fallback. If that path does not exist you get
   *no* presets, and the first tool call fails with `Available: (none configured)`.
2. `config/projects.yaml` **relative to the current working directory**, if that file exists.
3. `~/.geo-mcp/projects.yaml` — used only when step 2 finds nothing.

Step 2 is CWD-relative, so starting the server from a checkout of this repo silently prefers
the repo's example preset over your edited `~/.geo-mcp/projects.yaml`. Start the server from any
other directory, or point `GEO_MCP_PROJECTS` at the file you want.

One exception to step 1: if `GEO_MCP_PROJECTS` is a **relative** path that does not exist, the
server still falls back to `~/.geo-mcp/projects.yaml` (and to `$GEO_MCP_DATA_DIR/projects.yaml`
if you set that). Only an **absolute** `GEO_MCP_PROJECTS` is used exactly as given, with no
fallback — which is why the Claude Desktop example above sets none.

Tools also accept ad-hoc `company` + `website` (+ `queries`, `competitors`) for one-off checks without a preset.

**Variants are stable on purpose.** They're generated once per query and cached in `~/.geo-mcp/variants/`, so week-over-week changes reflect the engines, not new wording. Pass `regenerate_variants=true` to refresh.

## Connect a client

Copy-paste snippets for every client are in [`examples/clients/`](examples/clients/).

### Claude Code

```bash
# local (stdio)
claude mcp add geo -- geo-mcp serve
# remote (your self-hosted HTTP endpoint)
claude mcp add --transport http geo https://geo-mcp.example.com/mcp --header "Authorization: Bearer $GEO_MCP_TOKEN"
```

Prompts appear in the `/` menu as `/geo:full-analysis (MCP)`; typing `/mcp__geo__full-analysis local-construction` also works ([Claude Code docs](https://code.claude.com/docs/en/mcp-servers.md)).

### Claude Desktop

`claude_desktop_config.json` (Windows: `%APPDATA%\Claude\claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "geo": {
      "command": "geo-mcp",
      "args": ["serve"]
    }
  }
}
```

No `env` block is needed: Claude Desktop launches the server from an arbitrary working directory,
so the server finds `~/.geo-mcp/projects.yaml` on its own. Keys are read from `~/.geo-mcp/.env`.
If `geo-mcp` isn't on PATH, use `"command": "python", "args": ["-m", "geo_mcp", "serve"]`.

### Cursor

`.cursor/mcp.json` — local (stdio):

```json
{ "mcpServers": { "geo": { "command": "geo-mcp", "args": ["serve"] } } }
```

Or remote (your self-hosted HTTP endpoint):

```json
{ "mcpServers": { "geo-remote": { "url": "https://geo-mcp.example.com/mcp",
  "headers": { "Authorization": "Bearer ${env:GEO_MCP_TOKEN}" } } } }
```

The shipped [`examples/clients/cursor-mcp.json`](examples/clients/cursor-mcp.json) has both, under
different keys — `geo` for local, `geo-remote` for remote. Delete whichever one you don't use; a
single `mcp.json` cannot hold two servers under the same key.

### OpenAI Codex

`~/.codex/config.toml` ([Codex MCP docs](https://learn.chatgpt.com/docs/extend/mcp)):

```toml
[mcp_servers.geo]
command = "geo-mcp"
args = ["serve"]
tool_timeout_sec = 120

# or remote:
# [mcp_servers.geo]
# url = "https://geo-mcp.example.com/mcp"
# bearer_token_env_var = "GEO_MCP_TOKEN"
```

### Anything else

Any client that speaks MCP over stdio or Streamable HTTP works. HTTP endpoint: `POST /mcp`, auth
`Authorization: Bearer <GEO_MCP_TOKEN>` or `X-API-Key: <GEO_MCP_TOKEN>`.

### Long runs and client timeouts

Run tools wait up to `wait_seconds` (default 50s; 20s for `full_analysis`). If the run isn't done, they return `status: "running"` and a `run_id`; the agent polls `get_job(run_id, wait_seconds=50)`. Progress notifications are sent while the client is waiting. This keeps every client under its tool timeout, regardless of how many queries you run.

Use `dry_run: true` on `ai_visibility`, `sentiment` and `full_analysis` to see every prompt and the
exact number of engine calls first. `page_audit` has no `dry_run` parameter — it calls no engines
and is free to run.

### Inspect history from the CLI

```bash
geo-mcp history --project local-construction          # recent runs, newest first
geo-mcp trends  --project local-construction --kind full-analysis
```

## Automation

### 1. Plain cron (recommended for scheduled reporting)

No agent needed — the CLI runs the same code path and writes the same history:

```cron
# Mondays 07:00 — full analysis, alert on regressions
0 7 * * 1  cd /opt/geo-mcp && geo-mcp run full-analysis -p local-construction -q --out /srv/geo-reports --fail-on-alert
# Daily 06:30 — crawlability check
30 6 * * *  geo-mcp run page-audit -p local-construction -q
```

`--fail-on-alert` exits with code 3 when a run raised alerts, so cron mail / CI can flag it. Set `GEO_WEBHOOK_URL` to post a Slack-compatible summary on every completed run. More examples: [`examples/crontab`](examples/crontab), [`examples/systemd/`](examples/systemd/), Windows Task Scheduler in [`examples/windows-task.ps1`](examples/windows-task.ps1).

### 2. GitHub Actions

[`examples/github-actions/geo-weekly.yml`](examples/github-actions/geo-weekly.yml) runs the full analysis weekly, uploads the report as an artifact and fails the job on alerts. History lives in the runner's data dir, so for trends either point `GEO_MCP_DATA_DIR` at a cached path (shown) or call your self-hosted server's REST hook instead.

### 3. AI harnesses (agent writes the narrative)

```bash
# Claude Code, headless
claude -p "/mcp__geo__full-analysis local-construction" --allowedTools "mcp__geo__*" > weekly-brief.md
# Codex
codex exec "Use the geo MCP server: run full_analysis for local-construction, poll get_job until complete, \
  then write a one-page brief for the owner with the top 5 actions."
```

### 4. REST hooks (n8n, Zapier, Make, any scheduler)

When served over HTTP, the same runs are available as plain JSON endpoints (bearer auth):

```bash
curl -X POST https://geo-mcp.example.com/api/run/full-analysis \
  -H "Authorization: Bearer $GEO_MCP_TOKEN" -H "Content-Type: application/json" \
  -d '{"project": "local-construction"}'
# → 202 {"run_id": "...", "status": "running"}
curl -H "Authorization: Bearer $GEO_MCP_TOKEN" https://geo-mcp.example.com/api/runs/<run_id>
```

| Endpoint | |
|---|---|
| `POST /api/run/{page-audit,ai-visibility,sentiment,full-analysis}` | body: `project`, any tool params, optional `wait_seconds` |
| `GET /api/runs?project=&kind=&limit=` | run index with metrics |
| `GET /api/runs/{run_id}` | compact summary |
| `GET /reports/{run_id}` | HTML report (`?format=md` for Markdown; `?token=` accepted for browser links) |
| `GET /healthz` | public health check |

## Self-hosting over HTTP

```bash
cp .env.example .env              # add API keys + GEO_MCP_TOKEN
docker compose up -d --build      # serves http://127.0.0.1:8765/mcp
```

The container reads `config/projects.yaml` from the repo (mounted read-only at `/app/config`),
**not** `~/.geo-mcp/projects.yaml` — edit the repo copy for container runs, or set
`GEO_MCP_PROJECTS` to a mounted path of your own.

The compose file binds to localhost only. To reach it from cloud agents, put it behind your existing **Cloudflare Tunnel** — add an ingress rule:

```yaml
ingress:
  - hostname: geo-mcp.example.com
    service: http://geo-mcp:8765     # if cloudflared shares the compose network; else http://localhost:8765
```

…and set `GEO_MCP_ALLOWED_HOSTS=geo-mcp.example.com` and `GEO_PUBLIC_BASE_URL=https://geo-mcp.example.com` so DNS-rebinding protection accepts the tunnel hostname and webhook messages link to reports. The server refuses to bind a non-localhost address without `GEO_MCP_TOKEN` (override with `--insecure` only behind another auth layer such as Cloudflare Access).

**Forwarded headers.** `X-Forwarded-For` / `-Proto` / `-Host` are only believed from the proxy's own address. Set `GEO_MCP_TRUSTED_PROXIES` to the address(es) or CIDR your tunnel uses (e.g. `127.0.0.1` when cloudflared runs on the same host, `172.16.0.0/12` for the compose network). With the variable unset the headers are ignored entirely, so no remote client can spoof its address or the scheme in the access log.

## What the numbers mean

| Metric | Definition |
|---|---|
| **AI Visibility Index** (0-100) | 50% mention rate + 30% own-domain citation rate + 20% recommendation rate, over all usable answers |
| Mention rate | % of answers naming the brand (company, aliases, domain; `generic_terms` excluded) |
| Citation rate | % of answers citing your domain (any citation kind) |
| Recommend rate | % of answers framing you as a top recommendation or recommended option (judge model; falls back to rank 1) |
| Avg position | Order of first appearance among you + named competitors, when you appear |
| Share of voice | Your answer mentions ÷ mentions of you + named competitors |
| Citation opportunities | Third-party domains cited on answers where you were **absent** — listing / PR / outreach targets |
| **Sentiment index** (−100..100) | Mean sentiment of answers that **recognize** the company |
| Recognition rate | % of direct questions where the engine shows specific knowledge of the company |
| **Page score** (0-100) | Weighted: AI access 35%, content 25%, structured data 15%, indexability 15%, discovery 10% |
| **GEO score** (0-100) | 40% visibility index + 30% page score + 30% sentiment, where sentiment = (index+100)/2 × recognition rate. Weights re-normalize if a section is skipped. |

Citation kinds: `inline` = attached to a specific claim, `source` = retrieved during search, `in_text` = URL in the answer body.

Engine answers are stochastic and location/personalization-dependent. Treat single runs as samples; use `get_trends` / `compare_runs` and act on persistent patterns.

**Page audit caveat:** the crawler test spoofs user-agents from your server's IP. Firewalls that verify real bots by IP (Cloudflare Verified Bots, Vercel) may admit the genuine crawlers even when the spoofed request is challenged — confirm in your firewall logs before changing rules.

## Configuration reference

All settings are environment variables (also read from `./.env` and `~/.geo-mcp/.env`). See [`.env.example`](.env.example).

| Variable | Default | |
|---|---|---|
| `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GOOGLE_API_KEY`, `PERPLEXITY_API_KEY` | — | Any subset |
| `GEO_DEFAULT_ENGINES` | `claude,chatgpt,gemini` | Used when the project doesn't list `engines` |
| `GEO_CLAUDE_MODEL` / `GEO_CHATGPT_MODEL` / `GEO_GEMINI_MODEL` / `GEO_PERPLEXITY_MODEL` | `claude-sonnet-5-5` / `gpt-6.1-sol` / `gemini-3.8-flash` / `sonar` | Pin to the models your audience actually uses |
| `GEO_CLAUDE_WEB_SEARCH_TOOL` | `web_search_20260318` | Falls back to older tool versions automatically |
| `GEO_JUDGE_PROVIDER` | `auto` | `anthropic` \| `openai` \| `gemini` \| `none` (lexical fallback) |
| `GEO_ANALYST_MODEL` | `claude-sonnet-5-5` | Writes the full-analysis brief |
| `GEO_MAX_QUERIES_PER_RUN` | `60` | Hard cap on prompts per engine per run (cost guard) |
| `GEO_ENGINE_CONCURRENCY` / `GEO_ENGINE_TIMEOUT_S` / `GEO_MAX_RETRIES` | `4` / `120` / `4` | |
| `GEO_MCP_DATA_DIR` | `~/.geo-mcp` | Run history, reports, variant cache |
| `GEO_MCP_PROJECTS` | `config/projects.yaml` | Absolute path to the presets file; overrides the lookup entirely |
| `GEO_MCP_HOST` / `GEO_MCP_PORT` | `127.0.0.1` / `8765` | HTTP transport |
| `GEO_MCP_TOKEN` | — | Bearer token for HTTP (required for non-localhost binds) |
| `GEO_MCP_ALLOWED_HOSTS` | — | Public hostnames (tunnel/reverse proxy), comma-separated |
| `GEO_PUBLIC_BASE_URL` | — | Used for report links in webhooks |
| `GEO_WEBHOOK_URL` | — | Slack-compatible POST on run completion (includes alerts) |
| `GEO_ALLOW_PRIVATE_TARGETS` | `false` | Allow page audits of private/LAN addresses |
| `GEO_MCP_TRUSTED_PROXIES` | — | Peers whose `X-Forwarded-*` headers may be believed (IP or CIDR, comma-separated). Unset = the headers are ignored |

## Security

- **HTTP auth**: constant-time bearer-token check on every route except `/healthz`. Generate a token with `python -c "import secrets; print(secrets.token_urlsafe(32))"`. A report link may carry the token as `?token=…` so it opens in a plain browser; the token is dropped from the request as soon as it has been read, so it is never written to the access log.
- **SSRF guard**: page audits refuse private, loopback, link-local, CGNAT (RFC 6598) and cloud-metadata addresses on every request *and every redirect hop*. The hostname is resolved **again at connect time** and the socket is opened to that validated address only — so a name that answers with a public IP during the check and a private IP at connect time (DNS rebinding) still cannot reach an internal host. Only the socket target changes: the URL, the `Host` header, TLS SNI and certificate verification stay on the hostname.
- **Report rendering**: the HTML report is built from untrusted text (audited URLs, engine answers, model briefs, finding snippets). Those values are tag-stripped and escaped where they are interpolated, the generated HTML is then sanitised with `nh3` against an allowlist of the report's own tags, and the page is served with `Content-Security-Policy: default-src 'none'` — no script can run in a report, and the JSON-LD fix snippet stays copy-pasteable as text.
- **DNS-rebinding protection** is on for localhost binds and for any host listed in `GEO_MCP_ALLOWED_HOSTS`.
- **Secrets** are only read from the environment / `.env`; `server_status` reports which keys are set, never their values. Keep `.env` out of git (it's in `.gitignore`).
- **Cost control**: `dry_run`, `GEO_MAX_QUERIES_PER_RUN`, per-engine concurrency limits, and usage counters in each run's data.

## Development

```bash
pip install -e ".[dev]"
pytest -q          # fake engines + local HTTP fixtures; no network or keys needed
ruff check src tests
```

Add an engine by subclassing `geo_mcp.engines.Engine` (implement `_ask`) and registering it in `engines/__init__.py`; tests can inject engines with `register_engine()`.

## License

MIT
