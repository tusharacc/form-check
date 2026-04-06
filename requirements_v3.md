# FormCheck Feature Additions v3 — Apple Watch Integration + Wake Lock

## Project context
FormCheck is a macOS Electron + Python asyncio app. Python backend lives in `backend/`,
Electron renderer in `renderer/`. WebSocket on port 8765. Three asyncio tasks run per
client connection: camera_loop, analysis_loop, handle_client.

Current relevant files:
- backend/server.py     — asyncio WS server; handle_client creates state dict and tasks
- backend/sound.py      — SoundEngine for audio alerts (usage pattern to follow)
- backend/logger.py     — get_logger(__name__) used by every module
- backend/pyproject.toml — Python dependency list
- renderer/index.html   — UI layout: calibration overlay, canvas, sidebar, journal, summary
- renderer/app.js       — Frontend WS client; handles message types in a switch statement

watchbridge.py does NOT exist yet.
wakelock.py does NOT exist yet.

Read every file listed above before making any changes.

---

## Feature 1 — Apple Watch: heart rate stream + session notification

### Overview

On session start the backend:
1. Fires a macOS Shortcut called `"FormCheck Session Start"` (user-created, see setup note).
2. Starts polling Apple Watch heart rate every 30 s via HealthKit (pyobjc).
3. Sends `{"type": "heart_rate", "bpm": <int>}` WebSocket messages to the renderer.

On session stop the backend fires `"FormCheck Session Stop"` Shortcut.

If HealthKit is unavailable (import fails, authorization denied, Watch not paired),
every call fails silently — a WARNING is logged and heart rate messages are simply not sent.
The rest of the session continues normally.

### User setup note (document as a comment at the top of watchbridge.py)

The user must create two Shortcuts on their Mac (Shortcuts app → New Shortcut):
  • "FormCheck Session Start" — e.g. open Workout app on Watch, show a notification
  • "FormCheck Session Stop"  — e.g. end the workout, show a notification
These Shortcuts are invoked via `shortcuts run "<name>"`.  If they don't exist the
subprocess call returns non-zero and a WARNING is logged; nothing breaks.

### New file: backend/watchbridge.py

Use `from logger import get_logger` and `log = get_logger(__name__)`.

Conditional HealthKit import at module level (so the module loads even if pyobjc absent):
```python
try:
    from Foundation import NSSet
    from HealthKit import (
        HKHealthStore,
        HKObjectType,
        HKQuantityTypeIdentifierHeartRate,
        HKUnit,
        HKSampleQuery,
        NSSortDescriptor,
        HKSampleSortIdentifierStartDate,
    )
    _HK_AVAILABLE = True
except Exception:
    _HK_AVAILABLE = False
```

Class `WatchBridge`:

```
__init__(self) -> None
```
- `self._store = HKHealthStore.alloc().init() if _HK_AVAILABLE else None`
- `self._authorized = False`
- Log "WatchBridge initialised — healthkit_available=%s" with _HK_AVAILABLE

```
request_authorization(self) -> None
```
- If not _HK_AVAILABLE: log WARNING "HealthKit unavailable — skipping auth"; return
- Build read_types: NSSet containing HKObjectType.quantityTypeForIdentifier_("HKQuantityTypeIdentifierHeartRate")
- Call self._store.requestAuthorizationToShareTypes_readTypes_completion_(None, read_types, callback)
  where callback sets self._authorized = True and logs "HealthKit authorization granted"
  (on denial: log WARNING "HealthKit authorization denied — no heart rate data")
- Block until callback fires (use threading.Event with a 10 s timeout)
- Wrap entire method in try/except; on any Exception log WARNING and set self._authorized = False

```
get_heart_rate(self) -> int | None
```
- If not _HK_AVAILABLE or not self._authorized: return None
- Build HKSampleQuery for HKQuantityTypeIdentifierHeartRate, limit=1, sorted by
  HKSampleSortIdentifierStartDate descending
- Use threading.Event to wait for the results handler (timeout 5 s)
- In the results handler: if samples non-empty, extract
  `sample.quantity().doubleValueForUnit_(HKUnit.unitFromString_("count/min"))`,
  convert to int, store in a local list, set the event
- Return the int bpm, or None on timeout / empty / any Exception
- Log at DEBUG: "Heart rate sample: %d bpm" on success
- Log at WARNING on Exception; return None

```
notify_session_start(self) -> None
```
- subprocess.run(["shortcuts", "run", "FormCheck Session Start"],
                 timeout=10, capture_output=True)
- If returncode != 0: log WARNING "Shortcut 'FormCheck Session Start' failed (not set up?)"
- Else: log INFO "Shortcut 'FormCheck Session Start' fired"
- Wrap in try/except; on Exception: log WARNING, do not raise

```
notify_session_stop(self) -> None
```
- Same pattern as notify_session_start but for "FormCheck Session Stop"

### Modified: backend/server.py — heart_rate_loop

Add a new coroutine `heart_rate_loop(websocket, watch: WatchBridge, state: dict)`:

```python
async def heart_rate_loop(websocket, watch: WatchBridge, state: dict) -> None:
    loop = asyncio.get_running_loop()
    while True:
        await asyncio.sleep(30)
        if not state.get("active"):
            continue
        try:
            bpm = await loop.run_in_executor(None, watch.get_heart_rate)
        except Exception as exc:
            log.warning("heart_rate_loop: get_heart_rate raised: %s", exc)
            continue
        if bpm is None:
            continue
        try:
            await websocket.send(json.dumps({"type": "heart_rate", "bpm": bpm}))
            log.debug("heart_rate_loop: sent bpm=%d", bpm)
        except websockets.exceptions.ConnectionClosed:
            return
```

### Modified: backend/server.py — handle_client

Add `from watchbridge import WatchBridge` to imports.

In handle_client(), after `sound = SoundEngine()` add:
```python
watch = WatchBridge()
loop  = asyncio.get_running_loop()
await loop.run_in_executor(None, watch.request_authorization)
```

After `ana_task = asyncio.create_task(analysis_loop(...))` add:
```python
hr_task = asyncio.create_task(heart_rate_loop(websocket, watch, state))
```

In start_session handler, after `slog.info("Session started ...")`:
```python
await loop.run_in_executor(None, watch.notify_session_start)
```

In stop_session handler, after `state["active"] = False`:
```python
await loop.run_in_executor(None, watch.notify_session_stop)
```

In the finally block, after `ana_task.cancel()`:
```python
try:
    hr_task.cancel()
except (UnboundLocalError, Exception):
    pass
```

### Modified: renderer/index.html

In the sidebar section (near the analysis feedback area), add a heart rate display:
```html
<div id="hr-display" style="display:none;">
  <span id="hr-icon">❤️</span>
  <span id="hr-value">--</span> <span>bpm</span>
</div>
```

### Modified: renderer/app.js

Add a case for `"heart_rate"` in the WebSocket message switch:
```javascript
case 'heart_rate': {
  const hrDisplay = document.getElementById('hr-display');
  hrDisplay.style.display = 'inline';
  document.getElementById('hr-value').textContent = msg.bpm;
  break;
}
```

In the start_session click handler (where session-summary is hidden), also reset HR:
```javascript
document.getElementById('hr-display').style.display = 'none';
document.getElementById('hr-value').textContent = '--';
```

### Modified: backend/pyproject.toml

Add to dependencies:
```
"pyobjc-framework-HealthKit>=10.0",
```

---

## Feature 2 — Wake lock (prevent laptop sleep during session)

### Overview

When a session starts, spawn `caffeinate -i -w <backend_pid>` to prevent idle sleep.
When the session stops (or the backend exits), terminate caffeinate.
`caffeinate` is a standard macOS utility; no external dependencies needed.

### New file: backend/wakelock.py

Use `from logger import get_logger` and `log = get_logger(__name__)`.

Class `WakeLock`:

```
__init__(self) -> None
```
- `self._proc: subprocess.Popen | None = None`
- Log "WakeLock initialised"

```
acquire(self) -> None
```
- If self._proc is not None: log DEBUG "Wake lock already held"; return
- `self._proc = subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])`
- Log INFO "Wake lock acquired — caffeinate pid=%d" with self._proc.pid
- Wrap in try/except; on Exception: log WARNING "Wake lock failed: %s"; self._proc = None

```
release(self) -> None
```
- If self._proc is None: return
- Try self._proc.terminate(); self._proc.wait(timeout=3)
- Set self._proc = None
- Log INFO "Wake lock released"
- Wrap in try/except; on Exception: log WARNING "Wake lock release error: %s"; self._proc = None

### Modified: backend/server.py — wakelock integration

Add `from wakelock import WakeLock` to imports.
Add `import os` if not already present.

In handle_client(), after `watch = WatchBridge()` add:
```python
wakelock = WakeLock()
```

In start_session handler, after `await loop.run_in_executor(None, watch.notify_session_start)`:
```python
wakelock.acquire()
```

In stop_session handler, after `await loop.run_in_executor(None, watch.notify_session_stop)`:
```python
wakelock.release()
```

In the finally block, after hr_task.cancel():
```python
wakelock.release()
```

---

## Implementation order

1. backend/watchbridge.py — no project dependencies; needs pyobjc-framework-HealthKit installed
2. backend/wakelock.py    — no project dependencies; uses stdlib only
3. backend/pyproject.toml — add pyobjc-framework-HealthKit dependency
4. backend/server.py      — import + wire WatchBridge, WakeLock, heart_rate_loop
5. renderer/index.html    — add hr-display element
6. renderer/app.js        — add heart_rate message handler; reset HR on session start

---

## Acceptance criteria

1. wakelock.py exists; WakeLock.acquire() spawns a caffeinate process; release() terminates it
2. After start_session, `pgrep caffeinate` returns a PID; after stop_session it does not
3. watchbridge.py exists; module imports without error even when pyobjc not installed
   (import guard catches ImportError; _HK_AVAILABLE = False; all methods return/log gracefully)
4. WatchBridge.notify_session_start() calls `shortcuts run "FormCheck Session Start"`;
   if Shortcut absent the method logs a WARNING and does not raise
5. heart_rate_loop task is created in handle_client and cancelled in finally block
6. When heart rate is available, client receives {"type":"heart_rate","bpm":<int>}
   messages every ~30 s during an active session
7. #hr-display is hidden on load; becomes visible when first heart_rate message arrives
8. #hr-display is hidden and reset to "--" when a new session starts
9. If HealthKit authorization is denied or pyobjc absent, the session runs normally
   with no heart_rate messages — no exception propagates to handle_client
10. Wake lock is released in the finally block even if stop_session is never called
    (e.g. client disconnects mid-session)
11. Python syntax check passes for all new and modified files:
    python -m py_compile backend/watchbridge.py backend/wakelock.py backend/server.py
