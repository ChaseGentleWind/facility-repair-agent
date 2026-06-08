import { LitElement, html, css } from 'lit'
import { customElement, property } from 'lit/decorators.js'
import { sharedStyles } from '../styles/theme'
import type { ChatMessage } from '../types'

@customElement('message-bubble')
export class MessageBubble extends LitElement {
  static styles = [
    sharedStyles,
    css`
      :host {
        display: flex;
        margin: 10px 16px;
        align-items: flex-start;
      }
      :host([role='user']) {
        justify-content: flex-end;
      }

      .avatar {
        width: 32px;
        height: 32px;
        border-radius: 50%;
        flex-shrink: 0;
        display: flex;
        align-items: center;
        justify-content: center;
        font-size: 14px;
        color: #fff;
      }
      .avatar.bot {
        background: var(--ra-primary);
        margin-right: 8px;
      }
      .avatar.user {
        background: #52c41a;
        margin-left: 8px;
        order: 1;
      }

      .bubble {
        max-width: 75%;
        padding: 8px 12px;
        border-radius: 12px;
        word-break: break-word;
        white-space: pre-wrap;
        line-height: 1.5;
        font-size: 14px;
        min-width: 60px;
      }
      .bubble.confirm {
        width: min(280px, 100%);
        max-width: 82%;
        padding: 0;
        overflow: hidden;
        background: #fff;
        border: 1px solid var(--ra-border);
        white-space: normal;
      }
      :host([role='bot']) .bubble {
        background: var(--ra-bg-secondary);
        color: var(--ra-text);
        border-bottom-left-radius: 4px;
      }
      :host([role='bot']) .bubble.confirm {
        background: #fff;
      }
      :host([role='user']) .bubble {
        background: var(--ra-primary);
        color: #fff;
        border-bottom-right-radius: 4px;
      }

      .bubble img {
        max-width: 200px;
        max-height: 200px;
        border-radius: 8px;
        cursor: pointer;
        display: block;
        margin-bottom: 6px;
      }

      .bubble img:only-child {
        margin-bottom: 0;
      }
      .confirm-header {
        padding: 10px 12px 8px;
        border-bottom: 1px solid var(--ra-border);
      }
      .confirm-title {
        font-size: 14px;
        font-weight: 700;
        color: var(--ra-text);
        line-height: 1.3;
      }
      .confirm-subtitle {
        margin-top: 2px;
        font-size: 12px;
        color: var(--ra-text-secondary);
      }
      .confirm-body {
        padding: 10px 12px;
        display: grid;
        gap: 8px;
      }
      .confirm-row {
        display: grid;
        grid-template-columns: 64px minmax(0, 1fr);
        gap: 8px;
        align-items: start;
      }
      .confirm-label {
        font-size: 12px;
        color: var(--ra-text-secondary);
        line-height: 1.5;
      }
      .confirm-value {
        font-size: 13px;
        color: var(--ra-text);
        line-height: 1.5;
        overflow-wrap: anywhere;
      }
      .confirm-images {
        display: grid;
        grid-template-columns: repeat(3, 1fr);
        gap: 6px;
        margin-top: 2px;
      }
      .confirm-images img {
        width: 100%;
        aspect-ratio: 1;
        max-width: none;
        max-height: none;
        object-fit: cover;
        border-radius: 6px;
        margin: 0;
        border: 1px solid var(--ra-border);
      }
      .confirm-actions {
        display: grid;
        grid-template-columns: 1fr 1fr;
        gap: 8px;
        padding: 10px 12px 12px;
        border-top: 1px solid var(--ra-border);
        background: var(--ra-bg-secondary);
      }
      .confirm-actions button {
        height: 32px;
        border-radius: 6px;
        border: 1px solid var(--ra-border);
        background: #fff;
        color: var(--ra-text);
        font-size: 13px;
        font-weight: 600;
        cursor: pointer;
      }
      .confirm-actions button.primary {
        border-color: var(--ra-primary);
        background: var(--ra-primary);
        color: #fff;
      }
      .confirm-actions button:hover {
        filter: brightness(0.98);
      }

      .cursor {
        display: inline-block;
        width: 2px;
        height: 1em;
        background: var(--ra-text-secondary);
        margin-left: 2px;
        animation: blink 0.8s step-end infinite;
        vertical-align: text-bottom;
      }

      @keyframes blink {
        50% { opacity: 0; }
      }
    `,
  ]

  @property({ attribute: 'role', reflect: true }) role: 'user' | 'bot' = 'bot'
  @property({ attribute: false }) msg!: ChatMessage
  @property({ type: Boolean }) streaming = false

  render() {
    const isUser = this.role === 'user'
    const avatarLabel = isUser ? 'U' : 'A'
    if (this.msg.type === 'confirm_card' && this.msg.draftConfirm) {
      return html`
        <div class="avatar ${this.role}">${avatarLabel}</div>
        ${this._renderConfirmCard()}
      `
    }

    const content = (this.msg.content ?? '')
      .trim()
      .replace(/\n{2,}/g, '\n')

    return html`
      <div class="avatar ${this.role}">${avatarLabel}</div>
      <div class="bubble">${this.msg.imageUrl
          ? html`<img src=${this.msg.imageUrl} alt="报修图片" @click=${this._previewImage} />`
          : null}${this.msg.type !== 'image' || this.msg.content
          ? html`${content}${this.streaming ? html`<span class="cursor"></span>` : null}`
          : null}</div>
    `
  }

  private _renderConfirmCard() {
    const draft = this.msg.draftConfirm!
    const loc = draft.location ?? {}
    const location = [
      loc.estate,
      loc.building,
      loc.floor,
      loc.area || loc.room,
    ].filter(Boolean).join(' ') || '暂未提供'
    const description = draft.description || '暂未提供'
    const visitTime = draft.visit_time || '暂未提供'
    const images = draft.image_urls ?? []

    return html`
      <div class="bubble confirm">
        <div class="confirm-header">
          <div class="confirm-title">报修信息确认</div>
          <div class="confirm-subtitle">确认无误后生成工单预览</div>
        </div>
        <div class="confirm-body">
          <div class="confirm-row">
            <div class="confirm-label">位置</div>
            <div class="confirm-value">${location}</div>
          </div>
          <div class="confirm-row">
            <div class="confirm-label">问题</div>
            <div class="confirm-value">${description}</div>
          </div>
          <div class="confirm-row">
            <div class="confirm-label">上门时间</div>
            <div class="confirm-value">${visitTime}</div>
          </div>
          ${images.length > 0 ? html`
            <div class="confirm-images">
              ${images.map((url) => html`
                <img src=${url} alt="报修图片" @click=${() => this._previewUrl(url)} />
              `)}
            </div>
          ` : null}
        </div>
        <div class="confirm-actions">
          <button type="button" @click=${this._onModify}>修改</button>
          <button type="button" class="primary" @click=${this._onGeneratePreview}>生成预览</button>
        </div>
      </div>
    `
  }

  private _previewImage() {
    if (this.msg.imageUrl) window.open(this.msg.imageUrl, '_blank')
  }

  private _previewUrl(url: string) {
    window.open(url, '_blank')
  }

  private _onModify() {
    this.dispatchEvent(new CustomEvent('draft-modify', { bubbles: true, composed: true }))
  }

  private _onGeneratePreview() {
    this.dispatchEvent(new CustomEvent('draft-generate-preview', { bubbles: true, composed: true }))
  }
}
