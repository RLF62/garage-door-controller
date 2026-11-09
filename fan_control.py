import time
import json
import os
import threading
from gpiozero import OutputDevice

FAN_GPIO = 16
FAN_ON_TEMP_C = 45.0
FAN_OFF_TEMP_C = 40.0
CHECK_INTERVAL = 5
FAN_TEST_SECONDS = 5.0
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FAN_FILE = os.path.join(BASE_DIR, "fan_status.json")
ACTIVE_HIGH = True
fan = OutputDevice(FAN_GPIO, active_high=ACTIVE_HIGH, initial_value=False)

_lock = threading.Lock()
_wake = threading.Event()
_test_until = 0.0
_auto_is_on = False
_ready = False
_last_temp_c = None


def read_pi_temp_c():
    with open("/sys/class/thermal/thermal_zone0/temp", "r") as f:
        return int(f.readline().strip()) / 1000.0


def get_fan_test_status():
    with _lock:
        remaining = max(0.0, _test_until - time.monotonic())
        return {"test_active": remaining > 0,
                "remaining_seconds": round(remaining, 1),
                "fan": "on" if fan.value else "off", "ready": _ready}


def request_fan_test():
    global _test_until
    with _lock:
        now = time.monotonic()
        if not _ready:
            return {"ok": False, "error": "Fan controller is not ready"}, 503
        if _test_until > now:
            return {"ok": False, "error": "Fan test is already running"}, 409
        _test_until = now + FAN_TEST_SECONDS
        _wake.set()
        return {"ok": True, "duration_seconds": FAN_TEST_SECONDS}, 202


def write_status(state, temp_c, test_active=False):
    tmp = FAN_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"fan": state,
                   "temp_c": round(temp_c, 1) if temp_c is not None else None,
                   "test_active": test_active, "updated": time.time()}, f)
    os.replace(tmp, FAN_FILE)


def _control_once():
    """Only this controller writes GPIO; tests override automatic output briefly."""
    global _auto_is_on, _ready, _last_temp_c
    try:
        temp_c = read_pi_temp_c()
    except Exception as exc:
        temp_c = None
        print(f"Fan temperature error: {exc}")
    with _lock:
        _wake.clear()
        if temp_c is not None:
            _last_temp_c = temp_c
            if not _auto_is_on and temp_c >= FAN_ON_TEMP_C:
                _auto_is_on = True
            elif _auto_is_on and temp_c <= FAN_OFF_TEMP_C:
                _auto_is_on = False
        remaining = max(0.0, _test_until - time.monotonic())
        testing = remaining > 0
        desired = testing or _auto_is_on
        if desired:
            fan.on()
        else:
            fan.off()
        _ready = True
        try:
            write_status("on" if desired else "off", _last_temp_c, testing)
        except Exception as exc:
            print(f"Fan status error: {exc}")
        # Wake at test expiry rather than waiting another full polling period.
        return min(CHECK_INTERVAL, remaining) if testing else CHECK_INTERVAL


def fan_loop():
    while True:
        try:
            delay = _control_once()
        except Exception as exc:
            print(f"Fan control error: {exc}")
            delay = 0.25
        _wake.wait(delay)
