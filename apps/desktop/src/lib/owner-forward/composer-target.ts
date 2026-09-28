/**
 * #67 / D29 composer forward target: the composer control beside the model picker that switches
 * Send to "send to: <chat>". State is per ORIGIN chat (the stored session the composer belongs to),
 * so a target picked in one pane never re-routes another pane's Send.
 *
 * The target only changes where Send goes. The send itself is the existing owner-forward path
 * (`sendOwnerForward`: secrets.mask → main's native confirm → `owner.forward`), quote-only (no
 * conductor scope), with the TTL the owner picked in the control.
 */
import { map } from 'nanostores'

import type { ForwardTarget } from './client'
import { scopeTtlOptions, scopeTtlPolicy } from './scopes'

export interface ComposerForwardTarget extends ForwardTarget {
  ttlMs: number
}

/** Quote-only forwards: the composer path never carries a conductor scope. */
export const COMPOSER_FORWARD_TTL_OPTIONS = scopeTtlOptions([])
export const COMPOSER_FORWARD_DEFAULT_TTL_MS = scopeTtlPolicy([]).defaultTtlMs

export const $composerForwardTargets = map<Record<string, ComposerForwardTarget | undefined>>({})

export function composerForwardTargetFor(originId: null | string | undefined): ComposerForwardTarget | null {
  return originId ? ($composerForwardTargets.get()[originId] ?? null) : null
}

/** Set (or with `null`, clear) the target for the composer of `originId`. A chat never targets itself. */
export function setComposerForwardTarget(originId: string, target: ForwardTarget | null): void {
  if (!target || target.session_id === originId) {
    $composerForwardTargets.setKey(originId, undefined)

    return
  }

  const ttlMs = composerForwardTargetFor(originId)?.ttlMs ?? COMPOSER_FORWARD_DEFAULT_TTL_MS

  $composerForwardTargets.setKey(originId, {
    profile: target.profile,
    session_id: target.session_id,
    title: target.title ?? null,
    ttlMs
  })
}

export function setComposerForwardTtl(originId: string, ttlMs: number): void {
  const current = composerForwardTargetFor(originId)
  const policy = scopeTtlPolicy([])

  if (current && Number.isSafeInteger(ttlMs) && ttlMs > 0) {
    $composerForwardTargets.setKey(originId, { ...current, ttlMs: Math.min(ttlMs, policy.maxTtlMs) })
  }
}

/** A transcript selection as a markdown quote block: every line prefixed with "> ". */
export function quoteForComposer(text: string): string {
  const lines = text
    .replace(/\r\n?/g, '\n')
    .replace(/^\n+|\s+$/g, '')
    .split('\n')

  return lines.map(line => (line.trim() ? `> ${line}` : '>')).join('\n')
}
