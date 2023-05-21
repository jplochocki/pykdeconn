import re
import asyncio
import logging
import logging.config
import json
import sys
import uuid
import time
from typing import Any, List, Tuple, Dict, Literal
from pathlib import Path

from pydantic import BaseSettings, Field, validator
from anyio.from_thread import run as run_async  # noqa

from .protocol import BaseDeviceConfig, BaseHostConfig, IdentityPacket
from .utils import running_in_pytest
from .sslutils import generate_cert, read_cert_common_name  # noqa


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


class PyKDEConnDeviceConfig(BaseDeviceConfig):
    """
    Specialized version of BaseDeviceConfig (e.g. save options).
    """

    def __setattr__(self, name, value):
        super().__setattr__(name, value)
        if self.host_config and name in self.__config__._fields_to_save:
            self.save()

    def save(self):
        if self.host_config and hasattr(self.host_config, 'save'):
            self.host_config.save()

    class Config:
        orm_mode = True
        _fields_to_save = {
            'device_id',
            'device_name',
            'certificate_PEM',
            'type_',
            'incoming_capabilities',
            'outgoing_capabilities',
            'last_ip',
            'last_connection_date',
            'paired',
        }


class PyKDEConnSettingsSourceDir(BaseSettings):
    """
    Settings directory source (default or from environmet variables).
    """

    config_dir: Path = Field(
        env='PYKDECONN_CONFIG_DIR',
        default_factory=lambda: PyKDEConnSettingsSourceDir.config_dir_default_factory(),  # noqa
    )

    @staticmethod
    def config_dir_default_factory() -> Path:
        """
        Creates config directory path (``~/.config/pykdeconn`` if ``~/.config``
        exists or ``~/.pykdeconn`` else). If we're running in ``pytest``
        environment - returns ``tests/config`` path.
        """
        if running_in_pytest():
            # We are trying to determine the path to the tests/ directory.
            # Expected test calls python -m pytest -s ../tests/ and CWD was
            # venv dir
            tests_dir = ([i for i in sys.argv if 'tests' in i] + [''])[0]
            tests_dir = (Path('.') / tests_dir).resolve()
            if tests_dir.exists():
                return tests_dir / 'config'
            else:
                log.warning(
                    (
                        'Config directory to run in pytest does not exists '
                        f'({tests_dir})'
                    )
                )

        if Path('~/.config').expanduser().exists():
            return Path('~/.config/pykdeconn').expanduser()
        else:
            return Path('~/.pykdeconn').expanduser()

    @validator('config_dir', always=True)
    def create_dir_if_not_exists(cls, p: Path) -> Path:
        """
        Creates a configuration directory if it does not exists.
        """
        if not p.exists():
            p.mkdir(parents=True, exist_ok=True)
        elif not p.is_dir:
            raise ValueError('Path does not point to a directory.')
        return p

    config_file: Path = Field('config.json', env='PYKDECONN_CONFIG_FILE')

    @validator('config_file', always=True)
    def make_path_absolute(cls, v: Path, values, field) -> Path:
        """
        Makes the config_file path absoulte path.


        If you set path via the PYKDECONN_CONFIG_FILE variable, always use
        absolute path (otherwise it will be considered relative
        to the config_dir directory).
        """
        if not v.is_absolute():
            return values['config_dir'] / field.default

        return v


config_source = PyKDEConnSettingsSourceDir()


def json_config_settings_source(settings: BaseSettings) -> Dict[str, Any]:
    """
    Read settings from JSON file.
    """
    encoding = settings.__config__.env_file_encoding
    if config_source.config_file.exists():
        a = config_source.config_file.read_text(encoding)
        if a:
            return json.loads(a)

    return {}


class PyKDEConnSettings(BaseHostConfig):
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
    def parse_log_level(cls, v):
        """
        Parses and validates log_level value.
        """
        named_lvls = {
            'CRITICAL': logging.CRITICAL,
            'ERROR': logging.ERROR,
            'WARNING': logging.WARNING,
            'INFO': logging.INFO,
            'DEBUG': logging.DEBUG,
        }

        if v not in named_lvls.values() and v not in named_lvls.keys():
            raise ValueError(f'Invalid log_level value ({v}).')

        if v in named_lvls.keys():
            log.setLevel(named_lvls[v])
            return v
        else:
            log.setLevel(v)
            return [k for k, a in named_lvls.items() if a == v][0]

    @validator('device_id', always=True)
    def device_id_gen_or_read(cls, v, values) -> str:
        """
        Reads device_id from the certificate file (if it exists)
        or generates a new unique ID
        """
        if v:
            return v

        (
            device_certfile,
            _,
        ) = PyKDEConnSettings.gen_cert_files_paths()
        if device_certfile.exists():
            return asyncio.get_event_loop().run_until_complete(
                read_cert_common_name(device_certfile)
            )
        return uuid.uuid4().urn.replace('urn:uuid:', '')

    @validator('incoming_capabilities', always=True)
    def incoming_capabilities_defaults(cls, v) -> List:
        if v:
            return v

        return ['kdeconnect.share.request']

    @validator('outgoing_capabilities', always=True)
    def outgoing_capabilities_defaults(cls, v) -> List:
        if v:
            return v

        return ['kdeconnect.share.request']

    @validator('device_certfile', always=True)
    def device_certfile_gen_path(cls, v, values):
        if isinstance(v, Path) and not v.is_absolute():
            return config_source.config_file.parent / v
        elif isinstance(v, Path):
            return v

        return PyKDEConnSettings.gen_cert_files_paths()[0]

    @validator('device_keyfile', always=True)
    def device_keyfile_gen_path_and_cert(cls, v, values):
        device_keyfile = v
        device_certfile = values['device_certfile']

        if not isinstance(device_keyfile, Path):
            (
                device_certfile,
                device_keyfile,
            ) = PyKDEConnSettings.gen_cert_files_paths()

        if not device_keyfile.is_absolute():
            device_keyfile = config_source.config_file.parent / device_keyfile

        if not device_certfile.is_absolute():
            device_certfile = (
                config_source.config_file.parent / device_certfile
            )

        # create certs files if don't exists
        if not device_keyfile.exists() or not device_certfile.exists():
            asyncio.get_event_loop().run_until_complete(
                generate_cert(
                    values['device_id'],
                    device_certfile,
                    device_keyfile,
                )
            )

        return device_keyfile.resolve()

    @staticmethod
    def gen_cert_files_paths(device_name: str = '') -> Tuple[Path, Path]:
        """
        Generates paths to a certificate files pair.
        """
        if device_name:
            device_name = '-' + re.sub(r'[^a-z0-9\-]', '', device_name.lower())
        return (
            config_source.config_file.parent / f'certificate{device_name}.pem',
            config_source.config_file.parent / f'private{device_name}.pem',
        )

    ignore_device_ids: list = []

    @validator('ignore_device_ids', always=True)
    def ignore_device_ids_default(cls, v, values):
        """
        Add yours device_id to ignored devices list
        """
        v.append(values['device_id'])
        return v

    recent_device_ids_list: List[Tuple[int, IdentityPacket]] = []

    def update_recent_device_ids_list(self, id_pack: IdentityPacket):
        now = int(time.time())

        # add or update this id pack
        already_known_idp_idx = next(
            (
                idx
                for idx, (_, idp) in enumerate(self.recent_device_ids_list)
                if idp.body.deviceId == id_pack.body.deviceId
            ),
            -1,
        )
        if self.device_id != id_pack.body.deviceId:
            if already_known_idp_idx != -1:
                # update receive time
                self.recent_device_ids_list[already_known_idp_idx] = (
                    now,
                    id_pack,
                )
            else:
                self.recent_device_ids_list.append((now, id_pack))

        # remove old id packets
        past_60_secs = now - 60
        self.recent_device_ids_list = [
            (tm, idp)
            for tm, idp in self.recent_device_ids_list
            if tm >= past_60_secs
        ]

    devices: Dict[str, PyKDEConnDeviceConfig] = {}

    def get_or_create_device_config(
        self,
        remote_id_pack: IdentityPacket,
        remote_ip,
        must_be_paired: bool = False,
    ) -> BaseDeviceConfig:
        """
        Loads (or create) ``PyKDEConnDeviceConfig`` from
        ``kdeconnect.identity`` packet.
        """
        if remote_id_pack.body.deviceId in self.devices:
            dev = self.devices[remote_id_pack.body.deviceId]
            if must_be_paired and not dev.paired:
                return None
            return dev

        if must_be_paired:
            return None

        dev = PyKDEConnDeviceConfig(
            device_id=remote_id_pack.body.deviceId,
            device_name=remote_id_pack.body.deviceName,
            host_config=self,
            last_ip=remote_ip,
            remote_port=remote_id_pack.body.tcpPort,
            incoming_capabilities=remote_id_pack.body.incomingCapabilities.copy(),  # noqa
            outgoing_capabilities=remote_id_pack.body.outgoingCapabilities.copy(),  # noqa
        )

        self.devices[remote_id_pack.body.deviceId] = dev
        self.save()
        return dev

    def save(self):
        """
        Save settings to a JSON file.
        """
        encoding = self.__config__.env_file_encoding

        # save cert paths as relative
        a = self.dict(include=self.__config__._fields_to_save)

        try:
            a['device_certfile'] = str(
                self.device_certfile.relative_to(
                    config_source.config_file.parent
                )
            )
            a['device_keyfile'] = str(
                self.device_keyfile.relative_to(
                    config_source.config_file.parent
                )
            )
        except ValueError:
            a['device_certfile'] = str(self.device_certfile)
            a['device_keyfile'] = str(self.device_keyfile)

        # save devices
        a['devices'] = {
            dev_id: json.loads(
                dev.json(include=dev.__config__._fields_to_save)
            )
            for dev_id, dev in self.devices.items()
        }

        config_source.config_file.write_text(
            json.dumps(a, indent=4), encoding=encoding
        )

    class Config:
        env_prefix = 'PYKDECONN_'
        env_file_encoding = 'utf-8'
        _fields_to_save = {
            'log_level',
            'device_name',
            'device_id',
            'device_type',
            'incoming_capabilities',
            'outgoing_capabilities',
            'device_certfile',
            'device_keyfile',
        }

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


host_config = PyKDEConnSettings()
if not config_source.config_file.exists():
    host_config.save()
