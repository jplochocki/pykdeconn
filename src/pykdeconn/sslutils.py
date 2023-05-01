import logging
from pathlib import Path
import subprocess
import re

from anyio import run_process


log = logging.getLogger('pykdeconn.server')


async def generate_cert(
    device_id: str,
    device_certfile: Path,
    device_keyfile: Path,
):
    """
    Generates ``SSL`` certificate files.
    """
    await run_process(
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

    log.debug(
        (
            f'Certificate files ({device_certfile} and {device_keyfile}) '
            f'generated for id {device_id}.'
        )
    )


async def read_cert_common_name(cert_file: Path) -> str:
    """
    Reads ``CN`` field from ``SSL`` certificate.
    """
    result = (
        await run_process(
            [
                'openssl',
                'x509',
                '-in',
                cert_file,
                '-noout',
                '-subject',
                '-inform',
                'pem',
            ],
            stdout=subprocess.PIPE,
            check=True,
        )
    ).stdout.decode()

    log.debug(f'run_process output: {result}')

    # subject=O = jplochocki.github.io, OU = PyKDEConn, CN = e0f7faa7...
    m = re.search(r'CN\s*=\s*([^,\n]*)', result, re.I)
    if not m:
        return None

    m = m.group(1)
    log.info(f'Certificate\'s CN name readed: {cert_file} = {m}')

    return m
