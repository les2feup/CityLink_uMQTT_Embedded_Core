from micropython import const

PING_TIMEOUT_MS = const(60000)
REGISTRATION_PUBLISH_INTERVAL_MS = const(10000)  # 10 seconds
POLLING_INTERVAL_MS = const(500)  # 0.5 seconds

DEFAULT_MQTT_PORT = const(1883)
DEFAULT_MQTT_KEEPALIVE = const(120)
DEFAULT_MQTT_CLEAN_SESSION = const(True)

OTAU_FILE = const("/otau.flag")
