# Bug / feature inventory — 2026-09-12

Running inventory for this session. One entry per report. Status: `open` until planned.
Append new entries at the bottom; keep numbering stable.

## 1. SDK engine should be the default for new sessions — `open`
**Reported by:** Justin (screenshot 14:42)
**Symptom:** New desktop session lands on the plain Anthropic engine (unlabeled top group of the
model picker: Opus/Sonnet 4.x, Haiku 4.5). That engine isn't what we use, so a fresh session
doesn't work until the user manually picks a "CLAUDE AGENT SDK" model (Fable 5.1 / Fable 5 /
Opus 5 / Sonnet 5).
**Expected:** New sessions default to the SDK engine (Fable 5.1). Plain-Anthropic group demoted
or hidden.

## 2. Sessions should be able to spawn new sessions — `open`
**Reported by:** Justin
**Ask:** A manager or bug-report session should be able to open a new Hermes session in the
right project and hand it a task on its own (e.g. "file these bugs on the kanban"). SendMessage
reaches *existing* sessions; unclear whether creation exists. Verify before planning.

## 3. Inbound cross-session messages don't render in the recipient tab — `open`
**Reported by:** hermes:manager (relayed 14:4x), Hermes v0.21.0 (+1402), commit c0deb07
**Repro:** A (manager) → SendMessage → B (cleanup). B does the work (git/gh, audit table) and
replies. A renders the reply fine. B's tab still shows its last human-visible turn — no inbound
`<cross-session-message>`, no tool calls, no outbound SendMessage. Looks like nothing arrived.
**Expected:** Inbound cross-session turns + outbound SendMessage appear in the recipient
transcript (collapsed OK) and the tab gets an unread/activity indicator.
**Possibly related:** Sender gets a spurious
`[IMPORTANT: Background process unknown exited (exit code ?). Command: unknown Output: (empty)]`
immediately after each cross-session reply arrives (2/2 on 2026-09-09; no background processes
launched).

## 4. Session task inventory ("tasks" widget) in the desktop chat — `open` (investigating; see item 5 — same panel)
**Reported by:** Justin (screenshot 16:19)
**Ask:** The SDK lane doesn't expose `TaskCreate`/`TaskUpdate`/`TodoWrite`, so there's no
in-session task list. Implement as a **plugin**: tasks rendered in the chat session, above the
composer's fork/checkout header row (green branch icon + `cntrl-hermes`). Other sessions
(manager) should be able to write to it.
**Status:** council verdict in (GPT-6 Astra @ xhigh, Codex read-only, 18/20; job w_20260912T213200Z_13cb) — ADOPTED.
**Design (from council, verified against binary + repo):**
- Native Claude Task tools are env-gated, not headless-restricted: `CLAUDE_CODE_ENABLE_TODO_TOOLS=1`
  bypasses the model-availability gate; `CLAUDE_CODE_ENABLE_TASKS` (default on) selects
  TaskCreate/Get/Update/List vs legacy `TodoWrite` (do NOT pick legacy — it's in-memory only);
  `CLAUDE_CODE_TASK_LIST_ID` overrides the list (process-scoped). Hermes already has the surface:
  `agent.claude_agent_sdk.env` (claude_agent_sdk_session.py:493/523, merged after the scrub at :1385).
  Per-session list-id needs session-aware wiring; a constant in config would share one list.
- SDK's bundled CLI is 2.1.257 (SDK 0.2.150), not the PATH 2.1.270 — gates verified in both;
  `agent.claude_agent_sdk.cli_path` picks the binary (:631).
- State authority = Claude's on-disk task store `~/.claude/tasks/<list>/<n>.json`. A backend plugin
  reconciles from snapshots (poll/watch + bootstrap on resume), using tool activity only as a refresh
  signal. Renderer is a cache. NOT desktop-only, NOT a second Hermes copy.
- Tool-card chain is lossy for a reducer: results flattened to 4k text
  (claude_sdk_event_projector.py:34), `tool_use_result` not forwarded (:3164), child-subagent cards
  suppressed, TaskCreate input carries no id (pair by tool_use_id). Hence snapshots, not stream-only.
- Identity: Hermes session id != Claude session id (`sessions.claude_sdk_session_id`,
  hermes_state_common.py:279; failed resume can rotate it, claude_sdk_runtime.py:1710). Persist an
  explicit list-id mapping per Hermes session; freeze across child restarts; define fork behavior.
- Manager writes: do NOT write JSON by hand — private protocol (`.lock.lock` dir, `<id>.json.lock`
  dir with heartbeat, highwatermark id allocation, non-atomic writes). Use a separate writer process
  running native Task tools with `CLAUDE_CODE_TASK_LIST_ID=<target>`, or deterministic plugin CRUD
  (ctx.register_tool, hermes_cli/plugins.py:449 — needs exposure via the SDK bridge, :3218 /
  hermes_tool_exposure.py:113) if manager traffic grows.
- Render: `COMPOSER_AREAS.top` mounts at composer/index.tsx:1348, BELOW `CodingStatusRow` (:1321),
  above the input. Gap: slot gets no session props — multiple composer tiles would show the wrong
  list. Widen the SDK with a small generic composer-session context (slot.tsx:4, scope.tsx:12).
- Native (non-SDK) lane: separate mechanism — reuse Hermes's own `tools/todo_tool.py` store +
  `todo.updated` snapshot events (tui_gateway/tool_progress.py:228).
- Cache-safe: env set before first request; keep identical config across resumes.
- Kanban: one-way sync later at most; not the store.

## 5. Active subagents panel + named agent cards + tool visibility — `open`
**Reported by:** Justin (screenshot 16:30)
**Symptom (screenshot):** After "I'll spawn a Gemini worker through the…", the transcript shows only
`Using 9 tools` (rolled up, no per-tool rows) and an inline `Running agent  21s` row. The row has
no name — the agent's given name ("Gemini worker" / subagent_type) exists only in the prose — and
there is no way to open it to see what it is doing. Separately, tool calls are sometimes not shown
at all ("sometimes we can't see tools either").
**Ask:**
- (a) Hermes desktop plugin: an **active subagents** panel above the composer's fork/checkout chip
  (same `COMPOSER_AREAS.top` slot as the tasks widget, item 4), listing running/finished subagents
  with their **given name**, status, elapsed time; click to open and view that agent's transcript /
  tool stream live.
- (b) Inline agent card must carry the agent's name (SDK `Agent` tool `description` /
  `subagent_type` / agentId), not the generic "Running agent".
- (c) Tool-call visibility: audit why tool rows are sometimes missing on the SDK lane (relates to
  commit c0deb07497 "SDK tool cards reach the desktop"; likely a subset of message types / nested
  subagent tool calls not surfaced). Name matters for tracking across sessions.
**REVISED 09-13 (owner):** no plugin UI. Hermes ALREADY renders `todo | subagent | background` groups above the composer (`status-stack/index.tsx:47-73`), fed by `todo.updated` (`tool_progress.py:244`) and `subagent.*` events (`store/subagents.ts`). The SDK lane emits only `tool.start/complete` and silences subagent streams (`claude_agent_sdk_session.py:3019-3168`). Fix = emit the existing events from the SDK lane. Plan: `docs/cntrl/plan-2026-09-13-sdk-parity-and-status-feeds.md`.
**Design note (superseded detail):** item 4 (tasks) and item 5 (agents) are the same mechanism — mirror native SDK
state (task JSON on disk / SDK `TaskStartedMessage`, `TaskProgressMessage`, `TaskUpdatedMessage`,
`TaskNotificationMessage` for background agents) into one composer-top status panel. The SDK's
Task*Message types ARE the background-agent lifecycle stream — that is the data source for (a).

## 6. Rebuild on the fresh #65982 head + upstream parity — `in progress`
**Reported by:** Justin (09-13). Details + procedure in `docs/cntrl/plan-2026-09-13-sdk-parity-and-status-feeds.md` Phase 1.

## 7. Transcript can't scroll while a long tool runs — `open`
**Reported by:** Justin (screenshot 09-13 12:56, session gen_bug_fix)
**Symptom:** During a 2m+ Bash call (`Running bash …` spinner, `Using 16 tools` rolled up) the
transcript is pinned to the bottom, no scrollbar thumb, wheel/trackpad scroll does nothing.
**Anchors:** `apps/desktop/src/store/thread-scroll.ts` (`$threadScrolledUp`, `setThreadAtBottom`,
`requestScrollToBottom`), `components/assistant-ui/thread/transcript-window.tsx`, `thread/list.tsx`,
`use-messages-below.ts`, `chat/scroll-to-bottom-button.tsx`. Likely: follow-output re-pins on every
tool-card tick (duration counter re-render) so a user scroll-up is undone before it registers.

## 8. Queued message never auto-sent — `open`
**Reported by:** Justin (screenshot 09-13 13:21)
**Symptom:** A prompt queued during a long turn ("1 Queued · 1 attachment") stayed queued after the
turn ended; it only went out when the user acted (send-now/interrupt).
**Anchors:** `apps/desktop/src/store/composer-queue.ts` (`$parkedQueueSessions`, `isSteerableEntry` —
entries WITH attachments are never steerable), `chat/composer/hooks/use-composer-queue.ts` (auto-drain
gated on `busy`, `drainFailuresRef` + `MAX_AUTO_DRAIN_ATTEMPTS`, park on repeated failure),
`session/hooks/use-background-queue-drain.ts` (:164-188 busy/lineage check). Hypotheses: (a) drain
attempts fired while the SDK turn was still busy, hit the cap and parked the queue; (b) on the SDK
lane `busy` never flipped false between the tool-heavy turn and the next (see parity map "Steer /
queue" row and B#3 turn-ownership). Repro: queue a prompt with an attachment during a >2m tool.

## 9. Public-fork hygiene: conductor/private artifacts — `done 09-13` (verify on next push)
`.gitignore` gains repo-level entries (`.claude/state/`, `.claude/settings.local.json`,
`.claude/mcp-needs-auth-cache.json`, `.claude/*.json`, `docs/councils/`, `.tb-*/`) — previously only
covered by global excludes, which teammates' clones don't have. `docs/councils/council-log.jsonl`
(internal strategy council record) untracked. Still tracked on purpose: `CNTRL-HERMES.md`,
`docs/cntrl/*` (incl. the conductor review, which cites private plugin file:lines — Justin to decide
whether that stays public), `.claude/skills/fork-inventory/SKILL.md`.

## 10. tb-workers MCP + conductor subagents dead in NEW sessions — `root-caused 09-13, mitigated`
**Reported by:** Justin (09-13, "worked 1-2d ago, we didn't change much")
**Evidence:** this session (CLI child spawned 09-12 14:41) still has both. The plugin loaded by
Hermes is the marketplace clone (`~/.claude/plugins/marketplaces/thinkbot-plugin/plugins/conductor`),
auto-updated to 3.60.2 on 09-12 22:29; its `.mcp.json` is byte-identical to 3.59.0. Probe of that
launcher outside Hermes (PYTHONPATH unset): first run answered `tools/list` after **211.6 s**
(uv had to build the ephemeral `--with "mcp>=2.1.1,<2.2"` env for 3.14 — `environments-v2/d22081…`
created 13:49, wheels fetched into `archive-v0`); warm re-run: `initialize` 0.7 s, `tools/list` 0.7 s.
Claude Code's MCP startup timeout (`MCP_TIMEOUT`, default 30 s) kills the cold launch, marks the
server failed for ~15 min per process, and the conductor agents' MCP calls fail with it → "MCP and
subagents dead". Intermittent by construction: any cache invalidation (new interpreter, new `mcp`
release — 2.2.0 shipped 09-07, uv cache prune) re-arms it.
**Mitigation (applied, profile config, no restart needed for NEW sessions):**
`agent.claude_agent_sdk.env.MCP_TIMEOUT: "240000"` in `.hermes-test/profiles/thinkbot/config.yaml`.
**Durable fix (plugin repo, ties to conductor review #1):** stop using an ephemeral `uv run --with`
env for the MCP launcher — ship a locked venv inside the plugin (or `uv sync` on install) and launch
`.venv/bin/python tb_seat_mcp.py`; pin `--python 3.12` meanwhile (2 s warm, no 3.14 wheel gaps).
