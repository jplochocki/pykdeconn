import logging

from anyio import create_task_group, run as anyio_run

from .protocol import (
    wait_for_incoming_id,
    make_outgoing_connection,
    KDE_CONNECT_DEFAULT_PORT,
)


log = logging.getLogger('pyconnect.server')


async def server_main():
    ignore_device_ids = []

    async with create_task_group() as main_group:

        async def on_incoming_id(id_pack, ip):
            await make_outgoing_connection(id_pack, ip)
            # print('1 on_incoming_id', ip, id_pack.json())

        try:
            main_group.start_soon(
                wait_for_incoming_id, on_incoming_id, ignore_device_ids
            )
        except OSError as e:
            if e.errno == 98:
                log.exception(
                    (
                        f'Port {KDE_CONNECT_DEFAULT_PORT} already in use. '
                        'Is any KDE Connect / GSConnect application still '
                        'running?'
                    )
                )
            else:
                raise


def main():
    anyio_run(server_main)
