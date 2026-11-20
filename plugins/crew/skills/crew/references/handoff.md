# Dispatching the crew

Every dispatch and every return uses the same four fields: ISSUE, TEST, EVIDENCE, NEXT STEP. A dispatch adds the paths the agent needs. Give paths, not pasted content: the agent reads files in its own context, which is cheaper than you copying them into yours.

Fill in the angle brackets. Use absolute paths. `GRAPH` is the graph command with its `--graph` path, or `none`.

Dispatch one stage for the whole wave in a single message, with `run_in_background: false` on every agent, so the results return together. One agent per message costs you a full re-read of the conversation for each.

## Mapper (once, before the plan)

```
CREW: </abs/project>/.crew/bin/crew
ROOT: </abs/project>
ISSUE: map the repository for these issues:
- <id>: <one-line title>
TEST: none yet. Find the setup, full-suite, single-test and check commands in the repository's own files.
EVIDENCE: Team Rules at </abs/project>/.crew/rules.json
NEXT STEP: write </abs/project>/.crew/NOTES.md, then hand back.
```

## Breaker (one per issue, a wave in parallel)

```
CREW: </abs/project>/.crew/bin/crew
ISSUE: <id> <title>. Spec: </abs/project>/.crew/issues/<id>.md
TEST: none yet. Write the smallest test that fails on the base for this issue's reason, commit it, and seal it.
EVIDENCE: worktree </abs/project>/.crew/wt/<id> (work only here); notes </abs/project>/.crew/NOTES.md; rules </abs/project>/.crew/rules.json
GRAPH: <graphify ... --graph </abs/project>/graphify-out/graph.json | none>
NEXT STEP: seal with `$CREW seal <id> --cmd "..." --test-file ...`, then hand back.
```

## Builder (one per issue, a wave in parallel)

```
CREW: </abs/project>/.crew/bin/crew
ISSUE: <id> <title>. Spec: </abs/project>/.crew/issues/<id>.md
TEST: <the sealed proving command> (sealed: <test files>; do not edit them)
EVIDENCE: worktree </abs/project>/.crew/wt/<id> (work only here); lane <globs>; red output </abs/project>/.crew/evidence/<id>/red.log; notes </abs/project>/.crew/NOTES.md; rules </abs/project>/.crew/rules.json; full suite `<suite command>`
GRAPH: <graphify ... --graph ... | none>
NEXT STEP: smallest change that turns the test green, full suite green, `$CREW check <id>` passes, commit, hand back.
```

On a retry, replace the NEXT STEP line with:

```
NEXT STEP: your last commit was rejected by the <prover|reviewer|land>. Read the rejection in `$CREW status` and the full output in <evidence file path>. Fix that, re-run, commit, hand back.
```

Set `effort: high` on the dispatch for a second retry of the same issue.

## Prover (one per issue, as soon as its Builder hands back)

Give the Prover nothing about what the Builder did or said.

```
CREW: </abs/project>/.crew/bin/crew
ROOT: </abs/project>
ISSUE: <id> <title>. Spec: </abs/project>/.crew/issues/<id>.md
TEST: run `$CREW prove <id>` from ROOT.
EVIDENCE: none given. Report only what the tools print.
NEXT STEP: hand back the verdict.
```

## Reviewer (one per proven issue)

```
CREW: </abs/project>/.crew/bin/crew
ROOT: </abs/project>
ISSUE: <id> <title>. Spec: </abs/project>/.crew/issues/<id>.md
TEST: <the sealed proving command>
EVIDENCE: worktree </abs/project>/.crew/wt/<id>; diff `git diff <base sha>..<head sha>`; proof </abs/project>/<proof file from crew status>; notes </abs/project>/.crew/NOTES.md
GRAPH: <graphify ... --graph ... | none>
NEXT STEP: record the verdict with `$CREW review <id> --file ...` from ROOT, then hand back.
```

For an issue that touches a file NOTES.md lists as risky, or a path the user lifted a fence for, set `effort: xhigh` on the Reviewer dispatch.

## What comes back

Four lines. Then run `$CREW status`: it shows the status each issue really has, the next step, and any open rejection. If an agent's return disagrees with `crew status`, the status is right.

If an agent returns without the four fields, or stops mid-task, treat the issue as stuck: check `crew status`, and either re-dispatch with the missing piece or park the issue with one question.

## Logging usage

`$CREW budget` says where its numbers come from. With the guard hook installed it reads the session transcript and nothing needs logging. If it reports "no transcript recorded", log each return's reported usage yourself: `$CREW log --role <role> --issue <id> --tokens <n> --ms <n>`.
