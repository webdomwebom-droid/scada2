"""
Operaciones de alto nivel sobre un gateway Webdom, replicadas del proyecto
multi_gw_control (C#). Trabajan sobre un cliente Modbus TCP ya conectado
(unit id del gateway).

Referencias:
- GWitem.cs: getGWstatus, setConfigMode, Save_GW_NVM, Reset_GW, set_ssx_id_mac
- Process_GWs.cs: read_from_GW, write_to_GW, get_CBTB
- Process_slaves.cs: Read_ONE_slave, Write_SELECTED_slaves
- Process_scan.cs: Scan_slaves
- Process_files.cs: read_file_CBTB, write_file_CBTB
"""
import logging
import time
from typing import List, Optional

from . import constants as A
from . import codecs
from .protocol import ModbusTcpClient

logger = logging.getLogger(__name__)


# =====================================================================
# Helpers de nivel gateway
# =====================================================================

def read_gw_status(client: ModbusTcpClient) -> Optional[dict]:
    """GWitem.getGWstatus(): lee GW_DATA_TO_FE (6 registros)."""
    val = client.read_input_registers(A.GW_DATA_TO_FE, 6)
    if val is None or len(val) < 6:
        return None
    w0 = val[0]
    status = {
        "gw_status": (w0 >> 7) & 0x0F,
        "lora_updating": w0 & 0x000F,
        "cbt_modified": bool(w0 & 0x0010),
        "cbconf_modified": bool(w0 & 0x0020),
        "lst_modified": bool(w0 & 0x0040),
        "sntp_status": (w0 >> 11) & 0x07,
        "mac": "{0:02X}.{1:02X}.{2:02X}.{3:02X}.{4:02X}.{5:02X}.{6:02X}.{7:02X}".format(
            val[5] >> 8, val[5] & 0xFF, val[4] >> 8, val[4] & 0xFF,
            val[3] >> 8, val[3] & 0xFF, val[2] >> 8, val[2] & 0xFF),
        "slave_id": val[1],
    }
    return status


def read_sys_config(client: ModbusTcpClient) -> Optional[dict]:
    """lee REG_CFG_SYS (5 registros): modo, intervalo, zona horaria, DST, lora fail, threshold, gain."""
    val = client.read_input_registers(A.REG_CFG_SYS, 5)
    if val is None or len(val) < 5:
        return None
    mode_str = ["DataLog", "Config"]
    w2 = val[2]
    w4 = val[4]
    return {
        "mode_index": val[0],
        "mode": mode_str[val[0]] if val[0] < len(mode_str) else str(val[0]),
        "data_log_interval": val[1],
        "zone_time": (w2 & 0xFF) if (w2 & 0xFF) < 0x80 else (w2 & 0xFF) - 0x100,
        "dst_saving": (w2 >> 8) != 0,
        "n_lora_fail": w4 & 0xFF,
        "threshold": (w4 >> 8) & 0x3F,
        "gain": (w4 >> 14) & 0x03,
    }


def read_version(client: ModbusTcpClient) -> Optional[int]:
    val = client.read_input_registers(A.REG_VERSION, 1)
    if val is None or len(val) < 1:
        return None
    return val[0]


def get_mode_register(client: ModbusTcpClient) -> Optional[int]:
    val = client.read_input_registers(A.REG_CFG_SYS, 1)
    if val is None or len(val) < 1:
        return None
    return val[0]


def set_config_mode(client: ModbusTcpClient) -> str:
    """Pone el gateway en modo Config (escribe [1] en REG_CFG_SYS)."""
    return client.write_multiple_registers(A.REG_CFG_SYS, [1])


def set_mode(client: ModbusTcpClient, mode_index: int) -> str:
    """Escribe modo DataLog(0) o Config(1)."""
    return client.write_multiple_registers(A.REG_CFG_SYS, [int(mode_index)])


def save_gw_nvm(client: ModbusTcpClient) -> str:
    """Save_GW_NVM(): escribe [1] en SAVE_GW_CONFIG."""
    return client.write_multiple_registers(A.SAVE_GW_CONFIG, [1])


def reset_gw(client: ModbusTcpClient) -> str:
    """Reset_GW(): escribe CMD_RESET en CMD."""
    return client.write_multiple_registers(A.CMD, [A.CMD_RESET])


def send_gw_command(client: ModbusTcpClient, cmd_value: int) -> str:
    """Comando genérico al gateway escribiendo en CMD."""
    return client.write_multiple_registers(A.CMD, [int(cmd_value)])


def set_ssx_id_mac(client: ModbusTcpClient, cb_id: int, mac_bytes: bytes) -> str:
    """GWitem.set_ssx_id_mac(): escribe cbId + 8 bytes de MAC en SLV_ID_MAC (5 regs)."""
    if len(mac_bytes) < 8:
        mac_bytes = mac_bytes + b'\x00' * (8 - len(mac_bytes))
    buff = [int(cb_id) & 0xFF]
    # Los registros van de menos a más significativo, igual que la MAC leída de la
    # tabla CB y de GW_DATA_TO_FE: reg[0]=(m6,m7), reg[1]=(m4,m5), ...
    for i in range(3, -1, -1):
        hi = mac_bytes[2 * i]
        lo = mac_bytes[2 * i + 1]
        buff.append((hi << 8) | lo)
    return client.write_multiple_registers(A.SLV_ID_MAC, buff)


def wait_lora_updating(client: ModbusTcpClient, max_iter: int = 40,
                       poll_interval: float = 0.5) -> tuple:
    """
    Espera a que la actualización LoRa termine (lora_updating != waiting).
    Devuelve (ok: bool, status: dict).
    """
    for _ in range(max_iter):
        status = read_gw_status(client)
        if status is None:
            return False, status
        upd = status.get("lora_updating", 1)
        if upd != 1:  # ya no está "waiting"
            return upd == 0, status
        time.sleep(poll_interval)
    return False, read_gw_status(client)


# =====================================================================
# Tabla CB
# =====================================================================

def get_cb_table(client: ModbusTcpClient) -> tuple:
    """
    Obtiene la tabla CB completa.
    Devuelve (ok, {nslv, items:[decodificado...]})
    """
    val = client.read_input_registers(A.N_ITEMS_CB_TABLE, 1)
    if val is None or len(val) < 1:
        return False, {"error": "No response reading N_ITEMS_CB_TABLE"}
    nslv = val[0]
    items = []
    for _ in range(nslv):
        data = client.read_input_registers(A.READ_ITEM_CB_TABLE, 84)
        if data is None or len(data) < 84:
            return False, {"error": "Read CB item FAIL", "nslv": nslv, "items": items}
        item = _decode_cb_item(data)
        if item.get("id") != 0:
            items.append(item)
        time.sleep(0.1)
    return True, {"nslv": nslv, "items": items}


def _decode_cb_item(val: List[int]) -> dict:
    """CBitem.updateFromBytes(): decodifica 84 registros de un item CB."""
    mac = "{0:02X}.{1:02X}.{2:02X}.{3:02X}.{4:02X}.{5:02X}.{6:02X}.{7:02X}".format(
        val[4] >> 8, val[4] & 0xFF, val[3] >> 8, val[3] & 0xFF,
        val[2] >> 8, val[2] & 0xFF, val[1] >> 8, val[1] & 0xFF)

    key_bytes = bytearray(16)
    for i in range(16):
        reg = val[10 + i // 2]
        if i % 2 == 0:
            key_bytes[i] = reg & 0xFF
        else:
            key_bytes[i] = (reg >> 8) & 0xFF

    raw_bits = ((val[15] & 0xFFFF) << 16) | (val[14] & 0xFFFF)
    pream = val[16]
    fixed_pk = val[17]
    frq = ((val[19] & 0xFFFF) << 16) | (val[18] & 0xFFFF)

    comm_conf = []
    for i in range(2):
        base = 24 + 6 * i
        comm_conf.append(codecs.decode_comm_conf(val, base))

    lat = codecs.decode_regs_to_float(val, 44)
    lon = codecs.decode_regs_to_float(val, 46)

    sys_conf = {}
    if len(val) >= 80:
        sys_conf["config"] = ((val[67] & 0xFFFF) << 16) | (val[66] & 0xFFFF)
        sys_conf["data_log_interval"] = ((val[69] & 0xFFFF) << 16) | (val[68] & 0xFFFF)
        device_type = bytearray(4)
        try:
            device_type[0] = (val[78] >> 8) & 0xFF
            device_type[1] = val[78] & 0xFF
            device_type[2] = (val[79] >> 8) & 0xFF
            device_type[3] = val[79] & 0xFF
            sys_conf["device_type"] = bytes(device_type).decode(
                'utf-8', errors='ignore').replace('\x00', '').strip()
        except Exception:
            sys_conf["device_type"] = ""

    analog = {}
    try:
        analog["k"] = codecs.decode_regs_to_float(val, 84)
        analog["offset"] = codecs.decode_regs_to_float(val, 86)
        analog["n_mean"] = codecs.decode_regs_to_float(val, 88)
    except Exception:
        pass

    lo_conf = {
        "raw_bits": raw_bits,
        "pream_length": pream,
        "fixed_pk_length": fixed_pk,
        "frq": frq,
    }

    return {
        "id": val[0],
        "mac": mac,
        "key": codecs.key_bytes_to_str(key_bytes),
        "lo_conf": lo_conf,
        "comm_conf": comm_conf,
        "lat": lat,
        "lon": lon,
        "configured": (val[64] != 0) if len(val) > 64 else False,
        "updated": (val[80] != 0) if len(val) > 80 else False,
        "cmd_ok": (val[81] != 0) if len(val) > 81 else False,
        "datetime_ok": (val[82] != 0) if len(val) > 82 else False,
        "analog_in": analog,
        "sys_conf": sys_conf,
    }


# =====================================================================
# Operaciones sobre un esclavo (tarjeta) seleccionado
# =====================================================================

_CB_CACHE = {}
CB_CACHE_TTL = 300.0


def get_cb_items_cached(client: ModbusTcpClient, force: bool = False) -> List[dict]:
    """Items de la tabla CB con caché por gateway (evita releerla en cada operación)."""
    key = (client.ip, client.port)
    entry = _CB_CACHE.get(key)
    if not force and entry and (time.time() - entry["ts"]) < CB_CACHE_TTL:
        return entry["items"]
    ok, data = get_cb_table(client)
    if not ok:
        return entry["items"] if entry else []
    _CB_CACHE[key] = {"ts": time.time(), "items": data.get("items", [])}
    return _CB_CACHE[key]["items"]


def resolve_cb(client: ModbusTcpClient, cb_id: int, mac: Optional[str] = None) -> Optional[dict]:
    """
    Obtiene el item CB (id + MAC) necesario para seleccionar el esclavo.
    La MAC es imprescindible: set_ssx_id_mac la envía al gateway para dirigir la
    comunicación LoRa hacia ese esclavo.
    """
    if mac:
        return {"id": int(cb_id), "mac": mac}
    for force in (False, True):
        for item in get_cb_items_cached(client, force=force):
            if int(item.get("id", -1)) == int(cb_id):
                return item
    return None


def _select_slave(client: ModbusTcpClient, cb: dict) -> str:
    """set_ssx_id_mac a partir de un item CB."""
    mac = codecs.mac_str_to_bytes(cb.get("mac", ""))
    return set_ssx_id_mac(client, int(cb.get("id", 0)), bytes(mac))


def read_slave_lora_conf(client: ModbusTcpClient, cb: dict) -> dict:
    """Lee la configuración LoRa del esclavo (SLV_LORA_CONFIG, 10 regs)."""
    err = _select_slave(client, cb)
    if err:
        return {"ok": False, "error": err}
    err = client.write_multiple_registers(A.SLV_UPDATE_CFG_LORA, [0])
    if err:
        return {"ok": False, "error": err}
    ok, status = wait_lora_updating(client)
    if not ok:
        return {"ok": False, "error": "LoRa update error: " + A.lora_updating_to_text(
            status.get('lora_updating', -1) if status else -1)}
    val = client.read_input_registers(A.SLV_LORA_CONFIG, 10)
    if val is None:
        return {"ok": False, "error": "Read LoRa conf FAIL"}
    conf = codecs.decode_lora_conf(val)
    return {"ok": True, "lora_conf": conf.as_dict(), "id": cb.get("id"), "mac": cb.get("mac")}


def read_slave_analog_bottom(client: ModbusTcpClient, cb: dict) -> dict:
    """Lee la config analógica inferior (SLV_REG_CFG_ANALOGIN, 20 canales x 6)."""
    err = _select_slave(client, cb)
    if err:
        return {"ok": False, "error": err}
    err = client.write_multiple_registers(A.SLV_UPDATE_REG_CFG_ANALOG_IN, [0])
    if err:
        return {"ok": False, "error": err}
    ok, status = wait_lora_updating(client)
    if not ok:
        return {"ok": False, "error": "LoRa update error"}
    val = client.read_input_registers(A.SLV_REG_CFG_ANALOGIN, 20 * 6)
    if val is None:
        return {"ok": False, "error": "Read Analog Bottom FAIL"}
    channels = _decode_analog(val, 20)
    return {"ok": True, "channels": channels, "id": cb.get("id"), "mac": cb.get("mac")}


def read_slave_analog_top(client: ModbusTcpClient, cb: dict) -> dict:
    err = _select_slave(client, cb)
    if err:
        return {"ok": False, "error": err}
    err = client.write_multiple_registers(A.SLV_UPDATE_REG_CFG_ANALOG_IN_SLV, [0])
    if err:
        return {"ok": False, "error": err}
    ok, status = wait_lora_updating(client)
    if not ok:
        return {"ok": False, "error": "LoRa update error"}
    val = client.read_input_registers(A.SLV_REG_CFG_ANALOGIN_TOP, 18 * 6)
    if val is None:
        return {"ok": False, "error": "Read Analog Top FAIL"}
    channels = _decode_analog(val, 18)
    return {"ok": True, "channels": channels, "id": cb.get("id"), "mac": cb.get("mac")}


def read_slave_channel_map(client: ModbusTcpClient, cb: dict) -> dict:
    err = _select_slave(client, cb)
    if err:
        return {"ok": False, "error": err}
    err = client.write_multiple_registers(A.SLV_UPDATE_REG_CFG_MUX_CHANNELS, [0])
    if err:
        return {"ok": False, "error": err}
    ok, status = wait_lora_updating(client)
    if not ok:
        return {"ok": False, "error": "LoRa update error"}
    val = client.read_input_registers(A.SLV_REG_CFG_MUX_CHANNELS, 16)
    if val is None:
        return {"ok": False, "error": "Read Channel Map FAIL"}
    return {"ok": True, "channels": codecs.decode_channel_map(val),
            "id": cb.get("id"), "mac": cb.get("mac")}


def _decode_analog(val: List[int], n_channels: int) -> List[dict]:
    channels = []
    for i in range(n_channels):
        base = 6 * i
        channels.append({
            "channel": i,
            "k": codecs.decode_regs_to_float(val, base),
            "offset": codecs.decode_regs_to_float(val, base + 2),
            "n_mean": codecs.decode_regs_to_float(val, base + 4),
        })
    return channels


# =====================================================================
# Escritura de configuración a esclavos (Write_SELECTED_slaves)
# =====================================================================

def write_slave_config(client: ModbusTcpClient, cb: dict, mb_addr: int,
                       regs: List[int], save_nvm: bool = False) -> dict:
    """Escribe regs en mb_addr para un esclavo y opcionalmente guarda en NVM."""
    err = _select_slave(client, cb)
    if err:
        return {"ok": False, "error": err, "id": cb.get("id"), "mac": cb.get("mac")}
    err = client.write_multiple_registers(mb_addr, regs)
    if err:
        return {"ok": False, "error": err, "id": cb.get("id"), "mac": cb.get("mac")}
    ok, status = wait_lora_updating(client)
    if not ok:
        return {"ok": False, "error": "LoRa update error", "id": cb.get("id"), "mac": cb.get("mac")}
    if save_nvm:
        err = client.write_multiple_registers(A.SLV_CMD, [A.CMD_SAVE_CONFIG_NVM, 3])
        if err:
            return {"ok": False, "error": err, "id": cb.get("id"), "mac": cb.get("mac")}
        ok, status = wait_lora_updating(client)
        if not ok:
            return {"ok": False, "error": "LoRa update error (save NVM)",
                    "id": cb.get("id"), "mac": cb.get("mac")}
        return {"ok": True, "saved_nvm": True, "id": cb.get("id"), "mac": cb.get("mac")}
    return {"ok": True, "saved_nvm": False, "id": cb.get("id"), "mac": cb.get("mac")}


def write_slave_lora_conf(client: ModbusTcpClient, cb: dict, lora: dict,
                          save_nvm: bool = True) -> dict:
    conf = codecs.LoraConf(
        raw_bits=int(lora.get("raw_bits", 0)),
        pream_length=int(lora.get("pream_length", 0)),
        fixed_pk_length=int(lora.get("fixed_pk_length", 0)),
        frq=int(lora.get("frq", 0)),
    )
    regs = conf.to_registers()
    return write_slave_config(client, cb, A.SLV_LORA_CONFIG, regs, save_nvm)


def write_slave_analog_bottom(client: ModbusTcpClient, cb: dict, channels: List[dict],
                              save_nvm: bool = True) -> dict:
    regs = []
    for i in range(20):
        ch = channels[i] if i < len(channels) else {"k": 3.3333, "offset": 0, "n_mean": 50}
        regs += codecs.encode_float_to_regs(float(ch.get("k", 3.3333)))
        regs += codecs.encode_float_to_regs(float(ch.get("offset", 0)))
        regs += codecs.encode_float_to_regs(float(ch.get("n_mean", 50)))
    return write_slave_config(client, cb, A.SLV_REG_CFG_ANALOGIN, regs, save_nvm)


def write_slave_analog_top(client: ModbusTcpClient, cb: dict, channels: List[dict],
                           save_nvm: bool = True) -> dict:
    regs = []
    for i in range(18):
        ch = channels[i] if i < len(channels) else {"k": 3.3333, "offset": 0, "n_mean": 50}
        regs += codecs.encode_float_to_regs(float(ch.get("k", 3.3333)))
        regs += codecs.encode_float_to_regs(float(ch.get("offset", 0)))
        regs += codecs.encode_float_to_regs(float(ch.get("n_mean", 50)))
    return write_slave_config(client, cb, A.SLV_REG_CFG_ANALOGIN_TOP, regs, save_nvm)


def write_slave_channel_map(client: ModbusTcpClient, cb: dict, channels: List[int],
                            save_nvm: bool = True) -> dict:
    #
    # El canal asignado a cada toroide se empaqueta: reg[i] = (ch[2i+1]<<8) | ch[2i]
    #
    chans = [int(c) for c in channels]
    if len(chans) < 32:
        chans += [i + 1 for i in range(len(chans), 32)]
    regs = []
    for i in range(16):
        lo = chans[2 * i] & 0xFF
        hi = chans[2 * i + 1] & 0xFF
        regs.append((hi << 8) | lo)
    return write_slave_config(client, cb, A.SLV_REG_CFG_MUX_CHANNELS, regs, save_nvm)


def send_ssx_cmd(client: ModbusTcpClient, cb: dict, cmd_value: int, typ: int,
                 save_nvm: bool = True) -> dict:
    return write_slave_config(client, cb, A.SLV_CMD, [int(cmd_value), int(typ)], save_nvm)


# =====================================================================
# Escaneo LoRa (Process_scan.Scan_slaves)
# =====================================================================

def scan_slaves(client: ModbusTcpClient, cb_list: List[dict]) -> dict:
    """Escanea la señal LoRa de cada esclavo de la lista (SNR/RSSI)."""
    results = []
    # poner el gateway en modo config
    set_mode(client, 1)
    for cb in cb_list:
        err = _select_slave(client, cb)
        if err:
            results.append({"id": cb.get("id"), "mac": cb.get("mac"), "ok": False, "error": err})
            continue
        scan = None
        for _ in range(3):
            client.write_multiple_registers(A.SLV_UPDATE_REG_LORA, [0])
            ok, status = wait_lora_updating(client)
            if not ok:
                scan = {"ok": False, "error": "LoRa: " + A.lora_updating_to_text(
                    status.get('lora_updating', -1) if status else -1),
                    "id": cb.get("id"), "mac": cb.get("mac")}
                continue
            val = client.read_input_registers(A.SLV_REG_LORA, 2)
            if val is None:
                continue
            decode = lambda v: (v & 0xFF if v & 0x80 else v) if v else 0
            scan = {
                "ok": True,
                "id": cb.get("id"),
                "mac": cb.get("mac"),
                "pkt_snr": _to_signed(val[0] & 0xFF),
                "pkt_rssi": _to_signed((val[0] >> 8) & 0xFF),
                "rssi": _to_signed(val[1] & 0xFF),
            }
            break
        if scan:
            results.append(scan)
    return {"ok": True, "items": results}


def _to_signed(v: int) -> int:
    if v & 0x80:
        return v - 0x100
    return v


# =====================================================================
# Gestión de archivos (Process_files + ModbusOp)
# =====================================================================

DIR_ADDRESSES = {
    "LOGS/": A.OPEN_LOG_DIR,
    "LOG/": A.OPEN_LOG_DIR,
    "DATA/": A.OPEN_DATA_DIR,
    "CBTB/": A.OPEN_CBTB_DIR,
    "STDS/": A.OPEN_STDS_DIR,
}


def read_dir(client: ModbusTcpClient, directory: str) -> dict:
    """Lista los archivos de un directorio (LOGS/, DATA/, CBTB/, STDS/)."""
    addr = DIR_ADDRESSES.get((directory or "").upper())
    if addr is None:
        return {"ok": False, "error": f"Directorio no soportado: {directory}",
                "items": [], "directories": sorted(set(DIR_ADDRESSES))}
    response = client.read_input_registers(addr, 6)
    if response is None or len(response) < 6:
        return {"ok": False, "error": "No se pudo abrir el directorio", "items": []}
    file_list = []
    name = _regs_to_string(response)
    count = 0
    while name and (response[0] >> 8) != 0 and (response[0] & 0xFF) != 0 and count < 500:
        file_list.append(name)
        response = client.read_input_registers(A.READ_DIR, 6)
        if response is None:
            break
        name = _regs_to_string(response)
        count += 1
    file_list.sort()
    return {"ok": True, "directory": directory,
            "items": [{"name": n, "directory": False} for n in file_list]}


def _regs_to_string(regs: List[int]) -> str:
    raw = bytearray()
    for r in regs:
        raw.append((r >> 8) & 0xFF)
        raw.append(r & 0xFF)
    return bytes(raw).decode('utf-8', errors='ignore').replace('\x00', '')


def erase_file(client: ModbusTcpClient, filename: str) -> dict:
    buff = _string_to_regs(filename)
    err = client.write_multiple_registers(A.ERASE_FILE, buff)
    return {"ok": err is None, "error": err}


def _string_to_regs(s: str) -> List[int]:
    data = s.encode('utf-8')
    if len(data) % 2 == 1:
        data += b'\x00'
    regs = []
    for i in range(0, len(data), 2):
        regs.append((data[i] << 8) | data[i + 1])
    return regs


def send_path_filename(client: ModbusTcpClient, directory: str, filename: str) -> str:
    file_path = directory + filename
    regs = _string_to_regs(file_path)
    return client.write_multiple_registers(A.SEND_PATH_FILE, regs)


def read_file(client: ModbusTcpClient, directory: str, filename: str,
              block_size: int = 200) -> dict:
    """Descarga un archivo del gateway en bloques (igual que read_file_CBTB)."""
    set_mode(client, 1)
    err = send_path_filename(client, directory, filename)
    if err:
        return {"ok": False, "error": "sendPath: " + err}
    response = client.read_input_registers(A.OPEN_FILE, 2)
    if response is None or len(response) < 2:
        return {"ok": False, "error": "No se puede abrir el archivo"}
    file_size = (response[1] << 16) | response[0]
    data = bytearray()
    n_blocks = file_size // block_size + 1
    for i in range(n_blocks):
        if i == file_size // block_size:
            if (file_size - i * block_size) == 0:
                break
            chunk_regs = client.read_input_registers(
                A.READ_FILE_BLOCK, (file_size - i * block_size) // 2)
        else:
            chunk_regs = client.read_input_registers(A.READ_FILE_BLOCK, block_size // 2)
        if chunk_regs is None:
            return {"ok": False, "error": "Read file block FAIL", "data": bytes(data)}
        for r in chunk_regs:
            data.append((r >> 8) & 0xFF)
            data.append(r & 0xFF)
    return {"ok": True, "file_size": file_size, "data": bytes(data)}


def write_file(client: ModbusTcpClient, directory: str, filename: str,
               content: bytes) -> dict:
    """Sube un archivo al gateway en bloques (igual que write_file_CBTB)."""
    set_mode(client, 1)
    err = send_path_filename(client, directory, filename)
    if err:
        return {"ok": False, "error": "sendPath: " + err}
    response = client.read_input_registers(A.OPEN_FILE_TO_WRITE, 1)
    if response is None:
        return {"ok": False, "error": "No se puede abrir archivo para escritura"}
    data = content
    block = 200
    offset = 0
    while offset < len(data):
        piece = data[offset:offset + block]
        regs = []
        p = piece
        if len(p) % 2 == 1:
            p += b'\x00'
        for i in range(0, len(p), 2):
            regs.append((p[i] << 8) | p[i + 1])
        err = client.write_multiple_registers(A.WRITE_FILE_BLOCK, regs)
        if err:
            return {"ok": False, "error": err}
        offset += block
    client.read_input_registers(A.CLOSE_FILE_TO_WRITE, 1)
    return {"ok": True}


# =====================================================================
# Escaneo LoRa table (SCANLORA)
# =====================================================================

def scan_lora_table(client: ModbusTcpClient) -> dict:
    """
    Inicia el escaneo LoRa del gateway y lee la tabla de dispositivos escaneados.
    """
    set_mode(client, 1)
    # iniciar escaneo
    err = client.write_multiple_registers(A.SCANLORA_BEGIN, [1])
    if err:
        return {"ok": False, "error": err}
    # leer número de items
    val = client.read_input_registers(A.N_ITEMS_SCANLORA_TABLE, 1)
    if val is None:
        return {"ok": False, "error": "No response N_ITEMS_SCANLORA_TABLE"}
    n_items = val[0]
    items = []
    for idx in range(n_items):
        val = client.read_input_registers(A.SCAN_LORA_IDX, 1)
        if val is not None:
            items.append({"index": idx, "value": val[0]})
    return {"ok": True, "n_items": n_items, "items": items}
