import { LitElement, html, css } from 'lit'
import { customElement, property } from 'lit/decorators.js'
import { sharedStyles } from '../styles/theme'
import type { ChatStore } from '../stores/chat-store'
import './message-list'
import './input-bar'

@customElement('chat-panel')
export class ChatPanel extends LitElement {
  static styles = [
    sharedStyles,
    css`
      :host {
        display: none;
        position: fixed;
        z-index: 2147483646;
        flex-direction: column;
        width: 380px;
        height: 70vh;
        max-height: 600px;
        min-height: 400px;
        background: var(--ra-bg);
        border-radius: var(--ra-radius);
        box-shadow: var(--ra-shadow);
        overflow: hidden;
      }

      :host([open]) {
        display: flex;
      }

      :host([position='bottom-right']) {
        bottom: 96px;
        right: 24px;
      }
      :host([position='bottom-left']) {
        bottom: 96px;
        left: 24px;
      }

      .header {
        display: flex;
        align-items: center;
        justify-content: space-between;
        padding: 14px 16px;
        background: var(--ra-primary);
        color: #fff;
      }
      .header-title {
        font-size: 15px;
        font-weight: 600;
      }
      .header-status {
        font-size: 11px;
        opacity: 0.8;
        margin-top: 2px;
      }
      .btn-close {
        width: 28px;
        height: 28px;
        border: none;
        background: rgba(255, 255, 255, 0.2);
        border-radius: 50%;
        cursor: pointer;
        display: flex;
        align-items: center;
        justify-content: center;
        transition: background 0.2s;
      }
      .btn-close:hover {
        background: rgba(255, 255, 255, 0.35);
      }
      .btn-close svg {
        width: 14px;
        height: 14px;
        color: #fff;
      }

      message-list {
        flex: 1;
        min-height: 0;
        overflow-y: auto;
      }

      @media (max-width: 480px) {
        :host([open]) {
          width: 100vw;
          height: 100dvh;
          max-height: none;
          bottom: 0;
          right: 0;
          left: 0;
          border-radius: 0;
        }
      }
    `,
  ]

  @property({ type: Boolean, reflect: true }) open = false
  @property({ reflect: true }) position: 'bottom-right' | 'bottom-left' = 'bottom-right'
  @property({ attribute: false, hasChanged: () => true }) store!: ChatStore

  private _stateLabels: Record<string, string> = {
    GREETING: '等待您的描述',
    COLLECTING: '正在收集报修信息...',
    WAITING_IMAGE: '等待上传现场照片',
    CONFIRMING: '请确认报修信息',
    COMPLETED: '工单已生成',
    ESCALATED: '已转接人工服务',
  }

  render() {
    const st = this.store
    return html`
      <div class="header">
        <div>
          <div class="header-title">设施报修助手</div>
          <div class="header-status">${this._stateLabels[st.agentState] ?? ''}</div>
        </div>
        <button class="btn-close" @click=${this._onClose}>
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"
               stroke-linecap="round" stroke-linejoin="round">
            <line x1="18" y1="6" x2="6" y2="18"></line>
            <line x1="6" y1="6" x2="18" y2="18"></line>
          </svg>
        </button>
      </div>
      <message-list
        .messages=${st.messages}
        ?streaming=${st.isStreaming}
      ></message-list>
      <input-bar
        ?disabled=${st.isStreaming}
        @send-text=${this._onSendText}
        @send-image=${this._onSendImage}
      ></input-bar>
    `
  }

  private _onClose() {
    this.dispatchEvent(new CustomEvent('panel-close', { bubbles: true, composed: true }))
  }

  private _onSendText(e: CustomEvent<string>) {
    this.store.sendText(e.detail)
  }

  private _onSendImage(e: CustomEvent<{ file: File; text: string | null }>) {
    this.store.sendImage(e.detail.file, e.detail.text ?? undefined)
  }
}
