# SDK-lane state/tracking bug hunt — 2026-09-13 (Phase 2B)

Seat: GPT-6 Astra @ high via Codex CLI, read-only, on the rebased tree (`c0deb079aad`).
Job `w_20260913T180330Z_d9ca`, log `.claude/state/worker-spawn/logs/w_20260913T180330Z_d9ca.log`
(450s). Wrapper spot-checked #1 and #2 line-for-line (both confirmed). Not yet triaged by a human.
Inventory cross-refs: item 3 (cross-session rendering) = #5, #6, #1; item 5c (missing tool rows) =
#8, #9, #10, #11, #12; item 4/5 task feeds = #14 + the task-list wiring note.

## Findings (ranked, verbatim shape: impact/confidence · file:line · defect · fix)

1. **[high, certain]** `tui_gateway/session_notifications.py:98` — An SDK result with an empty
   `session_key` and `parent_session_id="B"` can be consumed by tab A: the foreign-owner check
   returns early at :62 and the required-owner gate ignores the parent id. → Require ownership for
   every SDK result and route by its parent Hermes id before dequeuing. *(cntrl-only)*
2. **[high, certain]** `agent/claude_sdk_runtime_continuity.py:49` — Rotating in workspace A then
   switching to B before the next turn resumes A's Claude id: the rotation stash returns before the
   workspace-binding validation (:59-73). → Store the stash with its workspace binding; validate
   through the persisted-id path. *(cntrl-only)*
3. **[high, likely]** `agent/transports/claude_agent_sdk_session_turn.py:855` — A peer-initiated
   turn that starts idle but finishes after a foreground claim leaks its remaining messages into the
   foreground inbox; its first `ResultMessage` terminates the wrong turn (:591). → Track unsolicited
   turn ownership to its terminal result; defer foreground claims while it is active. *(upstream)*
4. **[high, likely]** `agent/claude_sdk_runtime_continuity.py:169` — A concurrent MCP refresh can
   observe no inbox, then close the CLI (:180) after a foreground turn claims it — rotation and turn
   admission share no lock. → Serialize rotation with admission incl. `_turn_claim_requested`
   (`_turn.py:479`); defer teardown once admission begins. *(cntrl-only)*
5. **[high, certain]** `agent/transports/claude_agent_sdk_session_turn.py:1011` — An idle
   recipient's peer-triggered work loses its inbound `<cross-session-message>`, tool calls and
   outbound `SendMessage`: unsolicited handling keeps only assistant text and logs the rest (:1022).
   → Project + persist the whole unsolicited turn with recipient-scoped tool events and a labelled
   inbound peer message. **= inventory item 3.** *(upstream)*
6. **[high, certain]** `apps/desktop/src/app/session/hooks/use-message-stream/index.ts:575` —
   After Stop, a later independent SDK background reply is discarded while the interrupt flag is
   set; `message.start` refuses to re-arm (`gateway-event/message-stream.ts:121`) → no unread
   transition. → Give background deliveries their own message identity/append path. *(cntrl-only)*
7. **[high, certain]** `tui_gateway/session_notifications.py:416` — A delivery exception consumes
   the whole background-result event (deduped before delivery, caught :445, returned consumed :449)
   without retrying remaining payloads. → Ack per payload after delivery; retain undelivered ones
   with stable dedup ids. *(cntrl-only)*
8. **[med, certain]** `apps/desktop/src/lib/chat-messages/tool-parts.ts:272` — Plain-text SDK tool
   output (Bash stdout) vanishes from live cards: `parseMaybeJsonObject` returns `{}` for non-JSON
   (:45) and `toolResult` keeps no raw-text fallback. → Preserve string/array results. *(upstream)*
9. **[med, certain]** `agent/transports/claude_agent_sdk_session_notify.py:144` — A result matching
   an open card is dropped when its message carries `parent_tool_use_id`; results drained after an
   interrupt bypass completion callbacks (`_turn.py:555`). → Resolve known open ids before the parent
   filter; keep terminal bookkeeping during interrupt drains. *(upstream)*
10. **[med, certain]** `agent/transports/claude_agent_sdk_session_notify.py:161` — Tool failures
    become an `"Error: …"` string; the desktop card only reads `payload.error`
    (`tool-parts.ts:334`) → failed tools render as success. → Carry `is_error` through runtime
    callback → gateway payload → card. *(upstream)*
11. **[med, certain]** `agent/transports/claude_sdk_event_projector.py:183` — Tool results lose
    everything past 4,000 chars (:46) with no marker and drop the SDK's `tool_use_result` metadata;
    the live callback flattens the same way (`_notify.py:160`). → Store full results + metadata;
    truncate only display previews, labelled. *(upstream)*
12. **[med, certain]** `agent/transports/claude_agent_sdk_session_notify.py:120` — Native SDK
    subagent tools produce neither cards nor child activity: cards filtered here, named
    breadcrumbs discarded at `tui_gateway/tool_progress.py:407`. → Translate parent-tagged tool
    activity into scoped `subagent.*` events keyed by the spawning tool id. **= inventory item 5.**
    *(upstream)*
13. **[med, certain]** `agent/transports/hermes_tool_exposure.py:124` — Enabling the hybrid bridge
    renames `skill_manage` from `mcp__hermes-tools__skill_manage` to
    `mcp__hermes-hybrid__skill_manage` (SDK profile includes it at :110, legacy identity set omits
    it) → exact-name grants and display normalization break. → Add the SDK skill tools to the
    preserved legacy identity set. *(cntrl-only)*
14. **[med, certain]** `tui_gateway/tool_progress.py:274` — SDK `TodoWrite` and hybrid
    `mcp__hermes-hybrid__todo_list` never yield authoritative todo snapshots (backend accepts bare
    Hermes names only; desktop repeats it at `apps/desktop/src/lib/todos.ts:23`). → Normalize
    trusted runtime identities for task-state projection; adapt payloads, keep execution identity.
    **= inventory item 4 seam.** *(upstream)*
15. **[med, certain]** `agent/transports/claude_agent_sdk_session_watchdog.py:199` — A legitimate
    background answer starting "Session renamed to:" is swallowed as a rename ack even with no
    rename pending (`_turn.py:967`). → Require a pending rename and correlate the exact ack.
    *(cntrl-only)*
16. **[low, certain]** `apps/desktop/src/app/session/hooks/use-message-stream/gateway-event/tools.ts:104`
    — skill create/rename through either SDK MCP namespace doesn't invalidate slash completions
    (handler compares raw name to `skill_manage`; runtime forwards the namespaced name,
    `claude_sdk_runtime_session.py:182`). → Match the trusted Hermes identity. *(cntrl-only)*

**Task-tool wiring (unnumbered):** `_configured_sdk_env` can pass the enable vars
(`claude_agent_sdk_session_config.py:109`) but child options derive no per-session task-list id
(`claude_agent_sdk_session.py:635`). Derive/persist it from the Hermes session, pass from
`_create_session` (`claude_sdk_runtime_session.py:419`), preserve through resume/rotation, allocate
a fresh id on explicit fork. Child enablement/default-list behaviour not live-verified.

## Premises checked and NOT bugs
- Fork does not copy the parent's Claude id (`tui_gateway/methods_session.py:237`).
- Rename-pending IS cleared (`claude_sdk_runtime_session.py:546`).
- The phantom "Background process unknown exited" notice is already blocked at
  `tools/process_registry_notifications.py:362` (inventory item 3b — verify live before closing).

## Seat's fix order
#1 wrong-recipient delivery → #2 cross-workspace resume → #3 foreground/background misattribution.
Upstream-PR candidates: #3 #5 #8 #9 #10 #11 #12 #14. cntrl-only: #1 #2 #4 #6 #7 #13 #15 #16.
