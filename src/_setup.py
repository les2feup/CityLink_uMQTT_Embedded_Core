from umqtt.simple import MQTTClient


def mqtt_setup(client_id, rt_namespace, mqtt_client, on_message):

    base_topic = f"ssa/{client_id}"
    base_event_topic = f"{base_topic}/events"
    base_action_topic = f"{base_topic}/actions"
    base_property_topic = f"{base_topic}/properties"

    mqtt_client.set_callback(on_message)

    mqtt_client.subscribe(f"{base_action_topic}/{rt_namespace}/vfs/list", qos=2)
    mqtt_client.subscribe(f"{base_action_topic}/{rt_namespace}/vfs/read", qos=2)
    mqtt_client.subscribe(f"{base_action_topic}/{rt_namespace}/vfs/write", qos=2)
    mqtt_client.subscribe(f"{base_action_topic}/{rt_namespace}/vfs/delete", qos=2)
    mqtt_client.subscribe(f"{base_action_topic}/{rt_namespace}/reload", qos=2)
    mqtt_client.subscribe(f"{base_action_topic}/{rt_namespace}/set_property", qos=2)

    return (base_event_topic, base_action_topic, base_property_topic)


def initialize_mqtt_client(client_id, broker_config):
    client = MQTTClient(
        client_id=client_id,
        server=broker_config["hostname"],
        port=broker_config.get("port", 1883),
        user=broker_config.get("username"),
        password=broker_config.get("password"),
        keepalive=broker_config.get("keepalive", 0),
        ssl=broker_config.get("ssl"),
    )

    return client


def init_wlan(network_config):
    import network

    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    wlan.connect(network_config["ssid"], network_config["password"])

    return wlan
