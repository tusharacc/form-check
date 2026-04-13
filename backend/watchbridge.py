"""watchbridge.py — Apple Watch integration for FormCheck.

Fires macOS Shortcuts on session start/stop and polls HealthKit heart rate.

User setup (one-time):
  Open the Shortcuts app on Mac and create two Shortcuts:
    • "FormCheck Session Start" — e.g. start a workout on Apple Watch, show notification
    • "FormCheck Session Stop"  — e.g. end the workout, show notification
  These are invoked via `shortcuts run "<name>"`. If they don't exist the call
  returns non-zero and a WARNING is logged; nothing in FormCheck breaks.

All HealthKit calls are guarded: if pyobjc-framework-HealthKit is not installed
or authorization is denied, every method returns None/False and logs a WARNING.
The rest of the session continues normally.
"""
import subprocess
import threading
from typing import Optional

from logger import get_logger

log = get_logger(__name__)

# ── Conditional HealthKit import ──────────────────────────────────────────────
try:
    from Foundation import NSSet, NSSortDescriptor
    from HealthKit import (
        HKHealthStore,
        HKObjectType,
        HKQuantityTypeIdentifierHeartRate,
        HKUnit,
        HKSampleQuery,
    )
    # HKSampleSortIdentifierStartDate is a string constant in pyobjc, not an importable symbol
    HKSampleSortIdentifierStartDate = "startDate"
    _HK_AVAILABLE = True
    _HK_IMPORT_ERR = None
except Exception as _hk_import_exc:
    _HK_AVAILABLE = False
    _HK_IMPORT_ERR = str(_hk_import_exc)


class WatchBridge:
    def __init__(self) -> None:
        self._store = HKHealthStore.alloc().init() if _HK_AVAILABLE else None
        self._authorized = False
        log.info("WatchBridge initialised — healthkit_available=%s", _HK_AVAILABLE)
        if not _HK_AVAILABLE and _HK_IMPORT_ERR:
            log.warning("HealthKit import failed: %s", _HK_IMPORT_ERR)
            log.warning("Install with: pip install pyobjc-framework-HealthKit")

    # ── Authorization ─────────────────────────────────────────────────────────

    def request_authorization(self) -> None:
        """Request HealthKit read permission for heart rate. Blocks up to 10 s."""
        if not _HK_AVAILABLE:
            log.warning("WatchBridge.request_authorization: HealthKit unavailable — skipping")
            return
        try:
            hr_type = HKObjectType.quantityTypeForIdentifier_(HKQuantityTypeIdentifierHeartRate)
            read_types = NSSet.setWithObject_(hr_type)

            done = threading.Event()

            def _callback(success, error):
                if success:
                    self._authorized = True
                    log.info("HealthKit authorization granted")
                else:
                    log.warning("HealthKit authorization denied — no heart rate data (error=%s)", error)
                done.set()

            self._store.requestAuthorizationToShareTypes_readTypes_completion_(
                None, read_types, _callback
            )
            if not done.wait(timeout=10):
                log.warning("HealthKit authorization timed out after 10 s")
        except Exception as exc:
            log.warning("WatchBridge.request_authorization failed: %s", exc)
            self._authorized = False

    # ── Heart rate ────────────────────────────────────────────────────────────

    def get_heart_rate(self) -> Optional[int]:
        """Return the most recent heart rate sample in bpm, or None.

        Does NOT gate on self._authorized — HealthKit authorization persists
        across restarts in the system keychain. Even if request_authorization()
        failed at launch (e.g. missing Info.plist key), a previous grant may
        still be active and the query may succeed. We let HealthKit decide.
        """
        if not _HK_AVAILABLE:
            return None
        try:
            hr_type = HKObjectType.quantityTypeForIdentifier_(HKQuantityTypeIdentifierHeartRate)
            sort_desc = NSSortDescriptor.alloc().initWithKey_ascending_(
                HKSampleSortIdentifierStartDate, False
            )

            result_holder: list = []
            done = threading.Event()

            def _handler(query, samples, error):
                if error:
                    log.warning("HealthKit heart rate query error: %s", error)
                elif samples:
                    bpm_unit = HKUnit.unitFromString_("count/min")
                    bpm = int(samples[0].quantity().doubleValueForUnit_(bpm_unit))
                    result_holder.append(bpm)
                    log.debug("Heart rate sample: %d bpm", bpm)
                done.set()

            query = HKSampleQuery.alloc().initWithSampleType_predicate_limit_sortDescriptors_resultsHandler_(
                hr_type, None, 1, [sort_desc], _handler
            )
            self._store.executeQuery_(query)
            done.wait(timeout=5)
            return result_holder[0] if result_holder else None
        except Exception as exc:
            log.warning("WatchBridge.get_heart_rate failed: %s", exc)
            return None

    # ── Shortcuts ─────────────────────────────────────────────────────────────

    def notify_session_start(self) -> None:
        self._run_shortcut("FormCheck Session Start")

    def notify_session_stop(self) -> None:
        self._run_shortcut("FormCheck Session Stop")

    def _run_shortcut(self, name: str) -> None:
        try:
            result = subprocess.run(
                ["shortcuts", "run", name],
                timeout=10, capture_output=True,
            )
            if result.returncode != 0:
                log.warning("Shortcut '%s' failed (not set up?) — rc=%d", name, result.returncode)
            else:
                log.info("Shortcut '%s' fired", name)
        except Exception as exc:
            log.warning("_run_shortcut('%s') raised: %s", name, exc)
