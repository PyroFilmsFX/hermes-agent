/**
 * b10 H9a: the session → project binding pill in the session titlebar.
 *
 * Per-session, so it lives in the header beside the profile tag (never the composer row).
 * States: unbound (muted), suggested (dashed), bound (solid + lock), needs re-confirm (amber),
 * and signing off (disabled, explains why). Clicking opens a picker: the session's suggested
 * workspace, then the projects.tree projects / repos / lanes, then "Other folder…", then Unbind.
 *
 * Nothing is ever signed without an owner click here. A first bind asks an in-app confirm
 * (owner decision O2); a re-bind of a live build gets main's native confirm (H8). The renderer
 * sends only `{profile, hermes_session_id, path}` — main resolves the worktree root itself.
 */
import { useStore } from '@nanostores/react'
import { useEffect, useMemo } from 'react'

import type { SidebarProjectTree } from '@/app/chat/sidebar/projects/workspace-groups'
import { normalizePath } from '@/app/chat/sidebar/projects/workspace-groups'
import { Codicon } from '@/components/ui/codicon'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuLabel,
  dropdownMenuSectionLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger
} from '@/components/ui/dropdown-menu'
import { Tip } from '@/components/ui/tooltip'
import type { DesktopSessionBindingOutcome, DesktopSessionBindingRecord } from '@/global'
import { useI18n } from '@/i18n'
import { isDesktopFsRemoteMode, selectDesktopPaths } from '@/lib/desktop-fs'
import { pathLeaf } from '@/lib/display-path'
import { $ownerGrantStatus, ensureOwnerGrantStatus } from '@/lib/owner-forward/service'
import { isUnderPath } from '@/lib/path-compare'
import { cn } from '@/lib/utils'
import { confirm } from '@/store/confirm'
import { notify } from '@/store/notifications'
import { normalizeProfileKey } from '@/store/profile'
import { $projectTree, $projectTreeLoaded, refreshProjectTree } from '@/store/projects'
import {
  $sessionBindings,
  $sessionBindingSuggestions,
  clearSessionBinding,
  ensureSessionBinding,
  ensureSessionBindingSuggestion,
  refreshSessionBinding,
  sessionBindingAvailable,
  sessionBindingKey,
  type SessionBindingResolvedRoot,
  type SessionBindingSuggestion,
  setSessionBinding,
  type SuggestionSource
} from '@/store/session-binding'

export type BindingPillState = 'bound' | 'needs_reconfirm' | 'signing_off' | 'suggested' | 'unbound'

export interface BindingPickerOption {
  key: string
  path: string
  label: string
  detail: null | string
  depth: 0 | 1 | 2
  icon: string
}

export interface BindingPickerSection {
  id: string
  label: string
  options: BindingPickerOption[]
}

/** `git@host:org/repo.git`, `ssh://git@host/…`, `https://host/…` → `host`. Null when unparseable. */
export function remoteHost(remote: null | string | undefined): null | string {
  const value = (remote ?? '').trim()

  if (!value) {
    return null
  }

  const scp = value.match(/^[^@/\s]+@([^:/\s]+):/)

  if (scp) {
    return scp[1]
  }

  try {
    return new URL(value).hostname || null
  } catch {
    return null
  }
}

/** The pill's state from main's record, the suggestion and the owner-key status. */
export function bindingPillState(
  record: DesktopSessionBindingRecord | null,
  suggestion: SessionBindingSuggestion | null,
  canSign: boolean
): BindingPillState {
  if (!canSign) {
    return 'signing_off'
  }

  if (record?.state === 'bound') {
    return 'bound'
  }

  if (record?.state === 'needs_reconfirm') {
    return 'needs_reconfirm'
  }

  return suggestion ? 'suggested' : 'unbound'
}

/**
 * Picker rows from the cached `projects.tree`: each project's root, its repos, then its lanes,
 * deduped by path and never repeating the suggested workspace. Kanban and Home buckets are skipped.
 */
export function bindingPickerSections(
  tree: readonly SidebarProjectTree[],
  suggestionPath: null | string
): BindingPickerSection[] {
  const seen = new Set<string>()

  if (suggestionPath) {
    seen.add(normalizePath(suggestionPath))
  }

  const take = (path: null | string | undefined): null | string => {
    const value = (path ?? '').trim()
    const key = normalizePath(value)

    if (!value || seen.has(key)) {
      return null
    }

    seen.add(key)

    return value
  }

  const sections: BindingPickerSection[] = []

  for (const project of tree) {
    if (project.isNoProject || project.archived) {
      continue
    }

    const options: BindingPickerOption[] = []
    const projectPath = take(project.path)

    if (projectPath) {
      options.push({
        key: `p:${projectPath}`,
        path: projectPath,
        label: project.label,
        detail: null,
        depth: 0,
        icon: 'root-folder'
      })
    }

    for (const repo of project.repos) {
      const repoPath = take(repo.path)

      if (repoPath) {
        options.push({ key: `r:${repoPath}`, path: repoPath, label: repo.label, detail: null, depth: 1, icon: 'repo' })
      }

      for (const lane of repo.groups) {
        if (lane.isKanban) {
          continue
        }

        const lanePath = take(lane.path)

        if (lanePath) {
          options.push({
            key: `l:${lanePath}`,
            path: lanePath,
            label: lane.branch?.trim() || lane.label,
            detail: pathLeaf(lanePath),
            depth: 2,
            icon: 'git-branch'
          })
        }
      }
    }

    if (options.length) {
      sections.push({ id: project.id, label: project.label, options })
    }
  }

  return sections
}

/** Branch of the lane whose worktree is `root`, from the cached tree. */
function laneBranchFor(tree: readonly SidebarProjectTree[], root: string): null | string {
  const key = normalizePath(root)

  for (const project of tree) {
    for (const repo of project.repos) {
      for (const lane of repo.groups) {
        if (lane.path && normalizePath(lane.path) === key) {
          return lane.branch?.trim() || null
        }
      }
    }
  }

  return null
}

function outcomeReason(outcome: DesktopSessionBindingOutcome): null | string {
  return outcome.ok ? null : outcome.reason
}

const PILL_BASE =
  'pointer-events-auto inline-flex h-5 max-w-56 shrink-0 items-center gap-1 rounded-full border px-2 text-[0.6875rem] leading-4 transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-(--ui-stroke-secondary)'

const PILL_STATE_CLASS: Record<BindingPillState, string> = {
  unbound:
    'border-transparent text-(--ui-text-tertiary) hover:bg-(--ui-control-hover-background) hover:text-(--ui-text-secondary)',
  suggested:
    'border-dashed border-(--ui-stroke-secondary) text-(--ui-text-secondary) hover:bg-(--ui-control-hover-background) hover:text-foreground',
  bound:
    'border-(--ui-stroke-tertiary) bg-(--ui-control-active-background) text-(--ui-text-secondary) hover:border-(--ui-stroke-secondary) hover:text-foreground',
  needs_reconfirm: 'border-amber-500/40 bg-amber-500/10 text-amber-600 hover:bg-amber-500/15 dark:text-amber-300',
  signing_off: 'cursor-not-allowed border-transparent text-(--ui-text-quaternary) opacity-70'
}

const PILL_ICON: Record<BindingPillState, string> = {
  unbound: 'link',
  suggested: 'link',
  bound: 'lock',
  needs_reconfirm: 'warning',
  signing_off: 'lock'
}

export interface SessionBindingPillProps {
  className?: string
  session: SuggestionSource
}

export function SessionBindingPill({ className, session }: SessionBindingPillProps) {
  const { t } = useI18n()
  const copy = t.sessionBinding
  const profile = normalizeProfileKey(session.profile)
  const sessionId = session.id
  const key = sessionBindingKey(profile, sessionId)
  const entry = useStore($sessionBindings)[key]
  const suggestion = useStore($sessionBindingSuggestions)[key] ?? null
  const grant = useStore($ownerGrantStatus)
  const tree = useStore($projectTree)
  const available = sessionBindingAvailable() && !isDesktopFsRemoteMode()

  // Cache-first status + one suggestion lookup per session change. No polling.
  useEffect(() => {
    if (!available || !sessionId) {
      return
    }

    ensureOwnerGrantStatus()
    ensureSessionBinding(profile, sessionId)
    ensureSessionBindingSuggestion({ ...session, profile })
    // Identity (plus the first workspace report) drives this; the store dedupes, so no refetch storm.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [available, profile, sessionId, session.cwd, session.git_repo_root])

  const record = entry?.record ?? null
  // Unknown (still loading) reads as signable; only a definite "can't sign" disables the pill.
  const canSign = !(grant && grant !== 'error' && !grant.canSign)
  const state = bindingPillState(record, suggestion, canSign)
  const sections = useMemo(() => bindingPickerSections(tree, suggestion?.path ?? null), [tree, suggestion?.path])

  if (!available || !sessionId) {
    return null
  }

  const boundRoot = record && record.state !== 'unbound' ? record.project_root : null
  const boundName = boundRoot ? pathLeaf(record?.repo_common_root || boundRoot) : null

  const boundBranch = boundRoot
    ? laneBranchFor(tree, boundRoot) ||
      (suggestion && isUnderPath(boundRoot, suggestion.path) ? suggestion.branch : null)
    : null

  const boundLabel = boundName ? (boundBranch ? `${boundName} · ${boundBranch}` : boundName) : null

  const label =
    state === 'bound'
      ? boundLabel || copy.unbound
      : state === 'needs_reconfirm'
        ? copy.needsReconfirm(boundLabel || boundName || '')
        : state === 'suggested'
          ? copy.suggested(suggestion?.name || '')
          : state === 'signing_off'
            ? boundLabel || copy.unbound
            : copy.unbound

  const tip =
    state === 'signing_off'
      ? copy.signingOffTip
      : entry?.status === 'error' && !record
        ? copy.statusUnavailableTip
        : state === 'bound'
          ? copy.boundTip(boundRoot || '', remoteHost(record?.repo_remote))
          : state === 'needs_reconfirm'
            ? copy.needsReconfirmTip(boundRoot || '')
            : state === 'suggested'
              ? copy.suggestedTip(suggestion?.path || '')
              : copy.unboundTip

  const pill = (
    <button
      aria-disabled={state === 'signing_off' || undefined}
      aria-label={label}
      className={cn(PILL_BASE, PILL_STATE_CLASS[state], className)}
      data-slot="session-binding-pill"
      data-state={state}
      type="button"
    >
      <Codicon className="shrink-0" name={PILL_ICON[state]} size="0.6875rem" />
      <span className="min-w-0 truncate">{label}</span>
    </button>
  )

  if (state === 'signing_off') {
    return <Tip label={tip}>{pill}</Tip>
  }

  const firstBind = !record || record.state === 'unbound'

  const bindTo = async (path: string, name: string) => {
    // A first bind's in-app confirm names the exact root main resolved and will sign (b10 review);
    // a re-bind of a live build gets main's native confirm, which names it too.
    const confirmRoot = firstBind
      ? (resolved: SessionBindingResolvedRoot) =>
          confirm({
            title: copy.confirmTitle(name),
            description: copy.confirmDescription(resolved.project_root),
            confirmLabel: copy.confirmLabel
          })
      : undefined

    const outcome = await setSessionBinding({ profile, hermes_session_id: sessionId, path }, { confirmRoot })
    const reason = outcomeReason(outcome)

    if (reason && reason !== 'cancelled') {
      notify({ kind: 'error', title: copy.failedTitle, message: copy.failed(reason) })
    }
  }

  const pickOtherFolder = async () => {
    const [path] = await selectDesktopPaths({
      title: copy.pickerOtherFolderTitle,
      defaultPath: suggestion?.path || undefined,
      directories: true,
      multiple: false
    })

    if (path) {
      await bindTo(path, pathLeaf(path) || path)
    }
  }

  const unbind = async () => {
    const outcome = await clearSessionBinding({ profile, hermes_session_id: sessionId })
    const reason = outcomeReason(outcome)

    if (reason && reason !== 'cancelled') {
      notify({ kind: 'error', title: copy.failedTitle, message: copy.failed(reason) })
    }
  }

  const onOpenChange = (open: boolean) => {
    if (!open) {
      return
    }

    // Opening is the retry for a failed read, and the moment to load a tree nobody fetched yet.
    if (entry?.status === 'error') {
      void refreshSessionBinding(profile, sessionId)
    }

    if (!$projectTreeLoaded.get()) {
      void refreshProjectTree()
    }
  }

  const isCurrent = (path: string) => Boolean(boundRoot) && normalizePath(path) === normalizePath(boundRoot || '')

  return (
    <DropdownMenu onOpenChange={onOpenChange}>
      <Tip label={tip} side="bottom">
        <DropdownMenuTrigger asChild>{pill}</DropdownMenuTrigger>
      </Tip>
      <DropdownMenuContent
        align="start"
        className="max-h-80 min-w-56 max-w-80 overflow-y-auto"
        data-slot="session-binding-picker"
        sideOffset={6}
      >
        {suggestion && (
          <DropdownMenuGroup>
            <DropdownMenuLabel className={dropdownMenuSectionLabel}>{copy.pickerSuggested}</DropdownMenuLabel>
            <DropdownMenuItem
              data-binding-path={suggestion.path}
              data-slot="session-binding-option"
              onSelect={() => void bindTo(suggestion.path, suggestion.name)}
            >
              <Codicon name="link" size="0.75rem" />
              <span className="min-w-0 truncate">
                {suggestion.branch ? `${suggestion.name} · ${suggestion.branch}` : suggestion.name}
              </span>
              {isCurrent(suggestion.path) && <Codicon className="ml-auto" name="check" size="0.75rem" />}
            </DropdownMenuItem>
          </DropdownMenuGroup>
        )}

        {suggestion && <DropdownMenuSeparator />}

        <DropdownMenuLabel className={dropdownMenuSectionLabel}>{copy.pickerProjects}</DropdownMenuLabel>
        {sections.length === 0 && (
          <DropdownMenuItem disabled>
            <span className="text-(--ui-text-tertiary)">{copy.pickerEmpty}</span>
          </DropdownMenuItem>
        )}
        {sections.map(section => (
          <DropdownMenuGroup key={section.id}>
            {section.options.map(option => (
              <DropdownMenuItem
                data-binding-path={option.path}
                data-slot="session-binding-option"
                key={option.key}
                onSelect={() => void bindTo(option.path, option.label)}
                title={option.path}
              >
                <Codicon
                  className={cn('shrink-0', option.depth === 1 && 'ml-2.5', option.depth === 2 && 'ml-5')}
                  name={option.icon}
                  size="0.75rem"
                />
                <span className="min-w-0 truncate">{option.label}</span>
                {option.detail && option.detail !== option.label && (
                  <span className="min-w-0 truncate text-(--ui-text-tertiary)">{option.detail}</span>
                )}
                {isCurrent(option.path) && <Codicon className="ml-auto" name="check" size="0.75rem" />}
              </DropdownMenuItem>
            ))}
          </DropdownMenuGroup>
        ))}

        <DropdownMenuSeparator />
        <DropdownMenuItem data-slot="session-binding-other" onSelect={() => void pickOtherFolder()}>
          <Codicon name="folder-opened" size="0.75rem" />
          {copy.pickerOtherFolder}
        </DropdownMenuItem>
        {record && record.state !== 'unbound' && (
          <DropdownMenuItem data-slot="session-binding-unbind" onSelect={() => void unbind()}>
            <Codicon name="debug-disconnect" size="0.75rem" />
            {copy.pickerUnbind}
          </DropdownMenuItem>
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
