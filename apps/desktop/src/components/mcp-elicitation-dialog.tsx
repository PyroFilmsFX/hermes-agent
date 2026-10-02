'use client'

import { useStore } from '@nanostores/react'
import { type FormEvent, useCallback, useId, useMemo, useState } from 'react'

import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { CopyButton } from '@/components/ui/copy-button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle
} from '@/components/ui/dialog'
import { FieldHint } from '@/components/ui/field'
import { Input } from '@/components/ui/input'
import { useI18n } from '@/i18n'
import type { Translations } from '@/i18n/types'
import { triggerHaptic } from '@/lib/haptics'
import { ExternalLink, Globe, Loader2, Plug } from '@/lib/icons'
import {
  buildElicitationContent,
  type ElicitationDraft,
  type ElicitationField,
  type ElicitationFieldError,
  initialElicitationDraft,
  parseElicitationSchema
} from '@/lib/mcp-elicitation/schema'
import { openExternalDoor } from '@/lib/os-open-external'
import { cn } from '@/lib/utils'
import {
  $mcpElicitations,
  isExpiredMcpElicitation,
  type McpElicitationAction,
  type McpElicitationRequest,
  respondMcpElicitation,
  settleMcpElicitation,
  visibleMcpElicitation
} from '@/store/mcp-elicitation'
import { notify } from '@/store/notifications'
import { $activeSessionId } from '@/store/session'
import { isBrowserWindow, isHudWindow } from '@/store/windows'

// MCP elicitation (M4b): a connected MCP server asked the user for input in
// the middle of a tool call. Form mode renders the server's flat
// `requestedSchema`; URL mode asks consent before handing a link to the OS
// browser. Silence is never consent: Escape answers `cancel` (URL mode:
// `decline`, the only other answer the backend takes there), an outside click
// does nothing (it would throw away a half-filled form), and when the backend
// times out the card is dropped without answering (store/mcp-elicitation).

type Copy = Translations['prompts']['mcpElicitation']

type AnswerFn = (action: McpElicitationAction, content?: Record<string, unknown>) => void

function errorText(copy: Copy, error: ElicitationFieldError, kind: ElicitationField['kind']): string {
  switch (error.code) {
    case 'required':
      // A radio group is "chosen", not "filled in".
      return kind === 'enum' ? copy.errors.enum : copy.errors.required

    case 'format':
      return error.format === 'date-time' ? copy.errors.dateTime : copy.errors[error.format]

    case 'minimum':

    case 'maximum':

    case 'minLength':

    case 'maxLength':
      return copy.errors[error.code](error.limit)

    default:
      return copy.errors[error.code]
  }
}

const STRING_INPUT_TYPE: Record<string, string> = {
  date: 'date',
  'date-time': 'datetime-local',
  email: 'email',
  uri: 'url'
}

interface FieldProps {
  busy: boolean
  copy: Copy
  error?: ElicitationFieldError
  field: ElicitationField
  id: string
  onChange: (name: string, value: boolean | string) => void
  value: boolean | string | undefined
}

function FieldLabel({ copy, field, htmlFor }: { copy: Copy; field: ElicitationField; htmlFor?: string }) {
  const content = (
    <>
      {field.label}
      {field.required ? (
        <>
          <span aria-hidden="true" className="ml-0.5 text-destructive">
            *
          </span>
          <span className="sr-only"> ({copy.required})</span>
        </>
      ) : null}
    </>
  )

  return htmlFor ? (
    <label className="text-xs font-medium text-foreground" htmlFor={htmlFor} id={`${htmlFor}-label`}>
      {content}
    </label>
  ) : (
    <legend className="mb-1.5 text-xs font-medium text-foreground">{content}</legend>
  )
}

function ElicitationFieldControl({ busy, copy, error, field, id, onChange, value }: FieldProps) {
  const descriptionId = field.description ? `${id}-desc` : undefined
  const errorId = error ? `${id}-error` : undefined
  const describedBy = [descriptionId, errorId].filter(Boolean).join(' ') || undefined

  const hints = (
    <>
      {field.description ? (
        <p className="text-[0.66rem] leading-4 text-muted-foreground" id={descriptionId}>
          {field.description}
        </p>
      ) : null}
      {error ? (
        <p className="text-[0.66rem] leading-4 text-destructive" id={errorId}>
          {errorText(copy, error, field.kind)}
        </p>
      ) : null}
    </>
  )

  if (field.kind === 'boolean') {
    return (
      <div className="flex items-start gap-2">
        <Checkbox
          aria-describedby={describedBy}
          aria-invalid={error ? true : undefined}
          checked={value === true}
          className="mt-0.5"
          disabled={busy}
          id={id}
          onCheckedChange={checked => onChange(field.name, checked === true)}
        />
        <div className="grid gap-1">
          <FieldLabel copy={copy} field={field} htmlFor={id} />
          {hints}
        </div>
      </div>
    )
  }

  if (field.kind === 'enum') {
    const options = field.required
      ? field.options.map((option, index) => ({ key: String(index), label: option.label }))
      : [
          ...field.options.map((option, index) => ({ key: String(index), label: option.label })),
          { key: '', label: copy.noSelection }
        ]

    return (
      <fieldset
        aria-describedby={describedBy}
        aria-invalid={error ? true : undefined}
        aria-required={field.required || undefined}
        className="grid min-w-0 gap-1"
        disabled={busy}
        id={id}
        tabIndex={-1}
      >
        <FieldLabel copy={copy} field={field} />
        <div className="grid max-h-48 gap-1 overflow-y-auto">
          {options.map(option => (
            <label
              className="flex cursor-pointer items-center gap-2 rounded-md px-1 py-0.5 text-sm hover:bg-(--chrome-action-hover)"
              key={option.key || 'none'}
            >
              <input
                checked={value === option.key}
                className="size-3.5 accent-primary"
                name={id}
                onChange={() => onChange(field.name, option.key)}
                type="radio"
                value={option.key}
              />
              <span className={cn(option.key === '' && 'text-muted-foreground')}>{option.label}</span>
            </label>
          ))}
        </div>
        {hints}
      </fieldset>
    )
  }

  if (field.kind === 'unsupported') {
    return (
      <div className="grid gap-1.5" id={id} tabIndex={-1}>
        <span className="text-xs font-medium text-foreground">{field.label}</span>
        <p className="text-[0.66rem] leading-4 text-destructive" role="note">
          {copy.errors.unsupported}
        </p>
      </div>
    )
  }

  const inputType = field.kind === 'string' ? (STRING_INPUT_TYPE[field.format ?? ''] ?? 'text') : 'text'
  const inputMode = field.kind === 'number' ? (field.integer ? 'numeric' : 'decimal') : undefined

  return (
    <div className="grid gap-1.5">
      <FieldLabel copy={copy} field={field} htmlFor={id} />
      <Input
        aria-describedby={describedBy}
        aria-invalid={error ? true : undefined}
        aria-required={field.required || undefined}
        disabled={busy}
        id={id}
        inputMode={inputMode}
        maxLength={field.kind === 'string' ? field.maxLength : undefined}
        onChange={event => onChange(field.name, event.currentTarget.value)}
        type={inputType}
        value={typeof value === 'string' ? value : ''}
      />
      {hints}
    </div>
  )
}

function ElicitationForm({
  answer,
  busy,
  copy,
  request
}: {
  answer: AnswerFn
  busy: boolean
  copy: Copy
  request: Extract<McpElicitationRequest, { mode: 'form' }>
}) {
  const formId = useId()
  const fields = useMemo(() => parseElicitationSchema(request.requestedSchema), [request.requestedSchema])
  const visibleFields = useMemo(() => fields.filter(field => field.kind !== 'unsupported' || field.required), [fields])
  const [draft, setDraft] = useState<ElicitationDraft>(() => initialElicitationDraft(fields))
  const [attempted, setAttempted] = useState(false)
  const result = useMemo(() => buildElicitationContent(fields, draft), [fields, draft])
  const errors = attempted && !result.ok ? result.errors : {}
  const blocked = fields.some(field => field.kind === 'unsupported' && field.required)
  const fieldId = (index: number) => `${formId}-field-${index}`

  const onChange = useCallback((name: string, value: boolean | string) => {
    setDraft(current => ({ ...current, [name]: value }))
  }, [])

  const onSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    setAttempted(true)

    if (!result.ok) {
      const firstInvalid = visibleFields.findIndex(field => field.name in result.errors)

      if (firstInvalid >= 0) {
        document.getElementById(fieldId(firstInvalid))?.focus()
      }

      return
    }

    answer('accept', result.content)
  }

  return (
    <form className="grid gap-4" noValidate onSubmit={onSubmit}>
      {visibleFields.length > 0 ? (
        <div className="grid gap-4">
          {visibleFields.map((field, index) => (
            <ElicitationFieldControl
              busy={busy}
              copy={copy}
              error={field.kind === 'unsupported' ? undefined : errors[field.name]}
              field={field}
              id={fieldId(index)}
              key={field.name}
              onChange={onChange}
              value={draft[field.name]}
            />
          ))}
        </div>
      ) : null}
      <DialogFooter>
        <Button disabled={busy} onClick={() => answer('cancel')} type="button" variant="ghost">
          {copy.cancel}
        </Button>
        <Button disabled={busy} onClick={() => answer('decline')} type="button" variant="outline">
          {copy.decline}
        </Button>
        <Button disabled={busy || blocked} type="submit">
          {busy ? <Loader2 className="size-3.5 animate-spin" /> : copy.submit}
        </Button>
      </DialogFooter>
    </form>
  )
}

interface WebUrlParts {
  href: string
  prefix: string
  host: string
  rest: string
  secure: boolean
}

/** Only web links are offered: main's openExternal would also hand `file:`
 *  URLs to the OS file association, which a server must never trigger. */
export function parseElicitationUrl(raw: string): null | WebUrlParts {
  let url: URL

  try {
    url = new URL(raw)
  } catch {
    return null
  }

  if (url.protocol !== 'https:' && url.protocol !== 'http:') {
    return null
  }

  const userinfo = url.username ? `${url.username}${url.password ? `:${url.password}` : ''}@` : ''

  return {
    href: url.href,
    prefix: `${url.protocol}//${userinfo}`,
    host: url.host,
    rest: `${url.pathname}${url.search}${url.hash}`,
    secure: url.protocol === 'https:'
  }
}

function ElicitationUrl({
  answer,
  busy,
  copy,
  request
}: {
  answer: AnswerFn
  busy: boolean
  copy: Copy
  request: Extract<McpElicitationRequest, { mode: 'url' }>
}) {
  const url = useMemo(() => parseElicitationUrl(request.url), [request.url])
  const [opening, setOpening] = useState(false)
  const [openFailed, setOpenFailed] = useState(false)

  // Consent is this click and nothing else: the link is never opened on
  // mount, on Enter in another field, or by the timeout.
  const open = async () => {
    if (!url || opening) {
      return
    }

    setOpening(true)
    setOpenFailed(false)

    if (!(await openExternalDoor(url.href))) {
      setOpenFailed(true)
      setOpening(false)

      return
    }

    answer('accept')
  }

  return (
    <div className="grid gap-3">
      <div className="grid gap-1.5">
        <span className="text-xs font-medium text-foreground">{copy.urlLabel}</span>
        <div
          className="max-h-32 overflow-auto break-all rounded-md border border-(--stroke-nous) bg-muted px-3 py-2 font-mono text-xs select-all"
          data-testid="mcp-elicitation-url"
        >
          {url ? (
            <>
              <span className="text-muted-foreground">{url.prefix}</span>
              <strong className="font-semibold text-foreground">{url.host}</strong>
              <span className="text-muted-foreground">{url.rest}</span>
            </>
          ) : (
            <span className="text-muted-foreground">{request.url}</span>
          )}
        </div>
        {url && !url.secure ? <FieldHint error>{copy.urlInsecure}</FieldHint> : null}
        {!url ? <FieldHint error>{copy.urlUnsupported}</FieldHint> : null}
      </div>
      {url ? <p className="text-xs text-muted-foreground">{copy.urlNote(request.server)}</p> : null}
      {openFailed ? (
        <div className="flex items-center justify-between gap-2" role="alert">
          <p className="text-xs text-destructive">{copy.openFailed}</p>
          <CopyButton buttonSize="xs" buttonVariant="outline" text={url?.href ?? request.url} />
        </div>
      ) : null}
      <DialogFooter>
        <Button disabled={busy} onClick={() => answer('decline')} type="button" variant="ghost">
          {copy.decline}
        </Button>
        <Button disabled={busy || opening || !url} onClick={() => void open()} type="button">
          {busy || opening ? <Loader2 className="size-3.5 animate-spin" /> : <ExternalLink className="size-3.5" />}
          {copy.openInBrowser}
        </Button>
      </DialogFooter>
    </div>
  )
}

function ElicitationDialog({ request, waiting }: { request: McpElicitationRequest; waiting: number }) {
  const { t } = useI18n()
  const copy = t.prompts.mcpElicitation
  const [busy, setBusy] = useState(false)
  const [sendError, setSendError] = useState<null | string>(null)

  const answer = useCallback<AnswerFn>(
    (action, content) => {
      if (busy) {
        return
      }

      setBusy(true)
      setSendError(null)

      void respondMcpElicitation(request, action, content).then(
        () => {
          triggerHaptic('submit')
          settleMcpElicitation(request.requestId)
        },
        (error: unknown) => {
          if (isExpiredMcpElicitation(error)) {
            settleMcpElicitation(request.requestId)
            notify({ kind: 'info', message: copy.expired(request.server) })

            return
          }

          setSendError(error instanceof Error && error.message ? error.message : copy.sendFailed)
          setBusy(false)
        }
      )
    },
    [busy, copy, request]
  )

  // Escape funnels through Radix's single onOpenChange(false). Outside clicks
  // are swallowed below, so this is only ever an explicit dismissal.
  const onOpenChange = useCallback(
    (open: boolean) => {
      if (!open) {
        answer(request.mode === 'url' ? 'decline' : 'cancel')
      }
    },
    [answer, request.mode]
  )

  return (
    <Dialog onOpenChange={onOpenChange} open>
      <DialogContent
        blurBackdrop={false}
        data-mcp-elicitation={request.mode}
        onInteractOutside={event => event.preventDefault()}
        showCloseButton={false}
      >
        <DialogHeader>
          <DialogTitle icon={request.mode === 'url' ? Globe : Plug}>
            {request.mode === 'url' ? copy.urlTitle(request.server) : copy.formTitle(request.server)}
          </DialogTitle>
          <DialogDescription>
            {copy.fromServer(request.server)}
            {waiting > 0 ? (
              <span className="ml-2 rounded-full bg-(--ui-bg-quaternary) px-1.5 py-0.5 text-[0.65rem] text-foreground">
                {copy.queued(waiting)}
              </span>
            ) : null}
          </DialogDescription>
        </DialogHeader>

        {request.message.trim() ? (
          <p className="max-h-40 overflow-y-auto text-sm whitespace-pre-wrap break-words text-foreground">
            {request.message}
          </p>
        ) : null}

        {request.mode === 'form' ? (
          <ElicitationForm answer={answer} busy={busy} copy={copy} request={request} />
        ) : (
          <ElicitationUrl answer={answer} busy={busy} copy={copy} request={request} />
        )}

        {sendError ? (
          <p className="text-xs text-destructive" role="alert">
            {copy.sendFailed}: {sendError}
          </p>
        ) : null}
      </DialogContent>
    </Dialog>
  )
}

/** Pill for requests that belong to a chat the user is not looking at. */
function HiddenElicitationBadge({ count, copy, onShow }: { count: number; copy: Copy; onShow: () => void }) {
  return (
    <div
      className="fixed right-4 bottom-12 z-(--z-over-modal) flex items-center gap-2 rounded-full border border-(--stroke-nous) bg-(--ui-chat-bubble-background) py-1 pr-1 pl-3 text-xs text-foreground shadow-nous"
      role="status"
    >
      <Plug className="size-3.5 text-primary" />
      <span>{copy.hiddenBadge(count)}</span>
      <Button onClick={onShow} size="xs" type="button" variant="secondary">
        {copy.show}
      </Button>
    </div>
  )
}

function McpElicitationQueue() {
  const { t } = useI18n()
  const queue = useStore($mcpElicitations)
  const activeSessionId = useStore($activeSessionId)
  const [revealedId, setRevealedId] = useState<null | string>(null)
  const visible = visibleMcpElicitation(queue, activeSessionId, revealedId)

  if (visible) {
    return <ElicitationDialog key={visible.requestId} request={visible} waiting={queue.length - 1} />
  }

  if (queue.length > 0) {
    return (
      <HiddenElicitationBadge
        copy={t.prompts.mcpElicitation}
        count={queue.length}
        onShow={() => setRevealedId(queue[0]!.requestId)}
      />
    )
  }

  return null
}

/** App-level host: one MCP elicitation card at a time, oldest first. */
export function McpElicitationHost() {
  if (isHudWindow() || isBrowserWindow()) {
    return null
  }

  return <McpElicitationQueue />
}
