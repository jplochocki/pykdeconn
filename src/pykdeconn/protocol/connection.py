import socket
from typing import Tuple, Union
import logging
import errno
import ssl
from pathlib import Path

from anyio import (
    create_udp_socket,
    connect_tcp,
    sleep,
    EndOfStream,
    IncompleteRead,
    TASK_STATUS_IGNORED,
)
from anyio.abc import SocketStream, TaskStatus
from anyio.streams.tls import TLSStream
from anyio.streams.buffered import BufferedByteReceiveStream
from anyio.streams.memory import MemoryObjectSendStream
from pydantic import ValidationError, IPvAnyAddress


from .consts import KDE_CONNECT_DEFAULT_PORT
from .packets import (
    KDEConnectPacket,
    UnknownPacket,
    IdentityPacket,
    PairPacket,
    get_packet_by_kde_type_name,
)
from .deviceconfig import RemoteDeviceConfig


log = logging.getLogger('pykdeconn.server')


class KDEConnectPortBusy(Exception):
    pass


async def wait_for_incoming_ids_task(
    new_id_sender: MemoryObjectSendStream[
        Tuple[IPvAnyAddress, IdentityPacket]
    ],
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
        ) as udp_sock, new_id_sender:
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

                log.debug(
                    (
                        f'Id packet received: {pack.body.deviceName} / '
                        f'{pack.body.deviceId} (IP: {remote_ip})'
                    )
                )

                await new_id_sender.send((remote_ip, pack))
    except OSError as e:
        if e.errno == errno.EADDRINUSE:
            raise KDEConnectPortBusy()
        else:
            raise


async def outgoing_connection_task(
    remote_dev_config: RemoteDeviceConfig,
    my_id_pack: IdentityPacket,
    my_device_certfile: Path,
    my_device_keyfile: Path,
    *,
    task_status: TaskStatus = TASK_STATUS_IGNORED,
):
    """
    Outgoing connection (to the device that sended ID to us)
    """
    # connect
    async with await connect_tcp(
        remote_dev_config.last_ip, remote_dev_config.remote_port
    ) as sock, remote_dev_config.new_packet_sender:
        log.debug(f'Connected to {remote_dev_config.get_debug_id()}')

        # send identity
        await sock.send(my_id_pack.prepare_to_send())
        log.debug(
            f'Identity packet sent to {remote_dev_config.get_debug_id()}.'
        )

        # wrap TLS
        try:
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
                    'Certificate verification from '
                    f'{remote_dev_config.get_debug_id()} failed. Treat Device'
                    ' as unpaired and disconnect.'
                )
            )
            remote_dev_config.on_unpair()
            remote_dev_config.on_disconnect()

            raise RuntimeError('Not connected')  # TODO

        remote_dev_config.on_connect(ssock)
        log.debug(
            f'Wrapped TLS connection with {remote_dev_config.get_debug_id()}.'
        )
        task_status.started()

        # handle pair if needed
        bssock = BufferedByteReceiveStream(ssock)
        if not remote_dev_config.paired and not await handle_pairing(
            remote_dev_config
        ):
            await handle_device_disconnection(remote_dev_config)
            return

        # receiving packets
        while True:
            pack = await receive_packet(remote_dev_config, bssock)
            if not pack:
                await sleep(0.1)
                continue

            if type(pack) is PairPacket:
                if not await handle_pairing(
                    remote_dev_config,
                    received_pair_packet=pack,
                ):
                    await handle_device_disconnection(remote_dev_config)
                    return

                continue

            await remote_dev_config.new_packet_sender.send(pack)


async def receive_packet(
    remote_dev_config: RemoteDeviceConfig, bssock: BufferedByteReceiveStream
) -> Union[KDEConnectPacket, bool]:
    """
    Receiving and decoding a packet - the common part.
    """
    dev_debug_id = remote_dev_config.get_debug_id()

    try:
        pack_data = await bssock.receive_until(b'\n', 1024 * 1024 * 10)
    except (EndOfStream, IncompleteRead):
        return False
    except Exception:
        log.exception(f'Error while receiving packet from {dev_debug_id}.')
        return False

    try:
        pack = UnknownPacket.parse_raw(pack_data)

        cls = get_packet_by_kde_type_name(pack.type)

        is_known = 'unknown' if cls is UnknownPacket else 'known'
        log.debug(
            (
                f'Received {is_known} packet type {pack.type}'
                f'from {dev_debug_id}.'
            )
        )

        # handle only known packets
        if cls is UnknownPacket:
            return False

        return cls.parse_raw(pack_data)
    except ValidationError:
        log.exception(
            f'Error while decoding packet from {dev_debug_id}\n{pack_data}'
        )

    return False


async def handle_pairing(
    remote_dev_config: RemoteDeviceConfig,
    received_pair_packet: PairPacket = None,
) -> bool:
    """
    Common part of device pairing.
    """
    dev_debug_id = remote_dev_config.get_debug_id()

    # pair packet received from remote device
    if received_pair_packet:
        if received_pair_packet.body.pair:  # paired by remote device
            remote_dev_config.paired = True
            log.info(f'Device {dev_debug_id} paired.')
            return True

        elif not received_pair_packet.body.pair:  # unpaired by remote device
            remote_dev_config.paired = False
            log.info(f'Unpairing {dev_debug_id} done.')
            return False

    elif not remote_dev_config.paired:
        pass


async def send_unpair_request(remote_dev_config: RemoteDeviceConfig) -> bool:
    """
    Sends unpair request to the remote device.
    """
    if (
        not remote_dev_config.paired
        or not remote_dev_config.connected
        or not remote_dev_config.connection_tls_socket
    ):
        return False

    await remote_dev_config.connection_tls_socket.send(
        PairPacket.generate(pair=False).prepare_to_send()
    )

    return True


async def handle_device_disconnection(
    remote_dev_config: RemoteDeviceConfig,
    anysock: Union[SocketStream, TLSStream],
    dev_debug_id: str,
):
    """
    Device disconnection - the common code.
    """
    try:
        await anysock.aclose()
    except Exception:
        log.exception(
            (
                'Ignored exception while disconnecting '
                f'{remote_dev_config.get_debug_id()}.'
            )
        )

    remote_dev_config.on_disconnect()
