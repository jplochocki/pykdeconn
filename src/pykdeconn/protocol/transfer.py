import os
import time
import ssl
import logging
from pathlib import Path
from typing import Optional, Union, Callable, BinaryIO
from asyncio import iscoroutinefunction


from anyio import connect_tcp, open_file, wrap_file, Event
from anyio.abc import SocketAttribute

from .packets import ShareRequestPacket
from .deviceconfig import RemoteDeviceConfig


log = logging.getLogger('pykdeconn.server')

# (dest_file, file size, received, percent of received  file)
ProgressCallbackT = Callable[
    [Union[str, Path, BinaryIO], int, int, float], None
]


async def download(
    remote_dev_config: RemoteDeviceConfig,
    share_pack: ShareRequestPacket,
    dest_file: Union[str, Path, BinaryIO],
    progress_cb: Optional[ProgressCallbackT] = None,
):
    """
    File download initiated by received packet kdeconnect.share.request.
    """
    log.info(
        (
            f'Downloading file {share_pack.body.filename} from '
            f'{remote_dev_config.get_debug_id()}.'
        )
    )

    # download
    async with await connect_tcp(
        remote_dev_config.last_ip,
        share_pack.payloadTransferInfo.port,
        ssl_context=remote_dev_config.ssl_context(ssl.Purpose.SERVER_AUTH),
    ) as tls_socket:
        if type(dest_file) is str or isinstance(dest_file, Path):
            dest_file_obj = await open_file(dest_file, 'wb')
        else:
            dest_file_obj = wrap_file(dest_file)

        async with dest_file_obj as f:
            received = 0
            last_log_time = time.time()
            last_log_percent = 0

            while received < share_pack.payloadSize:
                data = await tls_socket.receive()
                await f.write(data)
                received += len(data)

                percent = (
                    float(received) / float(share_pack.payloadSize) * 100.0
                )
                progress_args = (
                    dest_file,
                    share_pack.payloadSize,
                    received,
                    percent,
                )
                if progress_cb and iscoroutinefunction(progress_cb):
                    await progress_cb(*progress_args)
                elif progress_cb:
                    progress_cb(*progress_args)
                else:
                    # log after +20% or next 3 seconds
                    if (time.time() - last_log_time) >= 3 or (
                        percent - last_log_percent
                    ) > 20:
                        log.debug(
                            (
                                f'Downloading {dest_file.name}: {received} of '
                                f'{share_pack.payloadSize} ({percent:.1f}%)'
                            )
                        )
                        last_log_time = time.time()
                        last_log_percent = percent

    log.info(
        f'Download connection closed with {remote_dev_config.get_debug_id()}.'
    )


async def upload(
    remote_dev_config: RemoteDeviceConfig,
    source_file: Union[str, Path, BinaryIO],
    progress_cb: Optional[ProgressCallbackT] = None,
    number_of_files: int = 1,
    total_files_size: int = -1,
):
    """
    Upload file to the remote device.
    """
    if type(source_file) is str or isinstance(source_file, Path):
        file_size = Path(source_file).stat().st_size
        file_name = Path(source_file).name
        file_stream = await open_file(source_file, 'rb')
    else:
        file_stream = wrap_file(source_file)

        file_stream.seek(-1, os.SEEK_END)
        file_size = file_stream.tell()
        file_stream.seek(0)

        # always set name in stream
        file_name = getattr(file_stream, 'name', '')

    # start upload server
    server = None
    close_server_event = Event()

    async def handle_connection(sock_client):
        async with sock_client:
            remote_ip, remote_port = sock_client.extra(
                SocketAttribute.remote_address
            )
            log.info(
                f'Upload file {file_name} - device connected ({remote_ip}).'
            )
