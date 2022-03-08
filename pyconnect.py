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
import uuid
import subprocess

import anyio
from anyio.streams.tls import TLSStream, TLSListener
from anyio.streams.buffered import BufferedByteReceiveStream


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

CONNECTED_DEVICES = []

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           'pyconnect.config.json')
config = None


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

    def __post_init__(self):
        self._ssl_cnx_cache = {}
        self.connected = False

    @staticmethod
    def is_known_device(device_id: str) -> bool:
        """
        Checks in the configuration whether the device ID is known.
        """
        return 'devices' in config and device_id in config['devices']

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

        if DeviceConfig.is_known_device(pack_body['deviceId']):
            dev = DeviceConfig.load(pack_body['deviceId'])
            dev.device_name = pack_body['deviceName']
        else:
            dev = DeviceConfig(device_id=pack_body['deviceId'],
                               device_name=pack_body['deviceName'])

        dev.save()
        return dev

    @classmethod
    def load(cls, device_id: str):
        """Loads device config."""
        if 'devices' in config and device_id in config['devices']:
            return cls(**config['devices'][device_id])
        raise RuntimeError(f'Device id "{device_id}" not  configured.')

    def save(self):
        """Saves device config."""
        if 'devices' not in config:
            config['devices'] = {}

        config['devices'][self.device_id] = asdict(self)
        save_config()

    def ssl_context(self, purpose: ssl.Purpose = ssl.Purpose.CLIENT_AUTH,
                    renew: bool = False) -> ssl.SSLContext:
        """Loads ``SSLContext`` for the specified ``purpose``."""
        if not renew and purpose.shortname in self._ssl_cnx_cache:
            return self._ssl_cnx_cache[purpose.shortname]

        cnx = ssl.create_default_context(purpose)
        cnx.load_cert_chain(
            certfile=PYCONNECT_CERTFILE, keyfile=PYCONNECT_KEYFILE)

        cnx.check_hostname = False
        cnx.verify_mode = ssl.CERT_NONE

        if self.certificate_PEM:
            cnx.load_verify_locations(cadata=self.certificate_PEM)
            cnx.check_hostname = True
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


could_send_my_id_packs = asyncio.Event()


async def send_my_id_packets():
    """
    Sends this device ID packets (``kdeconnect.identity``) using
    ``UDP broadcast``.
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


async def wait_for_incoming_id(main_group:  anyio.abc.TaskGroup):
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
            if dev_id == PYCONNECT_DEVICE_ID or dev_id in CONNECTED_DEVICES:
                continue

            dev_name = pack_body['deviceName']
            known = 'known' if DeviceConfig.is_known_device(dev_id) else 'unknown'  # noqa
            log.debug((
                f'Id packet received from {known} device: {dev_name} / '
                f'{dev_id} (IP: {host})'))

            # start connection task
            main_group.start_soon(outgoing_connection_task, pack, host)


async def outgoing_connection_task(
        id_packet: KDEConnectPacket, remote_ip: str,
        main_group: anyio.abc.TaskGroup):
    """
    Outgoing conection to the known device.
    """
    dev_config = DeviceConfig.load_from_id_pack(id_packet)

    remote_port = id_packet['body']['tcpPort']

    # connect
    async with await anyio.connect_tcp(remote_ip, remote_port) as sock:
        log.info(
            (f'Connected to {remote_ip}:{remote_port} '
             f'({dev_config.device_name}).'))
        CONNECTED_DEVICES.append(dev_config.device_id)
        dev_config.connected = True

        # send identity
        await sock.send(prepare_to_send(generate_my_identity()))
        log.debug('Identity packet sent to {remote_ip}:{remote_port}.')

        # wrap TLS
        ssock = await TLSStream.wrap(
            sock, server_side=True, ssl_context=dev_config.ssl_context(
                ssl.Purpose.CLIENT_AUTH))
        log.debug(f'Wrapped TLS connection with {remote_ip}:{remote_port}.')

        # handle pair if needed
        bssock = BufferedByteReceiveStream(ssock)
        if not dev_config.paired:
            if not await handle_pairing(ssock, bssock, dev_config):
                await ssock.aclose()
                return

        # receiving packets
        while True:
            pack = receive_packet(bssock)
            if not pack:
                continue

            # unpair packet
            if pack['type'] == 'kdeconnect.pair' and not pack['body']['pair']:
                dev_config.paired = False
                dev_config.save()
                await ssock.aclose()
                log.info('Unpairing {dev_config.device_name} done.')
                return

            if pack['type'] == 'kdeconnect.share.request':
                main_group.start_soon(download_file_task, pack, remote_ip,
                                      dev_config.ssl_context(
                                          ssl.Purpose.CLIENT_AUTH))


async def receive_packet(bssock: BufferedByteReceiveStream):
    try:
        pack_data = await bssock.receive_until(b'\n', 1024 * 1024 * 10)
    except Exception:
        log.exception('Error while receiving packet.')
        return False

    try:
        pack = json.loads(pack_data)
    except json.JSONDecodeError:
        log.exception(f'Error while decoding packet\n{pack_data}')
        return False

    log.debug(f'Received packet:\n{pack}')

    return pack


auto_send_pair_request = False
pair_request_auto_accept = False


async def handle_pairing(ssock: TLSStream, bssock: BufferedByteReceiveStream,
                         dev_config: DeviceConfig):
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
        pack = await receive_packet(bssock)
        if pack['type'] != 'kdeconnect.pair':
            log.error((
                'Unexpected packet type (expected kdeconnect.pair, got'
                f' {pack["type"]}).\n{pack!r}'))
            return False

        if not pack['body']['pair']:
            log.info('Unpairing {dev_config.device_name} done.')
            dev_config.paired = False
            dev_config.save()
            return False

        # TODO: pytanie do użytkownika

        await ssock.send(prepare_to_send(pair_pack))
        dev_config.paired = True
        dev_config.save()
        log.info('Pairing {dev_config.device_name} done.')
        return True


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


async def main():
    # config
    global config, PYCONNECT_CERTFILE, PYCONNECT_KEYFILE, PYCONNECT_DEVICE_ID,\
        PYCONNECT_DEVICE_NAME

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

    log.info((f'PYConnect starts as {PYCONNECT_DEVICE_NAME} '
              f'({PYCONNECT_DEVICE_ID})'))

    # main task
    could_send_my_id_packs.set()
    async with anyio.create_task_group() as main_group:
        main_group.start_soon(signal_handler, main_group.cancel_scope)
        main_group.start_soon(wait_for_incoming_id, main_group)
        main_group.start_soon(send_my_id_packets)

    log.info('Application end')


if __name__ == '__main__':
    anyio.run(main)
