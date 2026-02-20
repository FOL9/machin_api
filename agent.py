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
import subprocess
import sys
import termios


# ──────────────────────────────────────────────────────────────────────────────
# Dependency bootstrap — install websockets by any means necessary
# ──────────────────────────────────────────────────────────────────────────────

def _run_cmd(*cmd) -> bool:
    """Run a command silently. Returns True on success."""
    try:
        r = subprocess.run(list(cmd), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return r.returncode == 0
    except FileNotFoundError:
        return False
    except Exception:
        return False


def _can_import(module: str) -> bool:
    """Check if a module can be imported (reloads sys.path each time)."""
    try:
        import importlib
        importlib.invalidate_caches()
        importlib.import_module(module)
        return True
    except ImportError:
        return False


def _pip_install(pip_cmd: list[str], extra_flags: list[str] = []) -> bool:
    """Run pip install websockets with given flags. Returns True if importable after."""
    cmd = pip_cmd + ["install"] + extra_flags + ["websockets"]
    print(f"[hyprshare]   → {' '.join(cmd)}")
    _run_cmd(*cmd)
    return _can_import("websockets")


def _ensure_websockets() -> None:
    if _can_import("websockets"):
        return

    print("[hyprshare] websockets not found — trying to install …")

    PY = sys.executable

    # ── 1. Collect all pip-like commands available ────────────────────────────
    pip_cmds = []
    if _run_cmd(PY, "-m", "pip", "--version"):
        pip_cmds.append([PY, "-m", "pip"])
    for bin_name in ["pip3", "pip"]:
        if _run_cmd(bin_name, "--version"):
            pip_cmds.append([bin_name])

    # ── 2. If no pip at all → bootstrap it first ─────────────────────────────
    if not pip_cmds:
        print("[hyprshare]   No pip found — bootstrapping …")

        # 2a. ensurepip (built into CPython, may be disabled on Debian)
        _run_cmd(PY, "-m", "ensurepip", "--upgrade")

        # 2b. get-pip.py via urllib (zero external deps)
        if not _run_cmd(PY, "-m", "pip", "--version"):
            try:
                import urllib.request, tempfile
                print("[hyprshare]   Downloading get-pip.py …")
                tmp = tempfile.mktemp(suffix=".py")
                urllib.request.urlretrieve("https://bootstrap.pypa.io/get-pip.py", tmp)
                _run_cmd(PY, tmp)
                try:
                    os.unlink(tmp)
                except Exception:
                    pass
            except Exception as e:
                print(f"[hyprshare]   get-pip.py failed: {e}")

        # re-check
        if _run_cmd(PY, "-m", "pip", "--version"):
            pip_cmds.append([PY, "-m", "pip"])

    # ── 3. Try every pip × every flag combination ─────────────────────────────
    flag_sets = [
        [],
        ["--break-system-packages"],
        ["--user"],
        ["--user", "--break-system-packages"],
        ["--ignore-installed"],
        ["--ignore-installed", "--break-system-packages"],
    ]
    for pip_cmd in pip_cmds:
        for flags in flag_sets:
            if _pip_install(pip_cmd, flags):
                print("[hyprshare] ✓ websockets installed via pip")
                return

    # ── 4. pip install --target → inject into sys.path ───────────────────────
    target_dir = os.path.join(os.path.expanduser("~"), ".local", "lib", "hyprshare-pkgs")
    os.makedirs(target_dir, exist_ok=True)
    for pip_cmd in pip_cmds:
        cmd = pip_cmd + ["install", "--target", target_dir, "websockets"]
        print(f"[hyprshare]   → {' '.join(cmd)}")
        if _run_cmd(*cmd):
            if target_dir not in sys.path:
                sys.path.insert(0, target_dir)
            if _can_import("websockets"):
                print("[hyprshare] ✓ websockets installed into local target dir")
                return

    # ── 5. System package managers ────────────────────────────────────────────
    pkg_cmds = [
        ["apt-get", "install", "-y", "-q", "python3-websockets"],
        ["apt",     "install", "-y", "-q", "python3-websockets"],
        ["dnf",     "install", "-y", "-q", "python3-websockets"],
        ["yum",     "install", "-y", "-q", "python3-websockets"],
        ["pacman",  "-S", "--noconfirm", "python-websockets"],
        ["apk",     "add", "--quiet", "py3-websockets"],
        ["zypper",  "install", "-y", "python3-websockets"],
        ["brew",    "install", "python-websockets"],
    ]
    for pkg in pkg_cmds:
        for prefix in [[], ["sudo"]]:
            cmd = prefix + pkg
            print(f"[hyprshare]   → {' '.join(cmd)}")
            if _run_cmd(*cmd) and _can_import("websockets"):
                print(f"[hyprshare] ✓ websockets installed via {pkg[0]}")
                return

    # ── 6. Download wheel directly from PyPI and unzip ────────────────────────
    try:
        import urllib.request, tempfile, zipfile, json as _json
        print("[hyprshare]   Trying direct PyPI wheel download …")
        with urllib.request.urlopen("https://pypi.org/pypi/websockets/json", timeout=10) as resp:
            data = _json.loads(resp.read())
        version = data["info"]["version"]
        wheel_url = None
        # Prefer pure-python wheel
        for f in data["releases"].get(version, []):
            fname = f["filename"]
            if fname.endswith(".whl") and "py3-none-any" in fname:
                wheel_url = f["url"]
                break
        # fallback: any whl
        if not wheel_url:
            for f in data["releases"].get(version, []):
                if f["filename"].endswith(".whl"):
                    wheel_url = f["url"]
                    break
        if wheel_url:
            tmp = tempfile.mktemp(suffix=".whl")
            urllib.request.urlretrieve(wheel_url, tmp)
            extract_dir = os.path.join(os.path.expanduser("~"), ".local", "lib", "hyprshare-ws")
            os.makedirs(extract_dir, exist_ok=True)
            with zipfile.ZipFile(tmp) as z:
                for member in z.namelist():
                    if member.startswith("websockets/"):
                        z.extract(member, extract_dir)
            os.unlink(tmp)
            if extract_dir not in sys.path:
                sys.path.insert(0, extract_dir)
            if _can_import("websockets"):
                print("[hyprshare] ✓ websockets installed via direct wheel download")
                return
    except Exception as e:
        print(f"[hyprshare]   Wheel download failed: {e}")

    # ── 7. Try other Python interpreters and re-exec ──────────────────────────
    for alt_py in ["python3", "python3.13", "python3.12", "python3.11",
                   "python3.10", "python3.9", "python3.8"]:
        if not _run_cmd(alt_py, "--version"):
            continue
        for flags in flag_sets:
            cmd = [alt_py, "-m", "pip", "install"] + flags + ["websockets"]
            if _run_cmd(*cmd):
                print(f"[hyprshare] ✓ websockets installed — re-execing as {alt_py} …")
                os.execvp(alt_py, [alt_py] + sys.argv)   # replace process

    # ── Give up ───────────────────────────────────────────────────────────────
    print("\n[hyprshare] ✗ Could not install websockets automatically.")
    print(f"  Python  : {sys.executable}  ({sys.version.split()[0]})")
    print(f"  pip     : {'found' if pip_cmds else 'NOT FOUND'}")
    print("\n  Please install manually:\n")
    print(f"    {PY} -m pip install --break-system-packages websockets")
    print(f"    {PY} -m pip install --user websockets")
    print("    sudo apt install python3-websockets   # Debian/Ubuntu")
    print("    sudo dnf install python3-websockets   # Fedora/RHEL")
    print("    apk add py3-websockets                # Alpine/Docker")
    sys.exit(1)


_ensure_websockets()

import websockets  # noqa: E402
from websockets.exceptions import ConnectionClosed  # noqa: E402


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

    await ws.send(json.dumps({
        "type":  "register",
        "name":  socket.gethostname(),
        "shell": os.environ.get("SHELL", "/bin/bash"),
        "rows":  rows,
        "cols":  cols,
    }))

    raw = await asyncio.wait_for(ws.recv(), timeout=10.0)
    msg = json.loads(raw)
    if msg.get("type") != "session":
        raise RuntimeError(f"Unexpected server response: {msg}")

    sid      = msg["sid"]
    view_url = msg["url"].replace("__SERVER__", server_url)
    _print_banner(sid, view_url)

    pid, fd = pty.fork()

    if pid == 0:
        shell = os.environ.get("SHELL", "/bin/bash")
        os.environ["TERM"]      = "xterm-256color"
        os.environ["COLORTERM"] = "truecolor"
        try:
            os.setsid()
        except Exception:
            pass
        os.execvp(shell, [shell])
        os._exit(1)

    _set_terminal_size(fd, rows, cols)

    loop = asyncio.get_event_loop()
    done = asyncio.Event()

    async def pty_reader() -> None:
        while not done.is_set():
            try:
                data = await loop.run_in_executor(None, lambda: os.read(fd, 8192))
                if not data:
                    break
                await ws.send(json.dumps({"type": "output",
                                          "data": data.decode("utf-8", errors="replace")}))
            except (OSError, ConnectionClosed):
                break
        done.set()

    async def server_reader() -> None:
        async for raw_msg in ws:
            if done.is_set():
                break
            try:
                m = json.loads(raw_msg)
                if m["type"] == "input":
                    os.write(fd, m["data"].encode())
                elif m["type"] == "resize":
                    _set_terminal_size(fd, int(m.get("rows", rows)), int(m.get("cols", cols)))
                elif m["type"] == "ping":
                    await ws.send(json.dumps({"type": "pong"}))
            except (OSError, ConnectionClosed):
                break
        done.set()

    def _on_sigwinch(*_) -> None:
        r, c = _get_terminal_size()
        _set_terminal_size(fd, r, c)

    try:
        signal.signal(signal.SIGWINCH, _on_sigwinch)
    except Exception:
        pass

    try:
        await asyncio.gather(pty_reader(), server_reader())
    finally:
        done.set()
        for cleanup in [lambda: os.kill(pid, signal.SIGKILL),
                        lambda: os.close(fd),
                        lambda: os.waitpid(pid, 0)]:
            try:
                cleanup()
            except Exception:
                pass


# ──────────────────────────────────────────────────────────────────────────────
# Connection manager (with reconnect)
# ──────────────────────────────────────────────────────────────────────────────

async def run(server_url: str, reconnect: bool = True) -> None:
    server_url = server_url.rstrip("/")
    ws_url = server_url.replace("http://", "ws://").replace("https://", "wss://") + "/agent/ws"
    print(f"[hyprshare] Connecting to {server_url} …")
    retry_delay = 2.0

    while True:
        try:
            async with websockets.connect(
                ws_url, ping_interval=20, ping_timeout=10, max_size=10 * 1024 * 1024
            ) as ws:
                retry_delay = 2.0
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
    sep = "─" * 56
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
    parser = argparse.ArgumentParser(description="HyprShare agent — share this terminal over the web")
    parser.add_argument("--server", required=True, metavar="URL",
                        help="HyprShare server URL, e.g. http://192.168.1.20:8000")
    parser.add_argument("--no-reconnect", action="store_true",
                        help="Exit immediately on disconnect instead of retrying")
    args = parser.parse_args()
    try:
        asyncio.run(run(args.server, reconnect=not args.no_reconnect))
    except KeyboardInterrupt:
        print("\n[hyprshare] Bye!")


if __name__ == "__main__":
    main()
