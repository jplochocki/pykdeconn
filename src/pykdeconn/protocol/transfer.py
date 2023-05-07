from pathlib import Path
import ssl
from typing import Optional, Callable
import logging
from asyncio import iscoroutinefunction

from anyio import connect_tcp, open_file

from .packets import ShareRequestPacket
from .deviceconfig import RemoteDeviceConfig


log = logging.getLogger('pykdeconn.server')


async def download(
    remote_dev_config: RemoteDeviceConfig,
    share_pack: ShareRequestPacket,
    dest_file: Path,
    progress_cb: Optional[Callable[[int, int, int], None]] = None,
):
    """
    File download initiated by received packet kdeconnect.share.request.
    """
    log.info(
        (
            f'Downloading file {dest_file.name} from '
            f'{remote_dev_config.get_debug_id()}.'
        )
    )

    # download
    async with await connect_tcp(
        remote_dev_config.last_ip,
        share_pack.payloadTransferInfo.port,
        ssl_context=remote_dev_config.ssl_context(ssl.Purpose.SERVER_AUTH),
    ) as tls_socket:
        async with await open_file(dest_file, 'wb') as f:
            received = 0
            while received < share_pack.payloadSize:
                data = await tls_socket.receive()
                await f.write(data)
                received += len(data)

                progress_args = (share_pack.payloadSize, received, len(data))
                if progress_cb and iscoroutinefunction(progress_cb):
                    await progress_cb(*progress_args)
                elif progress_cb:
                    progress_cb(*progress_args)
                else:
                    percent = (
                        float(received) / float(share_pack.payloadSize) * 100.0
                    )
                    log.debug(
                        (
                            f'Downloading {dest_file.name}: {received} of '
                            f'{share_pack.payloadSize} ({percent:.1f}%)'
                        )
                    )

    log.info(
        f'Download connection closed with {remote_dev_config.get_debug_id()}.'
    )
