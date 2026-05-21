import { LitElement, html, css, nothing } from 'lit'
import { customElement, property, state } from 'lit/decorators.js'
import { sharedStyles } from '../styles/theme'
import { SpeechService, speechSupported } from '../services/speech'

@customElement('input-bar')
export class InputBar extends LitElement {
  static styles = [
    sharedStyles,
    css`
      :host {
        display: flex;
        align-items: center;
        gap: 8px;
        padding: 12px 16px;
        border-top: 1px solid var(--ra-border);
        background: var(--ra-bg);
      }

      .btn-icon {
        width: 36px;
        height: 36px;
        border-radius: 50%;
        border: none;
        background: var(--ra-bg-secondary);
        cursor: pointer;
        display: flex;
        align-items: center;
        justify-content: center;
        flex-shrink: 0;
        transition: background 0.2s;
      }
      .btn-icon:hover {
        background: var(--ra-border);
      }
      .btn-icon svg {
        width: 18px;
        height: 18px;
        color: var(--ra-text-secondary);
      }

      input {
        flex: 1;
        height: 36px;
        border: 1px solid var(--ra-border);
        border-radius: 18px;
        padding: 0 14px;
        font-size: 14px;
        outline: none;
        font-family: var(--ra-font);
        transition: border-color 0.2s;
      }
      input:focus {
        border-color: var(--ra-primary);
      }
      input::placeholder {
        color: var(--ra-text-secondary);
      }

      .btn-send {
        width: 36px;
        height: 36px;
        border-radius: 50%;
        border: none;
        background: var(--ra-primary);
        cursor: pointer;
        display: flex;
        align-items: center;
        justify-content: center;
        flex-shrink: 0;
        transition: opacity 0.2s;
      }
      .btn-send:disabled {
        opacity: 0.4;
        cursor: not-allowed;
      }
      .btn-send svg {
        width: 18px;
        height: 18px;
        color: #fff;
      }

      input[type='file'] {
        display: none;
      }

      .btn-mic.recording {
        background: #ff4d4f;
      }
      .btn-mic.recording svg {
        color: #fff;
      }

      .interim {
        color: var(--ra-text-secondary);
        font-style: italic;
      }
    `,
  ]

  @state() private _text = ''
  @state() private _interim = ''
  @state() private _recording = false
  @property({ type: Boolean }) disabled = false

  private _speech: SpeechService | null = null

  connectedCallback() {
    super.connectedCallback()
    if (speechSupported) {
      this._speech = new SpeechService({
        onInterim: (text) => {
          this._interim = text
        },
        onFinal: (text) => {
          this._interim = ''
          this._recording = false
          if (text.trim()) {
            this.dispatchEvent(
              new CustomEvent('send-text', { detail: text.trim(), bubbles: true, composed: true }),
            )
          }
        },
        onEnd: () => {
          this._interim = ''
          this._recording = false
        },
      })
    }
  }

  render() {
    const placeholder = this._recording
      ? this._interim || '正在聆听...'
      : '请输入您的问题...'

    return html`
      ${speechSupported
        ? html`
            <button
              class="btn-icon btn-mic ${this._recording ? 'recording' : ''}"
              @mousedown=${this._micDown}
              @mouseup=${this._micUp}
              @mouseleave=${this._micUp}
              @touchstart=${this._micDown}
              @touchend=${this._micUp}
              @touchcancel=${this._micUp}
              ?disabled=${this.disabled}
              title="长按说话"
            >
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
                   stroke-linecap="round" stroke-linejoin="round">
                <path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"></path>
                <path d="M19 10v2a7 7 0 0 1-14 0v-2"></path>
                <line x1="12" y1="19" x2="12" y2="23"></line>
                <line x1="8" y1="23" x2="16" y2="23"></line>
              </svg>
            </button>
          `
        : nothing}
      <button class="btn-icon" @click=${this._triggerCamera} title="拍照/选图">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
             stroke-linecap="round" stroke-linejoin="round">
          <rect x="3" y="3" width="18" height="18" rx="2" ry="2"></rect>
          <circle cx="8.5" cy="8.5" r="1.5"></circle>
          <polyline points="21 15 16 10 5 21"></polyline>
        </svg>
      </button>
      <input
        type="text"
        placeholder=${placeholder}
        class=${this._recording ? 'interim' : ''}
        .value=${this._recording ? '' : this._text}
        @input=${this._onInput}
        @keydown=${this._onKeydown}
        ?disabled=${this.disabled || this._recording}
      />
      <button
        class="btn-send"
        @click=${this._onSend}
        ?disabled=${this.disabled || !this._text.trim()}
        title="发送"
      >
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
             stroke-linecap="round" stroke-linejoin="round">
          <line x1="22" y1="2" x2="11" y2="13"></line>
          <polygon points="22 2 15 22 11 13 2 9 22 2"></polygon>
        </svg>
      </button>
      <input
        type="file"
        accept="image/jpeg,image/png,image/webp"
        capture="environment"
        @change=${this._onFileChange}
      />
    `
  }

  private _onInput(e: Event) {
    this._text = (e.target as HTMLInputElement).value
  }

  private _onKeydown(e: KeyboardEvent) {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
      e.preventDefault()
      this._onSend()
    }
  }

  private _onSend() {
    const text = this._text.trim()
    if (!text || this.disabled) return
    this.dispatchEvent(new CustomEvent('send-text', { detail: text, bubbles: true, composed: true }))
    this._text = ''
  }

  private _triggerCamera() {
    const fileInput = this.renderRoot.querySelector('input[type=file]') as HTMLInputElement
    fileInput.click()
  }

  private _onFileChange(e: Event) {
    const input = e.target as HTMLInputElement
    const file = input.files?.[0]
    if (file) {
      this.dispatchEvent(new CustomEvent('send-image', { detail: file, bubbles: true, composed: true }))
    }
    input.value = ''
  }

  private _micDown(e: Event) {
    e.preventDefault()
    if (this.disabled || !this._speech) return
    this._recording = true
    this._speech.start()
  }

  private _micUp(e: Event) {
    e.preventDefault()
    if (!this._speech || !this._recording) return
    this._speech.stop()
  }
}
