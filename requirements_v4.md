# FormCheck Bug Fixes v4 — Port race condition, display wake lock, HealthKit import

## Project context
FormCheck is a macOS Electron + Python asyncio app. Python backend in `backend/`,
Electron renderer in `renderer/`. WebSocket on port 8765. Started via `start.sh`.

Current relevant files:
- start.sh                  — launcher: kills stale backend, starts python, waits for port, starts Electron
- backend/wakelock.py       — WakeLock class using `caffeinate`
- backend/watchbridge.py    — WatchBridge class using HealthKit + macOS Shortcuts
- backend/server.py         — asyncio WS server; imports WatchBridge and WakeLock

Read every file listed above before making any changes.

---

## Bug 1 — Port 8765 race condition in start.sh

### Symptom
`OSError: [Errno 48] address already in use` on startup, even after our kill-stale
fix. The log shows "Database initialised" printed twice — two server instances.

### Root cause
`kill $(lsof -ti:8765)` sends SIGTERM, then `sleep 0.5` immediately starts the new
backend. The OS may take 1–3 s to release the port after SIGTERM. The new server
starts before the port is actually free.

### Fix: backend/start.sh — poll until port is free

Replace the current stale-kill block:
```bash
if lsof -ti:8765 > /dev/null 2>&1; then
    echo "[FormCheck] Killing stale backend on port 8765..."
    kill $(lsof -ti:8765) 2>/dev/null || true
    sleep 0.5
fi
```

With this version that polls until the port is confirmed free:
```bash
if lsof -ti:8765 > /dev/null 2>&1; then
    echo "[FormCheck] Killing stale backend on port 8765..."
    kill $(lsof -ti:8765) 2>/dev/null || true
    # Wait up to 5 s for the port to actually be released
    for i in $(seq 1 10); do
        sleep 0.5
        if ! nc -z 127.0.0.1 8765 2>/dev/null; then
            break
        fi
        if [[ $i -eq 10 ]]; then
            echo "[FormCheck] WARNING: port 8765 still in use after 5 s — trying SIGKILL"
            kill -9 $(lsof -ti:8765) 2>/dev/null || true
            sleep 1
        fi
    done
    echo "[FormCheck] Port 8765 is free."
fi
```

---

## Bug 2 — Wake lock does not prevent display sleep

### Symptom
The laptop screen turns off during a session even though WakeLock is acquired.

### Root cause
`caffeinate -i` prevents *system* idle sleep only. It does NOT prevent display
sleep. The `-d` flag is required to keep the display awake.

### Fix: backend/wakelock.py — add -d flag to caffeinate

In `WakeLock.acquire()`, change:
```python
self._proc = subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])
```
To:
```python
self._proc = subprocess.Popen(["caffeinate", "-d", "-i", "-w", str(os.getpid())])
```

`-d` = prevent display sleep, `-i` = prevent system idle sleep, `-w <pid>` = auto-release when our process exits.

No other changes needed in wakelock.py.

---

## Bug 3 — HealthKit import fails silently; Apple Watch never connects

### Symptom
`_HK_AVAILABLE` is always `False`. No heart rate data is sent. No error is shown.

### Root cause (two parts)

**Part A — `HKSampleSortIdentifierStartDate` is not importable**
In pyobjc, `HKSampleSortIdentifierStartDate` is a string constant (`"startDate"`),
not an exported symbol of the HealthKit module. Trying to import it raises
`ImportError`, which the except-all guard catches, setting `_HK_AVAILABLE = False`.

**Part B — `pyobjc-framework-HealthKit` may not be installed**
Even if the import were correct, the package may not be present in the environment.

### Fix: backend/watchbridge.py — fix imports and add install check

#### Step 1 — Fix the import block

Replace the current import block:
```python
try:
    from Foundation import NSSet
    from HealthKit import (
        HKHealthStore,
        HKObjectType,
        HKQuantityTypeIdentifierHeartRate,
        HKUnit,
        HKSampleQuery,
        HKSampleSortIdentifierStartDate,
    )
    from Foundation import NSSortDescriptor
    _HK_AVAILABLE = True
except Exception:
    _HK_AVAILABLE = False
```

With:
```python
try:
    from Foundation import NSSet, NSSortDescriptor
    from HealthKit import (
        HKHealthStore,
        HKObjectType,
        HKQuantityTypeIdentifierHeartRate,
        HKUnit,
        HKSampleQuery,
    )
    # HKSampleSortIdentifierStartDate is a string constant in pyobjc, not a symbol
    HKSampleSortIdentifierStartDate = "startDate"
    _HK_AVAILABLE = True
except Exception as _hk_import_err:
    _HK_AVAILABLE = False
    # Store error for logging after get_logger is set up
    _HK_IMPORT_ERR = str(_hk_import_err)
else:
    _HK_IMPORT_ERR = None
```

#### Step 2 — Log the import error in __init__

In `WatchBridge.__init__`, after the existing log.info line, add:
```python
if not _HK_AVAILABLE and _HK_IMPORT_ERR:
    log.warning("HealthKit import failed: %s", _HK_IMPORT_ERR)
    log.warning("Install with: pip install pyobjc-framework-HealthKit")
```

#### Step 3 — Fix get_heart_rate sort descriptor

In `get_heart_rate()`, the sort descriptor uses `HKSampleSortIdentifierStartDate`
which is now the string `"startDate"`. The existing code is already correct after
the import fix — no other changes needed in that method.

### Fix: backend/pyproject.toml — add pyobjc-framework-HealthKit

In the `[project] dependencies` list, add:
```
"pyobjc-framework-HealthKit>=10.0",
```

### Fix: start.sh — install pyobjc-framework-HealthKit before starting backend

Before the `python server.py &` line, add a silent pip install so the dependency
is always present when the app starts:
```bash
echo "[FormCheck] Checking Python dependencies..."
pip install pyobjc-framework-HealthKit --quiet --disable-pip-version-check 2>/dev/null || true
```

---

## Implementation order

1. start.sh — Bug 1 (port polling) + Bug 3 (pip install)
2. backend/wakelock.py — Bug 2 (-d flag)
3. backend/watchbridge.py — Bug 3 (import fix + logging)
4. backend/pyproject.toml — Bug 3 (dependency)

---

## Acceptance criteria

1. Running `npm start` twice in a row (without stopping the first instance) does not
   produce OSError: address already in use — the stale backend is killed and the port
   is confirmed free before the new backend starts.
2. After start_session, `pgrep caffeinate` shows a running process; the laptop display
   remains on; after stop_session `pgrep caffeinate` shows no process.
3. `python3 -c "from HealthKit import HKHealthStore; print('ok')"` succeeds after the
   pip install added to start.sh runs.
4. `python3 -m py_compile backend/watchbridge.py` passes.
5. When pyobjc-framework-HealthKit is installed and a paired Apple Watch with heart rate
   data is present, WatchBridge logs "HealthKit authorization granted" and sends
   heart_rate messages to the renderer.
6. When pyobjc-framework-HealthKit is NOT installed, WatchBridge logs the import error
   and "Install with: pip install pyobjc-framework-HealthKit" — session continues normally.
7. Python syntax check passes: `python3 -m py_compile backend/wakelock.py backend/watchbridge.py backend/server.py`
