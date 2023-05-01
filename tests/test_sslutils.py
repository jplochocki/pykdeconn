from unittest.mock import AsyncMock, MagicMock  # noqa
from pathlib import Path

import pytest
from anyio import run_process

from pykdeconn.sslutils import generate_cert, read_cert_common_name


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.mark.anyio
async def test_generate_cert(mocker):
    run_process_mock = mocker.patch(
        'pykdeconn.sslutils.run_process', spec=run_process
    )

    device_id = 'lorem-ipsum-dolor'
    device_certfile = Path('certificate.pem')
    device_keyfile = Path('private.pem')

    await generate_cert(
        device_id=device_id,
        device_certfile=device_certfile,
        device_keyfile=device_keyfile,
    )

    run_process_mock.assert_awaited()
    run_process_mock.assert_called_once_with(
        [
            'openssl',
            'req',
            '-new',
            '-x509',
            '-sha256',
            '-out',
            device_certfile,
            '-newkey',
            'rsa:4096',
            '-nodes',
            '-keyout',
            device_keyfile,
            '-days',
            '3650',
            '-subj',
            f'/O=jplochocki.github.io/OU=PyKDEConn/CN={device_id}',
        ],
        check=True,
    )


@pytest.mark.anyio
async def test_read_cert_common_name(mocker):
    # any result with CN = ... at the end is valid
    valid_result = (
        'subject=O = jplochocki.github.io, OU = PyKDEConn, CN = '
        '2f01efa7-58dd-45f4-a8c5-8a307d231cf0'
    )
    invalid_result = 'Lorem ipsum dolor sit amet'

    # valid openssl result
    run_process_mock = AsyncMock()
    run_process_stdout = run_process_mock.stdout = MagicMock()
    run_process_stdout.decode.return_value = valid_result
    mocker.patch(
        'pykdeconn.sslutils.run_process', return_value=run_process_mock
    )

    cert_file = Path('./certificate.pem')  # path would not be used in tests
    result = await read_cert_common_name(cert_file)

    run_process_stdout.decode.assert_called_once()
    assert result == '2f01efa7-58dd-45f4-a8c5-8a307d231cf0'

    # invalid openssl result
    run_process_stdout.decode.return_value = invalid_result
    run_process_stdout.decode.reset_mock()

    result = await read_cert_common_name(cert_file)

    run_process_stdout.decode.assert_called_once()
    assert result is None
