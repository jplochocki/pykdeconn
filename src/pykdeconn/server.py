import logging
from typing import Tuple

from anyio import (
    create_task_group,
    run as anyio_run,
    create_memory_object_stream,
)
from pydantic import IPvAnyAddress

from .protocol import (
    generate_IdentityPacket,
    IdentityPacket,
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
    my_id_pack = generate_IdentityPacket(
        **(
            await gsconnect_identity_params(
                incoming_capabilities=['kdeconnect.share.request'],
                outgoing_capabilities=['kdeconnect.share.request'],
            )
        )
    )
    ignore_device_ids = [my_id_pack.body.deviceId]
    my_device_certfile, my_device_keyfile = gsconnect_cert_files_paths()

    async with create_task_group() as main_group:
        # receiving new ids
        new_id_sender, new_id_receiver = create_memory_object_stream(
            item_type=Tuple[IPvAnyAddress, IdentityPacket]
        )
        try:
            main_group.start_soon(
                wait_for_incoming_ids_task, new_id_sender, ignore_device_ids
            )
        except KDEConnectPortBusy:
            return  # TODO

        # handle id pack
        async with new_id_receiver:
            remote_ip, remote_id_pack = await new_id_receiver.receive()

        remote_dev_config = DeviceConfig.parse_obj(
            await gsconnect_device_config(remote_id_pack.body.deviceId)
        )
        print('1', remote_dev_config, remote_ip, remote_id_pack)

        pack_sender, pack_receiver = create_memory_object_stream()
        main_group.start_soon(
            outgoing_connection_task,
            remote_ip,
            remote_id_pack.body.tcpPort,
            remote_dev_config,
            my_id_pack,
            my_device_certfile,
            my_device_keyfile,
            pack_sender,
        )

        # handle incoming packs


def main():
    anyio_run(server_main)
