#!/usr/bin/env python3
import json
import os
import re
import time
import subprocess
from datetime import datetime

BASE_DIR = "/home/pi/garage"
VENT_CFG_FILE = os.path.join(BASE_DIR, "vent_config.json")
NETWORK_STATUS_FILE = os.path.join(BASE_DIR, "network_status.json")
LOG_FILE = os.path.join(BASE_DIR, "network_watchdog.log")

DEFAULT_PING_IP = "192.168.50.1"
CHECK_EVERY_SEC = 60
FAILS_BEFORE_WIFI_RESET = 3
FAILS_BEFORE_REBOOT = 6


def log(msg):
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def safe_write_json(path, data):
    try:
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception as e:
        log(f"Failed to write {path}: {e}")


def read_json(path, default):
    try:
        if not os.path.exists(path):
            return default
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return default


def get_ping_ip():
    cfg = read_json(VENT_CFG_FILE, {})
    ping_ip = str(cfg.get("ping_ip", DEFAULT_PING_IP)).strip()
    return ping_ip or DEFAULT_PING_IP


def run(cmd):
    return subprocess.run(cmd, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def ping_ok(ping_ip):
    result = subprocess.run(
        ["ping", "-c", "1", "-W", "3", ping_ip],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    latency_ms = None
    output = (result.stdout or "") + "\n" + (result.stderr or "")
    match = re.search(r"time=([0-9.]+)\s*ms", output)
    if match:
        try:
            latency_ms = round(float(match.group(1)), 1)
        except Exception:
            latency_ms = None

    return result.returncode == 0, latency_ms


def write_status(online, ping_ip, fails, latency_ms=None):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    old = read_json(NETWORK_STATUS_FILE, {})

    data = {
        "online": bool(online),
        "ping_ip": ping_ip,
        "last_check": now,
        "fail_count": int(fails),
        "latency_ms": latency_ms,
    }

    if online:
        data["last_success"] = now
    else:
        data["last_success"] = old.get("last_success", "")

    safe_write_json(NETWORK_STATUS_FILE, data)


def wifi_reset():
    log("WiFi reset started")
    run("nmcli radio wifi off")
    time.sleep(10)
    run("nmcli radio wifi on")
    time.sleep(30)
    log("WiFi reset finished")


def reboot_pi():
    log("Network still down, rebooting Pi")
    os.system("sudo /sbin/reboot")


def main():
    log("Network watchdog started")

    fails = 0
    last_logged_ping_ip = None

    while True:
        ping_ip = get_ping_ip()

        if ping_ip != last_logged_ping_ip:
            log(f"Network watchdog ping target set to {ping_ip}")
            last_logged_ping_ip = ping_ip

        ok, latency_ms = ping_ok(ping_ip)

        if ok:
            if fails:
                log(f"Network recovered using {ping_ip}")
            fails = 0
            write_status(True, ping_ip, fails, latency_ms)
        else:
            fails += 1
            write_status(False, ping_ip, fails, None)
            log(f"Ping failed target={ping_ip} count={fails}")

            if fails == FAILS_BEFORE_WIFI_RESET:
                wifi_reset()

            if fails >= FAILS_BEFORE_REBOOT:
                reboot_pi()

        time.sleep(CHECK_EVERY_SEC)


if __name__ == "__main__":
    main()
