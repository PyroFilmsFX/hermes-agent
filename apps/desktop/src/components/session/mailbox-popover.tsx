import { type FC, useCallback, useEffect, useState } from 'react'

import { PeerStatusChip } from '@/components/assistant-ui/thread/system-message'
import { Button } from '@/components/ui/button'
import { Codicon } from '@/components/ui/codicon'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { Tip } from '@/components/ui/tooltip'
import { cn } from '@/lib/utils'
import { activeGateway } from '@/store/gateway'
import type {
  PeerMailboxCancelResult,
  PeerMailboxListResult,
  PeerMailboxMessage,
  PeerMailboxRetryResult
} from '@hermes/shared'

export interface SessionMailboxPopoverProps {
  sessionId: string
  className?: string
}

export const SessionMailboxPopover: FC<SessionMailboxPopoverProps> = ({ sessionId, className }) => {
  const [open, setOpen] = useState(false)
  const [loading, setLoading] = useState(false)
  const [messages, setMessages] = useState<PeerMailboxMessage[]>([])

  const fetchMailbox = useCallback(async () => {
    if (!sessionId) {
      return
    }
    const gateway = activeGateway()
    if (!gateway) {
      return
    }
    setLoading(true)
    try {
      const res = await gateway.request<PeerMailboxListResult>('peer_mailbox.list', {
        session_id: sessionId
      })
      setMessages(res?.messages ?? [])
    } catch (err) {
      console.warn('Failed to load peer mailbox', err)
    } finally {
      setLoading(false)
    }
  }, [sessionId])

  useEffect(() => {
    if (open) {
      void fetchMailbox()
    }
  }, [open, fetchMailbox])

  const handleRetry = async (messageId: number) => {
    const gateway = activeGateway()
    if (!gateway) {
      return
    }
    try {
      await gateway.request<PeerMailboxRetryResult>('peer_mailbox.retry', {
        message_id: messageId,
        session_id: sessionId
      })
      void fetchMailbox()
    } catch (err) {
      console.warn('Failed to retry peer message', err)
    }
  }

  const handleCancel = async (messageId: number) => {
    const gateway = activeGateway()
    if (!gateway) {
      return
    }
    try {
      await gateway.request<PeerMailboxCancelResult>('peer_mailbox.cancel', {
        message_id: messageId,
        session_id: sessionId
      })
      void fetchMailbox()
    } catch (err) {
      console.warn('Failed to cancel peer message', err)
    }
  }

  if (!sessionId) {
    return null
  }

  return (
    <Popover onOpenChange={setOpen} open={open}>
      <Tip label="Peer Mailbox" side="bottom">
        <PopoverTrigger asChild>
          <Button
            aria-label="Peer Mailbox"
            className={cn(
              'size-6 rounded-[4px] bg-transparent text-(--ui-text-tertiary) hover:bg-(--ui-control-active-background) hover:text-foreground focus-visible:ring-0',
              className
            )}
            data-slot="session-mailbox-trigger"
            size="icon-xs"
            variant="ghost"
          >
            <Codicon name="mail" size="0.875rem" />
          </Button>
        </PopoverTrigger>
      </Tip>
      <PopoverContent
        align="start"
        className="w-84 p-2 text-xs"
        data-slot="session-mailbox-popover"
        sideOffset={6}
      >
        <div className="flex items-center justify-between border-b border-border/40 pb-1.5 mb-1.5 font-medium text-foreground">
          <span className="flex items-center gap-1.5">
            <Codicon name="mail" size="0.875rem" />
            <span>Peer Mailbox</span>
          </span>
          <Button
            aria-label="Refresh"
            className="size-5 rounded-[3px] text-(--ui-text-tertiary) hover:text-foreground"
            disabled={loading}
            onClick={() => void fetchMailbox()}
            size="icon-xs"
            variant="ghost"
          >
            <Codicon className={loading ? 'codicon-modifier-spin' : ''} name="refresh" size="0.75rem" />
          </Button>
        </div>

        {loading && messages.length === 0 ? (
          <div className="py-4 text-center text-xs text-muted-foreground">Loading mailbox…</div>
        ) : messages.length === 0 ? (
          <div className="py-4 text-center text-xs text-muted-foreground" data-slot="mailbox-empty">
            No pending peer messages
          </div>
        ) : (
          <div className="flex flex-col gap-1 max-h-64 overflow-y-auto" data-slot="mailbox-list">
            {messages.map(msg => {
              const isQueued = msg.status === 'queued' || msg.status?.startsWith('queued')
              const peerLabel =
                msg.direction === 'in'
                  ? `↘ from ${msg.from_label || msg.from_session_id || 'peer'}`
                  : `↗ to ${msg.target_hint || msg.target_session_id || 'peer'}`
              return (
                <div
                  className="flex items-center justify-between gap-1.5 rounded-[3px] px-1.5 py-1 hover:bg-muted/40"
                  data-slot="mailbox-row"
                  key={msg.id}
                >
                  <div className="flex min-w-0 flex-1 items-center gap-1.5">
                    <span className="font-mono text-[0.625rem] text-muted-foreground shrink-0">
                      #{msg.id}
                    </span>
                    <span className="truncate text-[0.6875rem]" title={peerLabel}>
                      {peerLabel}
                    </span>
                  </div>
                  <div className="flex items-center gap-1 shrink-0">
                    <PeerStatusChip attempts={msg.attempts} status={msg.status} />
                    {isQueued && (
                      <>
                        <Button
                          aria-label="Retry"
                          className="h-5 px-1.5 text-[0.625rem]"
                          onClick={() => void handleRetry(msg.id)}
                          size="xs"
                          variant="outline"
                        >
                          Retry
                        </Button>
                        <Button
                          aria-label="Cancel"
                          className="h-5 px-1.5 text-[0.625rem]"
                          onClick={() => void handleCancel(msg.id)}
                          size="xs"
                          variant="ghost"
                        >
                          Cancel
                        </Button>
                      </>
                    )}
                  </div>
                </div>
              )
            })}
          </div>
        )}
      </PopoverContent>
    </Popover>
  )
}
