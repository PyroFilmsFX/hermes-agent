/**
 * #67 / D29: what in a chat transcript can be forwarded from the composer. A text selection that
 * lies inside one surface's transcript, or a rendered item (a code block, a table) under the pointer.
 * The answer names the surface's composer (`data-composer-target`) so the quote lands in the pane
 * the owner is reading, never in whichever composer last had focus.
 *
 * DOM reads only. Nothing here sends: the quote goes into the composer, and the owner decides.
 */

/** The transcript area of a chat surface (the thread; the composer is a sibling, not a child). */
const TRANSCRIPT_SELECTOR = '[data-slot="composer-bounds"]'
const SURFACE_SELECTOR = '[data-composer-target]'
/** Rendered items a right-click can forward whole. */
const ITEM_SELECTOR = 'pre, table'

const EDITABLE_SELECTOR =
  'input, textarea, [contenteditable=""], [contenteditable="true"], [contenteditable="plaintext-only"]'

export interface TranscriptForward {
  /** The composer the quote belongs in. */
  target: string
  text: string
}

function elementOf(node: Node | null | undefined): Element | null {
  if (!node) {
    return null
  }

  return node instanceof Element ? node : node.parentElement
}

function transcriptOf(element: Element | null): { bounds: Element; target: string } | null {
  if (!element || element.closest(EDITABLE_SELECTOR)) {
    return null
  }

  const bounds = element.closest(TRANSCRIPT_SELECTOR)
  const surface = bounds?.closest<HTMLElement>(SURFACE_SELECTOR)
  const target = surface?.dataset.composerTarget

  return bounds && target ? { bounds, target } : null
}

/** The live selection, when it is non-empty and wholly inside ONE transcript. */
export function transcriptSelectionForward(
  selection: null | Selection | undefined = typeof window === 'undefined' ? null : window.getSelection()
): null | TranscriptForward {
  if (!selection || selection.isCollapsed || selection.rangeCount === 0) {
    return null
  }

  const text = selection.toString().trim()

  if (!text) {
    return null
  }

  const anchor = transcriptOf(elementOf(selection.anchorNode))
  const focus = transcriptOf(elementOf(selection.focusNode))

  if (!anchor || !focus || anchor.bounds !== focus.bounds) {
    return null
  }

  return { target: anchor.target, text }
}

function tableText(table: HTMLTableElement): string {
  return Array.from(table.rows)
    .map(row =>
      Array.from(row.cells)
        .map(cell => (cell.textContent ?? '').trim().replace(/\s+/g, ' '))
        .join(' | ')
    )
    .join('\n')
}

/** A rendered item (code block or table) under `element`, as text a quote can carry. */
export function transcriptItemForward(element: Element | null): null | TranscriptForward {
  const context = transcriptOf(element)
  const item = element?.closest(ITEM_SELECTOR)

  if (!context || !item || !context.bounds.contains(item)) {
    return null
  }

  if (item instanceof HTMLTableElement) {
    const text = tableText(item).trim()

    return text ? { target: context.target, text } : null
  }

  const code = (item.textContent ?? '').replace(/\n+$/, '')

  if (!code.trim()) {
    return null
  }

  const language = /\blanguage-([\w+#.-]+)/.exec(item.querySelector('code')?.className ?? '')?.[1] ?? ''

  return { target: context.target, text: `\`\`\`${language}\n${code}\n\`\`\`` }
}
