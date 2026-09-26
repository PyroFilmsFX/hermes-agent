---
title: "Secret Hygiene — Find and mask secrets stored in Hermes history"
sidebar_label: "Secret Hygiene"
description: "Find and mask secrets stored in Hermes history"
---

{/* This page is auto-generated from the skill's SKILL.md by website/scripts/generate-skill-docs.py. Edit the source SKILL.md, not this page. */}

# Secret Hygiene

Find and mask secrets stored in Hermes history.

## Skill metadata

| | |
|---|---|
| Source | Bundled (installed by default) |
| Path | `skills/devops/secret-hygiene` |
| Version | `1.0.0` |
| Author | Justin (@PyroFilmsFX) + Hermes Agent |
| License | MIT |
| Platforms | linux, macos, windows |
| Tags | `security`, `secrets`, `privacy`, `cleanup` |

## Reference: full SKILL.md

:::info
The following is the complete skill definition that Hermes loads when this skill is triggered. This is what the agent sees as instructions when the skill is active.
:::

# Secret Hygiene Skill

Sweep secrets that already reached disk: composer pastes, attachments, transcripts, the gateway
document cache, `state.db` and Hermes-created Claude SDK transcripts. The sweep replaces each secret
with a `[REDACTED:<kind>:<tag>]` placeholder. It does not rotate credentials, and it never deletes a
transcript or log.

## When to Use

- The user pasted a credential (database URL, API key, Fly token) and wants it gone from history.
- The user asks what secrets Hermes has stored, or wants a report before sharing or exporting data.
- A `hermes security scrub` report listed deferred sessions and the user wants to retry them.

Do not use it to reveal a masked value. Placeholders cannot be reversed from the transcript.

## Prerequisites

- `terminal` access to the `hermes` CLI of the profile being cleaned.
- For a full clean the Hermes app and gateways should be closed. Sessions that are still live are
  deferred, not rewritten.

## How to Run

Always start with the dry run through `terminal` and show the user the report:

```bash
hermes security scrub
```

Run `--apply` only after the user explicitly asks for it:

```bash
hermes security scrub --apply
```

## Quick Reference

| Command | Effect |
|---|---|
| `hermes security scrub` | Dry run. Lists file or row, kind, count and action. Writes nothing. |
| `hermes security scrub --json` | The same report as JSON. |
| `hermes security scrub --apply` | Backs up, verifies the backup restores, then masks in place. |
| `hermes security scrub --all-profiles` | Covers the default profile and every named profile. |
| `hermes security scrub --targets pastes,state-db` | Limits the scan to some targets. |
| `hermes security scrub --status` | Shows the last run and the deferred-live ledger. |

Targets: `pastes`, `attachments`, `transcripts`, `state-db`, `doc-cache`, `sdk-transcripts`.

## Procedure

1. Run the dry run and summarise the totals by kind for the user. Never quote a secret value; the
   report does not contain any.
2. Point out `deferred-live` rows (open sessions) and `ambiguous-skip` Claude transcripts (not
   provably Hermes-created, so they are left alone).
3. Ask before applying. Mention that the backup directory it prints holds the original secrets.
4. On request, run `--apply` and relay the backup path and the masked totals.
5. If sessions were deferred, suggest closing the app and running `--apply` again.

## Pitfalls

- The backup under `backups/secret-scrub/` holds the raw secrets. Do not read, copy or upload it.
- A masked secret is gone from history but is still valid. Suggest rotation when the secret left
  the machine (it was sent to a model provider or a chat platform).
- A refusal (exit code 2) means another scrub is running, a search-index rebuild is in progress,
  or `state.db` failed its quick check. Do not force it; report the message.
- Never delete transcripts or logs to "clean" them. Masking in place is the only allowed change.

## Verification

- Re-run `hermes security scrub`: the same targets should report nothing left to mask.
- `hermes security scrub --status` lists what is still deferred.
