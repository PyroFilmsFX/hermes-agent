/**
 * Is owner forwarding usable right now? The Forward sheet can always open, but a send needs main's
 * confirm bridge and a ready owner key (the anchor from Settings → Gateways → "Let conductor verify
 * owner decisions"). The `:::send-to` button reads this to disable itself with a reason instead of
 * leading the owner into a confirm that can only fail.
 *
 * One shared status, fetched lazily on first need; the Settings row writes through it after
 * enable / rotate / revoke so every block updates in place.
 */
import { atom } from 'nanostores'

import type { DesktopOwnerGrantStatus } from '@/global'

export type OwnerGrantStatusValue = DesktopOwnerGrantStatus | 'error' | null

export const $ownerGrantStatus = atom<OwnerGrantStatusValue>(null)

let pending: Promise<void> | null = null

export function setOwnerGrantStatus(status: DesktopOwnerGrantStatus): void {
  $ownerGrantStatus.set(status)
}

/** Fetch main's owner-key status once (deduped). A failed fetch reads as unavailable until the next
 *  block mounts and asks again. */
export function ensureOwnerGrantStatus(): void {
  const current = $ownerGrantStatus.get()

  if ((current !== null && current !== 'error') || pending) {
    return
  }

  const status = window.hermesDesktop?.ownerGrant?.status

  if (!status) {
    $ownerGrantStatus.set('error')

    return
  }

  pending = status()
    .then(next => $ownerGrantStatus.set(next))
    .catch(() => $ownerGrantStatus.set('error'))
    .finally(() => {
      pending = null
    })
}

export function resetOwnerGrantStatusForTests(): void {
  pending = null
  $ownerGrantStatus.set(null)
}

export type ForwardServiceState = 'checking' | 'off' | 'ready' | 'unavailable'

export function forwardServiceState(status: OwnerGrantStatusValue, hasConfirmBridge: boolean): ForwardServiceState {
  if (!hasConfirmBridge) {
    return 'unavailable'
  }

  if (status === null) {
    return 'checking'
  }

  if (status === 'error' || status.state === 'unsupported') {
    return 'unavailable'
  }

  return status.state === 'ready' && status.canSign ? 'ready' : 'off'
}
