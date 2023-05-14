import subprocess
from unittest.mock import AsyncMock, MagicMock
from pathlib import Path

import pytest
from anyio import run_process
from ipaddress import IPv4Address

from pykdeconn.gsconnect import (
    is_running,
    try_kill,
    gen_cert_files_paths,
    list_device_names_and_ids,
    read_device_config,
    generate_identity_params,
)
from pykdeconn.settings import PyKDEConnDeviceConfig
from pykdeconn.protocol import IdentityPacket


dconf_result_example = (
    Path(__file__).parent / 'fixtures' / 'dconf-result-example.txt'
)


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.mark.anyio
async def test_is_running(mocker):
    example_output_running = '''
  123456 ?        Sl     0:16 gjs /home/systemik/.local/share/gnome-shell/extensions/gsconnect@andyholmes.github.io/service/daemon.js
  123457 pts/2    S+     0:00 grep --color=auto gsconnect@andyholmes.github.io
    '''  # noqa

    example_output_not_running = '''
  123457 pts/2    S+     0:00 grep --color=auto gsconnect@andyholmes.github.io
    '''  # noqa

    run_process_mock = AsyncMock()
    run_process_stdout = run_process_mock.stdout = MagicMock()
    run_process_stdout.decode.return_value = example_output_running
    mocker.patch(
        'pykdeconn.gsconnect.run_process', return_value=run_process_mock
    )

    # running result
    result = await is_running()
    assert result == 123456
    run_process_stdout.decode.assert_called_once()

    # not running result
    run_process_stdout.decode.return_value = example_output_not_running
    run_process_stdout.decode.reset_mock()
    result = await is_running()
    assert not result
    assert type(result) == bool
    run_process_stdout.decode.assert_called_once()


@pytest.mark.anyio
async def test_try_kill(mocker):
    run_process_mock = mocker.patch(
        'pykdeconn.gsconnect.run_process', spec=run_process
    )

    is_running_mock = mocker.patch(
        'pykdeconn.gsconnect.is_running', spec=is_running
    )
    is_running_mock.return_value = 12345678

    assert await try_kill()

    run_process_mock.assert_awaited()
    assert run_process_mock.call_count == 2
    run_process_mock.assert_any_call('kill -9 12345678', check=True)

    is_running_mock.assert_called_once()

    # error catching
    run_process_mock.side_effect = subprocess.CalledProcessError(
        returncode=1, cmd='lorem-ipsum-dolor'
    )
    assert not await try_kill()


def test_gen_cert_files_paths():
    cert, prv = gen_cert_files_paths()
    assert isinstance(cert, Path)
    assert isinstance(prv, Path)


@pytest.mark.anyio
async def test_list_device_names_and_ids(mocker):
    run_process_mock = AsyncMock()
    run_process_stdout = run_process_mock.stdout = MagicMock()
    run_process_stdout.decode.return_value = dconf_result_example.read_text()
    mocker.patch(
        'pykdeconn.gsconnect.run_process', return_value=run_process_mock
    )

    result = await list_device_names_and_ids()

    run_process_stdout.decode.assert_called_once()
    assert len(result) == 1
    assert result[0] == ('2fe54440ccaa5e3b', 'LM-K510')

    # empty devices list
    run_process_stdout.decode.return_value = ''
    result = await list_device_names_and_ids()
    assert type(result) == list and result == []


@pytest.mark.anyio
async def test_read_device_config(mocker):
    run_process_mock = AsyncMock()
    run_process_stdout = run_process_mock.stdout = MagicMock()
    run_process_stdout.decode.return_value = dconf_result_example.read_text()
    mocker.patch(
        'pykdeconn.gsconnect.run_process', return_value=run_process_mock
    )

    # known device
    result = await read_device_config('2fe54440ccaa5e3b')

    run_process_stdout.decode.assert_called_once()

    assert result['device_id'] == '2fe54440ccaa5e3b'
    assert result['certificate_PEM'].startswith('-----BEGIN CERTIFICATE-----')
    assert 'kdeconnect.battery' in result['incoming_capabilities']
    assert result['last_ip'] == '192.168.0.45'
    assert result['device_name'] == 'LM-K510'
    assert 'kdeconnect.battery.request' in result['outgoing_capabilities']
    assert type(result['paired']) == bool and result['paired']
    assert result['type_'] == 'phone'

    # known device convert to PyKDEConnDeviceConfig
    result = PyKDEConnDeviceConfig.parse_obj(result)

    assert result.device_id == '2fe54440ccaa5e3b'
    assert result.certificate_PEM.startswith('-----BEGIN CERTIFICATE-----')
    assert 'kdeconnect.battery' in result.incoming_capabilities
    assert (
        isinstance(result.last_ip, IPv4Address)
        and str(result.last_ip) == '192.168.0.45'
    )
    assert result.device_name == 'LM-K510'
    assert 'kdeconnect.battery.request' in result.outgoing_capabilities
    assert type(result.paired) == bool and result.paired
    assert result.type_ == 'phone'

    # unknown device
    result = await read_device_config('Lorem ipsum dolor')
    assert type(result) == dict and result == {}


@pytest.mark.anyio
async def test_generate_identity_params(mocker):
    run_process_mock = AsyncMock()
    run_process_stdout = run_process_mock.stdout = MagicMock()
    run_process_stdout.decode.return_value = dconf_result_example.read_text()
    mocker.patch(
        'pykdeconn.gsconnect.run_process', return_value=run_process_mock
    )

    result = await generate_identity_params()

    run_process_stdout.decode.assert_called_once()

    assert result['device_id'] == '00392cd2-329e-4ad6-9766-019a304b32f9'
    assert result['device_name'] == 'systemik-dell'

    # generate id packet test
    id_pack = IdentityPacket.generate(**result)

    assert isinstance(id_pack, IdentityPacket)
    assert id_pack.body.deviceId == '00392cd2-329e-4ad6-9766-019a304b32f9'
    assert id_pack.body.deviceName == 'systemik-dell'

    # no '/' section
    run_process_stdout.decode.return_value = ''
    result = await generate_identity_params()

    assert result == {}
