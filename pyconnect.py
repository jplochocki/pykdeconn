#! /usr/bin/env python3

import logging
import logging.config
import socket
import json
import time
import ssl
import os.path
import signal
import asyncio
import typing
import re
import sys
from dataclasses import dataclass, field, asdict  # noqa
import datetime

import anyio
from anyio.streams.tls import TLSStream, TLSListener
from anyio.streams.buffered import BufferedByteReceiveStream
from gi.repository import Gio


class CustomFormatter(logging.Formatter):
    grey = '\x1b[38;20m'
    yellow = '\x1b[33;20m'
    red = '\x1b[31;20m'
    bold_red = '\x1b[31;1m'
    green = '\x1b[32m'
    reset = '\x1b[0m'
    format = '[ %(levelname)s %(asctime)s %(funcName)20s()::%(lineno)d ]\n\t%(message)s'  # noqa

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


KDE_CONNECT_DEFAULT_PORT = 1716
KDE_CONNECT_TRANSFER_MIN = 1739
KDE_CONNECT_TRANSFER_MAX = 1764
PROTOCOL_VERSION = 7
GSCONNECT_DEVICE_ID = ''
GSCONNECT_DEVICE_NAME = ''
INCOMING_CAPABILITIES = [
    'kdeconnect.share.request',
]
OUTGOING_CAPABILITIES = [
    'kdeconnect.share.request',
]

GSCONNECT_CERTFILE = '~/.config/gsconnect/certificate.pem'
GSCONNECT_KEYFILE = '~/.config/gsconnect/private.pem'
GSCONNECT_KNOWN_DEVICES = []
CONNECTED_DEVICES = []


CONFIG_FILE = 'gsconnect.config.json'


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


@dataclass
class DeviceConfig:
    device_id: str
    device_name: str
    certificate_PEM: str = field(default='')
    paired: bool = field(default=False)
    last_ip: str = field(default='')
    last_connection_date: datetime.datetime = field(
        default_factory=lambda: datetime.datetime.now())

    def __post_init__(self):
        self._ssl_cnx_cache = {}
        self.connected = False

    @staticmethod
    def is_known_device(device_id: str) -> bool:
        """
        Checks in the configuration whether the device ID is known.
        """
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, 'r') as f:
                cfg = json.load(f, cls=EnhancedJSONDecoder)
                if 'devices' in cfg and device_id in cfg['devices']:
                    return True

        return False

    @classmethod
    def load_from_id_pack(cls, pack: KDEConnectPacket):
        if pack['type'] != 'kdeconnect.identity' or 'deviceId' not in pack['body']:  # noqa
            raise RuntimeError(
                ('Identity packet without body.deviceId or unknown type\n'
                 f'{pack=}'))

        if DeviceConfig.is_known_device(pack['body']['deviceId']):
            dev = DeviceConfig.load(pack['body']['deviceId'])
            dev.device_name = pack['body']['deviceName']
            return dev

        return DeviceConfig(device_id=pack['body']['deviceId'],
                            device_name=pack['body']['deviceName'])

    @classmethod
    def load(cls, device_id: str):
        """Loads device config."""
        pass


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
            'deviceId': GSCONNECT_DEVICE_ID,
            'deviceName': GSCONNECT_DEVICE_NAME,
            'deviceType': 'laptop',
            'protocolVersion': PROTOCOL_VERSION,
            'incomingCapabilities': INCOMING_CAPABILITIES,
            'outgoingCapabilities': OUTGOING_CAPABILITIES,
            'tcpPort': KDE_CONNECT_DEFAULT_PORT
        }
    }


could_send_my_id_packs = asyncio.Event()


async def send_my_id_packets():
    """
    Sends this device ID packets (kdeconnect.identity) using UDP broadcast.
    """
    id_pack = generate_my_identity()

    async with await anyio.create_udp_socket(
            family=socket.AF_INET, reuse_port=True,
            local_port=KDE_CONNECT_DEFAULT_PORT) as udp_sock:

        a = udp_sock.extra(anyio.abc.SocketAttribute.raw_socket)
        a.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)

        while True:
            await could_send_my_id_packs.wait()

            await udp_sock.sendto(
                prepare_to_send(id_pack), '<broadcast>',
                KDE_CONNECT_DEFAULT_PORT)

            await anyio.sleep(2)


async def wait_for_incoming_id(main_group):
    """
    Listens on UDP Broadcast for incoming device ID packets.
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
            dev_name = pack_body['deviceName']
            known = 'known' if dev_id in GSCONNECT_KNOWN_DEVICES else 'unknown'
            log.debug((
                f'Id packet received from {known} device: {dev_name} / '
                f'{dev_id} (IP: {host})'))

            if dev_id in GSCONNECT_KNOWN_DEVICES or dev_id == GSCONNECT_DEVICE_ID or dev_id in CONNECTED_DEVICES:  # noqa
                continue

            # start connection task
            main_group.start_soon(outgoing_connection_task, pack, host)


async def outgoing_connection_task(
        id_packet: KDEConnectPacket, remote_ip: str):
    """
    Outgoing conection to the known device.
    """
    remote_deviceName = id_packet['body']['deviceName']
    remote_deviceId = id_packet['body']['deviceId']
    CONNECTED_DEVICES.append(remote_deviceId)

    log.info(('device_connection_task() started: '
              f'{remote_ip} / {remote_deviceName}'))

    # device config + certs
    ssl_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ssl_context.load_cert_chain(GSCONNECT_CERTFILE, GSCONNECT_KEYFILE)
    ssl_context.verify_flags = ssl.CERT_REQUIRED

    gsconnect_config = Gio.Settings.new_with_path(
        'org.gnome.Shell.Extensions.GSConnect.Device',
        f'/org/gnome/shell/extensions/gsconnect/device/{remote_deviceId}/')

    ssl_context.load_verify_locations(
        cadata=ssl.PEM_cert_to_DER_cert(
            gsconnect_config.get_string('certificate-pem')))

    # connect
    async with await anyio.connect_tcp(remote_ip, KDE_CONNECT_DEFAULT_PORT) as sock:  # noqa
        log.debug(
            f'device_connection_task(): connected to {remote_ip} / {sock!r}')

        # send identity
        await sock.send(prepare_to_send(generate_my_identity()))
        log.debug('device_connection_task(): identity packet sent')

        # wrap TSL
        ssock = await TLSStream.wrap(sock, server_side=True,
                                     ssl_context=ssl_context)
        log.debug(('device_connection_task(): wrapped TSL connection '
                   f'({ssock!r})'))

        # receiving packets
        bssock = BufferedByteReceiveStream(ssock)
        async with anyio.create_task_group() as slave_group:
            slave_group.start_soon(test_upload_task, ssock, ssl_context)

            while True:
                pack_data = await bssock.receive_until(b'\n', 1024 * 1024)

                try:
                    pack = json.loads(pack_data)
                except json.JSONDecodeError:
                    log.exception(
                        ('device_connection_task(): Error while decoding '
                         f'packet / {pack_data}'))
                    continue
                log.debug(f'device_connection_task(): Packet {pack_data}')

                if pack['type'] == 'kdeconnect.share.request':
                    slave_group.start_soon(download_file_task, pack, remote_ip,
                                           ssl_context)


async def download_file_task(pack, remote_ip, ssl_context):
    log.info(f'download_file_task() from {remote_ip}')

    if 'filename' not in pack['body']:
        log.error('download_file_task(): No filename property in pack.')
        return

    if 'payloadSize' not in pack or 'payloadTransferInfo' not in pack \
            or 'port' not in pack['payloadTransferInfo']:
        log.error(('download_file_task(): No payloadSize or '
                   'payloadTransferInfo property in pack.'))
        return

    # dest filename
    filename = os.path.join(os.path.expanduser('~'), pack['body']['filename'])
    i = 1
    while os.path.exists(filename):
        filename = os.path.splitext(pack['body']['filename'])
        filename = os.path.join(os.path.expanduser('~'),
                                f'{filename[0]}-{i}{filename[1]}')
        i += 1
    log.debug(f'download_file_task(): destination file: {filename}')

    # download
    async with await anyio.connect_tcp(
            remote_ip, pack['payloadTransferInfo']['port'],
            ssl_context=ssl_context) as sock:

        with open(filename, 'wb') as f:
            received = 0
            while received < pack['payloadSize']:
                data = await sock.receive()
                f.write(data)
                received += len(data)
                print((f'\r* download_file_task(): {pack["body"]["filename"]}'
                       f' - received bytes  +{len(data)} ({received} of'
                       f' {pack["payloadSize"]})'),
                      end='')
        print('')

    log.info('download_file_task(): download connection closed.')


async def test_upload_task(ssock, ssl_context):
    '''
    Test upload task - upload script itself
    '''
    await anyio.sleep(2)
    await upload_file(os.path.abspath(__file__), ssock, ssl_context)


async def upload_file(file_path, ssock, ssl_context):
    log.info(f'upload_file(): {file_path=}, {ssock=}, {ssl_context=}')

    if not os.path.exists(file_path):
        log.error(f'upload_file(): File not exists ({file_path})')
        return

    # start upload server
    file_size = os.path.getsize(file_path)
    server = None
    close_server_event = anyio.Event()

    async def handle_connection(sock_client):
        async with sock_client:
            with open(file_path, 'rb') as f:
                sent = 0
                while sent < file_size:
                    data = f.read(63 * 1024)
                    await sock_client.send(data)

                    sent += len(data)
                    print((f'\rupload_file() sent {sent} of {file_size} '
                           f'(+{len(data)})'), end='')
        print('')

        await server.aclose()
        close_server_event.set()

    transfer_port = 0
    for port in range(KDE_CONNECT_TRANSFER_MIN, KDE_CONNECT_TRANSFER_MAX + 1):
        try:
            server = TLSListener(await anyio.create_tcp_listener(
                local_port=port, local_host='0.0.0.0'),
                ssl_context=ssl_context, standard_compatible=False)
            log.info(f'upload_file(): - Selected port {port}')
            transfer_port = port

            break
        except OSError as e:
            if e.errno == 98:  # port already in use
                continue
            raise e

    # # send ready packet
    pack = {
        'type': 'kdeconnect.share.request',
        'body': {
            'filename': os.path.basename(file_path),
            'open': False,
            'lastModified': int(os.path.getmtime(file_path)),
            'numberOfFiles': 1,
            'totalPayloadSize': file_size
        },
        'payloadSize': file_size,
        'payloadTransferInfo': {
            'port': transfer_port
        }
    }

    serve_forever = server.serve(handle_connection)
    await anyio.sleep(0.01)

    await ssock.send(prepare_to_send(pack))
    log.debug(f'upload_file(): invitation packet sent: {pack!r}')

    try:
        await serve_forever
    except anyio.ClosedResourceError:
        close_server_event.set()

    await close_server_event.wait()
    log.debug('upload_file(): transfer server closed')


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


async def main():
    # config
    global GSCONNECT_CERTFILE, GSCONNECT_KEYFILE, GSCONNECT_DEVICE_ID, \
        GSCONNECT_DEVICE_NAME, GSCONNECT_KNOWN_DEVICES

    gsconnect_config = Gio.Settings.new_with_path(
        'org.gnome.Shell.Extensions.GSConnect.Device',
        '/org/gnome/shell/extensions/gsconnect/')
    GSCONNECT_DEVICE_ID = gsconnect_config.get_string('id')
    GSCONNECT_DEVICE_NAME = gsconnect_config.get_string('name')

    gsconnect_config = Gio.Settings.new_with_path(
        'org.gnome.Shell.Extensions.GSConnect',
        '/org/gnome/shell/extensions/gsconnect/')
    GSCONNECT_KNOWN_DEVICES = list(gsconnect_config.get_value('devices'))

    log.info((f'Application start as {GSCONNECT_DEVICE_NAME} '
              f'({GSCONNECT_DEVICE_ID})'))

    GSCONNECT_CERTFILE = os.path.expanduser(GSCONNECT_CERTFILE)
    GSCONNECT_KEYFILE = os.path.expanduser(GSCONNECT_KEYFILE)
    assert os.path.exists(GSCONNECT_CERTFILE) and os.path.exists(GSCONNECT_KEYFILE)  # noqa

    # main task
    could_send_my_id_packs.set()
    async with anyio.create_task_group() as main_group:
        main_group.start_soon(signal_handler, main_group.cancel_scope)
        main_group.start_soon(wait_for_incoming_id, main_group)
        main_group.start_soon(send_my_id_packets)

    log.info('Application end')


if __name__ == '__main__':
    anyio.run(main)
