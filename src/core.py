from ssa import SSACore
from ssa.interfaces import Serializer

from micropython import const

import json
import asyncio

from time import ticks_ms, ticks_diff


RT_NAMESPACE = "umqtt_core"


class uMQTTCore(SSACore):
    """
    MQTT-based implementation of the SSA Core for MicroPython.

    This class provides a lightweight MQTT client for IoT devices running MicroPython,
    with support for SSA (Simple Service Architecture) pattern.
    """

    def __init__(self, config_dir, fopen_mode="r", serializer: Serializer = json, **_):
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

        self._builtin_actions = const(
            {
                "vfs/list": self._builtin_action_vfs_list,
                "vfs/read": self._builtin_action_vfs_read,
                "vfs/write": self._builtin_action_vfs_write,
                "vfs/delete": self._builtin_action_vfs_delete,
                "reload": self._builtin_action_reload_core,
                "set_property": self._builtin_action_set_property,
            }
        )

        self.is_registered = False

    def _load_config(self):
        """
        Load the configuration from the specified directory.

        Loads configuration files from the specified directory and sets up
        the device ID. If no ID exists, generates one from the device's unique ID.

        Interface: SSACore
        """
        from ._config import load_configuration

        self._config = load_configuration(
            self._config_dir, self._fopen_mode, self._serializer
        )

        self._id = self._config.get("id")
        if self._id is not None:
            self.is_registered = True
            return

        from machine import unique_id
        from binascii import hexlify

        self._id = hexlify(unique_id()).decode("utf-8")

    def _write_config(self, update_dict):
        """
        Write the configuration to the specified directory.

        Updates the current configuration with provided dictionary
        and persists it to storage.

        Args:
            update_dict: Dictionary containing configuration updates

        Interface: SSACore
        """
        self._config.update(update_dict)

        from ._config import write_configuration

        write_configuration(
            self._config, self._config_dir, self._fopen_mode, self._serializer
        )

    def _connect(self):
        """
        Attempt to connect to the Edge Node's SSA IoT Connector.

        Establishes connections to the configured WiFi network and MQTT broker
        with exponential backoff for retries.

        Interface: SSACore
        """
        broker_config = self._config["runtime"]["broker"]

        con_retries = self._config["runtime"]["connection"].get("retries", 3)
        con_timeout_ms = self._config["runtime"]["connection"].get("timeout_ms", 1000)

        from ._setup import initialize_mqtt_client, init_wlan

        self._wlan = init_wlan(self._config["network"])
        self._mqtt = initialize_mqtt_client(self._id, broker_config)

        ssid = self._config["network"]["ssid"]

        def connect_wlan():
            if not self._wlan.isconnected():
                raise Exception(f"connecting to `{ssid}` WLAN")
            print(f"connected to `{ssid}` WLAN")

        def connect_mqtt():
            self._mqtt.connect(broker_config.get("clean_session", True), con_timeout_ms)
            print(
                f"connected to MQTT broker at {broker_config['hostname']}:{broker_config.get('port', 1883)}"
            )

        from ._utils import with_exponential_backoff

        with_exponential_backoff(connect_wlan, con_retries, con_timeout_ms)
        with_exponential_backoff(connect_mqtt, con_retries, con_timeout_ms)

    def _disconnect(self):
        """
        Disconnect from the network and exit the runtime.

        Terminates MQTT broker connection and disconnects from WiFi network.

        Interface: SSACore
        """
        self._mqtt.disconnect()
        self._wlan.disconnect()

    def _listen(self, *_):
        """
        Listen and handle incoming requests.

        Checks for new MQTT messages and processes them.

        Args:
            *_: Variable arguments (ignored)

        Interface: SSACore
        """
        self._mqtt.check_msg()

    def _register_device(self):
        """
        Register the device with the WoT servient.

        Sends registration request to the WoT servient and waits for confirmation.
        If already registered, skips the registration process.

        Interface: SSACore
        """
        if self.is_registered:
            print(f"Device already registered with ID: {self._id}")
            return

        registration_payload = self._serializer.dumps(self._config["tm"])
        self._mqtt.publish(f"ssa/{self._id}/registration", registration_payload)

        def on_registration(topic, payload):
            print(f"[DEBUG] Received registration ack: {topic} - {payload}")
            payload = self._serializer.loads(payload)

            if topic != f"ssa/{self._id}/registration/ack":
                raise ValueError(f"Unexpected registration topic: {topic}")

            if payload["status"] != "success":
                raise ValueError(f"Failed to register device: {payload['message']}")

            self._id = payload["id"]
            self._write_config({"id": self._id})

            self.is_registered = True

        self._mqtt.set_callback(on_registration)
        self._mqtt.subscribe(f"ssa/{self._id}/registration/ack")

        while not self.is_registered:
            try:
                self._listen(True)
            except Exception as e:
                print(f"Error listening on socket: {e}")

    def _setup_mqtt(self):
        """
        Set up MQTT topic structure and message handling.

        Configures MQTT callbacks and topic subscriptions for action handling.
        """
        from ._setup import mqtt_setup

        def on_message(topic, payload):
            topic = topic.decode("utf-8")
            print(f"[DEBUG] Received message: {topic} - {payload}")
            if not topic.startswith(f"{self._base_action_topic}/"):
                return

            topic = topic[len(f"{self._base_action_topic}/") :]

            namespace, action_name = topic.split("/", 1)
            action_input = None
            if payload != b"":
                try:
                    action_input = self._serializer.loads(payload)
                except Exception as e:
                    print(f"Failed to deserialize payload: {e}")
                    return

            self._invoke_action(namespace, action_name, action_input)

        self._base_event_topic, self._base_action_topic, self._base_property_topic = (
            mqtt_setup(self._id, RT_NAMESPACE, self._mqtt, on_message)
        )

    def App(config_dir="./config", **kwargs):
        """
        Decorator to mark the entry point for the SSACore.

        Creates a uMQTTCore instance, loads configuration, and sets up
        the runtime environment.

        Args:
            config_dir: Directory containing configuration files (default: "./config")
            **kwargs: Additional arguments passed to uMQTTCore constructor

        Returns:
            Function decorator that wraps the main application entry point

        Interface: SSACore
        """
        core = uMQTTCore(config_dir, **kwargs)
        core._load_config()

        def main_decorator(main_func):
            """
            Decorates the main function of the SSACore.

            Args:
                main_func: The main function to be decorated

            Returns:
                Wrapped function that initializes the runtime
            """

            def main_wrapper():
                """
                Wrapper for the main function of the SSACore.

                Handles connection, registration, MQTT setup, and scheduler launch.
                """
                core._connect()
                core._register_device()
                core._setup_mqtt()

                async def main_task():
                    main_func(core)
                    while True:
                        core._listen()
                        await core.task_sleep_ms(250)

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
            raise ValueError(
                f"Property {property_name} already exists. Set a new value using 'set_property' method."
            )

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
            raise ValueError(
                f"Property {property_name} does not exist. Create it using 'create_property' method."
            )

        return self._properties[property_name]

    def set_property(self, property_name, value, retain=True, qos=0, **_):
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
        if property_name not in self._properties:
            raise ValueError(
                f"Property {property_name} does not exist. Create it using 'create_property' method."
            )

        if not isinstance(value, type(self._properties[property_name])):
            raise ValueError(
                f"Value of property '{property_name}' must be of type {type(self._properties[property_name]).__name__}"
            )

        if isinstance(value, dict):
            self._properties[property_name].update(value)
        else:
            self._properties[property_name] = value

        try:
            namespace = self._config["tm"]["name"]
            topic = f"{self._base_property_topic}/{namespace}/{property_name}"
            payload = self._serializer.dumps(value)
            print(f"[DEBUG] Publishing property: {topic} - {payload}")
            self._mqtt.publish(topic, payload, retain=retain, qos=qos)
            print(f"[DEBUG] Published property successfully.")
        except Exception as e:
            # TODO: publish the error
            raise ValueError(f"Failed to set property {property_name}: {e}") from e

    def emit_event(self, event_name, event_data, retain=False, qos=0, **_):
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
        namespace = self._config["tm"]["name"]
        topic = f"{self._base_event_topic}/{namespace}/{event_name}"
        payload = self._serializer.dumps(event_data)
        self._mqtt.publish(topic, payload, retain=retain, qos=qos)

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

    def register_action_executor(self, action_name, action_func, qos=0, **_):
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
            raise ValueError(f"Action {action_name} already exists.")

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
        print(f"[DEBUG] Invoking action: {namespace}/{action_name} - {action_input}")
        if namespace == RT_NAMESPACE and action_name in self._builtin_actions:
            asyncio.create_task(self._builtin_actions[action_name](action_input))

        elif namespace == self._config["tm"]["name"] and action_name in self._actions:
            asyncio.create_task(self._actions[action_name](self, action_input))

        else:
            raise ValueError(
                f"Action handler for {namespace}/{action_name} does not exist."
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

            from time import gmtime, mktime

            action_result.update(
                {"timestamp": {"epoch_year": gmtime(0)[0], "seconds": mktime(gmtime())}}
            )

            action_result = self._serializer.dumps(action_result)
            self._mqtt.publish(
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
        try:
            file_path = action_input.get("path")
            if file_path is None:
                raise ValueError("Missing path in action input.")

            payload = action_input.get("payload")
            if payload is None or not isinstance(payload, dict):
                raise ValueError("Missing or invalid payload in action input.")

            data = payload.get("data")
            data_hash = int(payload.get("hash"), 16)
            data_hash_algo = payload.get("algo")

            if any(map(lambda x: x is None, [data, data_hash, data_hash_algo])):
                raise ValueError("Missing or invalid payload data.")

            append = action_input.get("append", False)

            from ._builtins import vfs_write

            vfs_write(file_path, append, data, data_hash, data_hash_algo)

            return {"action": "write", "error": False, "message": file_path}
        except Exception as e:
            return {"action": "write", "error": True, "message": str(e)}

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
        property_name = action_input.get("property")
        property_value = action_input.get("value")
        if property_name is None or property_value is None:
            raise ValueError(
                "Malformed action input. Input template: {'property': 'name_string', 'value': Any}"
            )

        if property_name in self._net_ro_props:
            raise ValueError(f"Property {property_name} is read-only.")

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
            print(f"Failed to disconnect: {e}")

        from machine import soft_reset

        print("Reloading core module...")
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
                print(f"{future} '{future_name}' was cancelled.")
            elif isinstance(msg, Exception):
                print(f"Generic exception in {future} '{future_name}': {msg}")
                # TODO: publish the error
            else:
                print(f"Unknown exception in {future} '{future_name}': {msg}")

            for task_id in self._tasks:
                if self._tasks[task_id].done():
                    del self._tasks[task_id]

        print("Starting task scheduler...")
        loop = asyncio.new_event_loop()
        loop.set_exception_handler(loop_exception_handler)

        print("Executing main task...")
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
            raise ValueError(f"Task {task_id} already exists.")

        async def try_wrapper():
            try:
                await task_func(self)
            except asyncio.CancelledError:
                print(f"Task {task_id} was cancelled.")
                return True
            except Exception as e:
                print(f"Error in task {task_id}: {e}")
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
