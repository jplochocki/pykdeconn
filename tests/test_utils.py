from pathlib import Path

import pytest  # noqa

from pyconnect.utils import simple_toml_parser


dconf_result_example = (
    Path(__file__).parent / 'examples' / 'dconf-result-example.txt'
)


def test_simple_toml_parser():
    dconf = simple_toml_parser(dconf_result_example.read_text())
    assert '/' in dconf
