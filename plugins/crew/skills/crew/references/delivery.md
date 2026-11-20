# Delivering

Only approved work is delivered. `crew pr-body` and `crew land` refuse anything whose branch head has no passing proof, so a commit added after the proof means prove and review again.

## pr-per-issue

For each approved issue:

1. `git push -u origin <issue branch>` (the branch name is in `crew status --json`).
2. `$CREW pr-body <id> --out .crew/tmp/pr-<id>.md`, then open the pull request with that body, as a draft if the host session says so.
3. `$CREW pr <id> --url <url>`.

Independent issues are cut from the same base, so their PRs can merge in any order. A coupled issue is built on top of the issue it is coupled with (`crew waves` sets this, and `crew start` waits until that issue is approved). Its PR says "Stacked on" and must merge second; open it against the earlier issue's branch if the host allows, otherwise against the base and say that it contains the earlier commits until that PR merges.

## single-branch

Use this when the session may push to only one branch. Issue branches stay local.

1. Make sure the main checkout is on the delivery branch with no uncommitted changes.
2. For each approved issue, in wave order: `$CREW land <id>`. It cherry-picks the issue's commits onto the delivery branch in a scratch checkout, runs setup, re-runs the proving test of this issue and of every issue landed before it, then the full suite and the checks. Only if all are green does it fast-forward the delivery branch. A red result moves nothing: the issue goes back to its Builder to rebase onto the delivery branch, and then through Prover and Reviewer again.
3. `git push -u origin <delivery branch>`.
4. `$CREW pr-body --all --out .crew/tmp/pr.md`, open one pull request, then `$CREW pr <id> --url <url>` for each landed issue.

## What every PR carries

`crew pr-body` renders these from the evidence, so they are quoted, not recalled:

- a one-line summary
- the issue it closes
- the test that proves it
- output before (the proving test on the base) and after (on the head, clean checkout)
- files touched
- risk level
- how to roll it back, and in single-branch mode the ids of the commits that landed
- the reason for any new dependency, and any waiver the user granted
- a closing section headed WHY THIS COULD BREAK

If the repository has a PR template, keep its headings and place this content under them. Skip template sections that ask for credentials, environment variables or internal hostnames. Do not edit the quoted output.

## The final check

Before the brief, for every issue marked proven, open its evidence file and confirm:

- the red run on the base has a non-zero exit and fails for the issue's reason
- the green run and the full suite ran on a clean checkout of the head that is being delivered
- `files` lists nothing outside the issue
- no gate is `fail`; every `warn` has a reason in the review; every `waived` gate traces to a yes from the user

Anything that does not hold goes back into the loop. It does not go into the brief as a caveat.

## The brief

`$CREW brief` writes `.crew/BRIEF.md` and prints it. Send it as one message, unchanged. After it, add one sentence that states the single decision you recommend (merge, retry or drop) and any out-of-scope problem a Reviewer reported. It contains:

- a table of issue, proving test and status (proven, blocked or parked) with the reason
- total time, measured, and tokens by role, measured from the transcript when the guard hook is installed; a dollar figure only when the Team Rules carry rates, and labelled as an estimate
- every rejection and how it was resolved
- any waivers used
- WHAT STILL LEAKS: where the tokens went and how much rework there was

The brief ends the run: the guard hook stands down, so the user can edit their own project again, and the worktrees of delivered issues are removed. Branches and evidence are kept. `$CREW resume` reopens the run if the user answers a parked question.
