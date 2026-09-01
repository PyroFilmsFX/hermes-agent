# cntrl ↔ Hermes kanban — one-way sync

Status: proposal + v0 plan, 2026-09-01. Follows `CNTRL-HERMES.md` §3 (two boards on purpose,
sync one-way and narrow) and the Aug 26 council ruling (Hermes is the spine, cntrl the control
plane, all our code out-of-tree in `$HERMES_HOME/plugins/`).

## 1. What syncs, and what never does

**Down, cntrl → Hermes.** A cntrl task is pushed into one user's Hermes board when all three hold:

1. it is assigned to a user who has a Hermes profile (identity map, §5);
2. it carries the label `hermes` — an explicit opt-in, because cntrl has no dev-task vocabulary
   (`taskType`/`category`/`area` are free `varchar(30)` with no constraint or seed);
3. its `category` is not `bizops`.

Fields that travel: title, description, priority, the cntrl id. Nothing else.

**Up, Hermes → cntrl.** Status only, plus one comment on completion carrying the Hermes result
text. No comments, attachments, assignee changes, or field edits flow up.

**Never.** Bizops tasks (clients, deals, invoices) never reach Hermes. There is no reason for the
agent to see an invoice. Anything richer than one-way status is scope creep — §3 already says so.

## 2. Surfaces that exist today

### Hermes (`hermes-cntrl`, branch `cntrl-hermes`)

| Surface | Where | Notes |
|---|---|---|
| `tasks` table | `hermes_cli/kanban_db.py:1333-1426` | `id, title, body, assignee (profile name), status, priority INTEGER, created_by, created_at/started_at/completed_at, tenant, project_id, session_id, idempotency_key, result` + dispatcher fields |
| statuses | `kanban_db.py:102` | `triage, todo, scheduled, ready, running, blocked, review, done, archived` |
| external key | `idempotency_key` (`kanban_db.py:1355`, index `:2654`) | documented for "retried webhooks / automation" (`:3158`); the only slot for a foreign id |
| write API | `create_task :3121`, `assign_task :3665`, `complete_task :5315`, `add_comment :3945`, `list_tasks :3613`, `get_task :3594` | import `hermes_cli.kanban_db` directly from the plugin |
| plugin hooks | `hermes_cli/plugins.py:256-345` | `on_kanban_task_updated` (assign/priority/title/body; carries changed field names), `kanban_task_completed`, `kanban_task_blocked`, `on_kanban_dispatch_tick` |
| plugin contract | `hermes_cli/plugins.py:19-20`, fields `:648-662` | `plugin.yaml` + `__init__.py` with `register(ctx)`; `ctx.register_hook`, `ctx.spawn_task(coro)`, `ctx.state`, `ctx.get_config` |
| HTTP routes | `plugins/kanban/dashboard/plugin_api.py` | mounted at `/api/plugins/<name>` from `dashboard/manifest.json` (`web_server.py:17649-17758`); user plugins must be listed in `plugins.enabled` (`:17690`) |
| outbound feed | `WS /api/plugins/kanban/events` (`plugin_api.py:2892`) | tails `task_events` by integer cursor — the replay source if the sync ever runs out of process |

No external-sync precedent exists in the kanban plugin. The one outward push
(`kanban_notify_subs` → `gateway/kanban_watchers.py:179`) is chat-shaped, not HTTP.

### cntrl (`~/Documents/Projects/Business/thinks-mini`, branch `thinks-core`, port 3101)

| Surface | Where | Notes |
|---|---|---|
| `tasks` | `server/src/db/schema/schema.ts:2237-2310`, migrations `012, 055, 056, 062, 092` | `id uuid, title, description, status, priority (low/medium/high), assignedTo uuid→users, assignee text, assigneeAgentId, projectId, workspaceId, teamId, dueDate, createdAt/updatedAt, labels text[], metadata jsonb, taskType, area, category, scope, visibility, parentTaskId, assignedSessionId` |
| statuses | `migrations/062_dag_swarm_unification.sql:36-40` | `todo, in_progress, in_review, done, cancelled, pending_approval, backlog, ready, active, review, failed` |
| read | `GET /api/tasks/my-tasks` (`routes/tasks.ts:888`) | per-user, workspace-member scoped, `limit/offset`, optional `workspace_id` |
| write | `PUT /api/tasks/:id/move {status, position}` (`:1477`), `PATCH /api/tasks/:id` (`:1359`), `POST /:id/comments` (`:2005`) | `move` is the status transition the board itself uses |
| auth | `middleware/requireAuth.ts:63-90`, `middleware/apiKeyAuth.ts:28-65` | JWT cookie or `Authorization: Bearer tb_<64 hex>`; keys minted by `POST /api/api-keys` (`routes/api-keys.ts:27-50`), SHA-256 stored |
| events | `services/event-bus/event-bus.ts:81` | `task.status_changed` (`routes/tasks.ts:1503,1547,1627`), `task.assigned` (`:2166,2210`) — in-process + `domain_events` table |
| outbound webhooks | `services/webhook-outbound.ts:23` | **only** `task.completed` and `deal.stage_changed`; HMAC `X-Thinks-Signature`; subscriptions in `workspaces.settings.webhooks` (no route manages them) |

## 3. Field mapping (down)

| cntrl `tasks` | Hermes `tasks` | rule |
|---|---|---|
| `id` | `idempotency_key = "cntrl:<uuid>"` | dedupe on create; reverse lookup by prefix scan of the user's board |
| `title` | `title` | as is |
| `description` | `body` | as is, followed by a fenced `cntrl` block: workspace, project, labels, due date, link |
| `priority` low/medium/high | `priority` 0/1/2 | ordinal |
| `assignedTo` / `assignee` | `assignee` | identity map, §5 |
| `workspaceId` | `board` | one Hermes board per cntrl workspace |
| `labels`, `dueDate`, `metadata` | — | carried only inside the body block |
| `projectId` | — | different id spaces; body block only |

### Status (down, cntrl → Hermes)

| cntrl | Hermes |
|---|---|
| `todo`, `backlog`, `ready`, `pending_approval` | `todo` |
| `in_progress`, `active` | `todo` (Hermes owns run state; it is never told it is running) |
| `in_review`, `review` | `review` |
| `done` | `done` |
| `cancelled`, `failed` | `archived` |

### Status (up, Hermes → cntrl)

| Hermes | cntrl |
|---|---|
| `triage`, `todo`, `scheduled`, `ready` | `todo` |
| `running` | `in_progress` |
| `review` | `in_review` |
| `blocked` | `todo` + comment "blocked: <reason>" (cntrl has no blocked status; `012_kanban_board.sql:18` uses a label for it — the plugin adds label `blocked`) |
| `done` | `done` + comment with the Hermes result |
| `archived` | `cancelled` |

Only these transitions go up. A status Hermes sets that maps to the status cntrl already has is a
no-op (no echo loops: the down-sync ignores changes whose mapped Hermes status equals the current one).

## 4. Mechanism

### v0 — poll + hooks, no cntrl changes (buildable today)

Hermes plugin `cntrl_sync` in `$HERMES_HOME/plugins/cntrl_sync/`:

- `plugin.yaml`: `hooks: [on_kanban_task_updated, kanban_task_completed, kanban_task_blocked]`,
  `config_schema` for `base_url`, `api_key_env`, `poll_seconds`, `label`, `profile_map`,
  `board_prefix`. No pip dependencies (`urllib` only — the SDK env allowlist and the lazy-install
  quarantine both stay untouched).
- `register(ctx)`: registers the three hooks; starts the poll loop with `ctx.spawn_task()`
  (there is no plugin-facing cron; `on_kanban_dispatch_tick` is the alternative anchor).
- Poll: `GET /api/tasks/my-tasks?limit=200` with the user's `tb_` key → filter (§1) → for each
  task `create_task(idempotency_key="cntrl:<uuid>", …)` or refresh title/body/priority when
  they changed (compare against `ctx.state["seen"][uuid]`), and map a terminal cntrl status onto
  the Hermes task (`done` → `complete_task`, `cancelled` → archive).
- Flow-back: each hook resolves the Hermes task, reads its `idempotency_key`, and if it starts
  with `cntrl:` calls `PUT /api/tasks/<uuid>/move` with the mapped status (position kept) and
  `POST /api/tasks/<uuid>/comments` for `done`/`blocked`. Failures are logged and retried on the
  next poll (the poll also re-compares status, so a missed push self-heals).
- State: `ctx.state` — last poll time, `seen` map (uuid → {hermes_id, fingerprint, status}).

### v1 — push from cntrl (needs cntrl changes)

- `webhook-outbound.ts:23`: add `task.assigned` and `task.status_changed` to the outbound list.
- `tasks.metadata.hermes_task_id` written by the plugin on create (via `PATCH /:id`), so cntrl's
  UI can link to the Hermes task.
- A route to manage `workspaces.settings.webhooks` (today only DB helpers write it).
- Plugin gains `dashboard/manifest.json` → `plugin_api.py` with `POST /api/plugins/cntrl_sync/webhook`
  verifying `X-Thinks-Signature`; the poll loop stays on as reconciliation at a longer interval.

### v2 — surfaces

- cntrl task card shows Hermes status + link; Hermes task shows the cntrl link (already in the body).
- The popped-out single-session panel (§5 of `CNTRL-HERMES.md`) opens the Hermes run for a task.

## 5. Gaps found, and what closes them

| # | Gap | Closes it |
|---|---|---|
| 1 | No external-id column on either side | Hermes: `idempotency_key="cntrl:<uuid>"`; cntrl: `metadata.hermes_task_id` (v1) |
| 2 | No user ↔ profile identity map | plugin config `profile_map` (`{<cntrl user id or email>: <hermes profile>}`); v0 is single-user |
| 3 | cntrl emits no webhook for assignment/status change | v0 polls; v1 extends the outbound list |
| 4 | No route manages webhook subscriptions | v1 |
| 5 | Hermes has no outbound HTTP notifier | the plugin owns the PUT/POST back (hooks are the trigger) |
| 6 | No `updated_at` on Hermes tasks | not needed for status flow-back (hooks); reconciliation compares status, not time |
| 7 | No dev-task predicate in cntrl | label `hermes` + `category != bizops` |
| 8 | No plugin-facing scheduler | `ctx.spawn_task()` loop |

## 6. Where the code lives

Out-of-tree per the council, but versioned: `cntrl-plugins/cntrl_sync/` on branch `cntrl-hermes`
(nothing under Hermes' `plugins/`, nothing Hermes imports — zero merge tax), symlinked into
`$HERMES_HOME/plugins/cntrl_sync`. The alternative is `thinks-mini/hermes/plugins/` in the cntrl
repo; it keeps cntrl-facing code next to cntrl, at the cost of two repos per change. Justin decides.

## 7. Decisions needed

1. Label name (`hermes`) and whether `category != bizops` is enough, or an allow-list of `taskType`.
2. Poll interval for v0 (proposal: 30 s).
3. Whether a pushed task lands as Hermes `todo` (human starts it) or `ready` (dispatcher may pick
   it up). Proposal: `todo`.
4. §6.

## 8. Test plan

- Unit: fake `ctx`, temp `HERMES_HOME` with a real `kanban_db`, fake HTTP via monkeypatched
  `urllib`; cover filter, create/dedupe, refresh, terminal-status down, and each flow-back hook.
- Live: create a task in cntrl (`POST /api/tasks` with label `hermes`), watch it appear on the
  Hermes board, complete it in Hermes, watch cntrl move it to `done` with the result comment.
