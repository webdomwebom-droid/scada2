"""
Cliente Modbus TCP de bajo nivel replicando la mecánica de
modbusTcpClient.cs + ModbusOp.cs del proyecto multi_gw_control (C#).

- Lee INPUT REGISTERS (función Modbus 0x04), igual que el multiGW.
- Escribe MULTIPLE REGISTERS (función Modbus 0x10).
- Realiza reconexión automática si la conexión falla.
- Unit ID = 0 (acceso al gateway; los esclavos se seleccionan vía set_ssx_id_mac).
"""
import socket
import struct
import time
import logging
from typing import List

logger = logging.getLogger(__name__)

DEFAULT_PORT = 502
DEFAULT_UNIT = 0
CONNECT_TIMEOUT = 5.0
READ_TIMEOUT = 5.0
MAX_REGISTERS = 120


class ModbusTcpClient:

    def __init__(self, ip: str, port: int = DEFAULT_PORT, unit: int = DEFAULT_UNIT,
                 connect_timeout: float = CONNECT_TIMEOUT, read_timeout: float = READ_TIMEOUT):
        self.ip = ip
        self.port = port
        self.unit = unit
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self._sock: socket.socket = None
        self.transaction_id = 1

    # ------------------------------------------------------------------
    # Gestión de conexión
    # ------------------------------------------------------------------
    def _connect(self):
        self.close()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(self.connect_timeout)
        sock.connect((self.ip, self.port))
        sock.settimeout(self.read_timeout)
        self._sock = sock
        logger.debug(f"Modbus conectado a {self.ip}:{self.port}")

    def close(self):
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None

    def __enter__(self):
        self._connect()
        return self

    def __exit__(self, *args):
        self.close()

    # ------------------------------------------------------------------
    # Construcción de tramas Modbus TCP
    # ------------------------------------------------------------------
    def _next_tid(self) -> bytes:
        tid = self.transaction_id & 0xFFFF
        self.transaction_id += 1
        return struct.pack('>H', tid)

    def _build_read_request(self, register: int, count: int,
                            function: int = 0x04) -> bytes:
        tid = self._next_tid()
        mbap = tid + struct.pack('>HH', 0, 6)  # protocol=0, length=6
        pdu = bytes([self.unit, function,
                     (register >> 8) & 0xFF, register & 0xFF,
                     (count >> 8) & 0xFF, count & 0xFF])
        return mbap + pdu

    def _build_write_request(self, register: int, values: List[int]) -> bytes:
        count = len(values)
        byte_count = count * 2
        tid = self._next_tid()
        mbap = tid + struct.pack('>HH', 0, 6 + 1 + byte_count)  # length = 6 + 1 + byte_count
        pdu = bytes([self.unit, 0x10,
                     (register >> 8) & 0xFF, register & 0xFF,
                     (count >> 8) & 0xFF, count & 0xFF,
                     byte_count])
        data = b''
        for v in values:
            data += struct.pack('>H', v & 0xFFFF)
        return mbap + pdu + data

    def _recv_exact(self, length: int) -> bytes:
        data = b''
        deadline = time.time() + self.read_timeout
        while len(data) < length:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise TimeoutError("Timeout esperando respuesta Modbus")
            self._sock.settimeout(min(self.read_timeout, max(0.1, remaining)))
            chunk = self._sock.recv(length - len(data))
            if not chunk:
                raise ConnectionError("Conexión Modbus cerrada por el servidor")
            data += chunk
        return data

    def _read_response(self):
        header = self._recv_exact(7)  # tid(2)+proto(2)+len(2)+unit(1)
        # length del MBAP cuenta unit + PDU; el unit ya viene en la cabecera leída
        pdu_len = struct.unpack('>H', header[4:6])[0] - 1
        if pdu_len <= 0:
            raise IOError("Respuesta Modbus con longitud inválida")
        pdu = self._recv_exact(pdu_len)
        unit = header[6]
        function = pdu[0]
        if function & 0x80:
            code = pdu[1] if len(pdu) > 1 else -1
            raise IOError(f"Excepción Modbus (función 0x{function & 0x7F:02X}, código {code})")
        return function, pdu

    # ------------------------------------------------------------------
    # Operaciones públicas (con reconexión automática)
    # ------------------------------------------------------------------
    def read_input_registers(self, address: int, count: int):
        """Lee count registros de entrada (función 0x04). Devuelve lista o None."""
        if count > MAX_REGISTERS:
            raise ValueError(f"count {count} supera MAX_REGISTERS={MAX_REGISTERS}")
        try:
            if self._sock is None:
                self._connect()
            self._sock.sendall(self._build_read_request(address, count, 0x04))
            function, pdu = self._read_response()
            byte_count = pdu[1] if len(pdu) > 1 else 0
            data = pdu[2:2 + byte_count]
            regs = []
            for i in range(0, len(data) - 1, 2):
                regs.append((data[i] << 8) | data[i + 1])
            return regs
        except Exception:
            logger.debug(f"Modbus read falló {self.ip}@{address}, reconectando...")
            return self._retry_read(address, count)

    def _retry_read(self, address: int, count: int):
        try:
            self._connect()
            self._sock.sendall(self._build_read_request(address, count, 0x04))
            function, pdu = self._read_response()
            byte_count = pdu[1] if len(pdu) > 1 else 0
            data = pdu[2:2 + byte_count]
            regs = []
            for i in range(0, len(data) - 1, 2):
                regs.append((data[i] << 8) | data[i + 1])
            return regs
        except Exception as exc:
            logger.warning(f"Modbus read {self.ip}@{address} falló: {exc}")
            return None

    def write_multiple_registers(self, address: int, values) -> str:
        """Escribe múltiples registros (función 0x10). Devuelve None si OK, o mensaje de error."""
        values = [int(v) & 0xFFFF for v in values]
        try:
            self._send_write(address, values)
            return None
        except Exception:
            logger.debug(f"Modbus write falló {self.ip}@{address}, reconectando...")
            return self._retry_write(address, values)

    def _send_write(self, address: int, values):
        if self._sock is None:
            self._connect()
        self._sock.sendall(self._build_write_request(address, values))
        function, pdu = self._read_response()

    def _retry_write(self, address: int, values) -> str:
        try:
            self._connect()
            self._send_write(address, values)
            return None
        except Exception as exc:
            return f"{self.ip}: Error Modbus WRITE. Addr: {address:#06x}, n_regs: {len(values)}: {exc}"


