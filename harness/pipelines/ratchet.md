# Pipeline: `ratchet`

**Shape:** implement one phase with a sub-agent → adversarially review it with *fresh*
sub-agents until a review comes back quiet → final gate with a **second model** → commit,
rebase, push → next phase.

**Why "ratchet":** every phase is committed before the next one starts, so the work can
only move forward. A later phase going wrong never endangers an earlier one, and the
operator can stop cleanly at any phase boundary.

**When to reach for it:** a multi-phase change where the cost of a defect is high —
firmware that will ride a bitstream into hardware, a migration, anything touching a
device in the field. It is deliberately expensive. For a one-file fix it is overkill;
just make the change.

**When NOT to use it:** exploratory work where the design is still moving. The pipeline
assumes the *what* is settled and only the *how* is in question. Review rounds against a
moving target burn agents on churn.

---

## Operator inputs

Before starting, the operator (the orchestrating session) must have:

| Input | Notes |
|---|---|
| **The spec** | A file, or a specific row/section of one. Name the exact path *and how to read it* — a giant single-line markdown table row needs `awk 'NR==N' file \| tr '\|' '\n' \| fold -s -w 160`, and an agent that does not know that will read nothing. |
| **The guardrails** | The workstream's `AGENTS.md` plus any repo-wide rules. |
| **The phase list, in order** | Derive it if the spec has no numbered phases, and *get the split confirmed by the user before spawning anything*. |
| **The stop point** | Which phases are in scope and which are the user's (hardware, deploys, anything needing a bench). Say so explicitly, and forbid agents from simulating them. |
| **The verification recipe** | The exact command(s), where they run, and the **pre-change baseline** (see Failure Mode 4). |

---

## The loop — run once per phase

### 1. Implement
Spawn **one** implementor. Keep its agent id — you will send fixes back to it.

Its task: implement the phase per the spec, *including* tests and the workstream's
paperwork (mark the phase row, write/extend the progress log, update the feature list if
warranted). Require it to report **the exact list of files it touched** and its test
results, and to **not commit** — the orchestrator commits.

### 2. Review loop — repeat until a review returns nothing actionable
1. Spawn a **fresh** reviewer each round. Never reuse one: a reviewer that already
   blessed a change is invested in it.
2. Give it the blast radius, the spec, and — from round 2 on — **what previous rounds
   already established**, so it spends its effort on new ground instead of re-deriving
   settled facts. Tell it to spot-check those rather than trust them.
3. Require the output format: *only* concrete actionable defects as `file:line` + why +
   suggested fix, or the exact string `no actionable findings`. Explicitly: no style
   nitpicks, no praise, no restatement of what the change does.
4. If there are defects, send them to the implementor, wait for the fix, and loop.

### 3. Second-model gate
When the loop goes quiet, run one final adversarial review **with a different model**
(e.g. Fable). Give it the same blast radius plus an explicit list of what the previous
model claimed, and tell it **not to take any of that on trust**. A second model
reliably catches things the first one's blind spots let through — in the field it has
found both real defects and wrong *facts asserted as evidence* in the paperwork.

If it finds anything actionable: fix, then re-run it. Repeat until it is quiet.

### 4. Commit
Stage **only** this phase's files, **by explicit path**, one at a time. Then:

```
git diff --cached --stat        # confirm ONLY the intended files, and the expected count
```

If anything unexpected is staged, unstage it before committing. Commit with a message
naming the phase/task id. Then rebase onto the remote and push, if the workstream
requires it.

### 5. Next
Re-check `git status`, then return to step 1 for the next phase.

---

## Hard rules

- **Stage by explicit path. Never `git add -A`, `git add .`, or any broad add.** A
  working tree may carry months of unrelated uncommitted work.
- **Never `reset --hard`, `clean`, `stash`, or `checkout -- .`** — in the orchestrator or
  in any sub-agent prompt. Say so in every prompt; agents tidy by reflex.
- **Sub-agents do not commit.** Only the orchestrator does. This is what keeps the
  staged set reviewable.
- **Respect the repo's attribution rules.** If the project forbids tool-attribution
  trailers, say so in *every* sub-agent prompt — the default tooling instruction injects
  one and agents follow it unless told otherwise.
- **Cap file writes** at whatever the workstream's limit is (~400 lines is common), and
  pass that limit into every prompt.
- **Flag, don't fix, unrelated rot.** Tell agents the sighting/backlog mechanism and the
  next free id. Without the id they invent one and collide.

---

## Stop conditions

- The scoped phases are committed → stop and summarize.
- A review keeps finding **the same class** of issue after ~3 fix rounds → stop and
  summarize rather than looping. See Failure Mode 2 for what to do instead.
- An agent is blocked → stop and surface it. Do not let it work around a blocker.

---

## Failure modes seen in the field

These are the expensive ones. They are why this file exists rather than a bare copy of
the loop.

### 1. The implementor's transcript can vanish
Session task directories rotate. `SendMessage` to the implementor then fails with *"No
transcript found for agent ID"*, and the loop's whole premise — feeding corrections back
to the agent that has the context — collapses.

**Mitigation:** write review findings so they are **self-contained** — `file:line`, the
exact wrong text, the exact replacement, and the evidence. Then a *fresh* fixer can
execute them with no prior context. Do this from round 1; it costs nothing and it is
insurance. If the id dies, spawn a fixer instead of stalling.

### 2. Rounds of one-more-nit in the same defect class
A change that shifts line numbers breaks every hard `file:NNN` anchor below it, and
reviewers find them **one or two at a time**, because each round fixes the anchors it
looked at and misses their neighbours in the same sentence. One ticket produced **twelve**
stale-anchor findings across five rounds — more than every real bug in it combined.

**The mitigation is NOT a better sweep.** The first version of this file said to order an
exhaustive mechanical sweep on the second repeat. That is the wrong lesson, and it was
corrected in the field: *"I don't think we need to keep the history books up to date every
time a line of code is inserted. That sounds very intractable… that feels a bit like navel
gazing which isn't needed."* A `file:NNN` citation is invalidated by any insertion above
it, so its upkeep cost is unbounded and its payoff near zero. Sweeping harder only
industrialises the waste.

**Fix the citation convention, not the citations.** Cite a **symbol** — `send_version`,
`_drain_input`, `TestDetectStreamingGuard` — instead of a line. Symbols survive
insertions, they are greppable, and they say what they mean. Keep line numbers for things
that genuinely do not move, and prefer no link at all to a brittle one.

**Tell reviewers this up front**, in the prompt, or they will dutifully produce the sweep:
*"do not report an anchor merely because its line number moved; do report one whose target
contradicts the claim it supports."*

**What still counts as a defect:** an anchor whose *content* actively misleads — a link
offered as evidence for a claim the cited code contradicts. In the field an
anchor was correctly moved to a line reading `LOAD s5, "E"` under a sentence asserting
`"D"`. Verify both.

### 3. Reviewers re-derive settled work
An unbriefed round-3 reviewer will happily re-verify the assembly from scratch and report
nothing new. Brief each round with what is already established and tell it to spot-check.

### 4. Pre-existing test failures get misattributed
A dirty tree can carry failures that have nothing to do with the change. Without a
baseline, the implementor either "fixes" someone else's problem — entangling the commit
with unrelated work — or reports a red suite as its own failure.

**Mitigation:** capture a **full baseline before the first implementor touches anything**,
give it to every agent as the success criterion (*"these exact failures and no others"*),
and have it recorded in the progress log so the next session does not re-hunt them.

### 5. Verification environment surprises
"Run the test suite" is not a recipe. Establish *empirically* which legs run where before
the phase starts, and pass the exact commands into the prompts. Do not trust a note, your
memory, or an agent's assumption; run it. A suite that runs in one environment and not
another is a fact about the box, discovered in minutes, and mis-assumed at the cost of a
whole phase.

### 6. Running the second-model gate too early
It is tempting to launch the second-model gate alongside a same-model review round, since
both examine the same tree. **Don't.** The same-model loop exists precisely to flush out
the cheap findings *before* the expensive model spends its attention on them, and a gate
run against a tree that is about to change is stale the moment the fixes land.

Tried in the field and it backfired: the parallel round found 5 defects, the gate found 2
(one of them a duplicate of the round's), and the gate had to be **re-run from scratch**
on the settled tree afterwards — so parallelising added a gate invocation instead of
saving one.

The gate runs **once the same-model loop has gone quiet**, on a tree nothing is pending
against. Within a single round, independent reviewers on *different dimensions* can still
run concurrently — that is a different thing from racing the gate.

### 7. The orchestrator over-delegating
When a round is down to a single verifiable string replacement, the orchestrator
verifying and doing it directly is faster and lower-risk than another agent round-trip.
Delegation is the default, not a vow.

### 8. Ending a phase with a question
Do not close a phase by asking *"shall I start the next one?"* when the next phase is
already in scope and green-lit. The user may be away; the pipeline stalls for no reason.
Proceed, and report at the real stop condition.

---

### 9. Wrong *rationales* outnumber wrong code, and reviews do not catch them

The expensive defect class in a densely commented codebase is not broken code.
It is prose — comments, docstrings, plan text — asserting behaviour the code
does not have. Measured on the `expired-window-plan.md` run: of ~47 findings
across three rounds and a second-model gate, about **7 were wrong code and
about 16 were wrong rationales**. Five of those were in the plan file itself,
written confidently and retracted later. One was perfectly inverted — a
docstring said the hook "refuses to brake on" a void reading when braking on
void data was the whole doctrine — and three same-model rounds read past it.

They survive review because a reviewer reads a plausible sentence next to the
code it describes and agrees. Nothing forces the sentence to be executed.

**It is worse in a docs-only phase, not better.** The same change's
documentation phase — no code, pure prose — drew another 32 findings: a
review round of 27 and a gate of 5. Six of the 27 were outright false claims
in freshly written docs, including two numbers asserted rather than computed.
The gate then found one that no amount of careful reading would catch: the
changelog described the shipped bug using a property of a **rejected**
alternative, lifted from the plan section that rejected it. Writing about
code is not a lower-risk activity than writing code.

**The mitigation that worked: require every comment or doc claim that is
written or rewritten to be DEMONSTRATED, with the one-line result quoted in
the agent's report.** Put it in the implementor prompt, not just the reviewer
prompt. On that run it caught four false claims, three of them written by the
agent applying the fix, in the same message as the fix. A claim that cannot be
cheaply demonstrated should be written weaker or omitted.

Two corollaries, both earned the hard way on the same run:

- **Numbers are the worst offenders, because they look checked.** The same
  mistake recurred through every phase of that work and the one after it:
  figures computed under some non-default configuration -- one user's
  margins, a walkthrough's lowered knob -- then stated as general facts. One propagated out of the plan into a
  committed code comment and from there into a design-decisions section before
  anyone recomputed it; another survived three review rounds and a gate. The
  tell is a bare value in prose that nobody re-derived at the shipped
  defaults. Where the number is not the point, write the
  expression (`100 - m1`) rather than the value.
- **Re-measure before briefing anyone, including an expert.** A second-model
  adjudication was once dispatched with "measured on the current tree" figures
  taken from a tree that had since been edited. The model's first finding was
  that the brief described a tree that no longer existed.

## Sub-agent prompt templates

Fill the `<…>` and keep every guardrail line — they are load-bearing.

### Implementor
> Implement phase `<N>` of `<spec path>` (read it and `<AGENTS.md path>` first;
> `<how to read the spec, verbatim>`). Write the code, its tests, and update the phase row
> plus a progress note. Scope is `<items>` — do **not** touch `<other phases' files>`.
> Verification: `<exact commands and where they run>`. The pre-change baseline is
> `<baseline>`; any *additional* failure is yours. Follow all guardrails — especially:
> `<the workstream's specific invariants>`; ≤`<N>`-line file writes; **no attribution
> trailers**; **do not commit or stage**; the working tree carries unrelated uncommitted
> work, so never `git add -A`, `reset --hard`, `clean`, `stash`, or `checkout -- .`.
> Notice unrelated rot? File it as `<sighting mechanism>` (next free id: `<id>`), do not
> fix it. Report the **exact list of files you touched** and your test results per leg.

### Reviewer / second-model gate
> Adversarially review the **uncommitted** changes implementing phase `<N>` against
> `<spec path>` and its guardrails. Blast radius: `<paths>`; note `<new file>` is untracked
> and will not appear in `git diff`. **Ignore everything else in the working tree** — it
> carries unrelated months-old WIP. Hunt for: `<the specific invariants that matter>`,
> correctness bugs, weak or missing tests, and deviations from the spec. `<For round ≥2:>`
> Previous rounds established `<facts>` — spot-check rather than re-deriving, and spend
> your effort on new ground. `<For the second-model gate:>` A different model reported
> `<claims>` — do **not** take any of it on trust. Do not fix anything; do not commit,
> stage, or modify any file. Report **only** concrete actionable defects as `file:line` +
> why + suggested fix, or reply exactly `no actionable findings`. No style nitpicks.
