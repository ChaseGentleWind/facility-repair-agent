import { LitElement, html, css } from 'lit'
import { customElement, property } from 'lit/decorators.js'
import { sharedStyles } from '../styles/theme'

@customElement('fab-button')
export class FabButton extends LitElement {
  static styles = [
    sharedStyles,
    css`
      :host {
        display: block;
      }
      button {
        position: relative;
        width: 56px;
        height: 56px;
        border-radius: 50%;
        border: none;
        background: var(--ra-primary);
        color: #fff;
        cursor: pointer;
        box-shadow: 0 4px 12px rgba(0, 0, 0, 0.2);
        transition: transform 0.2s, box-shadow 0.2s;
        display: flex;
        align-items: center;
        justify-content: center;
      }
      button:hover {
        transform: scale(1.08);
        box-shadow: 0 6px 16px rgba(0, 0, 0, 0.28);
      }
      button:active {
        transform: scale(0.96);
      }
      .icon {
        width: 28px;
        height: 28px;
        transition: transform 0.3s;
      }
      :host([open]) .icon {
        transform: rotate(45deg);
      }
      .badge {
        position: absolute;
        top: -2px;
        right: -2px;
        min-width: 18px;
        height: 18px;
        border-radius: 9px;
        background: #ff4d4f;
        color: #fff;
        font-size: 11px;
        font-weight: 600;
        display: flex;
        align-items: center;
        justify-content: center;
        padding: 0 4px;
      }
    `,
  ]

  @property({ type: Number }) unread = 0
  @property({ type: Boolean, reflect: true }) open = false

  render() {
    return html`
      <button @click=${this._onClick} aria-label="报修助手">
        <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
             stroke-linecap="round" stroke-linejoin="round">
          ${this.open
            ? html`<line x1="12" y1="5" x2="12" y2="19"></line>
                   <line x1="5" y1="12" x2="19" y2="12"></line>`
            : html`<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"></path>`}
        </svg>
        ${this.unread > 0
          ? html`<span class="badge">${this.unread > 9 ? '9+' : this.unread}</span>`
          : null}
      </button>
    `
  }

  private _onClick() {
    this.dispatchEvent(new CustomEvent('fab-click', { bubbles: true, composed: true }))
  }
}
