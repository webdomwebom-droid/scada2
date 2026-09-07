import os
import asyncio
import subprocess
import logging
import time
import socket
import json
import sys
import shutil
import threading
import urllib.request
import zipfile
import tempfile
from typing import Dict, Optional, Tuple, List
from pathlib import Path
from app.core.config import settings

logger = logging.getLogger(__name__)

VPN_PID_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "logs", "vpn_pid.txt")

class VPNConfig:
    def __init__(self, vpn_file: str):
        self.vpn_file = vpn_file
        self.vpn_type = None
        self.vpn_subtype = None
        self.is_valid = False
        self.config_dict = {}
        self._parse()

    def _parse(self):
        try:
            with open(self.vpn_file, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith('#'):
                        continue
                    if '=' in line:
                        key, val = line.split('=', 1)
                        self.config_dict[key.strip().upper()] = val.strip()

            vpn_type = self.config_dict.get('VPN_TYPE', '').lower()
            if vpn_type == 'forticlient':
                self.vpn_type = 'forticlient'
            elif vpn_type in ('openconnect', 'anyconnect', 'ssl'):
                self.vpn_type = 'openconnect'
            elif vpn_type == 'openvpn':
                self.vpn_type = 'openvpn'
            elif vpn_type == 'ssh':
                self.vpn_type = 'ssh'

            if self.vpn_type == 'forticlient':
                self.is_valid = 'VPN_NAME' in self.config_dict and 'HOST' in self.config_dict
                if self.is_valid:
                    self.vpn_subtype = self.config_dict.get('SUBTYPE', 'ssl')
                    # Las VPN FortiClient SSL son candidatas a openconnect
                    if self.vpn_subtype == 'ssl':
                        self.openconnect_candidate = True
                    else:
                        self.openconnect_candidate = False
            elif self.vpn_type == 'openconnect':
                self.is_valid = 'HOST' in self.config_dict and 'USER' in self.config_dict
                self.openconnect_candidate = self.is_valid
            elif self.vpn_type == 'openvpn':
                self.is_valid = 'CONFIG' in self.config_dict and 'USER' in self.config_dict
            elif self.vpn_type == 'ssh':
                self.is_valid = 'SSH_HOST' in self.config_dict

            logger.info(f"VPN {vpn_type} parseado: valid={self.is_valid}")

        except Exception as e:
            logger.error(f"Error parseando VPN {self.vpn_file}: {e}")
            self.is_valid = False

    def get(self, key: str, default=None):
        return self.config_dict.get(key.upper(), default)


class VPNServiceV2:
    def __init__(self):
        self.current_vpn_process = None
        self.current_vpn_config = None
        self.current_plant_name = None
        self.temp_files = []
        self.vpn_connected = False
        self.connection_start_time = None
        self.ssh_client = None
        self.ssh_transport = None
        self._vpn_connection_name = None
        self._swan_conn_name_active = None

        # Estado de persistencia de la conexion VPN (reutilizacion entre operaciones)
        self._connected_plant = None
        self._connected_vpn_file = None
        self._connected_routes = None

        # Candado para serializar las operaciones VPN (evita que varias peticiones
        # simultáneas luchen por el adaptador TAP y se maten entre sí).
        self._vpn_lock = asyncio.Lock()

        self.openvpn_exe = self._find_openvpn()
        self.openfortivpn_exe = shutil.which('openfortivpn')
        self.openconnect_exe = self._find_openconnect()
        self.windows_vpn_available = self._check_windows_vpn_available()
        self.swanctl_exe = None if os.name == 'nt' else shutil.which('swanctl')

        self.demo_mode = settings.DEMO_MODE
        self.available_vpn_methods = self._detect_available_methods()

        logger.info(f"=== VPN SERVICE INITIALIZED ===")
        logger.info(f"OpenVPN: {self.openvpn_exe or 'NO ENCONTRADO'}")
        logger.info(f"OpenFortiVPN: {self.openfortivpn_exe or 'NO ENCONTRADO'}")
        logger.info(f"OpenConnect: {self.openconnect_exe or 'NO ENCONTRADO'}")
        logger.info(f"Windows VPN: {'DISPONIBLE' if self.windows_vpn_available else 'NO DISPONIBLE'}")
        logger.info(f"DEMO mode: {self.demo_mode}")
        logger.info(f"Métodos disponibles: {self.available_vpn_methods}")

    def _detect_available_methods(self) -> list:
        methods = []
        if self.openvpn_exe:
            methods.append('openvpn')
        if self.openfortivpn_exe:
            methods.append('openfortivpn')
        if self.openconnect_exe:
            methods.append('openconnect')
        if self.windows_vpn_available:
            methods.append('windows_vpn')
        if self.swanctl_exe:
            methods.append('strongswan')
        methods.append('ssh')  # SSH via paramiko siempre disponible (librería instalada)
        # 'demo' solo si está explícitamente activado: en producción una VPN que falla
        # debe dar error, no fingir que hay túnel y dejar los Modbus sin respuesta.
        if self.demo_mode:
            methods.append('demo')
        return methods

    def _check_windows_vpn_available(self) -> bool:
        try:
            result = subprocess.run(
                ['powershell', '-NoProfile', '-Command', 'Get-Command Add-VpnConnection -ErrorAction SilentlyContinue'],
                capture_output=True, text=True, timeout=5
            )
            return result.returncode == 0
        except:
            return False

    def _find_openvpn(self) -> Optional[str]:
        paths = [
            settings.VPN_EXECUTABLE_OPENVPN,
            r'C:\Program Files\OpenVPN\bin\openvpn.exe',
            r'C:\Program Files (x86)\OpenVPN\bin\openvpn.exe',
        ]
        for path in paths:
            if os.path.exists(path):
                logger.info(f"OpenVPN encontrado: {path}")
                return path
        # Buscar en PATH
        for p in os.environ.get('PATH', '').split(';'):
            candidate = os.path.join(p.strip(), 'openvpn.exe')
            if os.path.exists(candidate):
                logger.info(f"OpenVPN encontrado en PATH: {candidate}")
                return candidate
        return None

    def _is_admin(self) -> bool:
        try:
            import ctypes
            return ctypes.windll.shell32.IsUserAnAdmin() != 0
        except:
            return False

    def _find_openconnect(self) -> Optional[str]:
        """Busca el binario openconnect en PATH, ubicaciones comunes o WSL."""
        candidates = [shutil.which('openconnect')]
        globs = [
            r'C:\Program Files\OpenConnect\openconnect.exe',
            r'C:\Program Files (x86)\OpenConnect\openconnect.exe',
            r'C:\Program Files\openconnect\openconnect.exe',
        ]
        candidates.extend(globs)
        for c in candidates:
            if c and os.path.exists(c):
                logger.info(f"OpenConnect encontrado: {c}")
                return c
        # Intentar via WSL (openconnect suele instalarse con apt en WSL)
        try:
            check = subprocess.run(
                ['wsl', '--exec', 'which', 'openconnect'],
                capture_output=True, text=True, timeout=8
            )
            if check.returncode == 0 and check.stdout.strip():
                logger.info(f"OpenConnect disponible via WSL: {check.stdout.strip()}")
                return 'wsl'
        except Exception:
            pass
        return None

    async def check_ip_connectivity(self, test_ip: str, timeout: int = 5) -> Tuple[bool, float]:
        start = time.time()
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            result = sock.connect_ex((test_ip, 502))
            sock.close()
            response_time = (time.time() - start) * 1000
            return (result == 0, response_time)
        except:
            return (False, 0)

    def parse_vpn_config(self, vpn_file: str) -> VPNConfig:
        return VPNConfig(vpn_file)

    async def _run_elevated_openvpn(self, config: VPNConfig, plant_name: str) -> Optional[int]:
        config_file = config.get('CONFIG')
        user = config.get('USER')
        password = config.get('PASSWORD')

        if not config_file or not os.path.exists(config_file):
            logger.error(f"Archivo OVPN no encontrado: {config_file}")
            return None

        base_dir = os.path.dirname(config_file)
        launcher = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "scripts", "openvpn_elevated_launcher.ps1"
        )

        if not os.path.exists(launcher):
            logger.error(f"Launcher no encontrado: {launcher}")
            return None

        auth_file = ""
        if user and password:
            auth_file = os.path.join(base_dir, f'auth_{plant_name}.txt')
            with open(auth_file, 'w', encoding='utf-8') as f:
                f.write(user + '\n' + password + '\n')

        log_file = os.path.join(base_dir, f'openvpn_{plant_name}.log')
        pid_file = VPN_PID_FILE

        ps_cmd = (
            f'Start-Process powershell -Verb RunAs -ArgumentList '
            f'"-NoProfile -ExecutionPolicy Bypass -File \\"{launcher}\\" '
            f'-Action connect '
            f'-ConfigFile \\"{config_file}\\" '
            f'-AuthFile \\"{auth_file}\\" '
            f'-LogFile \\"{log_file}\\" '
            f'-PidFile \\"{pid_file}\\"" '
            f'-WindowStyle Hidden -Wait'
        )

        logger.info(f"Ejecutando OpenVPN elevado via PowerShell...")
        try:
            proc = await asyncio.create_subprocess_exec(
                'powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                '-Command', ps_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)

            output = stdout.decode('utf-8', errors='replace')
            for line in output.split('\n'):
                line = line.strip()
                if line:
                    logger.info(f"[Elevated] {line}")

            if 'CONNECTED' in output:
                if os.path.exists(pid_file):
                    with open(pid_file, 'r') as f:
                        pid_str = f.read().strip()
                        if pid_str and pid_str.isdigit():
                            return int(pid_str)
                return -1  # connected but no PID
            elif 'PROCESS_EXITED' in output:
                for line in output.split('\n'):
                    if 'PROCESS_EXITED' in line:
                        code = line.split(':')[-1].strip()
                        logger.error(f"OpenVPN elevado terminó con código: {code}")
                        return None
            elif 'TIMEOUT' in output:
                # Puede que se haya conectado pero no detectamos el mensaje
                if os.path.exists(pid_file):
                    with open(pid_file, 'r') as f:
                        pid_str = f.read().strip()
                        if pid_str and pid_str.isdigit():
                            logger.warning("OpenVPN timeout pero PID existe - asumiendo conexión")
                            return int(pid_str)
                logger.error("OpenVPN elevado timeout sin conexión")
                return None

            return None
        except asyncio.TimeoutError:
            logger.error("Timeout ejecutando OpenVPN elevado (60s)")
            return None
        except Exception as e:
            logger.error(f"Error ejecutando OpenVPN elevado: {e}")
            return None

    def _kill_previous_openvpn(self):
        """Mata cualquier proceso openvpn previo y limpia IP/rutas obsoletas
        para liberar el adaptador TAP/Wintun.

        Problema resuelto: si un adaptador TAP previo conserva la IP
        192.168.150.53, netsh no puede asignarla al nuevo TAP y OpenVPN aborta
        con 'Initialization Sequence Completed With Errors'. Limpiar la IP y las
        rutas obsoletas antes de conectar lo impide.
        """
        try:
            result = subprocess.run(
                ['taskkill', '/F', '/IM', 'openvpn.exe'],
                capture_output=True, text=True, timeout=8
            )
            if result.returncode == 0:
                logger.info("Procesos OpenVPN previos terminados (adaptador liberado)")
            time.sleep(1)
        except Exception as e:
            logger.debug(f"No se pudieron matar procesos OpenVPN previos: {e}")

        # Limpiar IP obtenida en sesiones previas (evita el fallo de netsh)
        try:
            out = subprocess.run(
                ['powershell', '-NoProfile', '-Command',
                 "Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | "
                 "Where-Object { $_.IPAddress -eq '192.168.150.53' } | "
                 "Remove-NetIPAddress -Confirm:$false -ErrorAction SilentlyContinue"],
                capture_output=True, text=True, timeout=10
            )
            logger.info("IP VPN previa (192.168.150.53) liberada")
        except Exception as e:
            logger.debug(f"No se pudo liberar IP VPN previa: {e}")

        # Limpiar rutas obsoletas hacia las redes de planta (las vuelve a crear OpenVPN)
        for prefix in ['10.110.0.0/20', '10.120.15.0/24', '10.130.15.0/24']:
            try:
                subprocess.run(
                    ['powershell', '-NoProfile', '-Command',
                     f"Get-NetRoute -DestinationPrefix '{prefix}' -ErrorAction SilentlyContinue | "
                     "Remove-NetRoute -Confirm:$false -ErrorAction SilentlyContinue"],
                    capture_output=True, text=True, timeout=10
                )
            except Exception as e:
                logger.debug(f"No se pudo limpiar la ruta {prefix}: {e}")

    async def connect_openvpn(self, config: VPNConfig, plant_name: str) -> bool:
        try:
            if not self.openvpn_exe:
                logger.warning("OpenVPN no está instalado")
                return False

            config_file = config.get('CONFIG')
            user = config.get('USER')
            password = config.get('PASSWORD')
            key_password = config.get('KEY_PASSWORD', '')

            if not config_file or not os.path.exists(config_file):
                logger.error(f"Archivo OVPN no encontrado: {config_file}")
                return False

            # Matar cualquier conexion previa que pueda bloquear el adaptador TAP
            self._kill_previous_openvpn()

            logger.info(f"Conectando OpenVPN: {config_file}")
            base_dir = os.path.dirname(config_file)
            self._cleanup_temp_files()

            cmd = [self.openvpn_exe, '--config', config_file]

            # Usar TAP explícitamente: el backend corre como admin (suficiente para TAP
            # via el servicio interactivo), y _kill_previous_openvpn() ya garantiza que
            # no haya procesos que ocupen todos los adaptadores tap-windows6.
            # (Wintun requiere SYSTEM y falla aquí: "Wintun requires SYSTEM privileges")
            #
            # --ip-win32 netsh: asigna la IP de la VPN con netsh en vez de por DHCP.
            # El DHCP de Windows falla aquí ("Initialization Sequence Completed With
            # Errors, dhcpclientserv"), dejando la interfaz sin IP y el túnel inútil.
            cmd += ['--windows-driver', 'tap-windows6', '--route-metric', '1',
                    '--ip-win32', 'netsh']

            if user and password:
                auth_file = os.path.join(base_dir, f'auth_{plant_name}.txt')
                with open(auth_file, 'w', encoding='utf-8') as f:
                    f.write(user + '\n' + password + '\n')
                cmd.extend(['--auth-user-pass', auth_file])
                self.temp_files.append(auth_file)

            if key_password:
                askpass_file = os.path.join(base_dir, f'keypass_{plant_name}.txt')
                with open(askpass_file, 'w', encoding='utf-8') as f:
                    f.write(key_password + '\n')
                cmd.extend(['--askpass', askpass_file])
                self.temp_files.append(askpass_file)

            self.cleanup_old_logs(base_dir, plant_name)
            log_file = os.path.join(base_dir, f'openvpn_{plant_name}_{int(time.time())}.log')
            cmd.extend(['--log', log_file, '--verb', '3'])

            # data-ciphers necesario para que OpenVPN negocie AES-128-CBC
            # (el .ovpn usa cipher AES-128-CBC, que DCO no soporta;
            #  con --windows-driver wintun ya no depende del adaptador TAP)
            cmd.extend(['--data-ciphers', 'AES-256-GCM:AES-128-GCM:AES-128-CBC'])

            logger.info(f"Ejecutando OpenVPN...")

            try:
                self.current_vpn_process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    cwd=base_dir,
                    text=True,
                    encoding='utf-8',
                    errors='replace',
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
                )
            except Exception as e:
                logger.warning(f"No se pudo iniciar OpenVPN: {e}")
                return False

            self.current_plant_name = plant_name
            pid = self.current_vpn_process.pid
            logger.info(f"OpenVPN PID: {pid}")

            start_time = time.time()
            connected = False
            CONNECT_TIMEOUT = 45
            VERIFY_TIMEOUT = 15

            # Esperar hasta CONNECT_TIMEOUT a que OpenVPN termine o se inicialice
            # (OpenVPN en Windows usa TAP/InteractiveService;
            #  el proceso CLI termina tras inicializar la conexion)
            log_checked_once = False
            while time.time() - start_time < CONNECT_TIMEOUT:
                proc_done = self.current_vpn_process.poll() is not None

                # Leer log para detectar inicializacion
                try:
                    if os.path.exists(log_file):
                        with open(log_file, 'r', encoding='utf-8', errors='replace') as f:
                            log_content = f.read()
                            if 'Initialization Sequence Completed' in log_content:
                                connected = True
                                logger.info("OpenVPN inicializado (detectado en log)")
                                break
                except:
                    pass

                if proc_done:
                    ret = self.current_vpn_process.returncode
                    logger.info(f"OpenVPN termino (exit={ret}) - verificando log...")
                    # Leer stdout restante
                    try:
                        rest = self.current_vpn_process.stdout.read()
                        if rest:
                            for line in rest.split('\n')[-5:]:
                                if line.strip():
                                    logger.info(f"[OpenVPN stdout] {line.strip()}")
                    except:
                        pass
                    break

                await asyncio.sleep(1)

            # Revisar log final por si aparecio el mensaje
            if not connected and os.path.exists(log_file):
                try:
                    with open(log_file, 'r', encoding='utf-8', errors='replace') as f:
                        if 'Initialization Sequence Completed' in f.read():
                            connected = True
                            logger.info("'Initialization Sequence Completed' confirmado en log")
                except:
                    pass

            if connected:
                self.vpn_connected = True
                self.connection_start_time = time.time()
                logger.info(f"OpenVPN conectado para {plant_name}")
                return True

            # Timeout sin senyal - verificar conectividad TCP real
            if self.current_vpn_process and self.current_vpn_process.poll() is not None:
                logger.warning(f"OpenVPN termino (exit={self.current_vpn_process.returncode}) sin senyal de inicializacion")

            logger.warning(f"Verificando conectividad TCP a gateways (timeout restante: {VERIFY_TIMEOUT}s)...")
            verify_start = time.time()
            while time.time() - verify_start < VERIFY_TIMEOUT:
                for test_ip in ['10.110.1.21', '10.110.2.21', '10.110.3.21', '10.110.4.21', '10.110.5.21']:
                    ok, ms = await self.check_ip_connectivity(test_ip, timeout=3)
                    if ok:
                        logger.info(f"Gateway {test_ip}:502 responde ({ms:.0f}ms) - VPN operativa")
                        self.vpn_connected = True
                        self.connection_start_time = time.time()
                        return True
                await asyncio.sleep(1)

            logger.error(f"Verificacion TCP fallida tras {VERIFY_TIMEOUT}s - VPN no operativa")
            if self.current_vpn_process and self.current_vpn_process.poll() is None:
                self.current_vpn_process.terminate()
            return False

        except Exception as e:
            logger.error(f"Error conectando OpenVPN: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return False

    async def _ensure_openfortivpn(self) -> Optional[str]:
        """Find openfortivpn or download portable binary"""
        exe = shutil.which('openfortivpn')
        if exe:
            return exe

        # Try bundled copy
        bundled = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            'bin', 'openfortivpn.exe'
        )
        if os.path.exists(bundled):
            logger.info(f"openfortivpn encontrado (bundled): {bundled}")
            return bundled

        # Try to download portable binary
        try:
            url = (
                "https://github.com/adrienverge/openfortivpn/releases/download/v1.22.0/"
                "openfortivpn-win64.zip"
            )
            download_dir = os.path.join(
                os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                'bin'
            )
            os.makedirs(download_dir, exist_ok=True)
            zip_path = os.path.join(download_dir, 'openfortivpn-win64.zip')

            logger.info(f"Descargando openfortivpn desde {url}...")
            try:
                urllib.request.urlretrieve(url, zip_path)
                with zipfile.ZipFile(zip_path, 'r') as zf:
                    zf.extractall(download_dir)
                os.remove(zip_path)
                exe_path = os.path.join(download_dir, 'openfortivpn.exe')
                if os.path.exists(exe_path):
                    logger.info(f"openfortivpn descargado: {exe_path}")
                    return exe_path
            except Exception as e:
                logger.warning(f"Error descargando openfortivpn: {e}")
                if os.path.exists(zip_path):
                    os.remove(zip_path)
        except Exception as e:
            logger.warning(f"No se pudo obtener openfortivpn: {e}")

        return None

    async def _connect_ipsec_windows(self, config: VPNConfig, plant_name: str, routes: List[str] = None) -> bool:
        """Connect IPSec VPN using Windows built-in VPN (L2TP/IPsec PSK, fallback IKEv2)"""
        try:
            vpn_name = config.get('VPN_NAME', plant_name)
            host = config.get('HOST')
            psk = config.get('PSK')
            user = config.get('USER')
            password = config.get('PRIVATE_KEY') or config.get('PASSWORD')

            if not host:
                return False

            logger.info(f"Conectando IPSec (Windows VPN): {vpn_name} -> {host}")

            base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            script_path = os.path.join(base_dir, 'scripts', 'vpn_connect_windows.ps1')
            if not os.path.exists(script_path):
                logger.error(f"Script no encontrado: {script_path}")
                return False

            async def run_script(action: str, tunnel_type: str = 'L2tp') -> Tuple[bool, str]:
                """
                Ejecuta el script VPN directamente (sin elevación para Add-VpnConnection y rasdial).
                Para las rutas, usamos un segundo comando elevado si es necesario.
                """
                ps_args = [
                    'powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                    '-File', script_path,
                    '-Action', action,
                    '-Name', vpn_name,
                    '-ServerAddress', host,
                    '-TunnelType', tunnel_type,
                ]
                if action in ('connect', 'connect_and_cleanup'):
                    if psk:
                        ps_args.extend(['-PresharedKey', psk])
                    if user:
                        ps_args.extend(['-Username', user])
                    if password:
                        ps_args.extend(['-Password', password])
                    if routes:
                        for r in routes:
                            ps_args.extend(['-Routes', r])

                logger.info(f"Ejecutando script VPN...")
                loop = asyncio.get_event_loop()
                try:
                    result = await asyncio.wait_for(
                        loop.run_in_executor(
                            None,
                            lambda: subprocess.run(
                                ps_args,
                                capture_output=True, text=True, timeout=60
                            )
                        ),
                        timeout=70
                    )
                    output = (result.stdout or "") + (result.stderr or "")
                    connected = 'STATUS:CONNECTED' in output
                    
                    # Si la VPN se conectó pero no tenemos rutas, intentar añadirlas elevado
                    if connected and routes:
                        await self._add_routes_elevated(vpn_name, routes, host)
                    
                    return connected, output
                except asyncio.TimeoutError:
                    return False, "Timeout"
                except subprocess.TimeoutExpired:
                    return False, "Timeout"

            # Try L2TP/IPsec with PSK first
            logger.info(f"Intentando L2TP/IPsec con PSK...")
            connected, output = await run_script('connect_and_cleanup', 'L2tp')
            logger.info(f"L2TP resultado: connected={connected}")
            for line in output.split('\n'):
                if line.strip():
                    logger.info(f"  [L2TP] {line.strip()}")

            if connected:
                self.vpn_connected = True
                self.connection_start_time = time.time()
                self.current_plant_name = plant_name
                self._vpn_connection_name = vpn_name
                return True

            # Fallback to IKEv2
            logger.info(f"L2TP falló, intentando IKEv2...")
            connected, output = await run_script('connect_and_cleanup', 'Ikev2')
            logger.info(f"IKEv2 resultado: connected={connected}")
            for line in output.split('\n'):
                if line.strip():
                    logger.info(f"  [IKEv2] {line.strip()}")

            if connected:
                self.vpn_connected = True
                self.connection_start_time = time.time()
                self.current_plant_name = plant_name
                self._vpn_connection_name = vpn_name
                return True

            logger.error(f"No se pudo conectar VPN IPSec (L2TP ni IKEv2)")
            return False

        except Exception as e:
            logger.error(f"Error en IPSec Windows VPN: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return False

    async def _add_routes_elevated(self, vpn_name: str, routes: List[str], server_address: str):
        """
        Añade rutas VPN de forma elevada usando un script .bat intermedio que escribe a archivo.
        """
        try:
            base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            bat_path = os.path.join(base_dir, 'scripts', 'run_vpn_elevated.bat')
            output_file = os.path.join(base_dir, 'logs', f'vpn_routes_{vpn_name}.out')
            
            # Construir comando para el .bat: Action=add_routes, Name, Routes, ServerAddress
            routes_str = ' '.join([f'-Routes "{r}"' for r in routes])
            bat_cmd = (
                f'Start-Process cmd.exe -Verb RunAs -ArgumentList '
                f'"/c \"{bat_path}\" connect_and_cleanup '
                f'-Name \"{vpn_name}\" '
                f'-ServerAddress \"{server_address}\" '
                f'{routes_str} '
                f'-TunnelType L2tp > \"{output_file}\" 2>&1" '
                f'-WindowStyle Hidden -Wait'
            )
            
            logger.info(f"Añadiendo rutas VPN elevado para {vpn_name}...")
            proc = await asyncio.create_subprocess_exec(
                'powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                '-Command', bat_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            await asyncio.wait_for(proc.communicate(), timeout=60)
            
            # Leer el archivo de salida
            if os.path.exists(output_file):
                with open(output_file, 'r', encoding='utf-8', errors='replace') as f:
                    output = f.read()
                    for line in output.split('\n'):
                        if line.strip():
                            logger.info(f"  [Routes] {line.strip()}")
                try:
                    os.remove(output_file)
                except:
                    pass
                    
        except Exception as e:
            logger.warning(f"Error añadiendo rutas elevadas: {e}")

    async def _connect_ssl_openfortivpn(self, config: VPNConfig, plant_name: str) -> bool:
        """Connect SSL VPN using openfortivpn (open-source FortiClient SSL client)"""
        try:
            host = config.get('HOST')
            port = int(config.get('PORT', '10443'))
            user = config.get('USER')
            password = config.get('PASSWORD')
            vpn_name = config.get('VPN_NAME', plant_name)

            if not host or not user:
                logger.error("HOST y USER requeridos para SSL VPN")
                return False

            openfortivpn_exe = await self._ensure_openfortivpn()
            if not openfortivpn_exe:
                logger.warning(f"openfortivpn no disponible para {vpn_name}")
                return False

            logger.info(f"Conectando SSL VPN con openfortivpn: {vpn_name} -> {host}:{port}")

            # Build auth file
            auth_dir = os.path.join(
                os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                'temp'
            )
            os.makedirs(auth_dir, exist_ok=True)
            auth_file = os.path.join(auth_dir, f'ofvpn_{plant_name}.auth')
            log_file = os.path.join(auth_dir, f'ofvpn_{plant_name}.log')

            with open(auth_file, 'w') as f:
                f.write(f"{user}\n{password or ''}\n")

            # Build openfortivpn args
            ofvpn_args = [
                openfortivpn_exe,
                '--host', host,
                '--port', str(port),
                '--username', user,
                '--password-on-stdin',
                '--log-level', 'debug',
                '--realm', config.get('REALM', ''),
                '--trusted-cert', config.get('TRUSTED_CERT', ''),
                '--no-cert-check' if config.get('ALLOW_INSECURE', '').lower() in ('1', 'true', 'yes') else '',
            ]
            ofvpn_args = [a for a in ofvpn_args if a]  # remove empties

            proc = await asyncio.create_subprocess_exec(
                *ofvpn_args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            if password:
                proc.stdin.write(f"{password}\n".encode())
                await proc.stdin.drain()

            # read stderr for tunnel URL (connection confirmation)
            async def read_output():
                lines = []
                try:
                    while True:
                        line = await asyncio.wait_for(proc.stdout.readline(), timeout=15)
                        if not line:
                            break
                        decoded = line.decode('utf-8', errors='replace').strip()
                        lines.append(decoded)
                        if 'tunnel' in decoded.lower() and ('established' in decoded.lower() or 'up' in decoded.lower()):
                            break
                        if 'connected' in decoded.lower():
                            break
                        if 'error' in decoded.lower():
                            logger.warning(f"openfortivpn: {decoded}")
                except asyncio.TimeoutError:
                    pass
                return lines

            output_lines = await read_output()
            for l in output_lines:
                logger.info(f"[openfortivpn] {l}")

            if proc.returncode is None:
                # Process still running = connected (daemon)
                logger.info(f"openfortivpn conectado (daemon): {host}:{port}")
                self.vpn_connected = True
                self.connection_start_time = time.time()
                self.current_plant_name = plant_name
                self.current_vpn_process = proc
                self.temp_files.append(auth_file)
                return True

            # Check exit code
            if proc.returncode == 0:
                logger.info(f"openfortivpn conectado: {host}:{port}")
                self.vpn_connected = True
                self.connection_start_time = time.time()
                self.current_plant_name = plant_name
                self.current_vpn_process = proc
                self.temp_files.append(auth_file)
                return True

            logger.warning(f"openfortivpn terminó (code={proc.returncode})")
            if any('connected' in l.lower() or 'tunnel' in l.lower() for l in output_lines):
                self.vpn_connected = True
                self.connection_start_time = time.time()
                self.current_plant_name = plant_name
                return True

            return False

        except Exception as e:
            logger.error(f"Error en SSL VPN: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return False

    async def connect_forticlient(self, config: VPNConfig, plant_name: str, routes: List[str] = None) -> bool:
        """
        Connect FortiClient-style VPN without requiring FortiClient.
        - IPSec subtype: uses Windows built-in VPN (L2TP/IPsec + PSK)
        - SSL subtype: uses openfortivpn (open-source client)
        """
        try:
            vpn_name = config.get('VPN_NAME')
            subtype = config.get('SUBTYPE', 'ssl').lower()

            if not vpn_name or not config.get('HOST'):
                logger.error("VPN_NAME y HOST requeridos")
                return False

            logger.info(f"Conectando FortiClient VPN: {vpn_name} (tipo: {subtype})")

            if subtype == 'ipsec':
                if os.name == 'nt':
                    return await self._connect_ipsec_windows(config, plant_name, routes)
                if self.swanctl_exe:
                    return await self._connect_ipsec_strongswan(config, plant_name, routes)
                logger.error(
                    "IPsec requiere strongSwan (swanctl) en Linux o Windows VPN nativa; "
                    "instala strongswan (apt install strongswan strongswan-swanctl)")
                return False
            else:
                return await self._connect_ssl_openfortivpn(config, plant_name)

        except Exception as e:
            logger.error(f"Error conectando FortiClient VPN: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return False

    # =================================================================
    # IPsec en Linux via strongSwan (swanctl)
    # =================================================================

    @staticmethod
    def _swan_conn_name(plant_name: str) -> str:
        safe = ''.join(c if c.isalnum() else '-' for c in (plant_name or 'vpn')).strip('-').lower()
        return f"scada-{safe or 'vpn'}"

    @staticmethod
    def _swan_proposal(proposal: str, dh_group: str) -> str:
        """'AES256-SHA512' + grupo DH 14 -> 'aes256-sha512-modp2048'."""
        dh_map = {'1': 'modp768', '2': 'modp1024', '5': 'modp1536', '14': 'modp2048',
                  '15': 'modp3072', '16': 'modp4096', '19': 'ecp256', '20': 'ecp384',
                  '21': 'ecp521'}
        parts = [p.strip().lower() for p in (proposal or 'AES256-SHA256').split('-') if p.strip()]
        dh = dh_map.get(str(dh_group or '14').strip(), 'modp2048')
        return '-'.join(parts + [dh])

    def _build_swanctl_conf(self, config: VPNConfig, conn_name: str,
                            routes: List[str] = None) -> str:
        """Genera la configuración swanctl equivalente al perfil IPsec de FortiClient."""
        host = config.get('HOST')
        user = config.get('USER', '')
        psk = config.get('PSK', '')
        secret = config.get('PRIVATE_KEY') or config.get('PASSWORD') or ''
        local_id = config.get('LOCAL_ID') or user
        version = '1' if str(config.get('IKE_VERSION', 'v2')).lower() in ('1', 'v1') else '2'
        ike_prop = self._swan_proposal(config.get('PHASE1_PROPOSAL'), config.get('PHASE1_DH_GROUP'))
        esp_prop = self._swan_proposal(config.get('PHASE2_PROPOSAL'), config.get('PHASE2_DH_GROUP'))
        remote_ts = ', '.join(routes) if routes else '0.0.0.0/0'

        if version == '2':
            local_blocks = (
                f"      local-psk {{\n"
                f"        auth = psk\n"
                f"        id = {local_id}\n"
                f"      }}\n"
                f"      local-eap {{\n"
                f"        auth = eap-mschapv2\n"
                f"        eap_id = {user}\n"
                f"      }}\n"
            )
            extra = ""
        else:
            local_blocks = (
                f"      local-psk {{\n"
                f"        auth = psk\n"
                f"        id = {local_id}\n"
                f"      }}\n"
                f"      local-xauth {{\n"
                f"        auth = xauth\n"
                f"        xauth_id = {user}\n"
                f"      }}\n"
            )
            extra = "    aggressive = yes\n"

        return (
            f"connections {{\n"
            f"  {conn_name} {{\n"
            f"    version = {version}\n"
            f"    remote_addrs = {host}\n"
            f"    proposals = {ike_prop}\n"
            f"    vips = 0.0.0.0\n"
            f"    dpd_delay = 30s\n"
            f"{extra}"
            f"    local {{\n"
            f"{local_blocks}"
            f"    }}\n"
            f"    remote {{\n"
            f"      auth = psk\n"
            f"      id = {host}\n"
            f"    }}\n"
            f"    children {{\n"
            f"      {conn_name} {{\n"
            f"        remote_ts = {remote_ts}\n"
            f"        esp_proposals = {esp_prop}\n"
            f"        start_action = none\n"
            f"        dpd_action = restart\n"
            f"      }}\n"
            f"    }}\n"
            f"  }}\n"
            f"}}\n"
            f"secrets {{\n"
            f"  ike-{conn_name} {{\n"
            f"    id = {host}\n"
            f"    secret = \"{psk}\"\n"
            f"  }}\n"
            f"  eap-{conn_name} {{\n"
            f"    id = {user}\n"
            f"    secret = \"{secret}\"\n"
            f"  }}\n"
            f"  xauth-{conn_name} {{\n"
            f"    id = {user}\n"
            f"    secret = \"{secret}\"\n"
            f"  }}\n"
            f"}}\n"
        )

    async def _run_privileged(self, args: List[str], timeout: int = 60) -> Tuple[int, str]:
        """Ejecuta un comando como root (directo si ya lo somos, si no via sudo -n)."""
        if os.geteuid() != 0:
            if not shutil.which('sudo'):
                return 1, 'se requieren privilegios de root y no hay sudo disponible'
            args = ['sudo', '-n'] + args
        try:
            proc = await asyncio.create_subprocess_exec(
                *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            return proc.returncode, (out or b'').decode('utf-8', errors='replace')
        except asyncio.TimeoutError:
            return 1, f'timeout ejecutando {args[0]}'
        except Exception as e:
            return 1, str(e)

    async def _connect_ipsec_strongswan(self, config: VPNConfig, plant_name: str,
                                        routes: List[str] = None) -> bool:
        """IPsec (FortiGate dialup) en Linux usando strongSwan/swanctl."""
        conn_name = self._swan_conn_name(plant_name)
        conf_dir = '/etc/swanctl/conf.d'
        conf_path = os.path.join(conf_dir, f'{conn_name}.conf')
        content = self._build_swanctl_conf(config, conn_name, routes)

        tmp = tempfile.NamedTemporaryFile('w', suffix='.conf', delete=False)
        try:
            tmp.write(content)
            tmp.close()
            os.chmod(tmp.name, 0o600)
            rc, out = await self._run_privileged(['mkdir', '-p', conf_dir], timeout=15)
            if rc != 0:
                logger.error(f"strongSwan: no se pudo preparar {conf_dir}: {out.strip()}")
                return False
            rc, out = await self._run_privileged(['install', '-m', '600', tmp.name, conf_path],
                                                 timeout=15)
            if rc != 0:
                logger.error(f"strongSwan: no se pudo instalar la configuración: {out.strip()}")
                return False
        finally:
            try:
                os.remove(tmp.name)
            except OSError:
                pass

        rc, out = await self._run_privileged([self.swanctl_exe, '--load-all'], timeout=30)
        if rc != 0:
            logger.error(f"strongSwan: fallo cargando configuración: {out.strip()}")
            return False

        rc, out = await self._run_privileged(
            [self.swanctl_exe, '--initiate', '--child', conn_name, '--timeout', '60'], timeout=90)
        if rc != 0:
            logger.error(f"strongSwan: no se pudo establecer el túnel {conn_name}: {out.strip()}")
            await self._run_privileged([self.swanctl_exe, '--terminate', '--ike', conn_name],
                                       timeout=30)
            return False

        self.vpn_connected = True
        self.connection_start_time = time.time()
        self.current_plant_name = plant_name
        self.current_vpn_config = config
        self._swan_conn_name_active = conn_name
        logger.info(f"IPsec strongSwan conectado: {plant_name} ({conn_name})")
        return True

    async def _disconnect_strongswan(self):
        conn_name = getattr(self, '_swan_conn_name_active', None)
        if not conn_name or not self.swanctl_exe:
            return
        await self._run_privileged([self.swanctl_exe, '--terminate', '--ike', conn_name],
                                   timeout=30)
        await self._run_privileged(['rm', '-f', f'/etc/swanctl/conf.d/{conn_name}.conf'],
                                   timeout=15)
        await self._run_privileged([self.swanctl_exe, '--load-all'], timeout=30)
        self._swan_conn_name_active = None

    async def connect_ssh(self, config: VPNConfig, plant_name: str, gateways: List[str] = None) -> bool:
        """
        SSH tunnel via paramiko.
        Crea un tunel SSH persistente y forwards de puertos para gateways.
        """
        try:
            host = config.get('SSH_HOST')
            port = int(config.get('SSH_PORT', '22'))
            username = config.get('SSH_USER')
            password = config.get('SSH_PASSWORD')
            key_path = config.get('SSH_KEY_PATH')

            if not host or not username:
                logger.error("SSH_HOST y SSH_USER requeridos")
                return False

            logger.info(f"Conectando SSH tunnel a {username}@{host}:{port}")

            import paramiko
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

            connect_kwargs = {
                'hostname': host,
                'port': port,
                'username': username,
                'timeout': 15,
                'allow_agent': False,
                'look_for_keys': False,
            }
            if password:
                connect_kwargs['password'] = password
            if key_path and os.path.exists(key_path):
                connect_kwargs['key_filename'] = key_path

            ssh.connect(**connect_kwargs)
            logger.info(f"SSH conectado a {host}")

            transport = ssh.get_transport()
            if not transport or not transport.is_active():
                logger.error("Transporte SSH no activo")
                ssh.close()
                return False

            # Crear port forwards para cada gateway remoto
            # Cada gateway se expone como localhost:<base_port + idx>
            self.ssh_forward_threads = []
            self.ssh_forward_ports = {}
            if gateways:
                base_port = 15000
                for i, gw_ip in enumerate(gateways):
                    local_port = base_port + i
                    self._start_ssh_forward(transport, local_port, gw_ip, 502)
                    self.ssh_forward_ports[gw_ip] = local_port
                    logger.info(f"Forward SSH: localhost:{local_port} -> {gw_ip}:502")

            self.ssh_client = ssh
            self.ssh_transport = transport
            self.vpn_connected = True
            self.connection_start_time = time.time()
            self.current_plant_name = plant_name

            logger.info(f"SSH tunnel establecido para {plant_name}")
            return True

        except Exception as e:
            logger.error(f"Error conectando SSH: {e}")
            return False

    def _start_ssh_forward(self, transport, local_port: int, remote_host: str, remote_port: int):
        """Inicia un forwarder de puerto en background usando el transporte SSH"""
        import threading
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(('127.0.0.1', local_port))
        server.listen(10)
        server.settimeout(None)

        def handler():
            while self.vpn_connected:
                try:
                    client = server.accept()[0]
                    client.settimeout(30)
                    channel = transport.open_channel(
                        'direct-tcpip',
                        (remote_host, remote_port),
                        ('', 0)
                    )
                    if channel is None:
                        client.close()
                        continue
                    channel.settimeout(30)
                    self._pipe_two_ways(client, channel)
                except (socket.timeout, OSError):
                    continue
                except Exception as e:
                    if self.vpn_connected:
                        logger.debug(f"Forward error: {e}")
                    break
            server.close()

        thread = threading.Thread(target=handler, daemon=True)
        thread.start()
        self.ssh_forward_threads.append(thread)

    def _pipe_two_ways(self, sock1, sock2):
        """Conecta bidireccionalmente dos sockets en segundo plano"""
        import threading

        def pipe(src, dst):
            try:
                while self.vpn_connected:
                    data = src.recv(4096)
                    if not data:
                        break
                    dst.send(data)
            except:
                pass
            finally:
                try: src.close()
                except: pass
                try: dst.close()
                except: pass

        t1 = threading.Thread(target=pipe, args=(sock1, sock2), daemon=True)
        t2 = threading.Thread(target=pipe, args=(sock2, sock1), daemon=True)
        t1.start()
        t2.start()

    def get_ssh_forward_port(self, gateway_ip: str) -> Optional[int]:
        """Retorna el puerto local forwardeado para una IP de gateway"""
        if hasattr(self, 'ssh_forward_ports'):
            return self.ssh_forward_ports.get(gateway_ip)
        return None

    def open_ssh_channel(self, target_host: str, target_port: int = 502) -> Optional[socket.socket]:
        """
        Abre un canal direct-tcpip a través del tunel SSH.
        """
        if not hasattr(self, 'ssh_transport') or not self.ssh_transport or not self.ssh_transport.is_active():
            logger.error("SSH no conectado")
            return None
        try:
            channel = self.ssh_transport.open_channel(
                'direct-tcpip',
                (target_host, target_port),
                ('', 0)
            )
            return channel
        except Exception as e:
            logger.error(f"Error abriendo canal SSH a {target_host}:{target_port}: {e}")
            return None

    async def connect_openconnect(self, config: VPNConfig, plant_name: str,
                                  routes: List[str] = None) -> bool:
        """
        Conecta mediante OpenConnect (SSL VPN: AnyConnect / FortiGate SSL / GlobalProtect).
        Compatible con servidores OpenVPN solo en modo legacy (protocolo OpenConnect),
        pero NO con OpenVPN clasico IPSEC/TLS-classic ni con IPsec.
        """
        try:
            host = config.get('HOST')
            port = int(config.get('PORT', '10443'))
            user = config.get('USER') or config.get('VPN_NAME')
            password = config.get('PASSWORD') or config.get('PRIVATE_KEY')
            protocol = config.get('PROTOCOL', 'anyconnect').lower()
            insecure = config.get('ALLOW_INSECURE', '').lower() in ('1', 'true', 'yes')

            if not host or not user:
                logger.error("HOST y USER requeridos para OpenConnect")
                return False

            # Para OpenVPN clasico el binary nativo openvpn es el correcto, no openconnect
            if not self.openconnect_exe or self.openconnect_exe == 'wsl':
                logger.warning(f"openconnect no disponible para {plant_name}")
                return False

            logger.info(f"Conectando SSL VPN con openconnect: {host}:{port} (proto={protocol})")

            args = [self.openconnect_exe, '--background', '--non-inter', '--user', user]
            if password:
                args += ['--passwd-on-stdin']
            if insecure:
                args += ['--no-cert-check']
            args.append('--protocol={}'.format(protocol))
            args.append(f'{host}:{port}')

            logger.info(f"Ejecutando openconnect: {' '.join(args)}")

            proc = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            if password:
                proc.stdin.write(f"{password}\n".encode())
                await proc.stdin.drain()

            # Leer salida hasta que el tunel este activo o error
            start = time.time()
            connected = False
            output = []
            try:
                while time.time() - start < 30:
                    try:
                        line = await asyncio.wait_for(proc.stdout.readline(), timeout=5)
                    except asyncio.TimeoutError:
                        if proc.returncode is not None:
                            break
                        continue
                    if not line:
                        break
                    decoded = line.decode('utf-8', errors='replace').strip()
                    output.append(decoded)
                    logger.info(f"[openconnect] {decoded}")
                    low = decoded.lower()
                    if 'connected' in low or 'established' in low or 'tunnel is up' in low:
                        connected = True
                        break
                    if 'error' in low or 'failed' in low:
                        logger.warning(f"openconnect: {decoded}")
            except Exception as e:
                logger.warning(f"Error leyendo openconnect: {e}")

            if connected and proc.returncode is None:
                self.current_vpn_process = proc
                self.vpn_connected = True
                self.connection_start_time = time.time()
                self.current_plant_name = plant_name
                logger.info(f"OpenConnect conectado para {plant_name}")
                return True

            # Si el proceso termino, reportar
            logger.warning(f"openconnect terminó (code={proc.returncode})")
            return False

        except Exception as e:
            logger.error(f"Error en OpenConnect: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return False

    async def connect_vpn(self, vpn_file: str, plant_name: str, routes: List[str] = None) -> bool:
        # Serializar: solo una conexión VPN a la vez (evita luchar por el TAP)
        async with self._vpn_lock:
            return await self._connect_vpn_locked(vpn_file, plant_name, routes)

    async def _connect_vpn_locked(self, vpn_file: str, plant_name: str, routes: List[str] = None) -> bool:
        try:
            # Reutilizar una VPN ya conectada a la misma planta (respuesta instantanea)
            if (self.vpn_connected and self._connected_plant == plant_name
                    and self._connected_vpn_file == vpn_file):
                logger.info(f"Reutilizando VPN ya conectada para {plant_name}")
                return True
            # Si hay una VPN activa de otra planta, desconectarla antes (esta operación usa la suya)
            if self.vpn_connected and self._connected_plant != plant_name:
                logger.info(f"Cambiando VPN: {self._connected_plant} -> {plant_name}")
                await self.disconnect_vpn()

            config = self.parse_vpn_config(vpn_file)

            if not config.is_valid:
                logger.error(f"Configuración VPN inválida: {vpn_file}")
                if 'demo' in self.available_vpn_methods:
                    logger.info(f"Fallback a DEMO para {plant_name}")
                    return await self.connect_demo(plant_name)
                return False

            logger.info(f"Conectando VPN {plant_name} (tipo: {config.vpn_type})")

            attempt_order = []
            if config.vpn_type:
                attempt_order.append(config.vpn_type)
            for method in self.available_vpn_methods:
                if method not in attempt_order and method != 'demo':
                    attempt_order.append(method)
            attempt_order.append('demo')

            for attempt_method in attempt_order:
                logger.info(f"Intento {attempt_order.index(attempt_method)+1}/{len(attempt_order)}: {attempt_method}")

                try:
                    if attempt_method == 'forticlient':
                        success = await self.connect_forticlient(config, plant_name, routes)
                    elif attempt_method == 'openconnect':
                        success = await self.connect_openconnect(config, plant_name)
                    elif attempt_method == 'windows_vpn':
                        success = await self._connect_ipsec_windows(config, plant_name, routes)
                    elif attempt_method == 'openfortivpn':
                        success = await self._connect_ssl_openfortivpn(config, plant_name)
                    elif attempt_method == 'openvpn':
                        success = await self.connect_openvpn(config, plant_name)
                    elif attempt_method == 'ssh':
                        success = await self.connect_ssh(config, plant_name)
                    elif attempt_method == 'demo':
                        success = await self.connect_demo(plant_name)
                    else:
                        success = False
                except Exception as e:
                    logger.warning(f"Error en método {attempt_method}: {e}")
                    success = False

                if success:
                    logger.info(f"{plant_name} conectado via {attempt_method}")
                    self._connected_plant = plant_name
                    self._connected_vpn_file = vpn_file
                    self._connected_routes = routes
                    return True

            logger.error(f"No se pudo conectar VPN para {plant_name}")
            self._connected_plant = None
            self._connected_vpn_file = None
            return False

        except Exception as e:
            logger.error(f"Error conectando VPN {plant_name}: {e}")
            return False

    async def connect_demo(self, plant_name: str) -> bool:
        try:
            logger.info(f"DEMO mode para {plant_name}")
            self.vpn_connected = True
            self.connection_start_time = time.time()
            self.current_plant_name = plant_name
            await asyncio.sleep(1)
            logger.info(f"DEMO: {plant_name} conectado (simulado)")
            return True
        except Exception as e:
            logger.error(f"Error en DEMO: {e}")
            return False

    async def disconnect_vpn(self, keep: bool = False) -> bool:
        try:
            # Al cerrar, limpiar metadatos de persistencia salvo que se pida conservar
            if not keep:
                self._connected_plant = None
                self._connected_vpn_file = None
                self._connected_routes = None
            # Cerrar tunel SSH si existe
            self.vpn_connected = False  # marcar primero para que threads se detengan
            if hasattr(self, 'ssh_forward_ports'):
                self.ssh_forward_ports.clear()
            if hasattr(self, 'ssh_transport') and self.ssh_transport:
                try:
                    self.ssh_transport.close()
                except:
                    pass
                self.ssh_transport = None
            if hasattr(self, 'ssh_client') and self.ssh_client:
                try:
                    self.ssh_client.close()
                except:
                    pass
                self.ssh_client = None

            # Cerrar túnel IPsec de strongSwan si existe
            try:
                await self._disconnect_strongswan()
            except Exception as e:
                logger.debug(f"Error cerrando strongSwan: {e}")

            # Matar proceso OpenVPN directo
            if self.current_vpn_process:
                try:
                    self.current_vpn_process.terminate()
                    await asyncio.wait_for(
                        asyncio.get_event_loop().run_in_executor(None, self.current_vpn_process.wait),
                        timeout=5
                    )
                except:
                    try:
                        self.current_vpn_process.kill()
                    except:
                        pass
                self.current_vpn_process = None

            # Matar procesos OpenVPN elevados via script
            launcher = os.path.join(
                os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                "scripts", "openvpn_elevated_launcher.ps1"
            )
            if os.path.exists(launcher):
                ps_cmd = (
                    f'Start-Process powershell -Verb RunAs -ArgumentList '
                    f'"-NoProfile -ExecutionPolicy Bypass -File \\"{launcher}\\" '
                    f'-Action disconnect -PidFile \\"{VPN_PID_FILE}\\"" '
                    f'-WindowStyle Hidden -Wait'
                )
                try:
                    proc = await asyncio.create_subprocess_exec(
                        'powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                        '-Command', ps_cmd,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE
                    )
                    await asyncio.wait_for(proc.communicate(), timeout=15)
                except:
                    pass

            # Desconectar y limpiar Windows VPN si existe
            if hasattr(self, '_vpn_connection_name') and self._vpn_connection_name:
                try:
                    script_path = os.path.join(
                        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                        'scripts', 'vpn_connect_windows.ps1'
                    )
                    if os.path.exists(script_path):
                        subprocess.run(
                            ['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                             '-File', script_path,
                             '-Action', 'remove',
                             '-Name', self._vpn_connection_name],
                            capture_output=True, timeout=15
                        )
                    else:
                        rasdial_path = os.path.join(
                            os.environ.get('SystemRoot', 'C:\\Windows'), 'System32', 'rasdial.exe'
                        )
                        subprocess.run(
                            [rasdial_path, self._vpn_connection_name, '/disconnect'],
                            capture_output=True, timeout=10
                        )
                    logger.info(f"Windows VPN desconectado: {self._vpn_connection_name}")
                except Exception as e:
                    logger.debug(f"Error desconectando Windows VPN: {e}")
                self._vpn_connection_name = None

            # Matar cualquier proceso openvpn.exe y openfortivpn
            for proc_name in ['openvpn.exe', 'openfortivpn.exe']:
                try:
                    result = subprocess.run(
                        ['taskkill', '/F', '/IM', proc_name],
                        capture_output=True, text=True, timeout=5
                    )
                    if result.returncode == 0:
                        logger.info(f"Procesos {proc_name} restantes terminados")
                except:
                    pass

            # Limpiar rutas VPN huérfanas
            try:
                route_output = subprocess.run(
                    ['route', 'print'],
                    capture_output=True, text=True, timeout=5
                )
                for line in route_output.stdout.split('\n'):
                    line = line.strip()
                    if any(p in line for p in ['10.110.', '10.120.', '10.130.']) and '192.168.150.' in line:
                        parts = [p for p in line.split() if p != '0.0.0.0' and p != 'ONLINK']
                        if len(parts) >= 3:
                            net = parts[0]
                            mask = parts[1]
                            gw = parts[2]
                            try:
                                subprocess.run(
                                    ['route', 'delete', net],
                                    capture_output=True, timeout=3
                                )
                            except:
                                pass
            except:
                pass

            # Limpiar PID file
            if os.path.exists(VPN_PID_FILE):
                try:
                    os.remove(VPN_PID_FILE)
                except:
                    pass

            self.current_vpn_config = None
            self.vpn_connected = False
            self._cleanup_temp_files()

            logger.info("VPN desconectado")
            return True

        except Exception as e:
            logger.error(f"Error desconectando VPN: {e}")
            return False

    def _cleanup_temp_files(self):
        for f in self.temp_files:
            try:
                if os.path.exists(f):
                    os.remove(f)
            except:
                pass
        self.temp_files = []

    def cleanup_old_logs(self, base_dir: str, plant_name: str, keep: int = 2):
        try:
            logs = sorted([
                os.path.join(base_dir, f)
                for f in os.listdir(base_dir)
                if f.startswith(f'openvpn_{plant_name}_') and f.endswith('.log')
            ], key=os.path.getctime)
            for old_log in logs[:-keep] if len(logs) > keep else []:
                try:
                    os.remove(old_log)
                except:
                    pass
        except:
            pass

    def is_vpn_connected(self) -> bool:
        if self.vpn_connected:
            return True
        if self.current_vpn_process:
            return self.current_vpn_process.poll() is None
        return False

    def connected_plant(self) -> Optional[str]:
        """Planta a la que esta conectada actualmente la VPN (para reutilizacion)."""
        return self._connected_plant

    def is_connected_to(self, plant_name: str) -> bool:
        """True si la VPN activa corresponde a la planta indicada."""
        return (self.vpn_connected and self._connected_plant == plant_name)

    def get_connection_uptime(self) -> int:
        if self.connection_start_time:
            return int(time.time() - self.connection_start_time)
        return 0


vpn_service = VPNServiceV2()
