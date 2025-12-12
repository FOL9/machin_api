#!/usr/bin/env python3
"""
app.py - VERCEL COMPATIBLE VERSION (using HTTP Polling)

!!! WARNING: This architecture uses HTTP Polling instead of WebSockets.
!!! Latency will be higher, but it will work on platforms like Vercel.

Backend: FastAPI PTY handling with polling queues.

Run:
    pip install fastapi uvicorn
    uvicorn app:app --host 0.0.0.0 --port 8000 --reload
"""
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse
import uuid
import pty
import os
import threading
import asyncio
import signal
import typing
import time
import json
import sys

# --- Configuration ---
POLLING_TIMEOUT = 25  # seconds - standard long polling timeout (must be < Vercel/server timeout)

# --- FastAPI App Setup ---
app = FastAPI(
    title="Vercel-Compatible Polling Terminal Service",
    description="FastAPI service using HTTP polling for a multi-user, web-based pseudo-terminal.",
    version="3.0.0-polling",
)

# --- Global State for Sessions ---
sessions: typing.Dict[str, typing.Dict] = {}
sessions_lock = threading.Lock()
MAIN_LOOP: typing.Optional[asyncio.AbstractEventLoop] = None


@app.on_event("startup")
async def startup_event():
    global MAIN_LOOP
    # Get the loop for scheduling PTY output read by the thread
    MAIN_LOOP = asyncio.get_event_loop()


# --- PTY Reader Thread Logic (Modified for Queue) ---

def reader_thread(sid_local: str):
    """
    Reads data from the PTY file descriptor and puts it into the session's Queue.
    """
    while True:
        sess = sessions.get(sid_local)
        if not sess or not sess.get("alive"):
            return 

        fd_local = sess["fd"]
        try:
            # Read non-blocking data from the PTY master
            data = os.read(fd_local, 4096)
        except OSError:
            data = b""

        if data:
            text = data.decode("utf-8", errors="replace")
            # Schedule the queue insertion on the main event loop
            if MAIN_LOOP:
                # Use a small function to put the item to prevent thread safety issues on the Queue
                def put_to_queue():
                    queue: asyncio.Queue = sess["output_queue"]
                    # Add data to the queue
                    queue.put_nowait(text) 

                MAIN_LOOP.call_soon_threadsafe(put_to_queue)
            continue

        # PTY Closed / Shell Exited Handling (Respawn)
        if MAIN_LOOP:
            # Schedule a message to be put in the queue
            def put_respawn_message():
                queue: asyncio.Queue = sess["output_queue"]
                queue.put_nowait("\r\n\x1b[33m[pty exited — respawning...]\x1b[0m\r\n")

            MAIN_LOOP.call_soon_threadsafe(put_respawn_message)
        
        # Respawn logic (Remains synchronous in the thread)
        try:
            pid, new_fd = pty.fork()
        except Exception as e:
            if MAIN_LOOP:
                def put_respawn_fail():
                    queue: asyncio.Queue = sess["output_queue"]
                    queue.put_nowait(f"\r\n\x1b[31m[respawn failed: {e}]\x1b[0m\r\n")
                MAIN_LOOP.call_soon_threadsafe(put_respawn_fail)
            time.sleep(1)
            continue

        if pid == 0:
            # Child process: start the shell
            shell = os.environ.get("SHELL", "/bin/bash")
            try:
                os.setsid()
            except Exception:
                pass
            try:
                os.execvp(shell, [shell])
            except Exception:
                os._exit(1)

        # Parent process: update session with new PTY details
        with sess["lock"]:
            try:
                os.close(sess.get("fd"))
            except Exception:
                pass
            sess["fd"] = new_fd
            sess["pid"] = pid
            sess["alive"] = True
            
        time.sleep(0.05) # Throttle respawn loop


# --- API Endpoints (Modified for Polling) ---

@app.post("/session", tags=["Session Management"])
async def create_session():
    sid = uuid.uuid4().hex
    try:
        pid, fd = pty.fork()
    except Exception as e:
        return JSONResponse({"error": f"pty.fork failed: {e}"}, status_code=500)

    if pid == 0:
        # Child process: Execute the shell
        shell = os.environ.get("SHELL", "/bin/bash")
        try:
            os.setsid()
        except Exception:
            pass
        try:
            os.execvp(shell, [shell])
        except Exception:
            os._exit(1)

    # Parent process: Store session details with an output queue
    session = {
        "fd": fd,
        "pid": pid,
        "output_queue": asyncio.Queue(), # !!! NEW: Queue replaces the set of WebSockets
        "lock": threading.Lock(),
        "alive": True,
        "last_active": time.time()
    }
    with sessions_lock:
        sessions[sid] = session

    # Start the background thread for reading PTY output
    thr = threading.Thread(target=reader_thread, args=(sid,), daemon=True)
    thr.start()

    return JSONResponse({"sid": sid, "message": "Session created successfully."})


@app.get("/poll/{sid}", tags=["Terminal Communication (Polling)"])
async def poll_pty(sid: str):
    """
    Long polling endpoint. Holds the connection until data is available or timeout occurs.
    """
    sess = sessions.get(sid)
    if not sess or not sess.get("alive"):
        raise HTTPException(status_code=404, detail="Session not found or terminated")

    sess["last_active"] = time.time()
    queue: asyncio.Queue = sess["output_queue"]
    
    # Use a list to aggregate data received within a short burst
    output_data = []

    # Try to get data immediately without waiting
    try:
        data = queue.get_nowait()
        output_data.append(data)
    except asyncio.QueueEmpty:
        pass

    if not output_data:
        # If no data is available immediately, wait for one item or timeout
        try:
            # Long-poll: wait up to POLLING_TIMEOUT seconds for new data
            data = await asyncio.wait_for(queue.get(), timeout=POLLING_TIMEOUT)
            output_data.append(data)
        except asyncio.TimeoutError:
            # If timeout occurs, return an empty response to signal the client to poll again
            return JSONResponse({"output": "", "status": "timeout"})
        except Exception:
            # Handle other errors (like session cleanup during poll)
            raise HTTPException(status_code=500, detail="Polling error")

    # If we got data, try to fetch any immediate subsequent data for better throughput
    while True:
        try:
            data = queue.get_nowait()
            output_data.append(data)
        except asyncio.QueueEmpty:
            break
        except Exception:
            break

    # Combine all queue items into a single string
    combined_output = "".join(output_data)

    return JSONResponse({"output": combined_output, "status": "ok"})


@app.post("/input/{sid}", tags=["Terminal Communication (Polling)"])
async def send_input(sid: str, input_data: typing.Dict[str, str]):
    """
    Accepts keyboard input via POST and writes it to the PTY.
    """
    sess = sessions.get(sid)
    if not sess or not sess.get("alive"):
        raise HTTPException(status_code=404, detail="Session not found or terminated")

    sess["last_active"] = time.time()
    data = input_data.get("data")
    if not data:
        return JSONResponse({"status": "error", "message": "No input data provided"}, status_code=400)

    try:
        # Write data directly to the PTY master's file descriptor
        os.write(sess["fd"], data.encode())
        return JSONResponse({"status": "success"})
    except Exception as e:
        # Note: In a production environment, you would handle pty errors more gracefully
        raise HTTPException(status_code=500, detail=f"Failed to write to PTY: {e}")

# --- Cleanup Task (to prevent zombie processes) ---

def cleanup_old_sessions():
    """Synchronous function to run in a background task/thread to kill old sessions."""
    
    # Use the synchronous list function to prevent issues with thread access
    current_time = time.time()
    sids_to_kill = []
    
    with sessions_lock:
        for sid, sess in sessions.items():
            # If the session is inactive for 60 seconds (or more), mark it for kill.
            if current_time - sess.get("last_active", 0) > 60:
                sids_to_kill.append(sid)

    for sid in sids_to_kill:
        # Use the synchronous version of the kill logic
        with sessions_lock:
            sess = sessions.pop(sid, None)
            
        if sess:
            sess["alive"] = False
            try:
                # Use os.killpg to kill the entire process group
                os.killpg(os.getpgid(sess["pid"]), signal.SIGKILL)
            except Exception:
                try:
                    os.kill(sess["pid"], signal.SIGKILL)
                except Exception:
                    pass
            try:
                os.close(sess["fd"])
            except Exception:
                pass
            print(f"Killed inactive session: {sid}")


# --- Schedule Cleanup on the Event Loop ---

async def schedule_cleanup(background_tasks: BackgroundTasks):
    """Periodically schedules the synchronous cleanup function."""
    while True:
        # Schedule the synchronous cleanup function to run in the thread pool
        background_tasks.add_task(cleanup_old_sessions)
        await asyncio.sleep(30) # Run cleanup every 30 seconds

# Endpoint to trigger background cleanup (optional, mostly for Vercel/serverless)
@app.get("/cleanup", include_in_schema=False)
async def trigger_cleanup(background_tasks: BackgroundTasks):
    # This is often needed on serverless platforms where background scheduling is difficult.
    # We call it as a background task to ensure it runs without blocking the main event loop.
    background_tasks.add_task(cleanup_old_sessions)
    return JSONResponse({"status": "Cleanup task scheduled"})

# Start the periodic scheduling on startup (may not work well on Vercel's cold start model)
@app.on_event("startup")
async def start_periodic_cleanup():
    global MAIN_LOOP
    MAIN_LOOP = asyncio.get_event_loop()
    # Note: On true serverless, this background task might not persist or run reliably.
    # The /cleanup endpoint is a more reliable way to trigger cleanup on a poll/input event.
    # asyncio.create_task(schedule_cleanup(BackgroundTasks()))
    pass


# --- Documentation and Frontend (Minor Changes) ---

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
        .description { margin-top: 10px; color: #a89984; }
    </style>
</head>
<body>
    <div class="container">
        <h1 class="text-3xl font-bold">Web Terminal API Documentation (Polling)</h1>
        <p class="description">Backend service for managing pseudo-terminal (PTY) sessions via FastAPI, using **HTTP Polling** for Vercel/Serverless compatibility.</p>

        <h2 class="text-xl font-semibold">1. Session Management</h2>
        
        <div class="mb-6 p-4 border border-gray-700 rounded-md">
            <span class="method POST">POST</span><code class="text-lg">/session</code>
            <p class="description">Creates a new terminal session (pty fork) and returns a unique Session ID (SID).</p>
        </div>

        <h2 class="text-xl font-semibold">2. Terminal Communication (Polling)</h2>

        <div class="mb-6 p-4 border border-gray-700 rounded-md">
            <span class="method GET">GET</span><code class="text-lg">/poll/{sid}</code>
            <p class="description">**Long Polling:** Client uses this to fetch terminal output. The request is held open until new data is available or a {POLLING_TIMEOUT}s timeout is reached. Returns JSON: <code>{"output": "...", "status": "ok"|"timeout"}</code>.</p>
        </div>

        <div class="mb-6 p-4 border border-gray-700 rounded-md">
            <span class="method POST">POST</span><code class="text-lg">/input/{sid}</code>
            <p class="description">Client uses this to send raw text input (key presses) to the PTY via a JSON body: <code>{"data": "raw_input_text"}</code>.</p>
        </div>
    </div>
</body>
</html>
""".replace("{POLLING_TIMEOUT}", str(POLLING_TIMEOUT))

@app.get("/docss", response_class=HTMLResponse, tags=["Documentation"])
async def docs_page():
    return HTMLResponse(DOCS_HTML)


# --- Main Terminal Page HTML (JavaScript Rewritten for Polling) ---

INDEX_HTML = r"""
<!doctype html>
<html lang="en">
<head>
    <meta charset="utf-8" />
    <title>Web Terminal (Vercel Polling)</title>
    <meta name="viewport" content="width=device-width,initial-scale=1" />
    
    <script src="https://cdn.tailwindcss.com"></script>
    
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/xterm@5.3.0/css/xterm.css" />
    <script src="https://cdn.jsdelivr.net/npm/xterm@5.3.0/lib/xterm.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/xterm-addon-fit@0.7.0/lib/xterm-addon-fit.js"></script>

    <style>
        /* CSS from original file remains mostly unchanged */
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { height: 100vh; width: 100vw; overflow: hidden; background-color: #0c0c0c; background-image: radial-gradient(#202020 1px, transparent 1px); background-size: 20px 20px; font-family: 'Menlo', 'Monaco', 'Courier New', monospace; color: #ebdbb2; user-select: none; }
        .window-container { position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%); width: 900px; height: 600px; max-width: 95vw; max-height: 90vh; background: rgba(18, 18, 18, 0.85); backdrop-filter: blur(8px); -webkit-backdrop-filter: blur(8px); border-radius: 10px; box-shadow: 0 20px 60px rgba(0,0,0,0.7); border: 1px solid #3c3836; display: flex; flex-direction: column; overflow: hidden; transition: box-shadow 0.3s ease; }
        .window-header { height: 32px; background: rgba(18, 18, 18, 0.9); display: flex; align-items: center; justify-content: center; position: relative; cursor: grab; border-bottom: 1px solid #3c3836; flex-shrink: 0; }
        .controls { position: absolute; left: 10px; display: flex; gap: 8px; }
        .dot { width: 12px; height: 12px; border-radius: 50%; cursor: pointer; }
        .dot.red { background: #cc241d; } .dot.yellow { background: #d79921; } .dot.green { background: #98971a; } .dot.blue { background: #458588; }
        .title { font-size: 13px; color: #a89984; font-weight: 500; }
        .terminal-body { flex: 1; position: relative; background: transparent; overflow: hidden; display: flex; flex-direction: column; min-height: 0; }
        #terminal { flex: 1; min-height: 0; }
        .xterm .xterm-viewport { background-color: transparent !important; overflow-y: hidden !important; -ms-overflow-style: none; scrollbar-width: none; }
        .xterm .xterm-viewport::-webkit-scrollbar { display: none !important; width: 0 !important; height: 0 !important; }
        .xterm-screen { background-color: transparent !important; }        .xterm { height: 100% !important; padding: 4px; }
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
        /* Command Palette Styles */
        #command-palette { position: absolute; top: 40px; left: 50%; transform: translateX(-50%); width: 300px; max-height: 400px; background: rgba(30, 30, 30, 0.95); backdrop-filter: blur(5px); border: 1px solid #fe8019; border-radius: 6px; box-shadow: 0 10px 30px rgba(0,0,0,0.8); z-index: 100; overflow: hidden; display: none; flex-direction: column; }
        .palette-item { padding: 8px 15px; cursor: pointer; color: #ebdbb2; font-size: 14px; border-bottom: 1px solid #3c3836; }
        .palette-item:hover, .palette-item.selected { background: #fe8019; color: #1c1c1c; }
        .palette-title { padding: 8px 15px; font-weight: bold; color: #d79921; border-bottom: 2px solid #504945; }
    </style>
</head>
<body>

    <div id="main-window" class="window-container">
        
        <div class="window-header" id="drag-handle">
            <div class="controls">
                <div class="dot red" onclick="location.reload()" title="Reload Terminal"></div>
                <div class="dot yellow" onclick="window.open('/', '_blank')" title="New Terminal Session"></div>
                <div class="dot blue" onclick="window.open('/docss', '_blank')" title="View API Docs"></div>
            </div>
            <div class="title">term://fastapi-polling-vercel</div>
        </div>

        <div class="terminal-body">
            <div id="terminal"></div>
        </div>

        <div class="status-bar">
            <div class="sb-section sb-active" id="sb-mode">POLLING</div>
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
            **WARNING:** Running on HTTP Polling (High Latency). <span class="text-fe8019" style="color:#fe8019;">Ctrl + P</span> to Change Theme.
        </div>
    </div>

    <script>
        // --- 0. Theme Definitions (Unchanged) ---
        const THEMES = {
            gruvbox_dark: {
                name: "Gruvbox Dark", background: '#1c1c1c', foreground: '#ebdbb2', cursor: '#ebdbb2', selectionBackground: 'rgba(235, 219, 178, 0.3)',
                black: '#282828', red: '#cc241d', green: '#98971a', yellow: '#d79921', blue: '#458588', magenta: '#b16286', cyan: '#689d6a', white: '#a89984',
                brightBlack: '#928374', brightRed: '#fb4934', brightGreen: '#b8bb26', brightYellow: '#fabd2f', brightBlue: '#83a598', brightMagenta: '#d3869b', brightCyan: '#8ec07c', brightWhite: '#ebdbb2'
            },
            dracula: {
                name: "Dracula", background: '#282a36', foreground: '#f8f8f2', cursor: '#f8f8f2', selectionBackground: 'rgba(68, 71, 90, 0.5)',
                black: '#21222c', red: '#ff5555', green: '#50fa7b', yellow: '#f1fa8c', blue: '#bd93f9', magenta: '#ff79c6', cyan: '#8be9fd', white: '#f8f8f2',
                brightBlack: '#6272a4', brightRed: '#ff6e6e', brightGreen: '#69ff94', brightYellow: '#ffffa5', brightBlue: '#d6acff', brightMagenta: '#ff92df', brightCyan: '#a4ffff', brightWhite: '#ffffff'
            },
            solarized_dark: {
                name: "Solarized Dark", background: '#002b36', foreground: '#839496', cursor: '#839496', selectionBackground: 'rgba(101, 123, 131, 0.5)',
                black: '#073642', red: '#dc322f', green: '#859900', yellow: '#b58900', blue: '#268bd2', magenta: '#d33682', cyan: '#2aa198', white: '#eee8d5',
                brightBlack: '#002b36', brightRed: '#cb4b16', brightGreen: '#586e75', brightYellow: '#657b83', brightBlue: '#839496', brightMagenta: '#6c71c4', brightCyan: '#93a1a1', brightWhite: '#fdf6e3'
            },
            dark_default: {
                name: "Dark Default", background: '#000000', foreground: '#ffffff', cursor: '#ffffff', selectionBackground: 'rgba(255, 255, 255, 0.3)',
                black: '#000000', red: '#cd0000', green: '#00cd00', yellow: '#cdcd00', blue: '#0000cd', magenta: '#cd00cd', cyan: '#00cdcd', white: '#e5e5e5',
                brightBlack: '#7f7f7f', brightRed: '#ff0000', brightGreen: '#00ff00', brightYellow: '#ffff00', brightBlue: '#0000ff', brightMagenta: '#ff00ff', brightCyan: '#00ffff', brightWhite: '#ffffff'
            }
        };

        const themeKeys = Object.keys(THEMES);

        function applyTheme(key) {
            const theme = THEMES[key];
            if (!theme) return;
            term.options.theme = theme;
            document.getElementById('sb-theme-name').textContent = ` ${theme.name} `;
            localStorage.setItem('terminalTheme', key);
            const xtermScreen = document.querySelector('.xterm-screen');
            if (xtermScreen) {
                xtermScreen.style.backgroundColor = 'transparent';
            }
        }
        
        const savedThemeKey = localStorage.getItem('terminalTheme') || 'gruvbox_dark';
        
        
        // --- 1. Debounce Function for Polling Input (Unchanged) ---
        function debounce(func, wait) {
            let timeout;
            return function(...args) {
                const context = this;
                clearTimeout(timeout);
                timeout = setTimeout(() => func.apply(context, args), wait);
            };
        }
        
        // --- 2. Draggable Logic (Unchanged) ---
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


        // --- 3. Terminal & POLLING Logic (!!! REWRITTEN !!!) ---
        const term = new Terminal({
            fontFamily: '"Fira Code", Menlo, "Courier New", monospace',
            fontSize: 15, lineHeight: 1.2, cursorBlink: true, allowTransparency: true, scrollback: 1000
        });

        applyTheme(savedThemeKey);

        const fitAddon = new FitAddon.FitAddon();
        term.loadAddon(fitAddon);
        term.open(document.getElementById('terminal'));
        
        setTimeout(() => {
            fitAddon.fit();
            term.focus();
        }, 100);

        let currentSid = null;
        let isPolling = false;
        let isActive = true; // State flag to control the polling loop

        async function sendInput(dataToSend) {
            if (!currentSid) return;
            
            // Note: Sending input via debounced POST requests
            try {
                await fetch(`/input/${currentSid}`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ data: dataToSend })
                });
            } catch (error) {
                console.error("Input failed:", error);
                // Allow the poll loop to handle fatal session errors
            }
        }

        const debouncedSendInput = debounce(sendInput, 20); // Higher debounce for Polling

        async function startPollingLoop() {
            if (!currentSid || !isActive) return;
            isPolling = true;

            while (isActive && currentSid) {
                try {
                    const response = await fetch(`/poll/${currentSid}`);
                    
                    if (response.status === 404) {
                        // Session terminated on the server
                        term.write('\r\n\x1b[31m[Session Terminated on Server]\x1b[0m');
                        isActive = false;
                        break;
                    }

                    if (!response.ok) {
                         throw new Error(`HTTP Error: ${response.status}`);
                    }
                    
                    const data = await response.json();
                    
                    if (data.output && data.output.length > 0) {
                        term.write(data.output);
                    }
                    
                    if (data.status === 'timeout') {
                        // The server intentionally closed the connection after timeout,
                        // this is normal for long polling. Loop will continue to poll.
                    }
                    
                    // Optional: Call cleanup endpoint on every poll to ensure session cleanup on Vercel
                    // fetch('/cleanup').catch(() => {});

                } catch (error) {
                    // This handles network errors, fetch timeouts, or JSON parsing errors
                    if (isActive) { // Only log/display if we didn't deliberately stop
                        term.write(`\r\n\x1b[31m[Polling Error: ${error.message} - Retrying...]\x1b[0m`);
                        await new Promise(resolve => setTimeout(resolve, 1000)); // Wait 1 second before retrying
                    }
                }
            }
            isPolling = false;
        }

        async function initSession() {
            try {
                // 1. Create Session via POST
                const res = await fetch('/session', { method: 'POST' });
                if (!res.ok) throw new Error("Failed to create session.");

                const data = await res.json();
                currentSid = data.sid;

                term.write('\r\n\x1b[32m[Polling Session ID: ' + currentSid + ']\x1b[0m');
                term.write('\r\n\x1b[33m[Warning: High latency is expected with HTTP Polling.]\x1b[0m\r\n');
                
                // 2. Attach input handler
                term.onData(data => {
                    debouncedSendInput(data);
                });
                
                // 3. Start the continuous polling loop
                startPollingLoop();

            } catch (err) {
                term.write(`\r\n\x1b[31mFATAL: Error initializing session: ${err.message}\x1b[0m`);
            }
        }

        initSession();
        
        // Resize handling (unmodified, but note that PTY resize is not implemented in backend)
        const resizeHandler = () => {
            setTimeout(() => {
                fitAddon.fit();
                // To support full terminal functionality, a POST request with new cols/rows 
                // would be needed here, targeting a new backend endpoint.
            }, 50);
        };
        
        window.addEventListener('resize', resizeHandler);
        new ResizeObserver(resizeHandler).observe(document.getElementById('terminal'));

        // --- 4. Command Palette (Ctrl+P) Logic (Unchanged) ---
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
                term.focus(); 
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
            if (items[selectedIndex]) {
                items[selectedIndex].scrollIntoView({ block: 'nearest' });
            }
        }
        
        function selectTheme(key) {
             applyTheme(key);
             togglePalette(false);
        }

        document.addEventListener('keydown', (e) => {
            if (e.key === 'p' && (e.ctrlKey || e.metaKey)) {
                e.preventDefault();
                togglePalette(!paletteVisible);
                return;
            }
            
            if (paletteVisible) {
                e.preventDefault(); 
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
        # Note: Set reload=False for production environments
        uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True)
    except ImportError:
        print("Please install uvicorn: pip install uvicorn")
    except Exception as e:
        if 'No such file or directory' in str(e) and ('/bin/bash' in str(e) or 'sh' in str(e)):
             print("\nERROR: The application requires a Unix-like environment (Linux/macOS) with a shell like /bin/bash.")
             print("If running on Windows, you must run this inside WSL (Windows Subsystem for Linux).")
        else:
             print(f"An unexpected error occurred: {e}")
