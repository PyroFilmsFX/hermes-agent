import { act, cleanup, render, waitFor } from '@testing-library/react'
import { atom } from 'nanostores'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  appColorScheme,
  INLINE_FRAME_BOX_CLASS,
  InlinePreviewDirective,
  resolveFrameTheme
} from './inline-preview-directive'

const cwd = atom('/ws')

vi.mock('@/app/chat/session-view', () => ({ useSessionView: () => ({ $cwd: cwd }) }))
vi.mock('@/app/chat/composer/focus', () => ({ requestComposerSubmit: vi.fn() }))
vi.mock('@/lib/desktop-fs', () => ({
  readDesktopFileText: vi.fn(async () => ({ binary: false, text: '<div class="row">todo</div>' }))
}))

const DARK_TOKENS = {
  '--ui-text-primary': '#ededf0',
  '--ui-text-tertiary': '#8b8b93',
  '--ui-accent': '#7aa2f7',
  '--ui-stroke-tertiary': '#2c2c31',
  '--ui-bg-editor': '#1b1b1f'
}

const LIGHT_TOKENS = {
  '--ui-text-primary': '#17171a',
  '--ui-text-tertiary': '#6b6b73',
  '--ui-accent': '#0053fd',
  '--ui-stroke-tertiary': '#e4e4e8',
  '--ui-bg-editor': '#ffffff'
}

/** What themes/context.tsx applyTheme does to <html>: the `dark` class,
 *  `color-scheme`, and the resolved tokens, all in one repaint. */
function paintTheme(mode: 'dark' | 'light') {
  const root = document.documentElement
  root.classList.toggle('dark', mode === 'dark')
  root.style.setProperty('color-scheme', mode)

  for (const [name, value] of Object.entries(mode === 'dark' ? DARK_TOKENS : LIGHT_TOKENS)) {
    root.style.setProperty(name, value)
  }
}

/** The injected `:root{…}` rule of the srcdoc, parsed into a map. */
function frameRoot(srcdoc: string): Record<string, string> {
  const rule = /<style>:root\{([^}]*)\}/.exec(srcdoc)

  expect(rule).not.toBeNull()

  return Object.fromEntries(
    rule![1]
      .split(';')
      .filter(Boolean)
      .map(decl => {
        const at = decl.indexOf(':')

        return [decl.slice(0, at), decl.slice(at + 1)]
      })
  )
}

async function mountFrame(): Promise<HTMLIFrameElement> {
  const { container } = render(<InlinePreviewDirective attrs={{ file: 'todo.html' }} streaming={false} />)

  return waitFor(() => {
    const frame = container.querySelector('iframe')

    expect(frame).not.toBeNull()

    return frame!
  })
}

beforeEach(() => paintTheme('dark'))

afterEach(() => {
  cleanup()
  const root = document.documentElement
  root.classList.remove('dark')
  root.removeAttribute('style')
})

describe('inline preview frame theme (bug: dark app, white widget)', () => {
  it('resolves the scheme from the app root', () => {
    expect(appColorScheme()).toBe('dark')
    paintTheme('light')
    expect(appColorScheme()).toBe('light')
  })

  it('dark app: the frame document and the iframe element both take the DARK scheme, with dark tokens', async () => {
    const frame = await mountFrame()
    const root = frameRoot(frame.getAttribute('srcdoc') ?? '')

    // The element and the document MUST agree, or Chromium paints the
    // frame canvas opaque (white for a default-light doc in a dark app).
    expect(root['color-scheme']).toBe('dark')
    expect(frame.style.colorScheme).toBe('dark')

    expect(root['--foreground']).toBe(DARK_TOKENS['--ui-text-primary'])
    expect(root['--muted-foreground']).toBe(DARK_TOKENS['--ui-text-tertiary'])
    expect(root['--accent']).toBe(DARK_TOKENS['--ui-accent'])
    expect(root['--border']).toBe(DARK_TOKENS['--ui-stroke-tertiary'])
    expect(root['--card']).toBe(DARK_TOKENS['--ui-bg-editor'])

    expect(frame.getAttribute('srcdoc')).toContain('background:transparent')
  })

  it('light app: both take the LIGHT scheme, with light tokens', async () => {
    paintTheme('light')

    const frame = await mountFrame()
    const root = frameRoot(frame.getAttribute('srcdoc') ?? '')

    expect(root['color-scheme']).toBe('light')
    expect(frame.style.colorScheme).toBe('light')
    expect(root['--foreground']).toBe(LIGHT_TOKENS['--ui-text-primary'])
    expect(root['--card']).toBe(LIGHT_TOKENS['--ui-bg-editor'])
  })

  it('follows a live light/dark switch without a remount, in both directions', async () => {
    const frame = await mountFrame()

    expect(frame.style.colorScheme).toBe('dark')

    await act(async () => paintTheme('light'))

    await waitFor(() => expect(frame.style.colorScheme).toBe('light'))
    expect(frame.isConnected).toBe(true)
    expect(frameRoot(frame.getAttribute('srcdoc') ?? '')['color-scheme']).toBe('light')
    expect(frameRoot(frame.getAttribute('srcdoc') ?? '')['--foreground']).toBe(LIGHT_TOKENS['--ui-text-primary'])

    await act(async () => paintTheme('dark'))

    await waitFor(() => expect(frame.style.colorScheme).toBe('dark'))
    expect(frameRoot(frame.getAttribute('srcdoc') ?? '')['color-scheme']).toBe('dark')
    expect(frameRoot(frame.getAttribute('srcdoc') ?? '')['--card']).toBe(DARK_TOKENS['--ui-bg-editor'])
  })

  it('unrelated <html> style churn leaves the srcdoc identical (no widget reload)', async () => {
    const frame = await mountFrame()
    const before = frame.getAttribute('srcdoc')

    await act(async () => document.documentElement.style.setProperty('--unrelated', '1px'))

    expect(frame.getAttribute('srcdoc')).toBe(before)
    expect(resolveFrameTheme().prelude).toBe(resolveFrameTheme().prelude)
  })

  it('keeps the sandbox exactly as it was', async () => {
    const frame = await mountFrame()

    expect(frame.getAttribute('sandbox')).toBe('allow-scripts')
  })
})

describe('inline preview frame clipping (bug: widget drew over the composer)', () => {
  it('the frame box is its own clip, paint-containment and stacking boundary', async () => {
    const frame = await mountFrame()
    const box = frame.parentElement!

    expect(box.dataset.slot).toBe('inline-preview-frame')
    expect(box.className).toBe(INLINE_FRAME_BOX_CLASS)

    const classes = box.className.split(/\s+/)

    expect(classes).toEqual(expect.arrayContaining(['relative', 'isolate', 'overflow-clip', 'contain-[layout_paint]']))
    // Never lifted out of the transcript's clip or above the composer.
    expect(classes.some(c => /^(fixed|absolute|sticky)$|^-?z-|^translate|^transform/.test(c))).toBe(false)
  })

  it('the iframe sits in flow inside that box, never as a positioned layer', async () => {
    const frame = await mountFrame()
    const classes = frame.className.split(/\s+/)

    expect(classes).toEqual(expect.arrayContaining(['block', 'size-full']))
    expect(classes.some(c => /^(fixed|absolute|sticky|inset-.*)$|^-?z-/.test(c))).toBe(false)
    expect(frame.style.position).toBe('')
    expect(frame.style.zIndex).toBe('')
  })

  it('no ancestor the directive renders is positioned out of flow', async () => {
    const frame = await mountFrame()
    let el: HTMLElement | null = frame.parentElement

    // Walk up to the directive's own outer span.
    while (el && el.parentElement && el.parentElement.tagName === 'SPAN') {
      el = el.parentElement
    }

    for (let node: HTMLElement | null = frame; node && node !== el?.parentElement; node = node.parentElement) {
      expect(node.className).not.toMatch(/(^|\s)(fixed|absolute|sticky)(\s|$)/)
    }
  })
})
