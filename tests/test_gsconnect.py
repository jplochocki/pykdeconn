from unittest.mock import AsyncMock, MagicMock

import pytest

from pykdeconn.gsconnect import is_running


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

    async_mock = AsyncMock()
    a = async_mock.stdout = MagicMock()
    a.decode.return_value = example_output_running
    mocker.patch('pykdeconn.gsconnect.run_process', return_value=async_mock)

    result = await is_running()
    assert result == 123456

    a.decode.return_value = example_output_not_running
    result = await is_running()
    assert not result
    assert type(result) == bool
