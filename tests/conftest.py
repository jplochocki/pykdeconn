import os
import tempfile
from pathlib import Path
import asyncio

import pytest

from pykdeconn import settings


@pytest.fixture
def pykdeconn_empty_config(mocker) -> pytest.fixture():
    """
    Creates empty config.json file for test.
    """
    with tempfile.NamedTemporaryFile(suffix='.json') as tmpf:
        os.environ['PYKDECONN_CONFIG_FILE'] = tmpf.name

        old_config_file = settings.config_source.config_file
        settings.config_source.config_file = Path(tmpf.name)

        old_host_config = settings.host_config
        asyncio.set_event_loop(asyncio.new_event_loop())
        settings.host_config = settings.PyKDEConnSettings()
        settings.host_config.save()

        yield settings.host_config

        settings.config_source.config_file = old_config_file
        settings.host_config = old_host_config
