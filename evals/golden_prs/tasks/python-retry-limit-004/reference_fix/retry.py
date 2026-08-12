def run_with_retry(operation, max_attempts):
    last_error = None
    for _ in range(max_attempts):
        try:
            return operation()
        except TimeoutError as error:
            last_error = error
    raise last_error
