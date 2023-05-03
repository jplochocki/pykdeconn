import logging
from typing import Tuple, Any
from functools import singledispatch

from anyio import (
    create_task_group,
    run as anyio_run,
    create_memory_object_stream,
)
from pydantic import IPvAnyAddress

from .protocol import (
    generate_IdentityPacket,
    IdentityPacket,
    ShareRequestPacket,
    wait_for_incoming_ids_task,
    KDEConnectPortBusy,
    outgoing_connection_task,
)
from .gsconnect import (
    generate_identity_params as gsconnect_identity_params,
    read_device_config as gsconnect_device_config,
    gen_cert_files_paths as gsconnect_cert_files_paths,
)
from .settings import DeviceConfig


log = logging.getLogger('pykdeconn.server')


async def server_main():
    # my_id_pack = generate_IdentityPacket(
    #     **(
    #         await gsconnect_identity_params(
    #             incoming_capabilities=['kdeconnect.share.request'],
    #             outgoing_capabilities=['kdeconnect.share.request'],
    #         )
    #     )
    # )
    # ignore_device_ids = [my_id_pack.body.deviceId]
    # my_device_certfile, my_device_keyfile = gsconnect_cert_files_paths()

    a = await gsconnect_identity_params(
        incoming_capabilities=['kdeconnect.share.request'],
        outgoing_capabilities=['kdeconnect.share.request'],
    )
    a['device_id'] = '2b90c70b-fd45-4da9-b8db-c84e95d686d7'
    my_id_pack = generate_IdentityPacket(**a)
    ignore_device_ids = [my_id_pack.body.deviceId]
    my_device_certfile, my_device_keyfile = gsconnect_cert_files_paths()

    my_device_certfile = '/home/systemik/github/pykdeconn/env_3.9/certificate-systemik-dell.pem'  # noqa
    my_device_keyfile = '/home/systemik/github/pykdeconn/env_3.9/private-systemik-dell.pem'  # noqa

    async with create_task_group() as main_group:
        # receiving new ids
        new_id_sender, new_id_receiver = create_memory_object_stream(
            item_type=Tuple[IPvAnyAddress, IdentityPacket]
        )
        try:
            main_group.start_soon(wait_for_incoming_ids_task, new_id_sender)
        except KDEConnectPortBusy:
            return  # TODO

        # handle id packs
        async for remote_ip, remote_id_pack in new_id_receiver:
            if remote_id_pack.body.deviceId in ignore_device_ids:
                continue

            remote_dev_config = DeviceConfig.parse_obj(
                await gsconnect_device_config(remote_id_pack.body.deviceId)
            )

            pack_sender, pack_receiver = create_memory_object_stream(
                item_type=Tuple[DeviceConfig, Any]
            )
            await main_group.start(
                outgoing_connection_task,
                remote_ip,
                remote_id_pack.body.tcpPort,
                remote_dev_config,
                my_id_pack,
                my_device_certfile,
                my_device_keyfile,
                pack_sender,
            )

            ignore_device_ids.append(remote_id_pack.body.deviceId)

            # handle incoming packs
            main_group.start_soon(
                handle_packets, remote_dev_config, pack_receiver
            )


# handle incoming packs
async def handle_packets(remote_dev_config: DeviceConfig, pack_receiver):
    async for pack in pack_receiver:
        await handle_packet(pack, remote_dev_config)


@singledispatch
async def handle_packet(pack, remote_dev_config: DeviceConfig):
    log.debug(f'Unknown packet {pack!r}')


@handle_packet.register
async def _(pack: ShareRequestPacket, remote_dev_config: DeviceConfig):
    print('Incoming file', pack)


def main():
    anyio_run(server_main)
