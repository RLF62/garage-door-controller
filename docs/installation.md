# Installation and first startup

[Back to project README](../README.md)

The source deployment uses Raspberry Pi OS Bookworm on a Pi Zero 2 W. The provided dashboard service expects the `pi` user and the project at `/home/pi/garage`. Use a fresh directory for a new deployment. For an existing controller, back up its installation and local settings before changing source files.

## Pi Zero

1. Enable the hardware serial interface and disable the serial login shell in `sudo raspi-config`. Reboot and confirm `readlink -f /dev/serial0` returns a device.
2. Put this checkout at `/home/pi/garage`, owned by the dashboard user. Run `./install.sh` from the checkout. It installs Raspberry Pi OS dependencies and installs the dashboard unit. Start the service explicitly after reviewing the wiring and Pico setup.
3. Run `sudo systemctl enable --now garage.service`. Open `http://<pi-address>:5000/`, then use the [calibration procedure](operation.md#setup-and-calibration) to save the settings and confirm the Pico received them. The application defaults both automations to off.
4. Check logs with `sudo journalctl -u garage.service -n 80 --no-pager`.

The dashboard owns the UART in its own process. Keep one dashboard process and one UART owner. Importing `app.py` starts the hardware threads, so use syntax checks for a hardware-free review.

## Signal reference

Pi numbers are BCM GPIO numbers. Pico numbers are GP numbers. Use 3.3 V logic and a shared ground between the controllers.

| Signal | Pi Zero | Pico |
| --- | --- | --- |
| UART TX from Pi | BCM 14 | GP1 (RX) |
| UART RX into Pi | BCM 15 | GP0 (TX) |
| Open | BCM 17 | GP11 |
| Close | BCM 27 | GP12 |
| Vent | BCM 22 | GP13 |
| Light | BCM 4 | GP10 |
| Stop input reserved by the pin map | BCM 5 | GP14 |
| Pico reset control | BCM 26 | Board reset circuit |
| Enclosure fan control | BCM 16 | - |

The Pico uses GP8 for I2C SDA, GP9 for I2C SCL, GP26 for the light sensor analog input, and GP18/GP22 for the opener and light relay control signals. GP2 controls the Pi power circuit on the source hardware. The bundled firmware leaves the external Stop input disabled. Follow your board wiring for the relay drivers, reset circuit, fan driver, and power switching. The table lists logic signals only. Confirm the opener's photo-eye and reversal protection before enabling scheduled operation.

## Pico bootstrap and updates

Use the [firmware guide](firmware.md#first-install-on-a-blank-pico) for first-time setup over USB, dashboard updates over UART, and recovery.

## Local settings and optional networking

Set the router or other ping target for your network on Setup. The application keeps local settings in `vent_config.json`, `notify_config.json`, and `diagnostics_config.json`. Use `deploy/vent_config.example.json` as a reference for starting settings. Save your measured values in Door Setup. Configure notifications through the dashboard. `notify_config.json` uses key-value text and may hold an SMTP app password. Keep it with private backups.

The optional Wi-Fi roam timer and network watchdog units are included for reference. The roam helper is specific to the source deployment's SSID. Review its SSID, interface, and BSSID settings before adapting it. The watchdog can cycle Wi-Fi and reboot the Pi. Review the permissions and script paths before enabling either optional unit, since they run with elevated privileges. The installer only installs the dashboard unit.
