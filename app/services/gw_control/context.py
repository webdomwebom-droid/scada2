"""
Contexto de operación sobre un gateway: conecta la VPN de la planta,
ejecuta una operación Modbus síncrona y desconecta al finalizar.
Mismo patrón de conexión que usa el escaneo (scan_service_v2).
"""
import asyncio
import logging
import os

from app.core.database import SessionLocal
from app.models.gateway import Gateway
from app.services.vpn_service_v2 import vpn_service
from app.services.modbus_service_v2 import modbus_service
from app.services.gw_control.protocol import ModbusTcpClient
from app.services.gw_control import operations
from app.core.config import settings

logger = logging.getLogger(__name__)


def _gateway_routes(gateway: Gateway):
    routes = set()
    if gateway.ip:
        parts = gateway.ip.split('.')
        if len(parts) == 4:
            routes.add(f"{parts[0]}.{parts[1]}.{parts[2]}.0/24")
    return sorted(routes) if routes else None


async def _connect_plant_vpn(gateway: Gateway) -> bool:
    """Conecta la VPN de la planta del gateway. Devuelve True si ok."""
    plant = gateway.plant
    if plant is None:
        logger.error("Gateway sin planta asignada")
        return False
    vpn_file = os.path.join(plant.path, 'vpn.txt')
    if not os.path.exists(vpn_file):
        logger.error(f"VPN no encontrada: {vpn_file}")
        return False

    routes = _gateway_routes(gateway)
    # Reutilizar si ya hay una conexión a esta planta (respuesta instantánea)
    already = vpn_service.is_connected_to(plant.name)
    if not await vpn_service.connect_vpn(vpn_file, plant.name, routes):
        logger.error(f"VPN falló para {plant.name}")
        return False

    if hasattr(vpn_service, 'ssh_transport') and vpn_service.ssh_transport:
        modbus_service.set_ssh_transport(vpn_service.ssh_transport)
    else:
        modbus_service.set_ssh_transport(None)

    # esperar estabilización de rutas solo si la VPN es nueva (no reutilizada)
    if not already:
        for _ in range(2):
            await asyncio.sleep(1)
    return True


def _make_client(gateway: Gateway) -> ModbusTcpClient:
    port = int(settings.MODBUS_PORT)
    return ModbusTcpClient(ip=gateway.ip, port=port,
                           unit=int(settings.GW_CONTROL_UNIT_ID))


async def reconnect_gateway(gateway_id: int) -> dict:
    """
    Fuerza una reconexión completa: cierra la VPN actual, limpia la caché de la
    tabla CB y vuelve a conectar. Pensado para trabajo en campo, cuando el túnel
    se queda colgado y las operaciones dejan de responder.
    """
    db = SessionLocal()
    try:
        gateway = db.query(Gateway).filter(Gateway.id == gateway_id).first()
        if gateway is None:
            return {"ok": False, "error": "Gateway no encontrado"}
        plant_name = gateway.plant.name if gateway.plant else None
        try:
            await vpn_service.disconnect_vpn()
        except Exception as e:
            logger.warning(f"Error desconectando VPN antes de reconectar: {e}")
        operations._CB_CACHE.clear()
        modbus_service.set_ssh_transport(None)
        if not await _connect_plant_vpn(gateway):
            return {"ok": False, "error": "No se pudo reconectar la VPN de la planta",
                    "plant": plant_name}
        client = _make_client(gateway)
        try:
            status = await asyncio.to_thread(operations.read_gw_status, client)
        finally:
            try:
                client.close()
            except Exception:
                pass
        if status is None:
            return {"ok": False, "error": "VPN conectada pero el gateway no responde por Modbus",
                    "plant": plant_name, "vpn_connected": True}
        return {"ok": True, "plant": plant_name, "vpn_connected": True, "status": status}
    except Exception as e:
        logger.error(f"Error reconectando gateway {gateway_id}: {e}")
        return {"ok": False, "error": str(e)}
    finally:
        db.close()


async def run_gateway_op(gateway_id: int, op, *args, **kwargs):
    """
    Ejecuta una operación síncrona 'op(client, *args, **kwargs)' sobre el gateway.
    Gestiona conexión VPN (persistente: se reutiliza la VPN ya conectada para la
    misma planta -> respuestas instantaneas en operaciones repetidas) y cliente Modbus.
    Devuelve el resultado de la operación o un dict de error si no se pudo conectar.
    """
    db = SessionLocal()
    client = None
    connected = False
    try:
        gateway = db.query(Gateway).filter(Gateway.id == gateway_id).first()
        if gateway is None:
            return {"ok": False, "error": "Gateway no encontrado"}

        # Reutiliza la VPN si ya estaba conectada a esta planta (sin reconexion)
        if not await _connect_plant_vpn(gateway):
            return {"ok": False, "error": "No se pudo conectar la VPN de la planta", "demo":
                    vpn_service.demo_mode}

        connected = True

        client = _make_client(gateway)

        def _run():
            return op(client, *args, **kwargs)

        result = await asyncio.to_thread(_run)
        try:
            if isinstance(result, dict) and not result.get("ok", True):
                pass
        except Exception:
            pass
        return result

    except Exception as e:
        logger.error(f"Error en operación gateway {gateway_id}: {e}")
        return {"ok": False, "error": str(e)}
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
        db.close()
