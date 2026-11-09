# Installation and maintenance

The source deployment uses Raspberry Pi OS Bookworm on a Pi Zero 2 W. The provided dashboard service expects the `pi` user and the project at `/home/pi/garage`. Use a fresh directory for a new deployment. For an existing controller, back up its installation and local settings before changing source files.

## Pi Zero

1. Enable the hardware serial interface and disable the serial login shell in `sudo raspi-config`. Reboot and confirm `readlink -f /dev/serial0` returns a device.
2. Put this checkout at `/home/pi/garage`, owned by the dashboard user. Run `./install.sh` from the checkout. It installs Raspberry Pi OS dependencies and installs the dashboard unit. Start the service explicitly after reviewing the wiring and Pico setup.
3. Run `sudo systemctl enable --now garage.service`. Open `http://<pi-address>:5000/` and calibrate the open, closed, and vent distances on Setup. Verify the displayed position and test the controls with the door in view before enabling Auto Close or Auto Vent. The application defaults both automations to off.
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
| Stop input | BCM 5 | GP14 |
| Pico reset control | BCM 26 | Board reset circuit |
| Enclosure fan control | BCM 16 | - |

The Pico uses GP8/GP9 for sensor I2C and GP18/GP22 for the opener and light relay control signals. Follow your board wiring for the relay drivers, reset circuit, fan driver, and power switching. The table lists logic signals only. Confirm the opener's photo-eye and reversal protection before enabling scheduled operation.

## Pico bootstrap and updates

For a blank Pico, first install MicroPython for the board over USB. Using a tool such as Thonny, copy `pico/BME280.py` and `pico/adafruit_simplemath.py` to the Pico filesystem first, then copy `pico/main.py` and reset the Pico. The snapshot also includes the two PiicoDev files from the existing OTA repository for the earlier VL53L1X sensor setup. The SimpleMath module comes from [Adafruit CircuitPython SimpleMath](https://github.com/adafruit/Adafruit_CircuitPython_SimpleMath/tree/3653b67541b7dbf0fbaaa65628c25cc6900dea87) under the included MIT license. Verify first boot over USB with the opener disconnected before relying on UART updates.

For an already bootstrapped Pico with a working UART updater, use **Pico Firmware** on the dashboard. The Pi downloads the OTA manifest and `main.py`, then the dashboard's serial reader transfers the application over UART. The OTA transfer updates only `main.py`. For a local firmware file, use `python3 pico_update_local.py /path/to/main.py` from the running Pi installation. Let the door settle and keep it in view while servicing firmware.

## Local settings and optional networking

Set the router or other ping target for your network on Setup. The application keeps local settings in `vent_config.json`, `notify_config.json`, and `diagnostics_config.json`. Configure notifications through the dashboard. `notify_config.json` uses key-value text and may hold an SMTP app password. Keep it with private backups.

The optional Wi-Fi roam timer and network watchdog units are included for reference. The roam helper is specific to the source deployment's SSID. Review its SSID, interface, and BSSID settings before adapting it. The watchdog can cycle Wi-Fi and reboot the Pi. Review the permissions and script paths before enabling either optional unit, since they run with elevated privileges. The installer only installs the dashboard unit.
