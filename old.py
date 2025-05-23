def with_exponential_backoff(func, retries, base_timeout_ms):
    # Attempts to execute a function with exponential backoff on failure.

    # This function calls the provided callable, retrying up to the specified number of times.
    # After each failure, it waits for an exponentially increasing delay starting at base_timeout_ms
    # milliseconds. If the callable succeeds, its result is returned immediately; if all attempts fail,
    # an Exception is raised.

    # Args:
    #    func: The callable to execute.
    #    retries: The maximum number of retry attempts.
    #    base_timeout_ms: The initial wait time in milliseconds, which doubles after each attempt.

    # Returns:
    #    The result of the callable if execution is successful.

    # Raises:
    #    Exception: If the callable fails on all retry attempts.

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


class EmbeddedCore:
    # Micropython implementation of the CityLink EmbeddedCore interface
    # using MQTT over a WLAN.

    def _load_ext(self, fname, **kwargs):
        # Load an external module.

        # Args:
        #    fname: Name of the external module to load
        #    **kwargs: Additional keyword arguments (ignored)
        try:
            with open(fname, "r") as f:
                code = f.read()

            g = {}  # Global namespace for exec
            exec(code, g)
            ext_class = g.get("EmbeddedCoreExt")
            if not ext_class:
                return

            for name, member in ext_class.__dict__.items():
                if callable(member) and name != "__init__":
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

    def __init__(
        self,
        otau=False,
        **kwargs,
    ):
        # Initialize the uMQTTCore with configuration and serialization parameters.

        # Args:
        #    config_dir: Directory containing configuration files
        #    fopen_mode: File open mode (default: "r")
        #    serializer: Object with dumps/loads methods for serialization (default: json)
        #    **_: Additional keyword arguments (ignored)

        # self._tasks = {}
        # self._actions = {}
        # self._properties = {}
        # self._net_ro_props = set()

        # self._builtin_actions = {
        #     "vfs/list": self._builtin_action_vfs_list,
        #     "vfs/read": self._builtin_action_vfs_read,
        #     "vfs/write": self._builtin_action_vfs_write,
        #     "vfs/delete": self._builtin_action_vfs_delete,
        #     "reload": self._builtin_action_reload_core,
        #     "set_property": self._builtin_action_set_property,
        # }
        self._otau = otau
        self._mqtt_ready = False
        self.is_registered = False
        self.last_publish_time = 0

        # Load configuration
        try:
            with open("./config/config.json", "r") as f:
                self.config = json.load(f)
        except Exception as e:
            raise Exception(f"[FATAL] Failed to load configuration file: {e}") from e

        self.id = self.config.get("id")
        if self.id is None:
            from machine import unique_id
            from binascii import hexlify

            # Id to use as part of the MQTT topics and client ID
            self._id = hexlify(unique_id()).decode("utf-8")
        else:
            self.is_registered = True

        # Load extentions
        self._actions = {}
        if self._otau:
            self._actions = {
                "core/otau/write": self._vfs_write,
                "core/otau/delete": self._vfs_delete,
                "core/otau/finish": self._otau_finish,
            }
        else:
            # if not in OTA Update mode, load existing extensions
            self._actions = {
                "core/otau/init": self._otau_init,
            }

            # If not in OTA Update mode, load existing extensions
            for fname in os.listdir("./ext"):
                self._load_ext(f"./ext/{fname}", **kwargs)

    # NOTE: optimizing for size. supressing serializer interface
    def _write_config(self, update_dict):
        # Write the configuration to the specified directory.

        # Updates the current configuration with provided dictionary
        # and persists it to storage.

        # Args:
        #    update_dict: Dictionary containing configuration updates

        # Interface: EmbeddedCore
        self._config.update(update_dict)
        try:
            with open("./config/config.json", r) as f:
                json.dump(self.config, f)
        except Exception as e:
            raise Exception(f"[ERROR] Failed to write configuration file: {e}") from e

    def _connect(self):
        # Attempt to connect to the Edge Connector.
        # In this concrete implementation, this function connects to the WLAN network
        # and then to the MQTT Broker.

        # Establishes connections to the configured WiFi network and MQTT broker
        # with exponential backoff for retries.

        # Interface: EmbeddedCore

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
        with_exponential_backoff(check_wlan, retries, timeout_ms)
        with_exponential_backoff(
            self._mqtt.connect(
                broker_conf.get("clean_session", DEFAULT_MQTT_CLEAN_SESSION),
                timeout_ms,
            ),
            retries,
            timeout_ms,
        )

    def _disconnect(self):
        # Disconnect from the network and exit the runtime.

        # Terminates MQTT broker connection and disconnects from WiFi network.

        # Interface: EmbeddedCore
        self._mqtt_ready = False
        self._mqtt.disconnect()
        self._wlan.disconnect()

    def _publish(self, topic, payload, retain, qos):
        # Publish a message to the MQTT broker.

        # Args:
        #    topic: MQTT topic to publish to
        #    payload: Message payload to send
        #    retain: MQTT retain flag (default: False)
        #    qos: MQTT QoS level (default: 0)
        self._mqtt.publish(topic, payload, retain=retain, qos=qos)
        self.last_publish_time = ticks_ms()

    def _listen(self):
        # Listen and handle incoming requests.

        # Checks for new MQTT messages and processes them.

        # Args:
        #    *_: Variable arguments (ignored)

        # Interface: EmbeddedCore
        self._mqtt.check_msg()

        if ticks_diff(ticks_ms(), self.last_publish_time) >= PING_TIMEOUT_MS:
            self._mqtt.ping()
            self.last_publish_time = ticks_ms()

    # TODO: simplify logic as much as possible
    #      suppress unnecessary logging
    def _edge_con_register(self):
        # Register the device with the Edge Connector

        # Sends registration request to the WoT servient and waits for confirmation.
        # If already registered, skips the registration process.

        # Interface: EmbeddedCore

        if self.is_registered:
            # self.log(f"Device already registered with ID: {self._id}", LogLevels.INFO)
            return

        registration_payload = json.dumps(self._config["reg"])
        registering = False

        def on_registration(topic, payload):
            nonlocal registering

            if self.is_registered or registering:
                return

            registering = True

            topic = topic.decode("utf-8")
            payload = json.loads(payload)

            if topic != f"citylink/{self._id}/registration/ack":
                # # self.log(f"Unexpected registration topic: {topic}", LogLevels.WARN)
                registering = False
                return

            # //TODO: handle better the schema validation
            if payload["status"] != "success":
                # # self.log(
                #     f"Failed to register device: {payload['message']}", LogLevels.ERROR
                # )
                registering = False
                return

            self._id = payload["id"]
            # # self.log(
            #     f"Device registered successfully with ID: {self._id}", LogLevels.INFO
            # )
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

    # TODO: only OTA stuff. move the rest to a separate file
    def _setup_mqtt(self):
        # Set up MQTT topic structure and message handling.

        # Configures MQTT callbacks and topic subscriptions for action handling.
        from ._setup import mqtt_setup

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
                    # # self.log(f"Failed to deserialize payload: {e}", LogLevels.ERROR)
                    return

            self._invoke_action(namespace, action_name, action_input)

        self.mqtt.set_callback(on_message)

        base = f"citylink/{self._id}"
        self._base_event_topic = f"{base}/events"
        self._base_action_topic = f"{base}/actions"
        self._base_property_topic = f"{base}/properties"

        if self._otau:
            self.mqtt.subscribe(f"{self._base_action_topic}/core/otau/write", qos=2)
            self.mqtt.subscribe(f"{self._base_action_topic}/core/otau/delete", qos=2)
            self.mqtt.subscribe(f"{self._base_action_topic}/core/otau/finish", qos=1)
        else:
            self.mqtt.subscribe(f"{self._base_action_topic}/core/otau/init", qos=1)

        self._mqtt_ready = True

    def App(polling_interval_ms=POLLING_INTERVAL_MS, **kwargs):
        # Decorator to mark the entry point for the EmbeddedCore.

        # Creates a uMQTTCore instance, loads configuration, and sets up
        # the runtime environment.

        # Args:
        #    polling_interval: Interval for message polling in milliseconds (default: 250)
        #    config_dir: Directory containing configuration files (default: "./config")
        #    **kwargs: Additional arguments passed to uMQTTCore uctor

        # Returns:
        #    Function decorator that wraps the main application entry point

        # Interface: EmbeddedCore

        # check if oau-init file exits
        otau = False
        try:
            with open("/otau-init.txt", "r") as f:
                otau = True
        except OSError:
            pass

        core = EmbeddedCore(otau, **kwargs)

        def main_decorator(main_func):
            # Decorates the main function of the EmbeddedCore.

            # Args:
            #    main_func: The main function to be decorated

            # Returns:
            #    Wrapped function that initializes the runtime

            def main_wrapper():
                # Wrapper for the main function of the EmbeddedCore.

                # Handles connection, registration, MQTT setup, and scheduler launch.
                core._connect()
                core._edge_con_register()
                core._setup_mqtt()

                async def main_task():
                    topic = f"{core._base_property_topic}/core/status"
                    if otau:
                        core._publish(topic, "ADAPT", True, 1)
                    else:
                        main_func(core)
                        core._publish(topic, "OK", True, 1)

                    while True:
                        start_time = ticks_ms()
                        core._listen()
                        elapsed_time = ticks_diff(ticks_ms(), start_time)
                        await core.task_sleep_ms(
                            max(0, polling_interval_ms - elapsed_time)
                        )

                core._start_scheduler(main_task())

            return main_wrapper

        return main_decorator

    def _invoke_action(self, action_name, action_input, **_):
        # Invoke an action with the specified input.

        # Args:
        #    namespace: Namespace for the action
        #    action_name: Name of the action to invoke
        #    action_input: Input data for the action
        #    **_: Additional keyword arguments (ignored)

        # Raises:
        #    ValueError: If action handler does not exist

        # Interface: AffordanceHandler

        action = self._actions.get(action_name)
        if action:
            asyncio.create_task(action(action_input))
        # else:
        # self.log(
        #     f"Action handler for {namespace}/{action_name} does not exist.",
        #     LogLevels.WARN,
        # )

    async def _otau_init(self, _):
        # Initialize the OTA update process.

        # Args:
        #    action_input: Input data for the OTA update

        # Returns:
        #    Dictionary with action result and error status

        # Interface: AffordanceHandler
        if self._otau:
            return

        open("/otau-init.txt", "x").close()
        self._reset()

    async def _otau_finish(self, _):
        if not self._otau:
            return

        # Finish the OTA update process, delete the update file, and reset the device.
        import os

        os.remove("/otau-init.txt")

        self._reset()

    def _wrap_vfs_action(action_func):
        # Decorator for VFS action handlers.

        # Wraps VFS actions to add timestamps and publish results as events.

        # Args:
        #    action_func: VFS action function to wrap

        # Returns:
        #    Wrapped function that publishes results
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
        # Write data to a file in the virtual file system.

        # Args:
        #    action_input: Dictionary containing path, payload, and append flag

        # Returns:
        #    Dictionary with action result and error status

        # Interface: AffordanceHandler
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
        # Delete a file from the virtual file system.

        # Args:
        #    action_input: Dictionary containing the file path to delete

        # Returns:
        #    Dictionary with deletion result

        # Raises:
        #    NotImplementedError: Must be implemented by subclasses

        # Interface: AffordanceHandler
        raise NotImplementedError(
            "Subclasses must implement builtin_action_vfs_delete()"
        )

    async def _reset(self, _):
        # Soft Reset the device.

        # Disconnects from network services and performs a soft reset of the device.

        # Args:
        #    _: Input parameter (ignored)

        # Interface: AffordanceHandler
        try:
            self._publish(f"{self._base_property_topic}/core/status", "UNDEF", True, 1)
            self._disconnect()
        except Exception as e:
            self.log(f"Failed to disconnect: {e}", LogLevels.ERROR)

        from machine import soft_reset

        soft_reset()

    ## TASK SCHEDULER INTERFACE ##
    def _start_scheduler(self, main_task):
        # Launch the runtime and start running all registered tasks.

        # Sets up the asyncio event loop with exception handling and runs
        # the main application task.

        # Args:
        #    main_task: Primary coroutine to execute

        # Interface: TaskScheduler

        def loop_exception_handler(loop, context):
            # Handle exceptions raised in tasks.

            # Args:
            #    loop: The event loop
            #    context: Exception context
            future = context.get("future")
            msg = context.get("exception", context["message"])

            future_name = getattr(future, "__name__", "unknown")

            if isinstance(msg, asyncio.CancelledError):
                pass
                # self.log(f"{future} '{future_name}' was cancelled.", LogLevels.INFO)
            elif isinstance(msg, Exception):
                pass
                # self.log(
                #     f"Generic exception in {future} '{future_name}': {msg}",
                #     LogLevels.ERROR,
                # )
            else:
                pass
                # self.log(
                #     f"Unknown exception in {future} '{future_name}': {msg}",
                #     LogLevels.WARN,
                # )

            for task_id in self._tasks:
                if self._tasks[task_id].done():
                    del self._tasks[task_id]

        loop = asyncio.new_event_loop()
        loop.set_exception_handler(loop_exception_handler)
        loop.run_until_complete(loop.create_task(main_task))
