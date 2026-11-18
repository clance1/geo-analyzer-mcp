> Sample output from `geo-mcp run page-audit`. The URLs below are placeholders
> (`example.com` is a reserved domain), so the figures are illustrative.

# AI crawlability audit — Local Construction Co.

- **Run:** 20261117-060956-22c681
- **Project:** local-construction
- **Website:** https://www.localconstruction.example.com
- **Compared with:** 20261117-060853-552c4c

## Alerts

- **HIGH** — Only 5 AI crawlers can access the site
- **HIGH** — 3 critical crawlability finding(s)

## Prioritized actions

| Pri | Area | Action | Impact | Effort | Why / evidence |
|---|---|---|---|---|---|
| P1 | Technical / crawlability | Server/WAF rejects 11 AI user-agent(s): GPTBot, OAI-SearchBot, ChatGPT-User, Googlebot, PerplexityBot, Perplexity-User, Bingbot, meta-externalagent, Amazonbot, CCBot, Bytespider | H | S | Requests with these user-agents received 4xx/5xx or a bot challenge, even if robots.txt allows them (detected: Vercel bot protection). Note: this test spoofs the user-agent from a non-crawler IP; firewalls that verify bots by IP may still admit the real crawlers, so confirm in the firewall logs. Evidence: 1 page(s) |
| P1 | Technical / crawlability | Page serves a bot challenge to regular visitors (HTTP 429, Vercel) | H | S | Anything that cannot run the JavaScript challenge — most AI fetchers — gets an interstitial instead of the page. Evidence: 1 page(s) |
| P1 | Technical / crawlability | AI fetchers receive a challenge page instead of the content | H | S | Content, structured-data and discovery checks (robots.txt, sitemap, llms.txt) were skipped because the server answered with a bot-protection interstitial. Re-run after allow-listing to audit the real page. Evidence: 1 page(s) |
| P2 | Technical / crawlability | Server/WAF rejects 1 AI user-agent(s): meta-externalagent | M | S | Requests with these user-agents received 4xx/5xx or a bot challenge, even if robots.txt allows them. Note: this test spoofs the user-agent from a non-crawler IP; firewalls that verify bots by IP may still admit the real crawlers, so confirm in the firewall logs. Evidence: 1 page(s) |

## AI crawlability — score 70/100 (+6)

2 page(s) audited in 34.3s.

| Severity | Finding | Pages | Fix |
|---|---|---|---|
| critical | Server/WAF rejects 11 AI user-agent(s): GPTBot, OAI-SearchBot, ChatGPT-User, Googlebot, PerplexityBot, Perplexity-User, Bingbot, meta-externalagent, Amazonbot, CCBot, Bytespider | 1 | Vercel: Project > Firewall — disable Attack Challenge Mode (or Bot Protection 'challenge') for this project, or add a firewall bypass rule for verified bots / AI crawler user-agents. |
| critical | Page serves a bot challenge to regular visitors (HTTP 429, Vercel) | 1 | Vercel: Project > Firewall — disable Attack Challenge Mode (or Bot Protection 'challenge') for this project, or add a firewall bypass rule for verified bots / AI crawler user-agents. |
| critical | AI fetchers receive a challenge page instead of the content | 1 | Vercel: Project > Firewall — disable Attack Challenge Mode (or Bot Protection 'challenge') for this project, or add a firewall bypass rule for verified bots / AI crawler user-agents. |
| high | Server/WAF rejects 1 AI user-agent(s): meta-externalagent | 1 | Cloudflare: Security > Bots — allow Verified Bots, turn off 'Block AI Scrapers and Crawlers' for answer-engine bots, or add a WAF skip rule for their user-agents. |
| medium | Missing meta description | 1 | <meta name="description" content="One-sentence summary of what this page offers, who it is for and where."> |
| low | No canonical URL | 1 | <link rel="canonical" href="https://build.localconstruction.example.com/"> |


### https://www.localconstruction.example.com — 88/100

HTTP 200 · 81 ms · 1685 words in raw HTML · AI bots accessible 15/16 · JSON-LD: HomeAndConstructionBusiness, LocalBusiness, Organization, Service, WebSite

| ai_access | content | structured_data | indexability | discovery |
|---|---|---|---|---|
| 65 | 100 | 100 | 100 | 100 |


| Bot | robots.txt | HTTP | Reason |
|---|---|---|---|
| meta-externalagent | allowed | None |  |


### https://build.localconstruction.example.com/ — 52/100

HTTP 429 · 4911 ms · None words in raw HTML · AI bots accessible 5/16 · JSON-LD: none

| ai_access | content | structured_data | indexability | discovery |
|---|---|---|---|---|
| 40 | 40 | 100 | 20 | 100 |


**Citation-critical bots blocked:** Bingbot, ChatGPT-User, Googlebot, OAI-SearchBot, Perplexity-User, PerplexityBot


| Bot | robots.txt | HTTP | Reason |
|---|---|---|---|
| GPTBot | allowed | None |  |
| OAI-SearchBot | allowed | None |  |
| ChatGPT-User | allowed | None |  |
| Googlebot | allowed | None |  |
| PerplexityBot | allowed | None |  |
| Perplexity-User | allowed | None |  |
| Bingbot | allowed | None |  |
| meta-externalagent | allowed | None |  |
| Amazonbot | allowed | None |  |
| CCBot | allowed | None |  |
| Bytespider | allowed | None |  |

