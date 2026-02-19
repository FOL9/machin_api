#!/usr/bin/env python3
"""
HyprShare Server
================
Self-hosted sshx-style terminal sharing over WebSockets.

Flow:
  curl -sSf http://HOST/get | sh -s run
  └─► agent.py connects to /agent/ws
       └─► server assigns session id + share URL
            └─► browser opens /s/{sid}
                 └─► viewer connects to /viewer/ws/{sid}
                      └─► bidirectional PTY relay

Usage:
  pip install fastapi uvicorn
  python server.py                         # default 0.0.0.0:8000
  python server.py --host 0.0.0.0 --port 9000
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import socket
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

import uvicorn
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, PlainTextResponse
from starlette.requests import Request

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCROLLBACK_BYTES = 64 * 1024          # 64 KB rolling buffer per session
SESSION_TTL_AFTER_DISCONNECT = 120    # seconds before dead session is pruned

HERE = pathlib.Path(__file__).parent

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Session:
    id:       str
    name:     str
    created:  float = field(default_factory=time.time)
    agent:    Optional[WebSocket] = None
    viewers:  set[WebSocket]      = field(default_factory=set)
    buf:      bytearray           = field(default_factory=bytearray)
    cols:     int                 = 220
    rows:     int                 = 50
    alive:    bool                = True

    # ------------------------------------------------------------------
    # Buffer helpers
    # ------------------------------------------------------------------

    def append_output(self, text: str) -> None:
        """Append PTY output to the scrollback buffer, trimming if needed."""
        chunk = text.encode("utf-8", errors="replace")
        self.buf.extend(chunk)
        overflow = len(self.buf) - SCROLLBACK_BYTES
        if overflow > 0:
            del self.buf[:overflow]

    def scrollback_text(self) -> str:
        return self.buf.decode("utf-8", errors="replace")

    # ------------------------------------------------------------------
    # Broadcast helpers
    # ------------------------------------------------------------------

    async def broadcast_to_viewers(self, payload: dict) -> None:
        """Send a JSON message to every viewer; drop dead connections."""
        raw = json.dumps(payload)
        dead: set[WebSocket] = set()
        for ws in list(self.viewers):
            try:
                await ws.send_text(raw)
            except Exception:
                dead.add(ws)
        self.viewers -= dead

    async def send_to_agent(self, payload: dict) -> bool:
        """Forward a message to the agent. Returns False if agent is gone."""
        if not self.agent or not self.alive:
            return False
        try:
            await self.agent.send_text(json.dumps(payload))
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Serialise for the REST API
    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "id":      self.id,
            "name":    self.name,
            "created": self.created,
            "alive":   self.alive,
            "viewers": len(self.viewers),
        }

# ---------------------------------------------------------------------------
# Session registry
# ---------------------------------------------------------------------------

_sessions: dict[str, Session] = {}


def create_session(name: str, rows: int, cols: int) -> Session:
    sid = uuid.uuid4().hex[:10]
    sess = Session(id=sid, name=name, rows=rows, cols=cols)
    _sessions[sid] = sess
    return sess


def get_session(sid: str) -> Optional[Session]:
    return _sessions.get(sid)


def all_sessions() -> list[Session]:
    return list(_sessions.values())


def schedule_prune(sid: str) -> None:
    """Remove a dead session after TTL seconds."""
    loop = asyncio.get_event_loop()
    loop.call_later(SESSION_TTL_AFTER_DISCONNECT, lambda: _sessions.pop(sid, None))

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="HyprShare", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Utility: derive server base URL from request
# ---------------------------------------------------------------------------

def base_url(request: Request) -> str:
    host   = request.headers.get("host", "localhost:8000")
    scheme = "https" if request.url.scheme == "https" else "http"
    return f"{scheme}://{host}"

# ---------------------------------------------------------------------------
# Routes: static / download
# ---------------------------------------------------------------------------

@app.get("/get", response_class=PlainTextResponse, include_in_schema=False)
async def route_installer(request: Request) -> PlainTextResponse:
    """Return the shell installer script, baking in the server URL."""
    script = _render_installer(base_url(request))
    return PlainTextResponse(script, media_type="text/plain")


@app.get("/agent.py", response_class=PlainTextResponse, include_in_schema=False)
async def route_agent_script() -> PlainTextResponse:
    """Serve the agent Python script so the installer can download it."""
    agent_path = HERE / "agent.py"
    if not agent_path.exists():
        raise HTTPException(status_code=404, detail="agent.py not found next to server.py")
    return PlainTextResponse(agent_path.read_text(), media_type="text/plain")

# ---------------------------------------------------------------------------
# Routes: REST API
# ---------------------------------------------------------------------------

@app.get("/api/sessions")
async def route_list_sessions() -> dict:
    return {"sessions": [s.to_dict() for s in all_sessions()]}

# ---------------------------------------------------------------------------
# Routes: HTML pages
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def route_dashboard() -> HTMLResponse:
    return HTMLResponse(_DASHBOARD_HTML)


@app.get("/s/{sid}", response_class=HTMLResponse)
async def route_viewer_page(sid: str) -> HTMLResponse:
    if not get_session(sid):
        raise HTTPException(status_code=404, detail=f"Session '{sid}' not found")
    html = _VIEWER_HTML.replace("{{SID}}", sid)
    return HTMLResponse(html)

# ---------------------------------------------------------------------------
# WebSocket: agent
# ---------------------------------------------------------------------------

@app.websocket("/agent/ws")
async def ws_agent(websocket: WebSocket) -> None:
    await websocket.accept()
    sess: Optional[Session] = None

    try:
        # Step 1 ── registration handshake
        raw  = await asyncio.wait_for(websocket.receive_text(), timeout=10.0)
        msg  = json.loads(raw)

        if msg.get("type") != "register":
            await websocket.close(code=4000)
            return

        sess = create_session(
            name=msg.get("name", "unknown"),
            rows=int(msg.get("rows", 50)),
            cols=int(msg.get("cols", 220)),
        )
        sess.agent = websocket

        # Reply with session info
        # Note: we embed __SERVER__ as placeholder; agent replaces it using
        # the --server flag value it already knows.
        await websocket.send_text(json.dumps({
            "type": "session",
            "sid":  sess.id,
            "url":  f"__SERVER__/s/{sess.id}",
        }))

        print(f"[agent +] {sess.name!r}  sid={sess.id}")

        # Step 2 ── relay loop
        async for raw_msg in websocket.iter_text():
            msg = _parse_json(raw_msg)
            if not msg:
                continue

            if msg["type"] == "output":
                sess.append_output(msg["data"])
                await sess.broadcast_to_viewers({"type": "output", "data": msg["data"]})

            elif msg["type"] == "pong":
                await sess.broadcast_to_viewers({"type": "pong"})

    except (WebSocketDisconnect, asyncio.TimeoutError):
        pass
    except Exception as exc:
        print(f"[agent !] {exc}")

    finally:
        if sess:
            _mark_agent_disconnected(sess)

# ---------------------------------------------------------------------------
# WebSocket: viewer
# ---------------------------------------------------------------------------

@app.websocket("/viewer/ws/{sid}")
async def ws_viewer(websocket: WebSocket, sid: str) -> None:
    await websocket.accept()

    sess = get_session(sid)
    if not sess:
        await websocket.send_text(json.dumps({
            "type":    "error",
            "message": f"Session '{sid}' not found or expired.",
        }))
        await websocket.close()
        return

    # Replay scrollback so the viewer sees existing output
    if sess.buf:
        await websocket.send_text(json.dumps({
            "type": "output",
            "data": sess.scrollback_text(),
        }))

    # Send current metadata
    await websocket.send_text(json.dumps(_meta_payload(sess)))

    sess.viewers.add(websocket)
    print(f"[view  +] sid={sid}  total={len(sess.viewers)}")

    try:
        async for raw_msg in websocket.iter_text():
            msg = _parse_json(raw_msg)
            if not msg:
                continue

            if msg["type"] == "ping":
                # Forward to agent; agent replies with pong which we relay
                if not await sess.send_to_agent({"type": "ping"}):
                    # Agent gone — reply directly so viewer latency still works
                    await websocket.send_text(json.dumps({"type": "pong"}))

            elif msg["type"] == "input":
                await sess.send_to_agent(msg)

            elif msg["type"] == "resize":
                sess.cols = int(msg.get("cols", sess.cols))
                sess.rows = int(msg.get("rows", sess.rows))
                await sess.send_to_agent(msg)
                await sess.broadcast_to_viewers(_meta_payload(sess))

    except (WebSocketDisconnect, Exception):
        pass

    finally:
        sess.viewers.discard(websocket)
        print(f"[view  -] sid={sid}  total={len(sess.viewers)}")

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _parse_json(raw: str) -> Optional[dict]:
    try:
        return json.loads(raw)
    except Exception:
        return None


def _meta_payload(sess: Session) -> dict:
    return {
        "type":    "meta",
        "name":    sess.name,
        "viewers": len(sess.viewers),
        "cols":    sess.cols,
        "rows":    sess.rows,
    }


def _mark_agent_disconnected(sess: Session) -> None:
    sess.alive = False
    sess.agent = None
    print(f"[agent -] {sess.name!r}  sid={sess.id}")
    asyncio.get_event_loop().create_task(
        sess.broadcast_to_viewers({
            "type":    "disconnect",
            "message": f"Agent '{sess.name}' disconnected",
        })
    )
    schedule_prune(sess.id)

# ---------------------------------------------------------------------------
# Shell installer script (generated dynamically with server URL baked in)
# ---------------------------------------------------------------------------

def _render_installer(server_url: str) -> str:
    return f"""\
#!/bin/sh
# HyprShare — agent installer
# Usage:
#   curl -sSf {server_url}/get | sh           # download & install
#   curl -sSf {server_url}/get | sh -s run    # download & run immediately
set -e

SERVER_URL="{server_url}"
INSTALL_DIR="$HOME/.local/bin"
BINARY="$INSTALL_DIR/hyprshare"

# ── detect python ────────────────────────────────────────────────────────────
PYTHON=""
for cmd in python3 python; do
  if command -v "$cmd" >/dev/null 2>&1; then
    PYTHON="$cmd"
    break
  fi
done
[ -z "$PYTHON" ] && {{ echo "[hyprshare] ERROR: python3 not found" >&2; exit 1; }}

# ── install websockets (silent) ──────────────────────────────────────────────
$PYTHON -m pip install --quiet websockets 2>/dev/null || true

# ── download agent.py ────────────────────────────────────────────────────────
mkdir -p "$INSTALL_DIR"
echo "[hyprshare] Downloading agent …"
if   command -v curl >/dev/null 2>&1; then curl -sSf "$SERVER_URL/agent.py" -o "$BINARY"
elif command -v wget >/dev/null 2>&1; then wget  -q   "$SERVER_URL/agent.py" -O "$BINARY"
else {{ echo "[hyprshare] ERROR: curl or wget required" >&2; exit 1; }}
fi
chmod +x "$BINARY"
echo "[hyprshare] Installed → $BINARY"

# ── run immediately when invoked as: sh -s run ───────────────────────────────
if [ "$1" = "run" ]; then
  exec $PYTHON "$BINARY" --server "$SERVER_URL"
fi

echo ""
echo "  Start a session:"
echo "    $PYTHON $BINARY --server $SERVER_URL"
echo ""
"""

# ---------------------------------------------------------------------------
# HTML templates (kept as module-level constants; real projects use Jinja2)
# ---------------------------------------------------------------------------

_DASHBOARD_HTML = """\
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>HyprShare — Dashboard</title>
  <link rel="preconnect" href="https://fonts.googleapis.com" />
  <link rel="stylesheet"
    href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@300;400;500;700&display=swap" />
  <style>
    /* ── reset ── */
    *, *::before, *::after { margin: 0; padding: 0; box-sizing: border-box; }

    /* ── design tokens ── */
    :root {
      --bg:      #1e1e2e;
      --surface: rgba(30, 30, 46, 0.75);
      --border:  rgba(203, 166, 247, 0.2);
      --mauve:   #cba6f7;
      --blue:    #89b4fa;
      --green:   #a6e3a1;
      --red:     #f38ba8;
      --text:    #cdd6f4;
      --sub:     #a6adc8;
      --muted:   #6c7086;
      --blur:    blur(22px) saturate(180%);
      --radius:  10px;
    }

    /* ── base ── */
    html, body {
      min-height: 100vh;
      background: var(--bg);
      color: var(--text);
      font-family: "JetBrains Mono", monospace;
    }

    /* ── wallpaper ── */
    #bg {
      position: fixed; inset: 0; z-index: 0;
      background: radial-gradient(ellipse at 30% 40%, #1e1e3e, #0d0d1a 60%, #1a0d2e);
    }

    /* ── layout ── */
    main {
      position: relative; z-index: 1;
      max-width: 860px; margin: 0 auto; padding: 64px 24px;
    }

    /* ── header ── */
    .logo    { font-size: 28px; font-weight: 700; color: var(--mauve); letter-spacing: .06em; }
    .tagline { font-size: 13px; color: var(--muted); margin: 4px 0 44px; }

    /* ── section title ── */
    h2 {
      font-size: 11px; font-weight: 600; color: var(--sub);
      letter-spacing: .12em; text-transform: uppercase;
      border-bottom: 1px solid var(--border);
      padding-bottom: 8px; margin-bottom: 14px;
    }

    /* ── quick-start box ── */
    .quickstart {
      background: var(--surface);
      backdrop-filter: var(--blur);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      padding: 22px 26px;
      margin-bottom: 32px;
    }
    .qs-label { font-size: 12px; font-weight: 600; color: var(--sub); margin-bottom: 12px; }
    .qs-cmd {
      display: flex; align-items: center; justify-content: space-between; gap: 12px;
      background: rgba(17, 17, 27, 0.8);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 12px 16px;
      font-size: 13px; color: var(--mauve);
      word-break: break-all;
      margin-bottom: 8px;
    }
    .qs-note { font-size: 11px; color: var(--muted); }
    .qs-note code { color: var(--blue); }

    /* ── copy button ── */
    .btn-copy {
      flex-shrink: 0;
      padding: 4px 12px;
      border-radius: 5px;
      border: 1px solid var(--border);
      background: transparent;
      color: var(--sub);
      font-family: inherit; font-size: 10px;
      cursor: pointer;
      white-space: nowrap;
      transition: background .15s, color .15s;
    }
    .btn-copy:hover { background: rgba(203, 166, 247, .1); color: var(--text); }

    /* ── session card ── */
    .card {
      display: flex; align-items: center; gap: 16px;
      background: var(--surface);
      backdrop-filter: var(--blur);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      padding: 18px 22px;
      margin-bottom: 10px;
      transition: border-color .15s;
    }
    .card:hover { border-color: rgba(203, 166, 247, .4); }

    .status-dot {
      width: 10px; height: 10px; border-radius: 50%; flex-shrink: 0;
    }
    .status-dot.live { background: var(--green); box-shadow: 0 0 8px var(--green); }
    .status-dot.dead { background: var(--red); }

    .card-info    { flex: 1; }
    .card-name    { font-size: 14px; font-weight: 600; color: var(--text); }
    .card-sid     { font-size: 11px; color: var(--mauve); margin-top: 2px; }
    .card-meta    { font-size: 11px; color: var(--muted); margin-top: 3px; }
    .card-actions { display: flex; gap: 8px; }

    /* ── buttons ── */
    .btn {
      display: inline-flex; align-items: center; gap: 5px;
      padding: 6px 14px;
      border-radius: 6px;
      border: 1px solid var(--border);
      background: transparent;
      color: var(--sub);
      font-family: inherit; font-size: 11px;
      cursor: pointer; text-decoration: none;
      transition: background .15s, color .15s, border-color .15s;
    }
    .btn:hover { background: rgba(203, 166, 247, .1); color: var(--text); border-color: var(--mauve); }
    .btn-primary {
      background: var(--mauve); color: #1e1e2e;
      border: none; font-weight: 700;
    }
    .btn-primary:hover { filter: brightness(1.1); }

    /* ── empty state ── */
    .empty {
      text-align: center; padding: 56px 24px;
      color: var(--muted); font-size: 13px; line-height: 2.2;
    }
    .empty code { color: var(--mauve); font-size: 13px; }

    /* ── footer ── */
    #refresh-info { text-align: center; font-size: 11px; color: var(--muted); margin-top: 16px; }
  </style>
</head>
<body>
  <div id="bg"></div>

  <main>
    <div class="logo">⚡ HyprShare</div>
    <p class="tagline">Self-hosted terminal sharing · sshx-compatible</p>

    <!-- Quick-start -->
    <div class="quickstart">
      <div class="qs-label">🚀 Share any terminal — one command:</div>
      <div class="qs-cmd">
        <span id="one-liner">loading…</span>
        <button class="btn-copy" onclick="copyOneLiner(event)">Copy</button>
      </div>
      <p class="qs-note">
        Or install permanently:
        <code>curl HOST/get | sh</code>
        then run
        <code>hyprshare --server HOST</code>
      </p>
    </div>

    <!-- Sessions -->
    <h2>Active Sessions</h2>
    <div id="session-list"><div class="empty">⏳ Loading…</div></div>
    <div id="refresh-info">Auto-refresh every 5 s</div>
  </main>

  <script>
    const origin = location.origin;
    document.getElementById("one-liner").textContent =
      `curl -sSf ${origin}/get | sh -s run`;

    // ── helpers ─────────────────────────────────────────────────────────────
    function timeAgo(unixSec) {
      const s = Math.floor(Date.now() / 1000 - unixSec);
      if (s <    60) return `${s}s ago`;
      if (s <  3600) return `${Math.floor(s / 60)}m ago`;
      return `${Math.floor(s / 3600)}h ago`;
    }

    function copyText(text, btn, label = "Copy") {
      navigator.clipboard.writeText(text);
      btn.textContent = "✓ Copied";
      setTimeout(() => btn.textContent = label, 1800);
    }

    function copyOneLiner(e) {
      copyText(document.getElementById("one-liner").textContent, e.target);
    }

    function copyLink(sid, btn) {
      copyText(`${origin}/s/${sid}`, btn, "⎘ Link");
    }

    // ── session list renderer ────────────────────────────────────────────────
    function renderCard(s) {
      const dot      = s.alive ? "live" : "dead";
      const status   = s.alive ? "🟢 Live" : "🔴 Offline";
      const viewers  = `${s.viewers} viewer${s.viewers !== 1 ? "s" : ""}`;
      const openBtn  = s.alive
        ? `<a class="btn btn-primary" href="/s/${s.id}" target="_blank">Open →</a>`
        : "";
      return `
        <div class="card">
          <div class="status-dot ${dot}"></div>
          <div class="card-info">
            <div class="card-name">${s.name}</div>
            <div class="card-sid">sid: ${s.id}</div>
            <div class="card-meta">${status} · ${viewers} · ${timeAgo(s.created)}</div>
          </div>
          <div class="card-actions">
            ${openBtn}
            <button class="btn" onclick="copyLink('${s.id}', this)">⎘ Link</button>
          </div>
        </div>`;
    }

    async function loadSessions() {
      const { sessions } = await fetch("/api/sessions").then(r => r.json());
      const list = document.getElementById("session-list");

      if (!sessions.length) {
        list.innerHTML = `<div class="empty">
          No active sessions.<br>
          Start one from any machine:<br>
          <code>curl -sSf ${origin}/get | sh -s run</code>
        </div>`;
      } else {
        list.innerHTML = sessions.map(renderCard).join("");
      }

      document.getElementById("refresh-info").textContent =
        `Updated ${new Date().toLocaleTimeString()}`;
    }

    loadSessions();
    setInterval(loadSessions, 5000);
  </script>
</body>
</html>
"""

# ---------------------------------------------------------------------------

_VIEWER_HTML = """\
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>HyprShare — {{SID}}</title>
  <link rel="preconnect" href="https://fonts.googleapis.com" />
  <link rel="stylesheet"
    href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@300;400;500;700&display=swap" />
  <link rel="stylesheet"
    href="https://cdn.jsdelivr.net/npm/xterm@5.3.0/css/xterm.css" />
  <script src="https://cdn.jsdelivr.net/npm/xterm@5.3.0/lib/xterm.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/xterm-addon-fit@0.7.0/lib/xterm-addon-fit.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/xterm-addon-web-links@0.9.0/lib/xterm-addon-web-links.js"></script>
  <style>
    /* ── reset ── */
    *, *::before, *::after { margin: 0; padding: 0; box-sizing: border-box; }

    /* ── design tokens ── */
    :root {
      --bg:      #1e1e2e;
      --glass:   rgba(30, 30, 46, 0.72);
      --bar:     rgba(17, 17, 27, 0.94);
      --border:  rgba(203, 166, 247, 0.2);
      --mauve:   #cba6f7;
      --blue:    #89b4fa;
      --green:   #a6e3a1;
      --red:     #f38ba8;
      --yellow:  #f9e2af;
      --text:    #cdd6f4;
      --sub:     #a6adc8;
      --muted:   #6c7086;
      --blur:    blur(22px) saturate(180%);
      --bar-h:   38px;
      --radius:  10px;
    }

    /* ── base ── */
    html, body {
      width: 100vw; height: 100vh; overflow: hidden;
      font-family: "JetBrains Mono", monospace;
      background: var(--bg); color: var(--text);
    }

    /* ── wallpaper ── */
    #wallpaper {
      position: fixed; inset: 0; z-index: 0;
      background: radial-gradient(ellipse at 30% 40%, #1e1e3e, #0d0d1a 60%, #1a0d2e);
    }
    #wallpaper canvas { position: absolute; inset: 0; width: 100%; height: 100%; }
    #wallpaper-dim    { position: absolute; inset: 0; background: rgba(11, 11, 23, .48); }

    /* ── top bar ── */
    #topbar {
      position: fixed; top: 0; left: 0; right: 0;
      height: var(--bar-h); z-index: 100;
      display: flex; align-items: center; justify-content: space-between;
      padding: 0 14px;
      background: var(--bar);
      backdrop-filter: var(--blur);
      border-bottom: 1px solid var(--border);
      font-size: 12px;
    }

    .bar-left  { display: flex; align-items: center; gap: 10px; }
    .bar-right { display: flex; align-items: center; gap: 8px; }
    .bar-sep   { width: 1px; height: 16px; background: var(--border); }

    .logo { font-size: 13px; font-weight: 700; color: var(--mauve); letter-spacing: .08em; }

    .badge {
      padding: 2px 8px; border-radius: 4px; font-size: 10px;
      background: rgba(30, 30, 46, .8);
      border: 1px solid var(--border);
      color: var(--sub);
    }

    /* connection status dot */
    .dot {
      width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0;
      transition: background .3s, box-shadow .3s;
    }
    .dot.connecting { background: var(--yellow); box-shadow: 0 0 6px var(--yellow); animation: pulse 1s infinite; }
    .dot.live       { background: var(--green);  box-shadow: 0 0 6px var(--green); }
    .dot.dead       { background: var(--red);    box-shadow: 0 0 6px var(--red); }
    @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: .3; } }

    #agent-name { color: var(--text); font-weight: 500; }
    #viewer-count, #latency { color: var(--sub); font-size: 11px; }

    /* bar buttons */
    .bar-btn {
      padding: 3px 10px;
      border-radius: 5px;
      border: 1px solid var(--border);
      background: transparent;
      color: var(--sub);
      font-family: inherit; font-size: 11px;
      cursor: pointer; text-decoration: none;
      display: inline-flex; align-items: center; gap: 4px;
      transition: background .15s, color .15s, border-color .15s;
    }
    .bar-btn:hover          { background: rgba(203,166,247,.1); color: var(--text); border-color: var(--mauve); }
    .bar-btn.green          { color: var(--green); border-color: rgba(166,227,161,.3); }
    .bar-btn.green:hover    { background: rgba(166,227,161,.08); }

    /* ── desktop / window ── */
    #desktop {
      position: fixed; top: var(--bar-h); left: 0; right: 0; bottom: 0;
      z-index: 1; padding: 8px;
    }

    #window {
      width: 100%; height: 100%;
      display: flex; flex-direction: column;
      background: var(--glass);
      backdrop-filter: var(--blur);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      box-shadow: 0 24px 64px rgba(0,0,0,.6), 0 0 0 .5px rgba(203,166,247,.07);
      overflow: hidden;
      animation: win-open .22s cubic-bezier(.16, 1, .3, 1);
    }
    @keyframes win-open {
      from { transform: scale(.95) translateY(8px); opacity: 0; }
      to   { transform: scale(1)   translateY(0);   opacity: 1; }
    }

    /* title bar */
    .titlebar {
      height: 30px; flex-shrink: 0;
      display: flex; align-items: center; gap: 8px;
      padding: 0 10px;
      background: rgba(17, 17, 27, .6);
      border-bottom: 1px solid var(--border);
      user-select: none;
    }
    .traffic-lights { display: flex; gap: 6px; }
    .tl { width: 11px; height: 11px; border-radius: 50%; }
    .tl.red    { background: #f38ba8; }
    .tl.yellow { background: #f9e2af; }
    .tl.green  { background: #a6e3a1; }

    .window-title { flex: 1; text-align: center; font-size: 11px; color: var(--muted); letter-spacing: .06em; }

    .readonly-badge {
      display: none;
      font-size: 10px; color: var(--yellow);
      padding: 2px 7px; border-radius: 4px;
      background: rgba(249,226,175,.1);
      border: 1px solid rgba(249,226,175,.2);
    }

    /* terminal body */
    .term-body { flex: 1; min-height: 0; display: flex; flex-direction: column; padding: 2px 3px 3px; }
    #terminal  { flex: 1; min-height: 0; }

    /* xterm transparency */
    .xterm { height: 100% !important; }
    .xterm .xterm-viewport {
      background: transparent !important;
      overflow-y: hidden !important;
      scrollbar-width: none;
    }
    .xterm .xterm-viewport::-webkit-scrollbar { display: none !important; }
    .xterm-screen { background: transparent !important; }

    /* status bar */
    .statusbar {
      height: 20px; flex-shrink: 0;
      display: flex; align-items: stretch;
      background: rgba(17, 17, 27, .85);
      border-top: 1px solid var(--border);
      font-size: 10px; overflow: hidden;
    }
    .sb-pill { display: flex; align-items: center; gap: 4px; padding: 0 9px; white-space: nowrap; }
    .sb-mode { background: var(--mauve); color: #1e1e2e; font-weight: 700; }
    .sb-info { background: rgba(49, 50, 68, .8); color: var(--sub); }
    .sb-fill { flex: 1; background: transparent; color: var(--muted); justify-content: flex-end; }
    .sb-enc  { background: var(--blue); color: #1e1e2e; font-weight: 600; }
    .chev-r  { border-left: 7px solid; border-top: 10px solid transparent; border-bottom: 10px solid transparent; }
    .chev-l  { border-right: 7px solid; border-top: 10px solid transparent; border-bottom: 10px solid transparent; }

    /* ── connecting overlay ── */
    #overlay {
      position: fixed; inset: 0; z-index: 200;
      display: flex; align-items: center; justify-content: center;
      background: rgba(11, 11, 23, .72);
      backdrop-filter: blur(14px);
    }
    #overlay.hidden { display: none; }

    .overlay-box {
      background: rgba(17, 17, 27, .97);
      border: 1px solid var(--border);
      border-radius: 14px;
      padding: 36px 44px; text-align: center;
      box-shadow: 0 40px 100px rgba(0,0,0,.75);
      min-width: 340px;
    }
    .spinner {
      width: 28px; height: 28px; margin: 0 auto 16px;
      border: 2px solid rgba(203,166,247,.2);
      border-top-color: var(--mauve);
      border-radius: 50%;
      animation: spin .8s linear infinite;
    }
    @keyframes spin { to { transform: rotate(360deg); } }
    .overlay-title { font-size: 16px; font-weight: 700; color: var(--text); margin-bottom: 6px; }
    .overlay-sub   { font-size: 12px; color: var(--sub); line-height: 1.6; margin-bottom: 20px; }
    .overlay-icon  { font-size: 40px; margin-bottom: 12px; }
    .overlay-code  {
      background: rgba(30,30,46,.8); border: 1px solid var(--border); border-radius: 8px;
      padding: 10px 16px; font-size: 12px; color: var(--mauve);
      word-break: break-all; margin-bottom: 16px;
    }
    .overlay-btn {
      padding: 9px 22px; border-radius: 8px; border: none;
      background: var(--mauve); color: #1e1e2e;
      font-family: inherit; font-size: 12px; font-weight: 700; cursor: pointer;
    }
    .overlay-btn:hover { filter: brightness(1.1); }
  </style>
</head>
<body>

  <!-- Wallpaper -->
  <div id="wallpaper">
    <canvas id="wp-canvas"></canvas>
    <div id="wallpaper-dim"></div>
  </div>

  <!-- Top bar -->
  <header id="topbar">
    <div class="bar-left">
      <span class="logo">⚡ HyprShare</span>
      <div class="bar-sep"></div>
      <div class="dot connecting" id="status-dot"></div>
      <span id="agent-name">Connecting…</span>
      <span class="badge">{{SID}}</span>
    </div>
    <div class="bar-right">
      <span id="viewer-count">— viewers</span>
      <div class="bar-sep"></div>
      <span id="latency">— ms</span>
      <div class="bar-sep"></div>
      <button class="bar-btn green" onclick="copyUrl(event)">⎘ Copy URL</button>
      <button class="bar-btn" id="ro-btn" onclick="toggleReadOnly()">🔒 Read-only</button>
      <a class="bar-btn" href="/">⊞ Dashboard</a>
    </div>
  </header>

  <!-- Desktop -->
  <div id="desktop">
    <div id="window">

      <!-- Title bar -->
      <div class="titlebar">
        <div class="traffic-lights">
          <div class="tl red"></div>
          <div class="tl yellow"></div>
          <div class="tl green"></div>
        </div>
        <div class="window-title" id="window-title">kitty — HyprShare · {{SID}}</div>
        <div class="readonly-badge" id="readonly-badge">READ-ONLY</div>
      </div>

      <!-- Terminal -->
      <div class="term-body">
        <div id="terminal"></div>
      </div>

      <!-- Status bar -->
      <div class="statusbar">
        <div class="sb-pill sb-mode">SHARE</div>
        <div class="chev-r" style="border-left-color: var(--mauve);"></div>
        <div class="sb-pill sb-info" id="sb-info">connecting…</div>
        <div class="sb-pill sb-fill">utf-8 · xterm-256color</div>
        <div class="chev-l" style="border-right-color: var(--blue);"></div>
        <div class="sb-pill sb-enc">UTF-8</div>
      </div>

    </div>
  </div>

  <!-- Connecting overlay -->
  <div id="overlay">
    <div class="overlay-box">
      <div class="spinner"></div>
      <div class="overlay-title">Connecting to session</div>
      <div class="overlay-sub">
        Establishing connection to agent…<br>
        Session: <code style="color: var(--mauve)">{{SID}}</code>
      </div>
    </div>
  </div>

  <script>
    "use strict";

    // ── constants ────────────────────────────────────────────────────────────
    const SID     = "{{SID}}";
    const WS_URL  = `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/viewer/ws/${SID}`;

    // ── state ────────────────────────────────────────────────────────────────
    let ws          = null;
    let readOnly    = false;
    let pingStart   = 0;
    let retryDelay  = 1000;

    // ── xterm.js setup ───────────────────────────────────────────────────────
    const term = new Terminal({
      fontFamily:          '"JetBrains Mono", "Fira Code", monospace',
      fontSize:            14,
      lineHeight:          1.28,
      cursorBlink:         true,
      allowTransparency:   true,
      scrollback:          8000,
      smoothScrollDuration: 80,
      theme: {
        background:          "#1e1e2e",
        foreground:          "#cdd6f4",
        cursor:              "#f5e0dc",
        selectionBackground: "rgba(203, 166, 247, .3)",
        black:    "#45475a", red:     "#f38ba8",
        green:    "#a6e3a1", yellow:  "#f9e2af",
        blue:     "#89b4fa", magenta: "#cba6f7",
        cyan:     "#94e2d5", white:   "#bac2de",
        brightBlack:   "#585b70", brightRed:     "#f38ba8",
        brightGreen:   "#a6e3a1", brightYellow:  "#f9e2af",
        brightBlue:    "#89b4fa", brightMagenta: "#cba6f7",
        brightCyan:    "#94e2d5", brightWhite:   "#a6adc8",
      },
    });

    const fitAddon   = new FitAddon.FitAddon();
    const linksAddon = new WebLinksAddon.WebLinksAddon();
    term.loadAddon(fitAddon);
    term.loadAddon(linksAddon);
    term.open(document.getElementById("terminal"));
    setTimeout(() => { fitAddon.fit(); term.focus(); }, 100);

    // Forward user keystrokes to server (unless read-only)
    term.onData(data => {
      if (!readOnly && ws?.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: "input", data }));
      }
    });

    // Resize: fit terminal then notify server
    function sendResize() {
      if (ws?.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: "resize", cols: term.cols, rows: term.rows }));
      }
    }

    new ResizeObserver(() => setTimeout(() => { fitAddon.fit(); sendResize(); }, 50))
      .observe(document.getElementById("terminal"));
    window.addEventListener("resize", () => { fitAddon.fit(); sendResize(); });

    // ── WebSocket connection ─────────────────────────────────────────────────
    function connect() {
      setStatus("connecting");
      document.getElementById("overlay").classList.remove("hidden");

      ws = new WebSocket(WS_URL);

      ws.onopen = () => {
        retryDelay = 1000;
        document.getElementById("overlay").classList.add("hidden");
        setStatus("live");
        document.getElementById("sb-info").textContent = `live · ${SID}`;
        sendResize();
        schedulePing();
      };

      ws.onmessage = ({ data }) => handleMessage(JSON.parse(data));

      ws.onclose = () => {
        setStatus("dead");
        document.getElementById("sb-info").textContent = "reconnecting…";
        setTimeout(connect, retryDelay);
        retryDelay = Math.min(retryDelay * 1.5, 10_000);
      };

      ws.onerror = () => ws.close();
    }

    function handleMessage(msg) {
      switch (msg.type) {
        case "output":
          term.write(msg.data);
          break;

        case "pong":
          document.getElementById("latency").textContent = `${Date.now() - pingStart} ms`;
          break;

        case "meta":
          document.getElementById("agent-name").textContent  = msg.name || SID;
          document.getElementById("window-title").textContent = `${msg.name} — HyprShare · ${SID}`;
          document.getElementById("viewer-count").textContent =
            `${msg.viewers} viewer${msg.viewers !== 1 ? "s" : ""}`;
          break;

        case "disconnect":
          term.write(`\\r\\n\\x1b[33m[HyprShare] ${msg.message}\\x1b[0m\\r\\n`);
          setStatus("dead");
          document.getElementById("sb-info").textContent = "agent offline";
          break;

        case "error":
          showError(msg.message);
          break;
      }
    }

    function schedulePing() {
      setInterval(() => {
        if (ws?.readyState === WebSocket.OPEN) {
          pingStart = Date.now();
          ws.send(JSON.stringify({ type: "ping" }));
        }
      }, 5000);
    }

    // ── UI helpers ───────────────────────────────────────────────────────────
    function setStatus(state) {
      document.getElementById("status-dot").className = `dot ${state}`;
    }

    function showError(message) {
      const overlay = document.getElementById("overlay");
      overlay.classList.remove("hidden");
      overlay.innerHTML = `
        <div class="overlay-box">
          <div class="overlay-icon">⚠️</div>
          <div class="overlay-title">Session Not Found</div>
          <div class="overlay-sub">${message}<br><br>The session may have expired.</div>
          <div class="overlay-code">${location.href}</div>
          <button class="overlay-btn" onclick="location.reload()">Retry</button>
        </div>`;
    }

    function copyUrl(event) {
      navigator.clipboard.writeText(location.href);
      const btn = event.target;
      btn.textContent = "✓ Copied!";
      setTimeout(() => btn.textContent = "⎘ Copy URL", 2000);
    }

    function toggleReadOnly() {
      readOnly = !readOnly;
      document.getElementById("ro-btn").textContent       = readOnly ? "✏️ Read-write" : "🔒 Read-only";
      document.getElementById("readonly-badge").style.display = readOnly ? "inline-flex" : "none";
      term.options.cursorBlink = !readOnly;
    }

    // ── Particle wallpaper ───────────────────────────────────────────────────
    (function initParticles() {
      const canvas  = document.getElementById("wp-canvas");
      const ctx     = canvas.getContext("2d");

      function resize() {
        canvas.width  = window.innerWidth;
        canvas.height = window.innerHeight;
      }
      resize();
      window.addEventListener("resize", resize);

      const particles = Array.from({ length: 90 }, () => ({
        x:  Math.random() * canvas.width,
        y:  Math.random() * canvas.height,
        vx: (Math.random() - .5) * .35,
        vy: (Math.random() - .5) * .35,
        r:  Math.random() * 1.8 + .4,
        h:  Math.random() * 60 + 240,
      }));

      function drawFrame() {
        ctx.fillStyle = "rgba(13, 13, 26, .1)";
        ctx.fillRect(0, 0, canvas.width, canvas.height);

        for (const p of particles) {
          p.x += p.vx;  p.y += p.vy;
          if (p.x < 0 || p.x > canvas.width)  p.vx *= -1;
          if (p.y < 0 || p.y > canvas.height)  p.vy *= -1;

          ctx.beginPath();
          ctx.arc(p.x, p.y, p.r, 0, Math.PI * 2);
          ctx.fillStyle = `hsla(${p.h}, 72%, 68%, .75)`;
          ctx.fill();
        }

        // Draw edges between close particles
        for (let i = 0; i < particles.length; i++) {
          for (let j = i + 1; j < particles.length; j++) {
            const dx = particles[i].x - particles[j].x;
            const dy = particles[i].y - particles[j].y;
            const dist = Math.sqrt(dx * dx + dy * dy);
            if (dist < 110) {
              ctx.strokeStyle = `rgba(160, 130, 240, ${(1 - dist / 110) * .22})`;
              ctx.lineWidth   = .5;
              ctx.beginPath();
              ctx.moveTo(particles[i].x, particles[i].y);
              ctx.lineTo(particles[j].x, particles[j].y);
              ctx.stroke();
            }
          }
        }

        requestAnimationFrame(drawFrame);
      }

      ctx.fillStyle = "#0d0d1a";
      ctx.fillRect(0, 0, canvas.width, canvas.height);
      drawFrame();
    })();

    // ── boot ─────────────────────────────────────────────────────────────────
    connect();
  </script>
</body>
</html>
"""

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="HyprShare — self-hosted terminal sharing server",
    )
    parser.add_argument("--host",   default="0.0.0.0", help="Bind address (default: 0.0.0.0)")
    parser.add_argument("--port",   default=8000, type=int, help="Bind port (default: 8000)")
    parser.add_argument("--reload", action="store_true", help="Enable auto-reload (dev mode)")
    args = parser.parse_args()

    # Get the local IP address
    try:
        # Try to get the local IP by connecting to an external socket
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
    except Exception:
        # Fallback to hostname
        local_ip = socket.gethostbyname(socket.gethostname())

    print(f"""
╔══════════════════════════════════════════════════════╗
║            ⚡  HyprShare  v1.0                       ║
╠══════════════════════════════════════════════════════╣
║  Dashboard   →  http://localhost:{args.port}/               ║
║                                                      ║
║  Share a terminal from any machine:                  ║
║  curl -sSf http://{local_ip}:{args.port}/get | sh -s run  ║
╚══════════════════════════════════════════════════════╝
""")

    uvicorn.run(
        "server:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        ws_ping_interval=20,
        ws_ping_timeout=10,
    )
