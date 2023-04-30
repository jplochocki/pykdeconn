from pathlib import Path
import pytest
from pykdeconn.protocol import make_pack_mutable, IdentityPacket


id_packet_example = (
    Path(__file__).parent / 'fixtures' / 'kdeconnect.identity.json'
)


def test_pack_become_mutable():
    # both packet are immutable now
    idp1 = IdentityPacket.parse_file(id_packet_example)
    idp2 = IdentityPacket.parse_file(id_packet_example)

    with pytest.raises(TypeError):
        idp1.type = 'Lorem ipsum dolor'

    with pytest.raises(TypeError):
        idp1.body.deviceId = 'Lorem ipsum dolor'

    with pytest.raises(TypeError):
        idp2.type = 'Lorem ipsum dolor'

    with pytest.raises(TypeError):
        idp2.body.deviceId = 'Lorem ipsum dolor'

    assert not idp1.__config__.allow_mutation
    assert not idp2.__config__.allow_mutation

    # make idp2 mutable
    make_pack_mutable(idp2)
    idp2.type = 'Lorem ipsum dolor'
    assert idp2.type == 'Lorem ipsum dolor'

    idp2.body.deviceId = 'Lorem ipsum dolor'
    assert idp2.body.deviceId == 'Lorem ipsum dolor'

    assert idp2.__config__.allow_mutation

    # idp1 should stay immutable
    assert not idp1.__config__.allow_mutation
    with pytest.raises(TypeError):
        idp1.type = 'Lorem ipsum dolor'

    with pytest.raises(TypeError):
        idp1.body.deviceId = 'Lorem ipsum dolor'

    # next new packet should be immutable
    idp3 = IdentityPacket.parse_file(id_packet_example)
    assert not idp3.__config__.allow_mutation
    with pytest.raises(TypeError):
        idp3.type = 'Lorem ipsum dolor'

    with pytest.raises(TypeError):
        idp3.body.deviceId = 'Lorem ipsum dolor'
