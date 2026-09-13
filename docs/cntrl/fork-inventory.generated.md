# Fork inventory — generated

Generated 2026-09-13 by `scripts/cntrl/fork_inventory.py`. Do not edit; edit `fork-inventory.md` for the why.

| | |
|---|---|
| branch | `cntrl-hermes` @ c0deb07a67 |
| vs `origin/main` | 84 ahead / 297 behind · common base 1c671beab2 (2026-09-12) |
| PR #65982 | `pr-65982-new` = 2c458827d9 · 41 ahead / 297 behind origin/main · HEAD is built on it |
| carried commits (since pr-65982-new) | 43 |
| core files touched | 62 (0 unlisted in §3) |

## Carried commits (oldest first)

| date | sha | subject | core files |
|---|---|---|---|
| 2026-09-13 | c0deb0754c | cntrl: carry — bounded atexit join of background reviews on one-shot exit; .hermes-test ignore; CNTRL-HERMES n | — |
| 2026-09-13 | c0deb0791a | cntrl: carry — Aug 26 council ledger + resume notes (code of c0deb07ff4 is already in PR #65982 head) | — |
| 2026-09-13 | c0deb07e33 | models: add claude-fable-5-1 to the Anthropic catalog (claude-agent-sdk picker) | `agent/model_metadata.py`, `hermes_cli/models_catalog_static.py` |
| 2026-09-13 | c0deb07c93 | cntrl: carry — bounded atexit join of background reviews on one-shot exit (code half of c0deb07f4a) | `run_agent.py` |
| 2026-09-13 | c0deb0723b | claude-sdk: bump to 0.2.150 (bundles Claude Code 2.1.257) and add cli_path | `agent/transports/claude_agent_sdk_session.py`, `agent/transports/claude_agent_sdk_session_config.py`, `hermes_cli/config_defaults.py`, `pyproject.toml`, `tests/agent/test_claude_sdk_session_core.py`, `tools/lazy_deps.py`, `uv.lock` |
| 2026-09-13 | c0deb07dc1 | CNTRL-HERMES: Sep 1 results — streaming proven, Fable 5.1 in the SDK picker | — |
| 2026-09-13 | c0deb07998 | CNTRL-HERMES: pgvector was proven Aug 16; restored psycopg + embed key Sep 1 | — |
| 2026-09-13 | c0deb0763a | claude-sdk: agent.claude_agent_sdk.plugins — load named plugin roots, keep isolation | `agent/transports/claude_agent_sdk_session.py`, `agent/transports/claude_agent_sdk_session_config.py`, `hermes_cli/config_defaults.py`, `tests/agent/test_claude_sdk_session_core.py`, `website/docs/user-guide/features/claude-agent-sdk-runtime.md` |
| 2026-09-13 | c0deb07a15 | CNTRL-HERMES: item 5 decided — conductor via plugins:, isolation kept | — |
| 2026-09-13 | c0deb07917 | claude-sdk: skill auto-capture on this runtime + compact skill guidance | `agent/claude_sdk_runtime.py`, `agent/claude_sdk_runtime_prompt.py`, `agent/transports/hermes_tool_exposure.py`, `tests/agent/test_claude_sdk_runtime_glue.py`, `tests/agent/test_claude_sdk_system_prompt.py`, `tests/agent/transports/test_hermes_tools_mcp_server.py`, `website/docs/user-guide/features/claude-agent-sdk-runtime.md` |
| 2026-09-13 | c0deb078d7 | docs(cntrl): kanban one-way sync — proposal, mapping, v0/v1/v2 plan | — |
| 2026-09-13 | c0deb07b13 | cntrl_sync v0: one-way cntrl ↔ Hermes kanban sync plugin (out-of-tree) | — |
| 2026-09-13 | c0deb0709e | docs(cntrl): kanban sync v0 uses a daemon thread, settings list matches plugin.yaml | — |
| 2026-09-13 | c0deb07b78 | CNTRL-HERMES: kanban sync v0 re-proven in the live desktop after relaunch; my-tasks is workspace-scoped | — |
| 2026-09-13 | c0deb075b7 | CNTRL-HERMES: tb-workers MCP proven inside Hermes after the conductor .mcp.json fix | — |
| 2026-09-13 | c0deb0789e | cntrl: CLAUDE.md fork rules + docs/cntrl/fork-inventory.md (what we carry, merge procedure, open SDK-runtime c | — |
| 2026-09-13 | c0deb07602 | cntrl: fork-inventory generator + /fork-inventory skill + rule; inventory rebuilt on the PR #65982 head | — |
| 2026-09-13 | c0deb07681 | cntrl: inventory — session-to-session tools proven inside the SDK runtime (ListAgents lists 12 live sessions) | — |
| 2026-09-13 | c0deb07223 | cntrl: public-repo rule in CLAUDE.md; ignore docs/telemetry | — |
| 2026-09-13 | c0deb07485 | claude-sdk: live MCP reload — /reload-mcp rotates the SDK session, next turn resumes in a fresh CLI | `agent/claude_sdk_runtime.py`, `agent/claude_sdk_runtime_continuity.py`, `gateway/run_turn.py`, `hermes_cli/cli_info_mixin.py`, `tests/agent/test_claude_sdk_session_identity.py`, `tests/tools/test_refresh_agent_mcp_tools.py`, `tools/mcp_tool_agent.py`, `tui_gateway/methods_tools.py` |
| 2026-09-13 | c0deb079a5 | cntrl: inventory snapshot after live-reload commit | — |
| 2026-09-13 | c0deb07258 | cntrl: inventory — gateway suite result classified (9 upstream, 7 order flakes, 0 ours) | — |
| 2026-09-13 | c0deb07fb2 | claude-sdk: name the spawned session so peers can address it (host-router seam) | `agent/claude_sdk_runtime_session.py`, `agent/transports/claude_agent_sdk_session.py`, `agent/transports/claude_agent_sdk_session_config.py`, `hermes_cli/config_defaults.py`, `tests/agent/test_claude_sdk_session_core.py`, `website/docs/user-guide/features/claude-agent-sdk-runtime.md` |
| 2026-09-13 | c0deb07609 | cntrl_router: out-of-tree session route registry + host-routing skill | — |
| 2026-09-13 | c0deb0786a | cntrl_groups: session groups as a project layer, out-of-tree | — |
| 2026-09-13 | c0deb079ef | cntrl_groups: real Hermes plugin — /groups, hermes groups, opt-in auto-tag by repo | — |
| 2026-09-13 | c0deb07d25 | fix(titles): a truncated model reply no longer becomes the session name | `agent/title_generator.py`, `tests/agent/test_title_generator.py` |
| 2026-09-13 | c0deb07014 | cntrl: router safety rules + the two traps that bit today | — |
| 2026-09-13 | c0deb07d5e | cntrl: inventory — list the title-generator test file in section 3 | — |
| 2026-09-13 | c0deb07419 | cntrl_groups: ls counts live sessions, not stale tags; add gc | — |
| 2026-09-13 | c0deb07e77 | cntrl_router: registry belongs to the install, not the profile | — |
| 2026-09-13 | c0deb07b74 | cntrl: Hermes-to-Hermes messaging works — deliver_background_results is the missing key | — |
| 2026-09-13 | c0deb07305 | claude-sdk desktop: peer replies display, tab renames reach peers, attachment titles, hermes: peer preference | `agent/claude_sdk_runtime.py`, `agent/claude_sdk_runtime_continuity.py`, `agent/claude_sdk_runtime_prompt.py`, `agent/claude_sdk_runtime_session.py`, `agent/title_generator.py`, `agent/transports/claude_agent_sdk_session.py`, `agent/transports/claude_agent_sdk_session_turn.py`, `agent/transports/claude_agent_sdk_session_watchdog.py`, `hermes_cli/web_routers/sessions.py`, `tests/agent/test_claude_sdk_session_identity.py`, `tests/agent/test_claude_sdk_streaming.py`, `tests/agent/test_title_generator.py`, `tests/tui_gateway/test_sdk_background_result_delivery.py`, `tools/process_registry_notifications.py`, `tui_gateway/methods_session.py`, `tui_gateway/session_notifications.py` |
| 2026-09-13 | c0deb079ea | desktop: stop the hermes:api 404 storm — latch a session gone when every profile 404s | `apps/desktop/src/app/session/hooks/use-session-actions/resolve-stored-session.test.ts`, `apps/desktop/src/app/session/hooks/use-session-actions/utils.ts`, `apps/desktop/src/store/runtime-gone.test.ts`, `apps/desktop/src/store/session-gone-latch.ts` |
| 2026-09-13 | c0deb07d06 | cntrl: inventory — attachment streaming stall bisected at three layers, not reproduced | — |
| 2026-09-13 | c0deb079b1 | desktop: the 404 storm's real driver — tile transcript reconcile sent no profile | `apps/desktop/src/app/contrib/hooks/use-background-sync.test.ts`, `apps/desktop/src/app/contrib/hooks/use-background-sync.ts` |
| 2026-09-13 | c0deb07ffe | desktop: turns ran invisibly after a websocket reconnect — fan detached-session events out to live sockets | `agent/claude_sdk_aux_client.py`, `agent/claude_sdk_runtime_continuity.py`, `tests/agent/claude_sdk_fakes.py`, `tests/agent/test_aux_cli_path.py`, `tests/agent/test_claude_sdk_session_identity.py`, `tests/tui_gateway/test_detached_event_fanout.py`, `tui_gateway/server.py` |
| 2026-09-13 | c0deb077e0 | cntrl: SDK lane runs Claude Code's auto mode — guardian one-shots only on prompt fall-through | — |
| 2026-09-13 | c0deb070bc | claude-sdk: fork default permission_mode=auto; doctor reports the mode's cost; default-mode sessions warn | `agent/transports/claude_agent_sdk_session.py`, `hermes_cli/config_defaults.py`, `hermes_cli/doctor_auth.py`, `tests/agent/test_claude_sdk_session_core.py`, `tests/hermes_cli/test_claude_agent_sdk_config.py`, `website/docs/user-guide/features/claude-agent-sdk-runtime.md` |
| 2026-09-13 | c0deb07e6f | CNTRL-HERMES: SDK tool cards reach the desktop; settings page re-seeds after a profile switch; sdk-lane ops do | `agent/claude_sdk_runtime_session.py`, `agent/transports/claude_agent_sdk_session.py`, `agent/transports/claude_agent_sdk_session_notify.py`, `agent/transports/claude_agent_sdk_session_turn.py`, `apps/desktop/src/app/settings/config-settings.test.tsx`, `apps/desktop/src/app/settings/config-settings.tsx`, `tests/agent/test_claude_sdk_streaming.py` |
| 2026-09-13 | c0deb0793d | docs(cntrl): sdk-lane ops — tool cards live-proven on the restarted desktop; renderer profile recipe | — |
| 2026-09-13 | c0deb073a4 | docs(cntrl): sdk-lane ops — per-step latency measured (model, not Hermes); vision pinned off Haiku | — |
| 2026-09-13 | c0deb07a67 | claude-sdk: plugin skills reach every slash surface; scrub PYTHONPATH so plugin MCPs start | `agent/claude_sdk_slash.py`, `agent/transports/claude_agent_sdk_session.py`, `agent/transports/claude_agent_sdk_session_billing.py`, `agent/transports/claude_agent_sdk_session_config.py`, `cli.py`, `gateway/run_inbound.py`, `hermes_cli/cli_tui_mixin.py`, `tests/agent/test_claude_sdk_configured_env.py`, `tests/agent/test_claude_sdk_session_core.py`, `tests/agent/test_claude_sdk_slash.py`, `tests/cli/test_cli_sdk_slash.py`, `tests/gateway/test_unknown_command.py`, `tests/test_tui_gateway_server.py`, `tui_gateway/methods_complete.py`, `tui_gateway/methods_tools.py` |

## Core files we touch

| file | commits | listed in §3 |
|---|---|---|
| `agent/claude_sdk_aux_client.py` | c0deb07ffe | yes |
| `agent/claude_sdk_runtime.py` | c0deb07917, c0deb07485, c0deb07305 | yes |
| `agent/claude_sdk_runtime_continuity.py` | c0deb07485, c0deb07305, c0deb07ffe | yes |
| `agent/claude_sdk_runtime_prompt.py` | c0deb07917, c0deb07305 | yes |
| `agent/claude_sdk_runtime_session.py` | c0deb07fb2, c0deb07305, c0deb07e6f | yes |
| `agent/claude_sdk_slash.py` | c0deb07a67 | yes |
| `agent/model_metadata.py` | c0deb07e33 | yes |
| `agent/title_generator.py` | c0deb07d25, c0deb07305 | yes |
| `agent/transports/claude_agent_sdk_session.py` | c0deb0723b, c0deb0763a, c0deb07fb2, c0deb07305, c0deb070bc, c0deb07e6f, c0deb07a67 | yes |
| `agent/transports/claude_agent_sdk_session_billing.py` | c0deb07a67 | yes |
| `agent/transports/claude_agent_sdk_session_config.py` | c0deb0723b, c0deb0763a, c0deb07fb2, c0deb07a67 | yes |
| `agent/transports/claude_agent_sdk_session_notify.py` | c0deb07e6f | yes |
| `agent/transports/claude_agent_sdk_session_turn.py` | c0deb07305, c0deb07e6f | yes |
| `agent/transports/claude_agent_sdk_session_watchdog.py` | c0deb07305 | yes |
| `agent/transports/hermes_tool_exposure.py` | c0deb07917 | yes |
| `apps/desktop/src/app/contrib/hooks/use-background-sync.test.ts` | c0deb079b1 | yes |
| `apps/desktop/src/app/contrib/hooks/use-background-sync.ts` | c0deb079b1 | yes |
| `apps/desktop/src/app/session/hooks/use-session-actions/resolve-stored-session.test.ts` | c0deb079ea | yes |
| `apps/desktop/src/app/session/hooks/use-session-actions/utils.ts` | c0deb079ea | yes |
| `apps/desktop/src/app/settings/config-settings.test.tsx` | c0deb07e6f | yes |
| `apps/desktop/src/app/settings/config-settings.tsx` | c0deb07e6f | yes |
| `apps/desktop/src/store/runtime-gone.test.ts` | c0deb079ea | yes |
| `apps/desktop/src/store/session-gone-latch.ts` | c0deb079ea | yes |
| `cli.py` | c0deb07a67 | yes |
| `gateway/run_inbound.py` | c0deb07a67 | yes |
| `gateway/run_turn.py` | c0deb07485 | yes |
| `hermes_cli/cli_info_mixin.py` | c0deb07485 | yes |
| `hermes_cli/cli_tui_mixin.py` | c0deb07a67 | yes |
| `hermes_cli/config_defaults.py` | c0deb0723b, c0deb0763a, c0deb07fb2, c0deb070bc | yes |
| `hermes_cli/doctor_auth.py` | c0deb070bc | yes |
| `hermes_cli/models_catalog_static.py` | c0deb07e33 | yes |
| `hermes_cli/web_routers/sessions.py` | c0deb07305 | yes |
| `pyproject.toml` | c0deb0723b | yes |
| `run_agent.py` | c0deb07c93 | yes |
| `tests/agent/claude_sdk_fakes.py` | c0deb07ffe | yes |
| `tests/agent/test_aux_cli_path.py` | c0deb07ffe | yes |
| `tests/agent/test_claude_sdk_configured_env.py` | c0deb07a67 | yes |
| `tests/agent/test_claude_sdk_runtime_glue.py` | c0deb07917 | yes |
| `tests/agent/test_claude_sdk_session_core.py` | c0deb0723b, c0deb0763a, c0deb07fb2, c0deb070bc, c0deb07a67 | yes |
| `tests/agent/test_claude_sdk_session_identity.py` | c0deb07485, c0deb07305, c0deb07ffe | yes |
| `tests/agent/test_claude_sdk_slash.py` | c0deb07a67 | yes |
| `tests/agent/test_claude_sdk_streaming.py` | c0deb07305, c0deb07e6f | yes |
| `tests/agent/test_claude_sdk_system_prompt.py` | c0deb07917 | yes |
| `tests/agent/test_title_generator.py` | c0deb07d25, c0deb07305 | yes |
| `tests/agent/transports/test_hermes_tools_mcp_server.py` | c0deb07917 | yes |
| `tests/cli/test_cli_sdk_slash.py` | c0deb07a67 | yes |
| `tests/gateway/test_unknown_command.py` | c0deb07a67 | yes |
| `tests/hermes_cli/test_claude_agent_sdk_config.py` | c0deb070bc | yes |
| `tests/test_tui_gateway_server.py` | c0deb07a67 | yes |
| `tests/tools/test_refresh_agent_mcp_tools.py` | c0deb07485 | yes |
| `tests/tui_gateway/test_detached_event_fanout.py` | c0deb07ffe | yes |
| `tests/tui_gateway/test_sdk_background_result_delivery.py` | c0deb07305 | yes |
| `tools/lazy_deps.py` | c0deb0723b | yes |
| `tools/mcp_tool_agent.py` | c0deb07485 | yes |
| `tools/process_registry_notifications.py` | c0deb07305 | yes |
| `tui_gateway/methods_complete.py` | c0deb07a67 | yes |
| `tui_gateway/methods_session.py` | c0deb07305 | yes |
| `tui_gateway/methods_tools.py` | c0deb07485, c0deb07a67 | yes |
| `tui_gateway/server.py` | c0deb07ffe | yes |
| `tui_gateway/session_notifications.py` | c0deb07305 | yes |
| `uv.lock` | c0deb0723b | yes |
| `website/docs/user-guide/features/claude-agent-sdk-runtime.md` | c0deb0763a, c0deb07917, c0deb07fb2, c0deb070bc | yes |
