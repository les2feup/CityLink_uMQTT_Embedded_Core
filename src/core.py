from ssa import SSACore
from ssa.interfaces import Serializer

from micropython import const
import json

RT_NAMESPACE = "umqtt_core"


class uMQTTCore(SSACore):
    def __init__(self, config_dir, fopen_mode="r", serializer: Serializer = json):
        self._config_dir = config_dir
        self._fopen_mode = fopen_mode
        self._serializer = serializer

        self._tasks = {}
        self._actions = {}
        self._properties = {}

        self._builtin_actions = const(
            {
                "vfs/list": self._builtin_action_vfs_list,
                "vfs/read": self._builtin_action_vfs_read,
                "vfs/write": self._builtin_action_vfs_write,
                "vfs/delete": self._builtin_action_vfs_delete,
                "reload": self._builtin_action_reload_core,
            }
        )

    def _load_config(self):
        """
        Interface: SSACore

        Load the configuration from the specified directory.
        """
        from ._config import load_configuration

        self.config = load_configuration(
            self._config_dir, self._fopen_mode, self._serializer
        )

    def _write_config(self, update_dict):
        """
        Interface: SSACore

        Write the configuration to the specified directory.
        """
        self.config.update(update_dict)

        from ._config import write_configuration

        write_configuration(
            self.config, self._config_dir, self._fopen_mode, self._serializer
        )

    def _connect(self):
        """
        Interface: SSACore

        Attempt to the Edge Node's SSA IoT Connector
        """

        broker_config = self.config["runtime"]["broker"]

        con_retries = self.config["runtime"]["connection"].get("retries", 3)
        con_timeout_ms = self.config["runtime"]["connection"].get("timeout_ms", 1000)

        from ._setup import initialize_mqtt_client, init_wlan

        self._wlan = init_wlan(self.config["network"])
        self._mqtt = initialize_mqtt_client(self.id, broker_config)

        def connect_wlan():
            if not self._wlan.isconnected():
                raise Exception(f"connecting to `{ssid}` WLAN")

        def connect_mqtt():
            self._mqtt.connect(broker_config.get(clean_session, True), timeout_ms)

        from ._utils import with_exponential_backoff

        with_exponential_backoff(connect_wlan, con_retries, con_timeout_ms)
        with_exponential_backoff(connect_mqtt, con_retries, con_timeout_ms)

    def _disconnect(self):
        """
        Interface: SSACore

        Disconnect from the network. And exit the runtime.
        """

        self._mqtt.disconnect()
        self._wlan.disconnect()

        asyncio.current_task().cancel()

    def _listen(self, blocking):
        """
        Interface: SSACore

        Listen and handle incoming requests.
        """
        if blocking:
            self._mqtt.wait_msg()
        else:
            self._mqtt.check_msg()

    def _register_device(self):
        """
        Interface: SSACore

        Registers the device with the WoT servient.

        Send registration request to the WoT servient to register the device.
        Wait for registration confirmation before proceeding.

        Subclasses must override this method to implement the device registration logic.
        """
        self.id = self.config.get("id")
        if self.id is not None:
            return

        from machine import unique_id
        from binascii import hexlify

        self.id = hexlify(unique_id()).decode("utf-8")

        registration_payload = self._serializer.dumps(self.config["tm"])
        self._mqtt.publish(f"ssa/{self.id}/registration", registration_payload)

        def on_registration(topic, payload):
            topic = topic.decode("utf-8")
            payload = self._serializer.loads(payload)

            if topic != f"ssa/{self.id}/registration/ack":
                raise ValueError(f"Unexpected registration topic: {topic}")

            if payload["status"] != "success":
                raise ValueError(f"Failed to register device: {payload['message']}")

            self.id = payload["id"]
            self._write_config({"id": self.id})

        self._mqtt.set_callback(on_registration)
        self._mqtt.subscribe(f"ssa/{self.id}/{registration}/ack")
        self._mqtt.wait_msg()  # Blocking wait for registration ack

    def _setup_mqtt(self):
        from ._setup import mqtt_setup

        def on_message(topic, payload):
            topic = topic.decode("utf-8")
            if not topic.startswith(f"{self._base_action_topic}/"):
                return

            topic = topic[len(f"{self._base_action_topic}/") :]

            namespace, action_name = topic.split("/", 1)
            action_input = self._serializer.loads(payload)

            self._invoke_action(namespace, action_name, action_input)

        self._base_event_topic, self._base_action_topic, self._base_property_topic = mqtt_setup(
            self.id, RT_NAMESPACE, self._mqtt, on_message
        )

    def SSACoreEntry(config_dir="./config", **kwargs):
        """
        Interface: SSACore

        Decorator to mark the entry point for the SSACore.
        """

        core = uMQTTCore(config_dir, **kwargs)
        core._load_config()

        def main_decorator(main_func):
            """Decorates the main function of the SSACore."""

            def main_wrapper():
                """Wrapper for the main function of the SSACore."""
                core._connect()
                core._register_device()
                core._setup_mqtt()

                async def main_task():
                    main_func()
                    while True:
                        blocking = len(core._tasks) == 0
                        core._listen(blocking)
                        await core.task_sleep_ms(100)

                core._start_scheduler(main_task())

            return main_wrapper

        return main_decorator

    ## AFFORDANCE HANDLER INTERFACE ##

    def create_property(self, property_name, initial_value, **_):
        """
        Interface: AffordanceHandler

        Create a new property with the specified name and initial value.
        """
        if property_name in self._properties:
            raise ValueError(
                f"Property {property_name} already exists. Set a new value using 'set_property' method."
            )

        # TODO: sanitize property names

        self._properties[property_name] = initial_value

    def get_property(self, property_name, **_):
        """
        Interface: AffordanceHandler

        Get the value of a property by name.
        """
        if property_name not in self._properties:
            raise ValueError(
                f"Property {property_name} does not exist. Create it using 'create_property' method."
            )

        return self._properties[property_name]

    def set_property(self, property_name, value, retain=True, qos=0, **_):
        """
        Interface: AffordanceHandler

        Set the value of a property.
        """
        if property_name not in self._properties:
            raise ValueError(
                f"Property {property_name} does not exist. Create it using 'create_property' method."
            )

        self._properties[property_name] = value

        try:
            namespace = self.config["tm"]["name"]
            topic = f"{self._base_property_topic}/{namespace}/{property_name}"
            payload = self._serializer.dumps(value)
            self._mqtt.publish(topic, payload, retain=retain, qos=qos)
        except Exception as e:
            # TODO: publish the error
            raise ValueError(f"Failed to set property {property_name}: {e}") from e

    def emit_event(self, event_name, event_data, retain=False, qos=0**_):
        """
        Interface: AffordanceHandler

        Emit an event with the specified name and data.
        """
        # TODO: sanitize event names
        namespace = self.config["tm"]["name"]
        topic = f"{self._base_event_topic}/{namespace}/{event_name}"
        payload = self._serializer.dumps(event_data)
        self._mqtt.publish(topic, payload, retain=retain, qos=qos)

    def sync_action(func):
        """
        Interface: AffordanceHandler

        Decorator for synchronous action handlers.
        """

        # Wrap the function in an async wrapper
        # So it can be executed as a coroutine
        async def wrapper(self, *args, **kwargs):
            return func(self, *args, **kwargs)

        # Copy the function name to allow for proper error messages
        wrapper.__name__ = func.__name__
        return wrapper

    def async_action(func):
        """
        Interface: AffordanceHandler

        Decorator for asynchronous action handlers.
        """

        async def wrapper(self, *args, **kwargs):
            return await func(self, *args, **kwargs)

        # Copy the function name to allow for proper error messages
        wrapper.__name__ = func.__name__
        return wrapper

    def register_action_handler(self, action_name, action_func, qos=0, **_):
        """
        Interface: AffordanceHandler

        Register a new action handler.
        """
        if action_name in self._actions:
            raise ValueError(f"Action {action_name} already exists.")

        #TODO: sanitize action names
        self._actions[action_name] = action_func

        namespace = self.config["tm"]["name"]
        self._mqtt.subscribe(
            f"{self._base_action_topic}/{namespace}/{action_name}", qos=qos
        )

    def _invoke_action(self, namespace, action_name, action_input, **_):
        """
        Interface: AffordanceHandler

        Invoke an action with the specified input.
        """

        if namespace == RT_NAMESPACE and action_name in self._builtin_actions:
            self.task_create(action_name, self._builtin_actions[action_name](action_input))

        elif namespace == self.config["tm"]["name"] and action_name in self._actions:
            self.task_create(action_name, self._actions[action_name](action_input))

        raise ValueError(f"Action handler for {namespace}/{action_name} does not exist.")

    def _wrap_builtin_action(action_func):
        async def builtin_action_wrapper(self, action_input):
            action_result = action_func(self, action_input)

            from time import gmtime, mktime
            action_result.update({
                "timestamp":{
                    "epoch_year": gmtime()[0],
                    "seconds": mktime(gmtime())
                    }
                })

            action_result = self._serializer.dumps(action_result)
            self._mqtt.publish(f"{self._base_event_topic}/{RT_NAMESPACE}/vfs/report", action_result, retain=False, qos=1)

        return builtin_action_wrapper

    @_wrap_builtin_action
    def _builtin_action_vfs_read(self, action_input):
        """
        Interface: AffordanceHandler
        Read the contents of a file from the virtual file system."""
        raise NotImplementedError("Subclasses must implement builtin_action_vfs_read()")

    @_wrap_builtin_action
    def _builtin_action_vfs_write(self, action_input):
        """
        Interface: AffordanceHandler
        Write data to a file in the virtual file system."""
        try:
            file_path = action_input.get("path")
            if file_path is None:
                raise ValueError("Missing path in action input.")

            payload = action_input.get("payload")
            if payload is None or not isinstance(payload, dict):
                raise ValueError("Missing or invalid payload in action input.")

            data = payload.get("data")
            data_hash = payload.get("hash")
            data_hash_algo = payload.get("algo")

            if any(map(lambda x: x is None, [data, data_hash, data_hash_algo])):
                raise ValueError("Missing or invalid payload data.")

            append = action_input.get("append", False)

            from ._builtins import vfs_write
            vfs_write(file_path, append, data, data_hash, data_hash_algo)

            return {"action": "write", "error": False, "message": file_path}
        except Exception as e:
            return {"action": "write", "error": True, "message": str(e)}
        
    @_wrap_builtin_action
    def _builtin_action_vfs_list(self, action_input):
        """
        Interface: AffordanceHandler
        List the contents of the virtual file system."""
        raise NotImplementedError("Subclasses must implement builtin_action_vfs_list()")

    @_wrap_builtin_action
    def _builtin_action_vfs_delete(self, action_input):
        """
        Interface: AffordanceHandler
        Delete a file from the virtual file system."""
        raise NotImplementedError(
            "Subclasses must implement builtin_action_vfs_delete()"
        )

    def _builtin_action_reload_core(self, _):
        """
        Interface: AffordanceHandler
        Reload the core module."""
        self._disconnect()

        from machine import soft_reset
        soft_reset()

    ## TASK SCHEDULER INTERFACE ##

    def _start_scheduler(self, main_task):
        """
        Interface: TaskScheduler

        Launch the runtime and start running all registered tasks.
        """

        def task_exception_handler(loop, context):
            """Handle exceptions raised in tasks."""
            future = context.get("future")
            msg = context.get("exception", context["message"])

            #TODO: (try to) publish the error as an event
            if isinstance(msg, asyncio.CancelledError):
                print(f"Task {future} was cancelled.")
            elif:
                isinstance(msg, Exception):
                print(f"Generic exception in task {future}: {msg}")
            else:
                print(f"Unknown exception in task {future}: {msg}")

            for id, task in self._tasks:
                if task.done():
                    del self._tasks[id]
                
        loop = asyncio.new_event_loop()
        loop.set_exception_handler(self._exception_handler)

        asyncio.run(main_task)

    def task_create(self, task_id, task_func):
        """
        Register a task for execution.

        Associates a unique task identifier with a callable that encapsulates the task's logic.
        Subclasses must override this method to provide the actual task scheduling or execution
        mechanism.

        Args:
            task_id: A unique identifier for the task.
            task_func: A callable implementing the task's functionality.
        """
        if task_id in self._tasks:
            raise ValueError(f"Task {task_id} already exists.")

        def task_wrapper():
            try:
                await task_func()
            except asyncio.CancelledError:
                print(f"Task {task_id} was cancelled.")
            except Exception as e:
                print(f"Error in task {task_id}: {e}")
                #TODO: log the error
            finally:
                del self._tasks[task_id]

        self._tasks[task_id] = asyncio.create_task(task_func())


    def task_cancel(self, task_id):
        """Cancel a registered task.

        Cancel the task identified by the given task_id. This method serves as a stub and must be overridden by subclasses to implement task cancellation. Calling this method directly will raise a NotImplementedError.

        Args:
            task_id: The identifier of the task to cancel.
        """
        if task_id not in self._tasks:
            raise ValueError(f"Task {task_id} does not exist.")

        self._tasks[task_id].cancel()

    async def task_sleep_s(self, s):
        """Asynchronously sleep for the specified number of seconds.

        This abstract method must be implemented by subclasses to pause
        execution asynchronously for the given duration.

        Args:
            s (int | float): The sleep duration in seconds.
        """
        await asyncio.sleep(s)

    async def task_sleep_ms(self, ms):
        """
        Asynchronously pause execution for a specified number of milliseconds.

        This coroutine should suspend execution for the provided duration. Subclasses
        must override this method to implement the actual sleep behavior.

        Args:
            ms: Duration to sleep in milliseconds.
        """
        await asyncio.sleep_ms(ms)
