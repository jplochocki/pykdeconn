from typing import TypeVar, Generic, List


from pydantic import BaseModel, validator
from pydantic.generics import GenericModel

from .consts import KDE_CONNECT_DEFAULT_PORT, KDE_CONNECT_PROTOCOL_VERSION


PacketBodyT = TypeVar('PacketBody')


class KDEConnectPacket(GenericModel, Generic[PacketBodyT]):
    id: int
    type: str
    body: PacketBodyT

    @validator('body', always=True)
    def expected_packet_type(cls, v, values):
        # validating in body, because we need type and body available
        exp_type = v.__config__._expected_packet_type
        type_ = values['type']
        if type_ != exp_type:
            raise ValueError(
                f'Expected "{exp_type}" packet type, got "{type_}".'
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
    def deviceId_not_empty(cls, v):
        if v == '':
            raise ValueError('deviceId can\'t be empty.')
        return v

    class Config:
        allow_mutation = False
        _expected_packet_type = 'kdeconnect.identity'


IdentityPacket = KDEConnectPacket[IdentityPacketBody]
