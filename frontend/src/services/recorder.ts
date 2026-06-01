/**
 * recorder-core 封装：16kHz mono int16 PCM 流式录音。
 *
 * 必须用 PCM 而非 mp3/wav，因为后端 FunASR 直接消费 16kHz int16 字节流。
 * recorder-core 在微信 / iOS WebView 上比原生 MediaRecorder 兼容性更好。
 *
 * 用法：
 *   const rec = new StreamRecorder({
 *     onPcmChunk: (bytes) => ws.send(bytes),
 *     onPower: (level) => waveAnim.update(level),
 *   })
 *   await rec.start()
 *   ...
 *   await rec.stop()
 */

// recorder-core 在 npm 上是 CommonJS，没有类型声明，统一用 any
// eslint-disable-next-line @typescript-eslint/no-explicit-any
type RecorderInstance = any

export interface StreamRecorderOptions {
  onPcmChunk: (bytes: Uint8Array) => void
  onPower?: (level: number) => void
  onError?: (err: Error) => void
}

export class StreamRecorder {
  private _rec: RecorderInstance | null = null
  private _opts: StreamRecorderOptions
  private _started = false
  private _lastBufferIdx = 0

  constructor(opts: StreamRecorderOptions) {
    this._opts = opts
  }

  get recording(): boolean {
    return this._started
  }

  async start(): Promise<void> {
    if (this._started) return

    const Recorder = await loadRecorderCore()

    const rec: RecorderInstance = Recorder({
      type: 'pcm',
      sampleRate: 16000,
      bitRate: 16,
      onProcess: (
        buffers: Int16Array[],
        powerLevel: number,
        _bufferDuration: number,
        _bufferSampleRate: number,
        newBufferIdx: number,
      ) => {
        // 取本次回调新增的 buffer 段（recorder-core 是累加 buffers 数组）
        for (let i = this._lastBufferIdx; i < buffers.length && i < newBufferIdx; i++) {
          const seg = buffers[i]
          if (seg && seg.byteLength > 0) {
            // Int16Array 视图 → 拷贝出独立的 ArrayBuffer，避免后续被 recorder-core 复用篡改
            const copy = new Uint8Array(seg.byteLength)
            copy.set(new Uint8Array(seg.buffer, seg.byteOffset, seg.byteLength))
            this._opts.onPcmChunk(copy)
          }
        }
        this._lastBufferIdx = newBufferIdx
        this._opts.onPower?.(powerLevel)
      },
    })

    await new Promise<void>((resolve, reject) => {
      rec.open(
        () => {
          this._rec = rec
          this._started = true
          this._lastBufferIdx = 0
          rec.start()
          resolve()
        },
        (msg: string, isUserNotAllow: boolean) => {
          const err = new Error(
            isUserNotAllow ? `permission_denied: ${msg}` : `recorder_open_failed: ${msg}`,
          )
          this._opts.onError?.(err)
          reject(err)
        },
      )
    })
  }

  async stop(): Promise<void> {
    const rec = this._rec
    if (!rec || !this._started) return
    this._started = false

    await new Promise<void>((resolve) => {
      rec.stop(
        () => resolve(),
        () => resolve(),
        true,
      )
    })
    rec.close()
    this._rec = null
  }
}

// recorder-core 没有 ESM 入口，CDN / dynamic import 都要拼装；这里用动态 import
// 让 vite 走 commonjs 互操作。模块本体把 Recorder 挂到全局 Recorder 上。
let _recorderCorePromise: Promise<RecorderInstance> | null = null

function loadRecorderCore(): Promise<RecorderInstance> {
  if (_recorderCorePromise) return _recorderCorePromise
  _recorderCorePromise = (async () => {
    // eslint-disable-next-line @typescript-eslint/ban-ts-comment
    // @ts-ignore — recorder-core 没类型声明
    const mod = await import('recorder-core')
    // eslint-disable-next-line @typescript-eslint/ban-ts-comment
    // @ts-ignore
    await import('recorder-core/src/engine/pcm')
    return (mod.default ?? mod) as RecorderInstance
  })()
  return _recorderCorePromise
}
