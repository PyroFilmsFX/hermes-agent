# cntrl_router

The registry a host session reads to route work to the session that owns it.

Out-of-tree: nothing here is imported by Hermes core, so it carries no merge
tax. It is a JSON file, a small CLI, and a Hermes skill.

## Why

Hermes sessions are now named (`agent.claude_agent_sdk.session_name`, default
`hermes:{title}`), so Claude Code's `ListAgents` / `SendMessage` can address
them. Naming makes sessions *reachable*; this makes them *routable* — it records
which session owns which piece of work.

## Required setting

Routing INTO a Hermes session needs one config key:

```yaml
agent:
  claude_agent_sdk:
    deliver_background_results: true
```

Upstream defaults it to `false`, which DROPS a peer message with a WARN
("dropped unsolicited ResultMessage ... no turn in flight"). The send still
reports success, so the message vanishes silently — the worst failure shape for
a router. Proven 2026-09-08: with the flag on, a peer `SendMessage` arrives as
"delivering unsolicited result burst"; with it off, nothing reaches the chat.

Set it in every profile you route to, not just the root: each profile has its
own `config.yaml`.

## Install

Symlink the skill into the Hermes skills directory:

```bash
ln -s "$PWD/cntrl-plugins/cntrl_router/skill" "$HERMES_HOME/skills/cntrl-router"
```

Then register your sessions:

```bash
python3 cntrl-plugins/cntrl_router/router.py add cntrl-core \
  --session "TB: cntrl core" --owns "cntrl app, server, bizops"
python3 cntrl-plugins/cntrl_router/router.py add hermes-fork \
  --session "hermes-cntrl-f2" --owns "the Hermes fork, upstream syncs"
```

## Commands

| command | does |
|---|---|
| `list` | print every route (`--json` for machine use) |
| `add <key> --session <name> --owns <text>` | add or replace a route |
| `remove <key>` | drop a route |
| `find <text...>` | match free text, exit 1 on no match |
| `path` | print the registry path |

## Registry

`<hermes install root>/cntrl-routes.json`, overridable with `CNTRL_ROUTER_ROUTES`.

The registry belongs to the INSTALL, not the profile. Each profile is its own
`HERMES_HOME` (`<root>/profiles/<name>`), so keying off it would give every
profile a private empty registry while routes added at the root stayed
invisible — the host would then report "no routes" and silently route nothing.
`routes_root()` walks up out of `profiles/<name>`.
Writes are atomic; a corrupt file fails loudly rather than routing nowhere.
