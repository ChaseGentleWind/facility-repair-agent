type SpeechCallback = (text: string) => void

interface SpeechRecognitionLike {
  lang: string
  interimResults: boolean
  continuous: boolean
  onresult: ((e: any) => void) | null
  onerror: ((e: any) => void) | null
  onend: (() => void) | null
  start(): void
  stop(): void
}

const SpeechRecognitionCtor: (new () => SpeechRecognitionLike) | undefined =
  (window as any).SpeechRecognition || (window as any).webkitSpeechRecognition

export const speechSupported = !!SpeechRecognitionCtor

export class SpeechService {
  private _recognition: SpeechRecognitionLike | null = null
  private _onInterim: SpeechCallback
  private _onFinal: SpeechCallback
  private _onEnd: () => void

  recording = false

  constructor(opts: {
    onInterim: SpeechCallback
    onFinal: SpeechCallback
    onEnd: () => void
  }) {
    this._onInterim = opts.onInterim
    this._onFinal = opts.onFinal
    this._onEnd = opts.onEnd
  }

  start() {
    if (!SpeechRecognitionCtor || this.recording) return

    const recognition = new SpeechRecognitionCtor()
    recognition.lang = 'zh-CN'
    recognition.interimResults = true
    recognition.continuous = false

    recognition.onresult = (e: any) => {
      let interim = ''
      let final = ''
      for (let i = e.resultIndex; i < e.results.length; i++) {
        const transcript = e.results[i][0].transcript
        if (e.results[i].isFinal) {
          final += transcript
        } else {
          interim += transcript
        }
      }
      if (final) {
        this._onFinal(final)
      } else if (interim) {
        this._onInterim(interim)
      }
    }

    recognition.onerror = () => {
      this._stop()
    }

    recognition.onend = () => {
      this._stop()
    }

    this._recognition = recognition
    this.recording = true
    recognition.start()
  }

  stop() {
    this._recognition?.stop()
  }

  private _stop() {
    this.recording = false
    this._recognition = null
    this._onEnd()
  }
}
