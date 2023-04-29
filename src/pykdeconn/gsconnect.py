from typing import Union, List, Tuple
from pathlib import Path
import subprocess
import re
import logging

from anyio import run_process


log = logging.getLogger('pykdeconn.server')


async def is_running() -> Union[int, bool]:
    """
    Checks if the ``GSConnect`` extension is running (and returns its PID
    or False otherwise)
    """
    gsconnect = await run_process(
        'ps ax | grep gsconnect@andyholmes.github.io',
        stdout=subprocess.PIPE,
    )

    for app in gsconnect.stdout.decode().splitlines():
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


def try_kill() -> bool:
    """
    Tries to kill and disable GSConnect extension.
    """
    try:
        subprocess.check_call(
            'gnome-extensions disable gsconnect@andyholmes.github.io',
            shell=True,
        )
        pid = is_running()
        if pid:
            subprocess.check_call(
                f'kill -9 {pid}',
                shell=True,
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


def list_device_name_and_id():
    pass


def list_known_devices() -> List[Tuple[str, str]]:
    dconf = subprocess.check_output(
        'dconf dump /org/gnome/shell/extensions/gsconnect/', shell=True
    )
    print(dconf)


def read_device_config():
    pass
