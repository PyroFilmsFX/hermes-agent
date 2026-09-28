import { useEffect, useState } from 'react'

import { Button } from '@/components/ui/button'
import type { DesktopOwnerGrantStatus } from '@/global'
import { Loader2 } from '@/lib/icons'
import { setOwnerGrantStatus } from '@/lib/owner-forward/service'
import { notifyError } from '@/store/notifications'

import { ListRow } from './primitives'

type OwnerGrantAction = 'enable' | 'revoke' | 'rotate'

const TITLE = 'Let conductor verify owner decisions'
const EXPLAIN = 'Signs your approvals so conductor can check they came from you. macOS will ask for an admin password once.'

// #60 U11: one row. Main does the real work (native confirm with the new key id, then one macOS
// admin prompt); this only asks and shows main's status.
export function OwnerGrantRow() {
  const [status, setStatus] = useState<DesktopOwnerGrantStatus | null>(null)
  const [busy, setBusy] = useState<OwnerGrantAction | null>(null)

  useEffect(() => {
    let cancelled = false

    void window.hermesDesktop?.ownerGrant
      ?.status()
      .then(next => {
        setOwnerGrantStatus(next)

        if (!cancelled) {
          setStatus(next)
        }
      })
      .catch(() => {})

    return () => {
      cancelled = true
    }
  }, [])

  if (!status || status.state === 'unsupported') {
    return null
  }

  const run = async (action: OwnerGrantAction) => {
    setBusy(action)

    try {
      const res = await window.hermesDesktop.ownerGrant!.action(action)
      setStatus(res.status)
      setOwnerGrantStatus(res.status)

      if (!res.ok && res.reason !== 'cancelled') {
        notifyError(new Error(res.reason ?? 'failed'), 'Owner key change did not finish')
      }
    } catch (err) {
      notifyError(err, 'Owner key change did not finish')
    } finally {
      setBusy(null)
    }
  }

  const disabled = busy !== null || status.busy
  const spinner = (action: OwnerGrantAction) => (busy === action ? <Loader2 className="animate-spin" /> : null)

  return (
    <ListRow
      action={
        status.state === 'ready' ? (
          <>
            <Button disabled={disabled} onClick={() => void run('rotate')} size="sm" variant="textStrong">
              {spinner('rotate')}
              Rotate key
            </Button>
            <Button disabled={disabled} onClick={() => void run('revoke')} size="sm" variant="textStrong">
              {spinner('revoke')}
              Revoke key
            </Button>
          </>
        ) : (
          <Button disabled={disabled} onClick={() => void run('enable')} size="sm">
            {spinner('enable')}
            Turn on
          </Button>
        )
      }
      description={status.state === 'off' ? EXPLAIN : `${status.message} ${status.state === 'ready' ? '' : EXPLAIN}`.trim()}
      title={TITLE}
    />
  )
}
