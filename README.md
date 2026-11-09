# Garage Door Controller

A local web dashboard for a garage door, built with a Raspberry Pi Zero 2 W and a Raspberry Pi Pico. The Pi hosts the dashboard and sends commands to the Pico. The Pico handles the opener relays and sensors, including the Garmin LIDAR-Lite v4 distance sensor. The dashboard shows position, environment readings, and Pico status and includes scheduled closing, temperature-based venting, diagnostics, and firmware updates.

## Repository layout

- `app.py`, `templates/`, `static/` - Flask dashboard and setup pages.
- `serial_reader.py`, `fan_control.py` - Pico UART communication and Pi enclosure fan.
- `deploy/` - systemd units from the source controller.
- `pico/` - firmware and support files from the [Pico OTA repository](https://github.com/RLF62/ota_garage_door_opener), pinned to commit `4321b28998dc1124a532f241245752c33145b995`.
- [Installation and maintenance](docs/installation.md) - setup, deployment, and update procedures.

The **Pico Firmware** page continues to fetch `version.json` and `main.py` from the dedicated OTA repository. The firmware snapshot here documents the matching controller package and provides files for a USB bootstrap.

Run the dashboard on a trusted local network. The dashboard has no application login, and its endpoints can operate a physical door.
