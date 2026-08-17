# Deploying the poller

`niceclaude watch` is the daemon: it polls `claude -p /usage`, appends every
sample to `usage.jsonl`, and publishes `state.json` for the hook to read. The
hook does the pacing arithmetic itself, so **no snapshot means no pacing** —
keeping this process alive is the whole job of everything in this directory.

Install the tool first — it is not on PyPI yet, so from git:

```bash
uv tool install "niceclaude[plot] @ git+https://github.com/JEL-LL/niceclaude"
niceclaude install
```

These files assume the executables are in `~/.local/bin`
(`%USERPROFILE%\.local\bin` on Windows).

| File | Use it when |
| --- | --- |
| `niceclaude.service` | Linux box with systemd and a user account — laptop, workstation, always-on server. Runs as *you*, not root. |
| `docker-entrypoint.sh` | The agent runs in a container; the poller has to live and die with that container. |
| `niceclaude-task.ps1` | Windows, started at logon. (Windows is verified end to end — see `harness/windows-results.md`.) |

Only one daemon per data directory: `watch` writes `<data>/daemon.pid` and
refuses to start if a live PID is already recorded there. Stop it with
`niceclaude stop`, never by matching on the process name — any shell whose
command line merely mentions `niceclaude watch` matches that pattern too.

## Paths

| | POSIX | Windows |
| --- | --- | --- |
| data (`usage.jsonl`, `state.json`, `policy.json`, `daemon.pid`) | `~/.local/share/niceclaude` | `%LOCALAPPDATA%\niceclaude` |
| settings fragment (`settings.json`) | `~/.config/niceclaude` | `%APPDATA%\niceclaude` |

`NICECLAUDE_DIR` overrides the **data** directory only; the settings fragment
does not move with it. Both the daemon and the hook must see the same data
directory, so if you set `NICECLAUDE_DIR` set it for both.

## Containers: bind-mount the data directory

```
docker run \
  -v "$HOME/.local/share/niceclaude:/root/.local/share/niceclaude" \
  -v "$HOME/.config/niceclaude:/root/.config/niceclaude:ro" \
  ...
```

Without that first mount the data directory is part of the image layer and is
destroyed on every rebuild or `docker rm`, taking with it:

- **`usage.jsonl`** — the entire burn-rate record. `niceclaude check` re-parses
  it from the stored raw output, so losing it also loses the ability to
  re-validate a parser fix against past samples. It never comes back.
- **`policy.json`** — every folder rule you ever set with `on` / `off`, plus the
  global switch. A missing policy file silently means "no folder is paced": the
  container comes up looking healthy and paces nothing.

`state.json` is cheap to lose (the next poll rewrites it); those two are not.

The entrypoint deliberately does **not** `exec` the container command: an
`exec`ed process replaces the shell, and nothing would then be left to forward
`docker stop`'s SIGTERM to the poller. It supervises instead, forwards
TERM/INT/HUP to both children, and exits with the app's own status.

If the daemon reports `daemon already running` right after a container start,
`<data>/daemon.pid` is stale — the recorded PID belongs to a dead container but
collides with a live PID in the new namespace. `docker-entrypoint.sh` clears it
when it is running as PID 1; otherwise delete the file by hand. Nothing removes
it on SIGTERM.

## Verifying the daemon is alive

```
niceclaude status .
```

Look at `snapshot age`. It should sit under the poll interval (60s by default)
and reset on every poll. **An age that keeps growing means the daemon is dead.**
The hook stops trusting a snapshot older than 180s, so it starts refreshing
synchronously on the tool-call hot path (with backoff) and, while it is flying
blind, brakes conservatively — a stale reading can justify braking but never
allowing. A dead daemon therefore does not disable pacing; it makes background
agents crawl. `no snapshot yet -- is 'niceclaude watch' running?` means it never
ran at all, or ran against a different data directory.

Also useful:

- `niceclaude list` — every configured folder and the global switch.
- `niceclaude check` — misparse assertions over the whole history.
- `journalctl --user -u niceclaude -f` / `docker logs -f <ctr>` — the daemon
  prints poll failures to stderr.
