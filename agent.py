#!/usr/bin/env python3
"""
HyprShare Agent
===============
Runs on the machine you want to share.
Spawns a real PTY shell and relays I/O to the HyprShare server over WebSocket.

Usage (via curl installer):
    curl -sSf http://HOST/get | sh -s run

Usage (direct):
    python agent.py --server http://192.168.1.20:8000

Usage (after install to ~/.local/bin):
    hyprshare --server http://192.168.1.20:8000
"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import os
import pty
import signal
import socket
import struct
import sys
import termios

# auto-install websockets if missing
try:
    import websockets
    from websockets.exceptions import ConnectionClosed
except ImportError:
    import subprocess
    print("[hyprshare] Installing websockets …")
    installed = False

    def _try(cmd):
        try:
            subprocess.check_call(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except Exception:
            return False

    # 1. Try ensurepip first to bootstrap pip on systems that lack it
    _try([sys.executable, "-m", "ensurepip", "--upgrade"])

    # 2. Try pip with --break-system-packages (Debian/Ubuntu 23.04+, WSL2)
    if _try([sys.executable, "-m", "pip", "install", "--quiet",
             "--break-system-packages", "websockets"]):
        installed = True

    # 3. Plain pip (works on most other systems / venvs)
    if not installed and _try([sys.executable, "-m", "pip", "install",
                                "--quiet", "websockets"]):
        installed = True

    # 4. Try apt as a last resort (Debian/Ubuntu)
    if not installed and _try(["sudo", "apt-get", "install", "-y", "-q",
                                "python3-websockets"]):
        installed = True

    if not installed:
        print("[hyprshare] ERROR: Could not install 'websockets' automatically.")
        print("  Please install it manually with one of:")
        print(f"    {sys.executable} -m pip install --break-system-packages websockets")
        print("    sudo apt install python3-websockets")
        sys.exit(1)

    import websockets
    from websockets.exceptions import ConnectionClosed


# ──────────────────────────────────────────────────────────────────────────────
# PTY helpers
# ──────────────────────────────────────────────────────────────────────────────

def _get_terminal_size() -> tuple[int, int]:
    """Return (rows, cols) of the local terminal, or sane defaults."""
    try:
        packed = struct.pack("HHHH", 0, 0, 0, 0)
        result = fcntl.ioctl(sys.stdout.fileno(), termios.TIOCGWINSZ, packed)
        rows, cols, _, _ = struct.unpack("HHHH", result)
        return max(rows, 24), max(cols, 80)
    except Exception:
        return 24, 220


def _set_terminal_size(fd: int, rows: int, cols: int) -> None:
    try:
        packed = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(fd, termios.TIOCSWINSZ, packed)
    except Exception:
        pass


# ──────────────────────────────────────────────────────────────────────────────
# Core session loop
# ──────────────────────────────────────────────────────────────────────────────

async def _run_session(ws: websockets.WebSocketClientProtocol, server_url: str) -> None:
    """Register with the server, fork a PTY, and relay I/O until done."""

    rows, cols = _get_terminal_size()

    # 1. Register
    await ws.send(json.dumps({
        "type":  "register",
        "name":  socket.gethostname(),
        "shell": os.environ.get("SHELL", "/bin/bash"),
        "rows":  rows,
        "cols":  cols,
    }))

    # 2. Receive session assignment
    raw = await asyncio.wait_for(ws.recv(), timeout=10.0)
    msg = json.loads(raw)
    if msg.get("type") != "session":
        raise RuntimeError(f"Unexpected server response: {msg}")

    sid      = msg["sid"]
    view_url = msg["url"].replace("__SERVER__", server_url)

    _print_banner(sid, view_url)

    # 3. Fork PTY
    pid, fd = pty.fork()

    if pid == 0:
        # child — exec shell
        shell = os.environ.get("SHELL", "/bin/bash")
        os.environ["TERM"]      = "xterm-256color"
        os.environ["COLORTERM"] = "truecolor"
        try:
            os.setsid()
        except Exception:
            pass
        os.execvp(shell, [shell])
        os._exit(1)

    # parent — set initial size
    _set_terminal_size(fd, rows, cols)

    loop = asyncio.get_event_loop()
    done = asyncio.Event()

    # -- PTY → server ---------------------------------------------------------
    async def pty_reader() -> None:
        while not done.is_set():
            try:
                data = await loop.run_in_executor(None, lambda: os.read(fd, 8192))
                if not data:
                    break
                await ws.send(json.dumps({
                    "type": "output",
                    "data": data.decode("utf-8", errors="replace"),
                }))
            except (OSError, ConnectionClosed):
                break
        done.set()

    # -- server → PTY ---------------------------------------------------------
    async def server_reader() -> None:
        async for raw_msg in ws:
            if done.is_set():
                break
            try:
                msg = json.loads(raw_msg)
                if msg["type"] == "input":
                    os.write(fd, msg["data"].encode())
                elif msg["type"] == "resize":
                    r = int(msg.get("rows", rows))
                    c = int(msg.get("cols", cols))
                    _set_terminal_size(fd, r, c)
                elif msg["type"] == "ping":
                    await ws.send(json.dumps({"type": "pong"}))
            except (OSError, ConnectionClosed):
                break
        done.set()

    # -- SIGWINCH: propagate local terminal resize ----------------------------
    def _on_sigwinch(*_) -> None:
        r, c = _get_terminal_size()
        _set_terminal_size(fd, r, c)

    try:
        signal.signal(signal.SIGWINCH, _on_sigwinch)
    except Exception:
        pass

    # -- run both tasks until either side closes ------------------------------
    try:
        await asyncio.gather(pty_reader(), server_reader())
    finally:
        done.set()
        try:
            os.kill(pid, signal.SIGKILL)
        except Exception:
            pass
        try:
            os.close(fd)
        except Exception:
            pass
        try:
            os.waitpid(pid, 0)
        except Exception:
            pass


# ──────────────────────────────────────────────────────────────────────────────
# Connection manager (with reconnect)
# ──────────────────────────────────────────────────────────────────────────────

async def run(server_url: str, reconnect: bool = True) -> None:
    server_url = server_url.rstrip("/")
    ws_url     = server_url.replace("http://", "ws://").replace("https://", "wss://") + "/agent/ws"

    print(f"[hyprshare] Connecting to {server_url} …")

    retry_delay = 2.0

    while True:
        try:
            async with websockets.connect(
                ws_url,
                ping_interval=20,
                ping_timeout=10,
                max_size=10 * 1024 * 1024,
            ) as ws:
                retry_delay = 2.0          # reset on successful connect
                await _run_session(ws, server_url)

        except (ConnectionClosed, OSError) as exc:
            if not reconnect:
                print(f"[hyprshare] Disconnected: {exc}")
                return
            print(f"[hyprshare] Connection lost ({exc}). Retrying in {retry_delay:.0f}s …")

        except KeyboardInterrupt:
            print("\n[hyprshare] Stopped.")
            return

        except Exception as exc:
            if not reconnect:
                raise
            print(f"[hyprshare] Error: {exc}. Retrying in {retry_delay:.0f}s …")

        if not reconnect:
            return

        await asyncio.sleep(retry_delay)
        retry_delay = min(retry_delay * 1.5, 30.0)


# ──────────────────────────────────────────────────────────────────────────────
# UI
# ──────────────────────────────────────────────────────────────────────────────

def _print_banner(sid: str, url: str) -> None:
    width = 56
    sep   = "─" * width
    print(f"\n{sep}")
    print(f"  ⚡  HyprShare — Session Active")
    print(sep)
    print(f"  Session  {sid}")
    print(f"  URL      {url}")
    print(sep)
    print(f"  Open the URL in any browser to view / type.")
    print(f"  Press Ctrl+C to stop.\n")


# ──────────────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="HyprShare agent — share this terminal over the web",
    )
    parser.add_argument(
        "--server", required=True,
        metavar="URL",
        help="HyprShare server URL, e.g. http://192.168.1.20:8000",
    )
    parser.add_argument(
        "--no-reconnect", action="store_true",
        help="Exit immediately on disconnect instead of retrying",
    )
    args = parser.parse_args()

    try:
        asyncio.run(run(args.server, reconnect=not args.no_reconnect))
    except KeyboardInterrupt:
        print("\n[hyprshare] Bye!")


if __name__ == "__main__":
    main()
