import type { ModelOptionProvider } from '@hermes/shared'

import { modelDisplayParts } from '@/lib/model-status-label'
import { modelFamilyKey } from '@/store/model-visibility'

/**
 * Stale-pin upgrade hint (D53). A session keeps the model it was created on —
 * we never migrate it silently, because a switch changes cost and behaviour and
 * throws away the conversation's prompt cache. Instead the picker offers a
 * one-click "Upgrade to <newest>" when the session sits on a superseded member
 * of its family. Everything here is pure: the catalog decides, the caller acts.
 */

/** Families we never suggest (and never suggest away from). */
const BANNED_UPGRADE_FAMILIES: ReadonlySet<string> = new Set(['haiku'])

const CLAUDE_TIERS: ReadonlySet<string> = new Set(['opus', 'sonnet', 'haiku', 'fable'])

// Anthropic's `[1m]` route suffix (1M context window) and the `-fast` sibling
// are VARIANTS of one model: an upgrade keeps them, never drops them.
const CONTEXT_VARIANT_RE = /\[\d+[mk]\]$/i
const FAST_VARIANT_RE = /-fast$/i

// A version token: `5`, `5.5`, `v4`. Leading-zero tokens (`001`, `0905`) are
// revision/date pins, handled by DATED_TOKEN_RE instead.
const VERSION_TOKEN_RE = /^v?(?:0|[1-9]\d*)(?:\.\d+)*$/i

// A date/revision pin segment: `20251001`, `2024`-`08`-`06`, `0905`, `001`.
const DATED_TOKEN_RE = /^(?:\d{8}|0\d+|(?:19|20)\d{2})$/

interface ParsedModel {
  /** Variant-free id, WITHOUT the vendor prefix (`claude-opus-5-5`). */
  core: string
  /** `[1m]`-style context suffix, lowercased, or ''. */
  ctx: string
  /** An explicit snapshot/revision pin the user chose on purpose. */
  dated: boolean
  family: string
  fast: boolean
  /** Vendor path prefix incl. trailing slash (`anthropic/`), lowercased, or ''. */
  prefix: string
  version: number[]
}

/** Family key for UPGRADE purposes. Claude ids reuse the picker's existing
 *  `modelFamilyKey` (the tier: opus / sonnet / …). Other vendors spell the
 *  version inside the name (`gpt-5.5`, `gemini-3-pro`), where the picker's
 *  key keeps the version; here the version tokens are dropped so `gpt-5.5`
 *  and `gpt-6` share the `gpt` family while `gpt-5.5-mini` stays `gpt-mini`. */
export function upgradeFamilyKey(model: string): string {
  const key = modelFamilyKey(model)

  if (CLAUDE_TIERS.has(key)) {
    return key
  }

  const base = model
    .trim()
    .toLowerCase()
    .replace(/^.*[/]/, '')

  const tokens = base.split(/[-_]/).filter(token => token && !VERSION_TOKEN_RE.test(token))

  return tokens.length > 0 ? tokens.join('-') : base
}

function parseModel(id: string): ParsedModel {
  const trimmed = id.trim()
  const slash = trimmed.lastIndexOf('/')
  const prefix = slash >= 0 ? trimmed.slice(0, slash + 1).toLowerCase() : ''
  let core = trimmed.slice(slash + 1)

  const ctx = core.match(CONTEXT_VARIANT_RE)?.[0].toLowerCase() ?? ''
  core = ctx ? core.slice(0, -ctx.length) : core

  const fast = FAST_VARIANT_RE.test(core)
  core = fast ? core.replace(FAST_VARIANT_RE, '') : core

  const tokens = core.toLowerCase().split(/[-_]/)
  const dated = tokens.some(token => DATED_TOKEN_RE.test(token))

  const version = tokens
    .filter(token => VERSION_TOKEN_RE.test(token))
    .flatMap(token => token.replace(/^v/i, '').split('.').map(Number))

  return { core, ctx, dated, family: upgradeFamilyKey(core), fast, prefix, version }
}

function compareVersions(a: readonly number[], b: readonly number[]): number {
  for (let i = 0; i < Math.max(a.length, b.length); i++) {
    const diff = (a[i] ?? 0) - (b[i] ?? 0)

    if (diff !== 0) {
      return diff
    }
  }

  return 0
}

/**
 * The model id `current` should be upgraded to, from ONE provider's catalog,
 * or null when there is nothing to suggest.
 *
 * - "Newest" is the highest version in the family; on a tie the curated
 *   (newest-first) catalog order wins. Taking the max rather than blindly the
 *   first entry keeps a catalog that is not newest-first (third-party lists)
 *   from ever suggesting a downgrade.
 * - `[1m]` / `-fast` variants map to the SAME variant of the newest model;
 *   when the catalog lacks that exact variant there is no hint.
 * - A dated snapshot (`-20251001`) is a deliberate pin: no hint.
 * - Banned families (Haiku) never get a hint.
 */
export function modelUpgradeTarget(current: string, catalog: readonly string[]): null | string {
  if (!current.trim()) {
    return null
  }

  // Resolve the current id the way the backend does (model_switch.py: an exact id, else the single
  // declared id that extends it): the Claude Agent SDK route lists only `[1m]` ids, so a session on
  // bare `claude-opus-5` actually runs `claude-opus-5[1m]` and must be compared as that variant.
  const extensions = catalog.includes(current) ? [] : catalog.filter(id => id.startsWith(`${current}[`))
  const cur = parseModel(extensions.length === 1 ? extensions[0] : current)

  if (cur.dated || cur.version.length === 0 || BANNED_UPGRADE_FAMILIES.has(cur.family)) {
    return null
  }

  let newest: ParsedModel | null = null

  for (const id of catalog) {
    const entry = parseModel(id)

    if (entry.dated || entry.version.length === 0 || entry.prefix !== cur.prefix || entry.family !== cur.family) {
      continue
    }

    // Strictly greater: an equal version later in the list never displaces
    // the curated-order winner.
    if (!newest || compareVersions(entry.version, newest.version) > 0) {
      newest = entry
    }
  }

  if (!newest || BANNED_UPGRADE_FAMILIES.has(newest.family) || compareVersions(newest.version, cur.version) <= 0) {
    return null
  }

  const target = newest.core.toLowerCase()

  return (
    catalog.find(id => {
      const entry = parseModel(id)

      return (
        entry.prefix === cur.prefix &&
        entry.core.toLowerCase() === target &&
        entry.fast === cur.fast &&
        entry.ctx === cur.ctx &&
        !entry.dated
      )
    }) ?? null
  )
}

export interface ModelUpgrade {
  /** The session's current model id. */
  from: string
  /** Catalog provider slug the upgrade is offered under. */
  provider: string
  /** The catalog id to switch to. */
  to: string
}

/** Resolve the upgrade for the current model against its provider's catalog row
 *  (the caller matches the row — slug, name or alias — the way the picker does). */
export function modelUpgradeFor(
  currentModel: string,
  provider: Pick<ModelOptionProvider, 'models' | 'slug'> | undefined
): ModelUpgrade | null {
  if (!currentModel || !provider) {
    return null
  }

  const to = modelUpgradeTarget(currentModel, provider.models ?? [])

  return to ? { from: currentModel, provider: provider.slug, to } : null
}

/** Human label for a hint side: "Opus 5.5", "Opus 5.5 · 1M", "Opus 5.5 · Fast". */
export function modelUpgradeLabel(model: string): string {
  const { name, tag } = modelDisplayParts(model)

  return tag ? `${name} · ${tag}` : name
}
