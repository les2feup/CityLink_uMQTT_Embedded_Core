def _check_template(template, provided, path="config"):
    """
    Validates that the given configuration matches the expected template.

    This function recursively verifies that all required keys specified in the template are present
    in the provided configuration and that their values are of the expected types. If the expected
    type for a key is a dictionary, the corresponding sub-configuration is recursively validated.

    Parameters:
        template: A dictionary mapping keys to expected types or nested templates.
        provided: The configuration dictionary to validate.
        path: The current path in the configuration (used in error messages), defaults to "config".

    Raises:
        ValueError: If the provided configuration is not a dictionary or if a required key is missing.
        TypeError: If a configuration value does not match the expected type.
    """
    if not isinstance(provided, dict):
        raise ValueError(f"{path} must be a dictionary")

    for key, expected_type in template.items():
        if key not in provided:
            raise ValueError(f"Missing required key: {path}['{key}']")

        if isinstance(expected_type, dict):
            _check_template(expected_type, provided[key], f"{path}['{key}']")

        elif not isinstance(provided[key], expected_type):
            raise TypeError(
                f"Expected {path}['{key}'] to be {expected_type.__name__}, "
                f"but got {type(provided[key]).__name__}"
            )


def _validate_configuration(config):
    configuration_template = {
        "tm": {
            "name": str,
            "href": str,
            "version": {
                "model": str,
                "instance": str,
            },
        },
        "network": {
            "ssid": str,
            "password": str,
        },
        "runtime": {
            "broker": {
                "hostname": str,
            },
            "connection": {
                "retries": int,
                "timeout_ms": int,
            },
        },
    }

    _check_template(configuration_template, config)


def load_configuration(config_dir, config_open_mode, serializer):
    import os

    root_dir = os.getcwd()
    if root_dir != config_dir:
        os.chdir(config_dir)

    config = {}
    for file in os.listdir():
        try:
            with open(file, config_open_mode) as f:
                config.update(serializer.load(f))
        except Exception as e:
            raise Exception(f"[ERROR] Failed to load configuration file: {e}") from e

    if root_dir != os.getcwd():
        os.chdir(root_dir)

    _validate_configuration(config)

    return config


def write_configuration(config, config_dir, config_open_mode, serializer):
    import os

    root_dir = os.getcwd()
    if root_dir != config_dir:
        os.chdir(config_dir)

    for file in os.listdir():
        try:
            with open(file, config_open_mode) as f:
                serializer.dump(config, f)
        except Exception as e:
            raise Exception(f"[ERROR] Failed to write configuration file: {e}") from e

    if root_dir != os.getcwd():
        os.chdir(root_dir)
