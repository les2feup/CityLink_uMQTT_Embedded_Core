from ssa import SSACore
from ssa.interfaces import Serializer

from micropython import const
import json

RT_NAMESPACE = "umqtt_core"


class uMQTTCore(SSACore):
    def __init__(
        self, config_dir, config_open_mode="r", serializer: Serializer = json
    ):
        self._config_dir = config_dir
        self._config_open_mode = config_open_mode
        self._serializer = serializer

        self._tasks = {}
        self._actions = {}
        self._properties = {}

        self._builtin_actions = const({
                "vfs/list": self._builtin_action_vfs_list,
                "vfs/read": self._builtin_action_vfs_read,
                "vfs/write": self._builtin_action_vfs_write,
                "vfs/delete": self._builtin_action_vfs_delete,
                "reload": self._builtin_action_reload_core,
                })


    def _load_config(self):
        """
        Interface: SSACore

        Load the configuration from the specified directory.
        """
        from ._config import load_configuration

        self.config = load_configuration(
            self._config_dir, self._config_open_mode, self._serializer
        )

        self.id = self.config.get("id")
        if self.id is None:
            from machine import unique_id
            from binascii import hexlify

            self.id = hexlify(unique_id()).decode("utf-8")

    def _write_config(self, update_dict):
        """
        Interface: SSACore

        Write the configuration to the specified directory.
        """
        self.config.update(update_dict)

        from ._config import write_configuration

        write_configuration(
            self.config, self._config_dir, self._config_open_mode, self._serializer
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

        Disconnect from the network.
        """
        self._mqtt.disconnect()
        self._wlan.disconnect()

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
        self._mqtt.wait_msg() # Blocking wait for registration ack

    def SSACoreEntry(*args, **kwargs):
        """
        Interface: SSACore

        Decorator to mark the entry point for the SSACore.
        """

        def main_decorator(main_func):
            """Decorates the main function of the SSACore."""

            def main_wrapper(self):
                """Wrapper for the main function of the SSACore."""
                raise NotImplementedError("Subclasses must implement SSACoreEntry()")

            return main_wrapper

        return main_decorator

    def _wrapp_builtin_action(action_func):
        def builtin_action_wrapper(self, action_input):
            try:
                action_result = action_func(self, action_input)
                action_result = self._serializer.dumps(action_result)
                self._mqtt.publish(f"{self._base_action_topic}/", action_result)
            except Exception as e:
                # TODO: Change this to comply with the SSA spec
                self._mqtt.publish(f"ssa/{self.id}/error", str(e))

        return builtin_action_wrapper

    @_wrapp_builtin_action
    def _builtin_action_vfs_read(self, action_input):
        """
        Interface: SSACore
        Read the contents of a file from the virtual file system."""
        raise NotImplementedError("Subclasses must implement builtin_action_vfs_read()")

    @_wrapp_builtin_action
    def _builtin_action_vfs_write(self, action_input):
        """
        Interface: SSACore
        Write data to a file in the virtual file system."""
        raise NotImplementedError("Subclasses must implement builtin_action_vfs_write()")

    @_wrapp_builtin_action
    def _builtin_action_vfs_list(self, action_input):
        """
        Interface: SSACore
        List the contents of the virtual file system."""
        raise NotImplementedError("Subclasses must implement builtin_action_vfs_list()")

    @_wrapp_builtin_action
    def _builtin_action_vfs_delete(self, action_input):
        """
        Interface: SSACore
        Delete a file from the virtual file system."""
        raise NotImplementedError("Subclasses must implement builtin_action_vfs_delete()")

    def _builtin_action_reload_core(self, _):
        """
        Interface: SSACore
        Reload the core module."""
        raise NotImplementedError("Subclasses must implement builtin_action_reload_core()")

    ## AFFORDANCE HANDLER INTERFACE ##

    def create_property(self, property_name, initial_value, **_):
        """
        Interface: AffordanceHandler

        Create a new property with the specified name and initial value.
        """
        if property_name in self._properties:
            raise ValueError(f"Property {property_name} already exists. Set a new value using 'set_property' method.")

        #TODO: sanitize property names

        self._properties[property_name] = initial_value

    def get_property(self, property_name, **_):
        """
        Interface: AffordanceHandler

        Get the value of a property by name.
        """
        if property_name not in self._properties:
            raise ValueError(f"Property {property_name} does not exist. Create it using 'create_property' method.")

        return self._properties[property_name]

    def set_property(self, property_name, value, retain=True, qos=0, **_):
        """
        Interface: AffordanceHandler

        Set the value of a property.
        """
        if property_name not in self._properties:
            raise ValueError(f"Property {property_name} does not exist. Create it using 'create_property' method.")

        self._properties[property_name] = value

        try: 
            namespace = self.config["tm"]["name"]
            topic = f"{self._base_property_topic}/{namespace}/{property_name}"
            payload = self._serializer.dumps(value)
            self._mqtt.publish(topic, payload, retain=retain, qos=qos)
        except Exception as e:
            #TODO: publish the error
            raise ValueError(f"Failed to set property {property_name}: {e}") from e

    def emit_event(self, event_name, event_data, retain=False, qos=0 **_):
        """
        Interface: AffordanceHandler

        Emit an event with the specified name and data.
        """
        #TODO: sanitize event names
        namespace = self.config["tm"]["name"]
        topic = f"{self._base_event_topic}/{namespace}/{event_name}"
        payload = self._serializer.dumps(event_data)
        self._mqtt.publish(topic, payload, retain=retain, qos=qos)

    def register_action_handler(self, action_name, action_func, qos=0, **_):
        """
        Interface: AffordanceHandler

        Register a new action handler.
        """
        if action_name in self._actions:
            raise ValueError(f"Action {action_name} already exists.")
        self._actions[action_name] = action_func #TODO: support uri variables
        
        namespace = self.config["tm"]["name"]
        self._mqtt.subscribe(f"{self._base_action_topic}/{namespace}/{action_name}", qos=qos)

    def _get_action_handler(self, action_name, **_):
        """
        Interface: AffordanceHandler

        Get the callback function for an action.
        """
        if action_name not in self._actions:
            raise ValueError(f"Action {action_name} does not exist.")

        return self._actions[action_name]

    def _invoke_action(self, action_name, action_input, **_):
        """
        Interface: AffordanceHandler

        Invoke an action with the specified input.
        """
        action_handler = self._get_action_handler(action_name)
        self.create_task(action_handler(action_input))




