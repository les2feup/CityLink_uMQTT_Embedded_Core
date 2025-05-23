## AFFORDANCE HANDLER INTERFACE ##
class EmbeddedCoreExt:
    def create_property(self, property_name, initial_value, default_setter=True, **_):
        if property_name in self._properties:
            return

        self._properties[property_name] = initial_value

    def get_property(self, property_name, **_):
        if property_name not in self._properties:
            return

        return self._properties[property_name]

    def set_property(
        self,
        property_name,
        value,
        retain,
        qos,
        **_,
    ):
        if property_name not in self._properties:
            return

        if not isinstance(value, type(self._properties[property_name])):
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

    def emit_event(
        self,
        event_name,
        event_data,
        retain,
        qos,
        **_,
    ):
        # TODO: sanitize event names
        topic = f"{self._base_event_topic}/app/{event_name}"
        payload = json.dumps(event_data)
        self._publish(topic, payload, retain, qos)

    def sync_executor(func):
        async def wrapper(self, *args, **kwargs):
            return func(self, *args, **kwargs)

        return wrapper

    def async_executor(func):
        async def wrapper(self, *args, **kwargs):
            return await func(self, *args, **kwargs)

        return wrapper

    def register_action_executor(self, action_name, action_func, qos=0, **_):
        if action_name in self._actions:
            return

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
