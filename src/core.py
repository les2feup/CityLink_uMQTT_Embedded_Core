import os
import json
import asyncio
from machine import unique_id
from umqtt.simple import MQTTClient
from binascii import crc32, a2b_base64, hexlify
from time import ticks_ms, ticks_diff, sleep_ms, gmtime, time
from micropython import const


PING_TIMEOUT_MS = const(60000)
REGISTRATION_PUBLISH_INTERVAL_MS = const(10000)  # 10 seconds
POLLING_INTERVAL_MS = const(500)  # 0.5 seconds

DEFAULT_MQTT_PORT = const(1883)
DEFAULT_MQTT_KEEPALIVE = const(120)
DEFAULT_MQTT_CLEAN_SESSION = const(True)

ADAPT_FLAG = const("adapt.flag")
ADAPT_FILE = const("/adapt.flag")


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

    files = os.listdir()
    main_exists = "main.py" in files or "main.mpy" in files
    adapt_mode = ADAPT_FLAG in files or ADAPT_FILE in files
    if not adapt_mode and not main_exists:
        open(ADAPT_FILE, "x").close()
        adapt_mode = True

    print(f"[MAIN] ADAPT mode: {adapt_mode}")

    core = EmbeddedCore(adapt_mode)

    core._connect()
    core._edge_con_register()

    def load_app(core):
        if core._adapt:
            print("[INFO] Core in adaptation mode, skipping application load.")
            return

        try:
            from main import setup

            setup(core)
        except Exception as e:
            print(f"[ERROR] Failed to run user setup function: {e}")
            # File should never exists at this point, but if it does, and open throws
            # it achieves the same effect as the forced exception below.
            # There is some loss of information if it does, but it is not critical.
            open(ADAPT_FILE, "x").close()
            raise Exception(
                "[ERROR] Setup failed, forcing reset into adaptation mode."
            ) from e

    async def main_task():

        core._setup_op_mode()
        core._setup_mqtt()

        load_app(core)

        topic = f"{core._base_property_topic}/core/status"
        core._publish(topic, "ADAPT" if adapt_mode else "APP", True, 1)
        print(f"[INFO] core._publish: {topic} -> {'ADAPT' if adapt_mode else 'APP'}")

        while True:
            start_time = ticks_ms()
            core._listen()
            elapsed_time = ticks_diff(ticks_ms(), start_time)
            await asyncio.sleep_ms(max(0, POLLING_INTERVAL_MS - elapsed_time))

    core._start_scheduler(main_task())


class EmbeddedCore:
    def __init__(self, adapt_mode=False):
        self._tasks = {}
        self._actions = {}
        self._properties = {}
        self._config = {}

        self._adapt = adapt_mode
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

    def _setup_op_mode(self):
        if self._adapt:
            self._actions = {
                "core/adapt/write": self._vfs_write,
                "core/adapt/delete": self._vfs_delete,
                "core/adapt/finish": self._adapt_finish,
            }

            self._pendings_writes = {}  # final_path: tmp:path
            self._pendings_deletes = []  # [path_to_delete]
        else:
            self._actions = {
                "core/adapt/init": self._adapt_init,
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

        if self._adapt:
            self._mqtt.subscribe(f"{self._base_action_topic}/core/adapt/write", qos=1)
            self._mqtt.subscribe(f"{self._base_action_topic}/core/adapt/delete", qos=1)
            self._mqtt.subscribe(f"{self._base_action_topic}/core/adapt/finish", qos=1)
        else:
            self._mqtt.subscribe(f"{self._base_action_topic}/core/adapt/init", qos=1)

        self._mqtt_ready = True
        print("[INFO] MQTT setup complete.")

    async def _adapt_init(self, new_tm_url):
        if self._adapt:
            return

        if not new_tm_url:
            raise ValueError("New TM URL must be provided for adaptation mode.")

        self._update_config({"reg": {"tm": new_tm_url}})

        open(ADAPT_FILE, "x").close()
        self._reset()

    def _commit_pending_writes(self):
        if not self._pendings_writes:
            return

        for final_path, temp_path in self._pendings_writes.items():
            try:
                if temp_path.startswith("a:"):
                    with open(temp_path, "r") as f:
                        append_data = f.read()
                        with open(final_path, "a") as final_f:
                            final_f.write(append_data)
                else:
                    os.rename(temp_path, final_path)
                print(f"[INFO] Committed write: {final_path}")
            except OSError as e:
                print(f"[ERROR] Failed to commit write for {final_path}: {e}")

        self._pendings_writes.clear()

    def _commit_pending_deletes(self):
        if not self._pendings_deletes:
            return

        for path in self._pendings_deletes:
            try:
                os.remove(path)
                print(f"[INFO] Committed delete: {path}")
            except OSError as e:
                print(f"[ERROR] Failed to commit delete for {path}: {e}")

    def _abort_changes(self):
        self._pendings_deletes.clear()
        if not self._pendings_writes:
            return

        for _, temp_path in self._pendings_writes.items():
            try:
                os.remove(temp_path)
                print(f"[INFO] Discarded back write: {temp_path}")
            except OSError as e:
                print(f"[ERROR] Failed to discard write for {temp_path}: {e}")

    async def _adapt_finish(self, commit):
        if not self._adapt:
            return

        if commit:
            self._commit_pending_deletes()
            self._commit_pending_writes()
        else:
            self._abort_changes()

        os.remove(ADAPT_FILE)
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
                f"{self._base_event_topic}/core/adapt/report",
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

            parts = file_path.split("/")
            dir_parts, file_name = parts[:-1], parts[-1]
            if ".." in dir_parts or file_path.startswith("/"):
                raise ValueError("Unsafe file path")

            for part in dir_parts:
                if part not in os.listdir():
                    os.mkdir(part)
                os.chdir(part)

            mode = "a" if action_input.get("append") else "w"
            if mode == "a" and file_name not in os.listdir():
                raise FileNotFoundError(
                    f"File {file_name} does not exist for appending."
                )
            if mode == "w" and file_name in os.listdir():
                raise FileExistsError(f"File {file_name} already exists for writing.")

            temp_name = f"{mode}:tmp_{file_name}"
            temp_path = f"{file_path}/{temp_name}"
            self._pendings_writes[file_path] = temp_path

            with open(temp_path, mode) as f:
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
            with open(path, "r") as f:  # Check if the file exists
                self._pendings_deletes.append(path)
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
