import os
import time
import ssl
import logging
import errno
import datetime
from pathlib import Path
from typing import Optional, Union, Callable, BinaryIO
from asyncio import iscoroutinefunction

from anyio import (
    sleep,
    connect_tcp,
    create_tcp_listener,
    open_file,
    wrap_file,
    Event,
    ClosedResourceError,
)
from anyio.abc import SocketAttribute
from anyio.streams.tls import TLSListener

from .consts import (
    KDE_CONNECT_TRANSFER_PORT_MIN,
    KDE_CONNECT_TRANSFER_PORT_MAX,
)
from .packets import ShareRequestPacket
from .settings import BaseDeviceConfig


log = logging.getLogger('pykdeconn.server')

# (dest_file, file size, received, percent of received  file)
ProgressCallbackT = Callable[
    [Union[str, Path, BinaryIO], int, int, float], None
]


async def download(
    remote_dev_config: BaseDeviceConfig,
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
                    if (
                        (time.time() - last_log_time) >= 3
                        or (percent - last_log_percent) > 20
                        or percent == 100.0
                    ):
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
    remote_dev_config: BaseDeviceConfig,
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
        last_modified = int(source_file.lstat().st_mtime)
    else:
        file_stream = wrap_file(source_file)

        file_stream.seek(-1, os.SEEK_END)
        file_size = file_stream.tell()
        file_stream.seek(0)

        # always set name in stream
        file_name = getattr(file_stream, 'name', '')
        last_modified = int(
            datetime.datetime.timestamp(datetime.datetime.now())
        )

    # start upload server
    upload_server = None
    server_shutdown_event = Event()

    async def handle_connection(client_socket):
        async with client_socket:
            remote_ip, remote_port = client_socket.extra(
                SocketAttribute.remote_address
            )
            log.info(
                f'Upload file {file_name} - device connected ({remote_ip}).'
            )

            if progress_cb and iscoroutinefunction(progress_cb):
                await progress_cb(source_file, file_size, 0, 0.0)
            elif progress_cb:
                progress_cb(source_file, file_size, 0, 0.0)

            async with file_stream as f:
                sent = 0
                last_log_time = time.time()
                last_log_percent = 0

                while sent < file_size:
                    data = await f.read(63 * 1024)
                    await client_socket.send(data)

                    sent += len(data)

                    percent = float(sent) / float(file_size) * 100.0
                    progress_args = (
                        source_file,
                        file_size,
                        sent,
                        percent,
                    )
                    if progress_cb and iscoroutinefunction(progress_cb):
                        await progress_cb(*progress_args)
                    elif progress_cb:
                        progress_cb(*progress_args)
                    else:
                        # log after +20% or next 3 seconds
                        if (
                            (time.time() - last_log_time) >= 3
                            or (percent - last_log_percent) > 20
                            or percent == 100.0
                        ):
                            log.debug(
                                (
                                    f'Uploading {file_name}: sent {sent} of '
                                    f'{file_size} ({percent:.1f}%)'
                                )
                            )
                            last_log_time = time.time()
                            last_log_percent = percent

        await upload_server.aclose()
        server_shutdown_event.set()

    # checking the first available upload port
    transfer_port = 0
    for port in range(
        KDE_CONNECT_TRANSFER_PORT_MIN, KDE_CONNECT_TRANSFER_PORT_MAX + 1
    ):
        try:
            upload_server = TLSListener(
                await create_tcp_listener(
                    local_port=port, local_host='0.0.0.0'
                ),
                ssl_context=remote_dev_config.ssl_context(
                    ssl.Purpose.CLIENT_AUTH
                ),
                standard_compatible=False,
            )
            log.debug(f'Selected port {port} for file upload.')
            transfer_port = port

            break
        except OSError as e:
            if e.errno == errno.EADDRINUSE:  # port already in use - ignore
                continue
            raise e

    # send share request packet
    total_files_size = (
        total_files_size
        if total_files_size != -1 and total_files_size > file_size
        else file_size
    )

    share_pack = ShareRequestPacket.generate(
        file_path=Path(file_name),
        file_size=file_size,
        last_modified=last_modified,
        number_of_files=number_of_files,
        total_size=total_files_size,
        transfer_port=transfer_port,
    )

    serve_forever = upload_server.serve(handle_connection)
    await sleep(0.01)

    await remote_dev_config.connection_tls_socket.send(
        share_pack.prepare_to_send()
    )
    log.info('Upload packet sent.')

    try:
        await serve_forever
    except ClosedResourceError:
        server_shutdown_event.set()

    await server_shutdown_event.wait()
    log.debug('Transfer server closed.')
