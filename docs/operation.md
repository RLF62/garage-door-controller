# Dashboard and operation

[Back to project README](../README.md)

## Everyday use

Open `http://<pi-address>:5000/`. The door picture moves with the reported position. **100% means closed** and **0% means open**. The Pico calculates position from the Garmin LIDAR-Lite v4 measurement, and the dashboard can show **Vented** when the Pico confirms the calibrated target. **Pico Online** comes from recent UART activity. The temperature and humidity cards report the BME280 readings. The CPU temperature next to the fan is the Pi's own temperature.

| Control | Behavior |
| --- | --- |
| Open / Close | Ask the Pico to move toward the corresponding endpoint. The controller observes door movement and position through the distance sensor. |
| Vent | Ask the Pico to stop at the calibrated partial-opening distance. |
| Light | Pulse the separate opener light control. |
| Auto Vent | Enable or disable the scheduled temperature-based feature. The settings and temperature threshold are on **Automation Setup**. |

The bundled Pico firmware disables the external Stop input. Use the opener's own controls when commissioning the controller.

## Setup and calibration

1. Leave **Auto Close** and **Auto Vent** off. With the door in view, move it using the opener's original control and let it settle at each measurement point.
2. In **Door Setup**, record **Position Inch** with the door fully open, fully closed, and at the vent height you want. Enter those readings under **Open Distance**, **Closed Distance**, and **Vent Distance**. On this mounting arrangement, the readings must increase from open to closed, with vent between them.
3. Observe **Light Level** with the light on and off. Set **Light Level On** using those two readings.
4. Save and check `pico_settings_status.json` for `"confirmed": true`. The Pi sends the distance and light values over UART and checks that the Pico acknowledges the same values.
5. Verify both endpoints and the vent position in view of the door. If the vent reading is inconsistent, check that the LIDAR target is secure and the beam has a clear path over the full travel.
6. Set a reachable LAN ping target. To link a second door, enable **Show Second Door Link on Main Page** and enter its IP address or full dashboard URL. A plain address uses port 5000.

## Show your own vehicle behind the door

The dashboard layers five moving door panels over `static/garage.png`. Replace that background PNG with a photo of your garage and vehicle. As the graphic opens, the vehicle is revealed.

1. Take a straight-on photo from outside with the garage door open. Crop it to the doorway and export it as a PNG. The original is **880 × 668 pixels**, so use that size or the same aspect ratio to avoid stretching.
2. Copy the PNG to the Pi, for example as `~/my-vehicle.png`. Then back up the original and replace the dashboard image.

   ```bash
   cd /home/pi/garage
   cp -n static/garage.png "$HOME/garage-background-original.png"
   cp "$HOME/my-vehicle.png" static/garage.png
   ```

3. Reload the dashboard. If you still see the old photo, use a hard refresh or clear the site's cached image. The app serves the PNG directly, so changing it requires no service restart.

To restore the original, run `cp "$HOME/garage-background-original.png" /home/pi/garage/static/garage.png`. In a two-door setup, repeat this on each Pi with its own photo. Keep your replacement image with your local backups.

The browser tab icon is a separate file at `static/icons/garage.png`. Replace that PNG as well if you want to change the favicon. Leave `static/panel.png` in place to keep the existing moving door panels.

## Automation and notifications

| Setting | Current behavior |
| --- | --- |
| Auto Close | Uses the Pi's local time. At the configured minute, the Pi requests Close if the latest stored position is below 98%. The log and notification record the request. Check the door reading for the result. |
| Auto Vent | Checks about every 30 seconds. Requests Vent when the measured temperature meets the threshold, the door is above 95% closed, the current time is inside the configured window, and at least one hour has elapsed since the last request in that app process. |
| Auto Vent window | The start is inclusive and the stop is exclusive. The scheduler supports overnight windows. Equal times disable the window. Set the window to end at or before the Auto Close time when you want the door to stay closed afterward. |

Verify the Pi's time zone with `timedatectl`. Keep the opener's manufacturer-provided obstruction detection and reversal protection in service and follow its instructions for testing those protections before enabling scheduled movement. See the [CPSC garage door operator guidance](https://www.cpsc.gov/s3fs-public/FinalRuleAmendSafetyStandardAutomaticResidentialGarageDoorOperators.pdf).

The **Notifications** page stores its SMTP settings on the Pi. Treat the notification settings file as private because it can contain an app password.

## Diagnostics and event logging

**Diagnostics & Test Commands** gives you a ping test and a five-second fan test. **Enable Event Logging** in Door Setup writes command source labels, such as `WEB`, `API`, `AUTO_CLOSE`, and `AUTO_VENT`, to `button_log.txt`. The firmware page's diagnostics switch controls more detailed Pico diagnostic collection. See the [reference](reference.md) for files and commands.
