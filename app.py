#!/usr/bin/env python3
"""
hub — the central service dashboard behind panel.inbox.li.

One page that answers "what runs on this box and is it up right now".
The list of services is not copied here: it is read live from the wiki's
source of truth, [REDACTED:server-path] (the same file
`gen_pages.py` renders the wiki from), so a project onboarded through the
new-project workflow appears on the panel with no second edit. Liveness is
a threaded TCP connect to each entry's loopback port — the same check
`zfs list -t snapshot` is for backups: boring, direct, no UI trust.

Runs on 127.0.0.1:[REDACTED:loopback-port]. nginx is the only intended client: the vhost
applies the tunnel ACL and the central SSO cookie gate before anything
reaches here, and injects the X-Hub-Gate header only past that gate, so a
direct loopback hit without the header answers 403 (fail closed). The
whole filesystem is read-only to this unit except [REDACTED:server-path];
the wiki YAML is read, never written.
"""

import html
import json
import os
import socket
import time
from concurrent.futures import ThreadPoolExecutor

import yaml
from flask import Flask, Response, request

BIND = os.environ.get("BIND", "127.0.0.1")
PORT = int(os.environ.get("PORT", "[REDACTED:loopback-port]"))
GATE_SECRET = os.environ.get("HUB_GATE_SECRET", "")
GATE_HEADER = "X-Hub-Gate"

# Where the truth lives. Read-only; the wiki build owns this file.
PROJECTS_YAML = "[REDACTED:server-path]"
WIKI_BASE = "https://wiki.inbox.li/projects/"

# Liveness probe tuning: a refused connect answers in microseconds; the
# timeout only matters for a wedged listener, which is exactly the state
# worth reporting, so keep it short rather than accurate.
CHECK_TIMEOUT = 0.4
CHECK_WORKERS = 16
CHECK_CACHE_SECS = 15

app = Flask(__name__)


# --- data ----------------------------------------------------------------

def slug(name: str) -> str:
    """Wiki page slug — mirrors slug() in wiki/tools/gen_pages.py."""
    return name.replace(".", "-").replace("_", "-").lower()


def _coerce_port(value) -> int | None:
    """YAML ports are ints when set and '' when not; tolerate strays."""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def load_services():
    """Parse projects.yaml into the dashboard's view of the world.

    A project is shown when it is reachable as a service: a unit, a port,
    or a URL. Pure documentation entries (data/, passkey-audit/, ...) have
    none of those and are skipped. Returns (services, error) where error
    is a user-facing string when the file could not be read this time.
    """
    try:
        with open(PROJECTS_YAML, encoding="utf-8") as fh:
            doc = yaml.safe_load(fh)
        error = None
    except (OSError, yaml.YAMLError) as exc:
        error = f"cannot read {PROJECTS_YAML}: {exc}"
        doc = None

    services = []
    if doc:
        for entry in doc.get("projects") or []:
            name = str(entry.get("name") or "").strip()
            if not name or name == "retired":
                # The archive directory has an entry too; it is not a service.
                continue
            unit = str(entry.get("unit") or "").strip()
            port = _coerce_port(entry.get("port"))
            urls = [u for u in (entry.get("urls") or []) if u]
            if not (unit or port or urls):
                continue
            services.append({
                "name": name,
                "title": str(entry.get("title") or "").strip(),
                "what": " ".join(str(entry.get("what") or "").split()),
                "unit": unit,
                "port": port,
                "url": urls[0] if urls else "",
                "auth": str(entry.get("auth") or "").strip(),
                "repo": str(entry.get("repo") or "").strip(),
                "wiki": f"{WIKI_BASE}{slug(name)}/",
            })
        services.sort(key=lambda s: s["name"])
    return services, error


def _probe(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), CHECK_TIMEOUT):
            return True
    except OSError:
        return False


def check_status(services: list) -> dict:
    """TCP-probe every known port; returns {name: 'up'|'down'|'none'}.

    Cached for CHECK_CACHE_SECS so a page refresh storm does not turn
    into a port scan of the box.
    """
    now = time.monotonic()
    cached = app.config.get("_status_cache")
    if cached and now - cached[0] < CHECK_CACHE_SECS:
        return cached[1]

    ports = {s["name"]: s["port"] for s in services if s["port"]}
    result = {name: "none" for name in (s["name"] for s in services)}
    if ports:
        with ThreadPoolExecutor(max_workers=CHECK_WORKERS) as pool:
            for name, alive in zip(ports, pool.map(_probe, ports.values())):
                result[name] = "up" if alive else "down"
    app.config["_status_cache"] = (now, result)
    return result


def auth_badge(auth: str) -> str:
    """Short badge from the free-text auth field; the wiki holds the truth."""
    text = auth.lower()
    if "sso" in text:
        return "sso"
    if "acl" in text or "internal" in text:
        return "acl"
    return "open"


# --- rendering -----------------------------------------------------------

BADGE_CLASS = {"sso": "badge-sso", "acl": "badge-acl", "open": "badge-open"}
DOT_CLASS = {"up": "dot-up", "down": "dot-down", "none": "dot-none"}
DOT_LABEL = {"up": "up", "down": "DOWN", "none": "no port"}


def _subtitle(name: str, title: str) -> str:
    # Titles are written as "name — what it is"; strip a redundant prefix.
    for sep in (" — ", " – ", ": "):
        if title.startswith(name + sep):
            return title[len(name) + len(sep):]
    return title


def _card(svc: dict, status: str) -> str:
    e = html.escape
    what = svc["what"]
    if len(what) > 160:
        what = what[:157].rstrip() + "…"
    url_html = ""
    if svc["url"]:
        url_html = f'<a class="url" href="{e(svc["url"])}">{e(svc["url"])}</a>'
    bits = []
    if svc["unit"]:
        bits.append(f'<span class="meta">{e(svc["unit"])}</span>')
    if svc["port"]:
        bits.append(f'<span class="meta">:{svc["port"]}</span>')
    if svc["repo"]:
        bits.append(f'<span class="meta">repo {e(svc["repo"])}</span>')
    return f"""
<article class="card" data-name="{e(svc["name"])}" data-search="{e((svc["name"] + " " + svc["title"] + " " + svc["what"]).lower())}">
  <header>
    <span class="dot {DOT_CLASS[status]}" title="{DOT_LABEL[status]}"></span>
    <h2><a href="{e(svc["url"] or svc["wiki"])}">{e(svc["name"])}</a></h2>
    <span class="badge {BADGE_CLASS[auth_badge(svc["auth"])]}">{auth_badge(svc["auth"])}</span>
  </header>
  <p class="sub">{e(_subtitle(svc["name"], svc["title"]))}</p>
  <p class="what">{e(what)}</p>
  <footer>{url_html}{' '.join(bits)}<a class="wiki" href="{e(svc["wiki"])}">wiki&nbsp;↗</a></footer>
</article>"""


def _page(services: list, status: dict, error: str | None, age: float) -> str:
    e = html.escape
    ups = sum(1 for v in status.values() if v == "up")
    downs = [n for n, v in status.items() if v == "down"]
    downs_html = ""
    if downs:
        downs_html = ('<p class="downs">down: '
                      + ", ".join(f'<a href="#c-{e(n)}">{e(n)}</a>' for n in downs)
                      + "</p>")
    err_html = f'<p class="error">{e(error)}</p>' if error else ""
    cards = "".join(
        f'<div id="c-{e(s["name"])}">' + _card(s, status.get(s["name"], "none")) + "</div>"
        for s in services
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>hub — inbox.li services</title>
<style>
  :root {{
    --bg: #101418; --card: #181e24; --line: #242c34; --text: #d7dde3;
    --dim: #7f8c99; --up: #46c46c; --down: #e5534b; --none: #55606b;
    --link: #6cb2f0; --sso: #2c5f8a; --acl: #5a4a86; --open: #4a5a44;
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; padding: 1.2rem; background: var(--bg); color: var(--text);
         font: 15px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; }}
  a {{ color: var(--link); text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  .top {{ display: flex; flex-wrap: wrap; align-items: baseline; gap: 1rem;
          margin-bottom: .6rem; }}
  h1 {{ font-size: 1.25rem; margin: 0; }}
  .count {{ color: var(--dim); font-size: .9rem; }}
  .count b {{ color: var(--up); }}
  .downs {{ margin: 0 0 .6rem; color: var(--down); font-size: .9rem; }}
  .downs a {{ color: var(--down); text-decoration: underline; }}
  .error {{ margin: 0 0 .6rem; color: var(--down); font-family: ui-monospace, monospace;
            font-size: .85rem; }}
  #q {{ background: var(--card); border: 1px solid var(--line); color: var(--text);
        border-radius: 6px; padding: .45rem .7rem; font-size: .95rem; width: 16rem; }}
  .src {{ color: var(--dim); font-size: .8rem; margin-left: auto; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(21rem, 1fr));
           gap: .7rem; margin-top: .8rem; }}
  .card {{ background: var(--card); border: 1px solid var(--line); border-radius: 8px;
           padding: .7rem .85rem; }}
  .card.hidden {{ display: none; }}
  .card > header {{ display: flex; align-items: center; gap: .5rem; }}
  .card h2 {{ font-size: 1rem; margin: 0; font-family: ui-monospace, "SF Mono", monospace; }}
  .dot {{ width: .6rem; height: .6rem; border-radius: 50%; flex: none; }}
  .dot-up {{ background: var(--up); }}
  .dot-down {{ background: var(--down); box-shadow: 0 0 6px var(--down); }}
  .dot-none {{ background: var(--none); }}
  .badge {{ margin-left: auto; font-size: .68rem; text-transform: uppercase;
            letter-spacing: .05em; border-radius: 4px; padding: .1rem .4rem; }}
  .badge-sso {{ background: var(--sso); }}
  .badge-acl {{ background: var(--acl); }}
  .badge-open {{ background: var(--open); }}
  .sub {{ margin: .25rem 0 0; color: var(--text); font-size: .88rem; }}
  .what {{ margin: .3rem 0 0; color: var(--dim); font-size: .8rem; }}
  .card footer {{ display: flex; flex-wrap: wrap; gap: .6rem; align-items: center;
                  margin-top: .5rem; font-size: .75rem; }}
  .card footer .meta, .card footer .url {{ color: var(--dim);
        font-family: ui-monospace, monospace; }}
  .card footer .wiki {{ margin-left: auto; color: var(--dim); }}
  .stale {{ color: var(--down); font-size: .8rem; margin-left: .5rem; }}
</style>
</head>
<body>
<div class="top">
  <h1>hub</h1>
  <span class="count" id="count"><span id="total" data-n="{len(services)}">{len(services)} services</span> · <b id="ups">{ups}</b> up ·
    <span id="checked">{age:.0f}s ago</span></span>
  <input id="q" type="search" placeholder="filter…" autofocus autocomplete="off">
  <span class="src">from {e(os.path.basename(PROJECTS_YAML))} (the wiki's source of truth)</span>
</div>
{downs_html}{err_html}
<div class="grid" id="grid">
{cards}
</div>
<script>
// Re-probe without a reload: refresh the dots and counts in place.
const DOT = {{up: "dot dot-up", down: "dot dot-down", none: "dot dot-none"}};
const totalEl = document.getElementById("total");
async function tick() {{
  try {{
    const r = await fetch("/api/services.json", {{cache: "no-store"}});
    const j = await r.json();
    for (const s of j.services) {{
      const card = document.querySelector(`[data-name="${{CSS.escape(s.name)}}"] .dot`);
      if (card) card.className = DOT[s.status] || DOT.none;
    }}
    document.getElementById("ups").textContent = j.up;
    document.getElementById("checked").textContent = "just now";
    totalEl.dataset.n = j.total;
    if (!document.getElementById("q").value)
      totalEl.textContent = j.total + " services";
  }} catch (e) {{ /* leave the last known state on screen */ }}
}}
setInterval(tick, 30000);
document.getElementById("q").addEventListener("input", ev => {{
  const q = ev.target.value.toLowerCase();
  let shown = 0;
  for (const card of document.querySelectorAll(".card")) {{
    const on = !q || card.dataset.search.includes(q);
    card.classList.toggle("hidden", !on);
    if (on) shown++;
  }}
  totalEl.textContent = q ? shown + " shown"
                          : totalEl.dataset.n + " services";
}});
</script>
</body>
</html>"""


# --- routes --------------------------------------------------------------

@app.before_request
def _gate():
    # /healthz stays open for the unit check; everything else needs the
    # header nginx injects past the SSO gate. Fail closed when unset.
    if request.path == "/healthz":
        return None
    if not GATE_SECRET or request.headers.get(GATE_HEADER) != GATE_SECRET:
        return Response("forbidden\n", status=403, mimetype="text/plain")
    return None


@app.get("/")
def index() -> Response:
    services, error = load_services()
    status = check_status(services)
    age = 0.0
    cached = app.config.get("_status_cache")
    if cached:
        age = time.monotonic() - cached[0]
    return Response(_page(services, status, error, age),
                    mimetype="text/html", headers={"Cache-Control": "no-store"})


@app.get("/api/services.json")
def api() -> Response:
    services, error = load_services()
    status = check_status(services)
    ups = sum(1 for v in status.values() if v == "up")
    body = {
        "ok": error is None,
        "error": error,
        "total": len(services),
        "up": ups,
        "services": [
            {**s, "status": status.get(s["name"], "none"),
             "badge": auth_badge(s["auth"])}
            for s in services
        ],
    }
    return Response(json.dumps(body, indent=2), mimetype="application/json",
                    headers={"Cache-Control": "no-store"})


@app.get("/healthz")
def healthz() -> Response:
    return Response("ok\n", mimetype="text/plain")


if __name__ == "__main__":
    app.run(host=BIND, port=PORT)
