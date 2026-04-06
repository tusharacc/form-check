import subprocess
from typing import Optional
import logging
try:
    from HealthKit import HKHealthStore, HKQuantityTypeIdentifierHeartRate
    _HK_AVAILABLE = True
except ImportError:
    _HK_AVAILABLE = False

logger = logging.getLogger(__name__)

class WatchBridge:
    def __init__(self):
        self.health_store = None
        if _HK_AVAILABLE:
            self.health_store = HKHealthStore()

    async def request_authorization(self) -> bool:
        if not _HK_AVAILABLE:
            logger.warning("pyobjc or HealthKit is unavailable.")
            return False
        try:
            types_to_read = [HKQuantityTypeIdentifierHeartRate]
            success, error = await self.health_store.requestAuthorizationToShareTypes_writeTypes_completion(types_to_read, [], None)
            if not success:
                logger.warning(f"HealthKit authorization failed: {error}")
                return False
            return True
        except Exception as e:
            logger.error(f"Error requesting HealthKit authorization: {e}")
            return False

    async def get_heart_rate(self) -> Optional[int]:
        if not _HK_AVAILABLE or not self.health_store:
            logger.warning("pyobjc or HealthKit is unavailable.")
            return None
        try:
            quantity_type = HKQuantityTypeIdentifierHeartRate
            query = self.health_store.queryStatisticsForQuantityType_quantitySamplePredicate_options_completion(quantity_type, None, 0, None)
            result, error = await query.start()
            if error:
                logger.error(f"Error fetching heart rate: {error}")
                return None
            if not result or len(result) == 0:
                logger.warning("No heart rate data available.")
                return None
            return int(result[0].averageQuantity.doubleValueForUnit_(quantity_type))
        except Exception as e:
            logger.error(f"Error fetching heart rate: {e}")
            return None

    def notify_session_start(self):
        try:
            subprocess.run(['shortcuts', 'run', 'FormCheck Session Start'], check=True)
        except FileNotFoundError:
            logger.warning("macOS Shortcuts not found. Please install it to receive session start notifications.")
        except subprocess.CalledProcessError as e:
            logger.warning(f"Failed to run shortcut: {e}")

    def notify_session_stop(self):
        try:
            subprocess.run(['shortcuts', 'run', 'FormCheck Session Stop'], check=True)
        except FileNotFoundError:
            logger.warning("macOS Shortcuts not found. Please install it to receive session stop notifications.")
        except subprocess.CalledProcessError as e:
            logger.warning(f"Failed to run shortcut: {e}")
