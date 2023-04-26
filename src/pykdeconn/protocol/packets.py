from typing import TypeVar, Generic, List
import time

from pydantic import BaseModel, validator, Field
from pydantic.generics import GenericModel
from pydantic.config import BaseConfig, inherit_config

from .consts import KDE_CONNECT_DEFAULT_PORT, KDE_CONNECT_PROTOCOL_VERSION


PacketBodyT = TypeVar('PacketBodyT')


class KDEConnectPacket(GenericModel, Generic[PacketBodyT]):
    id: int = Field(default_factory=lambda: int(time.time()))
    type: str
    body: PacketBodyT

    @validator('body', always=True)
    def expected_packet_type(cls, v, values):
        # validating in body, because we need type and body available
        exp_type = v.__config__._expected_packet_type
        type_ = values['type']
        if type_ != exp_type:
            raise ValueError(
                f'Expected "{exp_type}" packet type, got "{type_}".',
            )
        return v

    class Config:
        allow_mutation = False


class IdentityPacketBody(BaseModel):
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

    class Config:
        allow_mutation = False
        _expected_packet_type = 'kdeconnect.identity'


IdentityPacket = KDEConnectPacket[IdentityPacketBody]


def generate_IdentityPacket(
    device_id: str,
    device_name: str,
    protocol_version: int = KDE_CONNECT_PROTOCOL_VERSION,
    device_type: str = 'laptop',
    incoming_capabilities: List[str] = [],
    outgoing_capabilities: List[str] = [],
    tcp_port: int = KDE_CONNECT_DEFAULT_PORT,
) -> IdentityPacket:
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


def make_pack_mutable(pack: KDEConnectPacket) -> KDEConnectPacket:
    # little hack: we are making pack.__config__ independent and change it
    pack.__dict__['__config__'] = inherit_config(pack.__config__, BaseConfig)
    pack.__config__.allow_mutation = True
    pack.body.__dict__['__config__'] = inherit_config(
        pack.body.__config__, BaseConfig
    )
    pack.body.__config__.allow_mutation = True
