import { useStore } from '@nanostores/react'
import { useEffect, useMemo, useState } from 'react'

import { requestComposerSubmit } from '@/app/chat/composer/focus'
import { useSessionView } from '@/app/chat/session-view'
import { PreviewAttachment } from '@/components/chat/preview-attachment'
import { onThemeRepaint } from '@/hooks/use-theme-epoch'
import { readDesktopFileText } from '@/lib/desktop-fs'
import { localPreviewTarget } from '@/lib/local-preview'

/**
 * `::preview{file="…"}` — a workspace HTML file rendered LIVE inside the
 * assistant message. A sandboxed iframe with an opaque origin
 * (`sandbox="allow-scripts"`, deliberately no `allow-same-origin`): scripts
 * run and the widget is fully interactive, but the document cannot reach the
 * app, its cookies, storage, or the bridge. The doc arrives via `srcdoc`
 * from a bridge file read, so single-file HTML (what agents generate) is
 * fully live; relative sibling assets don't resolve in an opaque origin.
 *
 * SIZE IS CONTENT-DRIVEN. The opaque origin means the parent can't measure
 * the document, but we own the srcdoc string — an injected script posts the
 * content's size up via postMessage (tagged with a per-mount token). Height
 * tracks live within the clamp band; width adopts ONCE from the first
 * report, so a fixed-size widget shrink-wraps and sits left in the message
 * flow like an image, while a fluid page measures the full viewport and
 * stays column-wide. A `height="480"` attribute only sets the starting
 * height — measurement always wins.
 *
 * NATIVE BY DEFAULT. A theme prelude injects first: the app's color scheme,
 * the app's resolved theme tokens under friendly names (--foreground,
 * --muted-foreground, --accent, --border, --card), the app font, zero body
 * margin/padding, and a transparent background — so widget-shaped content
 * reads as part of the app. The page's own styles override all of it, so a
 * full page keeps its own design. The prelude re-resolves on every theme
 * repaint, so a light/dark switch re-themes a live frame.
 *
 * The color scheme is load-bearing, not cosmetic: when an iframe element's
 * used `color-scheme` differs from its document root's, Chromium paints the
 * frame's canvas an OPAQUE Canvas color (CSS Color Adjust §2.2) — a frame
 * doc left at the default (light) inside the dark app renders on solid
 * white, and `background:transparent` can't undo it. The element and the
 * document therefore always get the SAME scheme, from one resolution.
 *
 * CLIPPED LIKE ANY MESSAGE CONTENT. The frame box is its own clip, paint
 * containment and stacking boundary, and the iframe sits in flow inside it
 * (no absolutely positioned layer), so the transcript scroller's clip
 * bounds it and the composer dock (z-30, outside the transcript) paints
 * over it.
 *
 * WIDGETS TALK BACK OFF-SCREEN. `window.hermes.send(prompt)` (or declarative
 * `data-hermes-send` on any clickable element) routes the prompt through the
 * composer's send path as a user turn typed `display_kind=hidden`: the agent
 * wakes and the durable row exists (context, resume, audit via the DB), but
 * no bubble renders — the widget updating is the visible response. Token-
 * gated, length-capped, throttled to human speed.
 *
 * Non-HTML targets and remote gateways (no local file access) fall back to
 * the standard preview-attachment card rather than a broken frame.
 */

const MIN_HEIGHT = 120
const MAX_HEIGHT = 1200
const DEFAULT_HEIGHT = 280
/** The transcript column cap the frame renders inside (`max-w-160` = 40rem). */
const MAX_COLUMN_WIDTH = 640
/** Ignore sub-pixel/rounding churn so a vh-sized page can't oscillate. */
const RESIZE_TOLERANCE = 4

export function directiveFrameHeight(raw: string | undefined): number | null {
  if (!raw) {
    return null
  }

  const parsed = Number(raw)

  if (!Number.isInteger(parsed)) {
    return null
  }

  return Math.min(MAX_HEIGHT, Math.max(MIN_HEIGHT, parsed))
}

const SIZE_MESSAGE_TYPE = 'hermes-inline-preview-size'
const INTENT_MESSAGE_TYPE = 'hermes-inline-preview-intent'

/** Prompt length cap for a widget intent — a sentence, not a payload dump. */
const MAX_INTENT_LENGTH = 500
/** One intent per frame per second; clicks are human-speed. */
const INTENT_THROTTLE_MS = 1000

/** The script that gives the widget its ONE voice: `hermes.send(prompt)`.
 *  Posts the prompt up tagged with the mount token; the parent validates,
 *  throttles, and routes it through the composer as a normal user message —
 *  the widget speaks WITH the user's voice, visibly, never silently. Also
 *  wires `data-hermes-send` so declarative HTML works with zero script:
 *  `<button data-hermes-send="get-price eth">ETH</button>`. */
export function intentScript(token: string): string {
  return (
    '<script>(function(){var t=' +
    JSON.stringify(token) +
    ';function send(p){if(typeof p!=="string"||!p.trim())return false;' +
    'parent.postMessage({type:' +
    JSON.stringify(INTENT_MESSAGE_TYPE) +
    ',token:t,prompt:p.slice(0,' +
    String(MAX_INTENT_LENGTH) +
    ')},"*");return true}' +
    'window.hermes={send:send};' +
    'addEventListener("click",function(e){var el=e.target&&e.target.closest?' +
    'e.target.closest("[data-hermes-send]"):null;' +
    'if(el)send(el.getAttribute("data-hermes-send")||"")},true)})()</script>'
  )
}

/** Parse a widget intent. Null unless it is OUR type with OUR token and a
 *  non-empty string prompt — same trust boundary as size reports, because
 *  this one turns into a user message. Trimmed and length-capped. */
export function intentFromMessage(data: unknown, token: string): string | null {
  if (typeof data !== 'object' || data === null) {
    return null
  }

  const message = data as { type?: unknown; token?: unknown; prompt?: unknown }

  if (message.type !== INTENT_MESSAGE_TYPE || message.token !== token || typeof message.prompt !== 'string') {
    return null
  }

  const prompt = message.prompt.trim().slice(0, MAX_INTENT_LENGTH)

  return prompt || null
}

/** Semantic tokens handed into the frame, resolved to concrete values from
 *  the LIVE theme. Friendly names, not internal ones — this is the contract
 *  reference HTML / skills write against (`var(--foreground)` etc.). */
const THEME_BRIDGE_TOKENS: Record<string, string> = {
  '--foreground': '--ui-text-primary',
  '--muted-foreground': '--ui-text-tertiary',
  '--accent': '--ui-accent',
  '--border': '--ui-stroke-tertiary',
  '--card': '--ui-bg-editor'
}

/** Resolve the bridge tokens + app font against the current document. */
export function collectThemeBridge(): { vars: Record<string, string>; font: string } {
  const vars: Record<string, string> = {}

  if (typeof document !== 'undefined') {
    const root = getComputedStyle(document.documentElement)

    for (const [alias, source] of Object.entries(THEME_BRIDGE_TOKENS)) {
      const value = root.getPropertyValue(source).trim()

      if (value) {
        vars[alias] = value
      }
    }
  }

  const font = typeof document === 'undefined' ? '' : getComputedStyle(document.body).fontFamily

  return { vars, font }
}

export type FrameColorScheme = 'dark' | 'light'

/** The scheme the app is PAINTING: themes/context.tsx sets `color-scheme`
 *  and the `dark` class on <html> from the same rendered mode, so the class
 *  is the scheme the iframe element inherits. */
export function appColorScheme(): FrameColorScheme {
  return typeof document !== 'undefined' && document.documentElement.classList.contains('dark') ? 'dark' : 'light'
}

/**
 * The style prelude that makes an inline widget read as NATIVE: the app's
 * color scheme, its resolved theme tokens as CSS vars, the app font, no
 * margin, and a transparent background so the widget sits directly on the
 * chat surface. Injected FIRST, so the page's own styles override every
 * default here — a full page that wants its own look keeps it.
 *
 * `scheme` MUST equal the iframe element's `color-scheme`: a mismatch makes
 * Chromium paint the frame canvas opaque (white, for a light doc in a dark
 * app), which no background rule inside the frame can clear.
 */
export function themePrelude(vars: Record<string, string>, font: string, scheme: FrameColorScheme): string {
  const tokens = [`color-scheme:${scheme}`, ...Object.entries(vars).map(([name, value]) => `${name}:${value}`)].join(
    ';'
  )

  const fontRule = font ? `font-family:${font};` : ''

  return (
    `<style>:root{${tokens}}` +
    `html,body{margin:0;padding:0;background:transparent;color:var(--foreground,inherit);${fontRule}}</style>`
  )
}

export interface FrameTheme {
  scheme: FrameColorScheme
  /** The srcdoc prelude for `scheme` and the live tokens. A string, so an
   *  unchanged theme compares equal and never reloads the frame. */
  prelude: string
}

/** Resolve scheme + prelude together from the live document, so the element
 *  and the document can never be handed different schemes. */
export function resolveFrameTheme(): FrameTheme {
  const scheme = appColorScheme()
  const { vars, font } = collectThemeBridge()

  return { scheme, prelude: themePrelude(vars, font, scheme) }
}

/** The script injected into the srcdoc that reports content size to the
 *  parent. Runs inside the opaque origin, so postMessage is its only door —
 *  it can say "I am N pixels" and nothing else. Height is the document
 *  scrollHeight; width is the union of the body children's boxes (intrinsic
 *  content width — the document itself always fills the viewport, so
 *  scrollWidth would just echo the frame back). */
export function measurementScript(token: string): string {
  return (
    '<script>(function(){var t=' +
    JSON.stringify(token) +
    ';var lastH=0,lastW=0;function post(){var d=document.documentElement;var b=document.body;' +
    'var h=Math.max(d?d.scrollHeight:0,b?b.scrollHeight:0);' +
    'var w=0;if(b){var kids=b.children;var L=Infinity,R=0;for(var i=0;i<kids.length;i++){' +
    'var r=kids[i].getBoundingClientRect();if(r.width===0&&r.height===0)continue;' +
    'if(r.left<L)L=r.left;if(r.right>R)R=r.right}' +
    'if(R>L)w=R-L}' +
    'w=Math.ceil(w);' +
    'if(Math.abs(h-lastH)>1||Math.abs(w-lastW)>1){lastH=h;lastW=w;parent.postMessage({type:' +
    JSON.stringify(SIZE_MESSAGE_TYPE) +
    ',token:t,height:h,width:w},"*")}}' +
    'if(typeof ResizeObserver==="function"){var ro=new ResizeObserver(post);' +
    'ro.observe(document.documentElement);if(document.body)ro.observe(document.body)}' +
    'addEventListener("load",post);post()})()</script>'
  )
}

/** Assemble the srcdoc: theme prelude first (so the page's own styles win),
 *  then the measuring + intent scripts before `</body>` when present so they
 *  run after the page's own markup, appended otherwise. */
export function withInlineChrome(doc: string, token: string, prelude: string): string {
  const script = measurementScript(token) + intentScript(token)
  const bodyClose = /<\/body\s*>/i.exec(doc)
  const framed = bodyClose ? doc.slice(0, bodyClose.index) + script + doc.slice(bodyClose.index) : doc + script

  return prelude + framed
}

export interface FrameSizeReport {
  height: number
  /** Intrinsic content width, 0 when unmeasurable. */
  width: number
}

/** Parse a size report from the frame. Null unless it is OUR message type,
 *  carries OUR token, and holds a sane finite height — anything inside the
 *  sandbox can postMessage, so everything is validated before it moves the
 *  layout. Height clamped to the band; width sanitized but uncapped (the
 *  frame caps it against the column at render). */
export function frameSizeFromMessage(data: unknown, token: string): FrameSizeReport | null {
  if (typeof data !== 'object' || data === null) {
    return null
  }

  const message = data as { type?: unknown; token?: unknown; height?: unknown; width?: unknown }

  if (message.type !== SIZE_MESSAGE_TYPE || message.token !== token || typeof message.height !== 'number') {
    return null
  }

  if (!Number.isFinite(message.height) || message.height <= 0) {
    return null
  }

  const width =
    typeof message.width === 'number' && Number.isFinite(message.width) && message.width > 0
      ? Math.round(message.width)
      : 0

  return {
    height: Math.min(MAX_HEIGHT, Math.max(MIN_HEIGHT, Math.round(message.height))),
    width
  }
}

const HTML_FILE_RE = /\.(?:html?|xhtml)$/i

/** The frame box: its own clip (`overflow-clip`), paint containment, and
 *  stacking context (`isolate`), so the frame is bounded like any other
 *  message content. Exported for the regression test. */
export const INLINE_FRAME_BOX_CLASS =
  'relative isolate block max-w-full overflow-clip contain-[layout_paint] transition-[height] duration-200'

export function InlinePreviewDirective({
  attrs,
  streaming
}: {
  attrs: Readonly<Record<string, string>>
  streaming: boolean
}) {
  const file = attrs.file ?? ''

  // Not renderable inline: hand the leaf to the classic card. Non-HTML has
  // nothing to frame. (Remote gateways used to bail here too — that predates
  // the mode-aware fs bridge; the frame now reads through readDesktopFileText,
  // which fetches over the authenticated /api/fs bridge in remote mode, so a
  // URL connection — including a same-machine `hermes serve` — renders live.)
  if (!file || !HTML_FILE_RE.test(file)) {
    return file ? <PreviewAttachment target={file} /> : null
  }

  return <InlineHtmlFrame file={file} initialHeight={directiveFrameHeight(attrs.height)} streaming={streaming} />
}

function InlineHtmlFrame({
  file,
  initialHeight,
  streaming
}: {
  file: string
  /** `height` attribute — the starting height only; measurement overrides. */
  initialHeight: number | null
  streaming: boolean
}) {
  const cwd = useStore(useSessionView().$cwd)
  const [theme, setTheme] = useState(resolveFrameTheme)
  const [doc, setDoc] = useState<string | null>(null)
  const [failed, setFailed] = useState(false)
  const [measured, setMeasured] = useState<number | null>(null)
  const [contentWidth, setContentWidth] = useState<number | null>(null)

  // One token per mount: the message listener only trusts reports from the
  // document THIS mount injected, so two previews in one transcript (or a
  // hostile page inventing messages) can't move each other's frames.
  const token = useMemo(() => Math.random().toString(36).slice(2), [])

  // Resolve against THIS session's cwd (the file was written by its agent).
  const resolved = localPreviewTarget(file, cwd || undefined)
  const path = resolved?.path ?? null

  useEffect(() => {
    // Wait for turn settle: mid-stream the file is often mid-write, and a
    // half-written srcdoc renders as garbage that never self-corrects.
    if (!path || streaming) {
      return
    }

    let alive = true

    void Promise.resolve(readDesktopFileText(path))
      .then(result => {
        if (!alive) {
          return
        }

        if (!result || result.binary || !result.text) {
          setFailed(true)
        } else {
          setDoc(result.text)
        }
      })
      .catch(() => alive && setFailed(true))

    return () => {
      alive = false
    }
  }, [path, streaming])

  useEffect(() => {
    // Human-speed gate on widget intents. A closure local, not state: it's
    // a rate limiter read inside the handler, never rendered.
    let lastIntentAt = 0

    const onMessage = (event: MessageEvent) => {
      const intent = intentFromMessage(event.data, token)

      if (intent !== null) {
        const now = Date.now()

        if (now - lastIntentAt >= INTENT_THROTTLE_MS) {
          lastIntentAt = now
          // Off-screen: the prompt reaches the agent as a normal user turn
          // through the composer's own send path (steer/queue rules apply),
          // but the row is typed hidden — no bubble, no UI space. The widget
          // updating IS the visible response.
          requestComposerSubmit(intent, { target: 'active', displayKind: 'hidden' })
        }

        return
      }

      const next = frameSizeFromMessage(event.data, token)

      if (next === null) {
        return
      }

      // Functional updates so the comparisons read current state without a
      // shadow ref: same-value sets bail out in React, and the tolerance
      // keeps a vh-sized page (which measures what it's given) from
      // oscillating.
      setMeasured(prev =>
        Math.abs(next.height - (prev ?? initialHeight ?? DEFAULT_HEIGHT)) > RESIZE_TOLERANCE ? next.height : prev
      )

      // Width adopts ONCE, from the first report — measured at full column
      // width, so it is the content's intrinsic span. Tracking width live
      // would feedback-loop: %-width children reflow narrower every time
      // the frame shrinks, spiraling toward zero.
      if (next.width > 0) {
        setContentWidth(prev => prev ?? next.width)
      }
    }

    window.addEventListener('message', onMessage)

    return () => window.removeEventListener('message', onMessage)
  }, [initialHeight, token])

  // Follow the live theme. A light/dark switch does NOT remount the
  // transcript, so a prelude resolved once would leave the frame on the old
  // scheme — a scheme that no longer matches the element's, i.e. an opaque
  // canvas. Re-resolve on every theme repaint; the memo below keys on the
  // prelude STRING, so unrelated <html> style churn yields an identical
  // srcdoc and never reloads the widget.
  useEffect(() => {
    setTheme(resolveFrameTheme())

    return onThemeRepaint(() => setTheme(resolveFrameTheme()))
  }, [])

  const prelude = theme.prelude

  const framedDoc = useMemo(() => (doc === null ? null : withInlineChrome(doc, token, prelude)), [doc, prelude, token])

  if (!path || failed) {
    return <PreviewAttachment target={file} />
  }

  const height = measured ?? initialHeight ?? DEFAULT_HEIGHT
  // Left-aligned in the message flow, like an image: the frame is only as
  // wide as its content (capped at the column). Fluid pages measure the
  // full viewport and stay full-bleed.
  const width = contentWidth !== null ? Math.min(contentWidth, MAX_COLUMN_WIDTH) : undefined

  return (
    <span className="my-2 block w-full max-w-160">
      {framedDoc === null ? (
        <span
          className="block w-full animate-pulse rounded-md bg-[color-mix(in_srgb,currentColor_4%,transparent)]"
          style={{ height }}
        />
      ) : (
        // The frame box is a hard boundary: overflow clip + paint
        // containment + its own stacking context, and the iframe is IN FLOW
        // (not an absolutely positioned layer), so nothing the frame paints
        // can land outside this box — the transcript scroller clips it and
        // the composer dock paints over it.
        <span
          className={INLINE_FRAME_BOX_CLASS}
          data-slot="inline-preview-frame"
          style={{ height, width: width ?? '100%' }}
        >
          <iframe
            className="block size-full border-0 bg-transparent"
            loading="lazy"
            sandbox="allow-scripts"
            srcDoc={framedDoc}
            // Same resolution as the srcdoc's `:root{color-scheme}` — they
            // must match or Chromium paints the canvas opaque.
            style={{ colorScheme: theme.scheme }}
            title={file}
          />
        </span>
      )}
    </span>
  )
}
