import { useStore } from '@nanostores/react'
import type { MouseEvent } from 'react'

import { useSessionView } from '@/app/chat/session-view'
import { TooltipIconButton } from '@/components/assistant-ui/tooltip-icon-button'
import { useI18n } from '@/i18n'
import { Send } from '@/lib/icons'
import { openForwardSheet } from '@/lib/owner-forward/client'

/**
 * #60 "Forward to…" (design §4.1) on a message's action bar. A text selection inside the page wins
 * (gesture `selection`), else the message's visible text (gesture `menu`). Only opens the sheet, and
 * only on a trusted click; the sheet's Send… and main's native confirm do the rest.
 */
export function ForwardMessageButton({
  getText,
  role,
  rowId
}: {
  getText: () => string
  role: 'assistant' | 'peer' | 'user'
  rowId?: number
}) {
  const { t } = useI18n()
  const storedId = useStore(useSessionView().$storedId)

  if (!storedId || !window.hermesDesktop?.ownerForward) {
    return null
  }

  const onClick = (event: MouseEvent) => {
    const selection = window.getSelection()?.toString().trim() ?? ''
    const text = selection || getText().trim()

    if (!text) {
      return
    }

    openForwardSheet(
      {
        text,
        gesture: selection ? 'selection' : 'menu',
        origin: { session_id: storedId, message_id: rowId !== undefined ? String(rowId) : null, role }
      },
      event.nativeEvent
    )
  }

  return (
    <TooltipIconButton onClick={onClick} tooltip={t.ownerForward.forwardTo}>
      <Send className="size-3.5" />
    </TooltipIconButton>
  )
}
