from pathlib import Path
from unittest.mock import patch
import os
import sys

import pytest  # noqa

from pykdeconn.utils import running_in_pytest, simple_toml_parser


dconf_result_example = (
    Path(__file__).parent / 'examples' / 'dconf-result-example.txt'
)


def test_running_in_pytest_patched():
    valid_pytest_path = '.../site-packages/pytest/__main__.py'
    invalid_pytest_path = 'any-other-path.py'

    with patch.object(sys, 'argv') as sys_argv:
        with patch.dict(
            os.environ, {'PYTEST_RUNNING': '1'}, clear=True
        ) as os_environ:
            sys_argv.__getitem__.return_value = valid_pytest_path

            # should base on os.environ
            assert running_in_pytest()
            sys_argv.__getitem__.assert_not_called()

            # should base on sys.argv
            del os_environ['PYTEST_RUNNING']
            assert running_in_pytest()

            sys_argv.__getitem__.assert_called_once()

            # should base on sys.argv and return False
            sys_argv.__getitem__.return_value = invalid_pytest_path
            sys_argv.__getitem__.reset_mock()

            assert not running_in_pytest()

            sys_argv.__getitem__.assert_called_once()


def test_running_in_pytest():
    assert running_in_pytest()


def test_simple_toml_parser():
    result = simple_toml_parser(dconf_result_example.read_text())

    assert '/' in result
    assert '32151f87b8be9b96' in result['/']['devices']
    assert '2fe54440ccaa5e3b' in result['/']['devices']
    assert result['/']['enabled']
    assert result['/']['id'] == '00392cd2-329e-4ad6-9766-019a304b32f9'
    assert result['/']['name'] == 'systemik-dell'

    assert 'device/2fe54440ccaa5e3b' in result
    assert result['device/2fe54440ccaa5e3b']['certificate-pem'].startswith(
        '-----BEGIN CERTIFICATE-----'
    )
    assert 'disabled-plugins' not in result['device/2fe54440ccaa5e3b']
    assert (
        'kdeconnect.battery'
        in result['device/2fe54440ccaa5e3b']['incoming-capabilities']
    )
    assert (
        'kdeconnect.telephony.request_mute'
        in result['device/2fe54440ccaa5e3b']['incoming-capabilities']
    )
    assert (
        result['device/2fe54440ccaa5e3b']['last-connection']
        == 'lan://192.168.0.45:1716'
    )
    assert result['device/2fe54440ccaa5e3b']['name'] == 'LM-K510'
    assert (
        'kdeconnect.battery'
        in result['device/2fe54440ccaa5e3b']['outgoing-capabilities']
    )
    assert (
        'kdeconnect.telephony'
        in result['device/2fe54440ccaa5e3b']['outgoing-capabilities']
    )
    assert result['device/2fe54440ccaa5e3b']['paired']
    assert result['device/2fe54440ccaa5e3b']['type'] == 'phone'
    assert (
        result['device/2fe54440ccaa5e3b']['custom-battery-notification-value']
        == 80
    )

    # unnamed group not in list
    assert len(result.keys()) == 2
    assert '' not in result
    assert (
        'unnamed-group-should-not-be-in-list'
        not in result['device/2fe54440ccaa5e3b']
    )
    assert 'unnamed-group-should-not-be-in-list' not in result['/']

    # wrong list of string example
    assert 'wrong-list-of-strings' not in result['device/2fe54440ccaa5e3b']
    assert 'wrong-list-of-strings' not in result['/']
