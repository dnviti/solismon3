# ver. 0.0.29
import config.config as config
import config.registers as registers
import logging
import signal
import threading
import paho.mqtt.client as mqtt
from json import dumps
from time import strptime, mktime, sleep
from prometheus_client import start_http_server
from prometheus_client.core import GaugeMetricFamily, REGISTRY
from pysolarmanv5.pysolarmanv5 import PySolarmanV5

# Last successfully scraped snapshot. Only ever replaced atomically (rebound to a
# fully-populated new dict) so readers always see a complete, consistent snapshot
# and a failed cycle keeps the previous good values instead of crashing.
metrics_dict = {}
debug = 0

# Smallest retry delay (seconds) used by the background scraper's exponential
# backoff. The delay doubles after each failure but is capped at CHECK_INTERVAL.
_BASE_BACKOFF = 5

# Register names whose raw values feed the modified-metrics calculations.
_CUSTOM_KEYS = (
    'battery_power_2',
    'battery_current_direction',
    'meter_active_power_1',
    'meter_active_power_2',
    'house_load_power',
    'total_dc_input_power_2',
    'bypass_load_power',
)


def _check_interval():
    """CHECK_INTERVAL clamped to a sane minimum so a misconfigured 0/negative
    value can never turn the scraper into a busy loop."""
    try:
        return max(1, int(config.CHECK_INTERVAL))
    except (TypeError, ValueError):
        return 30


def _close_modbus(modbus):
    """Best-effort teardown of a PySolarmanV5 instance. Never raises.

    A fresh connection is opened for every scrape; without this the underlying
    TCP socket (and the library's background reader thread) would leak on every
    cycle and eventually exhaust file descriptors / threads.
    """
    if modbus is None:
        return
    for method_name in ('disconnect', 'close'):
        method = getattr(modbus, method_name, None)
        if callable(method):
            try:
                method()
            except Exception:
                pass
    sock = getattr(modbus, 'sock', None)
    if sock is not None:
        try:
            sock.close()
        except Exception:
            pass


def add_modified_metrics(metrics, custom_metrics_dict):
    required = (
        'meter_active_power_1', 'meter_active_power_2', 'house_load_power',
        'bypass_load_power', 'battery_current_direction', 'battery_power_2',
        'total_dc_input_power_2',
    )
    missing = [k for k in required if k not in custom_metrics_dict]
    if missing:
        logging.warning(f'Skipping modified metrics, missing inputs: {missing}')
        return

    met_pwr = custom_metrics_dict['meter_active_power_1'] - custom_metrics_dict['meter_active_power_2']
    total_load = custom_metrics_dict['house_load_power'] + custom_metrics_dict['bypass_load_power']

    # Present battery modified metrics
    if custom_metrics_dict['battery_current_direction'] == 0:
        metrics['battery_power_modified'] = 'Battery Power(modified)', custom_metrics_dict['battery_power_2']
        metrics['battery_power_in_modified'] = 'Battery Power In(modified)', custom_metrics_dict['battery_power_2']
        metrics['battery_power_out_modified'] = 'Battery Power Out(modified)', 0
        metrics['grid_to_battery_power_in_modified'] = 'Grid to Battery Power In(modified)', 0
    else:
        metrics['battery_power_modified'] = 'Battery Power(modified)', custom_metrics_dict['battery_power_2'] * -1  # negative
        metrics['battery_power_out_modified'] = 'Battery Power Out(modified)', custom_metrics_dict['battery_power_2']
        metrics['battery_power_in_modified'] = 'Battery Power In(modified)', 0
        metrics['grid_to_battery_power_in_modified'] = 'Grid to Battery Power In(modified)', 0

    if total_load < met_pwr and custom_metrics_dict['battery_power_2'] > 0:
        metrics['grid_to_battery_power_in_modified'] = 'Grid to Battery Power In(modified)', custom_metrics_dict['battery_power_2']

    # Present meter modified metrics
    if met_pwr > 0:
        metrics['meter_power_in_modified'] = 'Meter Power In(modified)', met_pwr
        metrics['meter_power_modified'] = 'Meter Power(modified)', met_pwr
        metrics['meter_power_out_modified'] = 'Meter Power Out(modified)', 0
    else:
        metrics['meter_power_out_modified'] = 'Meter Power Out(modified)', met_pwr * -1  # negative
        metrics['meter_power_in_modified'] = 'Meter Power In(modified)', 0
        metrics['meter_power_modified'] = 'Meter Power(modified)', met_pwr

    # Present load modified metrics
    metrics['total_load_power_modified'] = 'Total Load Power(modified)', total_load

    if 0 < custom_metrics_dict['total_dc_input_power_2'] <= total_load:
        metrics['solar_to_house_power_modified'] = 'Solar To House Power(modified)', custom_metrics_dict['total_dc_input_power_2']
    elif custom_metrics_dict['total_dc_input_power_2'] == 0:
        metrics['solar_to_house_power_modified'] = 'Solar To House Power(modified)', 0
    elif custom_metrics_dict['total_dc_input_power_2'] > total_load:
        metrics['solar_to_house_power_modified'] = 'Solar To House Power(modified)', total_load

    logging.info('Added modified metrics')


def scrape_solis(debug):
    """Scrape the inverter once.

    On success, atomically publishes a fresh snapshot to the global
    ``metrics_dict`` and returns True. On any recoverable failure it logs the
    cause, leaves the previous snapshot untouched, and returns False. It never
    terminates the process and always tears down its Modbus connection.
    """
    global metrics_dict
    local_metrics = {}
    custom_metrics_dict = {}
    regs_ignored = 0
    modbus = None

    try:
        try:
            logging.info('Connecting to Solis Modbus')
            modbus = PySolarmanV5(
                config.INVERTER_IP, config.INVERTER_SERIAL,
                port=config.INVERTER_PORT, mb_slave_id=1, verbose=debug)
        except Exception as e:
            logging.error(f'Could not connect to Solis Modbus: {repr(e)}. Skipping this cycle')
            return False

        logging.info('Scraping...')

        for r in registers.all_regs:
            reg = r[0]
            reg_des = r[1]
            reg_len = len(reg_des)

            # Sometimes the query fails; retry a few times before skipping the
            # whole cycle. We never exit the process or consume a partial read.
            regs = None
            c = 0
            while True:
                try:
                    logging.debug(f'Scrapping registers {reg} length {reg_len}')
                    # read registers at address, store result in regs list
                    regs = modbus.read_input_registers(register_addr=reg, quantity=reg_len)
                    logging.debug(regs)
                    break
                except Exception as e:
                    c += 1
                    if c > 3:
                        logging.error(f'Cannot read registers {reg} length {reg_len}. Tried {c} times. Skipping this cycle {repr(e)}')
                        return False
                    logging.error(f'Cannot read registers {reg} length {reg_len} {repr(e)}')
                    logging.error(f'Retry {c} in 3s')
                    sleep(3)  # hold before retry

            # Convert time to epoch
            if reg == 33022:
                try:
                    if len(regs) >= 6:
                        inv_time = '20{:02d}-{:02d}-{:02d} {:02d}:{:02d}:{:02d}'.format(
                            regs[0], regs[1], regs[2], regs[3], regs[4], regs[5])
                        logging.info(f'Solis Inverter time: {inv_time}')
                        time_tuple = strptime(inv_time, '%Y-%m-%d %H:%M:%S')
                        time_epoch = mktime(time_tuple)
                        local_metrics['system_epoch'] = 'System Epoch Time', time_epoch
                    else:
                        logging.warning(f'Register 33022 returned {len(regs)} values (expected >= 6); skipping epoch')
                except (ValueError, TypeError, OverflowError, IndexError, OSError) as e:
                    logging.warning(f'Could not parse inverter time: {repr(e)}')

            # Add metric to list
            for (i, item) in enumerate(regs):
                # Device may return more values than the register map describes;
                # skip the extras instead of raising IndexError.
                if i >= len(reg_des):
                    regs_ignored += 1
                    continue

                name = reg_des[i][0]
                if '*' in name:
                    regs_ignored += 1
                    continue

                local_metrics[name] = reg_des[i][1], item

                # Capture raw values needed for the modified-metric calculations
                if name in _CUSTOM_KEYS:
                    custom_metrics_dict[name] = item

        logging.info(f'Ignored registers: {regs_ignored}')

        # Create modified metrics
        if config.MODIFIED_METRICS:
            try:
                add_modified_metrics(local_metrics, custom_metrics_dict)
            except Exception as e:
                logging.error(f'Could not compute modified metrics: {repr(e)}')

        # Publish the complete snapshot in a single atomic rebind
        metrics_dict = local_metrics
        logging.info('Scraped')
        return True
    finally:
        _close_modbus(modbus)


def publish_mqtt():
    """Publish the latest snapshot to MQTT. Never raises, never exits."""
    mqtt_dict = {}
    try:
        snapshot = metrics_dict
        if not snapshot:
            logging.info('No metrics available to publish to MQTT')
            return

        # Resize dictionary and convert to JSON
        for metric, value in snapshot.items():
            mqtt_dict[metric] = value[1]
        mqtt_json = dumps(mqtt_dict)

        mqttc = mqtt.Client()
        if config.MQTT_USER != '':
            mqttc.username_pw_set(config.MQTT_USER, config.MQTT_PASS)

        try:
            mqttc.connect(config.MQTT_SERVER, config.MQTT_PORT, config.MQTT_KEEPALIVE)
            logging.info(f'Connected to MQTT {config.MQTT_SERVER}:{config.MQTT_PORT}')
            logging.info('Publishing MQTT')
            mqttc.publish(topic=config.MQTT_TOPIC, payload=mqtt_json)
        finally:
            # Always release the client socket, even if connect/publish failed.
            try:
                mqttc.disconnect()
            except Exception:
                pass

    except Exception as e:
        logging.error(f'Could not publish to MQTT {repr(e)}')


def scrape_loop(stop_event):
    """Background worker: scrape (and publish) forever until stopped.

    On a successful cycle it waits CHECK_INTERVAL before the next scrape. On any
    failure it retries with exponential backoff capped at CHECK_INTERVAL, so a
    transient error recovers quickly while a long outage keeps retrying forever
    without hammering the inverter or hanging. Any unexpected error is logged
    and the loop simply continues, so this thread can never die.
    """
    interval = _check_interval()
    base_backoff = max(1, min(_BASE_BACKOFF, interval))
    backoff = base_backoff

    while not stop_event.is_set():
        ok = False
        try:
            ok = scrape_solis(debug)
            if ok:
                publish_mqtt()
        except Exception as e:
            logging.error(f'Scrape/publish cycle failed: {repr(e)}')

        if ok:
            backoff = base_backoff
            stop_event.wait(interval)
        else:
            logging.warning(f'Cycle unsuccessful, retrying in {backoff}s')
            stop_event.wait(backoff)
            backoff = min(backoff * 2, interval)


class CustomCollector(object):
    def __init__(self):
        pass

    def collect(self):
        # Called on a Prometheus HTTP worker thread per /metrics request. It only
        # serves the latest snapshot produced by the background scraper, so it is
        # instant and never blocks, scrapes, or raises into the exporter.
        for metric, value in list(metrics_dict.items()):
            try:
                yield GaugeMetricFamily(metric, value[0], value=value[1])
            except Exception as e:
                logging.error(f'Could not export metric {metric}: {repr(e)}')


def _install_signal_handlers():
    """Make SIGTERM/SIGINT raise KeyboardInterrupt so shutdown is a clean,
    in-process unwind. Docker/Home Assistant stop the add-on with SIGTERM,
    whose default disposition would otherwise kill the process with no chance
    to log. Best-effort: ignores environments where signals can't be set."""
    def _shutdown(signum, frame):
        raise KeyboardInterrupt()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _shutdown)
        except (ValueError, OSError):
            pass


if __name__ == '__main__':
    if config.DEBUG:
        logging.basicConfig(format='%(asctime)s %(levelname)-8s %(message)s', level=logging.DEBUG,
                            datefmt='%Y-%m-%d %H:%M:%S')
        debug = 1
    else:
        logging.basicConfig(format='%(asctime)s %(levelname)-8s %(message)s', level=logging.INFO,
                            datefmt='%Y-%m-%d %H:%M:%S')
        debug = 0

    _install_signal_handlers()

    logging.info('Starting')

    stop_event = threading.Event()

    try:
        if config.PROMETHEUS:
            # Bring the exporter up resiliently: a transient bind failure
            # (port in use) is retried rather than killing the service.
            while True:
                try:
                    logging.info(f'Starting Web Server for Prometheus on port: {config.PROMETHEUS_PORT}')
                    start_http_server(config.PROMETHEUS_PORT)
                    break
                except Exception as e:
                    logging.error(f'Could not start Prometheus web server on port {config.PROMETHEUS_PORT}: {repr(e)}. Retrying in {_check_interval()}s')
                    sleep(_check_interval())

            try:
                REGISTRY.register(CustomCollector())
            except Exception as e:
                logging.error(f'Could not register Prometheus collector: {repr(e)}')

        # Single background scraper drives both modes: it keeps metrics_dict
        # fresh (served by the Prometheus collector) and publishes to MQTT.
        worker = threading.Thread(target=scrape_loop, args=(stop_event,), name='solis-scraper', daemon=True)
        worker.start()

        # Main thread idles until an operator signal triggers shutdown.
        while True:
            stop_event.wait(3600)

    except KeyboardInterrupt:
        logging.info('Shutting down')
    finally:
        stop_event.set()
