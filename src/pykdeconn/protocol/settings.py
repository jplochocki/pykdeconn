import datetime
import ssl
import socket
import re
from typing import Optional, Any, Dict, Tuple, List
from pathlib import Path


from pydantic import (
    BaseModel,
    BaseSettings,
    Field,
    PrivateAttr,
    validator,
    IPvAnyAddress,
    PastDate,
    PydanticValueError,
)
from anyio import create_memory_object_stream
from anyio.streams.memory import MemoryObjectSendStream

from .consts import KDE_CONNECT_DEFAULT_PORT
from .packets import KDEConnectPacket, IdentityPacket


class InvalidPEMFormatError(PydanticValueError):
    code = 'invalid_PEM_format'
    msg_template = 'Invalid PEM Certificate format'


class BaseHostConfig(BaseSettings):
    device_name: str = Field(default_factory=socket.gethostname)
    device_id: str = ''
    device_type: str = 'laptop'

    incoming_capabilities: List[str] = []
    outgoing_capabilities: List[str] = []

    device_certfile: Optional[Path] = None
    device_keyfile: Optional[Path] = None

    could_send_my_id_packs: bool = True

    new_id_receiver: Optional[Any] = None  # MemoryObjectReceiveStream
    new_id_sender: Optional[Any] = None  # MemoryObjectSendStream

    @validator('new_id_sender', always=True)
    def new_id_sender_and_receiver_default_factory(
        cls, v: Optional[Any], values
    ) -> MemoryObjectSendStream:
        new_id_sender, new_id_receiver = create_memory_object_stream(
            item_type=Tuple[IPvAnyAddress, IdentityPacket]
        )
        values['new_id_receiver'] = new_id_receiver

        return new_id_sender

    def generate_IdentityPacket(self) -> IdentityPacket:
        """
        Generates ID packet for this device.
        """
        return IdentityPacket.generate(**self.dict(include={
            'device_id', 'device_name', 'device_type', 'incoming_capabilities',
            'outgoing_capabilities'}))


class BaseDeviceConfig(BaseModel):
    """
    Base class for KDE Connect device config.

    It contains data necessary for the ``pykdeconn.protocol.connection``
    module to handle the connection. Other information should be placed
    in inheriting classes (i.e. ``settings.PyKDEConnDeviceConfig``).
    """

    host_config: Optional[BaseHostConfig] = None

    device_id: str
    device_name: str

    certificate_PEM: str = ''

    @validator('certificate_PEM')
    def cert_str_format_or_empty(cls, value: str) -> str:
        if value != '' and not re.match(
            (
                r'^\s*-----BEGIN CERTIFICATE-----\n[A-Za-z0-9\+\/=\n]+\n'
                r'-----END CERTIFICATE-----\s*$'
            ),
            value,
        ):
            raise InvalidPEMFormatError(pem_value=value)

        return value

    type_: str = Field('phone', alias='type')
    incoming_capabilities: List[str] = []
    outgoing_capabilities: List[str] = []

    last_ip: Optional[IPvAnyAddress] = None
    remote_port: int = KDE_CONNECT_DEFAULT_PORT

    last_connection_date: PastDate = Field(
        default_factory=lambda: datetime.datetime.now()
    )

    paired: bool = False

    new_packet_receiver: Optional[Any] = None  # MemoryObjectReceiveStream
    new_packet_sender: Optional[Any] = None  # MemoryObjectSendStream

    @validator('new_packet_sender', always=True)
    def new_packet_sender_and_receiver_default_factory(
        cls, v: Optional[Any], values
    ) -> MemoryObjectSendStream:
        new_packet_sender, new_packet_receiver = create_memory_object_stream(
            item_type=Tuple[BaseDeviceConfig, KDEConnectPacket]
        )
        values['new_packet_receiver'] = new_packet_receiver

        return new_packet_sender

    connected: bool = False
    connection_tls_socket: Optional[Any] = None  # TLSStream

    @classmethod
    def initialize(
        cls,
        config_base: Dict,
        host_config: BaseHostConfig,
        remote_ip,
        remote_port,
    ):
        a = cls(**config_base)

        a.host_config = host_config
        a.last_ip = remote_ip
        a.remote_port = remote_port

        return a

    def get_debug_id(self) -> str:
        a = self.last_ip if self.last_ip else 'not connected'
        return f'{a} ({self.device_name} / {self.device_id})'

    def on_unpair(self):
        self.paired = False
        self.certificate_PEM = ''

        self.on_disconnect()

    def on_disconnect(self):
        self.connected = False
        self.connection_tls_socket = None

    def on_connect(self, tls_socket):
        self.connected = True
        self.connection_tls_socket = tls_socket
        self.last_connection_date = datetime.datetime.now()

    _ssl_cnx_cache: Dict[str, Any] = PrivateAttr(default_factory=dict)

    def ssl_context(
        self,
        purpose: ssl.Purpose = ssl.Purpose.CLIENT_AUTH,
        renew: bool = False,
    ) -> ssl.SSLContext:
        """
        Loads ``SSLContext`` for the specified ``purpose``.
        """
        if renew:
            self._ssl_cnx_cache = {}

        if purpose.shortname in self._ssl_cnx_cache:
            return self._ssl_cnx_cache[purpose.shortname]

        cnx = ssl.create_default_context(purpose)
        cnx.load_cert_chain(
            certfile=self.host_config.device_certfile,
            keyfile=self.host_config.device_keyfile,
        )

        cnx.check_hostname = False
        cnx.verify_mode = ssl.CERT_NONE

        if self.certificate_PEM:
            cnx.load_verify_locations(
                cadata=ssl.PEM_cert_to_DER_cert(self.certificate_PEM)
            )
            cnx.verify_mode = ssl.CERT_REQUIRED

        self._ssl_cnx_cache[purpose.shortname] = cnx
        return cnx

    class Config:
        orm_mode = True
