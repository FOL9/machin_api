#!/usr/bin/env python3
"""
app.py - Super Work Version 3.0 (Optimized for Low Latency & Smooth UI)

Combined Version:
- Frontend: Custom Draggable Window with Darkness/Transparent Theme, Dynamic Theme Switching (Ctrl+P), and Debounced Input.
- Backend: FastAPI SSHX-style pty handling with session management.

Run:
    pip install fastapi uvicorn
    uvicorn app:app --host 0.0.0.0 --port 8000 --reload

Documentation:
    Visit http://127.0.0.1:8000/docs for API information.
"""
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
import uuid
import pty
import os
import threading
import asyncio
import signal
import time
import typing
import sys

# --- FastAPI App Setup ---
app = FastAPI(
    title="Low Latency Web Terminal Service",
    description="FastAPI service providing a multi-user, web-based pseudo-terminal.",
    version="3.0.0",
)

# --- Global State for Sessions ---
sessions: typing.Dict[str, typing.Dict] = {}
sessions_lock = threading.Lock()
MAIN_LOOP: typing.Optional[asyncio.AbstractEventLoop] = None


@app.on_event("startup")
async def startup_event():
    global MAIN_LOOP
    MAIN_LOOP = asyncio.get_event_loop()


# --- PTY Reader Thread Logic (Unchanged from V2 for stability) ---

def reader_thread(sid_local: str):
    """
    Reads data from the PTY file descriptor and broadcasts it to all
    connected WebSockets for the given session ID.
    Attempts to respawn the shell if the PTY closes.
    """
    while True:
        sess = sessions.get(sid_local)
        if not sess or not sess.get("alive"):
            return 

        fd_local = sess["fd"]
        try:
            data = os.read(fd_local, 4096)
        except OSError:
            data = b""

        if data:
            text = data.decode("utf-8", errors="replace")
            if MAIN_LOOP:
                MAIN_LOOP.call_soon_threadsafe(asyncio.create_task, broadcast_to_session(sid_local, text))
            continue

        # PTY Closed / Shell Exited Handling
        if MAIN_LOOP:
            MAIN_LOOP.call_soon_threadsafe(asyncio.create_task, broadcast_to_session(sid_local, "\r\n\x1b[33m[pty exited — respawning...]\x1b[0m\r\n"))
        
        try:
            pid, new_fd = pty.fork()
        except Exception as e:
            if MAIN_LOOP:
                MAIN_LOOP.call_soon_threadsafe(asyncio.create_task, broadcast_to_session(sid_local, f"\r\n\x1b[31m[respawn failed: {e}]\x1b[0m\r\n"))
            time.sleep(1)
            continue

        if pid == 0:
            shell = os.environ.get("SHELL", "/bin/bash")
            try:
                os.setsid()
            except Exception:
                pass
            try:
                os.execvp(shell, [shell])
            except Exception:
                os._exit(1)

        with sess["lock"]:
            try:
                os.close(sess.get("fd"))
            except Exception:
                pass
            sess["fd"] = new_fd
            sess["pid"] = pid
            sess["alive"] = True
            
        time.sleep(0.05)


async def broadcast_to_session(sid: str, data: str):
    """Sends data to all connected websockets for a specific session."""
    sess = sessions.get(sid)
    if not sess:
        return
        
    websockets = list(sess["sockets"])
    to_remove = []
    for ws in websockets:
        try:
            await ws.send_text(data)
        except Exception:
            to_remove.append(ws)
            
    if to_remove:
        with sess["lock"]:
            for ws in to_remove:
                sess["sockets"].discard(ws)


# --- API Endpoints (Unchanged from V2) ---

@app.post("/session", tags=["Session Management"])
async def create_session():
    # ... (function body remains the same) ...
    sid = uuid.uuid4().hex
    try:
        pid, fd = pty.fork()
    except Exception as e:
        return JSONResponse({"error": f"pty.fork failed: {e}"}, status_code=500)

    if pid == 0:
        shell = os.environ.get("SHELL", "/bin/bash")
        try:
            os.setsid()
        except Exception:
            pass
        try:
            os.execvp(shell, [shell])
        except Exception:
            os._exit(1)

    session = {
        "fd": fd,
        "pid": pid,
        "sockets": set(),
        "lock": threading.Lock(),
        "alive": True,
    }
    with sessions_lock:
        sessions[sid] = session

    thr = threading.Thread(target=reader_thread, args=(sid,), daemon=True)
    thr.start()

    return JSONResponse({"sid": sid, "message": "Session created successfully."})


@app.get("/sessions", tags=["Session Management"])
async def list_sessions():
    with sessions_lock:
        keys = list(sessions.keys())
    return {"sessions": keys}


@app.post("/kill/{sid}", tags=["Session Management"])
async def kill_session(sid: str):
    with sessions_lock:
        sess = sessions.get(sid)
    if not sess:
        return JSONResponse({"error": "Session not found"}, status_code=404)
        
    sess["alive"] = False
    
    try:
        os.kill(sess["pid"], signal.SIGKILL)
    except Exception:
        pass
    
    try:
        os.close(sess["fd"])
    except Exception:
        pass
        
    with sessions_lock:
        sessions.pop(sid, None)
        
    return {"status": "success", "killed": sid, "message": "Session terminated."}


@app.websocket("/ws/{sid}")
async def ws_pty(websocket: WebSocket, sid: str):
    await websocket.accept()
    with sessions_lock:
        sess = sessions.get(sid)
        
    if not sess:
        await websocket.send_text("\x1b[31m[error] session not found\x1b[0m")
        await websocket.close()
        return

    with sess["lock"]:
        sess["sockets"].add(websocket)

    try:
        while True:
            try:
                # Expects a JSON string with 'type' and 'data'/'cols'/'rows'
                raw_data = await websocket.receive_text()
                
                # The backend handles data written directly to the pty
                # We assume the client only sends raw terminal data for input here
                os.write(sess["fd"], raw_data.encode())
                
            except WebSocketDisconnect:
                break
            except Exception:
                break
                
    finally:
        with sess["lock"]:
            sess["sockets"].discard(websocket)


# --- HTML Frontends ---

# API Documentation Page HTML (Unchanged from V2)
DOCS_HTML = r"""
<!doctype html>
<html lang="en">
<head>
    <meta charset="utf-8" />
    <title>API Documentation</title>
    <meta name="viewport" content="width=device-width,initial-scale=1" />
    <script src="https://cdn.tailwindcss.com"></script>
    <style>
        body { background-color: #0c0c0c; color: #ebdbb2; font-family: sans-serif; }
        .container { max-width: 800px; margin: 50px auto; padding: 20px; background: #1a1a1a; border-radius: 10px; box-shadow: 0 0 20px rgba(0,0,0,0.5); border: 1px solid #3c3836; }
        h1, h2 { color: #fe8019; border-bottom: 2px solid #504945; padding-bottom: 10px; margin-top: 20px; }
        code { background: #3c3836; padding: 2px 4px; border-radius: 3px; color: #a89984; font-family: monospace; font-size: 0.9em; }
        pre { background: #282828; padding: 15px; border-radius: 5px; overflow-x: auto; font-family: monospace; color: #ebdbb2; border: 1px solid #504945; }
        .method { font-weight: bold; padding: 2px 6px; border-radius: 4px; color: #1c1c1c; margin-right: 10px; }
        .POST { background-color: #b8bb26; } 
        .GET { background-color: #458588; } 
        .WS { background-color: #fe8019; } 
        .description { margin-top: 10px; color: #a89984; }
    </style>
</head>
<body>
    <div class="container">
        <h1 class="text-3xl font-bold">Web Terminal API Documentation</h1>
        <p class="description">Backend service for managing pseudo-terminal (PTY) sessions via FastAPI.</p>

        <h2 class="text-xl font-semibold">1. Session Management</h2>
        
        <div class="mb-6 p-4 border border-gray-700 rounded-md">
            <span class="method POST">POST</span><code class="text-lg">/session</code>
            <p class="description">Creates a new terminal session (pty fork) and returns a unique Session ID (SID).</p>
            <h3 class="font-bold text-sm mt-3">Response (200 OK)</h3>
            <pre>{
    "sid": "a1b2c3d4e5f6...",
    "message": "Session created successfully."
}</pre>
        </div>

        <div class="mb-6 p-4 border border-gray-700 rounded-md">
            <span class="method GET">GET</span><code class="text-lg">/sessions</code>
            <p class="description">Lists all currently active Session IDs.</p>
            <h3 class="font-bold text-sm mt-3">Response (200 OK)</h3>
            <pre>{
    "sessions": [
        "a1b2c3d4e5f6...",
        "g7h8i9j0k1l2..."
    ]
}</pre>
        </div>

        <div class="mb-6 p-4 border border-gray-700 rounded-md">
            <span class="method POST">POST</span><code class="text-lg">/kill/{sid}</code>
            <p class="description">Forcibly terminates the session specified by <code>{sid}</code>.</p>
            <h3 class="font-bold text-sm mt-3">Response (200 OK)</h3>
            <pre>{
    "status": "success",
    "killed": "a1b2c3d4e5f6...",
    "message": "Session terminated."
}</pre>
        </div>

        <h2 class="text-xl font-semibold">2. Terminal Communication</h2>

        <div class="mb-6 p-4 border border-gray-700 rounded-md">
            <span class="method WS">WS</span><code class="text-lg">/ws/{sid}</code>
            <p class="description">Establishes a WebSocket connection for bidirectional terminal I/O for the session <code>{sid}</code>.</p>
            <ul class="list-disc ml-6 mt-2 text-sm">
                <li><strong>Client to Server:</strong> Sends raw text input (key presses) to the PTY.</li>
                <li><strong>Server to Client:</strong> Receives PTY output and sends it back to the terminal client.</li>
            </ul>
        </div>
        
        <h2 class="text-xl font-semibold">3. Frontend</h2>
        <div class="mb-6 p-4 border border-gray-700 rounded-md">
            <span class="method GET">GET</span><code class="text-lg">/</code>
            <p class="description">Serves the main HTML page with the custom web terminal client.</p>
        </div>

    </div>
</body>
</html>
"""

@app.get("/docss", response_class=HTMLResponse, tags=["Documentation"])
async def docs_page():
    return HTMLResponse(DOCS_HTML)


# --- Main Terminal Page HTML (Theme Adjusted & New Elements Added) ---

INDEX_HTML = r"""
<!doctype html>
<html lang="en">
<head>
    <meta charset="utf-8" />
    <title>Web Terminal</title>
    <meta name="viewport" content="width=device-width,initial-scale=1" />
    
    <script src="https://cdn.tailwindcss.com"></script>
    
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/xterm@5.3.0/css/xterm.css" />
    <script src="https://cdn.jsdelivr.net/npm/xterm@5.3.0/lib/xterm.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/xterm-addon-fit@0.7.0/lib/xterm-addon-fit.js"></script>

    <style>
        /* Base Styles (Darkness/Transparent Theme) */
        * { margin: 0; padding: 0; box-sizing: border-box; }
        
        body {
            height: 100vh; width: 100vw; overflow: hidden;
            background-color: #0c0c0c;
            background-image: radial-gradient(#202020 1px, transparent 1px);
            background-size: 20px 20px;
            font-family: 'Menlo', 'Monaco', 'Courier New', monospace;
            color: #ebdbb2; user-select: none;
        }

        .window-container {
            position: absolute; top: 50%; left: 50%;
            transform: translate(-50%, -50%);
            width: 900px; height: 600px; max-width: 95vw; max-height: 90vh;
            background: rgba(18, 18, 18, 0.85); 
            backdrop-filter: blur(8px); -webkit-backdrop-filter: blur(8px);
            border-radius: 10px; box-shadow: 0 20px 60px rgba(0,0,0,0.7);
            border: 1px solid #3c3836;
            display: flex; flex-direction: column; overflow: hidden;
            transition: box-shadow 0.3s ease;
        }
        .window-header {
            height: 32px; background: rgba(18, 18, 18, 0.9);
            display: flex; align-items: center; justify-content: center;
            position: relative; cursor: grab; border-bottom: 1px solid #3c3836;
            flex-shrink: 0;
        }
        .controls { position: absolute; left: 10px; display: flex; gap: 8px; }
        .dot { width: 12px; height: 12px; border-radius: 50%; cursor: pointer; }
        .dot.red { background: #cc241d; } 
        .dot.yellow { background: #d79921; } 
        .dot.green { background: #98971a; }
        .dot.blue { background: #458588; }
        .title { font-size: 13px; color: #a89984; font-weight: 500; }
        .terminal-body { flex: 1; position: relative; background: transparent; overflow: hidden; display: flex; flex-direction: column; min-height: 0; }
        #terminal { flex: 1; min-height: 0; }
.xterm .xterm-viewport {
        background-color: transparent !important;
        /* --- 🛑 FIX: HIDE SCROLLBAR (Start) --- */
        overflow-y: hidden !important; /* Hide scrollbar for standard browsers */
        /* Hide scrollbar for WebKit (Chrome, Safari) */
        -ms-overflow-style: none; /* IE and Edge */
        scrollbar-width: none; /* Firefox */
        /* --- 🛑 FIX: HIDE SCROLLBAR (End) --- */
    }

    /* Hide scrollbar for WebKit (Chrome, Safari) - this is necessary */
    .xterm .xterm-viewport::-webkit-scrollbar {
        display: none !important;
        width: 0 !important;
        height: 0 !important;
    }
    
    .xterm-screen {
        background-color: transparent !important;
    }        .xterm { height: 100% !important; padding: 4px; }
        .status-bar { height: 24px; width: 100%; background: #3c3836; display: flex; font-size: 12px; line-height: 24px; font-weight: bold; cursor: default; flex-shrink: 0; }
        .sb-section { padding: 0 10px; }
        .sb-active { background: #fe8019; color: #1c1c1c; }
        .sb-inactive { background: #504945; color: #a89984; }
        .sb-right-accent { background: #d79921; color: #1c1c1c; padding: 0 15px; }
        .spacer { flex: 1; background: #3c3836; }
        .chevron-right { width:0;height:0;border-top:12px solid transparent;border-bottom:12px solid transparent;border-left:12px solid; border-right:none; }
        .chevron-left { width:0;height:0;border-top:12px solid transparent;border-bottom:12px solid transparent;border-right:12px solid; border-left:none; }
        .chevron-active { border-left-color: #fe8019; }
        .chevron-inactive { border-left-color: #504945; }
        .chevron-right-accent { border-right-color: #d79921; }

        /* --- NEW: Command Palette Styles --- */
        #command-palette {
            position: absolute; top: 40px; left: 50%;
            transform: translateX(-50%);
            width: 300px; max-height: 400px;
            background: rgba(30, 30, 30, 0.95);
            backdrop-filter: blur(5px);
            border: 1px solid #fe8019;
            border-radius: 6px;
            box-shadow: 0 10px 30px rgba(0,0,0,0.8);
            z-index: 100;
            overflow: hidden;
            display: none; /* Controlled by JS */
            flex-direction: column;
        }
        .palette-item {
            padding: 8px 15px;
            cursor: pointer;
            color: #ebdbb2;
            font-size: 14px;
            border-bottom: 1px solid #3c3836;
        }
        .palette-item:hover, .palette-item.selected {
            background: #fe8019; /* Gruvbox Orange for selection */
            color: #1c1c1c;
        }
        .palette-title {
             padding: 8px 15px;
             font-weight: bold;
             color: #d79921; /* Yellow accent */
             border-bottom: 2px solid #504945;
        }
    </style>
</head>
<body>

    <div id="main-window" class="window-container">
        
        <div class="window-header" id="drag-handle">
            <div class="controls">
                <div class="dot red" onclick="location.reload()" title="Reload Terminal"></div>
                <div class="dot yellow" onclick="window.open('/', '_blank')" title="New Terminal Session"></div>
                <div class="dot blue" onclick="window.open('/docs', '_blank')" title="View API Docs"></div>
            </div>
            <div class="title">term://fastapi-pty</div>
        </div>

        <div class="terminal-body">
            <div id="terminal"></div>
        </div>

        <div class="status-bar">
            <div class="sb-section sb-active" id="sb-mode">NORMAL</div>
            <div class="chevron-right chevron-active" id="sb-chevron-1"></div>
            
            <div class="sb-section sb-inactive" style="background:transparent; color:#999;" id="sb-theme-name"> Gruvbox Dark </div>
            
            <div class="spacer"></div>
            
            <div class="chevron-left chevron-right-accent"></div>
            <div class="sb-right-accent">
                utf-8
            </div>
        </div>
        
        <div id="command-palette">
            <div class="palette-title">Select Terminal Theme (Ctrl+P)</div>
            </div>

        <div style="position:fixed; bottom:10px; right:10px; font-size:12px; color:#504945;">
            **Keyboard Shortcuts:** <span class="text-fe8019" style="color:#fe8019;">Ctrl + P</span> to Change Theme. <span class="text-fe8019" style="color:#fe8019;">Yellow Dot</span> for New Terminal.
        </div>
    </div>

    <script>
        // --- 0. Theme Definitions ---
        const THEMES = {
            gruvbox_dark: {
                name: "Gruvbox Dark", background: '#1c1c1c', foreground: '#ebdbb2', cursor: '#ebdbb2',
                selectionBackground: 'rgba(235, 219, 178, 0.3)',
                black: '#282828', red: '#cc241d', green: '#98971a', yellow: '#d79921',
                blue: '#458588', magenta: '#b16286', cyan: '#689d6a', white: '#a89984',
                brightBlack: '#928374', brightRed: '#fb4934', brightGreen: '#b8bb26', brightYellow: '#fabd2f',
                brightBlue: '#83a598', brightMagenta: '#d3869b', brightCyan: '#8ec07c', brightWhite: '#ebdbb2'
            },
            dracula: {
                name: "Dracula", background: '#282a36', foreground: '#f8f8f2', cursor: '#f8f8f2',
                selectionBackground: 'rgba(68, 71, 90, 0.5)',
                black: '#21222c', red: '#ff5555', green: '#50fa7b', yellow: '#f1fa8c',
                blue: '#bd93f9', magenta: '#ff79c6', cyan: '#8be9fd', white: '#f8f8f2',
                brightBlack: '#6272a4', brightRed: '#ff6e6e', brightGreen: '#69ff94', brightYellow: '#ffffa5',
                brightBlue: '#d6acff', brightMagenta: '#ff92df', brightCyan: '#a4ffff', brightWhite: '#ffffff'
            },
            solarized_dark: {
                name: "Solarized Dark", background: '#002b36', foreground: '#839496', cursor: '#839496',
                selectionBackground: 'rgba(101, 123, 131, 0.5)',
                black: '#073642', red: '#dc322f', green: '#859900', yellow: '#b58900',
                blue: '#268bd2', magenta: '#d33682', cyan: '#2aa198', white: '#eee8d5',
                brightBlack: '#002b36', brightRed: '#cb4b16', brightGreen: '#586e75', brightYellow: '#657b83',
                brightBlue: '#839496', brightMagenta: '#6c71c4', brightCyan: '#93a1a1', brightWhite: '#fdf6e3'
            },
            dark_default: {
                name: "Dark Default", background: '#000000', foreground: '#ffffff', cursor: '#ffffff',
                selectionBackground: 'rgba(255, 255, 255, 0.3)',
                black: '#000000', red: '#cd0000', green: '#00cd00', yellow: '#cdcd00',
                blue: '#0000cd', magenta: '#cd00cd', cyan: '#00cdcd', white: '#e5e5e5',
                brightBlack: '#7f7f7f', brightRed: '#ff0000', brightGreen: '#00ff00', brightYellow: '#ffff00',
                brightBlue: '#0000ff', brightMagenta: '#ff00ff', brightCyan: '#00ffff', brightWhite: '#ffffff'
            }
        };

        const themeKeys = Object.keys(THEMES);

        function applyTheme(key) {
            const theme = THEMES[key];
            if (!theme) return;
            term.options.theme = theme;
            document.getElementById('sb-theme-name').textContent = ` ${theme.name} `;
            localStorage.setItem('terminalTheme', key);
            
            // Ensure background transparency for the blur effect
            const xtermScreen = document.querySelector('.xterm-screen');
            if (xtermScreen) {
                xtermScreen.style.backgroundColor = 'transparent';
            }
        }
        
        // Load theme from localStorage or default
        const savedThemeKey = localStorage.getItem('terminalTheme') || 'gruvbox_dark';
        
        
        // --- 1. Debounce Function for Low Latency Input ---
        function debounce(func, wait) {
            let timeout;
            return function(...args) {
                const context = this;
                clearTimeout(timeout);
                timeout = setTimeout(() => func.apply(context, args), wait);
            };
        }
        
        // --- 2. Draggable Logic (No change needed) ---
        const win = document.getElementById('main-window');
        const handle = document.getElementById('drag-handle');
        
        let isDragging = false;
        let startX, startY, initialLeft, initialTop;

        handle.addEventListener('mousedown', (e) => {
            if(e.target.classList.contains('dot')) return;
            isDragging = true;
            const rect = win.getBoundingClientRect();
            startX = e.clientX;
            startY = e.clientY;
            initialLeft = rect.left;
            initialTop = rect.top;
            win.style.transform = 'none';
            win.style.left = initialLeft + 'px';
            win.style.top = initialTop + 'px';
        });

        window.addEventListener('mousemove', (e) => {
            if (!isDragging) return;
            const dx = e.clientX - startX;
            const dy = e.clientY - startY;
            win.style.left = (initialLeft + dx) + 'px';
            win.style.top = (initialTop + dy) + 'px';
        });

        window.addEventListener('mouseup', () => {
            isDragging = false;
        });


        // --- 3. Terminal & WebSocket Logic ---
        const term = new Terminal({
            fontFamily: '"Fira Code", Menlo, "Courier New", monospace',
            fontSize: 15,
            lineHeight: 1.2,
            cursorBlink: true,
            allowTransparency: true,
            scrollback: 1000
        });

        applyTheme(savedThemeKey);

        const fitAddon = new FitAddon.FitAddon();
        term.loadAddon(fitAddon);
        term.open(document.getElementById('terminal'));
        
        setTimeout(() => {
            fitAddon.fit();
            term.focus();
        }, 100);

        const wsScheme = (location.protocol === 'https:') ? 'wss' : 'ws';

        async function initSession() {
            try {
                // 1. Create Session via POST
                const res = await fetch('/session', { method: 'POST' });
                const data = await res.json();
                const sid = data.sid;

                // 2. Connect WebSocket
                const wsUrl = `${wsScheme}://${location.host}/ws/${sid}`;
                const ws = new WebSocket(wsUrl);
                
                // Debounced function to send input data
                const sendInput = debounce((dataToSend) => {
                    if (ws.readyState === WebSocket.OPEN) {
                        ws.send(dataToSend);
                    }
                }, 4); // Delay in ms: Adjust this value (e.g., 4ms) for best balance of latency vs throughput
                
                ws.onopen = () => {
                   console.log("Connected to session:", sid);
                   setTimeout(() => { fitAddon.fit(); term.focus(); }, 100);
                };

                ws.onmessage = (evt) => {
                  term.write(evt.data);
                };
                
                ws.onclose = () => {
                    term.write('\r\n\x1b[31m[Session Disconnected - Please Reload]\x1b[0m');
                }

                // *** CRITICAL FIX FOR TYPING LAG ***
                term.onData(data => {
                  sendInput(data); // Use debounced function
                });
                
                // Resize handling
                const resizeHandler = () => {
                    setTimeout(() => {
                        fitAddon.fit();
                        // Placeholder for sending PTY resize command to backend if needed
                    }, 50);
                };
                
                window.addEventListener('resize', resizeHandler);
                new ResizeObserver(resizeHandler).observe(document.getElementById('terminal'));

            } catch (err) {
                term.write(`\r\n\x1b[31mError initializing session: ${err}\x1b[0m`);
            }
        }

        initSession();

        // --- 4. Command Palette (Ctrl+P) Logic ---
        const palette = document.getElementById('command-palette');
        let paletteVisible = false;
        let selectedIndex = 0;

        function togglePalette(show) {
            paletteVisible = show;
            palette.style.display = show ? 'flex' : 'none';
            if (show) {
                renderPaletteItems();
                selectedIndex = 0;
                highlightSelectedItem();
            } else {
                term.focus(); // Return focus to terminal
            }
        }

        function renderPaletteItems() {
            palette.innerHTML = '<div class="palette-title">Select Terminal Theme (Ctrl+P)</div>';
            themeKeys.forEach((key, index) => {
                const item = document.createElement('div');
                item.className = 'palette-item';
                item.textContent = `Theme: ${THEMES[key].name}`;
                item.setAttribute('data-key', key);
                item.setAttribute('data-index', index);
                item.onclick = () => selectTheme(key);
                palette.appendChild(item);
            });
        }
        
        function highlightSelectedItem() {
            const items = palette.querySelectorAll('.palette-item');
            items.forEach((item, index) => {
                item.classList.toggle('selected', index === selectedIndex);
            });
        }
        
        function selectTheme(key) {
             applyTheme(key);
             togglePalette(false);
        }

        document.addEventListener('keydown', (e) => {
            // Check for Ctrl + P (or Cmd + P on Mac)
            if (e.key === 'p' && (e.ctrlKey || e.metaKey)) {
                e.preventDefault();
                togglePalette(!paletteVisible);
                return;
            }
            
            if (paletteVisible) {
                e.preventDefault(); // Prevent terminal input while palette is open
                
                if (e.key === 'Escape') {
                    togglePalette(false);
                } else if (e.key === 'ArrowDown') {
                    selectedIndex = (selectedIndex + 1) % themeKeys.length;
                    highlightSelectedItem();
                } else if (e.key === 'ArrowUp') {
                    selectedIndex = (selectedIndex - 1 + themeKeys.length) % themeKeys.length;
                    highlightSelectedItem();
                } else if (e.key === 'Enter') {
                    selectTheme(themeKeys[selectedIndex]);
                }
            }
        });

    </script>
</body>
</html>
"""

@app.get("/", response_class=HTMLResponse, tags=["Frontend"])
async def index():
    return HTMLResponse(INDEX_HTML)


if __name__ == "__main__":
    try:
        import uvicorn
        uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True)
    except ImportError:
        print("Please install uvicorn: pip install uvicorn")
