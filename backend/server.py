import asyncio
import websockets
from typing import Dict, Any
import logging
from .watchbridge import WatchBridge
from .wakelock import WakeLock
from dataclasses import dataclass, field

class FeedbackResult:
    exercise: str
    issues: list = field(default_factory=list)

state: Dict[str, Any] = {
    "active": False,
    "calibrating": False,
}

logger = logging.getLogger(__name__)
watch_bridge = WatchBridge()
wake_lock = WakeLock()

def handle_client(websocket):
    try:
        state["active"] = True
        wake_lock.acquire()
        watch_bridge.notify_session_start()
        asyncio.create_task(heart_rate_loop(websocket))
        async for message in websocket:
            # Handle incoming messages here if needed
            pass
    finally:
        state["active"] = False
        wake_lock.release()
        watch_bridge.notify_session_stop()

async def heart_rate_loop(websocket):
    try:
        while state["active"]:
            bpm = await watch_bridge.get_heart_rate()
            if bpm is not None:
                await websocket.send(f'{{"type": "heart_rate", "bpm": {bpm}}}')
            await asyncio.sleep(30)
    except websockets.exceptions.ConnectionClosedOK:
        logger.info("WebSocket connection closed normally.")
    except Exception as e:
        logger.error(f"Error in heart_rate_loop: {e}")
