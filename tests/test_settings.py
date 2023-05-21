from pathlib import Path

import pytest  # noqa

from pykdeconn.protocol import IdentityPacket
from pykdeconn.settings import PyKDEConnDeviceConfig


id_packet_example = (
    Path(__file__).parent
    / 'protocol'
    / 'fixtures'
    / 'kdeconnect.identity.json'
)


def test_get_or_create_device_config(pykdeconn_empty_config):
    new_host_config = pykdeconn_empty_config
    assert len(new_host_config.devices) == 0

    idp = IdentityPacket.parse_file(id_packet_example)

    dev = new_host_config.get_or_create_device_config(idp, '192.168.0.45')
    assert isinstance(dev, PyKDEConnDeviceConfig)
    assert len(new_host_config.devices) == 1

    assert dev.device_id == '32151f87b8be9b96'
    assert dev.device_id in new_host_config.devices
    assert dev.device_name == 'Redmi 6A'
    assert new_host_config.devices[dev.device_id].device_name == 'Redmi 6A'
