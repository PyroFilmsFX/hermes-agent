---
name: cntrl-router
description: Use when a request belongs to another session — routing work to the session that owns a cntrl feature, checking what other sessions are running, or acting as the host that dispatches across sessions. Triggers - route this, who owns this, hand this off, which session, host session, dispatch to.
---

# cntrl-router

Route work to the Claude session that owns it, instead of doing it here.

Every Hermes session carries a peer-addressable name (`hermes:<title>`), so the
Claude Code `ListAgents` and `SendMessage` tools can reach it. This skill is the
map from a piece of work to the session that owns it.

## When to use

- A request is clearly about a feature another session owns.
- You are the host session and are deciding where work goes.
- Someone asks what is running, or who owns a concept.

## Route

1. **Read the registry.**
   ```bash
   python3 cntrl-plugins/cntrl_router/router.py list
   ```
   Or match free text directly. Exit code 1 means no match:
   ```bash
   python3 cntrl-plugins/cntrl_router/router.py find "kanban sync is stuck"
   ```

2. **Check the session is alive.** Call `ListAgents`. Match the route's
   `session` value against a row. A route with no live row is a dead route.

3. **Hand off.** Call `SendMessage` with `to` set to that exact row name.
   Say what you want and what "done" looks like. One message, not a
   conversation.

4. **Report.** Tell the user which session you handed to and why.

## Rules

- **Say what a message IS.** A routed message is an instruction to a session
  that will act on it. Start a drill or a test with `DRILL — do not act:` on
  the first line. Learned the hard way 2026-09-07: a test handoff saying
  "merge main into the fork branch" was delivered, believed, and acted on —
  a session checked out `main` and fast-forwarded it mid-build.
- **Never route a destructive verb blind.** Anything that checks out, merges,
  rebases, resets, deletes, deploys or sends goes back to the user, not to a
  peer. Route investigation and reporting freely; route state changes never.

- **No match, no guess.** If `find` exits 1 or no session is live, say so and
  ask. Sending work to the wrong session loses it silently.
- **Never route a permission.** If an action was denied here, do not ask
  another session to do it. Take it back to the user.
- **Do not route what you can finish.** A one-line answer is faster than a
  hand-off. Route work that needs another session's context or repo.
- **One hop.** The session you hand to owns the work from there. Do not chain.
- **Name the branch and the repo.** A peer session may sit on a different
  branch or a different checkout of the same repo. A handoff that says "the
  fork branch" is ambiguous; say `cntrl-hermes` in `~/…/hermes-cntrl`.

## Register a route

```bash
python3 cntrl-plugins/cntrl_router/router.py add kanban \
  --session "hermes:kanban sync" \
  --owns "cntrl <-> Hermes board sync, cntrl_sync plugin" \
  --repo /Users/justin/Documents/Projects/Business/hermes-cntrl
```

`--session` must match the peer name exactly as `ListAgents` prints it. Set a
session's own name with `agent.claude_agent_sdk.session_name` in Hermes config,
or `claude --name <name>` for a plain Claude Code session.

Registry path: `$HERMES_HOME/cntrl-routes.json`
(`python3 cntrl-plugins/cntrl_router/router.py path` prints it).
