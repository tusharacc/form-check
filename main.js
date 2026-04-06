const { app, BrowserWindow } = require('electron');
const path = require('path');
const { spawn } = require('child_process');

let pythonProcess = null;

function startPythonBackend() {
  const backendDir = path.join(__dirname, 'backend');
  // Use the full path to the anaconda python3 so it has access to
  // the installed packages (mediapipe, cv2, etc.).
  // Falls back to system python3 if anaconda is not present.
  const anacondaPy = '/opt/homebrew/anaconda3/bin/python3';
  const fs = require('fs');
  const python = fs.existsSync(anacondaPy) ? anacondaPy : 'python3';

  pythonProcess = spawn(python, ['server.py'], {
    cwd: backendDir,
    stdio: ['ignore', 'pipe', 'pipe'],
    // Inherit the parent env so macOS camera TCC permissions are honoured
    env: { ...process.env },
  });

  pythonProcess.stdout.on('data', (d) => process.stdout.write(`[backend] ${d}`));
  pythonProcess.stderr.on('data', (d) => process.stderr.write(`[backend] ${d}`));

  pythonProcess.on('exit', (code) => {
    console.log(`Python backend exited with code ${code}`);
    pythonProcess = null;
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
  startPythonBackend();
  // Small delay to let Python start listening before the window opens
  setTimeout(createWindow, 1500);

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
