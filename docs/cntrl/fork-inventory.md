# Fork inventory — what cntrl changed in Hermes, and how we keep up with upstream

Last audit: 2026-09-07 (rebuilt on the PR #65982 head that day). Branch `cntrl-hermes`. Upstream `origin/main`.
Machine-derived snapshot with dates: `fork-inventory.generated.md`; run history: `fork-inventory-log.md`.

## 1. Shape of the carry

| layer | commits | lives in | merge cost |
|---|---|---|---|
| Claude Agent SDK runtime | 20 (the PR head itself) | upstream PR #65982 (author `fcavalcantirj`) | none for us: we sit ON the PR head. Not ours. |
| cntrl additions on the SDK runtime | 5 | core files, small hunks | low, listed in §3 |
| cntrl plugins + docs | 11 | `cntrl-plugins/`, `docs/cntrl/`, `CNTRL-HERMES.md`, `CLAUDE.md` | none |

Before 2026-09-07 the branch was 10,458 commits behind main and carried 74
commits (a stale copy of the PR plus ours). It was rebuilt that day: branch from
the PR head, re-apply only the cntrl commits. Three carried hunks were dropped
because the PR head already had them (routed review, startup traceback, picker
self-auth); the old branch is kept as `cntrl-hermes-pre-sep7`.

The PR author rebases #65982 onto fresh main every few days and squashes it
(20 commits on 2026-09-05, rebased again 2026-09-07). Every SDK fix we carried
(turn watchdog, background-result delivery, approval tool_use_id, setting_sources,
max_budget_usd, stream-death retire) is present in the PR head by content. The
four follow-ups from `fcavalcantirj/hermes-agent#3` (picker entry, background
review restore, one-shot join, startup tracebacks) are folded in too, except the
self-improve review routing which we still carry (see §3).

## 2. Two ways to sync, and which we use

- **Rebuild on the PR head (what we do while #65982 is open).** The author
  rebases the PR onto fresh main every few days. We fetch its head and rebase
  only our ~16 commits onto it. Conflicts touch only §3 files.
- **Merge** `origin/main` into our branch. One commit, one conflict round.
  Use this once the PR merges, or if the PR goes stale.
- **Rebase** our commits onto main directly. Not worth it while the PR is open;
  the SDK runtime would conflict on every sync.

Cadence target: merge upstream at least weekly. Smaller gap, smaller conflicts.

## 3. Core edits we own (cntrl-only, on top of the SDK runtime)

Each line is one reason to touch a core file. Keep our hunk on conflict unless
upstream moved the surrounding code.

| file | what | why |
|---|---|---|
| `agent/claude_sdk_runtime.py` | `_SDK_SKILLS_GUIDANCE` one-sentence skill guidance | the native `SKILLS_GUIDANCE` block trips Anthropic's harness screen (400 "out of extra usage") |
| `agent/claude_sdk_runtime.py`, `agent/transports/claude_agent_sdk_session.py` | `agent.claude_agent_sdk.plugins` list, passed as `--plugin-dir` | load conductor into SDK sessions with `setting_sources: []` kept (isolation, decided 2026-09-01) |
| `agent/transports/claude_agent_sdk_session.py` | `agent.claude_agent_sdk.cli_path` | pin the SDK to `~/.local/bin/claude` so it follows `claude update` |
| `agent/transports/hermes_tools_mcp_server.py` | `--profile` support, `skill_manage` served on the claude-agent-sdk profile | skill auto-capture silently dropped on the SDK runtime |
| `hermes_cli/models_catalog_static.py`, `agent/model_metadata.py` | `claude-fable-5-1` first in the Anthropic catalog + 1M context | SDK picker (catalog moved out of `hermes_cli/models.py` upstream) |
| `agent/transports/hermes_tool_exposure.py` | `CLAUDE_AGENT_SDK_SKILL_TOOLS = ("skill_manage",)` on the SDK profile | skill writer for auto-capture; profile machinery lives here upstream now |
| `tests/agent/test_claude_sdk_runtime.py`, `tests/agent/transports/test_hermes_tools_mcp_server.py` | tests for cli_path, plugins, skill guidance, warn-once | pin the rows above |
| `website/docs/user-guide/features/claude-agent-sdk-runtime.md` | cli_path, plugins, review routing paragraphs | user docs for the rows above |
| `hermes_cli/config_defaults.py`, `hermes_cli/inventory.py` | defaults for `plugins`, `cli_path` | config-only flags |
| `pyproject.toml`, `uv.lock`, `tools/lazy_deps.py` | `claude-agent-sdk==0.2.150`, exempt from the 14-day quarantine | 0.2.120 bundled a CLI that 400s on Fable 5.1 |
| `run_agent.py` | self-improve review routed off the SDK runtime | one-shot runs killed the review at exit |
| `.gitignore` | `.hermes-test/` | test home |
| `tui_gateway/session_notifications.py`, `tools/process_registry_notifications.py` | `sdk_background_result` delivered directly to the desktop chat (persist + `message.complete`), formatter refuses the type, `parent_session_id` proves ownership | desktop showed a phantom "Background process unknown exited" and the peer reply never displayed |
| `agent/title_generator.py` | `_strip_attachment_preamble` in `_summarize_user_message` | sessions were titled `[The user attached an image: …` |
| `agent/transports/claude_agent_sdk_session.py` | `rename()` via `/rename`, ack swallowed by text (`_is_rename_ack`) | tab renames now reach peers instantly |
| `agent/claude_sdk_runtime.py` | `rename_claude_sdk_session` + deferred rotation when busy; `hermes:` peer preference sentence in `_MCP_INSPECTION_PREFERENCE` (harness-probed OK 2026-09-09) | same; router guidance |
| `tui_gateway/methods_session.py`, `hermes_cli/web_routers/sessions.py` | rename hooks on `session.title` RPC and the REST PATCH the desktop uses | same |
| `tests/tui_gateway/test_sdk_background_result_delivery.py` | 6 tests | pins the delivery row |
| `tui_gateway/server.py` | `write_json` fans a detached session's event frames out to live WS transports (`_fan_out_detached_event`) | turns ran invisibly after a websocket reconnect |
| `tests/tui_gateway/test_detached_event_fanout.py` | 4 tests | pins the row above |
| `agent/claude_sdk_runtime.py` | `rotate_claude_sdk_session` defers while a turn is in flight | a between-turns MCP refresh must never close the CLI under a running turn |
| `agent/claude_sdk_aux_client.py`, `tests/agent/test_aux_cli_path.py` | `_build_aux_option_fields` passes the pinned `cli_path` | aux one-shots spawned the SDK's bundled CLI (35 spawns in a morning) |
| `apps/desktop/src/app/contrib/hooks/use-background-sync.ts` | plain tiles read their transcript under the sidebar row's profile; a gone answer latches the tile | the actual 404-storm driver: a thinkbot session asked of the default backend every tick |
| `apps/desktop/src/app/session/hooks/use-session-actions/utils.ts`, `apps/desktop/src/store/session-gone-latch.ts` | `resolveStoredSession` latches an id gone when every profile answers 404; the background-polling classifier accepts the REST `404 … Session not found` shape | the `hermes:api` 404 storm: a deleted id was re-probed across every profile on every 5s poll, forever |
| `tests/agent/test_title_generator.py` | 18 scaffolding cases | pins the title fix |
| `agent/title_generator.py` | `_is_scaffolding` guard + unterminated-fence strip in `_extract_title_text` | sessions were being named ```` ```json ```` and `{"title` from truncated model replies; 9 such rows existed. Upstream-shaped fix, worth proposing back. |
| `hermes_cli/config_defaults.py`, `hermes_cli/doctor_auth.py`, `agent/transports/claude_agent_sdk_session.py` | fork default `permission_mode: auto`; `hermes doctor` reports the mode and its cost; a session started in `default` logs a warning naming the guardian-spawn cost | nobody should discover the 35-spawn tax by reading logs |
| `agent/transports/claude_agent_sdk_session.py`, `agent/claude_sdk_runtime.py`, `hermes_cli/config_defaults.py` | `session_name` template -> the CLI's `--name` via `extra_args` | peers can address a Hermes session (host router) |
| `tools/mcp_tool_agent.py`, `hermes_cli/cli_info_mixin.py`, `gateway/run_turn.py`, `tui_gateway/methods_tools.py` | `sdk_rotate=` on `refresh_agent_mcp_tools` + `_maybe_rotate_sdk_session`; explicit `/reload-mcp` callers pass it | live MCP reload for SDK sessions |
| `agent/claude_sdk_runtime.py` | `rotate_claude_sdk_session` + in-memory resume stash in `_persisted_sdk_session_id` | same |
| `tests/tools/test_refresh_agent_mcp_tools.py` | rotation hook test | pins the row above |

## 4. Out-of-tree (zero merge cost)

- `cntrl-plugins/cntrl_sync/` — one-way cntrl ↔ Hermes kanban sync, v0. Docs in
  `docs/cntrl/kanban-one-way-sync.md`.
- `$HERMES_HOME/plugins/pgvector/` — memory provider (`hermes-memory-pgvector`),
  not in the repo, points at cntrl Postgres `:5434/thinkscore`.
- conductor — loaded via `agent.claude_agent_sdk.plugins`, lives in
  `~/.claude/plugins/marketplaces/…`.
- `cntrl-plugins/cntrl_router/` — session route registry + host-routing skill.
- `cntrl-plugins/cntrl_groups/` — session groups; own table in the state db.
- `docs/councils/council-log.jsonl`, `CNTRL-HERMES.md`, `docs/cntrl/`.

## 5. Merge procedure

```bash
git fetch origin
git fetch origin pull/65982/head:pr-65982-new
OLD=$(git merge-base cntrl-hermes pr-65982-sep7)      # the PR head we sit on now
git rebase --onto pr-65982-new $OLD cntrl-hermes       # only our commits move
# resolve. Files in §3 keep our hunks. Everything else takes upstream.
# If the PR is closed/merged instead: git merge origin/main.
uv sync --extra claude-agent-sdk --inexact             # --inexact keeps psycopg
.venv/bin/python -m pytest tests/agent/test_claude_sdk_runtime.py cntrl-plugins/cntrl_sync -q
# smoke: one real SDK turn in the desktop (CNTRL-HERMES.md, "Desktop dev launch")
git commit
.venv/bin/python scripts/cntrl/fork_inventory.py --check   # then commit its outputs
```

Gateway suite 2026-09-07 (7,817 pass / 16 fail): 9 of the 16 fail identically
on the pristine PR head; the other 7 (discord send, multi-image, session-store
prune, teams dotenv, telegram polling) pass in isolation on our branch and are
order-dependent flakes. None touch the carry. Not ours.

Two traps that bit on 2026-09-07, both now in CLAUDE.md: another Claude session
checked out `main` in this shared worktree mid-build (committed+pushed work was
untouched, which is the defence), and a `uv sync` on that branch stripped the
SDK, psycopg AND pytest so the next test run failed with `No module named
pytest` rather than anything meaningful.

Known: a headless one-shot (`hermes chat -q`) that calls a tool waits the full
300 s approval timeout with nobody to answer; not a runtime fault. The title
auxiliary lane once produced a `{"title` fragment as the session title (seen
2026-09-07 via gemini aux); harmless, not investigated.

## 6. Rules for teammates on their own branches

1. Branch from `cntrl-hermes`, not `main`.
2. Put your feature in `cntrl-plugins/<name>/` or `$HERMES_HOME/plugins/`.
   Ask before editing any file under `agent/`, `hermes_cli/`, `gateway/`.
3. Merge `cntrl-hermes` into your branch every day. Never rebase a shared branch.
4. If you must edit core, add a row to §3 in the same commit.

## 7. Open items to verify on the SDK runtime (tracked here, not done)

- **Session-to-session messaging — PROVEN 2026-09-07, both ways.** `ListAgents`
  and `SendMessage` are Claude Code CLI tools, so they ride the pinned `cli_path`
  binary (2.1.263). Probes in `.sdkprobe/` (`setting_sources=[]`): an SDK session
  inside Hermes listed 12 live sessions on the machine, then sent a message to
  the interactive Claude Code session that was driving this work; it arrived as
  a cross-session message. Goal stands: a host session in Hermes that routes
  work to the right session. Not yet done: Hermes sessions naming themselves so
  others can find them.
- **Live MCP reload inside the SDK runtime — DONE 2026-09-07.** The SDK's CLI
  holds its MCP/plugin list from process start, so `/reload-mcp` (CLI, gateway,
  TUI RPC) now rotates the live SDK session: `rotate_claude_sdk_session` closes
  the CLI, stashes the resume id, and the next turn rebuilds with fresh
  `mcp_servers` / `plugins` / `cli_path` and resumes the conversation. A tool
  surface change on any refresh path rotates too. Live proof
  (`.sdkprobe/rotate_probe.py`): two real turns, new CLI pid between them, the
  second turn recalled the first. Still true: `hermes mcp add` servers reach
  the SDK session only with `hybrid_mcp_bridge: true`; without it the SDK sees
  hermes-tools + `plugins:` only.
- **Desktop shows peer replies — DONE 2026-09-09.** With the flag on, the
  messaging gateway delivered bursts but the DESKTOP never did: its poller sent
  `sdk_background_result` through the generic process formatter, painting
  "[IMPORTANT: Background process unknown exited (exit code ?) Output: ]" and
  re-injecting that as a prompt while the real text sat unseen. Now persisted as an
  assistant row and painted as a completed message; the formatter refuses the type.
- **Tab renames reach peers — DONE 2026-09-09.** `--name` is fixed at CLI start, so
  a renamed tab kept its old peer name (`hermes:this session will be for managing
  the ci/cd…` for a tab called "ci/cd"). Proven: `/rename` over the SDK is instant
  and its ack is deterministic ("Session renamed to: X"), so idle sessions rename
  in place and the ack is swallowed; busy sessions rotate at the next turn.
- **Attachment titles — DONE 2026-09-09.** The desktop prepends
  `[The user attached an image: …]` to the typed text; titles now strip it.
- **Silent turns ("no updates on screen", "stuck", timer resets) — ROOT CAUSE
  FOUND AND FIXED 2026-09-10.** Not attachments, not streaming. A desktop
  websocket reconnect (09:41:47, `detached_sessions=2`) points the open tabs'
  sessions at the disconnected-WS sentinel; `prompt.submit` re-binds only when the
  request itself carries a transport, so those tabs' turns ran fully (7-8 min,
  33 tool calls) while every event frame was written to the drop sentinel.
  Proof: `session.events.since` on the live backend returned 266 and 221
  `message.delta` frames plus start/complete for the two tabs that nobody ever
  received. Fix in `tui_gateway/server.py` `write_json`: a detached session's
  event frames fan out to every live WS transport (clients route by
  `session_id`; the sentinel still records them for replay). 4 tests. Needs a
  backend restart to take effect. This also explains yesterday's "attachments
  stop streaming": every Vite HMR reload during my editing reconnected the
  socket and detached the open tab.
- **Rotation while a turn is in flight — guarded 2026-09-10.** `rotate_claude_sdk_session`
  now defers (pending flag, applied at the next turn start) when a turn owns the
  stream; a between-turns MCP refresh from the late-binding thread could
  otherwise close the CLI under a running turn. 1 test.
- **Guardian spawns — RESOLVED by config 2026-09-10 (owner's call).** Hermes'
  smart-approval guardian ran as an SDK one-shot for every Bash call that reached
  `can_use_tool` (35 spawns in one morning). Claude Code's own `auto` permission
  mode runs Anthropic's classifier inside the CLI, and the SDK invokes
  `can_use_tool` only when the flow falls through to a prompt (docs:
  agent-sdk/permissions, permission-modes; auto is the default starting mode on
  Pro/Max, no flag needed on CLI >= 2.1.207). Probe: a write command invoked the
  callback in `default` and never in `auto`. Set `agent.claude_agent_sdk.permission_mode: auto`
  in both test-home configs; new sessions pick it up. Trade-off, stated plainly:
  commands the classifier approves are no longer seen by Hermes' guardian or
  `approvals.smart_policy`; Hermes' immutable floors still apply to everything
  that does reach the callback.
- **Auxiliary one-shots spawn a fresh Claude CLI each time (CPU).** The desktop
  log shows `Using bundled Claude Code CLI` every 20-60 s: approval screening,
  titling and vision each spawn a full CLI process (node) with the SDK's
  bundled binary, ignoring `cli_path`. Real candidate for the "random node
  spikes". The binary mismatch is fixed (aux now uses `cli_path`); the spawn-per-call
  cost itself remains — a persistent aux client is the follow-up.

- **Streaming stalls with attachments — SUPERSEDED (see silent turns above).** Bisected
  at three layers with real turns: SDK transport (deltas flow), stdio JSON-RPC
  (image turn: 24 deltas), and the live desktop observed through CDP on the
  renderer's own socket with a real 3456x2168 screenshot attached: 36 deltas,
  then a tool-instructed image turn with 96 deltas, and the DOM grew as they
  arrived in both. What WAS seen: a 30-38 s silent lead before the first delta
  on image turns (the model works before it writes), then normal streaming. So
  the pipeline is proven end to end on `default`; the report came from the
  `thinkbot` profile on Fable 5.1 at Low effort with a 17-tool turn. To capture
  it next time it happens: `.sdkprobe/desktop_stream_repro.py` counts
  `message.delta` on the renderer's socket and samples the DOM per second.

- **Hermes-to-Hermes messaging — DONE 2026-09-08.** A peer `SendMessage` into
  a Hermes conversation needs `agent.claude_agent_sdk.deliver_background_results:
  true` in that profile's config; upstream defaults it false and drops the
  message with a WARN while the SENDER still sees success — silent loss, the
  worst shape for a router. Proven both ways on a live session: off, nothing
  arrives; on, the log shows "delivering unsolicited result burst" and the text
  reaches the chat. Config-only, no code change. Set in the test home's root and
  `profiles/thinkbot`.

- **404 retry storm — FIXED 2026-09-09 (renderer), two layers.** The REAL
  driver, found with a debugger breakpoint on the API bridge of the live desktop:
  the tile transcript reconcile (`use-background-sync.ts`) sent
  `/api/sessions/<id>/messages` with NO `?profile=` for any tile without an owner
  route, so a live `thinkbot` session ("manager") was asked of the `default`
  backend every sync tick, 404'd, and the catch swallowed it. Plain tiles now
  take the profile from their sidebar row, and a "Session not found" answer
  latches that tile until it rebinds. Live-proven: after the fix (HMR) the bridge
  showed zero transcript reads in 25 s. Second layer, same day, earlier: the
  cross-profile probe the cross-profile REST probe returned undefined when every profile 404'd but never latched the id gone, and the gone classifier only knew the JSON-RPC `4001` shape, so the 5s status poll re-probed a deleted id forever. Now the probe latches when every attempt was gone-shaped (transient errors do not count; the latch clears at the rebind seams) and the classifier accepts the REST shape. 5 vitest cases. Previously: Deleting sessions the
  desktop still references makes it poll `hermes:api` for a dead id forever:
  `Error occurred in handler for 'hermes:api': 404 {"detail":"Session not
  found"}` repeating with no backoff and no give-up. Trigger found 2026-09-08
  (a session wipe during a live desktop). Workaround: relaunch the desktop.
  Worth an upstream report; a permanent 404 should stop retrying.

- **Host router — DONE 2026-09-07.** Sessions are now named
  (`agent.claude_agent_sdk.session_name`, default `hermes:{title}`), so
  `ListAgents` / `SendMessage` can address a Hermes session. The map from work
  to owner is out-of-tree in `cntrl-plugins/cntrl_router/`: a JSON registry in
  `$HERMES_HOME/cntrl-routes.json`, a small CLI (`router.py list|add|find`),
  and a Hermes skill that teaches the protocol (read registry, verify the peer
  is live, hand off once, never guess). Live-proven: a host session matched
  "the upstream sync is behind", correctly picked `hermes-fork` over `kanban`,
  checked liveness, and the message arrived in the target session.

- **Session groups / projects — DONE 2026-09-07.** Out-of-tree in
  `cntrl-plugins/cntrl_groups/`: its own `cntrl_session_groups` table in the
  Hermes state db keyed by session id, joined back against `sessions`. Hermes'
  schema is never modified (pinned by a test), so upstream merges cannot break
  it and it drops out cleanly if upstream ever ships grouping. CLI:
  `groups.py ls|tag|untag|resume|auto`; `resume <group>` prints the newest id
  for `hermes --resume`. `auto` suggests groups from git repo root and never
  writes. A Hermes skill carries the protocol. 7 tests; proven against the real
  state db. Surfaced three ways with zero core edits: `/groups` in-session,
  `hermes groups` on the CLI, and an opt-in `on_session_start` hook that
  auto-tags a new session by its git repo (never overwrites a manual tag,
  fails open). 14 tests; proven inside the real plugin manager. Not done: a
  desktop filter. Hook points if we ever want native filtering are
  `hermes_cli/session_listing.py` (numbered /resume) and the `scope` dict in
  `hermes_cli/web_routers/sessions.py` (desktop REST).
