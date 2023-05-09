import logging
from typing import Optional, Tuple, List
from functools import singledispatch
from pathlib import Path

from anyio import (
    create_task_group,
    run as anyio_run,
    create_memory_object_stream,
)
from anyio.abc import TaskGroup
from anyio.streams.memory import MemoryObjectReceiveStream
from pydantic import IPvAnyAddress

from .protocol import (
    KDE_CONNECT_DEFAULT_PORT,
    RemoteDeviceConfig,
    HostConfig,
    IdentityPacket,
    ShareRequestPacket,
    send_host_id_packets_task,
    wait_for_incoming_ids_task,
    KDEConnectPortBusy,
    outgoing_connection_task,
    incoming_connection_task,
    download,
    upload,
)
from .gsconnect import (
    generate_identity_params as gsconnect_identity_params,
    read_device_config as gsconnect_device_config,
)

from .settings import DeviceConfig  # noqa


log = logging.getLogger('pykdeconn.server')


async def server_main():
    host_config = HostConfig.parse_obj(
        await gsconnect_identity_params(
            incoming_capabilities=['kdeconnect.share.request'],
            outgoing_capabilities=['kdeconnect.share.request'],
        )
    )
    host_config.device_id = '2b90c70b-fd45-4da9-b8db-c84e95d686d7'
    ignore_device_ids = [host_config.device_id]

    host_config.device_certfile = Path(
        '~/github/pykdeconn/env_3.9/certificate-systemik-dell.pem'
    ).expanduser()
    host_config.device_keyfile = Path(
        '~/github/pykdeconn/env_3.9/private-systemik-dell.pem'
    ).expanduser()

    async with create_task_group() as main_group:
        # receiving new ids
        new_id_sender, new_id_receiver = create_memory_object_stream(
            item_type=Tuple[IPvAnyAddress, IdentityPacket]
        )
        try:
            main_group.start_soon(wait_for_incoming_ids_task, new_id_sender)
        except KDEConnectPortBusy:
            log.exception(
                f'KDE Connect post {KDE_CONNECT_DEFAULT_PORT} already in use.'
            )
            return  # TODO

        # handle incoming id packs
        main_group.start_soon(
            handle_incoming_id_packs_task,
            main_group,
            host_config,
            new_id_receiver,
            ignore_device_ids,
        )

        # handle incoming connections
        async def new_incoming_connection_cb(
            remote_ip: IPvAnyAddress, remote_id_pack: IdentityPacket
        ) -> Optional[RemoteDeviceConfig]:
            if remote_id_pack.body.deviceId in ignore_device_ids:
                return None

            remote_dev_config = RemoteDeviceConfig.initialize(
                await gsconnect_device_config(remote_id_pack.body.deviceId),
                host_config,
                remote_ip,
                remote_id_pack.body.tcpPort,
            )

            ignore_device_ids.append(remote_id_pack.body.deviceId)

            return remote_dev_config

        await main_group.start(
            incoming_connection_task, host_config, new_incoming_connection_cb
        )

        # send my id packets
        main_group.start_soon(send_host_id_packets_task, host_config)


async def handle_incoming_id_packs_task(
    main_group: TaskGroup,
    host_config: HostConfig,
    new_id_receiver: MemoryObjectReceiveStream,
    ignore_device_ids: List[str],
):
    async for remote_ip, remote_id_pack in new_id_receiver:
        if remote_id_pack.body.deviceId in ignore_device_ids:
            continue

        remote_dev_config = RemoteDeviceConfig.initialize(
            await gsconnect_device_config(remote_id_pack.body.deviceId),
            host_config,
            remote_ip,
            remote_id_pack.body.tcpPort,
        )

        await main_group.start(outgoing_connection_task, remote_dev_config)

        ignore_device_ids.append(remote_id_pack.body.deviceId)

        # handle incoming packs
        main_group.start_soon(handle_packets_task, remote_dev_config)

        await upload(remote_dev_config, Path('~/Foto_1440.jpg').expanduser())


# handle incoming packs
async def handle_packets_task(remote_dev_config: RemoteDeviceConfig):
    async for pack in remote_dev_config.new_packet_receiver:
        await handle_packet(pack, remote_dev_config)


@singledispatch
async def handle_packet(pack, remote_dev_config: RemoteDeviceConfig):
    log.debug(f'Unknown packet {pack!r}')


@handle_packet.register
async def _(pack: ShareRequestPacket, remote_dev_config: RemoteDeviceConfig):
    dest_file = Path('~').expanduser() / pack.body.filename
    await download(remote_dev_config, pack, dest_file)


def main():
    anyio_run(server_main)
