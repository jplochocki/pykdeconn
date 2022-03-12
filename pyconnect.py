#! /usr/bin/env python3

import logging
import logging.config
import socket
import json
import time
import ssl
import os.path
import signal
import typing
import re
import sys
from dataclasses import dataclass, field, asdict  # noqa
import datetime
import uuid
import subprocess

import anyio
from anyio.abc import SocketStream
from anyio.streams.tls import TLSStream, TLSListener
from anyio.streams.buffered import BufferedByteReceiveStream

import ravel
import dbussy
from dbussy import DBUS

import click


class CustomFormatter(logging.Formatter):
    grey = '\x1b[38;20m'
    yellow = '\x1b[33;20m'
    red = '\x1b[31;20m'
    bold_red = '\x1b[31;1m'
    green = '\x1b[32m'
    reset = '\x1b[0m'
    format = ('[ %(levelname)s %(asctime)s %(funcName)20s()::%(lineno)d ]\n\t'
              '%(message)s')

    FORMATS = {
        logging.DEBUG: grey + format + reset,
        logging.INFO: green + format + reset,
        logging.WARNING: yellow + format + reset,
        logging.ERROR: red + format + reset,
        logging.CRITICAL: bold_red + format + reset
    }

    def format(self, record):
        log_fmt = self.FORMATS.get(record.levelno)
        formatter = logging.Formatter(log_fmt)
        return formatter.format(record)


logging.config.dictConfig({
    'version': 1,
    'disable_existing_loggers': True,
    'loggers': {
        'pyconnect.detailed_log': {
            'level': 'DEBUG',
            'handlers': [
                'detailed_console_handler'
            ],
            'propagate': True
        }
    },
    'handlers': {
        'detailed_console_handler': {
            'level': 'DEBUG',
            'class': 'logging.StreamHandler',
            'formatter': 'detailed_console_handler_fmt'
        }
    },
    'formatters': {
        'detailed_console_handler_fmt': {
            'class': __name__ + '.CustomFormatter'
        }
    }
})


log = logging.getLogger('pyconnect.detailed_log')


DBUS_IFACE_SERVER = 'io.github.jplochocki.pyconnect'
DBUS_SERVER_PATH = '/'
DBUS_IFACE_CLIENT = 'io.github.jplochocki.pyconnect.client'
DBUS_CLIENT_PATH = '/'


KDE_CONNECT_DEFAULT_PORT = 1716
KDE_CONNECT_TRANSFER_MIN = 1739
KDE_CONNECT_TRANSFER_MAX = 1764
KDE_CONNECT_PROTOCOL_VERSION = 7

PYCONNECT_DEVICE_ID = None
PYCONNECT_DEVICE_NAME = None

PYCONNECT_INCOMING_CAPABILITIES = [
    'kdeconnect.share.request',
]
PYCONNECT_OUTGOING_CAPABILITIES = [
    'kdeconnect.share.request',
]

PYCONNECT_CERTFILE = None
PYCONNECT_KEYFILE = None

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           'pyconnect.config.json')
config = None

auto_send_pair_request = False
pair_request_auto_accept = True

KDEConnectPacket = typing.Dict[str, typing.Any]


class EnhancedJSONEncoder(json.JSONEncoder):
    def default(self, obj):
        type_name = re.match(r'^(.+?)\(', repr(obj)).group(1)
        args = {
            'datetime.datetime': ('year', 'month', 'day', 'hour', 'minute',
                                  'second'),
            'datetime.date': ('year', 'month', 'day'),
            'datetime.time': ('hour', 'minute', 'second', 'microsecond'),
            'datetime.timedelta': ('days', 'seconds', 'microseconds'),
        }.get(type_name, None)

        if args:
            return {
                '__type__': type_name,
                '__args__': json.dumps([getattr(obj, a) for a in args])
            }

        return super().default(obj)


class EnhancedJSONDecoder(json.JSONDecoder):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, object_hook=self.object_hook,
                         **kwargs)

    def object_hook(self, d):
        available_types = ('datetime.datetime', 'datetime.date',
                           'datetime.time', 'datetime.timedelta')
        if d.get('__type__', None) not in available_types:
            return d

        o = sys.modules[__name__]
        for e in d['__type__'].split('.'):
            o = getattr(o, e)

        args = json.loads(d.get('__args__', ''))
        return o(*args)


def load_config() -> typing.Dict:
    """
    Loads config file.
    """
    global config
    config = {
        'devices': {}
    }

    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r') as f:
            config = json.load(f, cls=EnhancedJSONDecoder)

    return config


def save_config():
    """
    Saves config file.
    """
    with open(CONFIG_FILE, 'w') as f:
        json.dump(config, f, indent=4, cls=EnhancedJSONEncoder)


@dataclass
class DeviceConfig:
    device_id: str
    device_name: str
    certificate_PEM: str = field(default='')
    paired: bool = field(default=False)
    last_ip: str = field(default='')
    last_connection_date: datetime.datetime = field(
        default_factory=lambda: datetime.datetime.now())

    _cache: typing.ClassVar = {}

    def __post_init__(self):
        self._ssl_cnx_cache = {}
        self._connected = False
        self.connection_ssock = None

        DeviceConfig._cache[self.device_id] = self

    @property
    def connected(self) -> bool:
        return self._connected

    @connected.setter
    def connected(self, v: bool) -> None:
        self._connected = v

        if v:
            DeviceConfig._cache[self.device_id] = self
            self.last_connection_date = datetime.datetime.now()
            self.save()
        else:
            self.connection_ssock = None
            if self.device_id in DeviceConfig._cache:
                del DeviceConfig._cache[self.device_id]

    @staticmethod
    def is_known_device(device_id: str) -> bool:
        """
        Checks in configuration whether the device ID is known.
        """
        return 'devices' in config and device_id in config['devices']

    @staticmethod
    def is_connected_device(device_id: str) -> bool:
        """
        Checks by ID whether the device is connected.
        """
        if device_id not in DeviceConfig._cache:
            return False
        return DeviceConfig._cache[device_id].connected

    @staticmethod
    def is_paired_device(device_id: str) -> bool:
        """
        Checks in configuration whether the device is paired.
        """
        return config.get('devices', {}).get(device_id, {}).get(
            'paired', False)

    @classmethod
    def load_from_id_pack(cls, pack: KDEConnectPacket):
        """
        Loads (or create) ``DeviceConfig`` from ``kdeconnect.identity`` packet.
        """
        if pack['type'] != 'kdeconnect.identity':
            raise RuntimeError(
                f'kdeconnect.identity packet expected, got {pack["type"]}')

        pack_body = pack['body']
        if 'deviceId' not in pack_body or pack_body['deviceId'] == '':
            raise RuntimeError(
                f'Identity packet without body.deviceId\n{pack=}')

        if pack_body['deviceId'] in DeviceConfig._cache:
            return DeviceConfig._cache[pack_body['deviceId']]

        if DeviceConfig.is_known_device(pack_body['deviceId']):
            dev = cls.load(pack_body['deviceId'])
            dev.device_name = pack_body['deviceName']
        else:
            dev = cls(device_id=pack_body['deviceId'],
                      device_name=pack_body['deviceName'])

        dev.save()
        return dev

    @classmethod
    def load(cls, device_id: str):
        """
        Loads device config.
        """
        if device_id in DeviceConfig._cache:
            return DeviceConfig._cache[device_id]

        if 'devices' in config and device_id in config['devices']:
            return cls(**config['devices'][device_id])

        raise RuntimeError(f'Device id "{device_id}" not  configured.')

    def save(self):
        """
        Saves device config.
        """
        if 'devices' not in config:
            config['devices'] = {}

        config['devices'][self.device_id] = asdict(self)
        save_config()

    def ssl_context(self, purpose: ssl.Purpose = ssl.Purpose.CLIENT_AUTH,
                    renew: bool = False) -> ssl.SSLContext:
        """
        Loads ``SSLContext`` for the specified ``purpose``.
        """
        if not renew and purpose.shortname in self._ssl_cnx_cache:
            return self._ssl_cnx_cache[purpose.shortname]

        cnx = ssl.create_default_context(purpose)
        cnx.load_cert_chain(
            certfile=PYCONNECT_CERTFILE, keyfile=PYCONNECT_KEYFILE)

        cnx.check_hostname = False
        cnx.verify_mode = ssl.CERT_NONE

        if self.certificate_PEM:
            cnx.load_verify_locations(cadata=self.certificate_PEM)
            cnx.verify_mode = ssl.CERT_REQUIRED

        self._ssl_cnx_cache[purpose.shortname] = cnx
        return cnx


def prepare_to_send(pack: KDEConnectPacket) -> bytes:
    """
    Prepare packet to send.
    """
    pack2 = pack.copy()
    pack2['id'] = int(time.time())
    return (json.dumps(pack2) + '\n').encode('utf-8')


def generate_my_identity() -> KDEConnectPacket:
    """
    Generates ID packet for this device.
    """
    return {
        'id': 0,
        'type': 'kdeconnect.identity',
        'body': {
            'deviceId': PYCONNECT_DEVICE_ID,
            'deviceName': PYCONNECT_DEVICE_NAME,
            'deviceType': 'laptop',
            'protocolVersion': KDE_CONNECT_PROTOCOL_VERSION,
            'incomingCapabilities': PYCONNECT_INCOMING_CAPABILITIES,
            'outgoingCapabilities': PYCONNECT_OUTGOING_CAPABILITIES,
            'tcpPort': KDE_CONNECT_DEFAULT_PORT
        }
    }


could_send_my_id_packs = True


async def send_my_id_packets():
    """
    Sends this device ID packets (``kdeconnect.identity``) using
    ``UDP broadcast``.
    """
    id_pack = generate_my_identity()

    async with await anyio.create_udp_socket(
            family=socket.AF_INET, reuse_port=True,
            local_port=KDE_CONNECT_DEFAULT_PORT) as udp_sock:

        raw_socket = udp_sock.extra(anyio.abc.SocketAttribute.raw_socket)
        raw_socket.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)

        while True:
            if not could_send_my_id_packs:
                await anyio.sleep(2)
                continue

            await udp_sock.sendto(
                prepare_to_send(id_pack), '<broadcast>',
                KDE_CONNECT_DEFAULT_PORT)

            await anyio.sleep(2)


async def wait_for_incoming_id(main_group: anyio.abc.TaskGroup):
    """
    Listens on ``UDP Broadcast`` for incoming device ID packets.
    """
    async with await anyio.create_udp_socket(
            family=socket.AF_INET, local_host='255.255.255.255',
            local_port=KDE_CONNECT_DEFAULT_PORT, reuse_port=True) as udp_sock:
        async for data, (host, port) in udp_sock:
            try:
                pack = json.loads(data.decode('utf-8'))
            except json.JSONDecodeError:
                log.exception(f'Malformed packet from {host}:{port}\n{data}')
                continue

            if pack['type'] != 'kdeconnect.identity':
                log.warning(
                    f'kdeconnect.identity packet expected, got {pack["type"]}')
                continue

            pack_body = pack['body']
            if 'deviceId' not in pack_body or pack_body['deviceId'] == '':
                log.warning(f'Identity packet without body.deviceId\n{pack=}')
                continue

            dev_id = pack_body['deviceId']
            if dev_id == PYCONNECT_DEVICE_ID or DeviceConfig.is_connected_device(dev_id):  # noqa
                continue

            dev_name = pack_body['deviceName']
            known = 'known' if DeviceConfig.is_known_device(dev_id) else \
                'unknown'
            paired = 'paired' if DeviceConfig.is_paired_device(dev_id) else \
                'unpaired'
            log.debug((
                f'Id packet received from {known} and {paired} device:'
                f' {dev_name} / {dev_id} (IP: {host})'))

            # start connection task
            if DeviceConfig.is_paired_device(dev_id):
                main_group.start_soon(outgoing_connection_task, pack, host,
                                      main_group)


async def outgoing_connection_task(
        id_packet: KDEConnectPacket, remote_ip: str,
        main_group: anyio.abc.TaskGroup):
    """
    Outgoing conection to the known device.
    """
    global could_send_my_id_packs

    dev_config = DeviceConfig.load_from_id_pack(id_packet)
    remote_port = id_packet['body']['tcpPort']

    # connect
    async with await anyio.connect_tcp(remote_ip, remote_port) as sock:
        log.info((f'Connected to {remote_ip}:{remote_port} '
                  f'({dev_config.device_name}).'))
        dev_config.connected = True
        could_send_my_id_packs = False

        # send identity
        await sock.send(prepare_to_send(generate_my_identity()))
        log.debug('Identity packet sent to {remote_ip}:{remote_port}.')

        # wrap TLS
        ssock = await TLSStream.wrap(
            sock, server_side=True, ssl_context=dev_config.ssl_context(
                ssl.Purpose.CLIENT_AUTH), standard_compatible=False)
        dev_config.connection_ssock = ssock
        log.debug(f'Wrapped TLS connection with {remote_ip}:{remote_port}.')

        # handle pair if needed
        bssock = BufferedByteReceiveStream(ssock)
        if not dev_config.paired:
            if not await handle_pairing(dev_config, bssock):
                await on_device_disconnected(dev_config, ssock)
                could_send_my_id_packs = True
                return

        # receiving packets
        while True:
            pack = await receive_packet(bssock)
            if not pack:
                await anyio.sleep(1)
                continue

            if not await handle_packet(dev_config, pack, bssock, main_group):
                await on_device_disconnected(dev_config, ssock)
                break

        could_send_my_id_packs = True


async def incoming_connection_task(main_group: anyio.abc.TaskGroup):
    """
    Handles incoming connections.
    """
    async def handle_connection(client):
        global could_send_my_id_packs
        async with client:
            could_send_my_id_packs = False  # block sending next ids

            remote_ip, remote_port = client.extra(
                anyio.abc.SocketAttribute.remote_address)
            log.info(
                f'New incoming connection {remote_ip}:{remote_port}')

            # receive id packet
            bsock = BufferedByteReceiveStream(client)
            pack = await receive_packet(bsock)

            if pack['type'] != 'kdeconnect.identity':
                log.error(
                    f'kdeconnect.identity packet expected, got {pack["type"]}')
                could_send_my_id_packs = True
                return

            log.info((
                'Incoming connection id packet received:'
                f'{pack["body"]["deviceName"]} / {pack["body"]["deviceId"]}'))

            # disconnect already connected device
            if DeviceConfig.is_connected_device(pack['body']['deviceId']):
                await client.aclose()
                could_send_my_id_packs = True
                return

            # load device config
            dev_config = DeviceConfig.load_from_id_pack(pack)
            dev_config.last_ip = remote_ip
            dev_config.connected = True
            dev_config.save()

            ssock = await TLSStream.wrap(client, server_side=False,
                                         ssl_context=dev_config.ssl_context(
                                             ssl.Purpose.SERVER_AUTH),
                                         hostname=remote_ip,
                                         standard_compatible=False)
            dev_config.connection_ssock = ssock
            log.debug('TLS connection wrapped.')

            # try to obtain a device cert
            try:
                dev_config.certificate_PEM = ssl.DER_cert_to_PEM_cert(
                    ssock.extra(
                        anyio.streams.tls.TLSAttribute.peer_certificate_binary)
                )
                dev_config.ssl_context(renew=True)
                dev_config.save()
            except Exception:
                log.exception(
                    f'Error while retrieving certificate of {remote_ip}.')

            # handle pair if needed
            bssock = BufferedByteReceiveStream(ssock)
            if not dev_config.paired:
                if not await handle_pairing(dev_config, bssock):
                    await on_device_disconnected(dev_config, ssock)
                    could_send_my_id_packs = True
                    return

            # receive packets
            while True:
                pack = await receive_packet(bssock)
                if not pack:
                    await anyio.sleep(1)
                    continue

                if not await handle_packet(dev_config, pack, bssock,
                                           main_group):
                    await on_device_disconnected(dev_config, ssock)
                    could_send_my_id_packs = True
                    break

            could_send_my_id_packs = True

    listener = await anyio.create_tcp_listener(
        local_port=KDE_CONNECT_DEFAULT_PORT, local_host='0.0.0.0')
    await listener.serve(handle_connection)


async def receive_packet(bssock: BufferedByteReceiveStream) -> typing.Union[
        KDEConnectPacket, bool]:
    """
    Receiving and decoding a packet - the common part
    """
    try:
        pack_data = await bssock.receive_until(b'\n', 1024 * 1024 * 10)
    except (anyio.EndOfStream, anyio.IncompleteRead):
        return False
    except Exception:
        log.exception('Error while receiving packet.')
        return False

    try:
        pack = json.loads(pack_data)
    except json.JSONDecodeError:
        log.exception(f'Error while decoding packet\n{pack_data}')
        return False

    log.debug(f'Received packet:\n{pack!r}')

    return pack


async def handle_packet(dev_config: DeviceConfig, pack: KDEConnectPacket,
                        bssock: BufferedByteReceiveStream,
                        main_group: anyio.abc.TaskGroup) -> bool:
    """
    Common part of handling the incoming packet
    """
    # pair / unpair packet
    if pack['type'] == 'kdeconnect.pair':
        if pack['body']['pair'] is False:
            dev_config.paired = False
            dev_config.save()
            await on_device_disconnected(dev_config,
                                         dev_config.connection_ssock)
            log.info('Unpairing {dev_config.device_name} done.')
            return False
        else:
            # pair packet is possible, when we weren't properly unpaired before
            return await handle_pairing(dev_config, bssock,
                                        pair_pack_received=True)

    # incoming file
    if pack['type'] == 'kdeconnect.share.request':
        main_group.start_soon(download_file_task, pack, dev_config)

    return True


async def handle_pairing(
        dev_config: DeviceConfig, bssock: BufferedByteReceiveStream,
        pair_pack_received: bool = False) -> bool:
    """
    Common part of device pairing.
    """
    pair_pack = {
        'id': 0,
        'type': 'kdeconnect.pair',
        'body': {
            'pair': True
        }
    }

    if auto_send_pair_request:
        # TODO
        pass
    else:
        # answer only the pair request
        if not pair_pack_received:
            while True:
                pack = await receive_packet(bssock)
                if pack:
                    break
                else:
                    await anyio.sleep(1)
            if not pack or pack['type'] != 'kdeconnect.pair':
                log.error((
                    'Unexpected packet type (expected kdeconnect.pair, got'
                    f' {pack["type"]}).\n{pack!r}'))
                return False

            if not pack['body']['pair']:
                log.info('Unpairing {dev_config.device_name} done.')
                dev_config.paired = False
                dev_config.save()
                return False

        if not pair_request_auto_accept:
            pass
        # TODO: pytanie do użytkownika
        # + dev_config.paired = True

        await dev_config.connection_ssock.send(prepare_to_send(pair_pack))
        dev_config.paired = True
        dev_config.save()
        log.info('Pairing {dev_config.device_name} done.')
        return True


async def on_device_disconnected(dev_config: DeviceConfig,
                                 anysock: typing.Union[
                                     SocketStream, TLSStream] = None):
    """
    Device disconnection - the common code.
    """
    global could_send_my_id_packs

    if anysock:
        try:
            await anysock.aclose()
        except Exception:
            log.exception('Exception while disconnecting - ignored.')
            pass

    dev_config.connected = False

    could_send_my_id_packs = True


async def download_file_task(pack: KDEConnectPacket, dev_config: DeviceConfig):
    """
    File download initiated by packet kdeconnect.share.request.
    """
    log.info(f'Downloading file from {dev_config.last_ip}.')

    if 'filename' not in pack['body']:
        log.error('No filename property in pack.')
        return

    if 'payloadSize' not in pack or 'payloadTransferInfo' not in pack \
            or 'port' not in pack['payloadTransferInfo']:
        log.error('No payloadSize or payloadTransferInfo property in pack.')
        return

    # dest filename
    filename = os.path.join(os.path.expanduser('~'), pack['body']['filename'])
    i = 1
    while os.path.exists(filename):
        filename = os.path.splitext(pack['body']['filename'])
        filename = os.path.join(os.path.expanduser('~'),
                                f'{filename[0]}-{i}{filename[1]}')
        i += 1
    log.debug(f'Download destination file: {filename}.')

    # download
    async with await anyio.connect_tcp(
            dev_config.last_ip, pack['payloadTransferInfo']['port'],
            ssl_context=dev_config.ssl_context(
                ssl.Purpose.SERVER_AUTH)) as ssock:
        with open(filename, 'wb') as f:
            received = 0
            while received < pack['payloadSize']:
                data = await ssock.receive()
                f.write(data)
                received += len(data)
                print((
                    f'\r* Download {pack["body"]["filename"]} - received bytes'
                    f'  +{len(data)} ({received} of {pack["payloadSize"]})'),
                    end='')
        print('')

    log.info(f'Download connection closed with {dev_config.last_ip}.')


async def upload_file_task(
        dev_config: DeviceConfig, file_path: str, number_of_files: int = 1,
        total_files_size: int = -1):
    """
    Upload file task.
    """
    if not os.path.exists(file_path):
        log.error(f'File not exists ({file_path}).')
        return

    # start upload server
    file_size = os.path.getsize(file_path)
    file_name = os.path.basename(file_path)
    server = None
    close_server_event = anyio.Event()

    async def handle_connection(sock_client):
        async with sock_client:
            remote_ip, remote_port = sock_client.extra(
                anyio.abc.SocketAttribute.remote_address)
            log.info(
                f'Upload file {file_name} - device connected ({remote_ip}).')
            with open(file_path, 'rb') as f:
                sent = 0
                while sent < file_size:
                    data = f.read(63 * 1024)
                    await sock_client.send(data)

                    sent += len(data)
                    print((
                        f'\r* Upload file {file_name} - sent {sent} of '
                        f'{file_size} (+{len(data)})'), end='')
        print('')

        await server.aclose()
        close_server_event.set()

    transfer_port = 0
    for port in range(KDE_CONNECT_TRANSFER_MIN, KDE_CONNECT_TRANSFER_MAX + 1):
        try:
            server = TLSListener(await anyio.create_tcp_listener(
                local_port=port, local_host='0.0.0.0'),
                ssl_context=dev_config.ssl_context(ssl.Purpose.CLIENT_AUTH),
                standard_compatible=False)
            log.debug(f'Selected port {port} for upload file.')
            transfer_port = port

            break
        except OSError as e:
            if e.errno == 98:  # port already in use - ignore
                continue
            raise e

    # send ready packet
    total_size = total_files_size if total_files_size != -1 else file_size

    pack = {
        'id': 0,
        'type': 'kdeconnect.share.request',
        'body': {
            'filename': file_name,
            'open': False,
            'lastModified': int(os.path.getmtime(file_path)),
            'numberOfFiles': number_of_files,
            'totalPayloadSize': total_size
        },
        'payloadSize': file_size,
        'payloadTransferInfo': {
            'port': transfer_port
        }
    }

    serve_forever = server.serve(handle_connection)
    await anyio.sleep(0.01)

    await dev_config.connection_ssock.send(prepare_to_send(pack))
    log.debug(f'Upload invitation packet sent: {pack!r}.')

    try:
        await serve_forever
    except anyio.ClosedResourceError:
        close_server_event.set()

    await close_server_event.wait()
    log.debug('Transfer server closed.')


async def signal_handler(scope: anyio.CancelScope):
    with anyio.open_signal_receiver(signal.SIGINT, signal.SIGTERM) as signals:
        async for signum in signals:
            if signum == signal.SIGINT:
                print('\r')
                log.info('Ctrl+C pressed!')
            else:
                log.info('Terminated!')

            scope.cancel()
            return


async def generate_cert():
    """
    Generates ``SSL`` certificate files.
    """
    global PYCONNECT_DEVICE_ID
    PYCONNECT_DEVICE_ID = uuid.uuid4().urn.replace('urn:uuid:', '')

    log.debug((f'Generating certs for {PYCONNECT_DEVICE_NAME} '
               f'{PYCONNECT_DEVICE_ID} {PYCONNECT_CERTFILE} '
               f'{PYCONNECT_KEYFILE}'))

    openssl = await anyio.run_process(
        ['openssl', 'req', '-new', '-x509', '-sha256', '-out',
         PYCONNECT_CERTFILE, '-newkey', 'rsa:4096', '-nodes', '-keyout',
         PYCONNECT_KEYFILE, '-days', '3650', '-subj',
         '/O=jplochocki.github.io/OU=PYConnect/CN=' + PYCONNECT_DEVICE_ID],
        stderr=subprocess.STDOUT, check=False)

    if openssl.returncode != 0:
        raise RuntimeError(
            (f'OpenSSL returned an error code ({openssl.returncode})\n'
             f'{openssl.stdout.decode()}'))

    log.info((f'Cert generated for {PYCONNECT_DEVICE_NAME} '
              f'/ {PYCONNECT_DEVICE_ID}'))


async def read_cert_common_name(cert_file: str) -> str:
    """
    Reads ``CN`` field from ``SSL`` certificate.
    """
    openssl = await anyio.run_process([
        'openssl', 'x509', '-in', cert_file, '-noout', '-subject', '-inform',
        'pem'])

    # subject=O = jplochocki.github.io, OU = PYConnect, CN = e0f7faa7...
    a = re.search(r'CN\s*=\s*([^,\n]*)', openssl.stdout.decode(), re.I)
    if not a:
        raise RuntimeError(
            f'Invalid cert CN string ({openssl.stdout.decode()})')

    log.info(f'Certificate\'s CN name readed: {cert_file} = {a.group(1)}')

    return a.group(1)


@ravel.interface(ravel.INTERFACE.SERVER, name=DBUS_IFACE_SERVER)
class DBUSCallbackServer:
    def __init__(self):
        pass

    @ravel.method(
        name='status',
        in_signature='',
        out_signature='s',
      )
    async def handle_status(self):
        status = {
            'running': True,
            'connected': False,
            'devices': []
        }

        for dev_config in DeviceConfig._cache.values():
            status['connected'] = True

            status['devices'].append({
                'device_id': dev_config.device_id,
                'device_name': dev_config.device_name,
                'device_paired': dev_config.paired,
                'device_ip': dev_config.last_ip,
                'connected_since':
                    datetime.datetime.now() - dev_config.last_connection_date,
            })

        log.debug(f'Service status result = {status!r}')
        return [json.dumps(status, cls=EnhancedJSONEncoder)]

    @ravel.method(
        name='upload_file',
        in_signature='sas',
        arg_keys=('device_id', 'file_paths',),
        out_signature='b',
      )
    async def handle_upload_file(self, device_id, file_paths):
        log.info(f'handle_upload_file(): DBUS call {file_paths=}')

        dev_config = DeviceConfig.load(device_id)
        total_files_size = sum([os.path.getsize(f) for f in file_paths])

        for file_path in file_paths:
            await upload_file_task(dev_config, file_path, len(file_paths),
                                   total_files_size)

        return [True]


async def server_main_task():
    global config, PYCONNECT_CERTFILE, PYCONNECT_KEYFILE, PYCONNECT_DEVICE_ID,\
        PYCONNECT_DEVICE_NAME

    # config
    load_config()

    # init certs
    PYCONNECT_DEVICE_NAME = socket.gethostname()

    a = os.path.abspath(os.path.dirname(__file__))
    b = re.sub(r'[^a-z0-9\-]', '', PYCONNECT_DEVICE_NAME.lower())
    PYCONNECT_CERTFILE = os.path.join(a, f'certificate-{b}.pem')
    PYCONNECT_KEYFILE = os.path.join(a, f'private-{b}.pem')

    if not os.path.exists(PYCONNECT_CERTFILE) or not os.path.exists(PYCONNECT_KEYFILE):  # noqa
        await generate_cert()

    if not PYCONNECT_DEVICE_ID:
        PYCONNECT_DEVICE_ID = await read_cert_common_name(PYCONNECT_CERTFILE)

    log.info((f'PYConnect server starts as {PYCONNECT_DEVICE_NAME} '
              f'({PYCONNECT_DEVICE_ID})'))

    # dbus
    dbus = ravel.session_bus()
    dbus.attach_asyncio()
    dbus.request_name(bus_name=DBUS_IFACE_SERVER,
                      flags=DBUS.NAME_FLAG_DO_NOT_QUEUE)
    dbus_interface = DBUSCallbackServer()
    dbus.register(path=DBUS_SERVER_PATH, fallback=True,
                  interface=dbus_interface)

    # main task
    async with anyio.create_task_group() as main_group:
        main_group.start_soon(signal_handler, main_group.cancel_scope)
        main_group.start_soon(wait_for_incoming_id, main_group)
        main_group.start_soon(incoming_connection_task, main_group)
        main_group.start_soon(send_my_id_packets)

    log.info('Server ends.')


async def fetch_server_status(dbus):
    request = dbussy.Message.new_method_call(
        destination=DBUS_IFACE_SERVER, path=DBUS_SERVER_PATH,
        iface=DBUS_IFACE_SERVER, method='status')
    reply = await dbus.connection.send_await_reply(request)

    # print(reply, dbussy.Message.type_to_string(reply.type),)
    if reply.type == DBUS.MESSAGE_TYPE_ERROR:
        if reply.error_name == 'org.freedesktop.DBus.Error.ServiceUnknown':
            log.debug('You must run "pyconnect.py server" first.')
        else:
            log.error((f'Error occurred while method call.\n{reply.error_name}'
                       f' = {reply.all_objects!r}'))
        return {
            'running': False,
            'connected': False,
            'devices': []
        }

    return json.loads(reply.all_objects[0], cls=EnhancedJSONDecoder)


def human_readable_timedelta(td: datetime.timedelta):
    hours, remainder = divmod(int(td.total_seconds()), 3600)
    minutes, seconds = divmod(remainder, 60)

    if hours == 0:
        hours = ''
    elif hours == 1:
        hours = f'{hours} hour '
    else:
        hours = f'{hours} hours '

    if minutes == 0:
        minutes = ''
    elif minutes == 1:
        minutes = f'{minutes} minute '
    else:
        minutes = f'{minutes} minutes '

    if seconds == 0:
        seconds = ''
    elif seconds == 1:
        seconds = f'{seconds} second'
    else:
        seconds = f'{seconds} seconds'

    return f'{hours}{minutes}{seconds}'


async def handle_status_command(server_status: typing.Dict,
                                scope: anyio.CancelScope,
                                dbus: ravel.Connection):
    """
    Status command.
    """
    if not server_status['running']:
        print(click.style(
            '❌ Server unavailable. Run it typing pyconnect.py server.',
            fg='red'))
        scope.cancel()
        return

    print(click.style('✔️  Server running', fg='green'))
    for device in server_status['devices']:
        paired = '✔️' if ['device_paired'] else '❌'
        paired_end = '' if ['device_paired'] else click.style(
            ' - device unpaired', fg='red')
        dev_name = click.style(device['device_name'], bold=True, fg='blue')
        c_time = human_readable_timedelta(device['connected_since'])

        print((f'{paired}  Connected with {dev_name} ({device["device_id"]} '
               f'/ {device["device_ip"]}) since {c_time}{paired_end}'))

    if not len(server_status['devices']):
        print(click.style('❌ No device connected.', fg='red'))

    scope.cancel()


async def handle_upload_command(
        server_status: typing.Dict, filenames: typing.List,
        scope: anyio.CancelScope, dbus: ravel.Connection):
    """
    Upload file command.
    """
    request = dbussy.Message.new_method_call(
        destination=DBUS_IFACE_SERVER, path=DBUS_SERVER_PATH,
        iface=DBUS_IFACE_SERVER, method='upload_file')
    request.append_objects('s', server_status['devices'][0]['device_id'])
    request.append_objects('as', filenames)
    reply = await dbus.connection.send_await_reply(request)

    if reply.type == DBUS.MESSAGE_TYPE_ERROR:
        log.error((f'Error occurred while method call.\n{reply.error_name}'
                   f' = {reply.all_objects!r}'))
    scope.cancel()


async def client_main_task(command_name: str, command_args: typing.List):
    dbus = ravel.session_bus()
    dbus.attach_asyncio()

    server_status = await fetch_server_status(dbus)
    if not server_status['connected'] and command_name in ['upload']:
        print(click.style(
            '❌ Command unavailable without any device connected.', fg='red'))
        return

    async with anyio.create_task_group() as main_group:
        main_group.start_soon(signal_handler, main_group.cancel_scope)

        main_group.start_soon({
            'status': handle_status_command,
            'upload': handle_upload_command,
        }.get(command_name), server_status, *command_args,
            main_group.cancel_scope, dbus)


@click.group()
def main():
    pass


@main.command()
def status():
    anyio.run(client_main_task, 'status', [])


@main.command()
@click.argument('filenames', type=click.Path(exists=True), nargs=-1,
                required=True)
def upload(filenames):
    anyio.run(client_main_task, 'upload', [filenames])


@main.command()
def server():
    anyio.run(server_main_task)


if __name__ == '__main__':
    main()
