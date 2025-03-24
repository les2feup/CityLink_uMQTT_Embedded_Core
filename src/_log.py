class LogLevel:
    def __init__(self, name, value):
        self.name = name
        self.value = value

    def __str__(self):
        return self.name

    def __repr__(self):
        return self.name

    def __eq__(self, other):
        return self.value == other.value

    def __gt__(self, other):
        return self.value > other.value

    def __lt__(self, other):
        return self.value < other.value


class LogLevels:
    TRACE = LogLevel("TRACE", 0)
    DEBUG = LogLevel("DEBUG", 1)
    INFO = LogLevel("INFO", 2)
    WARN = LogLevel("WARN", 3)
    ERROR = LogLevel("ERROR", 4)
    FATAL = LogLevel("FATAL", 5)

    @classmethod
    def __iter__(cls):
        return iter(
            [cls.TRACE, cls.DEBUG, cls.INFO, cls.WARN, cls.ERROR, cls.FATAL]
        )

    @classmethod
    def __getitem__(cls, item):
        return [cls.TRACE, cls.DEBUG, cls.INFO, cls.WARN, cls.ERROR, cls.FATAL][
            item
        ]

    @classmethod
    def __len__(cls):
        return 6

    @classmethod
    def __contains__(cls, item):
        return item in [
            cls.TRACE,
            cls.DEBUG,
            cls.INFO,
            cls.WARN,
            cls.ERROR,
            cls.FATAL,
        ]

    @classmethod
    def __str__(cls):
        return "LogLevels Enumeration"

    @classmethod
    def from_str(cls, name):
        if not isinstance(name, str):
            return None

        name = name.upper()
        for level in cls:
            if level.name == name:
                return level

        return None
