/**
 * #67 / D29: selecting text (or right-clicking a rendered item) in a transcript offers "Forward",
 * which puts the selection into THAT surface's composer as a quote block. Nothing is sent here.
 */
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { onComposerInsertRequest } from '@/app/chat/composer/focus'
import { AppContextMenu } from '@/app/context-menu/app-context-menu'
import { $contextMenu } from '@/app/context-menu/store'
import { quoteForComposer } from '@/lib/owner-forward/composer-target'
import { transcriptItemForward, transcriptSelectionForward } from '@/lib/owner-forward/transcript-selection'

import { forwardIntoComposer, TranscriptSelectionForward } from './transcript-forward'

const SURFACE = `
  <div data-composer-target="tile:abc" data-composer-surface-id="s1">
    <div data-slot="composer-bounds">
      <div data-slot="aui_assistant-message-root">
        <p id="line-one">Ship the fix today</p>
        <p id="line-two">then tag the release</p>
        <pre id="code"><code class="language-ts">const a = 1
console.log(a)
</code></pre>
      </div>
    </div>
    <div contenteditable="true" data-slot="composer-rich-input" id="editor">draft words</div>
  </div>
`

let inserts: Array<{ mode: string; target: string; text: string }> = []

let offInsert: () => void = () => {}

function mountSurface(): HTMLElement {
  const host = window.document.createElement('div')

  host.innerHTML = SURFACE
  window.document.body.appendChild(host)

  return host
}

function select(from: Node, to: Node = from): void {
  const range = window.document.createRange()

  range.setStart(from, 0)
  range.setEnd(to, to.childNodes.length || (to.textContent ?? '').length)

  const selection = window.getSelection()!
  selection.removeAllRanges()
  selection.addRange(range)
}

beforeEach(() => {
  inserts = []
  offInsert = onComposerInsertRequest(detail =>
    inserts.push({ mode: detail.mode, target: detail.target, text: detail.text })
  )
})

afterEach(() => {
  offInsert()
  cleanup()
  window.getSelection()?.removeAllRanges()
  $contextMenu.set(null)
  window.document.body.innerHTML = ''
})

describe('quoteForComposer', () => {
  it('prefixes every line with "> " and keeps blank lines inside the quote', () => {
    expect(quoteForComposer('first\n\nsecond\n')).toBe('> first\n>\n> second')
  })
})

describe('selection → quote insert', () => {
  it('a selection inside a transcript names that surface composer and its text', () => {
    const host = mountSurface()

    select(host.querySelector('#line-one')!.firstChild!)

    expect(transcriptSelectionForward()).toEqual({ target: 'tile:abc', text: 'Ship the fix today' })
  })

  it('a selection in the composer editor (not the transcript) is not offered', () => {
    const host = mountSurface()

    select(host.querySelector('#editor')!.firstChild!)

    expect(transcriptSelectionForward()).toBeNull()
  })

  it('forwardIntoComposer inserts the selection as a quote block into that composer, nothing sent', async () => {
    const host = mountSurface()

    select(host.querySelector('#line-one')!.firstChild!, host.querySelector('#line-two')!.firstChild!)
    forwardIntoComposer(transcriptSelectionForward()!)

    await vi.waitFor(() => expect(inserts).toHaveLength(1))
    expect(inserts[0]).toEqual({
      mode: 'block',
      target: 'tile:abc',
      text: expect.stringMatching(/^> Ship the fix today\n(>\n)?> +then tag the release$/)
    })
  })

  it('a rendered code block is quoted whole, with its fence', () => {
    const host = mountSurface()

    expect(transcriptItemForward(host.querySelector('#code code'))).toEqual({
      target: 'tile:abc',
      text: '```ts\nconst a = 1\nconsole.log(a)\n```'
    })
    expect(transcriptItemForward(host.querySelector('#line-one'))).toBeNull()
  })
})

describe('the floating Forward action', () => {
  it('appears after a transcript selection and inserts the quote on click', async () => {
    const host = mountSurface()

    render(<TranscriptSelectionForward />)
    expect(screen.queryByRole('button', { name: 'Forward' })).toBeNull()

    select(host.querySelector('#line-one')!.firstChild!)
    act(() => {
      window.document.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }))
    })

    fireEvent.click(await screen.findByRole('button', { name: 'Forward' }))

    await vi.waitFor(() =>
      expect(inserts).toEqual([{ mode: 'block', target: 'tile:abc', text: '> Ship the fix today' }])
    )
    expect(screen.queryByRole('button', { name: 'Forward' })).toBeNull()
  })

  it('stays hidden for a selection in the composer', () => {
    const host = mountSurface()

    render(<TranscriptSelectionForward />)
    select(host.querySelector('#editor')!.firstChild!)
    act(() => {
      window.document.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }))
    })

    expect(screen.queryByRole('button', { name: 'Forward' })).toBeNull()
  })
})

describe('the app context menu', () => {
  it('offers Forward for a transcript selection and quotes it into that composer', async () => {
    const host = mountSurface()

    render(
      <MemoryRouter>
        <AppContextMenu />
      </MemoryRouter>
    )
    select(host.querySelector('#line-two')!.firstChild!)
    fireEvent.contextMenu(host.querySelector('#line-two')!)

    fireEvent.click(await screen.findByRole('menuitem', { name: 'Forward' }))

    await vi.waitFor(() =>
      expect(inserts).toEqual([{ mode: 'block', target: 'tile:abc', text: '> then tag the release' }])
    )
  })

  it('offers Forward for a rendered code block with no selection', async () => {
    const host = mountSurface()

    render(
      <MemoryRouter>
        <AppContextMenu />
      </MemoryRouter>
    )
    fireEvent.contextMenu(host.querySelector('#code code')!)

    fireEvent.click(await screen.findByRole('menuitem', { name: 'Forward' }))

    await vi.waitFor(() => expect(inserts).toHaveLength(1))
    expect(inserts[0]).toMatchObject({ target: 'tile:abc', text: '> ```ts\n> const a = 1\n> console.log(a)\n> ```' })
  })
})
