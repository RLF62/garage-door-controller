#!/usr/bin/env python3
"""Conservative preferred-BSSID roam for NetworkManager; default dry-run."""
import argparse, datetime, fcntl, json, os, re, subprocess, sys, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG = HERE / 'vent_config.json'
STATUS = HERE / 'wifi_roam_status.json'
MAC = re.compile(r'^(?:[0-9A-F]{2}:){5}[0-9A-F]{2}$')

def run(*args, timeout=20):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=True).stdout.strip()

def link():
    s = run('iw', 'dev', 'wlan0', 'link', timeout=8)
    b = re.search(r'Connected to ([0-9a-f:]{17})', s, re.I)
    n = re.search(r'^\s*SSID: (.*)$', s, re.M)
    d = re.search(r'signal:\s*(-?[\d.]+)\s*dBm', s)
    return (b.group(1).upper() if b else '', n.group(1) if n else '', float(d.group(1)) if d else None)

def status(message, preferred_seen=None):
    data = {'last_check': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'), 'last_result': message, 'preferred_seen': preferred_seen}
    temp = STATUS.with_suffix('.json.tmp')
    temp.write_text(json.dumps(data, indent=2) + '\n')
    os.replace(temp, STATUS)
    print(message, flush=True)

def door_stable():
    # Detect movement using fresh position readings. This is a precaution, not a motor interlock.
    f = HERE / 'position.json'
    def sample():
        if time.time() - f.stat().st_mtime > 20:
            raise ValueError('position data stale')
        v = json.loads(f.read_text())
        return float(v['position_in'])
    a = sample()
    time.sleep(3)
    b = sample()
    return abs(b-a) < 0.7

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true', help='Allow actual roaming')
    args = ap.parse_args()
    lock = open('/run/lock/garage-wifi-roam.lock', 'w')
    try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print('Another check is already running'); return
    cfg = json.loads(CONFIG.read_text())
    if not cfg.get('wifi_roam_enabled', False):
        status('Roaming disabled in Setup'); return
    preferred = str(cfg.get('wifi_preferred_bssid', '')).upper().strip()
    if not MAC.fullmatch(preferred):
        status('Invalid preferred BSSID'); return
    threshold = int(cfg.get('wifi_roam_min_db', 10))
    if threshold not in (5, 10, 15):
        status('Invalid improvement threshold'); return
    current, ssid, dbm = link()
    if not current or ssid != 'FISCHER' or dbm is None:
        status('Not connected to FISCHER; no change'); return
    if current == preferred:
        status(f'Already on preferred AP {preferred} ({dbm:.0f} dBm)', True); return
    try:
        run('nmcli', 'device', 'wifi', 'rescan', 'ifname', 'wlan0', timeout=25)
        time.sleep(4)
        raw = run('nmcli', '-t', '--escape', 'no', '-f', 'SSID,BSSID,SIGNAL', 'device', 'wifi', 'list', 'ifname', 'wlan0', '--rescan', 'no')
    except Exception as exc:
        status(f'Scan failed; unchanged: {exc}'); return
    signals = {}
    for line in raw.splitlines():
        parts = line.split(':')
        if len(parts) == 8 and parts[0] == 'FISCHER':
            mac = ':'.join(parts[1:7]).upper()
            try: signals[mac] = int(parts[7])
            except ValueError: pass
    if preferred not in signals or current not in signals:
        status('Preferred/current AP not both visible in scan; unchanged', preferred in signals); return
    diff = signals[preferred] - signals[current]
    # NM signal is a percentage, not dBm. 2 percentage points/dB is only an estimate.
    message = f'Current {current} {signals[current]}%, preferred {preferred} {signals[preferred]}% (difference {diff} points)'
    if diff < threshold * 2:
        status(message + f'; below approximate {threshold} dB threshold; unchanged', True); return
    try:
        if not door_stable():
            status(message + '; door position changing; unchanged', True); return
    except Exception as exc:
        status(message + f'; door motion cannot be verified ({exc}); unchanged', True); return
    if not args.apply:
        status(message + '; DRY RUN: would roam', True); return
    # Use existing profile, with a one-time AP selection. Do not persist a BSSID lock.
    profile = run('nmcli', '-g', 'GENERAL.CONNECTION', 'device', 'show', 'wlan0')
    if not profile or profile == '--':
        status('No active NetworkManager profile; unchanged', True); return
    status(message + f'; attempting roam using {profile}', True)
    try:
        run('nmcli', '--wait', '25', 'connection', 'up', profile, 'ifname', 'wlan0', 'ap', preferred, timeout=35)
        time.sleep(3)
        now, name, rssi = link()
        if now == preferred and name == 'FISCHER':
            status(f'Roam SUCCESS: {current} -> {preferred}; signal {rssi} dBm', True)
        else:
            status(f'Roam attempted but connected to {now or "none"}; check connectivity', True)
    except Exception as exc:
        status(f'Roam failed: {exc}; NetworkManager may reconnect automatically', True)

if __name__ == '__main__':
    try: main()
    except Exception as exc:
        status(f'Check failed safely: {exc}')
        sys.exit(1)
