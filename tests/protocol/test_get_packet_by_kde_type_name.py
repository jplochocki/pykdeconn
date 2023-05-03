from pathlib import Path

import pytest  # noqa

from pykdeconn.protocol import (
    get_packet_by_kde_type_name,
    UnknownPacket,
    IdentityPacket,
    ShareRequestPacket,
)


id_packet_example = (
    Path(__file__).parent / 'fixtures' / 'kdeconnect.identity.json'
)


def test_get_packet_by_kde_type_name():
    # known packet types
    assert get_packet_by_kde_type_name('kdeconnect.identity') is IdentityPacket
    assert (
        get_packet_by_kde_type_name('kdeconnect.share.request')
        is ShareRequestPacket
    )
    assert (
        get_packet_by_kde_type_name('kdeconnect.share.request')
        is not IdentityPacket
    )
    assert (
        get_packet_by_kde_type_name('kdeconnect.share.request')
        is not UnknownPacket
    )

    # unknown packet type
    assert (
        get_packet_by_kde_type_name('kdeconnect.lorem.ipsum.dolor')
        is UnknownPacket
    )

    # create class from return type
    idp = get_packet_by_kde_type_name('kdeconnect.identity').parse_file(
        id_packet_example
    )

    assert idp.id == 1644153455113
    assert idp.type == 'kdeconnect.identity'
    assert idp.body.deviceId == '32151f87b8be9b96'
    assert idp.body.deviceName == 'Redmi 6A'
