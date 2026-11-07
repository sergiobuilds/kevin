"""Loopback Broadlink RM mini peer for real python-broadlink UDP traffic.

The fixture uses synthetic pulse sequences and a public test session key.
No packet here is a recorded or verified MYKA/train/consumer-device IR code.
Protocol reference: github.com/mjg59/python-broadlink/blob/master/protocol.md
"""
from __future__ import annotations

import copy
import hashlib
import ipaddress
import socketserver
import struct
import threading


class SimulatedIR:
    source = 'simulation'

    def __init__(self):
        from broadlink.remote import rmmini, pulses_to_data
        self.lock = threading.RLock()
        self._initial_cipher = rmmini(('127.0.0.1', 0), bytes(6), 0x2737)
        self._session_cipher = rmmini(('127.0.0.1', 0), bytes(6), 0x2737)
        self._session_key = bytes(range(16))  # Synthetic test key, never a device credential.
        self._session_cipher.update_aes(self._session_key)
        self._codes = {'stop': pulses_to_data([1000, 500, 500, 500, 500, 1500, 500, 2000]),
                       'forward': pulses_to_data([1000, 500, 500, 1500, 500, 500, 500, 2000])}
        self.reset()

    def reset(self):
        with self.lock:
            self.fault = 'none'
            self.armed = None
            self.learning = False
            self.train = 'stopped'
            self.sent = []
            self.traces = []

    def arm(self, name):
        if name not in self._codes:
            raise ValueError('only synthetic stop and forward buttons exist')
        with self.lock:
            self.armed = name

    def set_fault(self, fault):
        if fault not in ('none', 'timeout', 'no_code', 'bad_code', 'reject', 'ignore', 'bad_checksum'):
            raise ValueError('unsupported IR simulator fault')
        with self.lock:
            self.fault = fault

    def snapshot(self):
        with self.lock:
            return {'source': self.source, 'simulated': True, 'physically_verified': False,
                    'fault': self.fault, 'armed': self.armed, 'learning': self.learning,
                    'state': {'train': self.train}, 'sent': copy.deepcopy(self.sent),
                    'traces': copy.deepcopy(self.traces)}

    @staticmethod
    def _reply(request, payload, cipher, error=0):
        packet = bytearray(request[:0x38])
        packet[0x20:0x22] = bytes(2)
        packet[0x22:0x24] = struct.pack('<h', error)
        packet[0x26:0x28] = (0xe9 if request[0x26] == 0x65 else 0xee).to_bytes(2, 'little')
        packet[0x34:0x36] = (sum(payload, 0xbeaf) & 0xffff).to_bytes(2, 'little')
        packet.extend(cipher.encrypt(payload + bytes((-len(payload)) % 16)))
        packet[0x20:0x22] = (sum(packet, 0xbeaf) & 0xffff).to_bytes(2, 'little')
        return bytes(packet)

    def handle(self, packet):
        with self.lock:
            if (len(packet) < 0x38 or len(packet[0x38:]) % 16 or
                    packet[:8] != bytes.fromhex('5aa5aa555aa5aa55')):
                return None
            checksum = (sum(packet, 0xbeaf) - sum(packet[0x20:0x22])) & 0xffff
            if checksum != int.from_bytes(packet[0x20:0x22], 'little'):
                return None
            packet_type = int.from_bytes(packet[0x26:0x28], 'little')
            trace = {'packet_type': packet_type, 'operation': 'unknown',
                     'source': 'simulation', 'checksum_valid': True,
                     'encrypted_payload_bytes': len(packet) - 0x38}
            if self.fault == 'timeout':
                trace['operation'] = 'dropped'
                self.traces.append(trace)
                self.traces[:] = self.traces[-100:]
                return None
            cipher = self._initial_cipher if packet_type == 0x65 else self._session_cipher
            payload = cipher.decrypt(packet[0x38:])
            if (sum(payload, 0xbeaf) & 0xffff) != int.from_bytes(packet[0x34:0x36], 'little'):
                return None
            error = 0
            if packet_type == 0x65:
                trace['operation'] = 'auth'
                answer = (6).to_bytes(4, 'little') + self._session_key
            elif packet_type == 0x6a:
                command = int.from_bytes(payload[:4], 'little')
                answer = bytes(4)
                trace['operation'] = {2: 'send', 3: 'learn', 4: 'check'}.get(command, 'unsupported')
                if self.fault == 'reject':
                    error = -4
                elif command == 3:
                    self.learning = True
                elif command == 4:
                    if not self.learning or self.armed is None or self.fault == 'no_code':
                        error = -1
                    elif self.fault == 'bad_code':
                        answer += b'\x26\x00\xff\x7f\x01'
                    else:
                        answer += self._codes[self.armed]
                        self.learning = False
                elif command == 2:
                    from devices.ir import validate_code
                    try:
                        code = validate_code(payload[4:])
                        name = next((name for name, value in self._codes.items() if value == code), None)
                        if name is None:
                            error = -4
                        else:
                            applied = self.fault != 'ignore'
                            if applied:
                                self.train = 'moving' if name == 'forward' else 'stopped'
                            entry = {'name': name, 'code_sha256': hashlib.sha256(code).hexdigest(),
                                     'applied': applied, 'source': 'simulation'}
                            self.sent.append(entry)
                            self.sent[:] = self.sent[-100:]
                            trace.update(entry)
                    except ValueError:
                        error = -6
                else:
                    error = -4
            else:
                trace['operation'] = 'unsupported'
                answer, error = bytes(4), -4
            trace['error'] = error
            self.traces.append(trace)
            self.traces[:] = self.traces[-100:]
            response = self._reply(packet, answer, cipher, error)
            if self.fault == 'bad_checksum':
                response = response[:-1] + bytes([response[-1] ^ 1])
            return response


def make_server(host='127.0.0.1', port=0):
    if not ipaddress.IPv4Address(host).is_loopback:
        raise ValueError('IR fixture binds only to IPv4 loopback')
    device = SimulatedIR()

    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            packet, connection = self.request
            try:
                response = device.handle(packet)
            except (ValueError, IndexError, struct.error):
                return
            if response is not None:
                connection.sendto(response, self.client_address)

    server = socketserver.UDPServer((host, port), Handler)
    server.device = device
    server.server_port = server.server_address[1]
    return server
