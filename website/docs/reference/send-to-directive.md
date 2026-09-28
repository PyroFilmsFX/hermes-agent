# Send-to blocks

A session can hand the owner a line to deliver to another session, such as an approval the
manager drafted for a worker. The session writes a `:::send-to` block. The desktop app shows the
body as a quote with a **Send to &lt;session&gt;** button. Nothing is sent until the owner clicks
that button and confirms.

## Syntax

```text
:::send-to{session="cntrl-core-worker"}
approve: merge w6/wd-ci-hold
:::
```

- The opening line is exactly `:::send-to{session="<target>"}`, alone on its line. Single quotes
  work too. The closing line is `:::` alone on its line.
- `<target>` is a session title or a session id prefix of 8 or more characters, matched the way
  `/to` matches targets (exact title first, then case-insensitive). The `hermes:` prefix is
  optional: `session="hermes:cntrl-core-worker"` is the same target.
- The body is sent exactly as written, trimmed at both ends. It is not rendered as markdown first,
  so write the literal text the other session should receive.
- One block names one target. Write one block per target. A message can hold several blocks.
- Put the block at the start of a line, outside any code fence. A block inside a code fence is
  shown as code, which is how you show the syntax without making a button.
- Don't nest blocks. The first `:::` line (outside a code fence in the body) closes the block. To
  put a literal `:::` line in the body, open with more colons: `::::send-to{…}` … `::::`.

## What the owner sees

The button is live only in the session's own replies and in peer or background results shown in
the chat. The owner's own messages show the text with no button.

The button stays disabled, with the reason next to it, when:

| Reason shown | Cause |
| --- | --- |
| No session named X | No chat matches the target. |
| N sessions match X | The title is ambiguous. Use a unique title or an id prefix. |
| This block names no session | The `session` attribute is missing or empty. |
| This block has no closing ::: line | The closing line is missing. |
| Waiting for the message to finish | The reply is still streaming. |
| Owner forwarding is off / unavailable | Turn on **Let conductor verify owner decisions** in Settings → Gateways. |

Clicking the button opens the same Forward sheet as **Forward to…**, prefilled with the body and
the target. The sheet shows the exact text and target and keeps the expiry picker. **Send…** masks
secrets in the text, then opens the system confirm, which signs the masked text, like every owner
forward. After a successful send the block shows **Sent ✓** for that message for the rest of the
app session.

The click is the only way to send. An agent-written block never sends by itself, and a
script-dispatched click can't open the sheet. See [Owner grants](./owner-grants.md) for what the
signed forward proves.

## Other surfaces

Surfaces without the desktop renderer, such as plain-text views and messaging platforms, show the
block as its raw text. The body stays readable, and the owner can copy it or use `/to`.
