import re
from typing import Optional, List, ClassVar
import datetime
import logging
import logging.config

from pydantic import (
    BaseModel,
    Field,
    validator,
    PydanticValueError,
    IPvAnyAddress,
    PastDate,
)

from .protocol.packets import IdentityPacket


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
