---
name: geo-analyzer-mcp
description: "AI search visibility, citations, sentiment and crawlability."
version: 0.1.0
author: Carson Lance (clance1), Hermes Agent
license: MIT
compatibility: "Hermes with Agent Plugins v1 support, Python 3.11+ and uvx on PATH. Engine API keys live in ~/.geo-mcp/.env and project presets in ~/.geo-mcp/projects.yaml."
---

# GEO Analyzer — geo MCP server

The `geo` MCP server measures how a company shows up in AI answer engines (ChatGPT, Claude,
Gemini, optionally Perplexity) and how AI crawlers can reach its pages. It returns scores,
per-engine answers with citations, deltas against previous runs, alerts and a Markdown/HTML report.

Tools are exposed by the MCP server named `geo`, so they appear to the model as
`mcp__geo__page_audit`, `mcp__geo__ai_visibility`, … (bare names below).

## When to Use

- "How does X show up in ChatGPT / Claude / Gemini?" → `ai_visibility`
- "What's our share of voice in AI answers / who cites us?" → `ai_visibility`
- "Is my site crawlable by AI bots? Why don't I get cited?" → `page_audit`
- "What do AI engines think of us / our reputation?" → `sentiment`
- "Give me a full GEO report" / "What's our GEO score?" → `full_analysis`
- "Is it getting worse / better than last month?" → `get_trends`, `compare_runs`, `list_runs`
- "Which projects are configured?" → `list_projects`, `get_project`
- "Are the engine keys configured?" → `server_status`

Do **not** use for classic SEO keyword research, backlink audits or traffic analytics — this server
measures answer-engine visibility and AI-crawler access only.

## Prerequisites

- **Presets**: `geo-mcp init` writes `~/.geo-mcp/projects.yaml` (one entry per company: website,
  aliases, queries, competitors, core services, ground-truth facts, alert thresholds). If the
  user cannot run `geo-mcp init`, have them create `~/.geo-mcp/projects.yaml` by hand — one
  `projects:` entry per company, each with at least `company` and `website`. A Hermes-plugin
  install has no checkout on disk, so do not point the user at a `config/projects.yaml` file.
- **Engine keys**: `geo-mcp init` also writes `~/.geo-mcp/.env` with empty
  `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `GOOGLE_API_KEY` (and optional `PERPLEXITY_API_KEY`).
  Fill in any subset; engines without a key are skipped with a warning. `page_audit` needs no keys.
- **Check first**: call `server_status()` — it reports which engines are configured, the model
  names in use, the projects file and the data dir. It never returns secret values.
- Preset lookup: `GEO_MCP_PROJECTS` (absolute path, used as-is — no fallback if it is missing) →
  `config/projects.yaml` relative to the server's working directory → `~/.geo-mcp/projects.yaml`.
  If the user says their preset edits "do nothing", the server is probably running from a
  directory that contains its own `config/projects.yaml`. (A *relative* `GEO_MCP_PROJECTS` that
  does not exist still falls back to `~/.geo-mcp/projects.yaml`; only an absolute one is
  used exactly as given.)

## Tools

| Tool | Intent | Key parameters |
|---|---|---|
| `page_audit` | AI crawlability: robots.txt + real HTTP access for 16 AI user-agents, WAF/bot-challenge detection, content parity, JS-only content, indexability, JSON-LD, sitemap, `llms.txt`; 0-100 score + copy-paste fixes | `urls` (≤10), `project`, `render_js`, `bots`, `wait_seconds` |
| `ai_visibility` | Runs the preset queries **and their stable variants** through each engine with web search on; mention / citation / recommendation rates, rank vs competitors, share of voice, most-cited domains, citation-outreach opportunities, lost queries | `project`, `queries`, `engines`, `include_variants`, `variants_per_query`, `grounded`, `company`, `website`, `competitors`, `include_answers` (put full answers in the stored report), `dry_run` |
| `sentiment` | Asks engines directly about the company (reputation, per-service, head-to-head); recognition rate, sentiment −100..100, aspects, praise, concerns, claims that contradict the preset's ground-truth facts | `project`, `services`, `engines`, `mode` (`grounded`/`ungrounded`/`both`), `include_comparisons`, `company`, `website` (ad-hoc, no preset), `include_answers`, `dry_run` |
| `full_analysis` | All three plus a GEO score (0-100), deltas vs the previous run, alerts, a prioritized action plan, campaign guidance and a Markdown/HTML report | `project`, `engines`, `include_page_audit`, `include_ai_visibility`, `include_sentiment`, `sentiment_mode` (**not** `mode` — that is the `sentiment` tool's parameter name), `render_js`, `brief`, `dry_run` |
| `get_job` | Status of a run; when complete returns the compact summary (metrics, deltas, alerts, highlights, actions) | `run_id`, `wait_seconds` |
| `get_run` | Retrieve a stored run: markdown report, full JSON, filtered raw answers with citations, or the recommendation list | `run_id`, `section` (`summary`/`report`/`data`/`answers`/`recommendations`), `engine`, `query`, `max_chars` |
| `list_runs` | Recent runs with headline metrics, newest first | `project`, `kind`, `status`, `limit` |
| `get_trends` | Time series of one metric across completed runs, with first/last/change/min/max | `project`, `kind`, `metrics`, `limit` |
| `compare_runs` | Diff two runs (older first): metric changes, per-query mention changes, new/dropped cited domains | `run_a`, `run_b` |
| `list_projects` | Configured project presets: slug, company, website, queries, competitors, core services | — |
| `get_project` | Full preset for one project, including ground-truth facts and alert thresholds | `project` |
| `server_status` | Server version, configured engines, model names, judge model, projects file, data dir | — |

## Canonical Workflow

1. **Find the project** — call `list_projects()`. Use the returned `slug` in every later call.
   Completion criterion: you have one exact slug (ask the user if several fit and you cannot tell).
2. **Check the wiring** (once per session) — call `server_status()`. If the target engines are
   unconfigured, tell the user which keys to add to `~/.geo-mcp/.env` instead of burning a run.
3. **Preview the cost** — call the run tool with `dry_run: true` (see Cost Guard). Completion
   criterion: you know the prompt count and the exact number of engine calls. `page_audit` has no
   `dry_run` parameter — skip this step for it.
4. **Run it** — call `page_audit`, `ai_visibility`, `sentiment` or `full_analysis` with `project`.
   Completion criterion: you hold a `run_id`.
5. **Poll** — the run may finish inside the call, or come back as `status: "running"` with a
   `run_id`. Then poll `get_job(run_id, wait_seconds=50)` until it reports `complete` (or `failed`).
   Completion criterion: a terminal status, not a `running` one.
6. **Read the result** — `get_run(run_id, section="report")` for the full Markdown report,
   `section="answers"` for raw engine answers with citations, `section="recommendations"` for the
   action list. For history use `get_trends(project, kind=…)` and `compare_runs(old_id, new_id)`.
7. **Report** — lead with the score and its change vs the previous run, list alerts, then the top
   actions. Cite `run_id` so the user can pull the stored report.

### Long runs

Run tools wait up to `wait_seconds` (default 50s; 20s for `full_analysis`). If the run is not
finished, the tool returns `status: "running"` plus a `run_id` — **that is not an error**.
Poll `get_job(run_id, wait_seconds=50)` until the status is `complete` or `failed`.

### Cost Guard

- Call the run tool with `dry_run: true` **first**: it makes no engine calls and returns every
  prompt plus the exact number of engine calls.
- Only then run for real, and keep the run to one `project` at a time.
- `page_audit` calls no engines and has no `dry_run` parameter — it is free to run.
- Preset query variants are generated once and cached, so repeat runs are comparable; pass
  `regenerate_variants: true` only when you deliberately want new wording.

## Pitfalls

- **No project slug** — a run tool without `project` fails when several presets exist. Always
  resolve the slug with `list_projects()` first.
- **Wrong status field** — `status: "running"` plus a `run_id` means "poll `get_job`", not "failed".
- **Single runs are samples** — engine answers are stochastic and location/personalization
  dependent. Report trends across runs (`get_trends`, `compare_runs`), not one-off anecdotes.
- **Missing engine keys** — engines without a key are skipped with a warning; a run can complete
  with fewer engines than requested. Say which engines actually ran.
- **Page-audit caveat** — the crawler test spoofs user-agents from the server's IP; a firewall that
  verifies bots by IP may admit the genuine crawler even when the spoofed request was challenged.
  Confirm in the firewall logs before changing rules.
- **Truncated payloads** — `get_run` caps output (`max_chars`, default 60000). Narrow with
  `section="answers"` plus `engine` / `query` filters instead of raising the cap blindly. Note the
  response keys: `section="report"` returns `markdown`, `section="data"` returns `data` (or
  `truncated` + `hint`), `section="answers"` returns `answers`.

## Verification

- A run is finished only when `get_job` reports `complete`; a returned `run_id` is not a result.
- Confirm the report exists: `get_run(run_id, section="report")` returns non-empty Markdown.
- Confirm the comparison basis: `get_trends(project, kind=…)` or `compare_runs` shows the delta
  against the previous run before you claim "improved" or "worse".
- Confirm the engine set: `server_status()` lists which engines were configured for that run.
