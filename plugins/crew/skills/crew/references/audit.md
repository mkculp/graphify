# Audit mode: cut token cost without lowering quality

You are auditing the user's Claude Code setup. Measure first, stay concrete, and never raise effort to look thorough. Change nothing without the user's OK.

## 1. Intake: at most four questions, in one message

Ask only what you cannot see: which repository, the effort level they run by default, whether a CLAUDE.md exists, and what "done" usually looks like for them. The first three are usually answered by the next step, so run it first and ask only what is left.

## 2. Measure

```bash
python3 "${CLAUDE_SKILL_DIR}/scripts/audit.py" --project . --sessions 5 --save .crew/tmp/audit-before.json
```

It reads, and does not change:

- instruction files (`CLAUDE.md`, `CLAUDE.local.md`, `AGENTS.md`, `.claude/CLAUDE.md`, `~/.claude/CLAUDE.md`) for filler lines and vague guardrails, with file and line
- settings and environment for a pinned model or effort level
- the project's recent session transcripts: tokens by kind, cache hit rate, effort per request, model and effort switches, requests that missed the cache, session length, compactions, and what each subagent type cost

Token counts are measured. The ranking weights them by standard price ratios, and that weighting is an estimate; say so. If there are no transcripts, say that nothing was measured and ask the user to run one real task first.

## 3. Report

Give the user, in this order:

1. The before and after table from the script: setting, old value, new value, why. Biggest measured exposure first.
2. The exact instruction-file lines to remove, as `file:line: text`.
3. The vague guardrails, each with the named rule that should replace it. Write the replacement rule yourself from what the project actually does: a command that must pass, a file that must be read first, a path that must not be touched.
4. The TASKS.md rule to add.
5. A closing section headed WHAT STILL LEAKS?, listing what the audit could not measure.

Mark every number as measured or as an estimate. Do not predict savings.

## 4. What to recommend, and when

- Effort: make `medium` the default. Go higher for a task only after a measured worse answer at medium, for a long chain of dependent steps, or for a high-stakes review. Never raise it just in case.
- Instruction files: remove every line that only tells the model to think harder ("think carefully", "think step by step", "apply the highest level of thinking depth"). Replace vague guardrails ("be thorough") with named, specific rules. Keep the file short; move detail into skills that load on demand.
- Replies: stop asking for reasoning to be written out in the answer. The thinking was already paid for once; the reply should carry the result.
- Context: `/clear` between unrelated tasks. Keep the task list in `TASKS.md` so the place survives a context summary.
- Cache: choose the model, the effort level and the system prompt once per session and leave them. Each model has its own prompt cache, so a switch re-reads the whole conversation at full price.
- Models: route by difficulty. File lookups and quick edits go to a small model; the top model is for hard logic.

When an instruction file tells the model both to think at maximum depth everywhere and to keep cost down, name the conflict and resolve it the way the crew does: depth is assigned per role and raised on evidence, not applied to every turn.

## 5. Apply, with the user's OK

Show the first change and wait for the user's go. Then apply one change at a time. For settings, show the exact key and file. For instruction files, show the diff.

## 6. Test before and after on one real task

Ask the user to run the same real task in a fresh session with the changes applied. Then:

```bash
python3 "${CLAUDE_SKILL_DIR}/scripts/audit.py" --project . --compare .crew/tmp/audit-before.json
```

Report the measured difference. If quality dropped on that task, say so and raise effort for that kind of task only.
