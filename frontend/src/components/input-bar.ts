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
        flex-direction: column;
        border-top: 1px solid var(--ra-border);
        background: var(--ra-bg);
      }

      .preview-bar {
        display: flex;
        align-items: center;
        gap: 8px;
        padding: 8px 16px 0;
      }

      .preview-wrap {
        position: relative;
        width: 56px;
        height: 56px;
        flex-shrink: 0;
      }

      .preview-wrap img {
        width: 56px;
        height: 56px;
        object-fit: cover;
        border-radius: 6px;
        border: 1px solid var(--ra-border);
      }

      .btn-remove {
        position: absolute;
        top: -6px;
        right: -6px;
        width: 18px;
        height: 18px;
        border-radius: 50%;
        border: none;
        background: #ff4d4f;
        color: #fff;
        font-size: 11px;
        line-height: 18px;
        text-align: center;
        cursor: pointer;
        padding: 0;
      }

      .input-row {
        display: flex;
        align-items: center;
        gap: 8px;
        padding: 12px 16px;
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

      input[type='text'] {
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
      input[type='text']:focus {
        border-color: var(--ra-primary);
      }
      input[type='text']::placeholder {
        color: var(--ra-text-secondary);
      }
      input[type='text'].interim {
        color: var(--ra-text-secondary);
        font-style: italic;
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

      .action-sheet-overlay {
        position: fixed;
        inset: 0;
        background: rgba(0, 0, 0, 0.4);
        z-index: 9999;
        display: flex;
        align-items: flex-end;
        justify-content: center;
      }

      .action-sheet {
        background: var(--ra-bg);
        border-radius: 16px 16px 0 0;
        padding: 8px 0 env(safe-area-inset-bottom, 0);
        width: 100%;
        max-width: 480px;
      }

      .action-sheet-title {
        text-align: center;
        font-size: 13px;
        color: var(--ra-text-secondary);
        padding: 12px 16px 8px;
      }

      .action-sheet-btn {
        display: flex;
        align-items: center;
        gap: 12px;
        width: 100%;
        padding: 16px 24px;
        border: none;
        background: none;
        font-size: 16px;
        color: var(--ra-text);
        cursor: pointer;
        font-family: var(--ra-font);
        border-top: 1px solid var(--ra-border);
      }

      .action-sheet-btn:first-of-type {
        border-top: none;
      }

      .action-sheet-btn svg {
        width: 22px;
        height: 22px;
        color: var(--ra-primary);
        flex-shrink: 0;
      }

      .action-sheet-cancel {
        display: block;
        width: calc(100% - 32px);
        margin: 8px 16px;
        padding: 14px;
        border: none;
        border-radius: 12px;
        background: var(--ra-bg-secondary);
        font-size: 16px;
        font-weight: 500;
        color: var(--ra-text);
        cursor: pointer;
        font-family: var(--ra-font);
      }

      .btn-mic.recording {
        background: #ff4d4f;
      }
      .btn-mic.recording svg {
        color: #fff;
      }
    `,
  ]

  @state() private _text = ''
  @state() private _recording = false
  @state() private _pendingImage: File | null = null
  @state() private _previewUrl: string | null = null
  @state() private _showActionSheet = false
  @property({ type: Boolean }) disabled = false

  private _speech: SpeechService | null = null

  connectedCallback() {
    super.connectedCallback()
    if (speechSupported) {
      this._speech = new SpeechService({
        onInterim: (text) => {
          this._text = text
        },
        onFinal: (text) => {
          this._recording = false
          if (text.trim()) {
            this._text = text.trim()
          }
        },
        onEnd: () => {
          this._recording = false
        },
      })
    }
  }

  disconnectedCallback() {
    super.disconnectedCallback()
    if (this._previewUrl) URL.revokeObjectURL(this._previewUrl)
  }

  render() {
    const placeholder = this._recording ? '正在聆听...' : '请输入您的问题...'
    const canSend = !this.disabled && (!!this._text.trim() || !!this._pendingImage)

    return html`
      ${this._showActionSheet
        ? html`
            <div class="action-sheet-overlay" @click=${this._closeActionSheet}>
              <div class="action-sheet" @click=${(e: Event) => e.stopPropagation()}>
                <div class="action-sheet-title">上传图片</div>
                <button class="action-sheet-btn" @click=${this._triggerCamera}>
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
                       stroke-linecap="round" stroke-linejoin="round">
                    <path d="M23 19a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4l2-3h6l2 3h4a2 2 0 0 1 2 2z"></path>
                    <circle cx="12" cy="13" r="4"></circle>
                  </svg>
                  拍照上传
                </button>
                <button class="action-sheet-btn" @click=${this._triggerGallery}>
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
                       stroke-linecap="round" stroke-linejoin="round">
                    <rect x="3" y="3" width="18" height="18" rx="2" ry="2"></rect>
                    <circle cx="8.5" cy="8.5" r="1.5"></circle>
                    <polyline points="21 15 16 10 5 21"></polyline>
                  </svg>
                  从相册选择
                </button>
                <button class="action-sheet-cancel" @click=${this._closeActionSheet}>取消</button>
              </div>
            </div>
          `
        : nothing}

      ${this._pendingImage
        ? html`
            <div class="preview-bar">
              <div class="preview-wrap">
                <img src=${this._previewUrl!} alt="待发图片" />
                <button class="btn-remove" @click=${this._removePendingImage} title="移除图片">x</button>
              </div>
            </div>
          `
        : nothing}

      <div class="input-row">
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
        <button class="btn-icon" @click=${this._openActionSheet} title="拍照/选图">
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
          .value=${this._text}
          @input=${this._onInput}
          @keydown=${this._onKeydown}
          ?disabled=${this.disabled || this._recording}
        />
        <button
          class="btn-send"
          @click=${this._onSend}
          ?disabled=${!canSend}
          title="发送"
        >
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
               stroke-linecap="round" stroke-linejoin="round">
            <line x1="22" y1="2" x2="11" y2="13"></line>
            <polygon points="22 2 15 22 11 13 2 9 22 2"></polygon>
          </svg>
        </button>
        <input
          id="file-camera"
          type="file"
          accept="image/*"
          capture="environment"
          @change=${this._onFileChange}
        />
        <input
          id="file-gallery"
          type="file"
          accept="image/*"
          @change=${this._onFileChange}
        />
      </div>
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
    if ((!text && !this._pendingImage) || this.disabled) return

    if (this._pendingImage) {
      this.dispatchEvent(
        new CustomEvent('send-image', {
          detail: { file: this._pendingImage, text: text || null },
          bubbles: true,
          composed: true,
        }),
      )
      this._clearPendingImage()
    } else {
      this.dispatchEvent(
        new CustomEvent('send-text', { detail: text, bubbles: true, composed: true }),
      )
    }
    this._text = ''
  }

  private _openActionSheet() {
    this._showActionSheet = true
  }

  private _closeActionSheet() {
    this._showActionSheet = false
  }

  private _triggerCamera() {
    this._showActionSheet = false
    const fileInput = this.renderRoot.querySelector('#file-camera') as HTMLInputElement
    fileInput.click()
  }

  private _triggerGallery() {
    this._showActionSheet = false
    const fileInput = this.renderRoot.querySelector('#file-gallery') as HTMLInputElement
    fileInput.click()
  }

  private _onFileChange(e: Event) {
    const input = e.target as HTMLInputElement
    const file = input.files?.[0]
    if (file) {
      if (this._previewUrl) URL.revokeObjectURL(this._previewUrl)
      this._pendingImage = file
      this._previewUrl = URL.createObjectURL(file)
    }
    input.value = ''
  }

  private _removePendingImage() {
    this._clearPendingImage()
  }

  private _clearPendingImage() {
    if (this._previewUrl) {
      URL.revokeObjectURL(this._previewUrl)
      this._previewUrl = null
    }
    this._pendingImage = null
  }

  private _micDown(e: Event) {
    e.preventDefault()
    if (this.disabled || !this._speech) return
    this._recording = true
    this._text = ''
    this._speech.start()
  }

  private _micUp(e: Event) {
    e.preventDefault()
    if (!this._speech || !this._recording) return
    this._speech.stop()
  }
}
