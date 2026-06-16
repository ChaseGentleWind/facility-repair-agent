/**
 * ASR WebSocket 客户端：与 voice-asr-lab 后端 / 主项目 backend/api/v1/asr 通信。
 *
 * 协议（与后端 ws_handler.py 对齐）：
 *  C → S text: {"action":"start","sample_rate":16000}
 *  C → S binary: 16kHz int16 mono PCM
 *  C → S text: {"action":"stop"}
 *  S → C text: {"action":"ready"}
 *  S → C text: {"action":"partial","text":"..."}
 *  S → C text: {"action":"final","text":"...","accumulated":"..."}
 *  S → C text: {"action":"error","code":"...","message":"..."}
 */

export interface ASRClientOptions {
  url: string
  onPartial?: (text: string) => void
  onFinal?: (text: string) => void
  onError?: (code: string, message: string) => void
  onReady?: () => void
  onClose?: () => void
}

export type ASRClientState = 'idle' | 'connecting' | 'ready' | 'closing' | 'closed' | 'error'

export class ASRClient {
  private _ws: WebSocket | null = null
  private _opts: ASRClientOptions
  private _accumulated = ''
  private _state: ASRClientState = 'idle'

  constructor(opts: ASRClientOptions) {
    this._opts = opts
  }

  get state(): ASRClientState {
    return this._state
  }

  get accumulatedText(): string {
    return this._accumulated
  }

  async connect(): Promise<void> {
    if (this._state !== 'idle' && this._state !== 'closed' && this._state !== 'error') {
      return
    }
    this._state = 'connecting'
    this._accumulated = ''

    await new Promise<void>((resolve, reject) => {
      let ws: WebSocket
      try {
        ws = new WebSocket(this._opts.url)
      } catch (err) {
        this._state = 'error'
        reject(err)
        return
      }
      ws.binaryType = 'arraybuffer'

      const onOpenError = () => {
        this._state = 'error'
        reject(new Error('ws_connect_failed'))
      }

      ws.onopen = () => {
        ws.send(JSON.stringify({ action: 'start', sample_rate: 16000 }))
      }

      ws.onmessage = (e) => {
        if (typeof e.data !== 'string') return
        let frame: Record<string, unknown>
        try {
          frame = JSON.parse(e.data)
        } catch {
          return
        }
        const action = frame.action
        if (action === 'ready') {
          this._state = 'ready'
          this._opts.onReady?.()
          resolve()
        } else if (action === 'partial') {
          const text = String(frame.text ?? '')
          if (text) {
            this._accumulated += text
            this._opts.onPartial?.(this._accumulated)
          }
        } else if (action === 'final') {
          const accum = String(frame.accumulated ?? frame.text ?? this._accumulated)
          this._accumulated = accum
          this._opts.onFinal?.(accum)
        } else if (action === 'error') {
          const code = String(frame.code ?? 'UNKNOWN')
          const message = String(frame.message ?? '')
          this._opts.onError?.(code, message)
          if (this._state === 'connecting') {
            this._state = 'error'
            reject(new Error(`${code}: ${message}`))
          }
        }
      }

      ws.onerror = () => {
        if (this._state === 'connecting') onOpenError()
      }

      ws.onclose = () => {
        this._state = 'closed'
        this._ws = null
        this._opts.onClose?.()
      }

      this._ws = ws
    })
  }

  sendPcm(bytes: Uint8Array): void {
    if (this._state !== 'ready' || !this._ws) return
    if (this._ws.readyState !== WebSocket.OPEN) return
    // 显式拷到独立 ArrayBuffer 再发，避免视图与底层 buffer 偏移引发问题
    const buf = bytes.byteOffset === 0 && bytes.byteLength === bytes.buffer.byteLength
      ? bytes.buffer
      : bytes.slice().buffer
    this._ws.send(buf)
  }

  async stop(): Promise<void> {
    if (!this._ws || this._state === 'closed' || this._state === 'closing') return
    this._state = 'closing'
    try {
      this._ws.send(JSON.stringify({ action: 'stop' }))
    } catch {
      // ignore
    }
    // 等服务端把 final 帧推过来再关
    await new Promise<void>((resolve) => {
      const start = Date.now()
      const tick = () => {
        if (this._state === 'closed' || Date.now() - start > 5000) {
          resolve()
        } else {
          setTimeout(tick, 50)
        }
      }
      tick()
    })
    try {
      this._ws?.close()
    } catch {
      // ignore
    }
  }

  abort(): void {
    if (!this._ws) return
    try {
      this._ws.close()
    } catch {
      // ignore
    }
    this._ws = null
    this._state = 'closed'
  }
}
