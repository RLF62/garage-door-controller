import os

from flask import Flask, render_template, jsonify, request
import requests

app = Flask(__name__)

GARAGES = {
    "truck": {
        "name": "Truck Garage",
        "url": os.environ["TRUCK_GARAGE_URL"].rstrip("/")
    },
    "car": {
        "name": "Car Garage",
        "url": os.environ["CAR_GARAGE_URL"].rstrip("/")
    }
}


def get_text(base_url, endpoint):
    try:
        r = requests.get(base_url + endpoint, timeout=2)
        r.raise_for_status()
        return r.text.strip()
    except Exception:
        return "OFFLINE"


def send_command(base_url, command):
    try:
        r = requests.post(base_url + "/command/" + command, timeout=3)
        return r.ok
    except Exception:
        return False


def set_auto_vent(base_url, enabled):
    try:
        r = requests.post(
            base_url + "/set_auto_vent",
            json={"enabled": bool(enabled)},
            timeout=3
        )
        if not r.ok:
            return False, {"error": "Controller returned HTTP " + str(r.status_code)}
        return True, r.json()
    except Exception as e:
        return False, {"error": str(e)}


@app.route("/")
def index():
    return render_template("index.html", garages=GARAGES)


@app.route("/status")
def status():
    return jsonify({
        "truck": {
            "position": get_text(GARAGES["truck"]["url"], "/position"),
            "temperature_f": get_text(
                GARAGES["truck"]["url"],
                "/environment_temp"
            ),
            "vent_status": get_text(GARAGES["truck"]["url"], "/vent_status"),
            "auto_vent_enabled": get_text(
                GARAGES["truck"]["url"],
                "/auto_vent_enabled"
            )
        },
        "car": {
            "position": get_text(GARAGES["car"]["url"], "/position"),
            "temperature_f": get_text(
                GARAGES["car"]["url"],
                "/environment_temp"
            ),
            "vent_status": get_text(GARAGES["car"]["url"], "/vent_status"),
            "auto_vent_enabled": get_text(
                GARAGES["car"]["url"],
                "/auto_vent_enabled"
            )
        }
    })


@app.route("/garage/<garage>/<command>", methods=["GET", "POST"])
def garage_command(garage, command):
    if garage not in GARAGES:
        return jsonify({"success": False, "error": "Unknown garage"}), 404

    if command not in {"open", "close", "vent", "light"}:
        return jsonify({"success": False, "error": "Unknown command"}), 400

    ok = send_command(GARAGES[garage]["url"], command)
    return jsonify({"success": ok}), (200 if ok else 502)


@app.route("/garage/<garage>/auto_vent", methods=["POST"])
def garage_auto_vent(garage):
    if garage not in GARAGES:
        return jsonify({"success": False, "error": "Unknown garage"}), 404

    data = request.get_json(silent=True) or {}
    enabled = data.get("enabled", None)

    if not isinstance(enabled, bool):
        return jsonify({
            "success": False,
            "error": "JSON field 'enabled' must be true or false"
        }), 400

    ok, result = set_auto_vent(GARAGES[garage]["url"], enabled)
    result["success"] = ok
    return jsonify(result), (200 if ok else 502)


@app.route("/both/<command>", methods=["GET", "POST"])
def both_command(command):
    if command not in {"open", "close", "vent", "light"}:
        return jsonify({"success": False, "error": "Unknown command"}), 400

    truck_ok = send_command(GARAGES["truck"]["url"], command)
    car_ok = send_command(GARAGES["car"]["url"], command)

    return jsonify({
        "success": truck_ok and car_ok,
        "truck": truck_ok,
        "car": car_ok
    }), (200 if truck_ok and car_ok else 502)


@app.route("/both/auto_vent", methods=["POST"])
def both_auto_vent():
    data = request.get_json(silent=True) or {}
    enabled = data.get("enabled", None)

    if not isinstance(enabled, bool):
        return jsonify({
            "success": False,
            "error": "JSON field 'enabled' must be true or false"
        }), 400

    truck_ok, truck_result = set_auto_vent(GARAGES["truck"]["url"], enabled)
    car_ok, car_result = set_auto_vent(GARAGES["car"]["url"], enabled)

    return jsonify({
        "success": truck_ok and car_ok,
        "truck": {"success": truck_ok, **truck_result},
        "car": {"success": car_ok, **car_result}
    }), (200 if truck_ok and car_ok else 502)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5050, threaded=True)
