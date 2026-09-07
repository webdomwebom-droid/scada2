"""Pruebas de trama Modbus TCP del control avanzado (lectura 0x04 / escritura 0x10)."""
import socket
import struct
import threading

import pytest

from app.services.gw_control.protocol import ModbusTcpClient


class FakeGateway:
    """Servidor Modbus TCP mínimo que registra las peticiones recibidas."""

    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(('127.0.0.1', 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.requests = []
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                self._handle(conn)

    def _handle(self, conn):
        while True:
            header = conn.recv(6)
            if len(header) < 6:
                return
            length = struct.unpack('>H', header[4:6])[0]
            body = b''
            while len(body) < length:
                chunk = conn.recv(length - len(body))
                if not chunk:
                    return
                body += chunk
            self.requests.append(header + body)
            unit, function = body[0], body[1]
            if function == 0x04:
                count = struct.unpack('>H', body[4:6])[0]
                payload = bytes([unit, function, count * 2]) + b''.join(
                    struct.pack('>H', i) for i in range(count))
            else:
                payload = bytes([unit, function]) + body[2:6]
            conn.sendall(header[:4] + struct.pack('>H', len(payload)) + payload)

    def close(self):
        self.sock.close()


@pytest.fixture
def gateway():
    gw = FakeGateway()
    yield gw
    gw.close()


def test_read_input_registers(gateway):
    client = ModbusTcpClient('127.0.0.1', gateway.port)
    try:
        values = client.read_input_registers(0x6600, 6)
    finally:
        client.close()
    assert values == [0, 1, 2, 3, 4, 5]
    request = gateway.requests[0]
    assert struct.unpack('>H', request[4:6])[0] == 6
    assert request[7] == 0x04
    assert struct.unpack('>H', request[8:10])[0] == 0x6600


def test_write_multiple_registers(gateway):
    client = ModbusTcpClient('127.0.0.1', gateway.port)
    try:
        error = client.write_multiple_registers(0x4F00, [1, 2, 3])
    finally:
        client.close()
    assert error is None
    request = gateway.requests[0]
    # MBAP: protocolo 0 y longitud = unit + funcion + addr + count + bytecount + datos
    assert struct.unpack('>H', request[2:4])[0] == 0
    assert struct.unpack('>H', request[4:6])[0] == 7 + 3 * 2
    assert len(request) == 6 + struct.unpack('>H', request[4:6])[0]
    assert request[7] == 0x10
    assert request[12] == 6
    assert list(struct.unpack('>3H', request[13:19])) == [1, 2, 3]
