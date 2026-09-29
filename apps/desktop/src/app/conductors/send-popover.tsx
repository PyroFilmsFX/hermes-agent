import { type ReactNode, useId, useRef, useState } from 'react'

import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { useI18n } from '@/i18n'
import { cn } from '@/lib/utils'

export interface SendPopoverProps {
  /** The trigger button (rendered via `asChild`). */
  children: ReactNode
  /** Put the text in the session's composer and open it. Never sends. */
  onSubmit: (text: string) => void
  onOpenChange: (open: boolean) => void
  open: boolean
  /** Where focus lands when the popover closes (the row, for the grid's
   *  roving focus). Radix's default would park it on the hidden trigger. */
  returnFocus?: () => void
  sessionLabel: string
}

/**
 * Send… (§8): one field, "Message to <session>". Enter puts the text in that
 * session's composer draft and opens the session; Shift+Enter is a newline.
 * This is the owner typing in the session — nothing is sent from here, so it
 * needs no forward grant and adds no gesture. The owner presses Enter there.
 */
export function SendPopover({ children, onOpenChange, onSubmit, open, returnFocus, sessionLabel }: SendPopoverProps) {
  const { t } = useI18n()
  const c = t.conductors
  const [text, setText] = useState('')
  const fieldId = useId()
  const hintId = useId()
  const blank = !text.trim()
  // A submit hands focus to the session it just opened; only a dismissal
  // (Escape, click outside) returns it to the row.
  const submittedRef = useRef(false)

  const submit = () => {
    if (blank) {
      return
    }

    submittedRef.current = true
    onSubmit(text)
    setText('')
    onOpenChange(false)
  }

  return (
    <Popover onOpenChange={onOpenChange} open={open}>
      <PopoverTrigger asChild>{children}</PopoverTrigger>
      <PopoverContent
        align="end"
        className="w-80"
        data-slot="conductors-send-popover"
        onCloseAutoFocus={event => {
          const submitted = submittedRef.current
          submittedRef.current = false

          if (submitted || returnFocus) {
            event.preventDefault()
          }

          if (!submitted) {
            returnFocus?.()
          }
        }}
        side="bottom"
      >
        <form
          className="flex flex-col gap-1.5"
          onSubmit={event => {
            event.preventDefault()
            submit()
          }}
        >
          <label className="truncate text-xs font-medium text-(--ui-text-primary)" htmlFor={fieldId}>
            {c.sendTitle(sessionLabel)}
          </label>
          <textarea
            aria-describedby={hintId}
            autoFocus
            className="min-h-16 resize-none rounded border border-(--ui-stroke-secondary) bg-(--ui-bg-secondary) px-2 py-1 text-xs text-(--ui-text-primary) outline-none placeholder:text-(--ui-text-quaternary) focus:border-(--ui-stroke-primary)"
            id={fieldId}
            onChange={event => setText(event.target.value)}
            onKeyDown={event => {
              if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
                event.preventDefault()
                submit()
              }
            }}
            placeholder={c.sendPlaceholder}
            rows={3}
            value={text}
          />
          <div className="flex items-start gap-2">
            <p className="min-w-0 flex-1 text-[0.65rem] leading-snug text-(--ui-text-tertiary)" id={hintId}>
              {c.sendHint}
            </p>
            <button
              className={cn(
                'shrink-0 rounded border border-(--ui-stroke-secondary) px-2 py-0.5 text-xs text-(--ui-text-secondary)',
                blank ? 'opacity-50' : 'hover:bg-(--ui-control-hover-background) hover:text-(--ui-text-primary)'
              )}
              disabled={blank}
              type="submit"
            >
              {c.sendSubmit}
            </button>
          </div>
        </form>
      </PopoverContent>
    </Popover>
  )
}
