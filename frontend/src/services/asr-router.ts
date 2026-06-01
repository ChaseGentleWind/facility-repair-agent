/**
 * 语音输入路由器：屏蔽底层差异，对外暴露统一接口。
 *
 * 决策顺序：
 *   1. 环境探针失败（HTTPS/getUserMedia 不可用）→ 不可用
 *   2. 后端 ASR 健康（health 探活成功）→ 用 WS ASR（recorder-core + WebSocket）
 *   3. 浏览器原生 SpeechRecognition 可用 → 用 Web Speech API（保留原 services/speech.ts）
 *   4. 都不行 → 不可用，UI 隐藏麦克风按钮
 *
 * 主项目阶段二联调时 wsUrl 临时指向沙箱 ws://localhost:8001/ws/asr，
 * 沙箱代码迁入后改为 /api/v1/asr/stream（同源相对路径自动拼协议）。
 */

import { SpeechService, speechSupported } from './speech'
import { ASRClient } from './asr-client'
import { StreamRecorder } from './recorder'

export type VoiceBackend = 'ws-asr' | 'web-speech' | 'none'

export interface VoiceRouterCallbacks {
  onPartial: (text: string) => void
  onFinal: (text: string) => void
  onError?: (message: string) => void
  onEnd: () => void
}

export interface VoiceRouterConfig {
  /** WebSocket ASR 端点。可以是绝对 ws(s):// 或相对路径 /api/v1/asr/stream */
  wsUrl?: string
  /** 健康检查端点（HEAD/GET 任意 200 都算可用）。留空则跳过探针，直接尝试连接 */
  healthUrl?: string
  /** 探针缓存 TTL，避免每次按麦都探一次。默认 60s */
  probeTtlMs?: number
}

const DEFAULT_PROBE_TTL = 60_000

let _probeCache: { backend: VoiceBackend; resolvedWsUrl: string | null; expireAt: number } | null =
  null

function resolveWsUrl(input?: string): string | null {
  if (!input) return null
  if (input.startsWith('ws://') || input.startsWith('wss://')) return input
  // 相对路径 → 基于当前页面拼协议
  if (typeof window === 'undefined') return null
  const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  const host = window.location.host
  const path = input.startsWith('/') ? input : `/${input}`
  return `${proto}//${host}${path}`
}

function envSupportsRecording(): boolean {
  if (typeof window === 'undefined') return false
  const isSecure =
    window.isSecureContext ||
    window.location.protocol === 'https:' ||
    window.location.hostname === 'localhost' ||
    window.location.hostname === '127.0.0.1'
  if (!isSecure) return false
  if (!navigator.mediaDevices?.getUserMedia) return false
  return true
}

async function probeHealth(url: string, timeoutMs = 1500): Promise<boolean> {
  try {
    const ctl = new AbortController()
    const t = setTimeout(() => ctl.abort(), timeoutMs)
    const resp = await fetch(url, { method: 'GET', signal: ctl.signal })
    clearTimeout(t)
    return resp.ok
  } catch {
    return false
  }
}

export async function detectVoiceBackend(
  config: VoiceRouterConfig,
): Promise<{ backend: VoiceBackend; resolvedWsUrl: string | null }> {
  const now = Date.now()
  if (_probeCache && _probeCache.expireAt > now) {
    return { backend: _probeCache.backend, resolvedWsUrl: _probeCache.resolvedWsUrl }
  }

  let backend: VoiceBackend = 'none'
  const resolvedWsUrl = resolveWsUrl(config.wsUrl)

  if (envSupportsRecording() && resolvedWsUrl) {
    const healthy = config.healthUrl ? await probeHealth(config.healthUrl) : true
    if (healthy) backend = 'ws-asr'
  }
  if (backend === 'none' && speechSupported) {
    backend = 'web-speech'
  }

  _probeCache = {
    backend,
    resolvedWsUrl,
    expireAt: now + (config.probeTtlMs ?? DEFAULT_PROBE_TTL),
  }
  return { backend, resolvedWsUrl }
}

export function clearVoiceBackendCache(): void {
  _probeCache = null
}

/**
 * 通用录音会话句柄。无论背后是 WS ASR 还是 Web Speech，调用方只用 start/stop。
 */
export interface VoiceSession {
  readonly backend: VoiceBackend
  stop(): Promise<void>
  abort(): void
}

export async function startVoiceSession(
  config: VoiceRouterConfig,
  cb: VoiceRouterCallbacks,
): Promise<VoiceSession> {
  const { backend, resolvedWsUrl } = await detectVoiceBackend(config)

  if (backend === 'ws-asr' && resolvedWsUrl) {
    return startWsAsrSession(resolvedWsUrl, cb)
  }
  if (backend === 'web-speech') {
    return startWebSpeechSession(cb)
  }
  cb.onError?.('voice_unavailable')
  cb.onEnd()
  return {
    backend: 'none',
    async stop() {},
    abort() {},
  }
}

async function startWsAsrSession(
  wsUrl: string,
  cb: VoiceRouterCallbacks,
): Promise<VoiceSession> {
  let recorder: StreamRecorder | null = null
  let ended = false
  const endOnce = () => {
    if (ended) return
    ended = true
    cb.onEnd()
  }

  const client = new ASRClient({
    url: wsUrl,
    onPartial: (text) => cb.onPartial(text),
    onFinal: (text) => {
      if (text) cb.onFinal(text)
    },
    onError: (code, message) => {
      cb.onError?.(`${code}: ${message}`)
    },
    onClose: () => {
      endOnce()
    },
  })

  try {
    await client.connect()
  } catch (err) {
    cb.onError?.((err as Error).message || 'ws_connect_failed')
    endOnce()
    // 让上层（input-bar）感知失败，可考虑下次按下时重新探针 → 回退 web-speech
    clearVoiceBackendCache()
    throw err
  }

  recorder = new StreamRecorder({
    onPcmChunk: (bytes) => client.sendPcm(bytes),
    onError: (err) => {
      cb.onError?.(err.message)
      client.abort()
      endOnce()
    },
  })

  try {
    await recorder.start()
  } catch (err) {
    client.abort()
    endOnce()
    throw err
  }

  return {
    backend: 'ws-asr',
    async stop() {
      try {
        await recorder?.stop()
      } finally {
        await client.stop()
        endOnce()
      }
    },
    abort() {
      try {
        void recorder?.stop()
      } finally {
        client.abort()
        endOnce()
      }
    },
  }
}

function startWebSpeechSession(cb: VoiceRouterCallbacks): VoiceSession {
  let ended = false
  const endOnce = () => {
    if (ended) return
    ended = true
    cb.onEnd()
  }

  const speech = new SpeechService({
    onInterim: (text) => cb.onPartial(text),
    onFinal: (text) => {
      if (text.trim()) cb.onFinal(text.trim())
    },
    onEnd: () => endOnce(),
  })
  speech.start()

  return {
    backend: 'web-speech',
    async stop() {
      speech.stop()
    },
    abort() {
      speech.stop()
      endOnce()
    },
  }
}
