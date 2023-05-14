import logging
from typing import Optional
from functools import singledispatch
from pathlib import Path

from anyio import (
    create_task_group,
    run as anyio_run,
)
from anyio.abc import TaskGroup
from pydantic import IPvAnyAddress

from .protocol import (
    KDE_CONNECT_DEFAULT_PORT,
    BaseDeviceConfig,
    BaseHostConfig,
    IdentityPacket,
    ShareRequestPacket,
    send_host_id_packets_task,
    wait_for_incoming_ids_task,
    KDEConnectPortBusy,
    outgoing_connection_task,
    incoming_connection_task,
    download,
)

from .settings import host_config


log = logging.getLogger('pykdeconn.server')


async def server_main():
    async with create_task_group() as main_group:
        # receiving new ids
        try:
            main_group.start_soon(
                wait_for_incoming_ids_task, host_config.new_id_sender
            )
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
        )

        # handle incoming connections
        async def new_incoming_connection_cb(
            remote_ip: IPvAnyAddress, remote_id_pack: IdentityPacket
        ) -> Optional[BaseDeviceConfig]:
            if remote_id_pack.body.deviceId in host_config.ignore_device_ids:
                return None
            remote_dev_config = host_config.get_or_create_device_config(
                remote_id_pack, remote_ip
            )
            host_config.ignore_device_ids.append(remote_id_pack.body.deviceId)

            return remote_dev_config
            # przyjmowanie pakietów - cd

        await main_group.start(
            incoming_connection_task, host_config, new_incoming_connection_cb
        )

        # send my id packets
        main_group.start_soon(send_host_id_packets_task, host_config)

        # handle incoming packs
        # main_group.start_soon(handle_packets_task, remote_dev_config)


async def handle_incoming_id_packs_task(
    main_group: TaskGroup,
    host_config: BaseHostConfig,
):
    async for remote_ip, remote_id_pack in host_config.new_id_receiver:
        host_config.update_recent_device_ids_list(remote_id_pack)

        if remote_id_pack.body.deviceId in host_config.ignore_device_ids:
            continue

        remote_dev_config = host_config.get_or_create_device_config(
            remote_id_pack, remote_ip, must_be_paired=True
        )
        if not remote_dev_config:
            return

        await main_group.start(outgoing_connection_task, remote_dev_config)

        host_config.ignore_device_ids.append(remote_id_pack.body.deviceId)

        # handle incoming packs
        main_group.start_soon(handle_packets_task, remote_dev_config)

        # await upload(remote_dev_config, Path('~/Foto_1440.jpg').expanduser())


# handle incoming packs
async def handle_packets_task(remote_dev_config: BaseDeviceConfig):
    async for pack in remote_dev_config.new_packet_receiver:
        await handle_packet(pack, remote_dev_config)


@singledispatch
async def handle_packet(pack, remote_dev_config: BaseDeviceConfig):
    log.debug(f'Unknown packet {pack!r}')


@handle_packet.register
async def _(pack: ShareRequestPacket, remote_dev_config: BaseDeviceConfig):
    dest_file = Path('~').expanduser() / pack.body.filename
    await download(remote_dev_config, pack, dest_file)


def main():
    anyio_run(server_main)
