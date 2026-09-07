"""
Endpoints de control avanzado de gateways (replican las funciones del programa
multi_gw_control / C#). Cada endpoint conecta la VPN de la planta del gateway,
ejecuta la operación Modbus y desconecta la VPN al finalizar.
"""
import base64
import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from pydantic import BaseModel

from app.core.database import get_db
from app.api.deps import get_current_user, require_admin
from app.models.user import User
from app.models.gateway import Gateway
from app.models.plant import Plant
from app.services.gw_control import operations as ops
from app.services.gw_control import constants as A
from app.services.gw_control import codecs
from app.services.gw_control.context import run_gateway_op, reconnect_gateway
from app.services.vpn_service_v2 import vpn_service

logger = logging.getLogger(__name__)

router = APIRouter()


# =====================================================================
# Esquemas de entrada
# =====================================================================

class LoraConfIn(BaseModel):
    raw_bits: Optional[int] = None
    pream_length: Optional[int] = None
    fixed_pk_length: Optional[int] = None
    frq: Optional[int] = None


class LoraConfWriteIn(BaseModel):
    low_data_rate_opt: Optional[bool] = None
    crc_dis: Optional[bool] = None
    explicit_en: Optional[bool] = None
    fix_pkln_en: Optional[bool] = None
    bandwidth: Optional[int] = None
    coding_rate: Optional[int] = None
    sfactor: Optional[int] = None
    tx_pwr: Optional[int] = None
    pream_length: Optional[int] = None
    fixed_pk_length: Optional[int] = None
    frq: Optional[int] = None


class AnalogChannelIn(BaseModel):
    k: Optional[float] = None
    offset: Optional[float] = None
    n_mean: Optional[float] = None


class ChannelMapIn(BaseModel):
    channels: List[int]  # 32 canales (channel asignado al toroide i)


class SlaveCmdIn(BaseModel):
    cmd: int
    typ: int = 3
    save_nvm: bool = True


class SlaveSelectIn(BaseModel):
    ids: Optional[List[int]] = None  # ids de la tabla CB; si None, todos


class FileContentIn(BaseModel):
    data: str  # base64


class ModeIn(BaseModel):
    mode: int  # 0=DataLog, 1=Config


class CommandIn(BaseModel):
    value: int


# =====================================================================
# Helpers
# =====================================================================

def _require_admin(user: User):
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Solo administradores")


def _get_gateway(db: Session, gateway_id: int) -> Gateway:
    gateway = db.query(Gateway).filter(Gateway.id == gateway_id).first()
    if not gateway:
        raise HTTPException(status_code=404, detail="Gateway no encontrado")
    return gateway


def _slave_op(cb_id: int, mac: Optional[str], op, *args, **kwargs):
    """Resuelve el item CB (id + MAC) y ejecuta la operación sobre ese esclavo."""
    def _wrap(client):
        cb = ops.resolve_cb(client, cb_id, mac)
        if cb is None:
            return {"ok": False, "error": f"Esclavo CB#{cb_id} no está en la tabla CB del gateway"}
        return op(client, cb, *args, **kwargs)
    return _wrap


def _check(result):
    if result is None:
        raise HTTPException(status_code=502, detail="Sin respuesta del gateway (¿VPN conectada?)")
    if isinstance(result, dict) and result.get("ok") is False:
        raise HTTPException(status_code=502, detail=result.get("error", "Error de conexión"))
    return result


# =====================================================================
# Operaciones de nivel gateway (estado, config, comandos)
# =====================================================================

@router.get("/{gateway_id}/connection")
async def gateway_connection(gateway_id: int, db: Session = Depends(get_db),
                             current_user: User = Depends(get_current_user)):
    """Estado de la VPN/túnel usado por este gateway (sin tocar el gateway)."""
    gateway = _get_gateway(db, gateway_id)
    plant_name = gateway.plant.name if gateway.plant else None
    return {
        "plant": plant_name,
        "gateway_ip": gateway.ip,
        "vpn_connected": bool(vpn_service.vpn_connected),
        "vpn_plant": vpn_service.current_plant_name,
        "vpn_ready": bool(vpn_service.vpn_connected and plant_name
                          and vpn_service.is_connected_to(plant_name)),
        "method": (vpn_service.current_vpn_config.vpn_type
                   if vpn_service.current_vpn_config else None),
        "uptime_seconds": vpn_service.get_connection_uptime(),
        "demo": bool(vpn_service.demo_mode),
    }


@router.post("/{gateway_id}/reconnect")
async def gateway_reconnect(gateway_id: int, db: Session = Depends(get_db),
                            current_user: User = Depends(get_current_user)):
    """Reconecta la VPN de la planta y verifica el gateway (botón de campo)."""
    _get_gateway(db, gateway_id)
    result = await reconnect_gateway(gateway_id)
    if not result.get("ok"):
        raise HTTPException(status_code=502, detail=result.get("error", "No se pudo reconectar"))
    return result


@router.get("/{gateway_id}/status")
async def gateway_status(gateway_id: int, db: Session = Depends(get_db),
                         current_user: User = Depends(get_current_user)):
    _get_gateway(db, gateway_id)
    return _check(await run_gateway_op(gateway_id, ops.read_gw_status))


@router.get("/{gateway_id}/firmware")
async def gateway_firmware(gateway_id: int, db: Session = Depends(get_db),
                           current_user: User = Depends(get_current_user)):
    _get_gateway(db, gateway_id)
    result = _check(await run_gateway_op(gateway_id, ops.read_version))
    return {"version": result}


@router.get("/{gateway_id}/sys-config")
async def gateway_sys_config(gateway_id: int, db: Session = Depends(get_db),
                             current_user: User = Depends(get_current_user)):
    _get_gateway(db, gateway_id)
    return _check(await run_gateway_op(gateway_id, ops.read_sys_config))


@router.post("/{gateway_id}/mode")
async def gateway_set_mode(gateway_id: int, data: ModeIn,
                           db: Session = Depends(get_db),
                           current_user: User = Depends(require_admin)):
    _get_gateway(db, gateway_id)
    result = await run_gateway_op(gateway_id, ops.set_mode, data.mode)
    return {"ok": result is None, "error": result}


@router.post("/{gateway_id}/save-nvm")
async def gateway_save_nvm(gateway_id: int, db: Session = Depends(get_db),
                           current_user: User = Depends(require_admin)):
    _get_gateway(db, gateway_id)
    result = await run_gateway_op(gateway_id, ops.save_gw_nvm)
    return {"ok": result is None, "error": result}


@router.post("/{gateway_id}/reset")
async def gateway_reset(gateway_id: int, db: Session = Depends(get_db),
                        current_user: User = Depends(require_admin)):
    _get_gateway(db, gateway_id)
    result = await run_gateway_op(gateway_id, ops.reset_gw)
    return {"ok": result is None, "error": result}


@router.post("/{gateway_id}/command")
async def gateway_command(gateway_id: int, data: CommandIn,
                          db: Session = Depends(get_db),
                          current_user: User = Depends(require_admin)):
    _get_gateway(db, gateway_id)
    result = await run_gateway_op(gateway_id, ops.send_gw_command, data.value)
    return {"ok": result is None, "error": result}


# =====================================================================
# Tabla CB
# =====================================================================

@router.get("/{gateway_id}/cb-table")
async def gateway_cb_table(gateway_id: int, db: Session = Depends(get_db),
                           current_user: User = Depends(get_current_user)):
    _get_gateway(db, gateway_id)
    result = await run_gateway_op(gateway_id, ops.get_cb_table)
    if not result[0]:
        raise HTTPException(status_code=502, detail=result[1].get("error"))
    data = result[1]
    return {"ok": True, "nslv": data.get("nslv", 0), "items": data.get("items", [])}


# =====================================================================
# Operaciones sobre esclavos
# =====================================================================

@router.get("/{gateway_id}/slaves/{cb_id}/lora")
async def slave_lora_conf(gateway_id: int, cb_id: int, mac: Optional[str] = None,
                          db: Session = Depends(get_db),
                          current_user: User = Depends(get_current_user)):
    _get_gateway(db, gateway_id)
    return _check(await run_gateway_op(
        gateway_id, _slave_op(cb_id, mac, ops.read_slave_lora_conf)))


@router.get("/{gateway_id}/slaves/{cb_id}/analog-bottom")
async def slave_analog_bottom(gateway_id: int, cb_id: int, mac: Optional[str] = None,
                              db: Session = Depends(get_db),
                              current_user: User = Depends(get_current_user)):
    _get_gateway(db, gateway_id)
    return _check(await run_gateway_op(
        gateway_id, _slave_op(cb_id, mac, ops.read_slave_analog_bottom)))


@router.get("/{gateway_id}/slaves/{cb_id}/analog-top")
async def slave_analog_top(gateway_id: int, cb_id: int, mac: Optional[str] = None,
                           db: Session = Depends(get_db),
                           current_user: User = Depends(get_current_user)):
    _get_gateway(db, gateway_id)
    return _check(await run_gateway_op(
        gateway_id, _slave_op(cb_id, mac, ops.read_slave_analog_top)))


@router.get("/{gateway_id}/slaves/{cb_id}/channel-map")
async def slave_channel_map(gateway_id: int, cb_id: int, mac: Optional[str] = None,
                            db: Session = Depends(get_db),
                            current_user: User = Depends(get_current_user)):
    _get_gateway(db, gateway_id)
    return _check(await run_gateway_op(
        gateway_id, _slave_op(cb_id, mac, ops.read_slave_channel_map)))


# --- Escrituras ---

@router.post("/{gateway_id}/slaves/{cb_id}/lora")
async def write_slave_lora(gateway_id: int, cb_id: int, data: LoraConfWriteIn,
                           mac: Optional[str] = None,
                           db: Session = Depends(get_db),
                           current_user: User = Depends(require_admin)):
    _get_gateway(db, gateway_id)
    lora = _build_lora_from_flags(data)
    return _check(await run_gateway_op(
        gateway_id, _slave_op(cb_id, mac, ops.write_slave_lora_conf, lora, save_nvm=True)))


@router.post("/{gateway_id}/slaves/{cb_id}/analog-bottom")
async def write_slave_analog_bottom(gateway_id: int, cb_id: int, channels: List[AnalogChannelIn],
                                    mac: Optional[str] = None,
                                    db: Session = Depends(get_db),
                                    current_user: User = Depends(require_admin)):
    _get_gateway(db, gateway_id)
    chans = [c.dict() for c in channels]
    return _check(await run_gateway_op(
        gateway_id, _slave_op(cb_id, mac, ops.write_slave_analog_bottom, chans, save_nvm=True)))


@router.post("/{gateway_id}/slaves/{cb_id}/analog-top")
async def write_slave_analog_top(gateway_id: int, cb_id: int, channels: List[AnalogChannelIn],
                                 mac: Optional[str] = None,
                                 db: Session = Depends(get_db),
                                 current_user: User = Depends(require_admin)):
    _get_gateway(db, gateway_id)
    chans = [c.dict() for c in channels]
    return _check(await run_gateway_op(
        gateway_id, _slave_op(cb_id, mac, ops.write_slave_analog_top, chans, save_nvm=True)))


@router.post("/{gateway_id}/slaves/{cb_id}/channel-map")
async def write_slave_channel_map(gateway_id: int, cb_id: int, data: ChannelMapIn,
                                  mac: Optional[str] = None,
                                  db: Session = Depends(get_db),
                                  current_user: User = Depends(require_admin)):
    _get_gateway(db, gateway_id)
    return _check(await run_gateway_op(
        gateway_id, _slave_op(cb_id, mac, ops.write_slave_channel_map, data.channels,
                              save_nvm=True)))


@router.post("/{gateway_id}/slaves/{cb_id}/command")
async def send_slave_cmd(gateway_id: int, cb_id: int, data: SlaveCmdIn,
                         mac: Optional[str] = None,
                         db: Session = Depends(get_db),
                         current_user: User = Depends(require_admin)):
    _get_gateway(db, gateway_id)
    return _check(await run_gateway_op(
        gateway_id, _slave_op(cb_id, mac, ops.send_ssx_cmd, data.cmd, data.typ, data.save_nvm)))


@router.post("/{gateway_id}/slaves/{cb_id}/zero")
async def slave_zero(gateway_id: int, cb_id: int, data: Optional[dict] = None,
                     mac: Optional[str] = None,
                     db: Session = Depends(get_db),
                     current_user: User = Depends(require_admin)):
    typ = (data or {}).get("typ", 3)
    _get_gateway(db, gateway_id)
    return _check(await run_gateway_op(
        gateway_id, _slave_op(cb_id, mac, ops.send_ssx_cmd, A.CMD_ZERO, typ, True)))


# =====================================================================
# Escaneo LoRa
# =====================================================================

@router.post("/{gateway_id}/lora-scan")
async def gateway_lora_scan(gateway_id: int, data: Optional[SlaveSelectIn] = None,
                            db: Session = Depends(get_db),
                            current_user: User = Depends(get_current_user)):
    _get_gateway(db, gateway_id)
    selected_ids = set(data.ids or []) if data else set()

    def _wrap(client):
        items = ops.get_cb_items_cached(client, force=True)
        if not items:
            return {"ok": False, "error": "Tabla CB vacía o no accesible"}
        if selected_ids:
            items = [it for it in items if it.get("id") in selected_ids]
        return ops.scan_slaves(client, items)

    return _check(await run_gateway_op(gateway_id, _wrap))


# =====================================================================
# Gestión de archivos
# =====================================================================

@router.get("/{gateway_id}/files")
async def gateway_dir(gateway_id: int, directory: str = Query("LOGS/"),
                      db: Session = Depends(get_db),
                      current_user: User = Depends(get_current_user)):
    _get_gateway(db, gateway_id)
    directory = directory if directory.endswith('/') else directory + '/'

    def _wrap(client):
        return ops.read_dir(client, directory)

    return _check(await run_gateway_op(gateway_id, _wrap))


@router.get("/{gateway_id}/file")
async def gateway_download_file(gateway_id: int, filename: str,
                                directory: str = Query("LOGS/"),
                                db: Session = Depends(get_db),
                                current_user: User = Depends(get_current_user)):
    _get_gateway(db, gateway_id)
    directory = directory if directory.endswith('/') else directory + '/'

    def _wrap(client):
        res = ops.read_file(client, directory, filename)
        if res.get("ok"):
            res["data_b64"] = base64.b64encode(res.get("data", b"")).decode('ascii')
            res.pop("data", None)
        return res

    return _check(await run_gateway_op(gateway_id, _wrap))


@router.post("/{gateway_id}/file")
async def gateway_upload_file(gateway_id: int, data: FileContentIn, filename: str,
                              directory: str = Query("LOGS/"),
                              db: Session = Depends(get_db),
                              current_user: User = Depends(require_admin)):
    _get_gateway(db, gateway_id)
    directory = directory if directory.endswith('/') else directory + '/'
    content = base64.b64decode(data.data)

    def _wrap(client):
        return ops.write_file(client, directory, filename, content)

    return _check(await run_gateway_op(gateway_id, _wrap))


# =====================================================================
# Helper para construir LoraConf desde flags
# =====================================================================

def _build_lora_from_flags(data: LoraConfWriteIn) -> dict:
    raw = 0
    if data.low_data_rate_opt is not None and data.low_data_rate_opt:
        raw |= 0x00000001
    if data.crc_dis is not None and data.crc_dis:
        raw |= 0x00000002
    if data.explicit_en is not None and data.explicit_en:
        raw |= 0x00000004
    if data.fix_pkln_en is not None and data.fix_pkln_en:
        raw |= 0x00000008
    if data.bandwidth is not None:
        raw |= (int(data.bandwidth) & 0x0F) << 4
    if data.coding_rate is not None:
        raw |= (int(data.coding_rate) & 0x0F) << 8
    if data.sfactor is not None:
        raw |= (int(data.sfactor) & 0x0F) << 12
    if data.tx_pwr is not None:
        raw |= (int(data.tx_pwr) & 0xFF) << 16
    return {
        "raw_bits": raw,
        "pream_length": int(data.pream_length or 0),
        "fixed_pk_length": int(data.fixed_pk_length or 0),
        "frq": int(data.frq or 0),
    }
