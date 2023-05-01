import socket
from typing import Tuple, List, Any
import logging
import errno
import ssl
from pathlib import Path
import datetime

from anyio import create_udp_socket, connect_tcp
from anyio.streams.tls import TLSStream
from anyio.streams.memory import MemoryObjectSendStream
from pydantic import ValidationError, IPvAnyAddress


from .consts import KDE_CONNECT_DEFAULT_PORT
from .packets import IdentityPacket
from ..settings import DeviceConfig


log = logging.getLogger('pykdeconn.server')


class KDEConnectPortBusy(Exception):
    pass


async def wait_for_incoming_ids_task(
    new_id_sender: MemoryObjectSendStream[
        Tuple[IPvAnyAddress, IdentityPacket]
    ],
    ignore_device_ids: List = [],
):
    """
    Listens on ``UDP Broadcast`` for incoming device ID packets.
    """
    try:
        async with await create_udp_socket(
            family=socket.AF_INET,
            local_host='255.255.255.255',
            local_port=KDE_CONNECT_DEFAULT_PORT,
            reuse_port=True,
        ) as udp_sock:
            async for data, (host, port) in udp_sock:
                try:
                    pack = IdentityPacket.parse_raw(data.decode('utf-8'))
                except ValidationError:
                    log.exception(
                        f'Malformed packet from {host}:{port}\n{data}'
                    )
                    continue

                if pack.body.deviceId in ignore_device_ids:
                    continue

                log.debug(
                    (
                        f'Id packet received: {pack.body.deviceName} / '
                        f'{pack.body.deviceId} (IP: {host})'
                    )
                )

                async with new_id_sender:
                    await new_id_sender.send((host, pack))
    except OSError as e:
        if e.errno == errno.EADDRINUSE:
            raise KDEConnectPortBusy()
        else:
            raise


async def outgoing_connection_task(
    remote_ip: IPvAnyAddress,
    remote_port: int,
    remote_dev_config: DeviceConfig,
    my_id_pack: IdentityPacket,
    my_device_certfile: Path,
    my_device_keyfile: Path,
    incoming_pack_sender: MemoryObjectSendStream[Tuple[DeviceConfig, Any]],
):
    """
    Outgoing conection to the known device.
    """
    dev_debug_id = (
        f'{remote_ip}:{remote_port} ({remote_dev_config.device_name})'
    )

    # connect
    async with await connect_tcp(remote_ip, remote_port) as sock:
        log.debug(f'Connected to {dev_debug_id}')
        remote_dev_config.last_ip = remote_ip
        remote_dev_config.last_connection_date = datetime.datetime.now()

        # send identity
        await sock.send(my_id_pack.json().encode())
        log.debug('Identity packet sent to {dev_debug_id}.')

        # wrap TLS
        try:
            ssock = await TLSStream.wrap(
                sock,
                server_side=True,
                ssl_context=remote_dev_config.ssl_context(
                    ssl.Purpose.CLIENT_AUTH,
                    my_device_certfile,
                    my_device_keyfile,
                ),
                standard_compatible=False,
            )
        except ssl.SSLCertVerificationError:
            log.exception(
                (
                    f'Cert verify failed with {dev_debug_id}'
                    f' = treat us as unpaired.'
                )
            )
            remote_dev_config._connected = False
            remote_dev_config._connection_ssock = None
            remote_dev_config.paired = False
            remote_dev_config.certificate_PEM = ''
            remote_dev_config.save()
            return

        remote_dev_config._connection_ssock = ssock
        remote_dev_config._connected = True
        log.debug(f'Wrapped TLS connection with {dev_debug_id}.')
