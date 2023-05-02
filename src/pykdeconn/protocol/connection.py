import socket
from typing import Tuple, List, Dict, Any, Union
import logging
import errno
import ssl
from pathlib import Path
import datetime
import json

from anyio import (
    create_udp_socket,
    connect_tcp,
    sleep,
    EndOfStream,
    IncompleteRead,
)
from anyio.streams.tls import TLSStream
from anyio.streams.buffered import BufferedByteReceiveStream
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
            async for data, (remote_ip, remote_port) in udp_sock:
                try:
                    pack = IdentityPacket.parse_raw(data.decode('utf-8'))
                except ValidationError:
                    log.exception(
                        (
                            f'Malformed packet from {remote_ip}:{remote_port}'
                            f'\n{data}'
                        )
                    )
                    continue

                if pack.body.deviceId in ignore_device_ids:
                    continue

                log.debug(
                    (
                        f'Id packet received: {pack.body.deviceName} / '
                        f'{pack.body.deviceId} (IP: {remote_ip})'
                    )
                )

                async with new_id_sender:
                    await new_id_sender.send((remote_ip, pack))
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
        await sock.send((my_id_pack.json() + '\n').encode('utf-8'))
        log.debug(f'Identity packet sent to {dev_debug_id}.')

        # wrap TLS
        try:
            # ipdb.set_trace()
            ssock = await TLSStream.wrap(
                sock,
                server_side=True,
                ssl_context=remote_dev_config.ssl_context(
                    ssl.Purpose.CLIENT_AUTH,
                    my_device_certfile,
                    my_device_keyfile,
                ),
                standard_compatible=True,
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

        # handle pair if needed
        bssock = BufferedByteReceiveStream(ssock)
        if not remote_dev_config.paired:
            pass
            # if not await handle_pairing(dev_config, bssock):
            #     await on_device_disconnected(dev_config, ssock)
            #     return

        # receiving packets
        # main_group.start_soon(receive_contacts, dev_config)

        while True:
            pack = await receive_packet(bssock)
            if not pack:
                await sleep(1)
                continue

            # if not await handle_packet(dev_config, pack, bssock, main_group):
            #     await on_device_disconnected(dev_config, ssock)
            #     break


async def receive_packet(
    bssock: BufferedByteReceiveStream,
) -> Union[Dict, bool]:
    """
    Receiving and decoding a packet - the common part
    """
    try:
        pack_data = await bssock.receive_until(b'\n', 1024 * 1024 * 10)
    except (EndOfStream, IncompleteRead):
        return False
    except Exception:
        log.exception('Error while receiving packet.')
        return False

    try:
        pack = json.loads(pack_data)
    except json.JSONDecodeError:
        log.exception(f'Error while decoding packet\n{pack_data}')
        return False

    log.debug(f'Received packet:\n{pack!r}')

    return pack
