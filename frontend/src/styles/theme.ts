import { css } from 'lit'

export const sharedStyles = css`
  :host {
    --ra-primary: #1677ff;
    --ra-primary-light: #e6f4ff;
    --ra-bg: #ffffff;
    --ra-bg-secondary: #f5f5f5;
    --ra-text: #1f1f1f;
    --ra-text-secondary: #8c8c8c;
    --ra-border: #e8e8e8;
    --ra-shadow: 0 6px 16px rgba(0, 0, 0, 0.12);
    --ra-radius: 12px;
    --ra-font: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial,
      'Noto Sans SC', sans-serif;

    font-family: var(--ra-font);
    font-size: 14px;
    line-height: 1.5;
    color: var(--ra-text);
  }

  * {
    box-sizing: border-box;
    margin: 0;
    padding: 0;
  }
`
