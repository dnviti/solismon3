# SolisMon3

## Configuration

Set these options in the Home Assistant app UI:

- `inverter_serial`: WiFi stick serial number.
- `inverter_ip`: IP address of the inverter WiFi stick.
- `inverter_port`: Modbus port on the inverter WiFi stick.
- `mqtt_server`: MQTT broker hostname or IP address. Use `core-mosquitto` for the official Mosquitto broker app.
- `mqtt_port`: MQTT broker port.
- `mqtt_topic`: MQTT topic used for the metrics JSON payload.
- `mqtt_user`: Optional MQTT username.
- `mqtt_pass`: Optional MQTT password.
- `check_interval`: Poll interval in seconds when Prometheus mode is disabled.
- `mqtt_keepalive`: MQTT keepalive in seconds.
- `prometheus`: Enable the Prometheus exporter.
- `modified_metrics`: Enable calculated convenience metrics.
- `debug`: Enable verbose logging.

When Prometheus is enabled, the app exposes port `18000`. Scrape `http://<home-assistant-host>:18000/`.
