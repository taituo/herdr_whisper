# whisper

Observable agent-to-agent messages for [Herdr](https://herdr.dev).

Agents in Herdr talk by typing into each other's panes, and Herdr doesn't keep
track of that traffic. whisper makes each message a logged, tracked object:
who sent what to whom, whether a reply was expected, and whether one came back.

```
13:56:01 560f91 ? replied    planner@w1:p2 → reviewer@w1:p3  what is the answer?
  13:56:03 445d2d ↩ done       reviewer@w1:p3 → planner@w1:p2  done, 42
13:56:07 5bcf0c ? unanswered planner@w1:p2 → tester@w1:p4    please answer
13:56:12 ca9758 ? blocked    planner@w1:p2 → deployer@w1:p6  do the risky thing
```

Tested against Herdr 0.9.4 (`tests/e2e_herdr.py` runs a real headless server
with fake agents).

## Install

```bash
herdr plugin link /path/to/herdr-whisper        # or: herdr plugin install <owner>/<repo>
herdr plugin action invoke whisper.install-shim # puts `whisper` in ~/.local/bin
```

Then teach your agents to use it. Either install the skill in
`skills/whisper/SKILL.md` for your agents, or paste its body into your
`AGENTS.md` / `CLAUDE.md`.

Requirements: Python 3.9+ (stdlib only), Linux or macOS.

Optional keybinding for the live feed (`~/.config/herdr/config.toml`):

```toml
[[keys.command]]
key = "prefix+w"
type = "plugin_action"
command = "whisper.feed"
description = "whisper feed"
```

## Use

Agents:

```bash
whisper send reviewer "Is the migration safe to run twice?" --expect-reply
whisper send reviewer "..." --expect-reply --wait   # block and print the reply
whisper reply a1b2c3 "Yes, it's idempotent."        # answer a message
whisper inbox                                       # what is waiting for my reply
whisper ack a1b2c3 | whisper reject a1b2c3 "why"    # judge a reply you received
```

Humans:

```bash
whisper feed               # live view; also the plugin pane `whisper.feed`
whisper status [--all]     # open threads (or everything)
whisper show a1b2c3        # one message with its full history
whisper log [-f] [--raw]   # history, follow, or raw JSONL
whisper where              # log and config paths
```

You can also `whisper send` from a shell outside Herdr. Replies to you are
kept in the log, since there is no pane to deliver them to.

## How it works

**Sending.** `whisper send` resolves the target through `herdr agent get`,
logs the message, and delivers it with `herdr agent prompt`. Herdr's own checks
still apply, so a blocked recipient fails with `agent_blocked`. The recipient
sees a header carrying the message token and how to answer:

```
[whisper msg:a1b2c3 from planner@w1:p1 · reply expected: whisper reply a1b2c3 "<answer>"] Is the migration safe...
```

**Tracking.** No daemon. The plugin hooks `pane.agent_status_changed`,
`pane.closed`, `pane.exited` and `pane.moved`, so Herdr spawns a short
`whisper hook` process per event. The hook moves messages through:

```
queued → delivered → working → done                (no reply expected)
                             → replied              (reply logged, or re:<id> seen in sender's pane)
                             → nudged → … → unanswered   (active mode)
                             → unanswered           (observe mode)
         delivered → stalled (never started)   working → blocked (approval UI)
         any open state → orphaned (recipient pane closed) · failed (delivery refused)
         replied → acked | rejected (sender's judgment)
```

A reply only counts once it has actually been delivered to the sender. A
reply that can't reach the sender alerts you.

Time-based checks (never started, still working after 30 min, a busy turn
that swallowed the message) run on every hook, every CLI call, every 5 s
while the feed pane is open, and at server startup.

**State.** One append-only `log.jsonl` per Herdr server, one line per message
and per state change, written under an flock and fsynced. `snapshot.json` is
only a cache: delete it and it rebuilds from the log. Torn writes from a crash
are skipped. The log lives at
`~/.local/state/herdr/plugins/whisper/sessions/<socket-hash>/`.

**Mechanical vs judgment.** Delivery, state tracking and "no reply arrived"
are plain code. The one judgment call, whether a reply actually answered the
question, is left to the sender agent (`ack` / `reject`). In active mode the
reply header asks for that judgment. whisper never calls an LLM itself.

## Config

`~/.config/herdr/plugins/config/whisper/config.toml` (`herdr plugin config-dir whisper`):

```toml
mode = "observe"       # observe: track, log, notify. active: also nudge silent recipients
                       # and ask senders to ack/reject replies
notify = true          # herdr notification show on failed / blocked / unanswered / stalled
max_nudges = 1         # active mode
settle_grace_s = 20    # recipient was busy at send: how long to wait for a separate turn
start_timeout_s = 120  # delivered but never started working → stalled
work_timeout_s = 1800  # long-running notice while a reply is outstanding
raw_fallback = false   # see below
scan_lines = 400       # sender pane lines scanned for a raw `re:<id>` reply
```

## Caveats

- **Herdr only prompts agents it recognises.** `herdr agent prompt` returns
  `agent_not_ready` for agents Herdr can't classify as a known kind.
  `raw_fallback = true` delivers those with `herdr pane run` instead, but only
  after checking the recipient isn't blocked. It never types into a pane with
  no agent (`agent_not_found`).
- **Sender identity is claimed, not proven.** It comes from `HERDR_PANE_ID`,
  which any process can set. The feed and `show` say so. Recipients should
  treat message bodies as untrusted input (the skill tells them to).
- **Raw traffic is invisible.** Messages sent with plain `herdr agent prompt`
  bypass whisper. Herdr doesn't expose input events to plugins, so they can't
  be inferred reliably. A raw reply that includes `re:<id>` is still detected.
- **"Busy at send" is a judgment call.** If the recipient was already working,
  whisper waits for it to go working → idle again. If the agent folded the
  message into its current turn, whisper decides after `settle_grace_s`.
- Pane ids change on cross-workspace moves, so whisper also records
  `terminal_id` and remaps on `pane.moved`.

## Tests

```bash
python3 -m unittest discover -s tests -p 'test_*.py'   # tracker logic, no Herdr needed
python3 tests/e2e_herdr.py /path/to/herdr              # real server, fake agents, ~1 min
```

The end-to-end run covers: reply via `whisper reply`, raw `re:` reply,
nudge → unanswered, fire-and-forget, blocked recipient (tracked, and
`agent_blocked` still enforced), non-agent pane refused, pane closed →
orphaned, reject, `--wait`, human sender, busy recipient, the feed pane, log
rebuild and hook exit codes.
