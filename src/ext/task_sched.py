## TASK SCHEDULER INTERFACE ##
class EmbeddedCoreExt:
    def task_create(self, task_id, task_func, period_ms=0):
        if task_id in self._tasks:
            return

        async def try_wrapper():
            try:
                await task_func(self)
            except asyncio.CancelledError:
                return True
            except Exception as e:
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
        if task_id not in self._tasks:
            raise ValueError(f"Task {task_id} does not exist.")

        self._tasks[task_id].cancel()

    async def task_sleep_s(self, s):
        await asyncio.sleep(s)

    async def task_sleep_ms(self, ms):
        await asyncio.sleep_ms(ms)
