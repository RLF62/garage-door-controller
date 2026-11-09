# -*- coding: utf-8 -*-

from flask import Flask, render_template, request, redirect, jsonify
import threading
import json
import os
import time
import subprocess
import re
from datetime import datetime
from gpiozero import OutputDevice

from serial_reader import serial_loop
from fan_control import fan_loop, request_fan_test, get_fan_test_status
from wifi_manager import enable_hotspot
import smtplib
from email.message import EmailMessage


app = Flask(__name__)

# -------------------------------------------------
# Paths (absolute, stable under systemd)
# -------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

def p(name: str) -> str:
    return os.path.join(BASE_DIR, name)

POSITION_FILE   = p("position.json")
VENT_FILE       = p("vent_status.json")
LIGHT_FILE      = p("light.json")
TEMP_FILE       = p("temperature.json")
HUMID_FILE      = p("humidity.json")
FAN_FILE        = p("fan_status.json")

VENT_CFG_FILE   = p("vent_config.json")
PICO_OUTBOX_FILE = p("pico_outbox.json")
NOTIFY_CFG_FILE = p("notify_config.json")
NETWORK_STATUS_FILE = p("network_status.json")
PICO_UPDATE_STATUS_FILE = p("pico_update_status.json")
PICO_FW_STATUS_FILE = p("pico_fw_status.json")
PICO_FW_REQUEST_FILE = p("pico_fw_request.json")
PICO_GITHUB_STATUS_FILE = p("pico_github_status.json")
GITHUB_UPDATE_SCRIPT = p("github_pico_update.py")

# -------------------------------------------------
# Default setup values
# -------------------------------------------------
DEFAULT_VENT_CONFIG = {
    "garage_name": "Car Garage",
    "vent_distance": 80,
    "min_distance": 11,
    "max_distance": 108,
    "light_level_on": 30000,
    "auto_close_time": "19:30",
    "auto_close_enabled": False,
    "auto_vent_enabled": False,
    "auto_vent_temp_f": 100,
    "auto_vent_start_time": "08:00",
    "auto_vent_stop_time": "19:30",
    "event_logging_enabled": False,
    "ping_ip": "192.168.50.1",
    "second_door_enabled": False,
    "second_door_name": "Truck Garage",
    "second_door_ip": "",
    "wifi_roam_enabled": False,
    "wifi_preferred_bssid": "",
    "wifi_roam_min_db": 10,
}

APP_LOG         = p("app.log")
AUTO_CLOSE_LOG  = p("auto_close.log")
BUTTON_LOG      = p("button_log.txt")

# -------------------------------------------------
# GPIO -> Pico command pins (BCM numbers on Pi Zero)
# -------------------------------------------------
OPEN_PIN  = 17
CLOSE_PIN = 27
VENT_PIN  = 22
LIGHT_PIN = 4
STOP_PIN  = 5   # wire Pi BCM 5 to the Pico STOP input if you want web STOP
FAN_PIN   = 16  # handled by fan_control.py, but keep here as reference

PULSE_TIME = .4  # seconds

_cmd_lock = threading.Lock()
_cmd = {
    "open":  OutputDevice(OPEN_PIN,  initial_value=True),
    "close": OutputDevice(CLOSE_PIN, initial_value=True),
    "vent":  OutputDevice(VENT_PIN,  initial_value=True),
    "light": OutputDevice(LIGHT_PIN, initial_value=True),
    "stop":  OutputDevice(STOP_PIN,  initial_value=True),
}

def log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}\n"
    print(line.strip())
    try:
        with open(APP_LOG, "a") as f:
            f.write(line)
    except Exception:
        pass


def event_logging_enabled() -> bool:
    """
    Master switch for command/event logging.
    Controlled by the Enable Event Logging checkbox on the setup page.
    """
    try:
        if not os.path.exists(VENT_CFG_FILE):
            return False
        with open(VENT_CFG_FILE, "r") as f:
            cfg = json.load(f)
        return bool(cfg.get("event_logging_enabled", False))
    except Exception:
        return False


def log_button(source: str, action: str):
    """
    Dedicated command/event log used to track why the garage moved.

    Examples:
    [2026-06-23 14:15:22] WEB: OPEN
    [2026-06-23 19:30:00] AUTO_CLOSE: CLOSE position=42
    [2026-06-23 14:20:05] SYSTEM: APP STARTED

    This only writes when Enable Event Logging is checked on the setup page.
    """
    if not event_logging_enabled():
        return

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {source}: {action}\n"

    try:
        with open(BUTTON_LOG, "a") as f:
            f.write(line)
    except Exception:
        pass

    # Also mirror the event into app.log so it is visible in the normal log too.
    log(f"BUTTON {source}: {action}")


def pulse(cmd: str) -> bool:
    dev = _cmd.get(cmd)
    if not dev:
        return False
    with _cmd_lock:
        dev.off()
        time.sleep(PULSE_TIME)
        dev.on()
    return True

# -------------------------------------------------
# JSON helpers
# -------------------------------------------------
def read_json(path, default):
    try:
        if not os.path.exists(path):
            return default
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return default


def read_key_value_config(path):
    try:
        if not os.path.exists(path):
            return {}
        data = {}
        with open(path, "r") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    k, v = line.split("=", 1)
                    data[k.strip()] = v.strip()
        return data
    except Exception:
        return {}


def write_key_value_config(path, data):
    # Save as KEY=VALUE lines to match your existing notify_config.json file.
    lines = []
    for key in [
        "GARAGE_NAME",
        "SENDER_EMAIL",
        "SENDER_APP_PASSWORD",
        "TEXT_TO",
        "SMTP_SERVER",
        "SMTP_PORT",
    ]:
        value = str(data.get(key, "")).strip()
        lines.append(f"{key}={value}")

    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(tmp, path)

def safe_write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def normalize_hhmm(value, default_value):
    """Return a browser-safe HH:MM string for input type=time."""
    try:
        value = str(value or "").strip()
        parts = value.split(":")
        if len(parts) < 2:
            return default_value
        hour = int(parts[0])
        minute = int(parts[1])
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return default_value
        return f"{hour:02d}:{minute:02d}"
    except Exception:
        return default_value


def read_vent_config():
    """
    Read vent_config.json and merge in any missing defaults.

    This fixes older config files that do not yet have Auto-Vent start/stop
    keys, and it makes sure setup.html always receives HH:MM values that
    browser time inputs can display.
    """
    cfg = DEFAULT_VENT_CONFIG.copy()
    saved_cfg = read_json(VENT_CFG_FILE, {})
    if isinstance(saved_cfg, dict):
        cfg.update(saved_cfg)

    cfg["auto_close_time"] = normalize_hhmm(
        cfg.get("auto_close_time"),
        DEFAULT_VENT_CONFIG["auto_close_time"]
    )
    cfg["auto_vent_start_time"] = normalize_hhmm(
        cfg.get("auto_vent_start_time"),
        DEFAULT_VENT_CONFIG["auto_vent_start_time"]
    )
    cfg["auto_vent_stop_time"] = normalize_hhmm(
        cfg.get("auto_vent_stop_time"),
        cfg.get("auto_close_time", DEFAULT_VENT_CONFIG["auto_vent_stop_time"])
    )

    # If vent_config.json was created before these settings existed, write the
    # merged/defaulted values back once so the file actually contains them.
    try:
        saved_cfg = read_json(VENT_CFG_FILE, {})
        if not isinstance(saved_cfg, dict):
            saved_cfg = {}
        missing_or_changed = False
        for key, value in cfg.items():
            if key not in saved_cfg:
                saved_cfg[key] = value
                missing_or_changed = True
        for key in ("auto_close_time", "auto_vent_start_time", "auto_vent_stop_time"):
            if saved_cfg.get(key) != cfg.get(key):
                saved_cfg[key] = cfg.get(key)
                missing_or_changed = True
        if missing_or_changed:
            safe_write_json(VENT_CFG_FILE, saved_cfg)
    except Exception as e:
        log(f"Could not backfill vent_config defaults: {e}")

    return cfg

# -------------------------------------------------
# Wi-Fi helpers
# -------------------------------------------------
def wifi_connected() -> bool:
    try:
        subprocess.check_output(["iwgetid"], stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False

def get_wifi_ssid():
    try:
        ssid = subprocess.check_output(
            ["iwgetid", "-r"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        return ssid if ssid else "Not connected"
    except Exception:
        return "Unknown"

def get_wifi_strength():
    try:
        out = subprocess.check_output(
            ["/sbin/iw", "dev", "wlan0", "link"],
            stderr=subprocess.DEVNULL,
            text=True,
        )

        rssi = None
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("signal:"):
                parts = line.replace("dBm", "").split()
                rssi = float(parts[-1])

        if rssi is None:
            return {"percent": 0, "rssi": 0, "ssid": get_wifi_ssid()}

        percent = int(min(max(2 * (rssi + 100), 0), 100))
        return {"percent": percent, "rssi": rssi, "ssid": get_wifi_ssid()}
    except Exception:
        return {"percent": 0, "rssi": 0, "ssid": get_wifi_ssid()}

def wifi_roam_status():
    """Read-only connection status. Does not rescan or disrupt Wi-Fi."""
    result = {"ssid": get_wifi_ssid(), "bssid": "", "rssi": None,
              "preferred_seen": None, "last_check": "Not run", "last_result": "Not run"}
    try:
        output = subprocess.check_output(["iw", "dev", "wlan0", "link"],
                                         text=True, stderr=subprocess.DEVNULL, timeout=5)
        match = re.search(r"Connected to ([0-9a-f:]{17})", output, re.I)
        if match:
            result["bssid"] = match.group(1).upper()
        match = re.search(r"signal:\s*(-?[0-9.]+)\s*dBm", output)
        if match:
            result["rssi"] = float(match.group(1))
    except Exception:
        pass
    state = read_json(p("wifi_roam_status.json"), {})
    if isinstance(state, dict):
        for key in ("preferred_seen", "last_check", "last_result"):
            if key in state:
                result[key] = state[key]
    return result


@app.route("/api/wifi_roam_status")
def wifi_roam_status_api():
    return jsonify(wifi_roam_status())


def get_cpu_temp_f():
    try:
        with open("/sys/class/thermal/thermal_zone0/temp", "r") as f:
            temp_c = int(f.read().strip()) / 1000.0
        return round((temp_c * 9 / 5) + 32, 1)
    except Exception:
        return None


def get_ping_ip():
    cfg = read_vent_config()
    return str(cfg.get("ping_ip", DEFAULT_VENT_CONFIG["ping_ip"])).strip() or DEFAULT_VENT_CONFIG["ping_ip"]


def ping_target(ip_address=None):
    ip = str(ip_address or get_ping_ip()).strip() or DEFAULT_VENT_CONFIG["ping_ip"]
    started = time.time()
    try:
        result = subprocess.run(
            ["ping", "-c", "1", "-W", "2", ip],
            capture_output=True,
            text=True,
            timeout=4,
        )
        latency_ms = None
        output = (result.stdout or "") + "\n" + (result.stderr or "")
        for part in output.replace("/", " ").split():
            if part.startswith("time="):
                try:
                    latency_ms = round(float(part.split("=", 1)[1]), 1)
                except Exception:
                    pass
        return {
            "ok": result.returncode == 0,
            "ping_ip": ip,
            "latency_ms": latency_ms,
            "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "elapsed_ms": round((time.time() - started) * 1000, 1),
        }
    except Exception as e:
        return {
            "ok": False,
            "ping_ip": ip,
            "latency_ms": None,
            "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "error": str(e),
        }


def get_local_ip():
    try:
        out = subprocess.check_output(
            "hostname -I | awk '{print $1}'",
            shell=True,
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        return out or "Unknown"
    except Exception:
        return "Unknown"


def get_garage_name():
    """
    Main display name for the controller.

    Primary source: vent_config.json, which is edited on Door Setup.
    Fallback: notify_config.json GARAGE_NAME, so older installs keep working.
    """
    vent_cfg = read_vent_config()
    name = str(vent_cfg.get("garage_name", "") or "").strip()
    if name:
        return name

    notify_cfg = read_key_value_config(NOTIFY_CFG_FILE)
    name = str(notify_cfg.get("GARAGE_NAME", "") or "").strip()
    return name or DEFAULT_VENT_CONFIG["garage_name"]


def sync_notify_garage_name(garage_name):
    """Keep SMS notification name matched to the main garage name without changing other notification settings."""
    try:
        name = str(garage_name or "").strip()
        if not name:
            return
        cfg = read_key_value_config(NOTIFY_CFG_FILE)
        if not cfg:
            return
        cfg["GARAGE_NAME"] = name
        write_key_value_config(NOTIFY_CFG_FILE, cfg)
    except Exception as e:
        log(f"Could not sync notification garage name: {e}")


def build_second_door_url(second_door_ip: str) -> str:
    """
    Convert setup value into a usable link for the other garage controller.

    Accepted examples:
      192.168.50.156
      192.168.50.156:5000
      http://192.168.50.156:5000/
    """
    value = str(second_door_ip or "").strip()
    if not value:
        return ""
    if value.startswith("http://") or value.startswith("https://"):
        return value.rstrip("/") + "/"
    if ":" in value:
        return "http://" + value.rstrip("/") + "/"
    return "http://" + value.rstrip("/") + ":5000/"


def automation_status():
    cfg = read_vent_config()
    temp_f = read_json(TEMP_FILE, {}).get("temp", "")
    position_pct = read_json(POSITION_FILE, {}).get("position", "")
    return {
        "auto_close_enabled": bool(cfg.get("auto_close_enabled", False)),
        "auto_close_time": normalize_hhmm(cfg.get("auto_close_time"), DEFAULT_VENT_CONFIG["auto_close_time"]),
        "auto_vent_enabled": bool(cfg.get("auto_vent_enabled", False)),
        "auto_vent_temp_f": int(cfg.get("auto_vent_temp_f", DEFAULT_VENT_CONFIG["auto_vent_temp_f"])),
        "auto_vent_start_time": normalize_hhmm(cfg.get("auto_vent_start_time"), DEFAULT_VENT_CONFIG["auto_vent_start_time"]),
        "auto_vent_stop_time": normalize_hhmm(cfg.get("auto_vent_stop_time"), cfg.get("auto_close_time", DEFAULT_VENT_CONFIG["auto_vent_stop_time"])),
        "auto_vent_time_allowed": auto_vent_time_allowed(cfg),
        "current_temp_f": temp_f,
        "position": position_pct,
        "next_action": build_next_action_text(cfg),
    }


def build_next_action_text(cfg):
    close_enabled = bool(cfg.get("auto_close_enabled", False))
    vent_enabled = bool(cfg.get("auto_vent_enabled", False))
    close_time = normalize_hhmm(cfg.get("auto_close_time"), DEFAULT_VENT_CONFIG["auto_close_time"])
    vent_start = normalize_hhmm(cfg.get("auto_vent_start_time"), DEFAULT_VENT_CONFIG["auto_vent_start_time"])
    vent_stop = normalize_hhmm(cfg.get("auto_vent_stop_time"), close_time)
    if close_enabled and vent_enabled:
        return f"Auto-Vent allowed {vent_start} to {vent_stop}; Auto-Close at {close_time}"
    if close_enabled:
        return f"Auto-Close at {close_time}"
    if vent_enabled:
        return f"Auto-Vent allowed {vent_start} to {vent_stop}"
    return "No automations enabled"

# -------------------------------------------------
# Auto-close if open
# -------------------------------------------------
_last_autoclose_key = None
_last_auto_vent = 0
AUTO_VENT_COOLDOWN_SEC = 3600  # 30 minutes
AUTO_VENT_DEADBAND_PCT = 5.0   # do not vent again if already near/beyond vent target

def is_door_open(position_pct) -> bool:
    try:
        return float(position_pct) < 98.0
    except Exception:
        return False


def vent_distance_to_percent(cfg) -> float:
    """
    Convert the configured vent distance into the same 0-100 position scale
    used by position.json. On this controller: 0 = fully open, 100 = closed.
    """
    try:
        vent_dist = float(cfg.get("vent_distance", 75))
        min_dist = float(cfg.get("min_distance", 11))
        max_dist = float(cfg.get("max_distance", 108))

        if max_dist <= min_dist:
            return 10.0

        pct = ((vent_dist - min_dist) / (max_dist - min_dist)) * 100.0
        return max(0.0, min(100.0, pct))
    except Exception:
        return 10.0


def parse_hhmm_to_minutes(value, default=None):
    try:
        value = str(value or "").strip()
        if len(value) != 5 or value[2] != ":":
            return default
        hour = int(value[0:2])
        minute = int(value[3:5])
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return default
        return (hour * 60) + minute
    except Exception:
        return default


def valid_hhmm(value) -> bool:
    return parse_hhmm_to_minutes(value, None) is not None


def auto_vent_time_allowed(cfg) -> bool:
    """
    Auto-vent is allowed only inside the configured daily time window.

    Default behavior:
      - start = auto_vent_start_time, default 08:00
      - stop  = auto_vent_stop_time, default auto_close_time

    Example:
      start 08:00 and stop 19:30 means auto-vent can run from
      8:00 AM through 7:29 PM, but not at/after 7:30 PM.
    """
    try:
        start_value = cfg.get("auto_vent_start_time", "08:00")
        stop_value = cfg.get("auto_vent_stop_time", cfg.get("auto_close_time", "19:30"))

        start_minutes = parse_hhmm_to_minutes(start_value, 8 * 60)
        stop_minutes = parse_hhmm_to_minutes(
            stop_value,
            parse_hhmm_to_minutes(cfg.get("auto_close_time", "19:30"), 19 * 60 + 30)
        )

        now = datetime.now()
        now_minutes = (now.hour * 60) + now.minute

        if start_minutes == stop_minutes:
            # Same start/stop means do not auto-vent.
            return False

        if start_minutes < stop_minutes:
            return start_minutes <= now_minutes < stop_minutes

        # Supports an overnight window if ever needed, such as 22:00 to 06:00.
        return now_minutes >= start_minutes or now_minutes < stop_minutes

    except Exception:
        # If the time window cannot be read, block auto-vent instead of
        # accidentally venting after the door was meant to stay closed.
        return False
        
        
def send_text_message(message):
    cfg = read_key_value_config(NOTIFY_CFG_FILE)

    garage_name = cfg.get("GARAGE_NAME", "Garage Controller")
    sender_email = cfg.get("SENDER_EMAIL", "")
    sender_password = cfg.get("SENDER_APP_PASSWORD", "")
    smtp_server = cfg.get("SMTP_SERVER", "smtp.gmail.com")
    smtp_port = int(cfg.get("SMTP_PORT", "465"))

    recipients_raw = cfg.get("TEXT_TO", "")
    recipients = [
        r.strip()
        for r in recipients_raw.split(",")
        if r.strip()
    ]

    if not sender_email:
        log("Text message skipped: SENDER_EMAIL missing")
        return False

    if not sender_password:
        log("Text message skipped: SENDER_APP_PASSWORD missing")
        return False

    if not recipients:
        log("Text message skipped: TEXT_TO missing")
        return False

    sent_count = 0

    try:
        with smtplib.SMTP_SSL(smtp_server, smtp_port) as smtp:
            smtp.login(sender_email, sender_password)

            for r in recipients:
                try:
                    msg = EmailMessage()
                    msg.set_content(message)

                    # Optional:
                    # Some carriers display this, some ignore it
                    msg["Subject"] = "Garage Alert"

                    msg["From"] = f"{garage_name} <{sender_email}>"
                    msg["To"] = r

                    smtp.send_message(msg)

                    sent_count += 1
                    log(f"Text message sent to {r}")

                except Exception as e:
                    log(f"Failed sending text to {r}: {e}")

        log(f"Text notification complete ({sent_count}/{len(recipients)} recipients)")
        return sent_count > 0

    except Exception as e:
        log(f"SMTP connection failed: {e}")
        return False
        
        
def auto_close_loop():
    global _last_autoclose_key

    while True:
        try:
            cfg = read_vent_config()
            if not cfg.get("auto_close_enabled", False):
                time.sleep(10)
                continue

            target = str(cfg.get("auto_close_time", "")).strip()
            if not target or ":" not in target:
                time.sleep(10)
                continue

            now = datetime.now()
            now_hhmm = now.strftime("%H:%M")

            if now_hhmm != target:
                _last_autoclose_key = None
                time.sleep(10)
                continue

            minute_key = now.strftime("%Y-%m-%d %H:%M")
            if _last_autoclose_key == minute_key:
                time.sleep(10)
                continue

            pos = read_json(POSITION_FILE, {})
            position_pct = pos.get("position", None)

            if position_pct is not None and is_door_open(position_pct):
                log_button("AUTO_CLOSE", f"CLOSE position={position_pct}")
                pulse("close")

                notify_cfg = read_key_value_config(NOTIFY_CFG_FILE)
                garage_name = notify_cfg.get("GARAGE_NAME", "Garage Controller")

                send_text_message(
                    f"{garage_name}: Garage door auto-closed at {datetime.now().strftime('%I:%M %p')}."
                )

                try:
                    with open(AUTO_CLOSE_LOG, "a") as f:
                        f.write(f"[{now.isoformat()}] Auto-close triggered (position={position_pct})\n")
                except Exception:
                    pass

                _last_autoclose_key = minute_key
                log(f"Auto-close triggered (position={position_pct})")

        except Exception as e:
            try:
                with open(AUTO_CLOSE_LOG, "a") as f:
                    f.write(f"[{datetime.now().isoformat()}] Auto-close ERROR: {e}\n")
            except Exception:
                pass

        time.sleep(10)


# -------------------------------------------------
# Auto-vent on high BME280 temperature
# -------------------------------------------------
def auto_vent_loop():
    global _last_auto_vent

    while True:
        try:
            cfg = read_vent_config()

            if not cfg.get("auto_vent_enabled", False):
                time.sleep(30)
                continue

            trigger_temp = float(cfg.get("auto_vent_temp_f", 100))
            temp_f = float(read_json(TEMP_FILE, {}).get("temp", 0))
            position_pct = float(read_json(POSITION_FILE, {}).get("position", 100))

            vent_pct = vent_distance_to_percent(cfg)
            now = time.time()

            # Auto-vent is only allowed inside the configured time window.
            # This keeps the garage from venting overnight or after auto-close.
            time_ok = auto_vent_time_allowed(cfg)

            # Only send VENT if the door is more closed than the configured
            # vent position. This prevents repeat vent commands when already vented.
            # needs_vent = position_pct > (vent_pct + AUTO_VENT_DEADBAND_PCT)
            needs_vent = position_pct > 95
            cooldown_ok = (now - _last_auto_vent) >= AUTO_VENT_COOLDOWN_SEC

            if temp_f >= trigger_temp and time_ok and needs_vent and cooldown_ok:
                log_button(
                    "AUTO_VENT",
                    f"VENT temp={temp_f}F trigger={trigger_temp}F position={position_pct} vent_target={round(vent_pct, 1)}"
                )
                pulse("vent")
                _last_auto_vent = now
                log(
                    f"Auto-vent triggered: temp={temp_f}F trigger={trigger_temp}F "
                    f"position={position_pct}% vent_target={round(vent_pct, 1)}%"
                )

        except Exception as e:
            log(f"Auto-vent ERROR: {e}")

        time.sleep(30)


# -------------------------------------------------
# Pico firmware / GitHub OTA helpers
# -------------------------------------------------
def version_key(value):
    """Simple version compare helper. Handles values like 1.0.2-uart-buffer-fix."""
    try:
        value = str(value or "").strip().lower().lstrip("v")
        parts = []
        buf = ""
        for ch in value:
            if ch.isdigit():
                buf += ch
            else:
                if buf:
                    parts.append(int(buf))
                    buf = ""
                if ch not in ".-_ ":
                    break
        if buf:
            parts.append(int(buf))
        while len(parts) < 3:
            parts.append(0)
        return tuple(parts[:3])
    except Exception:
        return (0, 0, 0)


def read_pico_fw_status():
    return read_json(PICO_FW_STATUS_FILE, {})


def read_github_fw_status():
    return read_json(PICO_GITHUB_STATUS_FILE, {})


def build_ota_status():
    fw = read_pico_fw_status()
    gh = read_github_fw_status()
    upd = read_json(PICO_UPDATE_STATUS_FILE, {})

    current = str(fw.get("fw_version", "") or "").strip()
    latest = str(gh.get("version", "") or "").strip()

    update_available = False
    version_relation = "unknown"
    comparison_message = "Run Check GitHub to compare versions."
    can_install = False

    if current and latest:
        cur_key = version_key(current)
        latest_key = version_key(latest)
        if latest_key > cur_key:
            update_available = True
            version_relation = "newer"
            comparison_message = "A newer GitHub firmware is available."
            can_install = True
        elif latest_key == cur_key:
            version_relation = "same"
            comparison_message = "Installed firmware matches GitHub."
        else:
            version_relation = "older"
            comparison_message = "GitHub firmware is older than the installed Pico firmware."
    elif latest and not current:
        version_relation = "no_current"
        comparison_message = "GitHub firmware is available, but the installed Pico version is unknown."
        can_install = True
    elif current and not latest:
        version_relation = "no_latest"
        comparison_message = "Pico version is known. Run Check GitHub to get the latest version."

    return {
        "current_version": current or "Unknown",
        "current_updated": fw.get("updated", ""),
        "latest_version": latest or "Unknown",
        "latest_notes": gh.get("notes", ""),
        "latest_file": gh.get("file", "main.py"),
        "github_checked": gh.get("checked_at", gh.get("updated", "")),
        "update_available": update_available,
        "version_relation": version_relation,
        "comparison_message": comparison_message,
        "can_install": can_install,
        "update_state": upd.get("state", "idle"),
        "progress": upd.get("progress", 0),
        "sent": upd.get("sent", 0),
        "size": upd.get("size", 0),
        "chunks": upd.get("chunks", 0),
        "checksum": upd.get("checksum", ""),
        "update_updated": upd.get("updated", ""),
        "error": upd.get("error", ""),
        "pico_response": upd.get("pico_response", {}),
    }


def run_github_check():
    if not os.path.exists(GITHUB_UPDATE_SCRIPT):
        raise FileNotFoundError(GITHUB_UPDATE_SCRIPT)

    result = subprocess.run(
        ["python3", GITHUB_UPDATE_SCRIPT, "check"],
        cwd=BASE_DIR,
        capture_output=True,
        text=True,
        timeout=35,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "GitHub check failed").strip())

    version_path = p("pico_updates/version.json")
    data = read_json(version_path, {})
    data["checked_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    data["stdout"] = (result.stdout or "").strip()[-1000:]
    safe_write_json(PICO_GITHUB_STATUS_FILE, data)
    return build_ota_status()

# -------------------------------------------------
# Main UI
# -------------------------------------------------
@app.route("/")
def index():
    cfg = read_vent_config()
    second_door_enabled = bool(cfg.get("second_door_enabled", False))
    second_door_name = str(cfg.get("second_door_name", "Truck Garage") or "Truck Garage").strip()
    second_door_ip = str(cfg.get("second_door_ip", "") or "").strip()
    second_door_url = build_second_door_url(second_door_ip) if second_door_enabled else ""

    return render_template(
        "index.html",
        garage_name=get_garage_name(),
        second_door_enabled=second_door_enabled,
        second_door_name=second_door_name,
        second_door_ip=second_door_ip,
        second_door_url=second_door_url,
    )

# -------------------------------------------------
# Endpoints expected by your index.html
# -------------------------------------------------
@app.route("/position")
def position():
    return str(read_json(POSITION_FILE, {}).get("position", ""))

@app.route("/position_in")
def position_in():
    return str(read_json(POSITION_FILE, {}).get("position_in", ""))

@app.route("/vent_status")
def vent_status():
    return str(read_json(VENT_FILE, {}).get("state", 0))

@app.route("/environment_temp")
def environment_temp():
    return str(read_json(TEMP_FILE, {}).get("temp", ""))

@app.route("/environment_humidity")
def environment_humidity():
    return str(read_json(HUMID_FILE, {}).get("humidity", ""))

@app.route("/fan_test", methods=["POST"])
def fan_test():
    result, status = request_fan_test()
    return jsonify(result), status

@app.route("/fan_test_status")
def fan_test_status():
    return jsonify(get_fan_test_status())

@app.route("/fan_status")
def fan_status():
    try:
        with open(FAN_FILE) as f:
            data = json.load(f)
        return data.get("fan", "unknown")
    except Exception:
        return "unknown"

@app.route("/wifi_strength")
def wifi_strength():
    return jsonify(get_wifi_strength())

@app.route("/network_status")
def network_status():
    cfg = read_vent_config()
    status = read_json(NETWORK_STATUS_FILE, {})
    return jsonify({
        "ssid": get_wifi_ssid(),
        "local_ip": get_local_ip(),
        "ping_ip": str(cfg.get("ping_ip", DEFAULT_VENT_CONFIG["ping_ip"])),
        "watchdog_online": bool(status.get("online", False)),
        "last_success": status.get("last_success", ""),
        "last_check": status.get("last_check", ""),
        "fail_count": int(status.get("fail_count", 0) or 0),
        "latency_ms": status.get("latency_ms", None),
    })

@app.route("/test_ping")
def test_ping():
    return jsonify(ping_target())

@app.route("/automation_status")
def automation_status_endpoint():
    return jsonify(automation_status())


@app.route("/auto_vent_enabled")
def auto_vent_enabled_endpoint():
    """Return 1 when Auto Vent is enabled, otherwise 0."""
    cfg = read_vent_config()
    return str(int(bool(cfg.get("auto_vent_enabled", False))))


@app.route("/set_auto_vent", methods=["POST"])
def set_auto_vent():
    """Enable or disable Auto Vent without changing its other settings."""
    data = request.get_json(silent=True) or {}
    enabled = data.get("enabled", None)

    if not isinstance(enabled, bool):
        return jsonify({
            "success": False,
            "error": "JSON field 'enabled' must be true or false"
        }), 400

    try:
        cfg = read_vent_config()
        cfg["auto_vent_enabled"] = enabled
        cfg["updated"] = time.time()
        safe_write_json(VENT_CFG_FILE, cfg)

        log_button("HUB", "AUTO_VENT_ON" if enabled else "AUTO_VENT_OFF")
        log(f"Auto Vent {'enabled' if enabled else 'disabled'} from Dual Controller")

        return jsonify({
            "success": True,
            "auto_vent_enabled": enabled
        })
    except Exception as e:
        log(f"Could not update Auto Vent from Dual Controller: {e}")
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500

@app.route("/cpu_temp")
def cpu_temp():
    temp = get_cpu_temp_f()
    if temp is None:
        return ""
    return str(temp)


@app.route("/system_status")
def system_status():
    try:
        data = read_json(p("pico_status.json"), {})
        age = int(float(data.get("last_rx_age_s", 999)))

        # Trust serial_reader.py's real Pico status.
        # This prevents the UI from showing ONLINE just because /dev/serial0 exists.
        return jsonify({
            "pico_online": bool(data.get("online", False)),
            "last_update_sec": age
        })

    except Exception:
        return jsonify({
            "pico_online": False,
            "last_update_sec": 999
        })

@app.route("/vent_distance")
def vent_distance():
    cfg = read_json(VENT_CFG_FILE, {})
    v = cfg.get("vent_distance", 75)
    try:
        return str(int(v))
    except Exception:
        return str(v)

@app.route("/light_level")
def get_light_level():
    light_level = read_json(LIGHT_FILE, {"light_value": 0})
    return str(light_level.get("light_value", 0))

@app.route("/light_status")
def light_status():
    try:
        with open(LIGHT_FILE, "r") as f:
            data = json.load(f)
        return str(data.get("light", "off")).lower()
    except Exception:
        return "off"



# -------------------------------------------------
# Pico firmware / GitHub OTA endpoints
# -------------------------------------------------
DIAGNOSTICS_CONFIG_FILE = p("diagnostics_config.json")
DIAGNOSTICS_STATUS_FILE = p("diagnostics_status.json")
_diagnostics_lock = threading.Lock()


@app.route("/diagnostics_settings", methods=["GET", "POST"])
def diagnostics_settings():
    with _diagnostics_lock:
        if request.method == "POST":
            body = request.get_json(silent=True)
            if not isinstance(body, dict) or not isinstance(body.get("enabled"), bool):
                return jsonify({"ok": False, "error": "enabled must be true or false"}), 400
            try:
                safe_write_json(DIAGNOSTICS_CONFIG_FILE, {"enabled": body["enabled"], "updated_epoch": time.time()})
            except Exception as exc:
                return jsonify({"ok": False, "error": "Could not save diagnostics setting"}), 500
        cfg = read_json(DIAGNOSTICS_CONFIG_FILE, {})
        desired = cfg.get("enabled", True) if isinstance(cfg, dict) else True
        if not isinstance(desired, bool):
            desired = True
        status = read_json(DIAGNOSTICS_STATUS_FILE, {})
        applied = status.get("enabled")
        ack_time = status.get("updated_epoch", 0)
        saved_time = cfg.get("updated_epoch", 0) if isinstance(cfg, dict) else 0
        confirmed = (isinstance(applied, bool) and applied == desired
                     and isinstance(ack_time, (int, float)) and isinstance(saved_time, (int, float))
                     and ack_time >= saved_time and 0 <= time.time() - ack_time < 30)
        return jsonify({"ok": True, "enabled": desired, "pico_enabled": applied,
                        "confirmed": confirmed})


@app.route("/pico_update")
def pico_update_page():
    return render_template(
        "pico_update.html",
        garage_name=get_garage_name()
    )


@app.route("/pico_update_status")
def pico_update_status_endpoint():
    return jsonify(build_ota_status())


@app.route("/pico_fw_refresh", methods=["POST"])
def pico_fw_refresh():
    try:
        safe_write_json(PICO_FW_REQUEST_FILE, {"request": "fw_version", "requested_at": time.time()})
        return jsonify({"ok": True, "status": build_ota_status()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/github_update_check", methods=["POST"])
def github_update_check():
    try:
        status = run_github_check()
        return jsonify({"ok": True, "status": status})
    except Exception as e:
        log(f"GitHub update check failed: {e}")
        return jsonify({"ok": False, "error": str(e), "status": build_ota_status()}), 500


@app.route("/github_update_install", methods=["POST"])
def github_update_install():
    try:
        current_status = build_ota_status()
        if current_status.get("update_state") in ("queued", "starting", "sending"):
            return jsonify({"ok": False, "error": "Update already running", "status": current_status}), 409

        # Do a fresh GitHub check right before install so the decision is current.
        try:
            current_status = run_github_check()
        except Exception as e:
            log(f"Install pre-check failed; continuing with last known GitHub status: {e}")
            current_status = build_ota_status()

        if not current_status.get("can_install", False):
            return jsonify({
                "ok": False,
                "error": current_status.get("comparison_message", "No newer GitHub firmware available."),
                "status": current_status
            }), 409

        if not os.path.exists(GITHUB_UPDATE_SCRIPT):
            raise FileNotFoundError(GITHUB_UPDATE_SCRIPT)

        # Clear stale terminal states so the UI immediately shows the new run.
        safe_write_json(PICO_UPDATE_STATUS_FILE, {
            "state": "queued",
            "progress": 0,
            "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })

        subprocess.Popen(
            ["python3", GITHUB_UPDATE_SCRIPT, "install"],
            cwd=BASE_DIR,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        log("GitHub Pico update install started from web UI")
        return jsonify({"ok": True, "status": build_ota_status()})
    except Exception as e:
        log(f"GitHub update install failed to start: {e}")
        return jsonify({"ok": False, "error": str(e), "status": build_ota_status()}), 500


# -------------------------------------------------
# Notification settings page
# -------------------------------------------------
@app.route("/notifications", methods=["GET", "POST"])
def notifications():
    defaults = {
        "GARAGE_NAME": get_garage_name(),
        "SENDER_EMAIL": "",
        "SENDER_APP_PASSWORD": "",
        "TEXT_TO": "",
        "SMTP_SERVER": "smtp.gmail.com",
        "SMTP_PORT": "465",
    }

    cfg = defaults.copy()
    cfg.update(read_key_value_config(NOTIFY_CFG_FILE))

    saved = request.args.get("saved")
    error = request.args.get("error")
    test = request.args.get("test")

    if request.method == "POST":
        action = (request.form.get("action") or "save").strip().lower()

        new_cfg = {
            "GARAGE_NAME": (request.form.get("garage_name") or "").strip(),
            "SENDER_EMAIL": (request.form.get("sender_email") or "").strip(),
            "SENDER_APP_PASSWORD": (request.form.get("sender_app_password") or "").strip(),
            "TEXT_TO": (request.form.get("text_to") or "").strip(),
            "SMTP_SERVER": (request.form.get("smtp_server") or "smtp.gmail.com").strip(),
            "SMTP_PORT": (request.form.get("smtp_port") or "465").strip(),
        }

        if not new_cfg["GARAGE_NAME"]:
            return redirect("/notifications?error=garage")
        if not new_cfg["SENDER_EMAIL"]:
            return redirect("/notifications?error=email")
        if not new_cfg["SENDER_APP_PASSWORD"]:
            return redirect("/notifications?error=password")
        if not new_cfg["TEXT_TO"]:
            return redirect("/notifications?error=recipients")
        try:
            port = int(new_cfg["SMTP_PORT"])
            if port < 1 or port > 65535:
                raise ValueError()
        except Exception:
            return redirect("/notifications?error=port")

        write_key_value_config(NOTIFY_CFG_FILE, new_cfg)
        log("Notification settings saved")

        if action == "test":
            ok = send_text_message("Garage Controller notification test.")
            return redirect("/notifications?saved=1&test=" + ("sent" if ok else "failed"))

        return redirect("/notifications?saved=1")

    return render_template(
        "notifications.html",
        garage_name=cfg.get("GARAGE_NAME", "Car Garage"),
        sender_email=cfg.get("SENDER_EMAIL", ""),
        sender_app_password=cfg.get("SENDER_APP_PASSWORD", ""),
        text_to=cfg.get("TEXT_TO", ""),
        smtp_server=cfg.get("SMTP_SERVER", "smtp.gmail.com"),
        smtp_port=cfg.get("SMTP_PORT", "465"),
        saved=saved,
        error=error,
        test=test,
    )

# -------------------------------------------------
# Setup page
# -------------------------------------------------
@app.route("/diagnostics")
def diagnostics_page():
    return render_template(
        "diagnostics.html",
        garage_name=get_garage_name()
    )


@app.route("/setup", methods=["GET", "POST"])
def setup():
    config = read_vent_config()

    if request.method == "POST":
        try:
            safe_write_json(p("last_setup_post.json"), request.form.to_dict(flat=True))
            log("Setup POST values received: " + json.dumps(request.form.to_dict(flat=True), sort_keys=True))
        except Exception as e:
            log(f"Setup POST logging failed: {e}")

        def get_form(name, default=""):
            value = request.form.get(name)
            if value is None:
                return str(default).strip()
            value = str(value).strip()
            return value if value != "" else str(default).strip()

        def to_int(name, default):
            try:
                return int(get_form(name, default))
            except Exception:
                return int(default)

        garage_name_value = get_form("garage_name", config.get("garage_name", get_garage_name()))
        if not garage_name_value:
            garage_name_value = DEFAULT_VENT_CONFIG["garage_name"]

        vent_dist = to_int("vent_dist", config.get("vent_distance", DEFAULT_VENT_CONFIG["vent_distance"]))
        min_dist = to_int("min_dist", config.get("min_distance", DEFAULT_VENT_CONFIG["min_distance"]))
        max_dist = to_int("max_dist", config.get("max_distance", DEFAULT_VENT_CONFIG["max_distance"]))
        light_lvl = to_int("light_level", config.get("light_level_on", DEFAULT_VENT_CONFIG["light_level_on"]))
        ping_ip = get_form("ping_ip", config.get("ping_ip", DEFAULT_VENT_CONFIG["ping_ip"]))
        event_logging_enabled_value = ("event_logging_enabled" in request.form)
        second_door_enabled_value = ("second_door_enabled" in request.form)
        second_door_name = get_form("second_door_name", config.get("second_door_name", DEFAULT_VENT_CONFIG["second_door_name"]))
        second_door_ip = get_form("second_door_ip", config.get("second_door_ip", DEFAULT_VENT_CONFIG["second_door_ip"]))
        wifi_roam_enabled = "wifi_roam_enabled" in request.form
        wifi_preferred_bssid = request.form.get("wifi_preferred_bssid", "").strip().upper()
        wifi_roam_min_db = to_int("wifi_roam_min_db", 10)
        if wifi_preferred_bssid and not re.fullmatch(r"(?:[0-9A-F]{2}:){5}[0-9A-F]{2}", wifi_preferred_bssid):
            return redirect("/setup?error=1")
        if wifi_roam_enabled and not wifi_preferred_bssid:
            return redirect("/setup?error=1")
        if wifi_roam_min_db not in (5, 10, 15):
            return redirect("/setup?error=1")

        if not (0 <= min_dist <= 500) or not (0 <= max_dist <= 500) or (min_dist >= max_dist):
            return redirect("/setup?error=1")
        if not (min_dist < vent_dist < max_dist):
            return redirect("/setup?error=1")
        if not ping_ip:
            return redirect("/setup?error=1")
        if second_door_enabled_value and not second_door_ip:
            return redirect("/setup?error=1")
        if not second_door_name:
            second_door_name = DEFAULT_VENT_CONFIG["second_door_name"]

        new_config = DEFAULT_VENT_CONFIG.copy()
        old_config = read_json(VENT_CFG_FILE, {})
        if isinstance(old_config, dict):
            new_config.update(old_config)

        new_config["garage_name"] = garage_name_value
        new_config["vent_distance"] = vent_dist
        new_config["min_distance"] = min_dist
        new_config["max_distance"] = max_dist
        new_config["light_level_on"] = light_lvl
        new_config["ping_ip"] = ping_ip
        new_config["event_logging_enabled"] = event_logging_enabled_value
        new_config["second_door_enabled"] = second_door_enabled_value
        new_config["second_door_name"] = second_door_name
        new_config["second_door_ip"] = second_door_ip
        new_config["wifi_roam_enabled"] = wifi_roam_enabled
        new_config["wifi_preferred_bssid"] = wifi_preferred_bssid
        new_config["wifi_roam_min_db"] = wifi_roam_min_db
        new_config["updated"] = time.time()

        safe_write_json(VENT_CFG_FILE, new_config)
        sync_notify_garage_name(garage_name_value)

        safe_write_json(PICO_OUTBOX_FILE, {
            "vent_distance": vent_dist,
            "min_distance": min_dist,
            "max_distance": max_dist,
            "light_level_on": light_lvl
        })

        log(f"Setup saved: ping_ip={ping_ip} path={VENT_CFG_FILE}")
        return redirect("/?saved=1")

    pos = read_json(POSITION_FILE, {})
    saved = request.args.get("saved")
    error = request.args.get("error")

    return render_template(
        "setup.html",
        garage_name=config.get("garage_name", get_garage_name()),
        vent_dist=config.get("vent_distance", DEFAULT_VENT_CONFIG["vent_distance"]),
        min_dist=config.get("min_distance", DEFAULT_VENT_CONFIG["min_distance"]),
        max_dist=config.get("max_distance", DEFAULT_VENT_CONFIG["max_distance"]),
        light_level=config.get("light_level_on", DEFAULT_VENT_CONFIG["light_level_on"]),
        ping_ip=config.get("ping_ip", DEFAULT_VENT_CONFIG["ping_ip"]),
        event_logging_enabled=bool(config.get("event_logging_enabled", False)),
        second_door_enabled=bool(config.get("second_door_enabled", False)),
        second_door_name=config.get("second_door_name", DEFAULT_VENT_CONFIG["second_door_name"]),
        second_door_ip=config.get("second_door_ip", DEFAULT_VENT_CONFIG["second_door_ip"]),
        local_ip=get_local_ip(),
        wifi_ssid=get_wifi_ssid(),
        wifi_roam_enabled=bool(config.get("wifi_roam_enabled", False)),
        wifi_preferred_bssid=config.get("wifi_preferred_bssid", ""),
        wifi_roam_min_db=int(config.get("wifi_roam_min_db", 10)),
        position=pos.get("position", ""),
        position_in=pos.get("position_in", ""),
        saved=saved,
        error=error
    )


# -------------------------------------------------
# Automation setup page
# -------------------------------------------------
@app.route("/automation_setup", methods=["GET", "POST"])
def automation_setup():
    config = read_vent_config()

    if request.method == "POST":
        try:
            safe_write_json(p("last_automation_post.json"), request.form.to_dict(flat=True))
            log("Automation POST values received: " + json.dumps(request.form.to_dict(flat=True), sort_keys=True))
        except Exception as e:
            log(f"Automation POST logging failed: {e}")

        def get_form(name, default=""):
            value = request.form.get(name)
            if value is None:
                return str(default).strip()
            value = str(value).strip()
            return value if value != "" else str(default).strip()

        def to_int(name, default):
            try:
                return int(get_form(name, default))
            except Exception:
                return int(default)

        auto_close_time = normalize_hhmm(
            get_form("auto_close_time", config.get("auto_close_time", DEFAULT_VENT_CONFIG["auto_close_time"])),
            DEFAULT_VENT_CONFIG["auto_close_time"]
        )
        auto_close_enabled = ("auto_close_enabled" in request.form)
        auto_vent_enabled_value = ("auto_vent_enabled" in request.form)
        auto_vent_temp_f = to_int("auto_vent_temp_f", config.get("auto_vent_temp_f", DEFAULT_VENT_CONFIG["auto_vent_temp_f"]))
        auto_vent_start_time = normalize_hhmm(
            get_form("auto_vent_start_time", config.get("auto_vent_start_time", DEFAULT_VENT_CONFIG["auto_vent_start_time"])),
            DEFAULT_VENT_CONFIG["auto_vent_start_time"]
        )
        auto_vent_stop_time = normalize_hhmm(
            get_form("auto_vent_stop_time", config.get("auto_vent_stop_time", auto_close_time)),
            auto_close_time
        )

        if not valid_hhmm(auto_close_time):
            return redirect("/automation_setup?error=1")
        if not valid_hhmm(auto_vent_start_time):
            return redirect("/automation_setup?error=1")
        if not valid_hhmm(auto_vent_stop_time):
            return redirect("/automation_setup?error=1")
        if auto_vent_start_time == auto_vent_stop_time:
            return redirect("/automation_setup?error=1")
        if not (60 <= auto_vent_temp_f <= 150):
            return redirect("/automation_setup?error=1")

        new_config = DEFAULT_VENT_CONFIG.copy()
        old_config = read_json(VENT_CFG_FILE, {})
        if isinstance(old_config, dict):
            new_config.update(old_config)

        new_config["auto_close_time"] = auto_close_time
        new_config["auto_close_enabled"] = auto_close_enabled
        new_config["auto_vent_enabled"] = auto_vent_enabled_value
        new_config["auto_vent_temp_f"] = auto_vent_temp_f
        new_config["auto_vent_start_time"] = auto_vent_start_time
        new_config["auto_vent_stop_time"] = auto_vent_stop_time
        new_config["updated"] = time.time()

        safe_write_json(VENT_CFG_FILE, new_config)
        log(
            "Automation saved/readback: "
            f"auto_close={auto_close_enabled}@{auto_close_time} "
            f"auto_vent={auto_vent_enabled_value} temp={auto_vent_temp_f} "
            f"window={auto_vent_start_time}-{auto_vent_stop_time}"
        )
        return redirect("/?saved=1")

    error = request.args.get("error")
    return render_template(
        "automation_setup.html",
        garage_name=get_garage_name(),
        auto_close_time=normalize_hhmm(config.get("auto_close_time"), DEFAULT_VENT_CONFIG["auto_close_time"]),
        auto_close_enabled=bool(config.get("auto_close_enabled", False)),
        auto_vent_enabled=bool(config.get("auto_vent_enabled", False)),
        auto_vent_temp_f=int(config.get("auto_vent_temp_f", DEFAULT_VENT_CONFIG["auto_vent_temp_f"])),
        auto_vent_start_time=normalize_hhmm(config.get("auto_vent_start_time"), DEFAULT_VENT_CONFIG["auto_vent_start_time"]),
        auto_vent_stop_time=normalize_hhmm(config.get("auto_vent_stop_time"), config.get("auto_close_time", DEFAULT_VENT_CONFIG["auto_vent_stop_time"])),
        status=automation_status(),
        error=error
    )

# -------------------------------------------------
# Commands
# -------------------------------------------------
@app.route("/command", methods=["POST"])
def command_post():
    data = request.get_json(silent=True) or {}
    cmd = str(data.get("command", "")).strip().lower()

    if pulse(cmd):
        log_button("API", cmd.upper())
        return jsonify({"ok": True})

    log_button("API", f"UNKNOWN command={cmd}")
    return jsonify({"ok": False, "error": "Unknown/disabled command"}), 400

@app.route("/command/open", methods=["GET", "POST"])
def cmd_open():
    log_button("WEB", "OPEN")
    pulse("open")
    return redirect("/")

@app.route("/command/close", methods=["GET", "POST"])
def cmd_close():
    log_button("WEB", "CLOSE")
    pulse("close")
    return redirect("/")

@app.route("/command/vent", methods=["GET", "POST"])
def cmd_vent():
    log_button("WEB", "VENT")
    pulse("vent")
    return redirect("/")

@app.route("/command/light", methods=["GET", "POST"])
def cmd_light():
    log_button("WEB", "LIGHT")
    pulse("light")
    return redirect("/")


@app.route("/command/stop", methods=["GET", "POST"])
def cmd_stop():
    log_button("WEB", "STOP")
    pulse("stop")
    return redirect("/")

# -------------------------------------------------
# Startup
# -------------------------------------------------
_threads_started = False
_threads_lock = threading.Lock()

def start_threads_once():
    global _threads_started
    with _threads_lock:
        if _threads_started:
            return
        _threads_started = True
        log_button("SYSTEM", "APP STARTED")

        # NOTE: app.py service owns UART by running serial_loop in-process.
        threading.Thread(target=serial_loop, daemon=True).start()
        threading.Thread(target=fan_loop, daemon=True).start()
        threading.Thread(target=auto_close_loop, daemon=True).start()
        threading.Thread(target=auto_vent_loop, daemon=True).start()
        log("Background threads started (serial/fan/autoclose/autovent)")

# If running under systemd/gunicorn, this will execute on import.
start_threads_once()

if __name__ == "__main__":
    log("Starting Flask on port 5000")
    app.run(host="0.0.0.0", port=5000)
