import socket
from typing import Tuple, Union, Optional, Callable
import logging
import errno
import ssl

from anyio import (
    create_udp_socket,
    connect_tcp,
    create_tcp_listener,
    sleep,
    EndOfStream,
    IncompleteRead,
    TASK_STATUS_IGNORED,
)
from anyio.abc import TaskStatus, SocketAttribute
from anyio.streams.tls import TLSStream, TLSAttribute
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
from .hostconfig import HostConfig

log = logging.getLogger('pykdeconn.server')


class KDEConnectPortBusy(Exception):
    pass


async def send_host_id_packets_task(host_config: HostConfig):
    """
    Sends host device ID packets (``kdeconnect.identity``) using
    ``UDP broadcast``.
    """
    async with await create_udp_socket(
        family=socket.AF_INET,
        reuse_port=True,
        local_port=KDE_CONNECT_DEFAULT_PORT,
    ) as udp_sock:
        raw_socket = udp_sock.extra(SocketAttribute.raw_socket)
        raw_socket.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)

        while True:
            if host_config.could_send_my_id_packs:
                await udp_sock.sendto(
                    host_config.generate_IdentityPacket().prepare_to_send(),
                    '<broadcast>',
                    KDE_CONNECT_DEFAULT_PORT,
                )

            await sleep(3)


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
    *,
    task_status: TaskStatus = TASK_STATUS_IGNORED,
):
    """
    Outgoing connection (to the device that sended ID to us)
    """
    # connect
    async with await connect_tcp(
        remote_dev_config.last_ip, remote_dev_config.remote_port
    ) as socket, remote_dev_config.new_packet_sender:
        log.debug(f'Connected to {remote_dev_config.get_debug_id()}')

        # send identity
        id_pack = remote_dev_config.host_config.generate_IdentityPacket()
        await socket.send(id_pack.prepare_to_send())
        log.debug(
            f'Identity packet sent to {remote_dev_config.get_debug_id()}.'
        )

        # wrap TLS
        try:
            tls_socket = await TLSStream.wrap(
                socket,
                server_side=True,
                ssl_context=remote_dev_config.ssl_context(
                    ssl.Purpose.CLIENT_AUTH,
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

        remote_dev_config.on_connect(tls_socket)
        log.debug(
            f'Wrapped TLS connection with {remote_dev_config.get_debug_id()}.'
        )
        task_status.started()

        # handle pair if needed
        bssock = BufferedByteReceiveStream(tls_socket)
        if not remote_dev_config.paired and not await handle_pairing(
            remote_dev_config
        ):
            await handle_device_disconnection(remote_dev_config)
            return

        # receiving packets
        while True:
            pack = await receive_packet(
                bssock, remote_dev_config.get_debug_id()
            )
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


async def incoming_connection_task(
    host_config: HostConfig,
    new_incoming_connection_cb: Callable[
        [IPvAnyAddress, IdentityPacket], Optional[RemoteDeviceConfig]
    ],
    *,
    task_status: TaskStatus = TASK_STATUS_IGNORED,
):
    """
    Handles incoming connection (with the device we've send ID to).
    """

    async def handle_connection(client_socket):
        async with client_socket:
            host_config.could_send_my_id_packs = (
                False  # block sending next ids
            )

            remote_ip, remote_port = client_socket.extra(
                SocketAttribute.remote_address
            )
            dev_debug_id = f'{remote_ip}:{remote_port}'
            log.info(f'New incoming connection from {dev_debug_id}')

            # receive id packet
            bsock = BufferedByteReceiveStream(client_socket)
            remote_id_pack = await receive_packet(bsock, dev_debug_id)

            if type(remote_id_pack) is not IdentityPacket:
                log.error(
                    'kdeconnect.identity packet expected, got '
                    f'{remote_id_pack.type}. Connection closed.'
                )
                host_config.could_send_my_id_packs = True
                await client_socket.aclose()
                return

            log.info(
                (
                    'Incoming connection id packet received: '
                    f'{remote_id_pack.body.deviceName} / '
                    f'{remote_id_pack.body.deviceId} (IP: {remote_ip})'
                )
            )

            # back to user and receive device's config
            remote_dev_config = await new_incoming_connection_cb(
                remote_ip, remote_id_pack
            )

            if not remote_dev_config:  # False - ie. already connected device
                host_config.could_send_my_id_packs = True
                await client_socket.aclose()
                return
            dev_debug_id = remote_dev_config.get_debug_id()

            # wrap TLS
            try:
                tls_socket = await TLSStream.wrap(
                    client_socket,
                    server_side=False,
                    ssl_context=remote_dev_config.ssl_context(
                        ssl.Purpose.SERVER_AUTH
                    ),
                    hostname=remote_ip,
                    standard_compatible=False,
                )
            except ssl.SSLCertVerificationError:
                log.exception(
                    (
                        f'Certificate verification from {dev_debug_id} failed.'
                        ' Treat Device as unpaired and disconnect.'
                    )
                )
                remote_dev_config.on_unpair()
                remote_dev_config.on_disconnect()
                host_config.could_send_my_id_packs = True
                await client_socket.aclose()
                return

            remote_dev_config.on_connect(tls_socket)
            log.debug(f'Wrapped TLS connection with {dev_debug_id}')

            # try to obtain a device cert
            try:
                remote_dev_config.certificate_PEM = ssl.DER_cert_to_PEM_cert(
                    tls_socket.extra(TLSAttribute.peer_certificate_binary)
                )
                remote_dev_config.ssl_context(renew=True)
                log.debug(f'Retrieved new certificate from {dev_debug_id}')
            except Exception:
                log.exception(
                    f'Error while retrieving certificate from {dev_debug_id}.'
                )

            # handle pair if needed
            bssock = BufferedByteReceiveStream(tls_socket)
            if not remote_dev_config.paired and not await handle_pairing(
                remote_dev_config
            ):
                remote_dev_config.on_disconnect()
                host_config.could_send_my_id_packs = True
                await client_socket.aclose()
                return

            # receiving packets
            async with remote_dev_config.new_packet_sender:
                while True:
                    pack = await receive_packet(
                        bssock, remote_dev_config.get_debug_id()
                    )
                    if not pack:
                        await sleep(0.1)
                        continue

                    if type(pack) is PairPacket:
                        if not await handle_pairing(
                            remote_dev_config,
                            received_pair_packet=pack,
                        ):
                            remote_dev_config.on_disconnect()
                            host_config.could_send_my_id_packs = True
                            await client_socket.aclose()
                            return

                        continue

                    await remote_dev_config.new_packet_sender.send(pack)

            host_config.could_send_my_id_packs = True

    listener = await create_tcp_listener(
        local_port=KDE_CONNECT_DEFAULT_PORT, local_host='0.0.0.0'
    )
    task_status.started()
    await listener.serve(handle_connection)


async def receive_packet(
    bssock: BufferedByteReceiveStream, dev_debug_id: str
) -> Union[KDEConnectPacket, bool]:
    """
    Receiving and decoding a packet - the common part.
    """
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
                f'Received {is_known} packet type {pack.type} '
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
):
    """
    Device disconnection - the common code.
    """
    try:
        if remote_dev_config.connection_tls_socket:
            await remote_dev_config.connection_tls_socket.aclose()
    except Exception:
        log.exception(
            (
                'Ignored exception while disconnecting '
                f'{remote_dev_config.get_debug_id()}.'
            )
        )

    remote_dev_config.on_disconnect()
