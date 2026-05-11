import json
import os


def _load_homeassistant_options():
    options_path = os.environ.get("HASSIO_OPTIONS", "/data/options.json")
    try:
        with open(options_path, encoding="utf-8") as options_file:
            return json.load(options_file)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


_OPTIONS = _load_homeassistant_options()


def _bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0

    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"Invalid boolean value: {value}")


def _setting(name, default, cast=None):
    value = os.environ.get(name.upper(), _OPTIONS.get(name, default))
    if value is None:
        return default
    if cast is None:
        return value
    return cast(value)


INVERTER_SERIAL = _setting("inverter_serial", 123456789, int)   # WiFi stick serial number
INVERTER_IP = _setting("inverter_ip", "192.168.1.55")           # IP address of inverter
INVERTER_PORT = _setting("inverter_port", 8899, int)            # Port number
MQTT_SERVER = _setting("mqtt_server", "192.168.1.20")           # IP address of MQTT server
MQTT_PORT = _setting("mqtt_port", 1883, int)                    # Port number of MQTT server
MQTT_TOPIC = _setting("mqtt_topic", "solis/METRICS")            # MQTT topic to use
MQTT_USER = _setting("mqtt_user", "foo")                        # MQTT auth user
MQTT_PASS = _setting("mqtt_pass", "bar")                        # MQTT auth password
CHECK_INTERVAL = _setting("check_interval", 30, int)            # How often to check(seconds), only applies when 'PROMETHEUS = False' otherwise uses Prometheus scrape interval
MQTT_KEEPALIVE = _setting("mqtt_keepalive", 60, int)            # MQTT keepalive
PROMETHEUS = _setting("prometheus", False, _bool)               # Enable Prometheus exporter
PROMETHEUS_PORT = _setting("prometheus_port", 18000, int)       # Port to use for Prometheus exporter
MODIFIED_METRICS = _setting("modified_metrics", True, _bool)    # Enable modified metrics
DEBUG = _setting("debug", False, _bool)                         # Enable debugging, helpfull to diagnose problems
