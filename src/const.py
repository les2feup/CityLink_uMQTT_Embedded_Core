from micropython import const
from time import gmtime
import json

from ._log import LogLevels

DEFAULT_LOG_LEVEL = LogLevels.TRACE

APP_NAMESPACE = const("app")
RT_NAMESPACE = const("umqtt_core")
PING_TIMEOUT_MS = const(60000)
REGISTRATION_PUBLISH_INTERVAL_MS = const(5000)  # 5 seconds
POLLING_INTERVAL_MS = const(500)  # 0.5 seconds

MQTT_TOPIC_ENCODING = const("utf-8")
DEFAULT_MQTT_PORT = const(1883)
DEFAULT_MQTT_QOS = const(0)
DEFAULT_MQTT_RETAIN = const(False)
DEFAULT_MQTT_CLEAN_SESSION = const(True)
DEFAULT_MQTT_KEEPALIVE = const(120)

REGISTRATION_RETAIN = const(False)
REGISTRATION_QOS = const(1)

PROPERTY_RETAIN = const(True)
PROPERTY_QOS = const(DEFAULT_MQTT_QOS)

EVENT_RETAIN = const(False)
EVENT_QOS = const(DEFAULT_MQTT_QOS)

CORE_SUBSCRIPTION_QOS = const(2)
ACTION_SUBSCRIPTION_QOS = const(DEFAULT_MQTT_QOS)

DEFAULT_CON_RETRIES = const(3)
DEFAULT_CON_TIMEOUT_MS = const(5000)

EPOCH_YEAR = gmtime(0)[0]

DEFAULT_FOPEN_MODE = const("r")
DEFAULT_SERIALIZER = json

STATUS_UNDEFINED = const("UNDEF")
STATUS_ADAPTING = const("ADAPT")
STATUS_OK = const("OK")
