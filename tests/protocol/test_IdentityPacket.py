from pathlib import Path
import json


import pytest
from pydantic import ValidationError


from pyconnect.protocol import (
    IdentityPacket,
    generate_IdentityPacket,
    KDE_CONNECT_PROTOCOL_VERSION,
    KDE_CONNECT_DEFAULT_PORT,
)


id_packet_example = (
    Path(__file__).parent / 'examples' / 'kdeconnect.identity.json'
)


def test_IdentityPacket_is_immutable():
    idp = IdentityPacket.parse_file(id_packet_example)
    with pytest.raises(TypeError):
        idp.type = 'Lorem ipsum dolor'

    with pytest.raises(TypeError):
        idp.body.deviceId = 'Lorem ipsum dolor'


def test_IdentityPacket_expected_values():
    idp = IdentityPacket.parse_file(id_packet_example)

    assert idp.id == 1644153455113
    assert idp.type == 'kdeconnect.identity'
    assert idp.body.deviceId == '32151f87b8be9b96'
    assert idp.body.deviceName == 'Redmi 6A'
    assert idp.body.protocolVersion == 7
    assert idp.body.deviceType == 'phone'
    assert 'kdeconnect.share.request' in idp.body.incomingCapabilities
    assert 'kdeconnect.share.request' in idp.body.outgoingCapabilities
    assert idp.body.tcpPort == 1716


def test_IdentityPacket_invalid_packet_type():
    idp_dict = json.loads(id_packet_example.read_text())
    idp_dict['type'] = 'lorem.ipsum.dolor'

    with pytest.raises(ValidationError) as e:
        IdentityPacket.parse_obj(idp_dict)

    assert e.match(
        'Expected "kdeconnect.identity" packet type, got "lorem.ipsum.dolor".'
    )


def test_generate_IdentityPacket():
    idp = generate_IdentityPacket(
        device_id='lorem.ipsum.dolor',
        device_name='lorem.ipsum.dolor device',
        device_type='desktop',
        incoming_capabilities=['lorem.ipsum.dolor1', 'lorem.ipsum.dolor2'],
        outgoing_capabilities=['lorem.ipsum.dolor3', 'lorem.ipsum.dolor4'],
    )

    assert isinstance(idp, IdentityPacket)

    assert idp.type == 'kdeconnect.identity'
    assert idp.body.deviceId == 'lorem.ipsum.dolor'
    assert idp.body.deviceName == 'lorem.ipsum.dolor device'
    assert idp.body.protocolVersion == KDE_CONNECT_PROTOCOL_VERSION
    assert idp.body.deviceType == 'desktop'
    assert idp.body.incomingCapabilities == [
        'lorem.ipsum.dolor1',
        'lorem.ipsum.dolor2',
    ]
    assert idp.body.outgoingCapabilities == [
        'lorem.ipsum.dolor3',
        'lorem.ipsum.dolor4',
    ]
    assert idp.body.tcpPort == KDE_CONNECT_DEFAULT_PORT
