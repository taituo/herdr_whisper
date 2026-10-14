# herdr_whisper

A [Herdr](https://herdr.dev) plugin. When one agent prompts another and
doesn't wait for the answer, whisper makes sure the answer gets back.

## The problem

Agent A runs `herdr agent prompt B "question"` and moves on. B answers in its
own pane. A never sees the answer.

## What whisper does

1. **Logs prompts.** A small `herdr` shim sits in front of the real `herdr`.
   When an agent runs `herdr agent prompt`, the shim writes down who sent what
   to whom, then runs the real command unchanged. Agents don't need to know
   whisper exists.

2. **Checks when B finishes.** Herdr tells the plugin each time an agent
   changes state. When B goes idle, whisper checks:
   - Did A use `--wait`? Then A already has the answer. Done.
   - Did B prompt A back? Then they are already talking. Done.
   - Is A a plain shell, not an agent? Nothing to do. Done.

3. **Asks an LLM otherwise.** The LLM reads A's message and B's recent output
   and picks one action:

   | Action | What happens |
   |---|---|
   | `close` | The conversation is finished. Nothing is sent. |
   | `forward` | B's answer is sent to A. |
   | `resend` | B never handled the message. It is sent to B again. |
   | `nudge` | B stopped halfway. B is asked to continue. |
   | `human` | You get a Herdr notification. |

Everything whisper sends starts with `[whisper]` and goes through the real
`herdr agent prompt`, so Herdr's normal checks still apply.

Limits stop endless loops: at most 2 resends or nudges per message, and at
most 12 forwards per hour between the same two agents. After that, you get a
notification instead.

You also get a notification when B is waiting on an approval, B's pane closes,
B never starts on the message, or the LLM can't be reached.

## Install

```bash
herdr plugin install taituo/herdr_whisper
herdr plugin action invoke whisper.install
```

The second command:
- writes the shim to `~/.local/share/herdr-whisper/bin/herdr`
- writes a `whisper` command to `~/.local/bin/whisper`
- adds these lines to `~/.bashrc` (and `~/.zshrc` if you have one):

```sh
# >>> herdr-whisper >>>
[ "${HERDR_ENV:-}" = 1 ] && case ":$PATH:" in *":$HOME/.local/share/herdr-whisper/bin:"*) ;; *) PATH="$HOME/.local/share/herdr-whisper/bin:$PATH" ;; esac
# <<< herdr-whisper <<<
```

They put the shim first on `PATH`, but only inside Herdr panes (`HERDR_ENV=1`). Open new panes, or restart
agents in old ones, to pick it up.

To add the line yourself instead, run `whisper install` (no `--rc`).
To remove everything: `whisper uninstall`.

You need Python 3.9+, Linux or macOS, and an LLM command line tool. The
default is Claude Code (`claude -p --model haiku`).

## Commands

```bash
whisper status          # open conversations, and anything that needs you
whisper status --all    # everything
whisper feed            # live view (also opens as a Herdr pane: whisper.feed)
whisper show <id>       # one message, its history, and the LLM's decision
whisper log -f          # follow new events
whisper judge <id>      # ask the LLM about a message without acting
whisper where           # file locations and current settings
```

Example `whisper status --all`:

```
14:09:37 73a3be   forwarded  planner@w1:p2 → tester@w1:p5    run the auth tests
14:09:34 9c2e05   replied    planner@w1:p2 → reviewer@w1:p4  review the diff
14:09:29 a3e91c w returned   planner@w1:p2 → coder@w1:p3     fix the build
```

`w` means the sender used `--wait`.

## Settings

File: `~/.config/herdr/plugins/config/whisper/config.toml`

```toml
push = true             # false: decide and notify, but never send anything
notify = true
judge_command = ["claude", "-p", "--model", "haiku"]
judge_timeout_s = 120
max_pushes = 2          # resends + nudges per message
max_forwards_per_pair_per_hour = 12
settle_grace_s = 20
start_timeout_s = 120   # notify if B hasn't started after this
work_timeout_s = 1800   # notify once if B is still working after this
read_lines = 150        # how much of B's output the LLM sees
```

Start with `push = false` if you want to see the decisions before whisper
sends anything.

Any LLM tool works if it reads the prompt from stdin and prints the answer,
for example `["codex", "exec", "-"]` or `["ollama", "run", "llama3.1"]`.

## Where data is kept

`~/.local/state/herdr/plugins/whisper/sessions/<id>/log.jsonl`

One line per event, append only. `snapshot.json` next to it is a cache and
can be deleted.

## Limits

- Only `herdr agent prompt` run from an agent pane is seen. Remote
  (`--machine`) calls, `pane send-text`, and calls to the real binary by full
  path are not tracked.
- The sender is whatever `HERDR_PANE_ID` says. It is not verified.
- The LLM reads terminal output, which could contain text meant to trick it.
  Whisper only accepts the five actions, labels what it sends, and keeps the
  limits above.
- If B was already busy when the message arrived, whisper waits up to
  `settle_grace_s` for B to start a new turn before deciding.

## Tests

```bash
python3 -m unittest discover -s tests -p 'test_*.py'   # no Herdr needed
python3 tests/e2e_herdr.py /path/to/herdr              # real Herdr, fake agents, ~1 min
```
