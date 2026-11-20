# crew

A Claude Code plugin that runs a proof-first engineering crew. You give it issues or a feature; it returns merge-ready pull requests, each with the test that proves it, a green full suite and a reviewed diff. One rule runs through all of it: nothing is done until a machine proves it.

Community-built on [graphify](https://github.com/Graphify-Labs/graphify). Not affiliated with or endorsed by Graphify Labs.

## Install

Requirements: git, and `python3` 3.9 or later on `PATH`. graphify is optional; without it the crew maps the repository with `rg` instead of a code graph.

**Claude Code CLI or desktop app** (plugin):

```
/plugin install crew --marketplace mkculp/graphify
```

Until this is merged to the default branch, add the branch explicitly:

```
/plugin marketplace add mkculp/graphify#claude/jolly-franklin-j2visf
/plugin install crew@mkculp
```

**Claude Code on the web** (cloud sessions do not load marketplace plugins): install the crew into the project itself and commit it.

```bash
python3 plugins/crew/skills/crew/scripts/install.py --project /path/to/project
```

This writes `.claude/skills/crew/`, `.claude/agents/crew-*.md` and one hook entry in `.claude/settings.json`. Run it again after an update; `--uninstall` removes exactly what it added.

**As an uploaded plugin**: zip the plugin folder and upload the `.plugin` file to your claude.ai account.

```bash
cd plugins/crew && zip -r ../../crew.plugin . -x "tests/*" -x "*/__pycache__/*" -x "*.pyc"
```

## Use

```
/crew:crew fix the issues in ISSUES.md        (plugin install)
/crew fix the issues in ISSUES.md             (project install)
```

| Argument | What happens |
|---|---|
| issues or a feature | Intake, map, plan, wait for your go, then the loop |
| `resume` | Continue a run from `.crew/` after a restart or a context summary |
| `status` | Every issue, its status and its next step; time and tokens so far |
| `audit` | Audit your Claude Code setup for token cost |
| `stop` | Stand the crew down and send the brief |

The Lead asks at most four questions (what done means, what must never be touched, the build and test commands, the budget), shows you the plan, and waits for one go. After that it does not ask permission again: an issue that needs you is parked with one question while the rest continue. It finishes with one message, the brief.

## The crew

| Role | Model | Effort | Job |
|---|---|---|---|
| Lead | your session's | your session's | Plans, assigns, verifies. Writes no feature code. The only one who messages you. |
| Mapper | sonnet | medium | Builds the code graph, writes `.crew/NOTES.md`: commands, module seams, risky files. |
| Breaker | your session's | high | Turns each issue into a failing test before anyone builds. |
| Builder | your session's | medium | One per independent issue. Owns its files and nothing else. |
| Prover | haiku | default | Re-runs every claim on a clean checkout. Its rejection is final. |
| Reviewer | your session's | high | Reads each diff against the spec like a stranger who expects it to be wrong. |

Depth is spent where it changes the outcome: understanding the issue and attacking the diff. Lookups and mechanical checks go to small models. A Builder's effort is raised only after a second rejection, which is a measured worse answer rather than a precaution.

## What the machine checks

`crew` is a single-file CLI (`skills/crew/scripts/crew.py`, standard library only) that every role calls. An issue moves forward only when it exits 0.

| Command | What it proves |
|---|---|
| `crew baseline` | The setup, suite and check commands pass on a clean checkout of the untouched base. If they do not, only an issue registered to fix the baseline may start. |
| `crew seal` | The proving test fails on the base plus the test files alone, and not because of a timeout or an unrunnable command. |
| `crew prove` | On a fresh checkout of the issue's head, made outside the repository so nothing resolves upward into it: the proving test passes and actually ran tests, the full suite and every extra check pass, the sealed test is byte-identical to what was sealed, nothing is uncommitted, no fenced path or file outside the lane was touched, no test was deleted, skipped or removed, and no existing assertion was rewritten outside the sealed files. |
| `crew review` | The approval carries a summary, a risk level, a rollback and a "why this could break", and explains any new dependency. It applies only to the commit that was proven. |
| `crew land` | For single-branch delivery: the issue's commits apply to the delivery branch on a scratch checkout, and every landed proving test and the full suite pass there. The branch moves only if they do. |
| `crew waves` | Which issues are safe to build in parallel, from lane overlap and from `graphify affected`. With a PR per issue, a coupled issue is built on top of the one it is coupled with, so the two PRs do not conflict. |
| `crew pr-body`, `crew brief` | Rendered from the recorded evidence, so the output before and after is quoted, not recalled. |

A PreToolUse hook (`guard.py`) stops rule-breaking early while a run is active: secrets are not read, fenced paths and sealed tests are not written, the Lead writes no project files, builders stay in their lane, and nothing is force-pushed or published. The hook is prevention and is heuristic for shell commands; `crew prove` reads the real diff and is the authority. The hook does nothing in a project with no active run.

Only the Breaker may change what a test expects. When an issue changes behaviour that an existing test pins, the Breaker rewrites that test before sealing, the proof flags every rewritten assertion, and the Reviewer must tie each one to the issue's own words. A Builder who edits an existing assertion to get green is rejected.

Three things are left to judgement, and each has a role assigned to it: whether the proving test fails for the issue's own reason (Breaker, checked by the Prover), whether the fix is right rather than merely green (Reviewer), and whether the spec says what you meant (you, at the go gate).

## What it writes

Everything lives in `.crew/`, hidden from git through `.git/info/exclude`, so your repository's files and `.gitignore` are untouched.

- `rules.json`: the Team Rules from your intake answers
- `NOTES.md`: the Mapper's map and the Lead's log
- `TASKS.md`: every issue and its next step, regenerated on each change
- `evidence/<issue>/`: full logs and a JSON record of every proof
- `wt/<issue>/`: one git worktree per issue
- `BRIEF.md`: the final brief

Issue branches are named `crew/<id>-<slug>`. The brief ends the run: the hook stands down and the worktrees of delivered issues are removed. Branches and evidence are kept.

## Cost

With the hook installed, `crew budget` and the brief report tokens by role measured from the session transcript. Without it, the Lead logs each agent's reported usage. A dollar figure appears only if you put your own rates in the Team Rules, and it is labelled an estimate.

The Lead is the largest cost, because every request it makes re-reads its whole conversation. The playbook has it run each stage for a whole wave in one message; in the trial runs that cut the Lead's requests from 39 to 21.

`/crew audit` runs `audit.py` against your setup: filler lines in `CLAUDE.md` with file and line, pinned effort and model, and what recent sessions actually spent, including cache misses and model switches. It changes nothing without your OK.

## How it was tested

- 196 tests drive the real scripts against real temporary git repositories, including the cheating cases: a weakened or deleted test, a fix left uncommitted, an edit outside the lane, a fenced path, a "green" run that executed no tests, two issues that are each green alone and red together.
- A mutation pass disabled each gate and guard rule in turn and confirmed the suite fails for every one.
- End-to-end runs loaded the plugin into a nested Claude Code session (`claude -p --plugin-dir`) on a small sample project with seeded bugs, a secret file and a migrations folder. See the pull request that added this plugin for the measured results.

## Development

```bash
uv run pytest plugins/crew/tests -q
claude plugin validate plugins/crew --strict
```

The tests drive the real scripts against real temporary git repositories.
