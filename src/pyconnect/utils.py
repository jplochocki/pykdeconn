import logging
import sys
from pathlib import Path
from typing import Dict, Any
import re
import json


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


def running_in_pytest() -> bool:
    """
    Checking if we're running in pytest.
    """
    p = Path(sys.argv[0])
    return p.name in ['pytest', 'py.test'] or p.match(
        '*/site-packages/pytest/__main__.py'
    )


def simple_toml_parser(txt: str) -> Dict[str, Any]:
    """
    Simple TOML (like) parser. dconf returns TOML-like string
    """
    res = {}
    last_section = None
    for ln in txt.splitlines():
        # sections
        if m := (re.match(r'^\s*\[\s*(.+)\s*\]\s*$', ln)):
            last_section = m.group(1)
            res[last_section] = {}

        # values
        elif m := (re.match(r'^\s*(?P<name>.+)\s*=\s*(?P<value>.+)$', ln)):
            name = m.group('name')
            value = m.group('value')

            # bool
            if m := (re.match(r'^\s*(true|false)\s*$', value, re.I)):
                res[last_section][name] = bool(
                    re.match('true', m.group(1), re.I)
                )

            # string
            elif m := (re.match(r'^\s*(\'|")(.+?)(\1)\s*$', value)):
                res[last_section][name] = m.group(2)

            # list of strings
            elif m := (re.match(r'^\s*(\[\s*.*?\s*\])\s*$', value)):
                res[last_section][name] = json.loads(
                    re.sub(r'(?<!\\)\'', '"', m.group(1)))

            # uint32
            elif m := (re.match(r'^\s*uint32\s+(\d+)\s*$', value, re.I)):
                res[last_section][name] = int(m.group(1))

            # just ignore anything you don't know
    return res
