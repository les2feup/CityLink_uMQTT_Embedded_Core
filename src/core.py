import json
import asyncio
from .const import *
from umqtt.simple import MQTTClient
from time import ticks_ms, ticks_diff, sleep_ms

def _with_exponential_backoff(func, retries, base_timeout_ms):
    for i in range(retries):
        retry_timeout = base_timeout_ms * (2**i)
        # TODO: suppress this print when building for production
        print(f"[INFO] Trying {func.__name__} (attempt {i + 1}/{retries})")
        try:
            return func()
        except Exception as e:
            print(
                f"[ERROR] {func.__name__} failed: {e}, retrying in {retry_timeout} milliseconds"
            )
            sleep_ms(retry_timeout)

    raise Exception(f"[ERROR] {func.__name__} failed after {retries} retries")


def main(setup=None):
    print("[MAIN] Starting main function...")
    otau = False
    try:
        open(OTAU_FILE, "r").close()
        otau = True
        print("[MAIN] OTA Update mode detected.")
    except OSError:
        print("[MAIN] No OTA Update mode detected.")
        pass

    core = EmbeddedCore(otau)
    if setup is None and not otau:
        print("[MAIN] [WARN] No setup function provided. Entering OTA Update mode.")
        core._otau_init()
        return

    core._init()
    core._connect()
    core._edge_con_register()
    core._setup_mqtt()

    async def main_task():
        topic = f"{core._base_property_topic}/core/status"
        if otau:
            core._publish(topic, "ADAPT", True, 1)
        else:
            setup(core)
            core._publish(topic, "OK", True, 1)

        while True:
            start_time = ticks_ms()
            core._listen()
            elapsed_time = ticks_diff(ticks_ms(), start_time)
            await core.task_sleep_ms(max(0, polling_interval_ms - elapsed_time))

    core._start_scheduler(main_task())


class EmbeddedCore:
    def __init__(self, otau):
        self._tasks = {}
        self._actions = {}
        self._properties = {}
        self._config = {}

        self._otau = otau
        self._mqtt_ready = False
        self.is_registered = False
        self.last_publish_time = 0

        try:
            with open("/config/config.json", "r") as f:
                self._config = json.load(f)
        except Exception as e:
            raise Exception(f"[FATAL] Failed to load configuration file: {e}") from e

        self._id = self._config.get("id")
 
    def _load_ext(self, fname):
        try:
            with open(fname, "r") as f:
                code = f.read()

            g = {}  # Global namespace for exec
            exec(code, g)
            ext_class = g.get("EmbeddedCoreExt")
            if not ext_class:
                return

            for name, member in ext_class.__dict__.items():
                if callable(member) and name != "_install":
                    bound_method = lambda self_inst=self, func=member: func(self_inst)
                    setattr(self, name, bound_method())

            install = g.get("_install")
            if install:
                install(self)

        except OSError as e:
            print(f"[ERROR] [_load_ext] Err opening/reading {fname}: {e}")
        except Exception as e:
            print(f"[ERROR] [_load_ext] Unexp. Err during: {e}")

    def _init(self):
        if self._id is None:
            from binascii import hexlify
            from machine import unique_id
            self._id = hexlify(unique_id()).decode("utf-8")
        else:
            self.is_registered = True

        if self._otau:
            self._actions = {
                "core/otau/write": self._vfs_write,
                "core/otau/delete": self._vfs_delete,
                "core/otau/finish": self._otau_finish,
            }
        else:
            self._actions = {
                "core/otau/init": self._otau_init,
            }
            for fname in os.listdir("/ext"):
                self._load_ext(f"/ext/{fname}")


    def _write_config(self, update_dict):
        self._config.update(update_dict)
        try:
            with open("./config/config.json", r) as f:
                json.dump(self._config, f)
        except Exception as e:
            raise Exception(f"[ERROR] Failed to write configuration file: {e}") from e

    def _connect(self):
        import network

        net_conf = self._config["network"]
        self.wlan = network.WLAN(network.STA_IF)
        self.wlan.active(True)
        self.wlan.connect(net_conf["ssid"], net_conf["password"])

        broker_conf = self._config["runtime"]["broker"]
        self._mqtt = MQTTClient(
            client_id=client_id,
            server=broker_conf["hostname"],
            port=broker_conf.get("port", DEFAULT_MQTT_PORT),
            user=broker_conf.get("username"),
            password=broker_conf.get("password"),
            keepalive=broker_conf.get("keepalive", DEFAULT_MQTT_KEEPALIVE),
            ssl=broker_conf.get("ssl"),
        )

        def check_wlan():
            if not self._wlan.isconnected():
                raise Exception(f"WiFi Connecting")

        retries = self._config["runtime"]["connection"].get("retries", 3)
        timeout_ms = self._config["runtime"]["connection"].get("timeout_ms", 5000)
        _with_exponential_backoff(check_wlan, retries, timeout_ms)
        _with_exponential_backoff(
            self._mqtt.connect(
                broker_conf.get("clean_session", DEFAULT_MQTT_CLEAN_SESSION),
                timeout_ms,
            ),
            retries,
            timeout_ms,
        )

    def _disconnect(self):
        self._mqtt_ready = False
        self._mqtt.disconnect()
        self._wlan.disconnect()

    def _publish(self, topic, payload, retain, qos):
        self._mqtt.publish(topic, payload, retain=retain, qos=qos)
        self.last_publish_time = ticks_ms()

    def _listen(self):
        self._mqtt.check_msg()

        if ticks_diff(ticks_ms(), self.last_publish_time) >= PING_TIMEOUT_MS:
            self._mqtt.ping()
            self.last_publish_time = ticks_ms()

    def _edge_con_register(self):
        if self.is_registered:
            return

        registration_payload = json.dumps(self._config["reg"])
        registering = False

        # TODO: handle better the schema validation
        def on_registration(topic, payload):
            nonlocal registering

            if self.is_registered or registering:
                return

            registering = True

            topic = topic.decode("utf-8")
            payload = json.loads(payload)

            if topic != f"citylink/{self._id}/registration/ack":
                registering = False
                return

            if payload["status"] != "success":
                registering = False
                return

            self._id = payload["id"]
            self._write_config({"id": self._id})
            self.is_registered = True

        self._mqtt.set_callback(on_registration)
        self._mqtt.subscribe(f"citylink/{self._id}/registration/ack", qos=1)

        time_passed = 0
        while not self.is_registered and not registering:
            if time_passed % REGISTRATION_PUBLISH_INTERVAL_MS == 0:
                self._publish(
                    f"citylink/{self._id}/registration",
                    registration_payload,
                    retain=False,
                    qos=1,
                )

            self._listen()
            sleep_ms(POLLING_INTERVAL_MS)
            time_passed += POLLING_INTERVAL_MS

    def _setup_mqtt(self):
        from ._setup import mqtt_setup

        base = f"citylink/{self._id}"
        self._base_event_topic = f"{base}/events"
        self._base_action_topic = f"{base}/actions"
        self._base_property_topic = f"{base}/properties"

        def on_message(topic, payload):
            topic = topic.decode("utf-8")
            if not topic.startswith(f"{self._base_action_topic}/"):
                return

            topic = topic[len(f"{self._base_action_topic}/") :]

            namespace, action_name = topic.split("/", 1)
            action_input = None
            if payload != b"":
                try:
                    action_input = json.loads(payload)
                except Exception as e:
                    print("Error in _on_message:", e)
                    return

            self._invoke_action(namespace, action_name, action_input)

        self.mqtt.set_callback(on_message)

        if self._otau:
            self.mqtt.subscribe(f"{self._base_action_topic}/core/otau/write", qos=2)
            self.mqtt.subscribe(f"{self._base_action_topic}/core/otau/delete", qos=2)
            self.mqtt.subscribe(f"{self._base_action_topic}/core/otau/finish", qos=1)
        else:
            self.mqtt.subscribe(f"{self._base_action_topic}/core/otau/init", qos=1)

        self._mqtt_ready = True

    def _invoke_action(self, action_name, action_input, **_):
        action = self._actions.get(action_name)
        if action:
            asyncio.create_task(action(action_input))

    async def _otau_init(self, _):
        if self._otau:
            return

        open(OTAU_FILE, "x").close()
        self._reset()

    async def _otau_finish(self, _):
        if not self._otau:
            return

        import os

        os.remove(OTAU_FILE)

        self._reset()

    def _wrap_vfs_action(action_func):
        from time import gmtime, time

        def builtin_action_wrapper(self, action_input):
            base = gmtime(0)[0]
            action_result = action_func(self, action_input)
            ts = time()
            epoch_ts = (
                {"time_base": base, "timestamp": ts}
                if base != 1970
                else {"timestamp": ts}
            )
            action_result.update({"epoch_timestamp": epoch_ts})
            self._publish(
                f"{self._base_event_topic}/core/otau/report",
                json.dumps(action_result),
                retain=False,
                qos=1,
            )

        return builtin_action_wrapper

    @_wrap_vfs_action
    async def _vfs_write(self, action_input):
        result = {"action": "write", "error": False, "message": ""}

        file_path = action_input.get("path")
        if file_path is None:
            result["error"] = True
            result["message"] = "Missing path in action input."
            return result

        payload = action_input.get("payload")
        if payload is None or not isinstance(payload, dict):
            result["error"] = True
            result["message"] = "Missing or invalid payload in action input."
            return result

        data = payload.get("data")
        data_hash = int(payload.get("hash"), 16)
        data_hash_algo = payload.get("algo")

        if any(map(lambda x: x is None, [data, data_hash, data_hash_algo])):
            result["error"] = True
            result["message"] = "Missing or invalid payload contents."
            return result

        append = action_input.get("append", False)

        try:
            from binascii import crc32, a2b_base64
            import os

            if data_hash_algo.lower() != "crc32":
                raise NotImplementedError("Only CRC32 is supported for now.")

            actual_hash = crc32(data)
            if actual_hash != data_hash:
                raise ValueError(
                    f"Hash mismatch, expected {hex(data_hash)}, got {hex(actual_hash)}"
                )

            mode = "a" if append else "w"

            file_path = file_path.rstrip("/")
            path_parts = file_path.split("/")
            file_name = path_parts[-1]

            root_dir = os.getcwd()
            file_root = path_parts[0]
            if len(path_parts) == 1 or file_root in ["", "."]:
                file_root = root_dir

            if file_root != root_dir:
                raise ValueError(
                    f"Invalid file path: '{file_path}' - expected root dir to be '{root_dir} but is '{file_root}'"
                )

            path_parts = path_parts[1:]  # Skip the root directory
            for part in path_parts[:-1]:
                if part not in os.listdir():
                    os.mkdir(part)
                os.chdir(part)

            with open(file_name, mode) as f:
                f.write(a2b_base64(data))

            os.chdir(root_dir)

            result["message"] = file_path
        except Exception as e:
            result["error"] = True
            result["message"] = str(e)

        return result

    @_wrap_vfs_action
    async def _vfs_delete(self, action_input):
        raise NotImplementedError(
            "Subclasses must implement builtin_action_vfs_delete()"
        )

    async def _reset(self, _):
        try:
            self._publish(f"{self._base_property_topic}/core/status", "UNDEF", True, 1)
            self._disconnect()
        except Exception as e:
            self.log(f"Failed to disconnect: {e}", LogLevels.ERROR)

        from machine import soft_reset

        soft_reset()

    ## TASK SCHEDULER INTERFACE ##
    def _start_scheduler(self, main_task):
        def loop_exception_handler(loop, context):
            future = context.get("future")
            msg = context.get("exception", context["message"])

            future_name = getattr(future, "__name__", "unknown")

            if isinstance(msg, asyncio.CancelledError):
                print(f"[INFO] Task {future_name} was cancelled.")
            else:
                print(f"[ERROR] Task {future_name} failed: {msg}")

            for task_id in self._tasks:
                if self._tasks[task_id].done():
                    del self._tasks[task_id]

        loop = asyncio.new_event_loop()
        loop.set_exception_handler(loop_exception_handler)
        loop.run_until_complete(loop.create_task(main_task))
