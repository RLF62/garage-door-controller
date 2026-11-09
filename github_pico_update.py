#!/usr/bin/env python3
"""
Simple GitHub downloader for Pico firmware.

Uses your public repo raw files:
  https://raw.githubusercontent.com/RLF62/ota_garage_door_opener/main/version.json
  https://raw.githubusercontent.com/RLF62/ota_garage_door_opener/main/main.py

Commands:
  python3 github_pico_update.py check
  python3 github_pico_update.py download
  python3 github_pico_update.py install
  python3 github_pico_update.py status
"""
import json
import os
import subprocess
import sys
import urllib.request
import shutil
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_RAW = "https://raw.githubusercontent.com/RLF62/ota_garage_door_opener/main"
VERSION_URL = REPO_RAW + "/version.json"
MAIN_URL = REPO_RAW + "/main.py"
DOWNLOAD_DIR = os.path.join(BASE_DIR, "pico_updates")
LATEST_MAIN = os.path.join(DOWNLOAD_DIR, "main.py")
LATEST_VERSION = os.path.join(DOWNLOAD_DIR, "version.json")
BACKUP_DIR = os.path.join(BASE_DIR, "pico_backups")


def fetch(url, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with urllib.request.urlopen(url, timeout=20) as r:
        data = r.read()
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)
    return len(data)


def read_json(path):
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def check():
    n = fetch(VERSION_URL, LATEST_VERSION)
    data = read_json(LATEST_VERSION)
    print(f"Downloaded version.json ({n} bytes)")
    print(json.dumps(data, indent=2, sort_keys=True))
    return 0


def backup_existing_main():
    if not os.path.exists(LATEST_MAIN):
        return None
    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    dst = os.path.join(BACKUP_DIR, f"main_{stamp}.py")
    shutil.copy2(LATEST_MAIN, dst)
    return dst


def download():
    check()
    backup = backup_existing_main()
    if backup:
        print(f"Backed up previous downloaded main.py to {backup}")
    n = fetch(MAIN_URL, LATEST_MAIN)
    print(f"Downloaded main.py ({n} bytes) to {LATEST_MAIN}")
    return 0


def install():
    download()
    helper = os.path.join(BASE_DIR, "pico_update_local.py")
    if not os.path.exists(helper):
        print(f"ERROR: missing {helper}")
        return 1
    return subprocess.call([sys.executable, helper, LATEST_MAIN])


def status():
    data = read_json(LATEST_VERSION)
    if not data:
        print(json.dumps({"ok": False, "error": "No downloaded version.json yet. Run check first."}, indent=2))
        return 1
    print(json.dumps({"ok": True, "latest": data}, indent=2, sort_keys=True))
    return 0


def main():
    cmd = sys.argv[1].strip().lower() if len(sys.argv) > 1 else "check"
    if cmd == "check":
        return check()
    if cmd == "download":
        return download()
    if cmd == "install":
        return install()
    if cmd == "status":
        return status()
    print("Usage: python3 github_pico_update.py check|download|install|status")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
