#!/usr/bin/env python3
import serial
import time
import json
import os
import subprocess
import base64
import logging
import threading
import uuid
from collections import deque
from logging.handlers import RotatingFileHandler
from datetime import datetime

import RPi.GPIO as GPIO  # reliable on Pi

SERIAL_PORT = "/dev/serial0"
BAUD_RATE = 115200

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Phase 1 Pico diagnostics. Uses the existing UART reader, not a second reader.
# Independent of the setup page's event-logging checkbox.
# Current log plus four backups: about 25 MiB maximum retained.
DIAGNOSTICS_ENABLED = True
DIAGNOSTICS_CONFIG_FILE = os.path.join(BASE_DIR, "diagnostics_config.json")
DIAGNOSTICS_STATUS_FILE = os.path.join(BASE_DIR, "diagnostics_status.json")
_diag_last_sync = None
_diag_last_sent = None
_diag_last_refresh = None

DIAGNOSTIC_LOG_FILE = os.path.join(BASE_DIR, "garage_diagnostics.jsonl")
DIAGNOSTIC_MAX_BYTES = 5 * 1024 * 1024
DIAGNOSTIC_BACKUPS = 4
_diag_handler = None
_diag_last_error = None
_diag_retry_after = 0.0
CAPTURE_DIR = os.path.join(BASE_DIR, "captures")
_capture_file = None
_capture_until_ms = None
_capture_recent = deque(maxlen=60)
_capture_last_position = None
_diag_last_sequence = None
_diag_last_ms = None
_diag_last_idle_ms = None
_serial_partial = b""
_net_worker = None
_net_result = (False, 0.0)



POSITION_FILE = os.path.join(BASE_DIR, "position.json")
VENT_FILE     = os.path.join(BASE_DIR, "vent_status.json")
FAN_FILE      = os.path.join(BASE_DIR, "fan_status.json")
LIGHT_FILE    = os.path.join(BASE_DIR, "light.json")
TEMP_FILE     = os.path.join(BASE_DIR, "temperature.json")
HUMID_FILE    = os.path.join(BASE_DIR, "humidity.json")
LOG_FILE      = os.path.join(BASE_DIR, "status.log")
BUTTON_LOG_FILE = os.path.join(BASE_DIR, "button_log.txt")
VENT_CFG_FILE = os.path.join(BASE_DIR, "vent_config.json")

# Optional status file (handy for UI)
PICO_STATUS_FILE = os.path.join(BASE_DIR, "pico_status.json")

# Outbox file written by app.py on Setup Save
OUTBOX_FILE = os.path.join(BASE_DIR, "pico_outbox.json")
PICO_UPDATE_REQUEST_FILE = os.path.join(BASE_DIR, "pico_update_request.json")
PICO_UPDATE_STATUS_FILE = os.path.join(BASE_DIR, "pico_update_status.json")
PICO_FW_STATUS_FILE = os.path.join(BASE_DIR, "pico_fw_status.json")
PICO_FW_REQUEST_FILE = os.path.join(BASE_DIR, "pico_fw_request.json")
DAILY_RESET_STATE_FILE = os.path.join(BASE_DIR, "pico_daily_reset.json")
_last_outbox_mtime = 0.0
_last_update_request_mtime = 0.0
# Set when the Pico announces a fresh boot. This forces the current saved
# settings to be resent even when pico_outbox.json has not changed.
_force_outbox_send = False

# ---- Pico reset on RUN (BCM 26) ----
PICO_RESET_GPIO = 26  # BCM numbering
RESET_PULSE_S = 0.35

# ---- Pi -> Pico heartbeat ----
HB_PERIOD_S = 1.0
_last_hb_tx = 0.0

# ---- Pi -> Pico network-ok heartbeat ----
NET_PERIOD_S = 2.0
_last_net_tx = 0.0

# ---- Failsafe timing ----
STALE_S = 12
RESET_S = 30
POST_RESET_GRACE_S = 8

_last_rx_time = time.monotonic()
_last_reset_time = 0.0
_reset_count = 0

# ---- Scheduled preventive Pico reset ----
# Uses the Pi's local clock. The five-minute window allows for a service that
# happens to restart at exactly 02:00, while the saved date prevents duplicates.
DAILY_RESET_HOUR = 2
DAILY_RESET_WINDOW_MINUTES = 5


def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}\n"
    print(line.strip())
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line)
    except:
        pass




def refresh_diagnostics_setting(force=False):
    global DIAGNOSTICS_ENABLED, _diag_last_refresh
    now = time.monotonic()
    if not force and _diag_last_refresh is not None and now - _diag_last_refresh < 0.5:
        return
    _diag_last_refresh = now
    cfg = read_json(DIAGNOSTICS_CONFIG_FILE, {})
    enabled = cfg.get("enabled", True) if isinstance(cfg, dict) else True
    if not isinstance(enabled, bool):
        enabled = True
    if not enabled and DIAGNOSTICS_ENABLED:
        _capture_close()
        _capture_recent.clear()
    DIAGNOSTICS_ENABLED = enabled


def reset_diagnostics_sync():
    global _diag_last_sync, _diag_last_sent
    _diag_last_sync = None
    _diag_last_sent = None
    write_json(DIAGNOSTICS_STATUS_FILE, {"enabled": None, "updated_epoch": time.time()})


def sync_diagnostics_setting(ser):
    global _diag_last_sync, _diag_last_sent
    refresh_diagnostics_setting(force=True)
    now = time.monotonic()
    status = read_json(DIAGNOSTICS_STATUS_FILE, {})
    confirmed = status.get("enabled") is DIAGNOSTICS_ENABLED
    interval = 10.0 if confirmed else 2.0
    if (_diag_last_sent is DIAGNOSTICS_ENABLED and _diag_last_sync is not None
            and now - _diag_last_sync < interval):
        return
    try:
        payload = {"diagnostics_enabled": DIAGNOSTICS_ENABLED}
        ser.write((json.dumps(payload) + "\n").encode())
        ser.flush()
        _diag_last_sync = now
        _diag_last_sent = DIAGNOSTICS_ENABLED
    except Exception as exc:
        diagnostic_warning("Pico diagnostics setting send failed: " + str(exc))


def diagnostic_warning(message):
    global _diag_last_error
    now = time.monotonic()
    if _diag_last_error is None or now - _diag_last_error >= 60:
        _diag_last_error = now
        log("Diagnostic logging warning: " + str(message))


class DiagnosticFileHandler(RotatingFileHandler):
    def handleError(self, record):
        # Disk/permission failures must not terminate the UART reader.
        diagnostic_warning("could not write " + DIAGNOSTIC_LOG_FILE)


def _capture_write(payload):
    _capture_file.write(json.dumps(payload) + "\n")
    _capture_file.flush()


def _capture_close():
    global _capture_file, _capture_until_ms
    if _capture_file is not None:
        _capture_file.close()
    _capture_file = None
    _capture_until_ms = None


def _capture_start(payload, label):
    global _capture_file
    _capture_close()
    os.makedirs(CAPTURE_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    path = os.path.join(CAPTURE_DIR, "garage-" + stamp + "-" + label + ".jsonl")
    # Exclusive creation: existing captures are never overwritten or rotated.
    _capture_file = open(path, "x", encoding="utf-8")
    for previous in _capture_recent:
        _capture_write(previous)
    log("Garage capture started: " + os.path.basename(path))


def save_pico_diagnostic(message):
    global _diag_handler, _diag_retry_after, _capture_until_ms
    global _capture_last_position, _diag_last_sequence, _diag_last_ms, _diag_last_idle_ms
    refresh_diagnostics_setting()
    if not DIAGNOSTICS_ENABLED or not isinstance(message, str) or not message.startswith("GARAGE_DIAG "):
        return
    try:
        record = json.loads(message[len("GARAGE_DIAG "):])
        payload = {"received_at": datetime.now().astimezone().isoformat(timespec="milliseconds")}
        if isinstance(record, list) and len(record) == 6:
            payload.update(zip(("pico_ms", "sequence", "session", "requested_action", "kind", "value"), record))
        elif isinstance(record, dict) and "dropped" in record:
            payload.update({"kind": "pico_buffer_overflow", "value": record, "pico_ms": record.get("ms")})
        else:
            raise ValueError("unexpected diagnostic record")
        ms = payload.get("pico_ms")
        seq = payload.get("sequence")
        if seq is not None:
            if _diag_last_sequence is not None and seq > _diag_last_sequence + 1:
                payload["missing_before"] = seq - _diag_last_sequence - 1
            if (_diag_last_sequence is not None and seq <= _diag_last_sequence) or (_diag_last_ms is not None and ms < _diag_last_ms):
                _capture_close()
                _capture_recent.clear()
                _capture_last_position = None
                _diag_last_idle_ms = None
            _diag_last_sequence = seq
            _diag_last_ms = ms
        kind = payload["kind"]
        if _capture_until_ms is not None and ms is not None and ms > _capture_until_ms:
            _capture_close()
        moving = False
        if kind == "accepted_in":
            position = float(payload["value"])
            if _capture_last_position is None:
                _capture_last_position = position
            elif abs(position - _capture_last_position) >= 1.5:
                moving = True
                _capture_last_position = position
        if kind == "command_begin":
            _capture_start(payload, str(payload.get("requested_action") or "command"))
            _capture_until_ms = ms + 45000
        elif moving and _capture_file is None:
            _capture_start(payload, "observed-motion")
            _capture_until_ms = ms + 45000
        if _capture_file is not None:
            if moving or kind in ("pulse_start", "pulse_end", "command_return"):
                _capture_until_ms = ms + 45000
            _capture_write(payload)
        # Preserve a little pre-command history in RAM, using Pico time rather
        # than delayed receive time. Save full movement detail and sparse idle data.
        if ms is not None:
            while _capture_recent and ms - _capture_recent[0].get("pico_ms", ms) > 2000:
                _capture_recent.popleft()
        _capture_recent.append(payload)
        sample = kind in ("raw_cm", "accepted_in")
        if _capture_file is None and sample:
            if kind != "accepted_in":
                return
            if _diag_last_idle_ms is not None and ms - _diag_last_idle_ms < 30000:
                return
            _diag_last_idle_ms = ms
        if _diag_handler is None:
            if time.monotonic() < _diag_retry_after:
                return
            _diag_retry_after = time.monotonic() + 60
            _diag_handler = DiagnosticFileHandler(DIAGNOSTIC_LOG_FILE,
                maxBytes=DIAGNOSTIC_MAX_BYTES, backupCount=DIAGNOSTIC_BACKUPS, encoding="utf-8")
            _diag_handler.setFormatter(logging.Formatter("%(message)s"))
        entry = logging.LogRecord("garage_diagnostics", logging.INFO, __file__, 0, json.dumps(payload), (), None)
        _diag_handler.handle(entry)
    except Exception as exc:
        diagnostic_warning(exc)


def consume_serial_bytes(raw):
    global _serial_partial
    _serial_partial += raw
    if len(_serial_partial) > 65536:
        log("UART partial buffer overflow; discarded incomplete data")
        _serial_partial = b""
        return
    while b"\n" in _serial_partial:
        line, _serial_partial = _serial_partial.split(b"\n", 1)
        handle_line(line.decode(errors="replace").strip())


def event_logging_enabled():
    """
    Master switch for Pico event logging.
    Controlled by the Enable Event Logging checkbox on the setup page.
    """
    try:
        if not os.path.exists(VENT_CFG_FILE):
            return False
        with open(VENT_CFG_FILE, "r") as f:
            cfg = json.load(f)
        return bool(cfg.get("event_logging_enabled", False))
    except:
        return False


def log_event(event):
    if not event_logging_enabled():
        return

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    clean_event = str(event).strip().upper()
    line = f"[{ts}] {clean_event}\n"

    try:
        with open(BUTTON_LOG_FILE, "a") as f:
            f.write(line)
    except:
        pass

    log(f"EVENT: {clean_event}")

def write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def read_json(path, default):
    try:
        if not os.path.exists(path):
            return default
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return default


def note_rx():
    global _last_rx_time
    _last_rx_time = time.monotonic()


def rx_age():
    return max(0.0, time.monotonic() - _last_rx_time)


def gpio_init():
    GPIO.setwarnings(False)
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(PICO_RESET_GPIO, GPIO.OUT, initial=GPIO.LOW)  # RUN idle HIGH


def reset_pico(reason="failsafe_no_telemetry"):
    global _reset_count, _last_reset_time
    _reset_count += 1
    _last_reset_time = time.monotonic()

    log(
        f"PICO RESET: pulsing RUN via GPIO{PICO_RESET_GPIO} "
        f"reason={reason} count={_reset_count}"
    )
    try:
        GPIO.output(PICO_RESET_GPIO, GPIO.HIGH)
        time.sleep(RESET_PULSE_S)
        GPIO.output(PICO_RESET_GPIO, GPIO.LOW)
    except Exception as e:
        log(f"PICO RESET: GPIO error: {e}")


def scheduled_daily_reset_check():
    """Reset the Pico once per local calendar day shortly after 02:00."""
    now = datetime.now()

    if now.hour != DAILY_RESET_HOUR:
        return
    if now.minute >= DAILY_RESET_WINDOW_MINUTES:
        return

    today = now.strftime("%Y-%m-%d")
    state = read_json(DAILY_RESET_STATE_FILE, {})
    if state.get("last_reset_date") == today:
        return

    # Save the date first. If the service is interrupted during the reset,
    # restarting it in the same window will not repeatedly reset the Pico.
    payload = {
        "last_reset_date": today,
        "last_reset_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "scheduled_hour": DAILY_RESET_HOUR,
    }
    try:
        write_json(DAILY_RESET_STATE_FILE, payload)
    except Exception as e:
        log(f"SCHEDULED RESET: state write failed; reset skipped: {e}")
        return

    log("SCHEDULED RESET: beginning daily 02:00 Pico reset")
    reset_pico(reason="scheduled_daily_0200")
    log("SCHEDULED RESET: Pico RUN pulse completed")


def update_pico_status():
    age = rx_age()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    payload = {
        "online": bool(age < STALE_S),
        "last_rx_age_s": round(age, 1),
        "reset_count": _reset_count,
        "updated": now
    }
    try:
        write_json(PICO_STATUS_FILE, payload)
    except:
        pass


def failsafe_check():
    age = rx_age()
    update_pico_status()

    if _last_reset_time and (time.monotonic() - _last_reset_time) < POST_RESET_GRACE_S:
        return

    if age > RESET_S:
        reset_pico()
        note_rx()


def _outbox_mtime():
    try:
        return os.path.getmtime(OUTBOX_FILE)
    except Exception:
        return 0.0


def checksum_file_sum32(path):
    total = 0
    size = 0
    with open(path, "rb") as f:
        while True:
            data = f.read(4096)
            if not data:
                break
            size += len(data)
            total = (total + sum(data)) & 0xFFFFFFFF
    return size, f"{total:08x}"


def write_update_status(data):
    payload = {"updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    payload.update(data)
    try:
        write_json(PICO_UPDATE_STATUS_FILE, payload)
    except Exception as e:
        log(f"Update status write failed: {e}")


def _read_update_line(ser, timeout_s=10):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        raw = ser.readline()
        if not raw:
            continue
            
        # Any Pico response during firmware update proves the Pico is alive.
        note_rx()

        line = raw.decode(errors="ignore").strip() 
        
        if not line:
            continue
        if line.startswith("{") and line.endswith("}"):
            try:
                msg = json.loads(line)
                if isinstance(msg, dict) and ("update" in msg or "fw_version" in msg):
                    return msg
                if isinstance(msg, dict):
                    handle_json(msg)
                    continue
            except Exception:
                pass
        handle_line(line)
    return None


def _wait_update_status(ser, wanted, timeout_s=30):
    wanted_set = set(wanted if isinstance(wanted, (list, tuple, set)) else [wanted])
    deadline = time.monotonic() + timeout_s
    last_msg = None

    while time.monotonic() < deadline:
        msg = _read_update_line(
            ser,
            timeout_s=max(0.2, min(1.0, deadline - time.monotonic()))
        )
        if not msg:
            continue

        # Pico is alive during update
        note_rx()

        if msg.get("update") == "busy":
            last_msg = msg
            continue

        last_msg = msg

        if msg.get("update") == "failed":
            raise RuntimeError(f"Pico update failed: {msg}")

        if msg.get("update") in wanted_set:
            return msg

    raise TimeoutError(f"Timed out waiting for update status {wanted_set}; last={last_msg}")


def request_pico_fw_version(ser):
    try:
        ser.write(b'{"cmd":"fw_version"}\n')
        ser.flush()
        msg = _read_update_line(ser, timeout_s=3)
        if msg and "fw_version" in msg:
            payload = {"fw_version": msg.get("fw_version"), "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
            write_json(PICO_FW_STATUS_FILE, payload)
            log(f"Pico firmware version: {payload['fw_version']}")
            return payload["fw_version"]
    except Exception as e:
        log(f"Firmware version request failed: {e}")
    return None


def send_fw_version_if_requested(ser):
    try:
        if not os.path.exists(PICO_FW_REQUEST_FILE):
            return False
        try:
            os.remove(PICO_FW_REQUEST_FILE)
        except Exception:
            pass
        request_pico_fw_version(ser)
        return True
    except Exception as e:
        log(f"Firmware version request file failed: {e}")
        return False


def install_pico_update(ser, update_path, chunk_size=256):
    update_path = os.path.abspath(update_path)
    if not os.path.exists(update_path):
        raise FileNotFoundError(update_path)

    size, checksum = checksum_file_sum32(update_path)
    log(f"Pico update starting: file={update_path} size={size} checksum={checksum}")
    write_update_status({"state": "starting", "file": update_path, "size": size, "checksum": checksum, "progress": 0})

    start_msg = {"cmd": "update_start", "filename": "main.py", "size": size, "checksum": checksum}
    ser.write((json.dumps(start_msg) + "\n").encode("utf-8"))
    ser.flush()
    _wait_update_status(ser, "ready", timeout_s=30)

    sent = 0
    seq = 0
    with open(update_path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            b64 = base64.b64encode(chunk).decode("ascii")
            msg = {"cmd": "update_chunk", "seq": seq, "data": b64}
            ser.write((json.dumps(msg) + "\n").encode("utf-8"))
            ser.flush()
            ack = _wait_update_status(ser, "chunk_ok", timeout_s=30)
            if int(ack.get("seq", -1)) != seq:
                raise RuntimeError(f"Bad chunk ack: expected {seq}, got {ack}")
            sent += len(chunk)
            seq += 1
            write_update_status({
                "state": "sending",
                "file": update_path,
                "size": size,
                "checksum": checksum,
                "sent": sent,
                "chunks": seq,
                "progress": round((sent / size) * 100, 1) if size else 0,
            })

    ser.write(b'{"cmd":"update_end"}\n')
    ser.flush()
    done = _wait_update_status(ser, "success", timeout_s=15)
    write_update_status({"state": "success", "file": update_path, "size": size, "checksum": checksum, "sent": sent, "chunks": seq, "progress": 100, "pico_response": done})
    log(f"Pico update success: sent={sent} chunks={seq}")

    # Pico soft-resets after a successful install. Give it a moment, then try
    # to refresh the firmware version shown in the web UI.
    time.sleep(2.0)
    for _ in range(6):
        if request_pico_fw_version(ser):
            break
        time.sleep(1.0)


def _update_request_mtime():
    try:
        return os.path.getmtime(PICO_UPDATE_REQUEST_FILE)
    except Exception:
        return 0.0


def send_update_if_requested(ser):
    """
    Process one Pico update request.

    Important safety behavior:
    - Removes the request file before starting, so a service restart does not
      automatically retry the same update over and over.
    - While this function is running, only updater traffic should be sent.
      The main housekeeping loop calls this before heartbeat/net/outbox and
      skips normal traffic if an update was processed.

    Returns True if an update request was processed, even if it failed.
    """
    global _last_update_request_mtime

    mtime = _update_request_mtime()
    if mtime == 0.0 or mtime == _last_update_request_mtime:
        return False

    _last_update_request_mtime = mtime
    req = read_json(PICO_UPDATE_REQUEST_FILE, {})

    # Delete the request immediately so restarting garage.service will not
    # keep re-running the same failed update request.
    try:
        os.remove(PICO_UPDATE_REQUEST_FILE)
    except Exception:
        pass

    if not isinstance(req, dict) or not req.get("file"):
        return False

    try:
        install_pico_update(ser, req.get("file"), int(req.get("chunk_size", 256)))
    except Exception as e:
        log(f"Pico update failed: {e}")
        write_update_status({"state": "failed", "error": str(e), "file": req.get("file", "")})
        try:
            ser.write(b'{"cmd":"update_cancel"}\n')
            ser.flush()
        except Exception:
            pass

    return True

SETTINGS_STATUS_FILE = os.path.join(BASE_DIR, "pico_settings_status.json")
_settings_target = None
_settings_token = None
_settings_last_send = None
_settings_last_confirm = None
_settings_last_poll = None


def reset_settings_sync():
    global _settings_target, _settings_token, _settings_last_send
    global _settings_last_confirm, _settings_last_poll
    _settings_target = None
    _settings_token = None
    _settings_last_send = None
    _settings_last_confirm = None
    _settings_last_poll = None
    write_json(SETTINGS_STATUS_FILE, {"confirmed": False, "reason": "startup_or_pico_reset",
                                     "updated_epoch": time.time()})


def _desired_pico_settings():
    # Saved dashboard config is authoritative; outbox is a fallback for older
    # installations. Never silently substitute generic door defaults.
    cfg = read_json(VENT_CFG_FILE, {})
    outbox = read_json(OUTBOX_FILE, {})
    keys = ("vent_distance", "min_distance", "max_distance", "light_level_on")
    if isinstance(cfg, dict) and all(k in cfg for k in keys):
        return {k: cfg[k] for k in keys}
    if isinstance(outbox, dict) and all(k in outbox for k in keys):
        return {k: outbox[k] for k in keys}
    return None


def handle_settings_ack(msg):
    global _settings_last_confirm
    actual = msg.get("settings_ack")
    if msg.get("settings_token") != _settings_token or _settings_token is None:
        return
    matched = (msg.get("settings_ok") is True and msg.get("settings_ready") is True
               and isinstance(actual, dict) and actual == _settings_target)
    if matched:
        if _settings_last_confirm is None:
            log("Pico settings confirmed: " + json.dumps(actual, sort_keys=True))
        _settings_last_confirm = time.monotonic()
    else:
        _settings_last_confirm = None
    write_json(SETTINGS_STATUS_FILE, {"confirmed": matched, "desired": _settings_target,
               "actual": actual, "error": msg.get("settings_error"), "updated_epoch": time.time()})


def send_outbox_if_needed(ser):
    global _settings_target, _settings_token, _settings_last_send
    global _settings_last_confirm, _settings_last_poll, _force_outbox_send
    target = _desired_pico_settings()
    if target is None:
        return
    now = time.monotonic()
    if target != _settings_target or _force_outbox_send:
        _settings_target = target
        _settings_token = uuid.uuid4().hex
        _settings_last_send = None
        _settings_last_confirm = None
        _settings_last_poll = None
        _force_outbox_send = False
        write_json(SETTINGS_STATUS_FILE, {"confirmed": False, "desired": target,
                                         "updated_epoch": time.time()})
    fresh = _settings_last_confirm is not None and now - _settings_last_confirm < 25
    if not fresh:
        if _settings_last_send is not None and now - _settings_last_send < 2:
            return
        payload = {"settings_apply": target, "settings_token": _settings_token}
    else:
        if _settings_last_poll is not None and now - _settings_last_poll < 10:
            return
        payload = {"cmd": "settings_get", "settings_token": _settings_token}
    try:
        ser.write((json.dumps(payload) + "\n").encode("utf-8"))
        ser.flush()
        if fresh:
            _settings_last_poll = now
        else:
            _settings_last_send = now
    except Exception as exc:
        log("Pico settings sync failed: " + str(exc))


def send_heartbeat(ser):
    """
    Send a simple heartbeat to the Pico once per second.
    Pico expects: {"hb":1}
    """
    global _last_hb_tx
    now = time.monotonic()

    if (now - _last_hb_tx) >= HB_PERIOD_S:
        try:
            ser.write(b'{"hb":1}\n')
            ser.flush()
            _last_hb_tx = now
        except Exception as e:
            log(f"Heartbeat send failed: {e}")


def _ping(host: str, timeout_s: int = 1) -> bool:
    # systemd PATH does not include /bin, so use absolute path
    PING = "/bin/ping"

    if not os.path.exists(PING):
        return False

    try:
        r = subprocess.run(
            [PING, "-c", "1", "-W", str(timeout_s), host],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=3
        )
        return r.returncode == 0
    except Exception:
        return False



def _default_gateway():
    try:
        out = subprocess.check_output(
            ["sh", "-lc", "ip route | awk '/default/ {print $3; exit}'"], timeout=3
        ).decode().strip()
        return out or None
    except Exception:
        return None


def _check_network_background():
    global _net_result
    try:
        gw = _default_gateway()
        good = bool(gw) and _ping(gw, 1) and _ping("1.1.1.1", 1)
        _net_result = (good, time.monotonic())
    except Exception:
        _net_result = (False, time.monotonic())


def send_net_ok(ser):
    # Ping subprocesses must not block the only UART reader.
    global _last_net_tx, _net_worker
    now = time.monotonic()
    if now - _last_net_tx < NET_PERIOD_S:
        return
    if _net_worker is None or not _net_worker.is_alive():
        _net_worker = threading.Thread(target=_check_network_background, daemon=True)
        _net_worker.start()
    good, checked = _net_result
    if good and now - checked <= 15:
        try:
            ser.write(b'{"net":1}\n')
            ser.flush()
        except Exception as exc:
            log(f"Net OK send failed: {exc}")
    _last_net_tx = now


def handle_json(msg):
    global _force_outbox_send

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    note_rx()

    if "settings_ack" in msg:
        handle_settings_ack(msg)
        return

    if "diagnostics_enabled" in msg and isinstance(msg["diagnostics_enabled"], bool):
        write_json(DIAGNOSTICS_STATUS_FILE, {"enabled": msg["diagnostics_enabled"], "updated_epoch": time.time()})
        return

    if "fw_version" in msg:
        write_json(PICO_FW_STATUS_FILE, {"fw_version": msg.get("fw_version"), "updated": now})
        return

    if "update" in msg:
        write_update_status({"state": msg.get("update"), "pico_response": msg})
        return

    if "event" in msg:
        event = str(msg.get("event", "UNKNOWN_EVENT")).strip()
        log_event(event)

        if event.lower() == "pico_boot":
            reset_diagnostics_sync()
            reset_settings_sync()
            _capture_close()
            _capture_recent.clear()
            _force_outbox_send = True
            log("Pico boot detected; saved settings queued for resend")
        return

    if "ack" in msg:
        ack_cmd = str(msg.get("ack", "")).strip().upper()
        ack_status = str(msg.get("status", "")).strip()
        ack_ms = msg.get("ms", "")
        log(f"PICO ACK: {ack_cmd} status={ack_status} ms={ack_ms}")
        return

    if "uart_health" in msg:
        health_type = msg.get("uart_health", "")
        rx_errors = msg.get("rx_errors", 0)
        recoveries = msg.get("recoveries", 0)
        commands_processed = msg.get("commands_processed", 0)
        rx_buffer_len = msg.get("rx_buffer_len", 0)
        health_ms = msg.get("ms", "")

        log(
            "PICO UART HEALTH: "
            f"type={health_type} "
            f"rx_errors={rx_errors} "
            f"recoveries={recoveries} "
            f"commands_processed={commands_processed} "
            f"rx_buffer_len={rx_buffer_len} "
            f"ms={health_ms}"
        )
        return

    if "dbg" in msg:
        save_pico_diagnostic(msg.get("dbg"))
        return

    if "position_percent" in msg:
        try:
            pos_pct = round(float(msg["position_percent"]), 1)
        except:
            pos_pct = msg["position_percent"]

        pos_in = None
        if "position_in" in msg:
            try:
                pos_in = round(float(msg["position_in"]), 2)
            except:
                pos_in = msg["position_in"]

        payload = {"position": pos_pct, "updated": now}
        if pos_in is not None:
            payload["position_in"] = pos_in

        write_json(POSITION_FILE, payload)

    if "vent_status" in msg:
        try:
            write_json(VENT_FILE, {"state": int(msg["vent_status"]), "updated": now})
        except Exception as e:
            log(f"Bad vent_status: {e}")

    if ("light" in msg) or ("light_value" in msg):
        try:
            current = read_json(LIGHT_FILE, {})
            payload = {
                "light": str(msg.get("light", current.get("light", "off"))).lower(),
                "light_value": int(msg.get("light_value", current.get("light_value", 0))),
                "updated": now
            }
            write_json(LIGHT_FILE, payload)
        except Exception as e:
            log(f"Bad light: {e}")

    if "temperature_f" in msg:
        try:
            write_json(TEMP_FILE, {"temp": float(msg["temperature_f"]), "updated": now})
        except Exception as e:
            log(f"Bad temp: {e}")

    if "humidity" in msg:
        try:
            write_json(HUMID_FILE, {"humidity": float(msg["humidity"]), "updated": now})
        except Exception as e:
            log(f"Bad humidity: {e}")


def handle_line(line):
    line = line.strip()
    if not line:
        return

    if line.startswith("{") and line.endswith("}"):
        try:
            msg = json.loads(line)
            if isinstance(msg, dict):
                handle_json(msg)
            return
        except Exception as e:
            log(f"Bad JSON: {e}")
            return


def serial_loop():
    global _serial_partial
    log("Starting serial reader")
    gpio_init()
    note_rx()

    last_housekeeping = time.monotonic()

    while True:
        try:
            ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=0.2, write_timeout=1)
            _serial_partial = b""
            reset_diagnostics_sync()
            reset_settings_sync()
            refresh_diagnostics_setting(force=True)
            log("UART connected")
            note_rx()
            request_pico_fw_version(ser)

            while True:
                raw = ser.read(ser.in_waiting or 1)
                if raw:
                    consume_serial_bytes(raw)

                now = time.monotonic()
                if (now - last_housekeeping) >= 1.0:
                    last_housekeeping = now

                    # IMPORTANT: firmware update owns the UART while active.
                    # Do not mix heartbeat, network heartbeat, or setup outbox
                    # messages with update chunks.
                    if not _serial_partial and send_update_if_requested(ser):
                        continue

                    if not _serial_partial:
                        send_fw_version_if_requested(ser)

                    send_heartbeat(ser)
                    send_net_ok(ser)
                    scheduled_daily_reset_check()
                    failsafe_check()
                    send_outbox_if_needed(ser)
                    sync_diagnostics_setting(ser)


        except serial.SerialException as e:
            log(f"UART error: {e}")
        except Exception as e:
            log(f"Non-UART error: {e}")
            for _ in range(2):
                failsafe_check()
                time.sleep(1)


if __name__ == "__main__":
    try:
        serial_loop()
    finally:
        try:
            GPIO.cleanup()
        except:
            pass
