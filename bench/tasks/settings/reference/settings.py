SETTINGS = {"theme": "dark", "font_size": "12"}


def get_setting(name, default=None):
    """The value of a setting, as a string, or ``default`` when it is missing."""
    return SETTINGS.get(name, default)


def get_int(name, default=0):
    """The value of a setting as an int, or ``default`` when it is missing."""
    value = SETTINGS.get(name)
    return default if value is None else int(value)
