import { useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'

import { requestComposerInsert } from '@/app/chat/composer/focus'
import { Button } from '@/components/ui/button'
import { Tip } from '@/components/ui/tooltip'
import { useI18n } from '@/i18n'
import { triggerHaptic } from '@/lib/haptics'
import { Send } from '@/lib/icons'
import { quoteForComposer } from '@/lib/owner-forward/composer-target'
import { type TranscriptForward, transcriptSelectionForward } from '@/lib/owner-forward/transcript-selection'

/**
 * #67 / D29: put a transcript selection (or a rendered item) into the composer of the pane it came
 * from, as a quote block. Nothing is sent: the owner picks a "send to" target beside the model picker
 * (or doesn't) and presses Send like any draft.
 */
export function forwardIntoComposer(forward: TranscriptForward): void {
  requestComposerInsert(quoteForComposer(forward.text), { mode: 'block', target: forward.target })
  window.getSelection()?.removeAllRanges()
}

interface Anchor {
  forward: TranscriptForward
  left: number
  top: number
}

const GAP_PX = 6
const EDGE_PX = 8
const BUTTON_WIDTH_PX = 88

function selectionRect(selection: Selection): DOMRect | null {
  const range = selection.rangeCount ? selection.getRangeAt(0) : null
  const rect = typeof range?.getBoundingClientRect === 'function' ? range.getBoundingClientRect() : null

  if (rect && (rect.width || rect.height)) {
    return rect
  }

  const node = selection.focusNode
  const element = node instanceof Element ? node : (node?.parentElement ?? null)

  return element ? element.getBoundingClientRect() : null
}

function anchorFor(selection: Selection | null): Anchor | null {
  const forward = transcriptSelectionForward(selection)

  if (!forward || !selection) {
    return null
  }

  const rect = selectionRect(selection)
  const width = typeof window.innerWidth === 'number' && window.innerWidth > 0 ? window.innerWidth : 1024
  const height = typeof window.innerHeight === 'number' && window.innerHeight > 0 ? window.innerHeight : 768
  const bottom = rect?.bottom ?? 0
  const top = bottom + GAP_PX + 28 > height ? Math.max(EDGE_PX, (rect?.top ?? 0) - GAP_PX - 28) : bottom + GAP_PX

  return {
    forward,
    left: Math.max(EDGE_PX, Math.min((rect?.right ?? 0) - BUTTON_WIDTH_PX / 2, width - BUTTON_WIDTH_PX - EDGE_PX)),
    top
  }
}

/**
 * The small "Forward" action that follows a finished text selection in a transcript. Mounted once
 * (app wiring); it appears on mouseup / keyup when the selection sits inside one chat transcript,
 * and goes away when the selection collapses or the page scrolls.
 */
export function TranscriptSelectionForward() {
  const { t } = useI18n()
  const copy = t.ownerForward
  const [anchor, setAnchor] = useState<Anchor | null>(null)
  const pressing = useRef(false)

  useEffect(() => {
    const show = () => {
      if (!pressing.current) {
        setAnchor(anchorFor(window.getSelection()))
      }
    }

    const onSelectionChange = () => {
      const selection = window.getSelection()

      if (!pressing.current && (!selection || selection.isCollapsed)) {
        setAnchor(null)
      }
    }

    const hide = () => setAnchor(null)

    document.addEventListener('mouseup', show)
    document.addEventListener('keyup', show)
    document.addEventListener('selectionchange', onSelectionChange)
    window.addEventListener('scroll', hide, true)
    window.addEventListener('blur', hide)

    return () => {
      document.removeEventListener('mouseup', show)
      document.removeEventListener('keyup', show)
      document.removeEventListener('selectionchange', onSelectionChange)
      window.removeEventListener('scroll', hide, true)
      window.removeEventListener('blur', hide)
    }
  }, [])

  if (!anchor || typeof document === 'undefined') {
    return null
  }

  return createPortal(
    <div className="fixed z-50" data-slot="transcript-forward-action" style={{ left: anchor.left, top: anchor.top }}>
      <Tip label={copy.forwardSelectionHint} side="top">
        <Button
          aria-label={copy.forwardSelection}
          onClick={() => {
            pressing.current = false
            triggerHaptic('selection')
            forwardIntoComposer(anchor.forward)
            setAnchor(null)
          }}
          // Keep the selection alive through the press (a mousedown would collapse it first).
          onMouseDown={event => {
            pressing.current = true
            event.preventDefault()
          }}
          onMouseUp={() => {
            pressing.current = false
          }}
          size="xs"
          type="button"
          variant="floating"
        >
          <Send />
          {copy.forwardSelection}
        </Button>
      </Tip>
    </div>,
    document.body
  )
}
