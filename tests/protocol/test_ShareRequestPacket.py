from pathlib import Path
import datetime

import pytest  # noqa

from pykdeconn.protocol import ShareRequestPacket


share_packet_example = (
    Path(__file__).parent / 'fixtures' / 'kdeconnect.share.request.json'
)


def test_ShareRequestPacket_expected_values():
    shp = ShareRequestPacket.parse_file(share_packet_example)
    assert shp.id == 1645343489598
    assert shp.type == 'kdeconnect.share.request'
    assert shp.body.filename == (
        'Screenshot_2022-02-07-23-30-00-849_com.facebook.lite.jpg'
    )
    assert shp.body.lastModified == 1644273001000
    assert shp.body.numberOfFiles == 2
    assert shp.body.totalPayloadSize == 310309
    assert shp.payloadSize == 260509
    assert shp.payloadTransferInfo.port == 1739


def test_ShareRequestPacket_generate():
    # only (existing) file path
    shp = ShareRequestPacket.generate(share_packet_example)

    assert type(shp) is ShareRequestPacket
    assert shp.type == 'kdeconnect.share.request'
    assert shp.body.filename == share_packet_example.name
    assert shp.body.lastModified == int(share_packet_example.lstat().st_mtime)
    assert (
        shp.payloadSize
        == shp.body.totalPayloadSize
        == share_packet_example.lstat().st_size
    )

    # not existing file path
    not_existing_path = Path('Lorem ipsum dolor')
    shp = ShareRequestPacket.generate(not_existing_path)

    assert type(shp) is ShareRequestPacket
    assert shp.type == 'kdeconnect.share.request'
    assert shp.body.filename == not_existing_path.name
    assert shp.body.lastModified == 0
    assert shp.payloadSize == shp.body.totalPayloadSize == 0

    # own last_modified param
    dt = datetime.datetime.now()
    shp = ShareRequestPacket.generate(not_existing_path, last_modified=dt)

    assert type(shp) is ShareRequestPacket
    assert shp.type == 'kdeconnect.share.request'
    assert shp.body.filename == not_existing_path.name
    assert shp.body.lastModified == int(datetime.datetime.timestamp(dt))
    assert shp.payloadSize == shp.body.totalPayloadSize == 0
