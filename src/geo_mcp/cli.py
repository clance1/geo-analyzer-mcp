"""`geo-mcp` command line: serve the MCP server, or run analyses headless (cron / CI).

  geo-mcp serve                          # stdio (Claude Desktop / Claude Code / Cursor spawn this)
  geo-mcp serve --transport http         # Streamable HTTP on 127.0.0.1:8765/mcp (+ REST hooks)
  geo-mcp run full-analysis --project local-construction --out reports/
  geo-mcp run ai-visibility --project local-construction --engines chatgpt,gemini --format json
  geo-mcp history --project local-construction
  geo-mcp trends --project local-construction --kind ai-visibility
  geo-mcp init      # writes ~/.geo-mcp/projects.yaml + .env templates
  geo-mcp doctor
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import shutil
import sys
from pathlib import Path

from . import __version__

LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _load_env() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover
        return
    load_dotenv(Path.cwd() / ".env", override=False)
    data_dir = Path(os.getenv("GEO_MCP_DATA_DIR", "~/.geo-mcp")).expanduser()
    load_dotenv(data_dir / ".env", override=False)


def _service():
    from .config import get_settings, reset_settings_cache
    from .service import GeoService

    reset_settings_cache()
    return GeoService(get_settings())


def create_http_app(service, *, host: str, stateless: bool = True):
    """Streamable HTTP ASGI app wrapped in bearer auth."""
    from mcp.server.transport_security import TransportSecuritySettings

    from .auth import BearerAuth
    from .server import build_server

    s = service.settings
    mcp = build_server(service)
    security = None
    if s.allowed_hosts:
        hosts = list(dict.fromkeys([*s.allowed_hosts, "127.0.0.1", "localhost", f"127.0.0.1:{s.http_port}",
                                    f"localhost:{s.http_port}"]))
        origins = [f"https://{h}" for h in s.allowed_hosts] + [f"http://{h}" for h in hosts]
        security = TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=hosts,
                                             allowed_origins=origins)
    elif host not in LOCAL_HOSTS:
        # Bound to 0.0.0.0 (e.g. Docker) without a host allow-list: the bearer token is the gate.
        security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
    app = mcp.streamable_http_app(streamable_http_path="/mcp", stateless_http=stateless, transport_security=security,
                                  host=host)
    return BearerAuth(app, s.auth_token)


def cmd_serve(a: argparse.Namespace) -> int:
    service = _service()
    s = service.settings
    if a.transport == "stdio":
        from .server import build_server

        logging.basicConfig(level=logging.WARNING, stream=sys.stderr)  # stdout is the protocol channel
        build_server(service).run("stdio")
        return 0
    host, port = a.host or s.http_host, a.port or s.http_port
    if host not in LOCAL_HOSTS and not s.auth_token and not a.insecure:
        print("Refusing to bind a non-localhost address without GEO_MCP_TOKEN. Set a token "
              "(e.g. `python -c \"import secrets;print(secrets.token_urlsafe(32))\"`) or pass --insecure.",
              file=sys.stderr)
        return 2
    import uvicorn

    logging.basicConfig(level=logging.INFO)
    app = create_http_app(service, host=host, stateless=not a.stateful)
    print(f"geo-mcp {__version__} → http://{host}:{port}/mcp  (auth: {'bearer token' if s.auth_token else 'NONE'})",
          file=sys.stderr)
    # Forwarded headers (X-Forwarded-For/-Proto/-Host) are only believed from
    # the configured proxy peers; with none configured they are ignored, so a
    # remote client cannot spoof its address or the scheme.
    trusted = s.trusted_proxies.strip()
    uvicorn.run(app, host=host, port=port, proxy_headers=bool(trusted),
                forwarded_allow_ips=trusted or "127.0.0.1", log_level="info")
    return 0


def _params(a: argparse.Namespace, kind: str) -> dict:
    p: dict = {"include_answers": a.include_answers}
    if a.engines:
        p["engines"] = [e.strip() for e in a.engines.split(",") if e.strip()]
    if kind in ("page_audit", "full_analysis"):
        p.update(urls=a.url or None, render_js=a.render_js)
    if kind in ("ai_visibility", "full_analysis"):
        p.update(include_variants=not a.no_variants, grounded=not a.ungrounded)
        if a.query:
            p["queries"] = a.query
    if kind in ("sentiment", "full_analysis"):
        p["mode"] = a.mode
    if kind == "full_analysis":
        p["brief"] = not a.no_brief
    return p


def cmd_run(a: argparse.Namespace) -> int:
    kind = a.kind.replace("-", "_")
    service = _service()
    project = None
    if not (kind == "page_audit" and a.url and not a.project):
        project = service.resolve_project(a.project)
    params = _params(a, kind)

    def progress(done: int, total: int, msg: str) -> None:
        if not a.quiet:
            print(f"\r[{done}/{total}] {msg[:90]:<90}", end="", file=sys.stderr, flush=True)

    async def go():
        if a.dry_run:
            return await service.plan(kind, project, engines=params.get("engines"),
                                      include_variants=params.get("include_variants", True), urls=params.get("urls"))
        return await service.run(kind, project, params, trigger="cli", progress=progress)

    try:
        summary = asyncio.run(go())
    except Exception as e:  # noqa: BLE001
        print(f"\nerror: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    if not a.quiet:
        print(file=sys.stderr)
    if a.dry_run:
        print(json.dumps(summary, indent=2))
        return 0
    run_id = summary["run_id"]
    if a.out:
        out = Path(a.out) / run_id
        out.mkdir(parents=True, exist_ok=True)
        for f in service.store.run_dir(run_id).iterdir():
            shutil.copy2(f, out / f.name)
        print(f"saved → {out}", file=sys.stderr)
    if a.format == "md":
        print(service.store.load_report(run_id))
    elif a.format == "json":
        print(json.dumps(service.store.load_data(run_id), indent=2, default=str))
    else:
        print(json.dumps(summary, indent=2, default=str))
    if a.fail_on_alert and summary.get("alerts"):
        return 3
    return 0


def cmd_history(a: argparse.Namespace) -> int:
    service = _service()
    kind = a.kind.replace("-", "_") if a.kind else None
    for r in service.store.list(project=a.project, kind=kind, limit=a.limit):
        m = r.get("metrics") or {}
        head = " ".join(f"{k}={m[k]}" for k in ("geo_score", "page_score", "visibility_index", "sentiment_index")
                        if m.get(k) is not None)
        print(f"{r['id']}  {r['kind']:<14} {r['project'] or '-':<18} {r['status']:<9} {head}")
    return 0


def cmd_trends(a: argparse.Namespace) -> int:
    print(json.dumps(_service().trends(a.project, a.kind.replace("-", "_"), None, a.limit)["summary"], indent=2))
    return 0


def cmd_init(a: argparse.Namespace) -> int:
    from importlib.resources import files

    data_dir = Path(os.getenv("GEO_MCP_DATA_DIR", "~/.geo-mcp")).expanduser()
    data_dir.mkdir(parents=True, exist_ok=True)
    target = Path(a.path).expanduser() if a.path else data_dir / "projects.yaml"
    if target.exists() and not a.force:
        print(f"{target} already exists (use --force to overwrite)", file=sys.stderr)
        return 1
    target.write_text(files("geo_mcp").joinpath("templates/projects.example.yaml").read_text(encoding="utf-8"),
                      encoding="utf-8")
    env = data_dir / ".env"
    if not env.exists():
        env.write_text("# API keys for geo-mcp (loaded automatically)\nANTHROPIC_API_KEY=\nOPENAI_API_KEY=\n"
                       "GOOGLE_API_KEY=\n# PERPLEXITY_API_KEY=\n", encoding="utf-8")
    print(f"wrote {target}\nadd API keys to {env} (or your environment), then run: geo-mcp doctor")
    return 0


def cmd_doctor(_: argparse.Namespace) -> int:
    d = _service().doctor()
    print(json.dumps({"version": __version__, **d}, indent=2))
    return 0 if any(d["engines_configured"].values()) else 1


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="geo-mcp", description="GEO Analyzer MCP server")
    ap.add_argument("--version", action="version", version=f"geo-mcp {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="Run the MCP server")
    s.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    s.add_argument("--host")
    s.add_argument("--port", type=int)
    s.add_argument("--stateful", action="store_true", help="Use stateful MCP sessions (default stateless)")
    s.add_argument("--insecure", action="store_true", help="Allow non-localhost bind without a token")
    s.set_defaults(fn=cmd_serve)

    r = sub.add_parser("run", help="Run an analysis now (for cron / CI)")
    r.add_argument("kind", choices=["page-audit", "ai-visibility", "sentiment", "full-analysis"])
    r.add_argument("--project", "-p")
    r.add_argument("--url", action="append", help="URL to audit (repeatable)")
    r.add_argument("--query", action="append", help="Override preset queries (repeatable)")
    r.add_argument("--engines", help="Comma list: claude,chatgpt,gemini,perplexity")
    r.add_argument("--mode", choices=["grounded", "ungrounded", "both"], default="grounded")
    r.add_argument("--no-variants", action="store_true")
    r.add_argument("--ungrounded", action="store_true", help="Visibility without web search")
    r.add_argument("--render-js", action="store_true")
    r.add_argument("--no-brief", action="store_true")
    r.add_argument("--include-answers", action="store_true", help="Put full answers in the markdown report")
    r.add_argument("--dry-run", action="store_true", help="Show prompts and call counts only")
    r.add_argument("--format", choices=["summary", "md", "json"], default="summary")
    r.add_argument("--out", help="Copy report.md/html + data.json into this directory")
    r.add_argument("--fail-on-alert", action="store_true", help="Exit 3 if the run raised alerts")
    r.add_argument("--quiet", "-q", action="store_true")
    r.set_defaults(fn=cmd_run)

    h = sub.add_parser("history", help="List past runs")
    h.add_argument("--project", "-p")
    h.add_argument("--kind")
    h.add_argument("--limit", type=int, default=20)
    h.set_defaults(fn=cmd_history)

    t = sub.add_parser("trends", help="Metric changes across runs")
    t.add_argument("--project", "-p", required=True)
    t.add_argument("--kind", default="full-analysis")
    t.add_argument("--limit", type=int, default=12)
    t.set_defaults(fn=cmd_trends)

    i = sub.add_parser("init", help="Create ~/.geo-mcp/projects.yaml and .env templates")
    i.add_argument("--path", help="Write the projects file here instead")
    i.add_argument("--force", action="store_true")
    i.set_defaults(fn=cmd_init)

    d = sub.add_parser("doctor", help="Check configuration")
    d.set_defaults(fn=cmd_doctor)
    return ap


def main(argv: list[str] | None = None) -> int:
    _load_env()
    a = build_parser().parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
