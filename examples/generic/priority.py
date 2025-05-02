"""
This is a simple application that simulates a sensor that generates random values.
The sensor value is then sent to different topics based on the priority of the sensor.
The priority of the sensor is set by the user and can be "low", "medium", or "high".
Setting the priority is handled by the user through the CityLink Embedded Core API (topic is mqtt://{...}/actions/setPriority)
"""

from citylink import EmbeddedCore
from random import randint


@EmbeddedCore.sync_executor
def simulate_random_sensor(ssa: EmbeddedCore) -> None:
    """
    Simulate a sensor reading and trigger a corresponding event.

    This asynchronous function generates a random integer between 0 and 100 to represent a sensor value.
    It retrieves the current priority level from the properties dictionary and triggers an event on the topic
    formatted as 'sensor_value/<priority>_prio', where <priority> may be 'low', 'medium', or 'high'.
    """

    sensor_value = randint(0, 100)
    priority = ssa.get_property("priority")
    ssa.emit_event(
        f"sensor_value/{priority}_prio", sensor_value
    )  # "low_prio", "medium_prio", "high_prio"


@EmbeddedCore.App()
def main(ssa: EmbeddedCore):
    """
    Initializes the sensor simulation application.

    Creates a default "priority" property set to "low" and registers the sensor
    simulation task to generate sensor values. Valid priority values include
    "low", "medium", and "high".
    """
    ssa.create_property("priority", "low")  # "low", "medium", "high"
    ssa.task_create("sensor_sim", simulate_random_sensor, 1000)
