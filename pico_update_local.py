#!/usr/bin/env python3
"""
Request a Pico firmware update through serial_reader.py.

Usage:
  cd /home/pi/garage
  python3 pico_update_local.py /home/pi/garage/pico_updates/main.py

This script does NOT open /dev/serial0. It writes pico_update_request.json so
serial_reader.py can perform the UART transfer without serial-port conflicts.
"""
import json
import os
import sys
import time
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REQUEST_FILE = os.path.join(BASE_DIR, "pico_update_request.json")
STATUS_FILE = os.path.join(BASE_DIR, "pico_update_status.json")


def write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


def read_json(path):
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def main():
    if len(sys.argv) != 2:
        print("Usage: python3 pico_update_local.py /full/path/to/main.py")
        return 2

    update_file = os.path.abspath(sys.argv[1])
    if not os.path.exists(update_file):
        print(f"ERROR: file not found: {update_file}")
        return 2

    req = {
        "file": update_file,
        "chunk_size": 256,
        "requested_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    write_json(REQUEST_FILE, req)
    print(f"Requested Pico update: {update_file}")
    print("Waiting for serial_reader.py to finish...")

    last_state = None
    start = time.time()
    while True:
        status = read_json(STATUS_FILE)
        state = status.get("state", "waiting")
        progress = status.get("progress", 0)
        if state != last_state or state == "sending":
            print(f"state={state} progress={progress}%")
            last_state = state

        if state in ("success", "failed", "cancelled"):
            print(json.dumps(status, indent=2, sort_keys=True))
            return 0 if state == "success" else 1

        if time.time() - start > 1800:
            print("ERROR: timed out waiting for update status")
            print(json.dumps(status, indent=2, sort_keys=True))
            return 1

        time.sleep(1)


if __name__ == "__main__":
    raise SystemExit(main())
