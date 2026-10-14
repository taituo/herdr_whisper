# whisper

A [Herdr](https://herdr.dev) plugin that keeps agent-to-agent conversations
from going silent.

Agents keep using Herdr exactly as the Herdr skill teaches
(`herdr agent prompt reviewer "..."`). Nothing new to learn, no extra skill.
Most of the time whisper does nothing visible. Its job is to catch the
"broken telephone" case: agent A asks agent B something, doesn't wait, B
finishes, and the answer just sits in B's pane where A never sees it.

## How it works

```
agent A ── herdr agent prompt B "..." ──▶ herdr shim ──▶ real herdr ──▶ B
                                            │ logs "A → B"
                                            ▼
                                       log.jsonl
                                            ▲
 B finishes its turn ──▶ pane.agent_status_changed hook ──▶ the loop
```

**1. The shim (silent).** A tiny `herdr` script goes first on `PATH`, but
only in Herdr panes. For `herdr agent prompt` called from an agent pane, it
records who prompted whom and whether the sender used `--wait`. Then it runs
the real herdr with the same arguments and passes its output and exit code
through unchanged. Every other command goes straight to the real herdr. If
whisper is broken or missing, the shim gets out of the way.

**2. The loop (one hook).** Herdr runs `whisper hook` on every agent status
change. When B returns (idle or done) with a message from A still open,
whisper first runs plain-code checks:

| Situation | Result |
|---|---|
| A is not an agent (a human's shell) | `returned`: nothing to push |
| A used `--wait` and got this turn's result | `returned`: A already has it |
| B prompted A back during its turn | `replied`: the conversation is moving |
| otherwise | `judging` |

**3. The judge (semantic).** Only in the last case does an LLM read A's
message and B's recent output, and decide whether the exchange was meant to
end there:

| Judge says | Whisper does |
|---|---|
| `close`: the exchange was meant to end (FYI, done) | nothing (`closed`) |
| `forward`: B has something A needs | prompts A with B's answer (`forwarded`) |
| `resend`: B never handled the message | prompts B with the message again (`resent`), then watches the next turn |
| `nudge`: B stopped halfway or asked to continue | prompts B to continue (`nudged`), then watches the next turn |
| `human`: only you can sort it out | Herdr notification (`escalated`) |

The judge only decides. Whisper does the sending itself, through the real
`herdr agent prompt`, so Herdr's `agent_blocked` check still applies. Every
push is labelled `[whisper]`. Hard limits turn any runaway into a
notification: at most `max_pushes` resends or nudges per message, and at most
`max_forwards_per_pair_per_hour` forwards between two agents.

A forward looks like this in A's pane:

```
[whisper] reviewer@w1:p3 finished the message you sent at 14:02 ("Is migration 0042 safe to run twice?"):
reviewer answered: yes, 0042 is safe to run twice; both statements are idempotent.
(full output: herdr agent read w1:p3 --source recent-unwrapped --lines 200)
```

You also get a notification when B gets blocked on an approval, B's pane
closes mid-conversation, B never picks the message up, a push fails, or the
judge can't decide.

## Install

```bash
herdr plugin link /path/to/herdr-whisper      # or: herdr plugin install <owner>/<repo>
herdr plugin action invoke whisper.install    # shim + `whisper` command + one PATH line in ~/.bashrc / ~/.zshrc
```

Open new panes, or restart agents in existing ones, so they pick up the shim.

The PATH line only applies inside Herdr (`HERDR_ENV=1`):

```sh
# >>> herdr-whisper >>>
[ "${HERDR_ENV:-}" = 1 ] && case ":$PATH:" in *":$HOME/.local/share/herdr-whisper/bin:"*) ;; *) PATH="$HOME/.local/share/herdr-whisper/bin:$PATH" ;; esac
# <<< herdr-whisper <<<
```

If you'd rather add it yourself, run `whisper install` without `--rc`.
`whisper uninstall` removes everything again. For fish, add
`~/.local/share/herdr-whisper/bin` to `fish_user_paths` when `HERDR_ENV` is 1.

You need Python 3.9+ (stdlib only), Linux or macOS, and an LLM CLI for the
judge. By default that's Claude Code (`claude -p --model haiku`).

## Watching

```bash
whisper feed            # live view; also the plugin pane (action whisper.feed)
whisper status [--all]  # open conversations plus anything that needs you
whisper show <id>       # one message: history, judge verdict, pushed text
whisper log [-f|--raw]  # history, follow, or raw JSONL
whisper judge <id>      # dry-run the judge on a message (--prompt prints the prompt)
whisper where           # paths, judge command, push on/off
```

```
14:09:37 73a3be   forwarded  planner@w1:p2 → ignorebot@w1:p5  q-ignored
14:09:34 9c2e05   replied    planner@w1:p2 → callbackbot@w1:p4  please ask back
14:09:29 a3e91c w returned   planner@w1:p2 → answerbot@w1:p3  q-wait
```

Optional keybinding (`~/.config/herdr/config.toml`):

```toml
[[keys.command]]
key = "prefix+w"
type = "plugin_action"
command = "whisper.feed"
description = "whisper feed"
```

## Config

`~/.config/herdr/plugins/config/whisper/config.toml`:

```toml
push = true                                   # false = dry run: judge and notify, never type into an agent
notify = true
judge_command = ["claude", "-p", "--model", "haiku"]   # any command: prompt on stdin, answer on stdout
judge_timeout_s = 120
max_pushes = 2                                # resends + nudges per message, then you get it
max_forwards_per_pair_per_hour = 12           # loop guard
settle_grace_s = 20                           # see "busy recipient" below
start_timeout_s = 120                         # delivered but never started → notification
work_timeout_s = 1800                         # still working → one notification
read_lines = 150                              # lines of B's output the judge sees
```

Other judges work if they read stdin and print the JSON verdict, for example
`["codex", "exec", "-"]`, `["llm", "-m", "gpt-4o-mini"]` or
`["ollama", "run", "llama3.1"]`.

## State

There is one append-only `log.jsonl` per Herdr server, under
`~/.local/state/herdr/plugins/whisper/sessions/<id>/`. It holds one line per
message, state change and verdict, written under a file lock and fsynced.
`snapshot.json` is only a cache: delete it and it rebuilds from the log. There
is no daemon. The checks that depend on time run on every hook, every CLI
call, every 5 s while the feed is open, and at server startup.

## Limits and trade-offs

- **The shim only sees what goes through it.** That covers `herdr agent prompt`
  run from an agent pane through `PATH`, which is what the Herdr skill
  teaches. Calls with an absolute path to the real binary, `--machine`
  (remote) calls, and raw `pane send-text` are passed through untracked.
- **Sender identity is claimed, not proven.** It comes from `HERDR_PANE_ID`.
- **The judge reads terminal output.** That output is untrusted: an agent's
  pane could contain text aimed at the judge. The prompt tells the judge to
  treat it as data. Whisper only accepts the five actions, caps the message
  length, labels every push, and enforces the limits above. Use
  `push = false` if you want to watch verdicts before letting it act.
- **Busy recipient.** If B was already working when the prompt arrived,
  whisper waits for B to go through a working → idle turn of its own. If B
  folded the message into the turn it was already in, whisper judges after
  `settle_grace_s`.
- **`agent prompt` only reaches agents Herdr recognises.** That's Herdr's own
  rule, and whisper doesn't change it.

## Tests

```bash
python3 -m unittest discover -s tests -p 'test_*.py'   # loop logic, no Herdr or LLM needed
python3 tests/e2e_herdr.py /path/to/herdr              # real Herdr server, fake agents, fake judge (~1 min)
```

The end-to-end run uses Herdr 0.9.4. It installs the shim through the plugin
action and checks:

- the shim passes output and errors through byte for byte
- `--wait` → `returned`
- no wait → `forwarded`
- B prompts back → `replied`
- B ignores the message → `resent` → `forwarded`
- FYI → `closed`
- blocked recipient, with `agent_blocked` still enforced
- a shell as sender
- a busy recipient
- a missing judge → `escalated`
- dry run
- pane closed → `orphaned`
- the feed, rebuilding the log, hook exit codes, and uninstall
