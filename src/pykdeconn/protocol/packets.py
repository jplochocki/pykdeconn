from typing import get_type_hints, TypeVar, Generic, List, Optional
import time
from pathlib import Path
import datetime
import re

from pydantic import BaseModel, validator, Field
from pydantic.generics import GenericModel
from pydantic.main import ModelMetaclass
from pydantic.config import BaseConfig, inherit_config

from .consts import (
    KDE_CONNECT_DEFAULT_PORT,
    KDE_CONNECT_PROTOCOL_VERSION,
    KDE_CONNECT_TRANSFER_PORT_MIN,
)


PacketBodyT = TypeVar('PacketBodyT')


class KDEConnectPacket(GenericModel, Generic[PacketBodyT]):
    """
    Base class for KDE Connect packets.
    """

    id: int = Field(default_factory=lambda: int(time.time()))
    type: str
    body: PacketBodyT

    @validator('body', always=True)
    def expected_packet_type(cls, v, values):
        """
        Packet type field validation (e.g. ``IdentityPacket`` must always have
        ``type == 'kdeconnect.identity'``).
        """
        # validating in body, because we need type and body available
        exp_type = v.__config__._expected_packet_type
        type_ = values['type']
        if exp_type is not None and type_ != exp_type:
            raise ValueError(
                f'Expected "{exp_type}" packet type, got "{type_}".',
            )
        return v

    def prepare_to_send(self) -> bytes:
        """
        Prepares packet data to send to the KDE Connect device.
        """
        return (self.json() + '\n').encode('utf-8')

    @classmethod
    def generate(cls, *args, **kwargs):
        """
        Generates a new packet instance.
        """
        return get_type_hints(cls, include_extras=True)['body'].generate(
            *args, **kwargs
        )

    class Config:
        allow_mutation = False


class UnknownPacketBody(BaseModel):
    """
    Class for a body of package of any other type (usually unsupported type).
    """

    pass

    class Config:
        allow_mutation = True

        # None means any packet type is allowed here
        _expected_packet_type = None


class UnknownPacket(KDEConnectPacket[UnknownPacketBody]):
    """
    Class for a package of any other type (usually unsupported).
    """

    pass


class IdentityPacketBody(BaseModel):
    """
    Device's identification packet body.
    """

    deviceId: str
    deviceName: str
    protocolVersion: int = KDE_CONNECT_PROTOCOL_VERSION
    deviceType: str = ''
    incomingCapabilities: List[str] = []
    outgoingCapabilities: List[str] = []
    tcpPort: int = KDE_CONNECT_DEFAULT_PORT

    @validator('deviceId')
    def deviceId_cant_be_empty(cls, v: str) -> str:
        if str(v) == '':
            raise ValueError('deviceId can\'t be empty.')
        return v

    @classmethod
    def generate(
        cls,
        device_id: str,
        device_name: str,
        *,
        protocol_version: int = KDE_CONNECT_PROTOCOL_VERSION,
        device_type: str = 'laptop',
        incoming_capabilities: List[str] = [],
        outgoing_capabilities: List[str] = [],
        tcp_port: int = KDE_CONNECT_DEFAULT_PORT,
        **kwargs,
    ):
        """
        Generates a IdentityPacket instance.
        """
        return IdentityPacket.parse_obj(
            {
                'type': 'kdeconnect.identity',
                'body': {
                    'deviceId': device_id,
                    'deviceName': device_name,
                    'protocolVersion': protocol_version,
                    'deviceType': device_type,
                    'incomingCapabilities': incoming_capabilities,
                    'outgoingCapabilities': outgoing_capabilities,
                    'tcpPort': tcp_port,
                },
            }
        )

    class Config:
        allow_mutation = False
        _expected_packet_type = 'kdeconnect.identity'


class IdentityPacket(KDEConnectPacket[IdentityPacketBody]):
    """
    Device's identification packet.
    """

    pass


class PairPacketBody(BaseModel):
    """
    Device's pair packet body.
    """

    pair: bool

    @classmethod
    def generate(cls, pair: bool, **kwargs):
        """
        Generates a PairPacket instance.
        """
        return PairPacket.parse_obj(
            {'type': 'kdeconnect.pair', 'body': {'pair': pair}}
        )

    class Config:
        allow_mutation = False
        _expected_packet_type = 'kdeconnect.pair'


class PairPacket(KDEConnectPacket[PairPacketBody]):
    """
    Device's pair packet body.
    """

    pass


class KDEConnectTransferPacket(
    KDEConnectPacket[PacketBodyT], Generic[PacketBodyT]
):
    """
    Extended X KDEConnectPacket class for transfer packages.
    """

    class PayloadTransferInfo(BaseModel):
        port: int
        # conint w zakresie

    payloadSize: int
    payloadTransferInfo: PayloadTransferInfo


class ShareRequestPacketBody(BaseModel):
    """
    Body of file share packet.
    """

    filename: str
    lastModified: int  # timestamp
    numberOfFiles: int
    totalPayloadSize: int
    open: bool = False

    @classmethod
    def generate(
        cls,
        file_path: Path,
        file_size: Optional[int] = None,
        *,
        open_: bool = False,
        last_modified: Optional[datetime.datetime] = None,
        number_of_files: int = 1,
        total_size: Optional[int] = None,
        transfer_port: int = KDE_CONNECT_TRANSFER_PORT_MIN,
        **kwargs,
    ):
        if file_size is None and file_path.exists():
            file_size = file_path.lstat().st_size
        elif file_size is None:
            file_size = 0

        if last_modified is None and file_path.exists():
            last_modified = int(file_path.lstat().st_mtime)
        if last_modified is None:
            last_modified = 0
        elif isinstance(last_modified, datetime.datetime):
            last_modified = int(datetime.datetime.timestamp(last_modified))

        if total_size is None:
            total_size = file_size

        return ShareRequestPacket.parse_obj(
            {
                'type': 'kdeconnect.share.request',
                'body': {
                    'filename': file_path.name,
                    'open': open_,
                    'lastModified': last_modified,
                    'numberOfFiles': number_of_files,
                    'totalPayloadSize': total_size,
                },
                'payloadSize': file_size,
                'payloadTransferInfo': {'port': transfer_port},
            }
        )

    class Config:
        allow_mutation = False
        _expected_packet_type = 'kdeconnect.share.request'


class ShareRequestPacket(KDEConnectTransferPacket[ShareRequestPacketBody]):
    """
    File share packet.
    """

    pass


KDE_CONNECT_TYPE_TO_PACKET_CLS = {
    get_type_hints(value, include_extras=True)[
        'body'
    ].Config._expected_packet_type: value
    for name, value in vars().items()
    if isinstance(value, ModelMetaclass)
    and issubclass(value, (KDEConnectPacket, KDEConnectTransferPacket))
    and not re.search(r'\[', name)
    and name not in ('KDEConnectPacket', 'KDEConnectTransferPacket')
}

"""
ie.
{
    None: <class 'pykdeconn.protocol.packets.KDEConnectPacket[UnknownPacketBody]'>,
    'kdeconnect.identity': <class 'pykdeconn.protocol.packets.KDEConnectPacket[IdentityPacketBody]'>,
    'kdeconnect.share.request': <class 'pykdeconn.protocol.packets.KDEConnectTransferPacket[ShareRequestPacketBody]'>
}
"""  # noqa


def get_packet_by_kde_type_name(type_name: str):
    """
    Specifies the class type from the KDE Connect package name
    (e.g. ShareRequestPacket for 'kdeconnect.share.request')
    """
    return KDE_CONNECT_TYPE_TO_PACKET_CLS.get(type_name, UnknownPacket)


def make_pack_mutable(pack: KDEConnectPacket) -> KDEConnectPacket:
    # little hack: we are making pack.__config__ independent and change it
    pack.__dict__['__config__'] = inherit_config(pack.__config__, BaseConfig)
    pack.__config__.allow_mutation = True
    pack.body.__dict__['__config__'] = inherit_config(
        pack.body.__config__, BaseConfig
    )
    pack.body.__config__.allow_mutation = True
