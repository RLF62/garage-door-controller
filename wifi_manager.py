import subprocess
import time
import shutil

WPA_CONF = "/etc/wpa_supplicant/wpa_supplicant.conf"


def _bin(cmd, fallback):
    path = shutil.which(cmd)
    return path if path else fallback


SYSTEMCTL = _bin("systemctl", "/bin/systemctl")
IW = _bin("iw", "/sbin/iw")
WPA_PASS = _bin("wpa_passphrase", "/usr/bin/wpa_passphrase")
WPA_CLI = _bin("wpa_cli", "/sbin/wpa_cli")
RFKILL = _bin("rfkill", "/usr/sbin/rfkill")


def run(cmd):
    return subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def scan_wifi():
    result = run([IW, "dev", "wlan0", "scan"])
    networks = set()

    for line in result.stdout.splitlines():
        line = line.strip()
        if line.startswith("SSID:"):
            ssid = line.replace("SSID:", "").strip()
            if ssid:
                networks.add(ssid)

    return sorted(networks)


def save_wifi(ssid, password):
    result = run([WPA_PASS, ssid, password])
    if result.returncode != 0:
        raise RuntimeError("Failed to generate WPA config")

    with open(WPA_CONF, "w") as f:
        f.write("country=US\n")
        f.write("ctrl_interface=DIR=/var/run/wpa_supplicant GROUP=netdev\n")
        f.write("update_config=1\n\n")
        f.write(result.stdout)


def switch_to_client():
    run([SYSTEMCTL, "stop", "hostapd"])
    run([SYSTEMCTL, "stop", "dnsmasq"])

    run([RFKILL, "unblock", "wlan"])
    run([WPA_CLI, "-i", "wlan0", "reconfigure"])
    time.sleep(6)


def enable_hotspot():
    run([SYSTEMCTL, "start", "dnsmasq"])
    run([SYSTEMCTL, "start", "hostapd"])
