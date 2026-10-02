/**
 * MCP Apps (unit M9): the isolation contract for a `ui://` app document.
 *
 * An app is server-authored HTML. It runs in an `<iframe srcdoc>` whose only
 * sandbox token is `allow-scripts`: without `allow-same-origin` the document
 * gets an opaque origin, so it cannot reach the host's DOM, cookies, storage
 * or IPC bridge, and every other capability (top navigation, popups, forms,
 * modals, downloads, pointer lock) stays off. The CSP below is injected as the
 * first node of the document, ahead of any server markup, and denies all
 * network: the app can only talk to the host through the validated
 * postMessage bridge (./bridge.ts).
 */

/** The exact `sandbox` attribute. Never add `allow-same-origin`: combined
 *  with `allow-scripts` it lets the frame remove its own sandbox. */
export const MCP_APP_SANDBOX = 'allow-scripts'

/** The exact CSP injected into every app document. */
export const MCP_APP_CSP = [
  "default-src 'none'",
  "script-src 'unsafe-inline'",
  "style-src 'unsafe-inline'",
  'img-src data:',
  "connect-src 'none'",
  "frame-src 'none'",
  "form-action 'none'",
  "base-uri 'none'"
].join('; ')

/** Permissions Policy for the frame: every powerful feature explicitly off. */
export const MCP_APP_PERMISSIONS_POLICY = [
  'camera',
  'microphone',
  'geolocation',
  'display-capture',
  'clipboard-read',
  'clipboard-write',
  'fullscreen',
  'payment',
  'usb',
  'serial',
  'hid',
  'bluetooth'
]
  .map(feature => `${feature} 'none'`)
  .join('; ')

/** Largest app document (UTF-8 bytes) the host will render. */
export const MCP_APP_MAX_HTML_BYTES = 512 * 1024

/** Host theme values handed to the app as `--hermes-*` CSS variables. */
export const MCP_APP_THEME_VARS = [
  'background',
  'foreground',
  'muted-foreground',
  'border',
  'primary',
  'font-sans'
] as const

export type McpAppThemeVar = (typeof MCP_APP_THEME_VARS)[number]
export type McpAppTheme = Partial<Record<McpAppThemeVar, string>>

export type McpAppSrcdoc = { ok: true; srcdoc: string } | { ok: false; reason: 'empty' | 'too_large' }

// A theme value is interpolated into a <style> block, so it may only contain
// characters a color/font value needs: no `<`, `;`, braces, quotes or escapes.
const SAFE_THEME_VALUE = /^[\w\s#%.,()+\-/]{1,120}$/

/** Keep only well-formed theme values; anything else is dropped, not escaped. */
export function sanitizeMcpAppTheme(theme: McpAppTheme | undefined): McpAppTheme {
  const out: McpAppTheme = {}

  for (const key of MCP_APP_THEME_VARS) {
    const value = theme?.[key]?.trim()

    if (value && SAFE_THEME_VALUE.test(value)) {
      out[key] = value
    }
  }

  return out
}

/** Read the host's current theme tokens from the document root. */
export function readHostMcpAppTheme(root: Element | null = globalThis.document?.documentElement ?? null): McpAppTheme {
  if (!root || typeof getComputedStyle !== 'function') {
    return {}
  }

  const style = getComputedStyle(root)
  const theme: McpAppTheme = {}

  for (const key of MCP_APP_THEME_VARS) {
    const value = style.getPropertyValue(`--${key}`).trim()

    if (value) {
      theme[key] = value
    }
  }

  return sanitizeMcpAppTheme(theme)
}

export function mcpAppHtmlBytes(html: string): number {
  return new TextEncoder().encode(html).byteLength
}

// Reports the document height to the host so the frame can size itself
// without the host ever reading the frame. Runs under the injected CSP.
const RESIZE_REPORTER =
  '(function(){var last=-1;function report(){var h=Math.ceil(document.documentElement.scrollHeight);' +
  "if(h!==last){last=h;parent.postMessage({type:'resize',height:h},'*')}}" +
  "addEventListener('load',report);try{new ResizeObserver(report).observe(document.documentElement)}catch(e){}})()"

/**
 * Wrap server HTML into the srcdoc the frame renders. The CSP meta is the
 * very first element (it lands first in the implicit <head>), so it governs
 * every script and stylesheet the server markup contains. A later CSP meta
 * from the app can only add restrictions — browsers enforce every policy.
 */
export function buildMcpAppSrcdoc(html: string, theme?: McpAppTheme): McpAppSrcdoc {
  if (!html.trim()) {
    return { ok: false, reason: 'empty' }
  }

  if (mcpAppHtmlBytes(html) > MCP_APP_MAX_HTML_BYTES) {
    return { ok: false, reason: 'too_large' }
  }

  const vars = Object.entries(sanitizeMcpAppTheme(theme))
    .map(([key, value]) => `--hermes-${key}:${value}`)
    .join(';')

  const head =
    '<!doctype html>' +
    `<meta http-equiv="Content-Security-Policy" content="${MCP_APP_CSP}">` +
    '<meta charset="utf-8">' +
    '<meta name="referrer" content="no-referrer">' +
    `<style>:root{color-scheme:light dark;${vars}}` +
    'html,body{margin:0;background:transparent;color:var(--hermes-foreground,inherit);' +
    'font-family:var(--hermes-font-sans,system-ui,sans-serif);font-size:13px}</style>' +
    `<script>${RESIZE_REPORTER}</script>`

  return { ok: true, srcdoc: head + html }
}
