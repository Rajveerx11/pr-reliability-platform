def run_with_retry(operation, max_attempts):
    for _ in range(max_attempts + 1):
        try:
            return operation()
        except TimeoutError:
            pass
    raise TimeoutError("attempts exhausted")
