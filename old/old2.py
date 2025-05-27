## AFFORDANCE HANDLER INTERFACE ##
class EmbeddedCoreExt:
    def create_property(self, property_name, initial_value, default_setter=True**_):
        # Create a new property with the specified name and initial value.

        # Args:
        #    property_name: Name of the property to create
        #    initial_value: Initial value for the property
        #    net_ro: If True, property is read-only over the network (default: False)
        #    **_: Additional keyword arguments (ignored)

        # Raises:
        #    ValueError: If property already exists

        # Interface: AffordanceHandler
        if property_name in self._properties:
            # self.log(
            #     f"Property {property_name} already exists. Set a new value using 'set_property'",
            #     LogLevels.WARN,
            # )
            return

        self._properties[property_name] = initial_value

    def get_property(self, property_name, **_):
        # Get the value of a property by name.

        # Args:
        #    property_name: Name of the property to retrieve
        #    **_: Additional keyword arguments (ignored)

        # Returns:
        #    Current value of the property

        # Raises:
        #    ValueError: If property does not exist

        # Interface: AffordanceHandler
        if property_name not in self._properties:
            # self.log(
            #     f"Property {property_name} does not exist. Create it using 'create_property' method.",
            #     LogLevels.WARN,
            # )
            return

        return self._properties[property_name]

    def set_property(
        self,
        property_name,
        value,
        retain,
        qos,
    ):
        # Set the value of a property.

        # Updates a property value and publishes it via MQTT. For dict values,
        # merges with the existing property value.

        # Args:
        #    property_name: Name of the property to update
        #    value: New value for the property
        #    retain: MQTT retain flag (default: True)
        #    qos: MQTT QoS level (default: 0)
        #    **_: Additional keyword arguments (ignored)

        # Raises:
        #    ValueError: If property does not exist or has type mismatch

        # Interface: AffordanceHandler
        if property_name not in self._properties:
            return

        if not isinstance(value, type(self._properties[property_name])):
            # TODO: Log the error
            # self.log(
            #     f"Error setting: '{app}/{property_name}' expected {type(self._properties[property_name]).__name__} type, got {type(value).__name__}",
            #     LogLevels.ERROR,
            # )
            return

        if isinstance(value, dict):
            self._properties[property_name].update(value)
        else:
            self._properties[property_name] = value

        topic = f"{self._base_property_topic}/app/{property_name}"
        payload = json.dumps(value)

        try:
            self._publish(topic, payload, retain, qos)
        except Exception as e:
            pass
            # self.log(f"Failed to publish property update: {e}", LogLevels.ERROR)

    def emit_event(
        self,
        event_name,
        event_data,
        retain,
        qos,
        *_,
    ):
        # Emit an event with the specified name and data.

        # Publishes an event to the MQTT broker with the given payload.

        # Args:
        #    event_name: Name of the event to emit
        #    event_data: Data payload for the event
        #    retain: MQTT retain flag (default: False)
        #    qos: MQTT QoS level (default: 0)
        #    **_: Additional keyword arguments (ignored)

        # Interface: AffordanceHandler

        # TODO: sanitize event names
        topic = f"{self._base_event_topic}/app/{event_name}"
        payload = json.dumps(event_data)
        self._publish(topic, payload, retain, qos)

    def sync_executor(func):
        # Decorator for synchronous task or action executors.

        # Wraps a synchronous function to make it compatible with async execution.

        # Args:
        #    func: Synchronous function to wrap

        # Returns:
        #    Async-compatible wrapped function

        # Interface: AffordanceHandler

        # Wrap the function in an async wrapper
        # So it can be executed as a coroutine
        async def wrapper(self, *args, **kwargs):
            return func(self, *args, **kwargs)

        return wrapper

    def async_executor(func):
        # Decorator for asynchronous task or action executors.

        # Ensures proper async execution for functions that are already coroutines.

        # Args:
        #    func: Async function to wrap

        # Returns:
        #    Properly wrapped async function

        # Interface: AffordanceHandler

        async def wrapper(self, *args, **kwargs):
            return await func(self, *args, **kwargs)

        return wrapper

    def register_action_executor(
        self, action_name, action_func, qos=ACTION_SUBSCRIPTION_QOS, **_
    ):
        # Register a new action handler.

        # Associates an action name with a function and subscribes to the
        # corresponding MQTT topic.

        # Args:
        #    action_name: Name of the action to register
        #    action_func: Function to execute when the action is triggered
        #    qos: MQTT QoS level (default: 0)
        #    **_: Additional keyword arguments (ignored)

        # Raises:
        #    ValueError: If action already exists

        # Interface: AffordanceHandler
        if action_name in self._actions:
            return
            # self.log(f"Action {action_name} already exists.", LogLevels.WARN)

        # TODO: sanitize action names
        self._actions[action_name] = action_func

        self._mqtt.subscribe(f"{self._base_action_topic}/app/{action_name}", qos=qos)

    # async def _builtin_action_set_property(self, action_input):
    #     """
    #     Set the value of a property.
    #
    #     Args:
    #         action_input: Dictionary containing property name and value
    #
    #     Raises:
    #         ValueError: If property is read-only or input is malformed
    #
    #     Interface: AffordanceHandler
    #     """
    #     property_name = action_input.get("pname")
    #     property_value = action_input.get("pval")
    #     if property_name is None or property_value is None:
    #         # self.log(
    #         #     "Malformed set_property action input. Input template: {'pname': 'name_string', 'pval': Any}",
    #         #     LogLevels.ERROR,
    #         # )
    #         return
    #
    #     if property_name in self._net_ro_props:
    #         # self.log(f"Property {property_name} is read-only.", LogLevels.WARN)
    #         return
    #
    #     self.set_property(property_name, property_value)
