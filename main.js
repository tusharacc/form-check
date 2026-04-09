const { app, BrowserWindow } = require('electron');
const path = require('path');
const { spawn, exec } = require('child_process');
const net = require('net');

const WS_PORT = 8765;
let pythonProcess = null;

// ── Port helpers ──────────────────────────────────────────────────────────────

/** Kill any process listening on WS_PORT, poll until the port is free. */
function killStaleBackend(callback) {
  exec(`lsof -ti:${WS_PORT}`, (err, stdout) => {
    if (err || !stdout.trim()) {
      return callback(); // nothing on the port
    }
    const pids = stdout.trim().split('\n').join(' ');
    console.log(`[FormCheck] Killing stale backend (pids ${pids})...`);
    exec(`kill ${pids} 2>/dev/null || true`, () => {
      let attempts = 0;
      const poll = setInterval(() => {
        attempts++;
        isPortFree(WS_PORT, (free) => {
          if (free) {
            clearInterval(poll);
            console.log(`[FormCheck] Port ${WS_PORT} is free.`);
            callback();
          } else if (attempts >= 10) {
            // Escalate to SIGKILL after 5 s
            console.log(`[FormCheck] WARNING: port ${WS_PORT} still in use — SIGKILL`);
            exec(`kill -9 ${pids} 2>/dev/null || true`, () => {
              clearInterval(poll);
              setTimeout(callback, 500);
            });
          }
        });
      }, 500);
    });
  });
}

/** Resolve true if nothing is listening on port. */
function isPortFree(port, callback) {
  const sock = net.createConnection({ port, host: '127.0.0.1' });
  sock.on('connect', () => { sock.destroy(); callback(false); });
  sock.on('error', () => callback(true));
}

/** Poll until something is listening on port, then call callback. */
function waitForPort(port, maxAttempts, callback) {
  let attempts = 0;
  const poll = setInterval(() => {
    attempts++;
    isPortFree(port, (free) => {
      if (!free) {
        clearInterval(poll);
        callback(true); // port is open
      } else if (attempts >= maxAttempts) {
        clearInterval(poll);
        callback(false); // timed out
      }
    });
  }, 500);
}

// ── Backend / window lifecycle ────────────────────────────────────────────────

function spawnBackend() {
  const backendDir = path.join(__dirname, 'backend');
  const fs = require('fs');
  const anacondaPy = '/opt/homebrew/anaconda3/bin/python3';
  const python = fs.existsSync(anacondaPy) ? anacondaPy : 'python3';

  pythonProcess = spawn(python, ['server.py'], {
    cwd: backendDir,
    stdio: ['ignore', 'pipe', 'pipe'],
    env: { ...process.env },
  });

  pythonProcess.stdout.on('data', (d) => process.stdout.write(`[backend] ${d}`));
  pythonProcess.stderr.on('data', (d) => process.stderr.write(`[backend] ${d}`));
  pythonProcess.on('exit', (code) => {
    console.log(`[FormCheck] Python backend exited (code ${code})`);
    pythonProcess = null;
  });
}

function startPythonBackend(onReady) {
  killStaleBackend(() => {
    console.log('[FormCheck] Starting Python backend...');
    spawnBackend();

    console.log(`[FormCheck] Waiting for backend on ws://127.0.0.1:${WS_PORT}...`);
    waitForPort(WS_PORT, 30, (ready) => { // up to 15 s
      if (ready) {
        console.log('[FormCheck] Backend ready.');
      } else {
        console.error('[FormCheck] ERROR: Backend did not open port within 15 s.');
      }
      onReady();
    });
  });
}

function createWindow() {
  const win = new BrowserWindow({
    width: 1100,
    height: 700,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });

  win.loadFile(path.join(__dirname, 'renderer', 'index.html'));
}

app.whenReady().then(() => {
  startPythonBackend(createWindow);

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on('window-all-closed', () => {
  if (pythonProcess) {
    pythonProcess.kill();
    pythonProcess = null;
  }
  app.quit();
});
