---
name: cntrl-groups
description: Use when sessions need to be organised into projects — grouping sessions, finding or resuming past work on a project, or asking what sessions belong to something. Triggers - session group, project, which sessions, resume the fork work, group these sessions, my sessions for.
---

# cntrl-groups

Put sessions into named projects, then find and resume them by project.

Hermes has profiles and sessions, nothing in between. A busy profile is a flat
list of hundreds of sessions. A group is a name you choose.

Groups live in their own table in the Hermes state db. Hermes core never reads
it, so upstream merges cannot break it.

## Use

In a session, use the slash command:

```
/groups                     # every group with a count
/groups ls hermes-fork      # sessions in one
/groups tag hermes-fork <id>
/groups resume hermes-fork
```

Outside a session, the same verbs:

```bash
G="python3 cntrl-plugins/cntrl_groups/groups.py"   # or: hermes groups

$G ls                       # every group with a count
$G ls hermes-fork           # sessions in one, newest first
$G tag hermes-fork <id>...  # put sessions in a group (re-tagging moves them)
$G untag <id>...            # take them out
$G resume hermes-fork       # print the newest id
$G auto                     # suggest groups from git repo root
```

Resume the newest session of a project:

```bash
hermes --resume "$(python3 cntrl-plugins/cntrl_groups/groups.py resume hermes-fork)"
```

## Rules

- **Tagging an unknown id is refused.** A typo would sit in the group forever
  and never appear in a listing. `--force` is the escape hatch.
- **A session belongs to one group.** Tagging again moves it.
- **`auto` never writes.** It prints the commands and you choose.
- **An empty group exits 1.** Do not report a project as empty without saying
  which name you looked for; the name may just be wrong.

## Finding ids

`$G ls <group>` prints them. For untagged sessions use `hermes sessions`, or
query the db directly:

```bash
sqlite3 "$HERMES_HOME/state.db" \
  "SELECT id, title FROM sessions ORDER BY last_activity_at DESC LIMIT 20"
```
