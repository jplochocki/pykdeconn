import re
from typing import Optional, Any, List, Tuple, Dict, Literal, ClassVar
import datetime
import logging
import logging.config
import socket
from pathlib import Path
import json
import sys
import uuid
import ssl

from pydantic import (
    BaseModel,
    BaseSettings,
    Field,
    validator,
    PydanticValueError,
    IPvAnyAddress,
    PastDate,
    PrivateAttr,
)

# from anyio.streams.tls import TLSStream

from .protocol.packets import IdentityPacket, generate_IdentityPacket
from .utils import running_in_pytest

# from .sslutils import generate_cert, read_cert_common_name


logging.config.dictConfig(
    {
        'version': 1,
        'disable_existing_loggers': True,
        'loggers': {
            'pykdeconn.server': {
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
                'class': 'pykdeconn.utils.CustomConsoleFormatter'
            }
        },
    }
)


log = logging.getLogger('pykdeconn.server')


class InvalidPEMFormatError(PydanticValueError):
    code = 'invalid_PEM_format'
    msg_template = 'Invalid PEM Certificate format'


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
                r'^\s*-----BEGIN CERTIFICATE-----\n[A-Za-z0-9\+\/=\n]+\n'
                r'-----END CERTIFICATE-----\s*$'
            ),
            value,
        ):
            raise InvalidPEMFormatError(pem_value=value)

        return value

    paired: bool = False
    type_: str = 'phone'
    last_ip: Optional[IPvAnyAddress] = None
    last_connection_date: PastDate = Field(
        default_factory=lambda: datetime.datetime.now()
    )
    _connected: bool = PrivateAttr(False)
    _connection_ssock: Optional[Any] = PrivateAttr(None)  # TLSStream

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
        DeviceConfig._devices_cache[pack.body.deviceId] = dev

        dev.incoming_capabilities = pack.body.incomingCapabilities.copy()
        dev.outgoing_capabilities = pack.body.outgoingCapabilities.copy()

        return dev

    _ssl_cnx_cache: Dict[str, Any] = PrivateAttr(default_factory=dict)

    def ssl_context(
        self,
        purpose: ssl.Purpose = ssl.Purpose.CLIENT_AUTH,
        my_device_certfile: Optional[Path] = None,
        my_device_keyfile: Optional[Path] = None,
        renew: bool = False,
    ) -> ssl.SSLContext:
        """
        Loads ``SSLContext`` for the specified ``purpose``.
        """
        if not renew and purpose.shortname in self._ssl_cnx_cache:
            return self._ssl_cnx_cache[purpose.shortname]

        cnx = ssl.create_default_context(purpose)
        cnx.load_cert_chain(
            certfile=my_device_certfile, keyfile=my_device_keyfile
        )

        cnx.check_hostname = False
        cnx.verify_mode = ssl.CERT_NONE

        if self.certificate_PEM:
            cnx.load_verify_locations(cadata=self.certificate_PEM)
            cnx.verify_mode = ssl.CERT_REQUIRED

        self._ssl_cnx_cache[purpose.shortname] = cnx
        return cnx

    def save(self):
        pass


class PyKDEConnSettingsSourceDir(BaseSettings):
    """
    Settings directory (default or from environmet variables).
    """

    config_dir: Path = Field(
        env='PYKDECONN_CONFIG_DIR',
        default_factory=lambda: PyKDEConnSettingsSourceDir.config_dir_default_factory(),  # noqa
    )

    @staticmethod
    def config_dir_default_factory() -> Path:
        """
        Creates config directory path (``~/.config/pykdeconn`` if ``~/.config``
        exists or ``~/.pykdeconn`` else). If we're running in pytest
        environment - return tests/config path.
        """
        if running_in_pytest():
            # We are trying to determine the path to the tests/ directory.
            # Expected test calls python -m pytest -s ../tests/ and CWD was
            # venv dir
            tests_dir = ([i for i in sys.argv if 'tests' in i] + [''])[0]
            tests_dir = (Path('.') / tests_dir).resolve()
            if tests_dir.exists():
                return tests_dir / 'config'

        if Path('~/.config').expanduser().exists():
            return Path('~/.config/pykdeconn').expanduser()
        else:
            return Path('~/.pykdeconn').expanduser()

    @validator('config_dir')
    def create_dir_if_not_exists(cls, p: Path) -> Path:
        if not p.exists():
            p.mkdir(parents=True, exist_ok=True)
        elif not p.is_dir:
            raise ValueError('Path does not point to a directory.')
        return p


config_source_dir = PyKDEConnSettingsSourceDir()
config_file = config_source_dir.config_dir / 'config.json'


def json_config_settings_source(settings: BaseSettings) -> Dict[str, Any]:
    """
    Read settings from JSON file.
    """
    encoding = settings.__config__.env_file_encoding
    if config_file.exists():
        return json.loads(config_file.read_text(encoding))
    else:
        return {}


class PyKDEConnSettings(BaseSettings):
    log_level: Literal[
        logging.CRITICAL,
        logging.ERROR,
        logging.WARNING,
        logging.INFO,
        logging.DEBUG,
        'CRITICAL',
        'ERROR',
        'WARNING',
        'INFO',
        'DEBUG',
    ] = logging.DEBUG

    @validator('log_level', always=True)
    def log_level_set(cls, v):
        named_lvls = {
            'CRITICAL': logging.CRITICAL,
            'ERROR': logging.ERROR,
            'WARNING': logging.WARNING,
            'INFO': logging.INFO,
            'DEBUG': logging.DEBUG,
        }

        if (
            v
            not in [
                logging.CRITICAL,
                logging.ERROR,
                logging.WARNING,
                logging.INFO,
                logging.DEBUG,
            ]
            and v not in named_lvls.keys()
        ):
            raise ValueError(f'Invalid log_level value ({v}).')
        if v in named_lvls.keys():
            log.setLevel(named_lvls[v])
            return v
        else:
            log.setLevel(v)
            return [k for k, a in named_lvls.items() if a == v][0]

    device_name: str = Field(default_factory=socket.gethostname)
    device_id: str = ''

    @validator('device_id', always=True)
    def device_id_gen_or_read(cls, v, values) -> str:
        """
        Reads device_id from the certificate file (if any) or generates one.
        """
        (
            device_certfile,
            _,
        ) = PyKDEConnSettings.gen_cert_files_paths(values['device_name'])
        if device_certfile.exists():
            # return read_cert_common_name(device_certfile)
            return ''
        else:
            return uuid.uuid4().urn.replace('urn:uuid:', '')

    device_type: str = 'laptop'
    incoming_capabilities: List[str] = []
    outgoing_capabilities: List[str] = []

    device_certfile: Optional[Path] = None
    device_keyfile: Optional[Path] = None

    @validator('device_certfile', always=True)
    def device_certfile_gen_path(cls, v, values):
        if isinstance(v, Path):
            return v.resolve()
        else:
            return PyKDEConnSettings.gen_cert_files_paths(
                values['device_name']
            )[0]

    @validator('device_keyfile', always=True)
    def device_keyfile_gen_path_and_cert(cls, v, values):
        device_keyfile = v
        device_certfile = values['device_certfile']
        if not isinstance(device_keyfile, Path):
            (
                device_certfile,
                device_keyfile,
            ) = PyKDEConnSettings.gen_cert_files_paths(values['device_name'])

        # create certs files if don't exists
        if not device_keyfile.exists() or not device_certfile.exists():
            # generate_cert(
            #     values['device_name'],
            #     values['device_id'],
            #     device_certfile,
            #     device_keyfile,
            # )
            pass

        return device_keyfile.resolve()

    @staticmethod
    def gen_cert_files_paths(device_name) -> Tuple[Path, Path]:
        """
        Generates paths to a certificate files pair.
        """
        dev_name = re.sub(r'[^a-z0-9\-]', '', device_name.lower())
        return (
            config_source_dir.config_dir / f'certificate-{dev_name}.pem',
            config_source_dir.config_dir / f'private-{dev_name}.pem',
        )

    def save(self):
        """
        Save settings to a JSON file.
        """
        encoding = self.__config__.env_file_encoding

        # save cert paths as relative
        a = self.dict()
        a['device_certfile'] = str(
            self.device_certfile.relative_to(config_file.parent)
        )
        a['device_keyfile'] = str(
            self.device_keyfile.relative_to(config_file.parent)
        )

        config_file.write_text(json.dumps(a, indent=4), encoding=encoding)

    class Config:
        env_prefix = 'PYKDECONN_'
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


config = PyKDEConnSettings()
if not config_file.exists():
    config.save()


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
