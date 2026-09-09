# cntrl_groups

A project layer between profiles and sessions.

Out-of-tree: groups live in their own table (`cntrl_session_groups`) inside the
Hermes state db, keyed by session id. Hermes core never reads it and its schema
is never modified, so this survives every upstream merge. Membership is read
back by joining against the `sessions` table.

## Why not a core column

Adding `group` to `sessions` would mean a schema migration in core, a conflict
surface on every upstream merge, and edits to the CLI list, `--resume`, and the
desktop API. A side table plus a small CLI delivers the same thing with zero
merge tax. If upstream ever ships real grouping, this drops out cleanly.

## Install

As a Hermes plugin (gives `/groups` in-session and `hermes groups` on the CLI):

```bash
ln -s "$PWD/cntrl-plugins/cntrl_groups" "$HERMES_HOME/plugins/cntrl_groups"
```

Then enable it in `config.yaml` under `plugins.enabled`. Optional auto-grouping:

```yaml
plugins:
  enabled: [cntrl_groups]
  cntrl_groups:
    auto_group_by_repo: true    # tag each new session by its git repo
    auto_group_prefix: "repo:"  # optional
```

And the skill, so the agent knows the protocol:

```bash
ln -s "$PWD/cntrl-plugins/cntrl_groups/skill" "$HERMES_HOME/skills/cntrl-groups"
```

The `groups.py` CLI keeps working with no plugin context at all.

## Commands

| command | does |
|---|---|
| `ls` | every group with a session count |
| `ls <group> [--limit N]` | sessions in a group, newest first; exit 1 if empty |
| `tag <group> <id>...` | add or move sessions; refuses unknown ids without `--force` |
| `untag <id>...` | remove from whatever group they are in |
| `resume <group>` | print the newest session id, for `hermes --resume` |
| `auto [--min N]` | suggest groups from git repo root; prints commands, never writes |
| `gc` | drop tags whose session was deleted |
| `path` | print the db path |

Same verbs from a session (`/groups ls`, `/groups tag fork <id>`) or the CLI
(`hermes groups ls`).

## Auto-grouping

With `auto_group_by_repo: true` the `on_session_start` hook tags each new
session into a group named after its git repo directory. It never overwrites a
tag you set by hand, skips sessions with no git metadata, and fails open — a
grouping miss must never cost a session start.

Db: `$HERMES_HOME/state.db`, overridable with `CNTRL_GROUPS_DB`.

## Known

Some session titles are junk (`{"title`, ```` ```json ````) because the title
auxiliary lane occasionally returns raw JSON. That is a Hermes title-generation
issue, not a grouping one; the ids are still correct.
