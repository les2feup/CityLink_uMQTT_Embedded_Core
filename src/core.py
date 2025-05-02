from ssa import EmbeddedCore
from ssa.interfaces import Serializer

import asyncio
from time import ticks_ms, ticks_diff, sleep_ms

from .const import *
from ._log import LogLevels, LogLevel
from ._utils import get_epoch_timestamp


class uMQTTCore(EmbeddedCore):
    """
    Micropython implementation of the CityLink EmbeddedCore interface
    using MQTT over a WLAN.
    """

    def __init__(
        self,
        config_dir,
        fopen_mode=DEFAULT_FOPEN_MODE,
        serializer: Serializer = DEFAULT_SERIALIZER,
        **_,
    ):
        """
        Initialize the uMQTTCore with configuration and serialization parameters.

        Args:
            config_dir: Directory containing configuration files
            fopen_mode: File open mode (default: "r")
            serializer: Object with dumps/loads methods for serialization (default: json)
            **_: Additional keyword arguments (ignored)
        """
        self._config_dir = config_dir
        self._fopen_mode = fopen_mode
        self._serializer = serializer

        self._tasks = {}
        self._actions = {}
        self._properties = {}
        self._net_ro_props = set()

        self._builtin_actions = {
            "vfs/list": self._builtin_action_vfs_list,
            "vfs/read": self._builtin_action_vfs_read,
            "vfs/write": self._builtin_action_vfs_write,
            "vfs/delete": self._builtin_action_vfs_delete,
            "reload": self._builtin_action_reload_core,
            "set_property": self._builtin_action_set_property,
        }

        self._builtin_properties = {
            "log_level": DEFAULT_LOG_LEVEL,
        }

        self.is_registered = False
        self._mqtt_ready = False
        self.last_publish_time = 0

    def _load_config(self):
        """
        Load the configuration from the specified directory.

        Loads configuration files from the specified directory and sets up
        the device ID. If no ID exists, generates one from the device's unique ID.

        Interface: EmbeddedCore
        """
        from ._config import load_configuration

        self._config = load_configuration(
            self._config_dir, self._fopen_mode, self._serializer
        )

        log_level = LogLevels.from_str(self._config.get("log_level"))
        if log_level is None:
            self.log_level = DEFAULT_LOG_LEVEL

        self._id = self._config.get("id")
        if self._id is not None:
            self.is_registered = True
            return

        from machine import unique_id
        from binascii import hexlify

        # Id to use as part of the MQTT topicss and client ID
        self._id = hexlify(unique_id()).decode(MQTT_TOPIC_ENCODING)

    def _write_config(self, update_dict):
        """
        Write the configuration to the specified directory.

        Updates the current configuration with provided dictionary
        and persists it to storage.

        Args:
            update_dict: Dictionary containing configuration updates

        Interface: EmbeddedCore
        """
        self._config.update(update_dict)

        from ._config import write_configuration

        write_configuration(
            self._config, self._config_dir, self._fopen_mode, self._serializer
        )

    def _connect(self):
        """
        Attempt to connect to the Edge Connector.
        In this concrete implementation, this function connects to the WLAN network
        and then to the MQTT Broker.

        Establishes connections to the configured WiFi network and MQTT broker
        with exponential backoff for retries.

        Interface: EmbeddedCore
        """
        broker_config = self._config["runtime"]["broker"]

        con_retries = self._config["runtime"]["connection"].get(
            "retries", DEFAULT_CON_RETRIES
        )
        con_timeout_ms = self._config["runtime"]["connection"].get(
            "timeout_ms", DEFAULT_CON_TIMEOUT_MS
        )

        from ._setup import initialize_mqtt_client, init_wlan

        self._wlan = init_wlan(self._config["network"])
        self._mqtt = initialize_mqtt_client(self._id, broker_config)

        ssid = self._config["network"]["ssid"]

        def connect_wlan():
            if not self._wlan.isconnected():
                raise Exception(f"connecting to `{ssid}` WLAN")
            self.log(f"connected to `{ssid}` WLAN", LogLevels.INFO)

        def connect_mqtt():
            self._mqtt.connect(
                broker_config.get("clean_session", DEFAULT_MQTT_CLEAN_SESSION),
                con_timeout_ms,
            )
            self.log(
                f"connected to MQTT broker at {broker_config['hostname']}",
                LogLevels.INFO,
            )

        from ._utils import with_exponential_backoff

        with_exponential_backoff(connect_wlan, con_retries, con_timeout_ms)
        with_exponential_backoff(connect_mqtt, con_retries, con_timeout_ms)

    def _disconnect(self):
        """
        Disconnect from the network and exit the runtime.

        Terminates MQTT broker connection and disconnects from WiFi network.

        Interface: EmbeddedCore
        """
        self._mqtt_ready = False
        self._mqtt.disconnect()
        self._wlan.disconnect()

    def _publish(self, topic, payload, retain, qos):
        """
        Publish a message to the MQTT broker.

        Args:
            topic: MQTT topic to publish to
            payload: Message payload to send
            retain: MQTT retain flag (default: False)
            qos: MQTT QoS level (default: 0)
        """
        self._mqtt.publish(topic, payload, retain=retain, qos=qos)
        self.last_publish_time = ticks_ms()

    def log(self, msg, level=DEFAULT_LOG_LEVEL):
        if level not in LogLevels:
            log(f"Invalid log level: {level}", "ERROR")

        if level < self.log_level:
            return

        print(f"[{level}] {msg}")

        if not self._mqtt_ready:
            return

        try:
            self.emit_event(
                "log",
                {
                    "level": str(level),
                    "message": msg,
                    "epoch_timestamp": get_epoch_timestamp(),
                },
                core_event=True,
            )
        except Exception as e:
            print(f"[ERROR] Failed to emit log event: {e}")

    def _listen(self):
        """
        Listen and handle incoming requests.

        Checks for new MQTT messages and processes them.

        Args:
            *_: Variable arguments (ignored)

        Interface: EmbeddedCore
        """

        self._mqtt.check_msg()

        if ticks_diff(ticks_ms(), self.last_publish_time) >= PING_TIMEOUT_MS:
            self._mqtt.ping()
            self.last_publish_time = ticks_ms()

    def _register_device(self):
        """
        Register the device with the WoT servient.

        Sends registration request to the WoT servient and waits for confirmation.
        If already registered, skips the registration process.

        Interface: EmbeddedCore
        """
        if self.is_registered:
            self.log(f"Device already registered with ID: {self._id}", LogLevels.INFO)
            return

        registration_payload = self._serializer.dumps(self._config["tm"])

        def on_registration(topic, payload):
            if self.is_registered:
                return

            topic = topic.decode(MQTT_TOPIC_ENCODING)
            payload = self._serializer.loads(payload)

            if topic != f"citylink/{self._id}/registration/ack":
                self.log(f"Unexpected registration topic: {topic}", LogLevels.WARN)
                return

            if payload["status"] != "success":
                self.log(f"Failed to register device: {payload['message']}", LogLevels.ERROR)
                return

            self._id = payload["id"]
            self._write_config({"id": self._id})

            self.is_registered = True

        self._mqtt.set_callback(on_registration)
        self._mqtt.subscribe(f"citylink/{self._id}/registration/ack", CORE_SUBSCRIPTION_QOS)

        time_passed = 0
        while not self.is_registered:
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
        """
        Set up MQTT topic structure and message handling.

        Configures MQTT callbacks and topic subscriptions for action handling.
        """
        from ._setup import mqtt_setup

        def on_message(topic, payload):
            topic = topic.decode(MQTT_TOPIC_ENCODING)
            if not topic.startswith(f"{self._base_action_topic}/"):
                return

            topic = topic[len(f"{self._base_action_topic}/") :]

            namespace, action_name = topic.split("/", 1)
            action_input = None
            if payload != b"":
                try:
                    action_input = self._serializer.loads(payload)
                except Exception as e:
                    self.log(f"Failed to deserialize payload: {e}", LogLevels.ERROR)
                    return

            self._invoke_action(namespace, action_name, action_input)

        self._base_event_topic, self._base_action_topic, self._base_property_topic = (
            mqtt_setup(self._id, RT_NAMESPACE, self._mqtt, on_message)
        )
        
        self._mqtt_ready = True

    def App(polling_interval_ms=POLLING_INTERVAL_MS, config_dir="./config", **kwargs):
        """
        Decorator to mark the entry point for the EmbeddedCore.

        Creates a uMQTTCore instance, loads configuration, and sets up
        the runtime environment.

        Args:
            polling_interval: Interval for message polling in milliseconds (default: 250)
            config_dir: Directory containing configuration files (default: "./config")
            **kwargs: Additional arguments passed to uMQTTCore uctor

        Returns:
            Function decorator that wraps the main application entry point

        Interface: EmbeddedCore
        """
        core = uMQTTCore(config_dir, **kwargs)
        core._load_config()

        def main_decorator(main_func):
            """
            Decorates the main function of the EmbeddedCore.

            Args:
                main_func: The main function to be decorated

            Returns:
                Wrapped function that initializes the runtime
            """

            def main_wrapper():
                """
                Wrapper for the main function of the EmbeddedCore.

                Handles connection, registration, MQTT setup, and scheduler launch.
                """
                core._connect()
                core._register_device()
                core._setup_mqtt()

                core.log("Connected to network. Initiating main task", LogLevels.INFO)

                async def main_task():
                    main_func(core)
                    core.log("User main completed. Entering main loop", LogLevels.DEBUG)
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

    ## AFFORDANCE HANDLER INTERFACE ##

    def create_property(self, property_name, initial_value, net_ro=False, **_):
        """
        Create a new property with the specified name and initial value.

        Args:
            property_name: Name of the property to create
            initial_value: Initial value for the property
            net_ro: If True, property is read-only over the network (default: False)
            **_: Additional keyword arguments (ignored)

        Raises:
            ValueError: If property already exists

        Interface: AffordanceHandler
        """
        if property_name in self._properties:
            self.log(f"Property {property_name} already exists. Set a new value using 'set_property'", LogLevels.WARN)
            return

        # TODO: sanitize property names
        if net_ro:
            self._net_ro_props.add(property_name)
        self._properties[property_name] = initial_value

    def get_property(self, property_name, **_):
        """
        Get the value of a property by name.

        Args:
            property_name: Name of the property to retrieve
            **_: Additional keyword arguments (ignored)

        Returns:
            Current value of the property

        Raises:
            ValueError: If property does not exist

        Interface: AffordanceHandler
        """
        if property_name not in self._properties:
            self.log(f"Property {property_name} does not exist. Create it using 'create_property' method.", LogLevels.WARN)
            return

        return self._properties[property_name]

    def set_property(
        self,
        property_name,
        value,
        core_prop=False,
        retain=PROPERTY_RETAIN,
        qos=PROPERTY_QOS,
        **_,
    ):
        """
        Set the value of a property.

        Updates a property value and publishes it via MQTT. For dict values,
        merges with the existing property value.

        Args:
            property_name: Name of the property to update
            value: New value for the property
            retain: MQTT retain flag (default: True)
            qos: MQTT QoS level (default: 0)
            **_: Additional keyword arguments (ignored)

        Raises:
            ValueError: If property does not exist or has type mismatch

        Interface: AffordanceHandler
        """

        map = None
        namespace = None

        if core_prop and property_name in self._builtin_properties:
            map = self._builtin_properties
            namespace = RT_NAMESPACE
        if not core_prop and property_name in self._properties:
            map = self._properties
            namespace = self._config["tm"]["name"]

        if map is None or namespace is None:
            # TODO: Log the error
            self.log(f"Property {namespace}/{property_name} does not exist.", LogLevels.WARN)
            return

        if not isinstance(value, type(map[property_name])):
            # TODO: Log the error
            self.log(f"Value of property '{namespace}/{property_name}' must be of type {type(map[property_name]).__name__}", LogLevels.ERROR)
            return

        if isinstance(value, dict):
            map[property_name].update(value)
        else:
            map[property_name] = value

        topic = f"{self._base_property_topic}/{namespace}/{property_name}"
        payload = self._serializer.dumps(value)

        try:
            self._publish(topic, payload, retain=retain, qos=qos)
        except Exception as e:
            self.log(f"Failed to publish property update: {e}", LogLevels.ERROR)

    def emit_event(
        self,
        event_name,
        event_data,
        core_event=False,
        retain=EVENT_RETAIN,
        qos=EVENT_QOS,
        **_,
    ):
        """
        Emit an event with the specified name and data.

        Publishes an event to the MQTT broker with the given payload.

        Args:
            event_name: Name of the event to emit
            event_data: Data payload for the event
            retain: MQTT retain flag (default: False)
            qos: MQTT QoS level (default: 0)
            **_: Additional keyword arguments (ignored)

        Interface: AffordanceHandler
        """
        # TODO: sanitize event names
        namespace = RT_NAMESPACE if core_event else self._config["tm"]["name"]
        topic = f"{self._base_event_topic}/{namespace}/{event_name}"
        payload = self._serializer.dumps(event_data)
        self._publish(topic, payload, retain=retain, qos=qos)

    def sync_executor(func):
        """
        Decorator for synchronous task or action executors.

        Wraps a synchronous function to make it compatible with async execution.

        Args:
            func: Synchronous function to wrap

        Returns:
            Async-compatible wrapped function

        Interface: AffordanceHandler
        """

        # Wrap the function in an async wrapper
        # So it can be executed as a coroutine
        async def wrapper(self, *args, **kwargs):
            return func(self, *args, **kwargs)

        return wrapper

    def async_executor(func):
        """
        Decorator for asynchronous task or action executors.

        Ensures proper async execution for functions that are already coroutines.

        Args:
            func: Async function to wrap

        Returns:
            Properly wrapped async function

        Interface: AffordanceHandler
        """

        async def wrapper(self, *args, **kwargs):
            return await func(self, *args, **kwargs)

        return wrapper

    def register_action_executor(
        self, action_name, action_func, qos=ACTION_SUBSCRIPTION_QOS, **_
    ):
        """
        Register a new action handler.

        Associates an action name with a function and subscribes to the
        corresponding MQTT topic.

        Args:
            action_name: Name of the action to register
            action_func: Function to execute when the action is triggered
            qos: MQTT QoS level (default: 0)
            **_: Additional keyword arguments (ignored)

        Raises:
            ValueError: If action already exists

        Interface: AffordanceHandler
        """
        if action_name in self._actions:
            self.log(f"Action {action_name} already exists.", LogLevels.WARN)

        # TODO: sanitize action names
        self._actions[action_name] = action_func

        namespace = self._config["tm"]["name"]
        self._mqtt.subscribe(
            f"{self._base_action_topic}/{namespace}/{action_name}", qos=qos
        )

    def _invoke_action(self, namespace, action_name, action_input, **_):
        """
        Invoke an action with the specified input.

        Executes either a built-in action or a user-defined action based on
        the namespace and action name.

        Args:
            namespace: Namespace for the action
            action_name: Name of the action to invoke
            action_input: Input data for the action
            **_: Additional keyword arguments (ignored)

        Raises:
            ValueError: If action handler does not exist

        Interface: AffordanceHandler
        """
        if namespace == RT_NAMESPACE and action_name in self._builtin_actions:
            asyncio.create_task(self._builtin_actions[action_name](action_input))

        elif namespace == self._config["tm"]["name"] and action_name in self._actions:
            asyncio.create_task(self._actions[action_name](self, action_input))

        else:
            self.log(
                f"Action handler for {namespace}/{action_name} does not exist.",
                LogLevels.WARN,
            )

    def _wrap_vfs_action(action_func):
        """
        Decorator for VFS action handlers.

        Wraps VFS actions to add timestamps and publish results as events.

        Args:
            action_func: VFS action function to wrap

        Returns:
            Wrapped function that publishes results
        """

        def builtin_action_wrapper(self, action_input):
            action_result = action_func(self, action_input)
            action_result.update({"epoch_timestamp": get_epoch_timestamp()})
            action_result = self._serializer.dumps(action_result)
            self._publish(
                f"{self._base_event_topic}/{RT_NAMESPACE}/vfs/report",
                action_result,
                retain=False,
                qos=1,
            )

        return builtin_action_wrapper

    @sync_executor
    @_wrap_vfs_action
    def _builtin_action_vfs_read(self, action_input):
        """
        Read the contents of a file from the virtual file system.

        Args:
            action_input: Dictionary containing the file path to read

        Returns:
            Dictionary with action result

        Raises:
            NotImplementedError: Must be implemented by subclasses

        Interface: AffordanceHandler
        """
        raise NotImplementedError("Subclasses must implement builtin_action_vfs_read()")

    @sync_executor
    @_wrap_vfs_action
    def _builtin_action_vfs_write(self, action_input):
        """
        Write data to a file in the virtual file system.

        Args:
            action_input: Dictionary containing path, payload, and append flag

        Returns:
            Dictionary with action result and error status

        Interface: AffordanceHandler
        """
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
            from ._builtins import vfs_write

            vfs_write(file_path, append, data, data_hash, data_hash_algo)
            result["message"] = file_path
        except Exception as e:
            result["error"] = True
            result["message"] = str(e)

        return result

    @sync_executor
    @_wrap_vfs_action
    def _builtin_action_vfs_list(self, action_input):
        """
        List the contents of the virtual file system.

        Args:
            action_input: Dictionary containing parameters for listing

        Returns:
            Dictionary with listing results

        Raises:
            NotImplementedError: Must be implemented by subclasses

        Interface: AffordanceHandler
        """
        raise NotImplementedError("Subclasses must implement builtin_action_vfs_list()")

    @sync_executor
    @_wrap_vfs_action
    def _builtin_action_vfs_delete(self, action_input):
        """
        Delete a file from the virtual file system.

        Args:
            action_input: Dictionary containing the file path to delete

        Returns:
            Dictionary with deletion result

        Raises:
            NotImplementedError: Must be implemented by subclasses

        Interface: AffordanceHandler
        """
        raise NotImplementedError(
            "Subclasses must implement builtin_action_vfs_delete()"
        )

    @sync_executor
    def _builtin_action_set_property(self, action_input):
        """
        Set the value of a property.

        Args:
            action_input: Dictionary containing property name and value

        Raises:
            ValueError: If property is read-only or input is malformed

        Interface: AffordanceHandler
        """
        property_name = action_input.get("pname")
        property_value = action_input.get("pval")
        if property_name is None or property_value is None:
            self.log(
                "Malformed set_property action input. Input template: {'pname': 'name_string', 'pval': Any}",
                LogLevels.ERROR,
            )
            return

        if property_name in self._net_ro_props:
            self.log(f"Property {property_name} is read-only.", LogLevels.WARN)
            return

        self.set_property(property_name, property_value)

    @sync_executor
    def _builtin_action_reload_core(self, _):
        """
        Reload the core module.

        Disconnects from network services and performs a soft reset of the device.

        Args:
            _: Input parameter (ignored)

        Interface: AffordanceHandler
        """
        try:
            self._disconnect()
        except Exception as e:
            self.log(f"Failed to disconnect: {e}", LogLevels.ERROR)

        from machine import soft_reset

        self.log("Reloading core module...", LogLevels.WARN)
        soft_reset()

    ## TASK SCHEDULER INTERFACE ##

    def _start_scheduler(self, main_task):
        """
        Launch the runtime and start running all registered tasks.

        Sets up the asyncio event loop with exception handling and runs
        the main application task.

        Args:
            main_task: Primary coroutine to execute

        Interface: TaskScheduler
        """

        def loop_exception_handler(loop, context):
            """
            Handle exceptions raised in tasks.

            Args:
                loop: The event loop
                context: Exception context
            """
            future = context.get("future")
            msg = context.get("exception", context["message"])

            future_name = getattr(future, "__name__", "unknown")

            # TODO: (try to) publish the error as an event
            if isinstance(msg, asyncio.CancelledError):
                self.log(f"{future} '{future_name}' was cancelled.", LogLevels.INFO)
            elif isinstance(msg, Exception):
                self.log(
                    f"Generic exception in {future} '{future_name}': {msg}",
                    LogLevels.ERROR,
                )
            else:
                self.log(
                    f"Unknown exception in {future} '{future_name}': {msg}",
                    LogLevels.WARN,
                )

            for task_id in self._tasks:
                if self._tasks[task_id].done():
                    del self._tasks[task_id]

        self.log("Starting task scheduler...", LogLevels.DEBUG)
        loop = asyncio.new_event_loop()
        loop.set_exception_handler(loop_exception_handler)

        self.log("Running main task...", LogLevels.DEBUG)
        loop.run_until_complete(loop.create_task(main_task))

    def task_create(self, task_id, task_func, period_ms=0):
        """
        Register a task for execution.

        Associates a unique task identifier with a callable that encapsulates the task's logic.

        Args:
            task_id: A unique identifier for the task. Must be unique across all active tasks.
            task_func: A callable implementing the task's functionality. Should be an async
                      function that accepts 'self' as its parameter.
            period_ms: The period in milliseconds between consecutive executions of the task.
                      If set to 0 (default), the task will execute only once (one-shot task).
                      If greater than 0, the task will execute repeatedly at the specified interval.

        Raises:
            ValueError: If a task with the specified task_id already exists.

        Notes:
            - For periodic tasks, the time spent executing the task is considered when
              calculating the next execution time.
            - Tasks are automatically removed from the registry when they exit or raise
              an unhandled exception.
        """
        if task_id in self._tasks:
            self.log(f"Task {task_id} already exists.", LogLevels.WARN)
            return

        async def try_wrapper():
            try:
                await task_func(self)
            except asyncio.CancelledError:
                self.log(f"Task {task_id} was cancelled.", LogLevels.INFO)
                return True
            except Exception as e:
                self.log(f"Task {task_id} failed: {e}", LogLevels.ERROR)
                return True

            return False

        async def task_wrapper():
            while True:
                start_time = ticks_ms()

                should_exit = await try_wrapper()

                elapsed_time = ticks_diff(ticks_ms(), start_time)
                await self.task_sleep_ms(max(0, period_ms - elapsed_time))

                if should_exit:
                    break

            del self._tasks[task_id]

        if period_ms == 0:
            asyncio.create_task(try_wrapper())  # One shot task
        else:
            self._tasks[task_id] = asyncio.create_task(task_wrapper())

    def task_cancel(self, task_id):
        """
        Cancel a registered task.

        Cancels the task identified by the given task_id.

        Args:
            task_id: The identifier of the task to cancel.

        Raises:
            ValueError: If task with the specified ID does not exist
        """
        if task_id not in self._tasks:
            raise ValueError(f"Task {task_id} does not exist.")

        self._tasks[task_id].cancel()

    async def task_sleep_s(self, s):
        """
        Asynchronously sleep for the specified number of seconds.

        Args:
            s (int | float): The sleep duration in seconds.
        """
        await asyncio.sleep(s)

    async def task_sleep_ms(self, ms):
        """
        Asynchronously pause execution for a specified number of milliseconds.

        Args:
            ms: Duration to sleep in milliseconds.
        """
        await asyncio.sleep_ms(ms)
