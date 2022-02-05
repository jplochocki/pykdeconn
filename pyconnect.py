#! /usr/bin/env python3

import logging
import logging.config
import socket
import json
import time
import ssl
import os.path
import signal

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
    format = '%(asctime)s / %(levelname)s (%(lineno)d) :::  %(message)s'

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


async def wait_for_incoming_id(main_group):
    async with await anyio.create_udp_socket(
            family=socket.AF_INET, local_host='255.255.255.255',
            local_port=KDE_CONNECT_DEFAULT_PORT) as udp:
        async for data, (host, port) in udp:
            try:
                pack = json.loads(data.decode('utf-8'))
            except json.JSONDecodeError:
                log.exception(('wait_for_incoming_id(): malformed id packet '
                               f'{data} from {host}:{port}'))
                return

            if pack['type'] != 'kdeconnect.identity' or 'deviceId' not in pack['body']:  # noqa
                log.warning(
                    ('wait_for_incoming_id(): identity packet without '
                     f'body.deviceId or unknown type\n{pack=}'))
                return

            dev_id = pack['body']['deviceId']
            known = dev_id in GSCONNECT_KNOWN_DEVICES
            log.info(('wait_for_incoming_id(): id packet received from '
                      f'{pack["body"]["deviceName"]} / {dev_id} (IP: {host})'
                      f'{known=}'))

            if not known or dev_id == GSCONNECT_DEVICE_ID or dev_id in CONNECTED_DEVICES:  # noqa
                return

            main_group.start_soon(device_connection_task, pack, host)


def generate_my_identity():
    return {
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


def prepare_to_send(pack):
    pack2 = pack.copy()
    pack2['id'] = time.time_ns()
    return (json.dumps(pack2) + '\n').encode('utf-8')


async def device_connection_task(id_packet, remote_ip):
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
    async with anyio.create_task_group() as main_group:
        main_group.start_soon(signal_handler, main_group.cancel_scope)
        main_group.start_soon(wait_for_incoming_id, main_group)

    log.info('Application end')


if __name__ == '__main__':
    anyio.run(main)
