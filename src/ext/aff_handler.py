class EmbeddedCoreExt:
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
            self.log(
                f"Property {property_name} already exists. Set a new value using 'set_property'",
                LogLevels.WARN,
            )
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
            self.log(
                f"Property {property_name} does not exist. Create it using 'create_property' method.",
                LogLevels.WARN,
            )
            return

        return self._properties[property_name]

    def _set_property(
        self,
        property_name,
        value,
        retain,
        qos,
        core_prop=False,
    ):
        map = None
        namespace = None

        if core_prop and property_name in self._builtin_properties:
            map = self._builtin_properties
            namespace = RT_NAMESPACE
        if not core_prop and property_name in self._properties:
            map = self._properties
            namespace = APP_NAMESPACE

        if map is None or namespace is None:
            # TODO: Log the error
            self.log(f"Property {property_name} does not exist.", LogLevels.WARN)
            return

        if not isinstance(value, type(map[property_name])):
            # TODO: Log the error
            self.log(
                f"Error setting: '{namespace}/{property_name}' expected {type(map[property_name]).__name__} type, got {type(value).__name__}",
                LogLevels.ERROR,
            )
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

    def set_property(
        self,
        property_name,
        value,
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
        self._set_property(property_name, value, retain, qos, False)

    def _emit_event(
        self,
        event_name,
        event_data,
        retain,
        qos,
        core_event=False,
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
        namespace = RT_NAMESPACE if core_event else APP_NAMESPACE
        topic = f"{self._base_event_topic}/{namespace}/{event_name}"
        payload = self._serializer.dumps(event_data)
        self._publish(topic, payload, retain=retain, qos=qos)

    def emit_event(
        self,
        event_name,
        event_data,
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
        self._emit_event(event_name, event_data, retain, qos, False)

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

        # TODO: get rid of / refactor the namespace stuff
        self._mqtt.subscribe(
            f"{self._base_action_topic}/{APP_NAMESPACE}/{action_name}", qos=qos
        )
