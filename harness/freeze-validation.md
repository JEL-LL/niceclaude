# Multi-hour freeze validation

Settles `open-questions.md` #4: only 120s and 700s freezes had been
proven. This holds a real `claude -p` process against an unreachable
pace line (m0=0, m1=99) for three hours, then releases it and checks the
tool call still completes.

Costs no tokens while asleep.

```
start:  2026-08-14T21:55:20Z
target: 10800s frozen
2026-08-14T22:05:20Z  +600s  still frozen
2026-08-14T22:15:20Z  +1200s  still frozen
2026-08-14T22:25:20Z  +1800s  still frozen
2026-08-14T22:35:20Z  +2400s  still frozen
2026-08-14T22:45:20Z  +3000s  still frozen
2026-08-14T22:55:20Z  +3600s  still frozen
2026-08-14T23:05:20Z  +4200s  still frozen
2026-08-14T23:15:20Z  +4800s  still frozen
2026-08-14T23:25:20Z  +5400s  still frozen
2026-08-14T23:35:20Z  +6000s  still frozen
2026-08-14T23:45:20Z  +6600s  still frozen
2026-08-14T23:55:20Z  +7200s  still frozen
2026-08-15T00:05:20Z  +7800s  still frozen
2026-08-15T00:15:20Z  +8400s  still frozen
2026-08-15T00:25:21Z  +9000s  still frozen
2026-08-15T00:35:21Z  +9600s  still frozen
2026-08-15T00:45:21Z  +10200s  still frozen
2026-08-15T00:55:21Z  +10800s  still frozen
releasing: 2026-08-15T00:55:21Z
claude rc: 0
output:    FREEZE-SURVIVED
end:       2026-08-15T00:55:37Z
--- hook.log ---
2026-08-14T21:55:23Z brake  cwd=/tmp/nc-freeze session 21.0% over line 0.4%; week:all models 17.0% over line 0.8%
2026-08-14T21:57:17Z brake  cwd=/tmp/nc-globaltest session 21.0% over line 0.4%; week:all models 17.0% over line 0.8%
2026-08-14T22:03:22Z brake  cwd=/tmp/nc-fan [SubagentStart] session 25.0% over line 22.9%
2026-08-15T00:55:35Z release cwd=/tmp/nc-freeze after 10811s (unpaced)
```
