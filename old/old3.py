## TASK SCHEDULER INTERFACE ##
class EmbeddedCoreExt:
    def __init__(self):
        # Initialize the EmbeddedCoreExt class.

        # This class provides an interface for scheduling tasks and managing properties.

        # Attributes:
        #    _tasks: A dictionary to store active tasks, where the key is the task ID and
        #            the value is the asyncio Task object.
        self._tasks = {}

    def task_create(self, task_id, task_func, period_ms=0):
        # Register a task for execution.

        # Associates a unique task identifier with a callable that encapsulates the task's logic.

        # Args:
        #    task_id: A unique identifier for the task. Must be unique across all active tasks.
        #    task_func: A callable implementing the task's functionality. Should be an async
        #              function that accepts 'self' as its parameter.
        #    period_ms: The period in milliseconds between consecutive executions of the task.
        #              If set to 0 (default), the task will execute only once (one-shot task).
        #              If greater than 0, the task will execute repeatedly at the specified interval.

        # Raises:
        #    ValueError: If a task with the specified task_id already exists.

        # Notes:
        #    - For periodic tasks, the time spent executing the task is considered when
        #      calculating the next execution time.
        #    - Tasks are automatically removed from the registry when they exit or raise
        #      an unhandled exception.
        if task_id in self._tasks:
            # self.log(f"Task {task_id} already exists.", LogLevels.WARN)
            return

        async def try_wrapper():
            try:
                await task_func(self)
            except asyncio.CancelledError:
                # self.log(f"Task {task_id} was cancelled.", LogLevels.INFO)
                return True
            except Exception as e:
                # self.log(f"Task {task_id} failed: {e}", LogLevels.ERROR)
                return True

            return False

        async def task_wrapper():
            while True:
                start_time = ticks_ms()

                should_exit = await try_wrapper()

                elapsed_time = ticks_diff(ticks_ms(), start_time)
                await self.task_sleep_ms(max(0, period_ms - elapsed_time))

                if should_exit:
                    break

            del self._tasks[task_id]

        if period_ms == 0:
            asyncio.create_task(try_wrapper())  # One shot task
        else:
            self._tasks[task_id] = asyncio.create_task(task_wrapper())

    def task_cancel(self, task_id):
        # Cancel a registered task.

        # Cancels the task identified by the given task_id.

        # Args:
        #    task_id: The identifier of the task to cancel.

        # Raises:
        #    ValueError: If task with the specified ID does not exist
        if task_id not in self._tasks:
            raise ValueError(f"Task {task_id} does not exist.")

        self._tasks[task_id].cancel()

    async def task_sleep_s(self, s):
        # Asynchronously sleep for the specified number of seconds.

        # Args:
        #    s (int | float): The sleep duration in seconds.
        await asyncio.sleep(s)

    async def task_sleep_ms(self, ms):
        # Asynchronously pause execution for a specified number of milliseconds.

        # Args:
        #    ms: Duration to sleep in milliseconds.
        await asyncio.sleep_ms(ms)
