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
        margin: 8px 16px;
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
        padding: 10px 14px;
        border-radius: 12px;
        word-break: break-word;
        white-space: pre-wrap;
        line-height: 1.6;
        font-size: 14px;
      }
      :host([role='bot']) .bubble {
        background: var(--ra-bg-secondary);
        color: var(--ra-text);
        border-bottom-left-radius: 4px;
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

    return html`
      <div class="avatar ${this.role}">${avatarLabel}</div>
      <div class="bubble">
        ${this.msg.imageUrl
          ? html`<img src=${this.msg.imageUrl} alt="报修图片" @click=${this._previewImage} />`
          : null}
        ${this.msg.type !== 'image' || this.msg.content
          ? html`${this.msg.content}${this.streaming ? html`<span class="cursor"></span>` : null}`
          : null}
      </div>
    `
  }

  private _previewImage() {
    if (this.msg.imageUrl) window.open(this.msg.imageUrl, '_blank')
  }
}
