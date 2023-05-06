import pytest  # noqa


from pykdeconn.protocol import PairPacket


def test_generate():
    prp = PairPacket.generate(True)
    assert isinstance(prp, PairPacket)
    assert prp.type == 'kdeconnect.pair'
    assert prp.body.pair

    prp = PairPacket.generate(False)
    assert isinstance(prp, PairPacket)
    assert prp.type == 'kdeconnect.pair'
    assert not prp.body.pair
