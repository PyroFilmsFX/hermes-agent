# Fork inventory — generated

Generated 2026-09-07 by `scripts/cntrl/fork_inventory.py`. Do not edit; edit `fork-inventory.md` for the why.

| | |
|---|---|
| branch | `cntrl-hermes` @ c0deb07c57 |
| vs `origin/main` | 48 ahead / 65 behind · common base 233757037d (2026-09-07) |
| PR #65982 | `pr-65982-sep7` = ad43250612 · 20 ahead / 65 behind origin/main · HEAD is built on it |
| carried commits (since pr-65982-sep7) | 28 |
| core files touched | 20 (0 unlisted in §3) |

## Carried commits (oldest first)

| date | sha | subject | core files |
|---|---|---|---|
| 2026-09-07 | c0deb07531 | cntrl: carry — bounded atexit join of background reviews on one-shot exit; .hermes-test ignore; CNTRL-HERMES n | — |
| 2026-09-07 | c0deb079aa | cntrl: carry — Aug 26 council ledger + resume notes (code of c0deb07ff4 is already in PR #65982 head) | — |
| 2026-09-07 | c0deb079b4 | models: add claude-fable-5-1 to the Anthropic catalog (claude-agent-sdk picker) | `agent/model_metadata.py`, `hermes_cli/models_catalog_static.py` |
| 2026-09-07 | c0deb0751f | cntrl: carry — bounded atexit join of background reviews on one-shot exit (code half of c0deb07f4a) | `run_agent.py` |
| 2026-09-07 | c0deb0750f | claude-sdk: bump to 0.2.150 (bundles Claude Code 2.1.257) and add cli_path | `agent/transports/claude_agent_sdk_session.py`, `hermes_cli/config_defaults.py`, `pyproject.toml`, `tests/agent/test_claude_sdk_runtime.py`, `tools/lazy_deps.py`, `uv.lock` |
| 2026-09-07 | c0deb07407 | CNTRL-HERMES: Sep 1 results — streaming proven, Fable 5.1 in the SDK picker | — |
| 2026-09-07 | c0deb072fc | CNTRL-HERMES: pgvector was proven Aug 16; restored psycopg + embed key Sep 1 | — |
| 2026-09-07 | c0deb07b3b | claude-sdk: agent.claude_agent_sdk.plugins — load named plugin roots, keep isolation | `agent/transports/claude_agent_sdk_session.py`, `hermes_cli/config_defaults.py`, `tests/agent/test_claude_sdk_runtime.py`, `website/docs/user-guide/features/claude-agent-sdk-runtime.md` |
| 2026-09-07 | c0deb076ef | CNTRL-HERMES: item 5 decided — conductor via plugins:, isolation kept | — |
| 2026-09-07 | c0deb07f0f | claude-sdk: skill auto-capture on this runtime + compact skill guidance | `agent/claude_sdk_runtime.py`, `agent/transports/hermes_tool_exposure.py`, `tests/agent/test_claude_sdk_runtime.py`, `tests/agent/transports/test_hermes_tools_mcp_server.py`, `website/docs/user-guide/features/claude-agent-sdk-runtime.md` |
| 2026-09-07 | c0deb07124 | docs(cntrl): kanban one-way sync — proposal, mapping, v0/v1/v2 plan | — |
| 2026-09-07 | c0deb07d32 | cntrl_sync v0: one-way cntrl ↔ Hermes kanban sync plugin (out-of-tree) | — |
| 2026-09-07 | c0deb079eb | docs(cntrl): kanban sync v0 uses a daemon thread, settings list matches plugin.yaml | — |
| 2026-09-07 | c0deb07a75 | CNTRL-HERMES: kanban sync v0 re-proven in the live desktop after relaunch; my-tasks is workspace-scoped | — |
| 2026-09-07 | c0deb07bb1 | CNTRL-HERMES: tb-workers MCP proven inside Hermes after the conductor .mcp.json fix | — |
| 2026-09-07 | c0deb075fa | cntrl: CLAUDE.md fork rules + docs/cntrl/fork-inventory.md (what we carry, merge procedure, open SDK-runtime c | — |
| 2026-09-07 | c0deb07d5f | cntrl: fork-inventory generator + /fork-inventory skill + rule; inventory rebuilt on the PR #65982 head | — |
| 2026-09-07 | c0deb079a8 | cntrl: inventory — session-to-session tools proven inside the SDK runtime (ListAgents lists 12 live sessions) | — |
| 2026-09-07 | c0deb07530 | cntrl: public-repo rule in CLAUDE.md; ignore docs/telemetry | — |
| 2026-09-07 | c0deb07f7d | claude-sdk: live MCP reload — /reload-mcp rotates the SDK session, next turn resumes in a fresh CLI | `agent/claude_sdk_runtime.py`, `gateway/run_turn.py`, `hermes_cli/cli_info_mixin.py`, `tests/agent/test_claude_sdk_runtime.py`, `tests/tools/test_refresh_agent_mcp_tools.py`, `tools/mcp_tool_agent.py`, `tui_gateway/methods_tools.py` |
| 2026-09-07 | c0deb07348 | cntrl: inventory snapshot after live-reload commit | — |
| 2026-09-07 | c0deb075d5 | cntrl: inventory — gateway suite result classified (9 upstream, 7 order flakes, 0 ours) | — |
| 2026-09-07 | c0deb072a4 | claude-sdk: name the spawned session so peers can address it (host-router seam) | `agent/claude_sdk_runtime.py`, `agent/transports/claude_agent_sdk_session.py`, `hermes_cli/config_defaults.py`, `tests/agent/test_claude_sdk_runtime.py`, `website/docs/user-guide/features/claude-agent-sdk-runtime.md` |
| 2026-09-07 | c0deb07307 | cntrl_router: out-of-tree session route registry + host-routing skill | — |
| 2026-09-07 | c0deb07eca | cntrl_groups: session groups as a project layer, out-of-tree | — |
| 2026-09-07 | c0deb07fb3 | cntrl_groups: real Hermes plugin — /groups, hermes groups, opt-in auto-tag by repo | — |
| 2026-09-07 | c0deb07e0c | fix(titles): a truncated model reply no longer becomes the session name | `agent/title_generator.py`, `tests/agent/test_title_generator.py` |
| 2026-09-07 | c0deb07c57 | cntrl: router safety rules + the two traps that bit today | — |

## Core files we touch

| file | commits | listed in §3 |
|---|---|---|
| `agent/claude_sdk_runtime.py` | c0deb07f0f, c0deb07f7d, c0deb072a4 | yes |
| `agent/model_metadata.py` | c0deb079b4 | yes |
| `agent/title_generator.py` | c0deb07e0c | yes |
| `agent/transports/claude_agent_sdk_session.py` | c0deb0750f, c0deb07b3b, c0deb072a4 | yes |
| `agent/transports/hermes_tool_exposure.py` | c0deb07f0f | yes |
| `gateway/run_turn.py` | c0deb07f7d | yes |
| `hermes_cli/cli_info_mixin.py` | c0deb07f7d | yes |
| `hermes_cli/config_defaults.py` | c0deb0750f, c0deb07b3b, c0deb072a4 | yes |
| `hermes_cli/models_catalog_static.py` | c0deb079b4 | yes |
| `pyproject.toml` | c0deb0750f | yes |
| `run_agent.py` | c0deb0751f | yes |
| `tests/agent/test_claude_sdk_runtime.py` | c0deb0750f, c0deb07b3b, c0deb07f0f, c0deb07f7d, c0deb072a4 | yes |
| `tests/agent/test_title_generator.py` | c0deb07e0c | yes |
| `tests/agent/transports/test_hermes_tools_mcp_server.py` | c0deb07f0f | yes |
| `tests/tools/test_refresh_agent_mcp_tools.py` | c0deb07f7d | yes |
| `tools/lazy_deps.py` | c0deb0750f | yes |
| `tools/mcp_tool_agent.py` | c0deb07f7d | yes |
| `tui_gateway/methods_tools.py` | c0deb07f7d | yes |
| `uv.lock` | c0deb0750f | yes |
| `website/docs/user-guide/features/claude-agent-sdk-runtime.md` | c0deb07b3b, c0deb07f0f, c0deb072a4 | yes |
