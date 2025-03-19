def with_exponential_backoff(func, retries, base_timeout_ms):
    """
    Attempts to execute a function with exponential backoff on failure.

    This function calls the provided callable, retrying up to the specified number of times.
    After each failure, it waits for an exponentially increasing delay starting at base_timeout_ms
    milliseconds. If the callable succeeds, its result is returned immediately; if all attempts fail,
    an Exception is raised.

    Args:
        func: The callable to execute.
        retries: The maximum number of retry attempts.
        base_timeout_ms: The initial wait time in milliseconds, which doubles after each attempt.

    Returns:
        The result of the callable if execution is successful.

    Raises:
        Exception: If the callable fails on all retry attempts.
    """
    from time import sleep_ms

    for i in range(retries):
        retry_timeout = base_timeout_ms * (2**i)
        print(f"[INFO] Trying {func.__name__} (attempt {i + 1}/{retries})")
        try:
            return func()
        except Exception as e:
            print(
                f"[ERROR] {func.__name__} failed: {e}, retrying in {retry_timeout} milliseconds"
            )
            sleep_ms(retry_timeout)

    raise Exception(f"[ERROR] {func.__name__} failed after {retries} retries")
