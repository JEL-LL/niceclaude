# Releasing

Publishing is a tag push. Everything else is automated by
`.github/workflows/publish.yml`, which builds, runs the tests, validates the
metadata, installs the built wheel and smoke-tests its entry points *before*
anything reaches an index.

Authentication uses **PyPI Trusted Publishing**: PyPI verifies the workflow's
OIDC identity directly, so there is no API token to create, store, rotate or
leak. The cost is a one-time registration on each index, below.

---

## One-time setup

### 1. GitHub environments

Create two repository environments (Settings → Environments):

| Name | Purpose |
|---|---|
| `testpypi` | rehearsal publishes |
| `pypi` | real releases |

On `pypi`, consider adding yourself as a **required reviewer**. That turns every
release into an explicit approval click, which is worth the two seconds — a tag
push is easy to do by accident, and a PyPI release cannot be deleted.

### 2. Register the trusted publisher on TestPyPI

At <https://test.pypi.org/manage/account/publishing/>:

| Field | Value |
|---|---|
| PyPI Project Name | `niceclaude` |
| Owner | `JEL-LL` |
| Repository name | `niceclaude` |
| Workflow name | `publish.yml` |
| Environment name | `testpypi` |

### 3. Register the trusted publisher on PyPI

Same at <https://pypi.org/manage/account/publishing/>, but with environment
`pypi`.

Both are "pending publishers" until the first upload creates the project — you
do **not** need to reserve the name first.

---

## Rehearse on TestPyPI first

Actions → **publish** → *Run workflow* → target `testpypi`.

This exercises the full pipeline against a throwaway index. Then confirm the
artifact is actually usable, which is the part a build log cannot tell you:

```bash
uv tool install --index https://test.pypi.org/simple/ \
  --index-strategy unsafe-best-match "niceclaude[plot]"
niceclaude install
niceclaude --help
```

The `--index-strategy` flag is needed because matplotlib and its dependencies
live on real PyPI, not TestPyPI.

Check the rendered page at <https://test.pypi.org/project/niceclaude/> — README
formatting, the description, and the license all show up there exactly as they
will on the real index.

---

## Release

```bash
# 1. bump the version in pyproject.toml and describe the release in
#    CHANGELOG.md, in the same commit
# 2. tag it — the workflow refuses to publish if these disagree
git tag v0.1.0
git push --tags
```

The build job fails loudly on a tag/version mismatch rather than shipping a
release nobody can reproduce from source.

---

## Before the first release

Two gates, recorded in `harness/open-questions.md`:

1. **Windows handoff check 4** (`harness/windows-handoff.md`) — does Claude Code
   invoke hooks synchronously and block on them on Windows? That blocking *is*
   the freeze mechanism. If it does not hold, the tool does not degrade on
   Windows, it **silently does nothing** while reporting itself as paced. Do not
   publish before this is answered.
2. **One real paced run** (`open-questions.md` §8) — every brake observed so far
   was forced with an artificial policy (`m0=0, m1=99`). Whether the defaults
   `m0=5, m1=8` produce useful work or useless stalling is genuinely untested.
   Not strictly blocking for an `0.1.x`, but the README should stay honest about
   it until it is done.

---

## Versioning

`0.x` while the pace-line defaults are unproven in practice. The mechanism is
well tested; the *policy* is not, and the version should say so.

A PyPI release can be yanked but never deleted, so the number is permanent even
if the artifact is withdrawn.
