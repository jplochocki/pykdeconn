from typing import Optional, Any, Dict, Tuple
import datetime
import ssl
from pathlib import Path

from pydantic import BaseModel, Field, PrivateAttr, IPvAnyAddress, PastDate
from anyio import create_memory_object_stream

from .consts import KDE_CONNECT_DEFAULT_PORT
from .packets import KDEConnectPacket


class RemoteDeviceConfig(BaseModel):
    """
    Intentionally separate configuration from that of
    ``pykdeconn.settings.DeviceConfig``. Compatible with it (you can initialize
    ''pykdeconn.settings.DeviceConfig.from_orm(RemoteDeviceConfig instance)''
    and vice versa).

    It contains the data necessary for the ``pykdeconn.protocol.connection``
    module to handle the connection.
    """

    device_id: str
    device_name: str

    certificate_PEM: str = ''

    last_ip: Optional[IPvAnyAddress] = None
    remote_port: int = KDE_CONNECT_DEFAULT_PORT

    last_connection_date: PastDate = Field(
        default_factory=lambda: datetime.datetime.now()
    )

    paired: bool = False

    new_packet_sender: Optional[Any] = None  # MemoryObjectSendStream
    new_packet_receiver: Optional[Any] = None  # MemoryObjectReceiveStream

    connected: bool = False
    connection_tls_socket: Optional[Any] = None  # TLSStream

    @classmethod
    def initialize(cls, config_base: Dict, remote_ip, remote_port):
        a = cls.parse_obj(config_base)
        a.last_ip = remote_ip
        a.remote_port = remote_port

        (
            a.new_packet_sender,
            a.new_packet_receiver,
        ) = create_memory_object_stream(
            item_type=Tuple[RemoteDeviceConfig, KDEConnectPacket]
        )

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
            cnx.load_verify_locations(
                cadata=ssl.PEM_cert_to_DER_cert(self.certificate_PEM)
            )
            cnx.verify_mode = ssl.CERT_REQUIRED

        self._ssl_cnx_cache[purpose.shortname] = cnx
        return cnx

    class Config:
        orm_mode = True
