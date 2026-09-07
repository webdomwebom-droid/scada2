import { useEffect, useState } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { gwControlAPI, gatewaysAPI } from '../services/api'
import { useAuth } from '../context/AuthContext'
import {
  ArrowLeft, RefreshCw, Cpu, Radio, FileText, Terminal, Activity,
  Loader, CheckCircle, Database, Download, Save, Wifi, WifiOff, PlugZap
} from 'lucide-react'

const DIRECTORIES = ['LOGS/', 'DATA/', 'CBTB/', 'STDS/']

type Tab = 'status' | 'grid' | 'conf' | 'commands' | 'scan' | 'files'

export default function GatewayControl() {
  const { id } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const gwId = parseInt(id || '0')
  const { user } = useAuth()
  const isAdmin = user?.role === 'admin'

  const [tab, setTab] = useState<Tab>('status')
  const [gateway, setGateway] = useState<any>(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [toast, setToast] = useState<{ type: 'ok' | 'err'; msg: string } | null>(null)

  // Status
  const [status, setStatus] = useState<any>(null)
  const [firmware, setFirmware] = useState<string>('')
  const [sysConfig, setSysConfig] = useState<any>(null)

  // Tabla CB
  const [cbItems, setCbItems] = useState<any[]>([])

  // Conf slave
  const [selectedCb, setSelectedCb] = useState<number | null>(null)
  const [slaveLora, setSlaveLora] = useState<any>(null)
  const [analogBottom, setAnalogBottom] = useState<any[]>([])

  // Escaneo LoRa
  const [scanResults, setScanResults] = useState<any[]>([])

  // Archivos
  const [dir, setDir] = useState<string>('LOGS/')
  const [fileList, setFileList] = useState<any[]>([])

  // Conexión (VPN/túnel) - para trabajo en campo
  const [conn, setConn] = useState<any>(null)
  const [reconnecting, setReconnecting] = useState(false)

  useEffect(() => {
    loadAll()
  }, [gwId])

  const notify = (type: 'ok' | 'err', msg: string) => {
    setToast({ type, msg })
    setTimeout(() => setToast(null), 3500)
  }

  const loadConnection = async () => {
    try {
      const res = await gwControlAPI.connection(gwId)
      setConn(res.data)
    } catch {
      setConn(null)
    }
  }

  const reconnect = async () => {
    setReconnecting(true)
    try {
      await gwControlAPI.reconnect(gwId)
      notify('ok', 'VPN reconectada y gateway respondiendo')
      await loadConnection()
      await loadStatus()
      await loadGrid()
    } catch (e: any) {
      notify('err', e?.response?.data?.detail || 'No se pudo reconectar')
      await loadConnection()
    } finally {
      setReconnecting(false)
    }
  }

  const loadAll = async () => {
    setLoading(true)
    try {
      const [gw] = await Promise.all([
        gatewaysAPI.getById(gwId)
      ])
      setGateway(gw.data)
      loadConnection()
      loadStatus()
      loadGrid()
    } catch (e: any) {
      notify('err', e?.response?.data?.detail || 'Error cargando gateway')
    } finally {
      setLoading(false)
    }
  }

  const loadStatus = async () => {
    try {
      const [st, fw, sc] = await Promise.all([
        gwControlAPI.status(gwId),
        gwControlAPI.firmware(gwId),
        gwControlAPI.sysConfig(gwId)
      ])
      setStatus(st.data)
      setFirmware(fw.data.version)
      setSysConfig(sc.data)
    } catch (e: any) {
      notify('err', 'No se pudo leer el estado del gateway. Verifique la VPN.')
    }
  }

  const loadGrid = async () => {
    try {
      const res = await gwControlAPI.cbTable(gwId)
      const items = Array.isArray(res.data.items) ? res.data.items : []
      setCbItems(items)
    } catch (e: any) {
      notify('err', e?.response?.data?.detail || 'No se pudo leer la tabla CB')
    }
  }

  const run = async (fn: () => Promise<any>, okMsg: string) => {
    setBusy(true)
    try {
      await fn()
      notify('ok', okMsg)
    } catch (e: any) {
      notify('err', e?.response?.data?.detail || 'Error al conectar con el gateway (¿VPN?)')
    } finally {
      setBusy(false)
    }
  }

  const macOf = (cbId: number | null) =>
    cbItems.find(it => it.id === cbId)?.mac as string | undefined

  const selectSlave = async (cbId: number) => {
    setSelectedCb(cbId)
    const mac = macOf(cbId)
    try {
      const [lora, ab] = await Promise.all([
        gwControlAPI.slaveLora(gwId, cbId, mac),
        gwControlAPI.slaveAnalogBottom(gwId, cbId, mac)
      ])
      setSlaveLora(lora.data?.lora_conf || lora.data?.lora || lora.data)
      setAnalogBottom((ab.data?.channels) || [])
    } catch (e: any) {
      notify('err', e?.response?.data?.detail || 'No se pudo leer configuración del esclavo')
    }
  }

  const saveLora = async () => {
    if (selectedCb === null) return
    const l = slaveLora || {}
    await run(() => gwControlAPI.writeSlaveLora(gwId, selectedCb, {
      low_data_rate_opt: l.low_data_rate_opt,
      crc_dis: l.crc_dis,
      explicit_en: l.explicit_en,
      fix_pkln_en: l.fix_pkln_en,
      bandwidth: l.bandwidth,
      coding_rate: l.coding_rate,
      sfactor: l.sfactor,
      tx_pwr: l.tx_pwr,
      pream_length: l.pream_length,
      fixed_pk_length: l.fixed_pk_length,
      frq: l.frq
    }, macOf(selectedCb)), 'Configuración LoRa guardada')
  }

  const scanLora = async () => {
    await run(async () => {
      const res = await gwControlAPI.loraScan(gwId)
      setScanResults(res.data?.items || res.data?.results || [])
    }, 'Escaneo LoRa completado')
  }

  const loadDir = async (d: string) => {
    setDir(d)
    try {
      const res = await gwControlAPI.dir(gwId, d)
      const items = res.data?.items || []
      setFileList(items.map((f: any) => (typeof f === 'string' ? { name: f, directory: false } : f)))
    } catch (e: any) {
      notify('err', e?.response?.data?.detail || 'No se pudo listar el directorio')
    }
  }

  const downloadFile = async (d: string, f: string) => {
    try {
      const res = await gwControlAPI.download(gwId, d, f)
      const b64 = res.data?.data_b64
      if (!b64) { notify('err', 'Formato de descarga inválido'); return }
      const bin = atob(b64)
      const bytes = new Uint8Array(bin.length)
      for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i)
      const blob = new Blob([bytes])
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = f
      a.click()
      URL.revokeObjectURL(url)
      notify('ok', 'Archivo descargado')
    } catch (e: any) {
      notify('err', 'Error descargando archivo')
    }
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center h-screen">
        <Loader className="animate-spin text-blue-600" />
      </div>
    )
  }

  const statusColor: Record<string, string> = {
    '0': 'bg-green-100 text-green-700',
    '1': 'bg-yellow-100 text-yellow-700',
    '2': 'bg-red-100 text-red-700',
    '3': 'bg-red-100 text-red-700',
    '4': 'bg-gray-100 text-gray-700'
  }
  const modeNames = ['DataLog', 'Configuration', 'Unknown', 'Error', 'Offline']

  const tabs: { key: Tab; label: string; icon: any }[] = [
    { key: 'status', label: 'Estado', icon: Activity },
    { key: 'grid', label: 'Tabla CB', icon: Database },
    { key: 'conf', label: 'Configuración', icon: Cpu },
    { key: 'commands', label: 'Comandos', icon: Terminal },
    { key: 'scan', label: 'Escaneo LoRa', icon: Radio },
    { key: 'files', label: 'Archivos', icon: FileText }
  ]

  return (
    <div className="min-h-screen bg-gray-50">
      <div className="bg-white border-b border-gray-200 px-6 py-4 flex items-center justify-between">
        <div className="flex items-center gap-3">
          <button onClick={() => navigate(-1)} className="p-2 hover:bg-gray-100 rounded-lg">
            <ArrowLeft className="w-5 h-5 text-gray-600" />
          </button>
          <div>
            <h1 className="text-lg font-semibold text-gray-800">Control de Gateway</h1>
            <p className="text-sm text-gray-500">
              {gateway?.name || gateway?.ip || ''} · {gateway?.ip || ''}
            </p>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <span
            className={`flex items-center gap-2 px-3 py-2 text-sm rounded-lg border ${
              conn?.vpn_ready ? 'bg-green-50 text-green-700 border-green-200'
                : 'bg-red-50 text-red-700 border-red-200'
            }`}
            title={conn?.vpn_plant ? `VPN: ${conn.vpn_plant}` : 'Sin túnel activo'}
          >
            {conn?.vpn_ready ? <Wifi className="w-4 h-4" /> : <WifiOff className="w-4 h-4" />}
            {conn?.vpn_ready ? `VPN ${conn?.plant || ''}` : 'VPN desconectada'}
            {conn?.demo && <span className="text-xs">(demo)</span>}
          </span>
          <button
            onClick={reconnect}
            disabled={reconnecting}
            className="flex items-center gap-2 px-3 py-2 text-sm bg-amber-500 text-white rounded-lg hover:bg-amber-600 disabled:opacity-50"
            title="Cierra el túnel y vuelve a conectar (uso en campo)"
          >
            {reconnecting ? <Loader className="w-4 h-4 animate-spin" /> : <PlugZap className="w-4 h-4" />} Reconectar
          </button>
          <button onClick={() => { loadConnection(); loadStatus() }} className="flex items-center gap-2 px-3 py-2 text-sm border rounded-lg hover:bg-gray-50">
            <RefreshCw className={`w-4 h-4 ${busy ? 'animate-spin' : ''}`} /> Refrescar
          </button>
        </div>
      </div>

      {toast && (
        <div className={`mx-6 mt-4 px-4 py-3 rounded-lg text-sm flex items-center gap-2 ${
          toast.type === 'ok' ? 'bg-green-50 text-green-700 border border-green-200'
            : 'bg-red-50 text-red-700 border border-red-200'
        }`}>
          {toast.type === 'ok' ? <CheckCircle className="w-4 h-4" /> : <span>⚠</span>}
          {toast.msg}
        </div>
      )}

      <div className="px-6 mt-4 flex gap-2 flex-wrap">
        {tabs.map(t => (
          <button
            key={t.key}
            onClick={() => setTab(t.key)}
            className={`flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-medium transition ${
              tab === t.key ? 'bg-blue-600 text-white' : 'bg-white text-gray-600 border hover:bg-gray-50'
            }`}
          >
            <t.icon className="w-4 h-4" /> {t.label}
          </button>
        ))}
      </div>

      <div className="px-6 py-6">
        {/* ============ ESTADO ============ */}
        {tab === 'status' && (
          <div className="space-y-4">
            <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
              <div className="bg-white rounded-xl shadow-sm border p-5">
                <div className="text-sm text-gray-500">Estado del Gateway</div>
                <div className="mt-2 flex items-center gap-2">
                  <Wifi className="w-5 h-5 text-blue-600" />
                  <span className={`px-2 py-1 rounded text-sm font-medium ${statusColor[String(status?.gw_status)] || 'bg-gray-100'}`}>
                    {modeNames[status?.gw_status] || String(status?.gw_status)}
                  </span>
                </div>
                <div className="mt-3 text-xs text-gray-400">
                  LoRa updating: {status?.lora_updating}
                </div>
              </div>
              <div className="bg-white rounded-xl shadow-sm border p-5">
                <div className="text-sm text-gray-500">Firmware</div>
                <div className="mt-2 text-lg font-semibold text-gray-800">{firmware || '—'}</div>
              </div>
              <div className="bg-white rounded-xl shadow-sm border p-5">
                <div className="text-sm text-gray-500">MAC / Slave ID</div>
                <div className="mt-2 text-sm text-gray-700">{status?.mac || '—'}</div>
                <div className="text-xs text-gray-400">slave_id: {status?.slave_id}</div>
              </div>
            </div>

                {sysConfig && (
                  <div className="bg-white rounded-xl shadow-sm border p-5">
                    <h3 className="font-semibold text-gray-800 mb-3">Configuración del Sistema</h3>
                    <div className="grid grid-cols-2 md:grid-cols-4 gap-4 text-sm">
                      <div><span className="text-gray-500">Modo:</span> <b>{sysConfig.mode}</b></div>
                      <div><span className="text-gray-500">Intervalo data log:</span> <b>{sysConfig.data_log_interval} s</b></div>
                      <div><span className="text-gray-500">Zona horaria:</span> <b>{sysConfig.zone_time}</b></div>
                      <div><span className="text-gray-500">DST:</span> <b>{sysConfig.dst_saving ? 'Sí' : 'No'}</b></div>
                      <div><span className="text-gray-500">Fallos LoRa:</span> <b>{sysConfig.n_lora_fail}</b></div>
                      <div><span className="text-gray-500">Umbral:</span> <b>{sysConfig.threshold}</b></div>
                      <div><span className="text-gray-500">Ganancia:</span> <b>{sysConfig.gain}</b></div>
                    </div>
                    {status && (
                      <div className="mt-4 grid grid-cols-2 md:grid-cols-4 gap-2 text-xs">
                        <div className={status.cbt_modified ? 'text-amber-600' : 'text-green-600'}>
                          CB table: {status.cbt_modified ? 'modificada' : 'ok'}
                        </div>
                        <div className={status.cbconf_modified ? 'text-amber-600' : 'text-green-600'}>
                          CB config: {status.cbconf_modified ? 'modificada' : 'ok'}
                        </div>
                        <div className={status.lst_modified ? 'text-amber-600' : 'text-green-600'}>
                          Lista: {status.lst_modified ? 'modificada' : 'ok'}
                        </div>
                        <div className="text-gray-500">SNTP: {status.sntp_status}</div>
                      </div>
                    )}
                  </div>
                )}
          </div>
        )}

        {/* ============ TABLA CB ============ */}
        {tab === 'grid' && (
          <div className="bg-white rounded-xl shadow-sm border overflow-hidden">
            <div className="px-5 py-4 border-b flex items-center justify-between">
              <h3 className="font-semibold text-gray-800">Tabla CB ({cbItems.length})</h3>
              <button onClick={loadGrid} className="flex items-center gap-2 text-sm text-blue-600 hover:underline">
                <RefreshCw className="w-4 h-4" /> Actualizar
              </button>
            </div>
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead className="bg-gray-50 text-gray-500">
                  <tr>
                    <th className="text-left px-4 py-2">ID</th>
                    <th className="text-left px-4 py-2">MAC</th>
                    <th className="text-left px-4 py-2">Key</th>
                    <th className="text-left px-4 py-2">Freq (MHz)</th>
                    <th className="text-left px-4 py-2">SF/BW</th>
                    <th className="text-left px-4 py-2">Configurado</th>
                    <th className="text-left px-4 py-2">Acciones</th>
                  </tr>
                </thead>
                <tbody>
                  {cbItems.map(item => (
                    <tr key={item.id} className="border-t hover:bg-gray-50">
                      <td className="px-4 py-2 font-medium">{item.id}</td>
                      <td className="px-4 py-2">{item.mac}</td>
                      <td className="px-4 py-2 text-xs">{item.key}</td>
                      <td className="px-4 py-2">{(item.lo_conf?.frq / 1000000).toFixed(1)}</td>
                      <td className="px-4 py-2">SF{((item.lo_conf?.raw_bits >> 12) & 0xF) || '7'} / BW{(((item.lo_conf?.raw_bits >> 4) & 0xF) || 0)}</td>
                      <td className="px-4 py-2">
                        <span className={`px-2 py-0.5 rounded text-xs ${item.configured ? 'bg-green-100 text-green-700' : 'bg-gray-100 text-gray-500'}`}>
                          {item.configured ? 'Sí' : 'No'}
                        </span>
                      </td>
                      <td className="px-4 py-2">
                        <button
                          onClick={() => selectSlave(item.id)}
                          className="px-3 py-1 text-xs bg-blue-600 text-white rounded-lg hover:bg-blue-700"
                        >
                          Configurar
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {cbItems.length === 0 && (
              <div className="p-10 text-center text-gray-400">Tabla CB vacía. Conecte la VPN e intente de nuevo.</div>
            )}
          </div>
        )}

        {/* ============ CONFIGURACIÓN ============ */}
        {tab === 'conf' && (
          <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
            <div className="lg:col-span-1 bg-white rounded-xl shadow-sm border p-4">
              <h3 className="font-semibold text-gray-800 mb-3">Seleccionar esclavo</h3>
              <select
                value={selectedCb ?? ''}
                onChange={e => selectSlave(parseInt(e.target.value))}
                className="w-full border rounded-lg px-3 py-2 text-sm"
              >
                <option value="">— Seleccione —</option>
                {cbItems.map(it => (
                  <option key={it.id} value={it.id}>
                    {it.id} · {it.mac}
                  </option>
                ))}
              </select>
              {selectedCb !== null && slaveLora && (
                <div className="mt-4">
                  <div className="text-xs text-gray-500 mb-2">Parámetros LoRa</div>
                  <div className="space-y-2 text-sm">
                    <div className="flex justify-between"><span className="text-gray-500">Frecuencia</span><b>{(slaveLora.frq / 1000000).toFixed(3)} MHz</b></div>
                    <div className="flex justify-between"><span className="text-gray-500">Bandwidth</span><b>{slaveLora.bandwidth}</b></div>
                    <div className="flex justify-between"><span className="text-gray-500">Coding rate</span><b>{slaveLora.coding_rate}</b></div>
                    <div className="flex justify-between"><span className="text-gray-500">SF</span><b>{slaveLora.sfactor}</b></div>
                    <div className="flex justify-between"><span className="text-gray-500">Tx Power</span><b>{slaveLora.tx_pwr}</b></div>
                    <div className="flex justify-between"><span className="text-gray-500">Preamble</span><b>{slaveLora.pream_length}</b></div>
                  </div>
                </div>
              )}
            </div>

            {selectedCb !== null && slaveLora ? (
              <div className="lg:col-span-2 space-y-4">
                <div className="bg-white rounded-xl shadow-sm border p-5">
                  <h3 className="font-semibold text-gray-800 mb-4">Configuración LoRa</h3>
                  <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
                    {[
                      ['bandwidth', 'Bandwidth', 'number', 0],
                      ['coding_rate', 'Coding rate', 'number', 0],
                      ['sfactor', 'SF (7-12)', 'number', 7],
                      ['tx_pwr', 'Tx power', 'number', 0],
                      ['pream_length', 'Preamble', 'number', 0],
                      ['fixed_pk_length', 'Fixed PK length', 'number', 0],
                      ['frq', 'Frecuencia (Hz)', 'number', 0],
                      ['lora_id', 'LoRa ID', 'number', 0]
                    ].map(([key, label, , def]) => (
                      <div key={key as string}>
                        <label className="text-xs text-gray-500">{label}</label>
                        <input
                          type="number"
                          defaultValue={(slaveLora as any)[key as string] ?? def}
                          onChange={e => setSlaveLora((p: any) => ({ ...p, [key]: parseInt(e.target.value) }))}
                          className="w-full border rounded-lg px-3 py-2 text-sm mt-1"
                        />
                      </div>
                    ))}
                  </div>
                  <div className="mt-4 grid grid-cols-2 md:grid-cols-4 gap-3">
                    {[
                      ['low_data_rate_opt', 'Low Data Rate Opt'],
                      ['crc_dis', 'CRC disable'],
                      ['explicit_en', 'Explicit header'],
                      ['fix_pkln_en', 'Fixed packet length']
                    ].map(([key, label]) => (
                      <label key={key as string} className="flex items-center gap-2 text-sm">
                        <input
                          type="checkbox"
                          defaultChecked={(slaveLora as any)[key as string] || false}
                          onChange={e => setSlaveLora((p: any) => ({ ...p, [key]: e.target.checked }))}
                          className="rounded"
                        />
                        {label}
                      </label>
                    ))}
                  </div>
                  <button
                    onClick={saveLora}
                    disabled={!isAdmin || busy}
                    className="mt-5 flex items-center gap-2 px-4 py-2 bg-blue-600 text-white rounded-lg text-sm hover:bg-blue-700 disabled:opacity-50"
                  >
                    <Save className="w-4 h-4" /> Guardar (NVM)
                  </button>
                </div>

                <div className="bg-white rounded-xl shadow-sm border p-5">
                  <h3 className="font-semibold text-gray-800 mb-4">Canales analógicos (Bottom)</h3>
                  <div className="overflow-x-auto">
                    <table className="w-full text-sm">
                      <thead className="bg-gray-50 text-gray-500">
                        <tr><th className="px-3 py-2 text-left">Ch</th><th className="px-3 py-2 text-left">K</th><th className="px-3 py-2 text-left">Offset</th><th className="px-3 py-2 text-left">N media</th></tr>
                      </thead>
                      <tbody>
                        {analogBottom.map((ch, i) => (
                          <tr key={i} className="border-t">
                            <td className="px-3 py-1">{ch.channel}</td>
                            <td className="px-3 py-1">{ch.k ?? 0}</td>
                            <td className="px-3 py-1">{ch.offset ?? 0}</td>
                            <td className="px-3 py-1">{ch.n_mean ?? 0}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </div>
              </div>
            ) : (
              <div className="lg:col-span-2 bg-white rounded-xl shadow-sm border p-10 text-center text-gray-400">
                Seleccione un esclavo de la tabla CB para ver y editar su configuración LoRa y analógica.
              </div>
            )}
          </div>
        )}

        {/* ============ COMANDOS ============ */}
        {tab === 'commands' && (
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div className="bg-white rounded-xl shadow-sm border p-5">
              <h3 className="font-semibold text-gray-800 mb-4 flex items-center gap-2"><Cpu className="w-4 h-4" /> Gateway</h3>
              <div className="space-y-2">
                <button onClick={() => run(() => gwControlAPI.setMode(gwId, 0), 'Modo DataLog')} disabled={!isAdmin} className="w-full text-left px-4 py-2 border rounded-lg hover:bg-gray-50 text-sm disabled:opacity-50">Modo DataLog</button>
                <button onClick={() => run(() => gwControlAPI.setMode(gwId, 1), 'Modo Configuration')} disabled={!isAdmin} className="w-full text-left px-4 py-2 border rounded-lg hover:bg-gray-50 text-sm disabled:opacity-50">Modo Configuration</button>
                <button onClick={() => run(() => gwControlAPI.saveNvm(gwId), 'Configuración guardada en NVM')} disabled={!isAdmin} className="w-full text-left px-4 py-2 border rounded-lg hover:bg-gray-50 text-sm disabled:opacity-50">Guardar en NVM</button>
                <button onClick={() => run(() => gwControlAPI.reset(gwId), 'Gateway reiniciado')} disabled={!isAdmin} className="w-full text-left px-4 py-2 border rounded-lg hover:bg-red-50 text-sm text-red-600 disabled:opacity-50">Reiniciar Gateway</button>
              </div>
            </div>
            <div className="bg-white rounded-xl shadow-sm border p-5">
              <h3 className="font-semibold text-gray-800 mb-4 flex items-center gap-2"><Radio className="w-4 h-4" /> Esclavo seleccionado</h3>
              <p className="text-sm text-gray-500 mb-4">
                {selectedCb !== null ? `Aplicando a esclavo CB#${selectedCb}` : 'Seleccione primero un esclavo en la pestaña Tabla CB'}
              </p>
              <div className="space-y-2">
                <button onClick={() => selectedCb !== null && run(() => gwControlAPI.slaveZero(gwId, selectedCb, macOf(selectedCb)), 'ZERO enviado')} disabled={!isAdmin || selectedCb === null} className="w-full text-left px-4 py-2 border rounded-lg hover:bg-gray-50 text-sm disabled:opacity-50">Enviar comando ZERO</button>
                <button onClick={() => selectedCb !== null && run(() => gwControlAPI.slaveCommand(gwId, selectedCb, 1, macOf(selectedCb)), 'Comando enviado')} disabled={!isAdmin || selectedCb === null} className="w-full text-left px-4 py-2 border rounded-lg hover:bg-gray-50 text-sm disabled:opacity-50">Enviar comando 1</button>
                <button onClick={() => selectedCb !== null && run(() => gwControlAPI.slaveCommand(gwId, selectedCb, 2, macOf(selectedCb)), 'Comando enviado')} disabled={!isAdmin || selectedCb === null} className="w-full text-left px-4 py-2 border rounded-lg hover:bg-gray-50 text-sm disabled:opacity-50">Enviar comando 2</button>
                <button onClick={() => selectedCb !== null && run(() => gwControlAPI.slaveCommand(gwId, selectedCb, 3, macOf(selectedCb)), 'Comando enviado')} disabled={!isAdmin || selectedCb === null} className="w-full text-left px-4 py-2 border rounded-lg hover:bg-gray-50 text-sm disabled:opacity-50">Enviar comando 3</button>
              </div>
            </div>
          </div>
        )}

        {/* ============ ESCANEO LORA ============ */}
        {tab === 'scan' && (
          <div className="bg-white rounded-xl shadow-sm border p-5">
            <div className="flex items-center justify-between mb-4">
              <h3 className="font-semibold text-gray-800">Escaneo LoRa de esclavos</h3>
              <button
                onClick={scanLora}
                disabled={busy}
                className="flex items-center gap-2 px-4 py-2 bg-blue-600 text-white rounded-lg text-sm hover:bg-blue-700 disabled:opacity-50"
              >
                {busy ? <Loader className="animate-spin w-4 h-4" /> : <Radio className="w-4 h-4" />} Escanear
              </button>
            </div>
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead className="bg-gray-50 text-gray-500">
                  <tr><th className="px-4 py-2 text-left">ID</th><th className="px-4 py-2 text-left">MAC</th><th className="px-4 py-2 text-left">PKT SNR</th><th className="px-4 py-2 text-left">PKT RSSI</th><th className="px-4 py-2 text-left">RSSI</th></tr>
                </thead>
                <tbody>
                  {scanResults.map((r: any, i) => (
                    <tr key={i} className="border-t">
                      <td className="px-4 py-2">{r.id}</td>
                      <td className="px-4 py-2">{r.mac}</td>
                      <td className="px-4 py-2">{r.pkt_snr}</td>
                      <td className="px-4 py-2">{r.pkt_rssi}</td>
                      <td className="px-4 py-2">{r.rssi}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {scanResults.length === 0 && (
              <div className="p-8 text-center text-gray-400">Sin resultados. Ejecute el escaneo LoRa con la VPN conectada.</div>
            )}
          </div>
        )}

        {/* ============ ARCHIVOS ============ */}
        {tab === 'files' && (
          <div className="bg-white rounded-xl shadow-sm border p-5">
            <div className="flex items-center gap-2 mb-4 flex-wrap">
              <h3 className="font-semibold text-gray-800">Gestión de archivos</h3>
              {DIRECTORIES.map(d => (
                <button
                  key={d}
                  onClick={() => loadDir(d)}
                  className={`px-3 py-1.5 rounded-lg text-sm border ${
                    dir === d ? 'bg-blue-600 text-white border-blue-600' : 'hover:bg-gray-50'
                  }`}
                >
                  {d.replace('/', '')}
                </button>
              ))}
              <input
                value={dir}
                onChange={e => setDir(e.target.value)}
                className="border rounded-lg px-3 py-2 text-sm flex-1 min-w-40"
              />
              <button onClick={() => loadDir(dir)} className="px-4 py-2 bg-blue-600 text-white rounded-lg text-sm hover:bg-blue-700">
                Listar
              </button>
            </div>
            <div className="space-y-1">
              {fileList.map((f, i) => (
                <div key={i} className="flex items-center justify-between px-3 py-2 border rounded-lg text-sm">
                  <span className="flex items-center gap-2 text-gray-700">
                    <FileText className="w-4 h-4 text-gray-400" /> {f.name}
                    {f.directory && <span className="text-xs text-blue-500">directorio</span>}
                  </span>
                  <div className="flex gap-2">
                    <button onClick={() => f.directory ? loadDir(f.name) : downloadFile(dir, f.name)} className="flex items-center gap-1 px-2 py-1 text-xs border rounded-lg hover:bg-gray-50">
                      {f.directory ? 'Abrir' : <><Download className="w-3 h-3" /> Descargar</>}
                    </button>
                  </div>
                </div>
              ))}
            </div>
            {fileList.length === 0 && (
              <div className="p-8 text-center text-gray-400">Directorio vacío o no accesible.</div>
            )}
          </div>
        )}
      </div>
    </div>
  )
}
