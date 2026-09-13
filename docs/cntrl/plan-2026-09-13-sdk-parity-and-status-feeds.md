# Plan — 2026-09-13: SDK parity rebuild, status-stack feeds, session bugs

Source: `docs/cntrl/bug-inventory-2026-09-12.md` (items 1–5) + Justin's 09-13 direction.
Owner rulings baked in: hook into Hermes's EXISTING todo/subagent/background rows (no plugin UI);
native Claude Task tools, state mirrored; kanban stays separate; Astra seats run @ high.

## Facts established (read-only, 09-13)

- Upstream `origin/main` has NO Claude Agent SDK transport. The lane comes from upstream PR #65982
  (`fcavalcantirj/hermes-agent:upstream/claude-sdk-parity`), still OPEN, 41 commits, **rebased onto
  main @ 2026-09-12 23:17 +0530** (`2c458827d9`). We sit on its 09-07 head (`ad43250612`) + 42 own
  commits. `pr-65982-new` local ref was STALE until force-fetched (`+pull/65982/head`).
- 21 PR commits since our head: the author split `claude_agent_sdk_session.py` into 11 sibling
  modules (`_availability _billing _child _compaction _config _input _notify _permissions _sanitize
  _turn _watchdog`), `claude_sdk_runtime.py` into 8 (`_compaction _context _continuity _fallback
  _prompt _session _state _tools _usage`), and `test_claude_sdk_runtime.py` into 16 modules; plus
  fixes: preserve terminal result over late stop, workspace-bound resume ids, both halves of agent
  stop, post-terminal stop decline, `claude_sdk_session_id` schema history.
- Our SDK footprint to re-home: 10 commits, ~540 lines across `claude_agent_sdk_session.py` (315),
  `claude_sdk_runtime.py` (196), `claude_sdk_aux_client.py` (42), `hermes_tool_exposure.py` (7).
  `git merge-tree` dry-run vs PR head: ~31 conflicted paths (SDK files, tests, pyproject/uv.lock,
  config_defaults, doctor_auth, gateway/run*, tui_gateway/session_notifications, docs).
- Versions: CLI 2.1.270 on PATH = npm latest. SDK: we pin 0.2.150 (bundles CLI 2.1.257), PR pins
  0.2.144, PyPI latest **0.2.152** (2026-09-02). Keep ours → bump to 0.2.152 in Phase 1.
- Desktop status stack ALREADY has groups `todo | subagent | background | goal`
  (`apps/desktop/src/app/chat/composer/status-stack/index.tsx:47-73`). Feeds:
  `todo.updated` (`tui_gateway/tool_progress.py:244`, from `_TODO_TOOL_NAMES` tool results,
  cached per session), `subagent.spawn_requested/start/progress/thinking/complete`
  (`apps/desktop/src/store/subagents.ts`, `gateway-event/tools.ts:141`), background store.
  The SDK lane emits `tool.start/complete` only and SILENCES subagent streams
  (`claude_agent_sdk_session.py:3019-3168`, `parent_tool_use_id` gate) — that is item 5's root.
- Native Task tools are env-gated in the CLI (`CLAUDE_CODE_ENABLE_TODO_TOOLS=1`,
  `CLAUDE_CODE_ENABLE_TASKS` Task* vs legacy TodoWrite); config surface exists:
  `agent.claude_agent_sdk.env`. Store: `~/.claude/tasks/<list-id>/<n>.json` (private lock protocol —
  never hand-write). Council verdict adopted (job `w_20260912T213200Z_13cb`).
- Conductor plugin dev checkout: `/Users/justin/Documents/Projects/Business/thinkbot-plugin/plugins/conductor`
  (installed 3.60.2 from marketplace `ThinkBotHQ/thinkbot-plugin`).

## Phases

### Phase 1 — Rebuild on the fresh PR head (upstream + parity in one move)
Procedure = fork-inventory §5. Backup tag `backup/cntrl-hermes-pre-rebase-2026-09-13`.
1. `git rebase --onto pr-65982-new ad43250612 cntrl-hermes` — only our 42 commits move.
2. Re-home the 10 SDK commits onto the split modules (our hunks → the sibling that now owns the
   code: cli_path/plugins/env → `_config`; session_name/rename/rotate → `_child`/runtime `_session`;
   on_tool_use/on_tool_result cards → `_turn`; permission_mode=auto → `_permissions`/config_defaults;
   skills guidance → runtime `_prompt`). Tests: our `TestSdkToolCards` etc. move into the matching
   split test module.
3. Keep our pins: `claude-agent-sdk==0.2.152` (bump), `cli_path`, `permission_mode: auto`.
4. Gates: `scripts/run_tests.sh` (SDK + gateway + tui_gateway suites), `bun run check`, one live
   SDK turn in the desktop (CNTRL-HERMES.md "Desktop dev launch"). Fork-inventory rebuilt
   (`/fork-inventory`). NO push until Justin's go (/tb-ship).

### Phase 2 — Investigations on the merged tree (Astra @ high, read-only, parallel)
- **A. Cohesion/parity:** Hermes-native features vs what the SDK lane exposes (todo/subagent/
  background feeds, slash surfaces, approvals, memory, skills, session tools, attachments, steer/
  queue) → classify each gap: wire-in (core seam) · Hermes plugin · skill · not worth it.
- **B. Bugs — state/tracking:** session-id mapping (Hermes ↔ Claude ↔ resume/fork), tool-card
  loss (4k flatten, no `tool_use_result`, subagent silence), cross-session message rendering
  (item 3), spurious "Background process unknown exited" (item 3b), hermes-tools ↔ Claude tools
  identity map both directions.
- **C. Conductor plugin review** (spawned 09-13 before the merge; independent):
  correctness, dead lanes, MCP startup fragility in the Hermes repo, doc drift.
- **D. Upstream PR candidates** back to #65982 / fcavalcantirj: from fork-inventory §3 rows that
  are generic (cli_path, plugins list, session_name, rename seam, tool cards, rotate-while-busy
  guard, title scaffolding guard, detached-event fan-out, background-result delivery).

### Phase 3 — Build waves (consolidated from 2A parity map + 2B bug hunt; awaiting Justin's go)
Each wave: implementer ≠ validator, cross-model; E2E on a temp HERMES_HOME; one live desktop turn.
Refs: `review-2026-09-13-sdk-parity-map.md` (A), `review-2026-09-13-sdk-lane-bugs.md` (B).

| wave | inventory | scope | anchors | size |
|---|---|---|---|---|
| W1 | item 1 | SDK engine is the default for new desktop sessions; plain-Anthropic group demoted | model picker + `config_defaults` provider default | S |
| W2 | item 3 | Cross-session turns render in the recipient tab + unread: project provenance-marked peer input and the full unsolicited turn (B#5, A row "Cross-session"); own message identity for background deliveries after Stop (B#6); ownership by `parent_session_id` before dequeue (B#1); per-payload ack (B#7). Live re-check that the phantom process notice is gone (`session_notifications.py:495`). | `_turn.py:929-1022`, `session_notifications.py:62-98,416-449`, `use-message-stream/index.ts:575` | M |
| W3 | item 5b/5c | Tool-card fidelity: keep raw text/array results (B#8), carry `is_error` (B#10), labelled truncation + `tool_use_result` (B#11), resolve open cards before the parent filter + interrupt drains (B#9) | `tool-parts.ts:45,272,334`, `_notify.py:144-161`, `projector.py:34,183` | M |
| W4 | item 5a | Subagent rows with NAME + open-to-view: lifecycle Task*Message → existing `subagent.*` (A#1), `goal` = Agent `description`/`subagent_type` (A row NAME), child tool/text into owned child transcripts (B#12, A#5); SDK-backed `methods_subagents` lookup/tail/cancel via `stop_task` | `projector.py:98,134`, `_notify.py:120`, `tool_progress.py:407`, `methods_subagents.py:19-78` | L |
| W5 | item 4 | Todo feed from native Task tools: config default `CLAUDE_CODE_ENABLE_TODO_TOOLS=1`; per-session `CLAUDE_CODE_TASK_LIST_ID` persisted beside `claude_sdk_session_id`, kept across resume/rotate, fresh on fork; reconcile `~/.claude/tasks/<list>` snapshots → `todo.updated` (A#2, B#14 name normalization at `tool_progress.py:274` / `todos.ts:23`) | `_config.py:54,109`, `claude_agent_sdk_session.py:635`, `claude_sdk_runtime_session.py:419`, `_continuity.py` | M |
| W6 | item 5 (bg) | SDK background processes in the process view + stop via `stop_task` (A#4) | `methods_tools.py:233-243`, `projector.py:106` | M |
| W7 | carries | cntrl-only bugs in our own carries: rotation stash honours workspace binding (B#2); rotation serialized with turn admission (B#4); rename-ack swallow requires a pending rename (B#15); hybrid identity keeps `skill_manage` (B#13); skill-cache invalidation matches namespaced name (B#16) | `_continuity.py:49,169`, `_watchdog.py:199`, `hermes_tool_exposure.py:124`, `gateway-event/tools.ts:104` | S–M |
| W8 | item 2 | Sessions spawning sessions: verify kanban dispatcher / `session.create` RPC coverage; expose the minimal seam for a manager session | `tui_gateway/methods_session.py`, `plugins/kanban` | S (investigate) → ? |
| W9 | parity | streaming + reasoning defaults (A#8), steer delivery accounting (A#6), `session_search detail` (A row), gateway slash uses live names (A row), `max_turns` mapping | see A | S each |
| PR | upstream | back to #65982: fable-5-1 catalog id; B#3 #5 #8 #9 #10 #11 #12 #14; fork-inventory §3 generic rows (cli_path, plugins, session_name, rename seam, tool cards, rotate guard, detached fan-out) | — | — |

Suggested order: W1 → W7 (cheap, our own bugs) → W2 → W3 → W4 → W5 → W6 → W9 → W8; PR batch after W3.

## Status log
- 09-13: Phase 1 started. Phase 2C spawned.
- 09-13: Phase 1 step 1–2 DONE — 43 commits rebased onto `2c458827d9` (backup tag
  `backup/cntrl-hermes-pre-rebase-2026-09-13` = old `c0deb07abd`). All 10 SDK commits re-homed onto
  the split siblings; monolith `test_claude_sdk_runtime.py` retired, our tests live in
  `test_claude_sdk_session_core.py` (cli_path, plugins, session_name, permission_mode, init slash),
  `_streaming.py` (rename, tool cards), `_session_identity.py` (rotate/rename helpers),
  `_system_prompt.py` + `_runtime_glue.py` (skills guidance, unrouted-review warn).
  Catalog: kept the dashed `claude-fable-5-1` id over upstream's dotted `claude-fable-5.1`
  (metadata matching normalizes dots→dashes but the id is SENT raw) → upstream PR candidate.
  Gate (env -u HERMES_HOME): 3476 passed / 5 failed, all classified NOT OURS —
  `test_tui_gateway_server.py::test_load_enabled_toolsets_{rejects_disabled_mcp_env,falls_back_when_tui_env_invalid}`
  fail identically on the pristine PR head (upstream/env); `test_subagent_snapshot.py` ×2 and
  `test_session_create_continues_when_state_db_is_unavailable` pass in isolation (order flakes).
  Desktop: vitest 38/38 on touched files, tsc clean, eslint clean after two post-rebase test
  fix-ups (`vi.waitFor` import; `{ passive: true }` third arg in use-background-sync). Not pushed.
- 09-13: Phase 2C DONE → `docs/cntrl/review-2026-09-13-conductor-plugin.md` (28 findings).
  Phase 2A (cohesion map) + 2B (state/tracking bugs) spawned on the rebased tree.
- 09-13: Phase 2A DONE → `review-2026-09-13-sdk-parity-map.md`; 2B DONE → `review-2026-09-13-sdk-lane-bugs.md`.
  Phase 3 waves consolidated above; waiting on Justin's go + push decision.
