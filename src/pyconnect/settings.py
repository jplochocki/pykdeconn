import re
from typing import Optional, Any, List, Dict, ClassVar
import datetime
import logging
import logging.config
import socket
from pathlib import Path
import json
import sys
import uuid

from pydantic import (
    BaseModel,
    BaseSettings,
    Field,
    validator,
    PydanticValueError,
    IPvAnyAddress,
    PastDate,
)

from .protocol.packets import IdentityPacket, generate_IdentityPacket
from .utils import runnin_in_pytest
from .sslutils import generate_cert


logging.config.dictConfig(
    {
        'version': 1,
        'disable_existing_loggers': True,
        'loggers': {
            'pyconnect.server': {
                'level': 'DEBUG',
                'handlers': ['detailed_console_handler'],
                'propagate': True,
            }
        },
        'handlers': {
            'detailed_console_handler': {
                'level': 'DEBUG',
                'class': 'logging.StreamHandler',
                'formatter': 'detailed_console_handler_fmt',
            }
        },
        'formatters': {
            'detailed_console_handler_fmt': {
                'class': 'pyconnect.utils.CustomConsoleFormatter'
            }
        },
    }
)


class InvalidPEMFormatError(PydanticValueError):
    code = 'invalid_PEM_format'
    message = 'Invalid PEM Certificate format'


class DeviceConfig(BaseModel):
    """
    KDE Connect device information and config.
    """

    device_id: str
    device_name: str

    certificate_PEM: str = ''

    @validator('certificate_PEM')
    def cert_str_format_or_empty(cls, value: str) -> str:
        if value == '':
            return value
        if not re.match(
            (
                r'^-----BEGIN CERTIFICATE-----\n[A-Za-z0-9\+\/=]+\n'
                r'-----END CERTIFICATE-----\n?'
            ),
            value,
        ):
            raise InvalidPEMFormatError(pem_value=value)

    paired: bool = False
    last_ip: Optional[IPvAnyAddress] = None
    last_connection_date: PastDate = Field(
        default_factory=lambda: datetime.datetime.now
    )

    incoming_capabilities: List[str] = []
    outgoing_capabilities: List[str] = []

    _devices_cache: ClassVar = {}

    @classmethod
    def load_from_id_packet(cls, pack: IdentityPacket):
        """
        Loads (or create) ``DeviceConfig`` from ``kdeconnect.identity`` packet.
        """
        if pack.body.deviceId in DeviceConfig._devices_cache:
            return DeviceConfig._devices_cache[pack.body.deviceId]

        dev = cls(
            device_id=pack.body.deviceId,
            device_name=pack.body.deviceName,
        )
        dev.incoming_capabilities = pack.body.incomingCapabilities.copy()
        dev.outgoing_capabilities = pack.body.outgoingCapabilities.copy()

        return dev


class PyConnectSettingsSourceDir(BaseSettings):
    """
    Settings directory (default or from environmet variables).
    """

    config_dir: Path = Field(
        env='PYCONNECT_CONFIG_DIR',
        default_factory=lambda: PyConnectSettingsSourceDir.config_dir_default_factory(),  # noqa
    )

    @staticmethod
    def config_dir_default_factory() -> Path:
        """
        Creates config directory path (``~/.config/pyconnect`` if ``~/.config``
        exists or ``~/.pyconnect`` else). If we're running in pytest
        environment - return tests/config path.
        """
        if runnin_in_pytest():
            # We are trying to determine the path to the tests/ directory.
            # Expected test calls python -m pytest -s ../tests/ and CWD was
            # venv dir
            tests_dir = ([i for i in sys.argv if 'tests' in i] + [''])[0]
            tests_dir = (Path('.') / tests_dir).resolve()
            if tests_dir.exists():
                return tests_dir / 'config'

        if Path('~/.config').expanduser().exists():
            return Path('~/.config/pyconnect').expanduser()
        else:
            return Path('~/.pyconnect').expanduser()

    @validator('config_dir')
    def create_dir_if_not_exists(cls, p: Path) -> Path:
        if not p.exists():
            p.mkdir(parents=True, exist_ok=True)
        elif not p.is_dir:
            raise ValueError('Path does not point to a directory.')
        return p


config_source_dir = PyConnectSettingsSourceDir()


def json_config_settings_source(settings: BaseSettings) -> Dict[str, Any]:
    """ """
    cfg_path = config_source_dir.config_dir / 'config.json'
    encoding = settings.__config__.env_file_encoding
    if cfg_path.exists():
        return json.loads(cfg_path.read_text(encoding))
    else:
        return {}


class PyConnectSettings(BaseSettings):
    device_id: str = Field(
        default_factory=lambda: uuid.uuid4().urn.replace('urn:uuid:', ''),
    )
    device_name: str = Field(default_factory=socket.gethostname)
    device_type: str = 'laptop'
    incoming_capabilities: List[str] = []
    outgoing_capabilities: List[str] = []

    device_certfile: Optional[Path] = None
    device_keyfile: Optional[Path] = None

    @validator('device_certfile', always=True)
    def device_certfile_gen_path(cls, v, values):
        if isinstance(v, Path):
            return v
        else:
            dev_name = re.sub(
                r'[^a-z0-9\-]', '', values['device_name'].lower()
            )
            return config_source_dir.config_dir / f'certificate-{dev_name}.pem'

    @validator('device_keyfile', always=True)
    def device_keyfile_gen_path_and_cert(cls, v, values):
        device_keyfile = v
        if not isinstance(device_keyfile, Path):
            dev_name = re.sub(
                r'[^a-z0-9\-]', '', values['device_name'].lower()
            )
            device_keyfile = (
                config_source_dir.config_dir / f'private-{dev_name}.pem'
            )

        # create certs files if don't exists
        device_certfile = values['device_certfile']
        if not device_keyfile.exists() or not device_certfile.exists():
            generate_cert(
                values['device_name'],
                values['device_id'],
                device_certfile,
                device_keyfile,
            )

        return device_keyfile

    class Config:
        env_prefix = 'PYCONNECT_'
        env_file_encoding = 'utf-8'

        @classmethod
        def customise_sources(
            cls,
            init_settings,
            env_settings,
            file_secret_settings,
        ):
            return (
                init_settings,
                env_settings,
                json_config_settings_source,
                file_secret_settings,
            )


config = PyConnectSettings()


def generate_my_identity() -> IdentityPacket:
    """
    Generates ID packet for this device.
    """
    return generate_IdentityPacket(
        device_id=config.device_id,
        device_name=config.device_name,
        incoming_capabilities=config.incoming_capabilities,
        outgoing_capabilities=config.outgoing_capabilities,
    )
