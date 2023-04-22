import logging
import sys
from pathlib import Path


class CustomConsoleFormatter(logging.Formatter):
    grey = '\x1b[38;20m'
    yellow = '\x1b[33;20m'
    red = '\x1b[31;20m'
    bold_red = '\x1b[31;1m'
    green = '\x1b[32m'
    reset = '\x1b[0m'
    format = (
        '[ %(levelname)s %(asctime)s %(funcName)20s()::%(lineno)d ]\n\t'
        '%(message)s'
    )

    FORMATS = {
        logging.DEBUG: grey + format + reset,
        logging.INFO: green + format + reset,
        logging.WARNING: yellow + format + reset,
        logging.ERROR: red + format + reset,
        logging.CRITICAL: bold_red + format + reset,
    }

    def format(self, record):
        log_fmt = self.FORMATS.get(record.levelno)
        formatter = logging.Formatter(log_fmt)
        return formatter.format(record)


def runnin_in_pytest() -> bool:
    """
    Checking if we're running in pytest.
    """
    p = Path(sys.argv[0])
    return p.name in ['pytest', 'py.test'] or p.match(
        '*/site-packages/pytest/__main__.py'
    )
