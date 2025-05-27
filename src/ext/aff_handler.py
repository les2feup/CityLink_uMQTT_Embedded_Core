import json
import asyncio


## AFFORDANCE HANDLER INTERFACE ##
class EmbeddedCoreExt:
    __static__ = ["sync_executor", "async_executor"]

    def create_property(self, property_name, initial_value, pub_only=True, **_):
        if property_name in self._properties:
            return

        if not pub_only:
            self._assign_default_setter(property_name)

        self._properties[property_name] = initial_value

    def get_property(self, property_name, **_):
        if property_name not in self._properties:
            return

        return self._properties[property_name]

    def set_property(
        self,
        property_name,
        value,
        retain=True,
        qos=0,
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
        retain=False,
        qos=0,
        **_,
    ):
        # TODO: sanitize event names
        topic = f"{self._base_event_topic}/app/{event_name}"
        payload = json.dumps(event_data)
        self._publish(topic, payload, retain, qos)

    def register_action_executor(self, action_name, action_func, qos=0, **_):
        if action_name in self._actions:
            return

        # TODO: sanitize action names
        self._actions[f"app/{action_name}"] = action_func
        self._mqtt.subscribe(f"{self._base_action_topic}/app/{action_name}", qos=qos)

    def sync_executor(func):
        async def wrapper(self, *args, **kwargs):
            return func(self, *args, **kwargs)

        return wrapper

    def async_executor(func):
        async def wrapper(self, *args, **kwargs):
            return await func(self, *args, **kwargs)

    def _assign_default_setter(self, property_name):
        async def default_set_action(self, action_input):
            self.set_property(property_name, action_input)

        self.register_action_executor(
            f"set/{property_name}",
            default_set_action,
            qos=0,
        )
