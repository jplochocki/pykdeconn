from typing import Union, List, Tuple, Dict
from pathlib import Path
import subprocess
import re
import logging

from anyio import run_process

from .utils import simple_toml_parser


log = logging.getLogger('pykdeconn.server')


async def is_running() -> Union[int, bool]:
    """
    Checks if the ``GSConnect`` extension is running (and returns its PID
    or False otherwise)
    """
    result = (
        await run_process(
            'ps ax | grep gsconnect@andyholmes.github.io',
            stdout=subprocess.PIPE,
        )
    ).stdout.decode()

    log.debug(f'run_process output: {result}')

    for app in result.splitlines():
        # ie "2562 ?  Sl 0:09 gjs /home/systemik/.local/share/gnome-shell
        # /extensions/gsconnect@andyholmes.github.io/service/daemon.js"
        if not re.search(
            r'gjs.+?gsconnect\@andyholmes\.github\.io/service/daemon\.js',
            app,
            re.I,
        ):
            continue
        return int(re.match(r'\s*(\d+)\s*', app).group(1))
    return False


async def try_kill() -> bool:
    """
    Tries to kill and disable GSConnect extension.
    """
    try:
        await run_process(
            'gnome-extensions disable gsconnect@andyholmes.github.io',
            check=True,
        )
        pid = await is_running()
        if pid:
            await run_process(
                f'kill -9 {pid}',
                check=True,
            )
        return True
    except subprocess.CalledProcessError:
        log.exception('Error while trying to kill GSConnect')
        return False


def gen_cert_files_paths() -> Tuple[Path, Path]:
    """
    Returns the GSConnect certificate.pem and private.pem paths.
    """
    return (
        Path('~/.config/gsconnect/certificate.pem').expanduser(),
        Path('~/.config/gsconnect/private.pem').expanduser(),
    )


async def list_device_names_and_ids() -> List[Tuple[str, str]]:
    """
    Lists device names and ids saved in GSConnect config. Returns list of
    tuples (id, name).
    """
    result = (
        await run_process(
            'dconf dump /org/gnome/shell/extensions/gsconnect/',
            stdout=subprocess.PIPE,
        )
    ).stdout.decode()

    log.debug(f'run_process output: {result}')

    result = simple_toml_parser(result)
    '''
    [/]
    devices=['32151f87b8be9b96', '2fe54440ccaa5e3b']
    ...
    [device/2fe54440ccaa5e3b]
    name='LM-K510'
    ...
    '''

    if '/' not in result or 'devices' not in result['/']:
        return []

    ret = []
    for dev_id in result['/']['devices']:
        dev_sec_id = f'device/{dev_id}'
        if dev_sec_id in result and 'name' in result[dev_sec_id]:
            ret.append((dev_id, result[dev_sec_id]['name']))
    return ret


async def read_device_config(dev_id) -> Dict[str, str]:
    """
    Parses device data saved in GSConnect config. Returns dictionary suitable
    to pass to settings.DeviceConfig.parse_obj().
    """
    result = (
        await run_process(
            'dconf dump /org/gnome/shell/extensions/gsconnect/',
            stdout=subprocess.PIPE,
        )
    ).stdout.decode()

    log.debug(f'run_process output: {result}')

    result = simple_toml_parser(result)

    dev_sec_id = f'device/{dev_id}'
    if dev_sec_id not in result:
        return {}

    fields = {
        'device-id': ('device_id', dev_id),
        'certificate-pem': ('certificate_PEM', ''),
        'incoming-capabilities': ('incoming_capabilities', []),
        'last-connection': ('last_ip', None),
        'name': ('device_name', ''),
        'outgoing-capabilities': ('outgoing_capabilities', []),
        'paired': ('paired', False),
        'type': ('type_', 'phone'),
    }
    ret = {}

    for source_key, (dest_key, default) in fields.items():
        if source_key not in result[dev_sec_id]:
            ret[dest_key] = default
            continue

        ret[dest_key] = result[dev_sec_id][source_key]
        if source_key == 'last-connection' and (
            m := re.match(
                r'^lan:\/\/(?P<ip>[0-9\.]+?)(:\d+)?$',
                result[dev_sec_id][source_key],
                re.I,
            )
        ):
            ret[dest_key] = m.group('ip')

    return ret


async def generate_identity_params(
    incoming_capabilities: List[str] = [],
    outgoing_capabilities: List[str] = [],
    device_type: str = 'laptop',
) -> Dict[str, str]:
    """
    Generates parameters for protocol.generate_IdentityPacket() call
    with the name and ID from the GSConnect configuration.
    """
    result = (
        await run_process(
            'dconf dump /org/gnome/shell/extensions/gsconnect/',
            stdout=subprocess.PIPE,
        )
    ).stdout.decode()

    log.debug(f'run_process output: {result}')

    result = simple_toml_parser(result)

    if '/' not in result:
        return {}

    return {
        'device_id': result['/'].get('id', ''),
        'device_name': result['/'].get('name', ''),
        'incoming_capabilities': incoming_capabilities,
        'outgoing_capabilities': outgoing_capabilities,
        'device_type': device_type,
    }
