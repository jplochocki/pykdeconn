import logging
from pathlib import Path
import subprocess
import re


log = logging.getLogger('pyconnect.server')


def generate_cert(
    device_name: str,
    device_id: str,
    device_certfile: Path,
    device_keyfile: Path,
):
    """
    Generates ``SSL`` certificate files.
    """
    log.debug(
        (
            f'Generating certs for {device_name} / {device_id} '
            f'({device_certfile} and {device_keyfile})'
        )
    )

    openssl = subprocess.run(
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
            f'/O=jplochocki.github.io/OU=PyConnect/CN={device_id}',
        ],
        stderr=subprocess.STDOUT,
        check=False,
    )

    if openssl.returncode != 0:
        raise RuntimeError(
            (
                f'OpenSSL returned an error code ({openssl.returncode})\n'
                f'{openssl.stdout.decode()}'
            )
        )

    log.info(f'Cert generated for {device_name} / {device_id}')


def read_cert_common_name(cert_file: Path) -> str:
    """
    Reads ``CN`` field from ``SSL`` certificate.
    """
    openssl = subprocess.run(
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
    )

    # subject=O = jplochocki.github.io, OU = PYConnect, CN = e0f7faa7...
    a = re.search(r'CN\s*=\s*([^,\n]*)', openssl.stdout.decode(), re.I)
    if not a:
        raise RuntimeError(
            f'Invalid cert CN string ({openssl.stdout.decode()})'
        )
    a = a.group(1)
    log.info(f'Certificate\'s CN name readed: {cert_file} = {a}')

    return a
