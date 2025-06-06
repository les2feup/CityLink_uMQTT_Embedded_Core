import os
import json
import asyncio
from .const import *
from machine import unique_id
from umqtt.simple import MQTTClient
from binascii import crc32, a2b_base64, hexlify
from time import ticks_ms, ticks_diff, sleep_ms, gmtime, time


def _with_exponential_backoff(func, retries, base_timeout_ms):
    print(
        f"[INFO] Trying {func.__name__}: {retries} retries. {base_timeout_ms} ms base timeout."
    )
    for i in range(retries):
        retry_timeout = base_timeout_ms * (2**i)
        try:
            return func()
        except Exception as e:
            sleep_ms(retry_timeout)

    raise Exception(f"[ERROR] {func.__name__} failed after {retries} retries")


def main():
    print("[MAIN] Starting main function...")
    otau_mode = False
    try:
        open(OTAU_FILE, "r").close()
        print("[MAIN] OTAU flag detected. Entering adaptation mode.")
        otau_mode = True
    except Exception:
        pass

    core = EmbeddedCore(otau_mode)

    core._init()
    core._connect()
    core._edge_con_register()

    def run_setup(core):
        try:
            from main import setup

            setup(core)
        except Exception as e:
            print(f"[FATAL] Failed to run user setup function: {e}")
            open(OTAU_FILE, "x").close()
            raise Exception("Forcing OTA Update mode") from e

    async def main_task():
        core._setup_mqtt()

        topic = f"{core._base_property_topic}/core/status"
        if otau_mode:
            core._publish(topic, "OTAU", True, 1)
            print("[INFO] Core in adaptation mode. Waiting for OTAU actions.")
        else:
            run_setup(core)
            core._publish(topic, "APP", True, 1)
            print("[INFO] Setup finished. Core in APP mode.")

        while True:
            start_time = ticks_ms()
            core._listen()
            elapsed_time = ticks_diff(ticks_ms(), start_time)
            await asyncio.sleep_ms(max(0, POLLING_INTERVAL_MS - elapsed_time))

    core._start_scheduler(main_task())


class EmbeddedCore:
    def __init__(self, otau):
        self._tasks = {}
        self._actions = {}
        self._properties = {}
        self._config = {}

        self._otau = otau
        self._mqtt_ready = False
        self.last_publish_time = 0

        try:
            with open("/config/config.json", "r") as f:
                self._config = json.load(f)
        except Exception as e:
            raise Exception(f"[FATAL] Failed to load configuration file: {e}") from e

        self._id = self._config.get("id", hexlify(unique_id()).decode("utf-8"))

    def _load_ext(self, fname):
        ext_class = None
        module_name = None
        try:
            print(f"[INFO] Loading extension: {fname}")
            if fname.endswith(".py"):
                g = {}  # Global namespace for exec
                with open(fname, "rb") as f:
                    code = f.read()
                    exec(code, g)
                ext_class = g.get("EmbeddedCoreExt")

            elif fname.endswith(".mpy"):
                module_name = fname.replace(".mpy", "")
                module = __import__(module_name)
                ext_class = getattr(module, "EmbeddedCoreExt", None)

            if not ext_class:
                print(f"[ERROR] [_load_ext] No EmbeddedCoreExt class found in {fname}")
                return

            static_methods = getattr(ext_class, "__static__", None)

            for name, member in ext_class.__dict__.items():
                if callable(member):
                    if static_methods and name in static_methods:
                        setattr(EmbeddedCore, name, staticmethod(member))
                    else:
                        bound_method = (
                            lambda self_inst=self, func=member: lambda *a, **kw: func(
                                self_inst, *a, **kw
                            )
                        )
                        setattr(self, name, bound_method())

        except OSError as e:
            print(f"[ERROR] [_load_ext] Err opening/reading {fname}: {e}")
        except Exception as e:
            print(f"[ERROR] [_load_ext] Unexp. Err during: {e}")

    def _init(self):
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
            try:
                for fname in os.listdir("/citylink/ext"):
                    self._load_ext(f"/citylink/ext/{fname}")
            except Exception as e:
                raise Exception(f"[FATAL] Failed to load extensions: {e}") from e

    def _update_config(self, update_dict):
        self._config.update(update_dict)
        try:
            with open("./config/config.json", "w") as f:
                json.dump(self._config, f)
        except Exception as e:
            raise Exception(f"[ERROR] Failed to write configuration file: {e}") from e

    def _connect(self):
        import network

        net_conf = self._config["network"]
        self._wlan = network.WLAN(network.STA_IF)
        self._wlan.active(True)
        self._wlan.connect(net_conf["ssid"], net_conf["password"])

        broker_conf = self._config["runtime"]["broker"]
        self._mqtt = MQTTClient(
            client_id=self._id,
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
            else:
                print(f"[INFO] Connected to {self._wlan.ifconfig()[0]}")

        def mqtt_connect():
            self._mqtt.connect(
                broker_conf.get("clean_session", DEFAULT_MQTT_CLEAN_SESSION),
                timeout_ms,
            )
            print(f"[INFO] Connected to MQTT broker {broker_conf['hostname']}")

        retries = self._config["runtime"]["connection"].get("retries", 3)
        timeout_ms = self._config["runtime"]["connection"].get("timeout_ms", 5000)
        _with_exponential_backoff(check_wlan, retries, timeout_ms)
        _with_exponential_backoff(mqtt_connect, retries, timeout_ms)

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
        acked = False
        registered = False
        parsing_msg = False

        # TODO: handle better the schema validation
        def on_registration(topic, payload):
            nonlocal acked, parsing_msg, registered

            if registered or parsing_msg:
                return

            registering = True

            topic = topic.decode("utf-8")
            payload = json.loads(payload)

            if topic != f"citylink/{self._id}/registration/ack":
                registering = False
                return

            reg_status = payload.get("status", None)
            if reg_status == "ack":
                print("[INFO] Registration acknowledged by the broker.")
                acked = True

            elif reg_status == "success":
                reg_id = payload.get("id", None)

                if reg_id is not None:
                    self._id = reg_id
                    self._update_config({"id": self._id})

                registered = True
                print("[INFO] Registration successful.")

            elif reg_status == "error":
                print(
                    "[ERROR] Registration error:",
                    payload.get("message", "Unknown error"),
                )

            else:
                print("[ERROR] Malformed registration ack:", payload)

            parsing_msg = False

        self._mqtt.set_callback(on_registration)
        self._mqtt.subscribe(f"citylink/{self._id}/registration/ack", qos=1)

        print("[INFO] Initiating registration process.")
        time_passed = 0
        registration_payload = json.dumps(self._config["reg"])
        while not registered:
            if (
                time_passed % REGISTRATION_PUBLISH_INTERVAL_MS == 0
                and not parsing_msg
                and not acked
            ):
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
        base = f"citylink/{self._id}"
        self._base_event_topic = f"{base}/events"
        self._base_action_topic = f"{base}/actions"
        self._base_property_topic = f"{base}/properties"

        def on_message(topic, payload):
            if topic is None or payload is None:
                print("[WARN] Null MQTT topic or payload received.")
                return

            topic = topic.decode("utf-8")
            if not topic.startswith(f"{self._base_action_topic}/"):
                return

            action_name = topic[len(f"{self._base_action_topic}/") :]
            action_input = None
            if payload != b"":
                try:
                    action_input = json.loads(payload)
                except Exception as e:
                    print("[ERROR] json loads failed:", e)
                    return

            action = self._actions.get(action_name)
            try:
                if action and action_name.startswith("core"):
                    asyncio.create_task(action(action_input))
                elif action and action_name.startswith("app"):
                    asyncio.create_task(action(self, action_input))
                else:
                    print(f"[ERROR] Action '{action_name}' not found.")
            except Exception as e:
                print(f"[ERROR] Error executing action '{action_name}': {e}")

        self._mqtt.set_callback(on_message)

        if self._otau:
            self._mqtt.subscribe(f"{self._base_action_topic}/core/otau/write", qos=1)
            self._mqtt.subscribe(f"{self._base_action_topic}/core/otau/delete", qos=1)
            self._mqtt.subscribe(f"{self._base_action_topic}/core/otau/finish", qos=1)
        else:
            self._mqtt.subscribe(f"{self._base_action_topic}/core/otau/init", qos=1)

        self._mqtt_ready = True
        print("[INFO] MQTT setup complete.")

    async def _otau_init(self, *_):
        if self._otau:
            return

        open(OTAU_FILE, "x").close()
        self._reset()

    async def _otau_finish(self, *_):
        if not self._otau:
            return

        os.remove(OTAU_FILE)
        self._reset()

    def _wrap_vfs_action(action_func):
        async def wrapper(self, action_input):
            base = gmtime(0)[0]
            output = {
                "result": await action_func(self, action_input),
                "timestamp": (
                    {"epoch_year": base, "seconds": time()}
                    if base != 1970
                    else {"seconds": time()}
                ),
            }
            self._publish(
                f"{self._base_event_topic}/core/otau/report",
                json.dumps(output),
                retain=False,
                qos=1,
            )

        return wrapper

    @_wrap_vfs_action
    async def _vfs_write(self, action_input):
        result = {}
        try:
            file_path = action_input["path"].strip("/")
            payload = action_input["payload"]

            algo = payload["algo"]
            if algo != "crc32":
                raise NotImplementedError("Only CRC32 is supported.")

            data = payload["data"]
            expected_hash = int(payload["hash"], 16)
            if crc32(data) != expected_hash:
                raise ValueError("CRC32 mismatch")

            mode = "a" if action_input.get("append") else "w"
            parts = file_path.split("/")
            dir_parts, file_name = parts[:-1], parts[-1]
            if ".." in dir_parts or file_path.startswith("/"):
                raise ValueError("Unsafe file path")

            for part in dir_parts:
                if part not in os.listdir():
                    os.mkdir(part)
                os.chdir(part)

            with open(file_name, mode) as f:
                f.write(a2b_base64(data))

            os.chdir("/")  # Reset to root for safety
            result["written"] = file_path

        except Exception as e:
            result.update({"error": True, "message": str(e)})

        return result

    @_wrap_vfs_action
    async def _vfs_delete(self, action_input):
        path = action_input.get("path", "").strip("/")
        recursive = action_input.get("recursive", False)
        if recursive:
            return {"error": True, "message": "Recursive delete not implemented"}

        if not path or path == "": 
            return {"error": True, "message": "Path not specified"}

        try:
            os.remove(path)
            return {"deleted": [path]}
        except OSError as e:
            return {"error": True, "message": str(e)}

    def _reset(self):
        if self._mqtt_ready:
            try:
                self._publish(
                    f"{self._base_property_topic}/core/status", "UNDEF", True, 1
                )
                self._disconnect()
            except Exception as e:
                pass

        from machine import soft_reset

        print("[INFO] Issuing soft reset...")
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
