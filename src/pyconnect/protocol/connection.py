import socket
from typing import List, Callable
import logging

from anyio import create_udp_socket
from pydantic import ValidationError


from .consts import KDE_CONNECT_DEFAULT_PORT
from .packets import IdentityPacket
from ..settings import DeviceConfig


log = logging.getLogger('pyconnect.server')


async def wait_for_incoming_id(
    on_new_incoming_id_cb: Callable[[IdentityPacket, str], None],
    ignore_device_ids: List = [],
):
    """
    Listens on ``UDP Broadcast`` for incoming device ID packets.
    """
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
                log.exception(f'Malformed packet from {host}:{port}\n{data}')
                continue

            if pack.body.deviceId in ignore_device_ids:
                continue

            log.debug(
                (
                    f'Id packet received: {pack.body.deviceName} / '
                    f'{pack.body.deviceId} (IP: {host})'
                )
            )

            if on_new_incoming_id_cb:
                await on_new_incoming_id_cb(pack, host)


async def make_outgoing_connection(
    id_packet: IdentityPacket,
    remote_ip: str,
):
    """
    Outgoing conection to the known device.
    """
    dev_config = DeviceConfig.load_from_id_packet(id_packet)
    remote_port = id_packet.body.tcpPort
    print('1', dev_config, remote_port)
