# Pico firmware

[Back to project README](../README.md)

## First install on a blank Pico

1. Install MicroPython for the board over USB. Use a USB tool such as Thonny to connect to the Pico filesystem.
2. Copy [BME280.py](../pico/BME280.py) and [adafruit_simplemath.py](../pico/adafruit_simplemath.py) first, then [main.py](../pico/main.py). The PiicoDev files are preserved from the OTA repository for the earlier VL53L1X sensor setup. The bundled `main.py` uses the Garmin LIDAR-Lite v4 driver it includes.
3. With the opener disconnected, verify that the Pico boots over USB. Connect the UART as shown in the [wiring table](installation.md#signal-reference), then verify **Pico Online**, the firmware version, and the calibration acknowledgement on the Pi.
4. Calibrate the door and light sensor using [the operation guide](operation.md#setup-and-calibration).

SimpleMath is from [Adafruit CircuitPython SimpleMath](https://github.com/adafruit/Adafruit_CircuitPython_SimpleMath/tree/3653b67541b7dbf0fbaaa65628c25cc6900dea87) under the [included license](../pico/LICENSE-adafruit-simplemath).

## Update from the dashboard

Use **Door Setup → Pico Firmware Update** on an already bootstrapped controller. Keep the door stationary and wait for the update to finish before operating it.

1. Select **Refresh Pico Version** to request the Pico's running version over UART.
2. Select **Check GitHub** to fetch the release manifest from [ota_garage_door_opener](https://github.com/RLF62/ota_garage_door_opener).
3. Select **Install Update** when the page offers it. Watch the transfer status and let the Pico reboot.
4. Confirm the Pico is online again, refresh its version, and check that calibration is confirmed before normal use.

The dashboard compares only the three numeric version components. For example, the pinned manifest and firmware identify themselves with different suffixes but both compare as `1.0.26`. When releasing new code, increment the numeric version in both places. The dashboard fetches only `main.py` and its manifest. Install missing support modules separately over USB.

## Pi-side commands

Run as the normal dashboard user from `/home/pi/garage`. The running dashboard owns the UART. The helper submits a file-based request to that process.

| Command | Purpose |
| --- | --- |
| `python3 github_pico_update.py check` | Fetch and display the OTA manifest. |
| `python3 github_pico_update.py download` | Cache the manifest and firmware on the Pi and back up the previous cached `main.py` if present. |
| `python3 github_pico_update.py status` | Display the locally cached manifest. |
| `python3 pico_update_local.py /path/to/main.py` | Queue a specific local file and wait for the UART transfer result. This bypasses the dashboard's version comparison. |

## Transfer and recovery

The Pi sends `update_start`, waits for `ready`, sends base64-encoded chunks with sequential acknowledgements, then sends `update_end`. The Pico checks the expected byte count and a 32-bit additive checksum, stages the payload as `main.py.new`, keeps the prior program as `main.py.bak` when the rename succeeds, and soft-resets after acknowledging success. The checksum checks transfer consistency. It provides no signature verification.

If the transfer stalls, read `pico_update_status.json` and `status.log` on the Pi and check UART health before retrying. If the Pico fails to boot after the transfer, use USB with the opener disconnected and restore a known-good `main.py` and its required modules. If accessible, inspect the Pico's `main.py.bak` before using it as the restore copy. Files under `pico_backups/` on the Pi are previous Pi-side downloads.
