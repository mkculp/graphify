#!/usr/bin/env python3
"""PreToolUse guard for an active crew run.

Stops a rule from being broken before it costs a build cycle: secrets are not
read, fenced paths and sealed proving tests are not written, each role writes
only where it is allowed to, and nothing is published or force-pushed.

This is prevention and it is heuristic, above all for Bash. The exact check is
`crew prove`, which reads the real diff. So the guard fails open: with no
active run, or on any internal error, it allows the call (exit 0). A denial is
exit 2 with the reason on stderr, which Claude Code shows to the agent.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import sys
from pathlib import Path

WRITE_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
# Files under .crew/ that only the crew command may write.
GENERATED = ("state.json", "state.lock", "sessions.json", "host.json", "ledger.jsonl", "TASKS.md", "BRIEF.md")
# Commands that change the files they are given.
_MUTATING_VERBS = {"rm", "mv", "tee", "truncate", "chmod", "chown", "dd", "touch", "shred", "unlink", "rmdir"}
# Commands that write only their last argument and read the rest.
_COPY_VERBS = {"cp", "ln", "install", "rsync", "scp"}
_GIT_MUTATING = {"rm", "mv", "checkout", "restore", "apply", "stash", "clean", "reset", "revert"}
_SEPARATORS = {";", "&&", "||", "|", "&", "|&", ";;", "(", ")"}
_PUBLISH = [
    (r"\b(npm|yarn|pnpm|bun)\s+publish\b", "publishing a package"),
    (r"\b(twine\s+upload|poetry\s+publish|uv\s+publish|flit\s+publish)\b", "publishing a package"),
    (r"\b(cargo\s+publish|gem\s+push|mvn\s+deploy|dotnet\s+nuget\s+push)\b", "publishing a package"),
    (r"\bdocker\s+(image\s+)?push\b", "pushing a container image"),
    (r"\bgh\s+release\s+(create|upload|edit|delete)\b", "changing a release"),
    (r"\b(terraform|tofu|pulumi)\s+(apply|destroy|up)\b", "changing infrastructure"),
    (r"\bkubectl\s+(apply|delete|replace|patch|scale|rollout)\b", "changing a cluster"),
    (r"\bhelm\s+(install|upgrade|uninstall|rollback)\b", "changing a cluster"),
    (r"\b(vercel|netlify|fly|flyctl|railway|heroku)\s+(deploy|--prod|releases?)\b", "deploying"),
    (r"\b(aws|gcloud|az)\s+\S+\s+(deploy|delete|rm|rb)\b", "changing cloud resources"),
]


def deny(reason: str) -> int:
    print("crew guard: " + reason, file=sys.stderr)
    return 2


def find_run(start: str) -> Path | None:
    """The project root whose .crew/state.json covers `start`, found without git."""
    try:
        path = Path(start).resolve()
    except OSError:
        return None
    for candidate in (path, *path.parents):
        if (candidate / ".crew" / "state.json").is_file():
            return candidate
    return None


def role_of(agent_type: str | None, roles: tuple[str, ...]) -> str:
    """lead (main thread), one of the crew roles, or other (any other subagent)."""
    if not agent_type:
        return "lead"
    name = str(agent_type).split(":")[-1].lower()
    if name.startswith("crew-"):
        name = name[len("crew-"):]
    return name if name in roles else "other"


def record_session(state_dir: Path, transcript: str | None, permission_mode: str | None = None) -> None:
    """Remember the transcript so `crew brief` can report measured token usage,
    and the permission mode so the plan can warn about an attended-only setup."""
    if permission_mode:
        host = state_dir / "host.json"
        try:
            current = json.loads(host.read_text(encoding="utf-8")).get("permission_mode") if host.exists() else None
        except (OSError, ValueError):
            current = None
        if current != permission_mode:
            host.write_text(json.dumps({"permission_mode": permission_mode}), encoding="utf-8")
    if not transcript:
        return
    path = state_dir / "sessions.json"
    try:
        known = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    except (OSError, ValueError):
        known = []
    if transcript not in known:
        known.append(transcript)
        tmp = path.with_name(path.name + f".tmp{os.getpid()}")
        tmp.write_text(json.dumps(known), encoding="utf-8")
        os.replace(tmp, path)


def locate(root: Path, target: str, cwd: str) -> tuple[str, str, str]:
    """Classify a path: (zone, issue id, path relative to that zone's checkout).

    zone is `outside` (not in this project), `state` (crew's own files),
    `worktree` (an issue worktree) or `main` (the main checkout).
    """
    raw = Path(target)
    if not raw.is_absolute():
        raw = Path(cwd) / raw
    resolved = Path(os.path.realpath(str(raw)))
    try:
        rel = resolved.relative_to(Path(os.path.realpath(str(root)))).as_posix()
    except ValueError:
        return "outside", "", ""
    parts = rel.split("/")
    if parts[0] == ".crew":
        if len(parts) >= 3 and parts[1] == "wt":
            return "worktree", parts[2], "/".join(parts[3:])
        return "state", "", "/".join(parts[1:])
    return "main", "", rel


def check_write(crew, root: Path, state: dict, rules: dict, role: str, target: str, cwd: str) -> str | None:
    zone, issue_id, rel = locate(root, target, cwd)
    if zone == "outside":
        return None
    if zone == "state":
        if rel in GENERATED or rel.startswith("evidence/") or rel.startswith("proof/") or rel.startswith("bin/"):
            return f".crew/{rel} is written only by the crew command; never edit evidence or state by hand."
        if rel == "rules.json" and role != "lead":
            return "only the Lead writes the Team Rules, and only from the user's intake answers."
        return None
    fence = crew.protected_hit(rel, rules)
    if fence:
        return (f"{rel} is fenced by never-touch pattern {fence!r}. It needs the user's explicit yes "
                "(Team Rules allow_touch). Park the issue with one question instead.")
    if zone == "main":
        if role == "lead":
            return ("the Lead plans, assigns and verifies; it does not write project files. Dispatch a "
                    "builder into the issue worktree. To work by hand, run `crew pause` first.")
        return ("a crew run is active: project files change only inside an issue worktree under "
                ".crew/wt/<issue>/. Use the worktree path the Lead gave you.")
    issue = (state.get("issues") or {}).get(issue_id) or {}
    sealed = (issue.get("tests") or {}).get("files") or {}
    if rel in sealed:
        return (f"{rel} is a sealed proving test for issue {issue_id}. Nobody edits it after seal. If the "
                "test is wrong, hand back to the Lead with the evidence; the Lead runs `crew unseal`.")
    if role == "lead":
        return "the Lead does not write feature code. Dispatch a builder for issue " + issue_id + "."
    if role in ("prover", "reviewer", "mapper"):
        return f"the {role} is read-only on project code."
    if role == "builder":
        owns = issue.get("owns") or []
        if owns and not crew.matches_any(rel, owns) and not crew.is_test_path(rel):
            return (f"{rel} is outside the lane for issue {issue_id} ({', '.join(owns)}). Stop and hand "
                    "back with NEXT STEP: widen the lane, and say why this file is needed.")
    return None


def _unquoted(command: str) -> str:
    """The command with quoted strings blanked, so text inside quotes is not read as syntax."""
    return re.sub(r"'[^']*'|\"(?:\\.|[^\"\\])*\"", " Q ", command)


_HEREDOC = re.compile(r"<<-?\s*(['\"]?)(\w+)\1")


def _without_heredocs(command: str) -> str:
    """Drop heredoc bodies: they are data being written, not paths being touched."""
    lines = command.split("\n")
    kept: list[str] = []
    waiting: list[str] = []
    for line in lines:
        if waiting:
            if line.strip() == waiting[0]:
                waiting.pop(0)
            continue
        kept.append(line)
        waiting = [m.group(2) for m in _HEREDOC.finditer(line)]
    return "\n".join(kept)


def check_bash(crew, root: Path, state: dict, rules: dict, role: str, command: str, cwd: str) -> str | None:
    command = _without_heredocs(command)
    bare = _unquoted(command)
    for pattern, what in _PUBLISH:
        if re.search(pattern, bare):
            return f"{what} is public and needs the user's explicit yes. It is not part of a crew run."
    for segment in re.split(r"[;&|\n]+", bare):
        if re.search(r"\bgit\b.*\bpush\b", segment):
            if re.search(r"\s(--force|--force-with-lease|-f)\b|\s\+\S+", segment):
                return "force-pushing rewrites history. The crew never needs it; fix forward with a new commit."
            if re.search(r"\s--(tags|mirror|delete)\b|\s:\S+", segment):
                return "pushing tags or deleting remote refs is public and needs the user's explicit yes."
            if role != "lead":
                return "only the Lead pushes. Commit in your worktree and hand back."
        if re.search(r"\bgit\b.*\b(commit|push|merge|am)\b.*\s--no-verify\b", segment) or re.search(
                r"\bgit\b.*\bcommit\b.*\s-\w*n\w*\b", segment):
            return "skipping commit or push hooks hides failures. Run the hooks and fix what they report."
    for segment in _segments(command):
        reason = _check_segment(crew, root, state, rules, segment, cwd)
        if reason:
            return reason
    return None


def _segments(command: str) -> list[list[str]]:
    """Split a shell command into simple commands, keeping redirect operators as tokens."""
    try:
        lexer = shlex.shlex(command.replace("\n", " ; "), posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return [command.split()]
    segments: list[list[str]] = [[]]
    for token in tokens:
        if token in _SEPARATORS:
            segments.append([])
        else:
            segments[-1].append(token)
    return [segment for segment in segments if segment]


def _check_segment(crew, root: Path, state: dict, rules: dict, tokens: list[str], cwd: str) -> str | None:
    """One simple command: which paths does it read, and which does it write?"""
    written: list[str] = []
    named: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in (">", ">>", ">|", "&>", "&>>") and index + 1 < len(tokens):
            written.append(tokens[index + 1])
            index += 2
            continue
        if token and set(token) <= set("<>&|"):
            index += 2 if token in ("<", "<<", "<<<", "<<-", ">&", "<&") else 1
            continue
        if token and not token.startswith("-") and len(token) <= 400:
            named.append(token)
        index += 1
    words = [word.rsplit("/", 1)[-1] for word in named]
    paths = named[1:]
    if _MUTATING_VERBS & set(words) or ("sed" in words and any(re.match(r"-\w*i|--in-place", t) for t in tokens)) \
            or ("perl" in words and any(re.match(r"-\w*i", t) for t in tokens)):
        written += paths
    elif _COPY_VERBS & set(words) and paths:
        written.append(paths[-1])
    elif "git" in words and _GIT_MUTATING & set(words):
        written += paths
    for target in named + written:
        zone, _issue, rel = locate(root, target, cwd)
        if zone in ("main", "worktree") and rel:
            secret = crew.secret_hit(rel, rules)
            if secret:
                return (f"{rel} matches secret pattern {secret!r}. Secrets are never read or written without "
                        "the user's explicit yes (Team Rules allow_touch).")
    for target in written:
        zone, issue_id, rel = locate(root, target, cwd)
        if zone not in ("main", "worktree") or not rel:
            continue
        fence = crew.protected_hit(rel, rules)
        if fence:
            return (f"this command would change {rel}, which is fenced by never-touch pattern {fence!r}. "
                    "It needs the user's explicit yes (Team Rules allow_touch).")
        if zone == "worktree":
            sealed = ((state.get("issues") or {}).get(issue_id) or {}).get("tests", {}).get("files") or {}
            if rel in sealed:
                return (f"this command would change the sealed proving test {rel}. Sealed tests are "
                        "immutable; hand back to the Lead if the test is wrong.")
    return None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        return 0
    if not isinstance(payload, dict):
        return 0
    cwd = str(payload.get("cwd") or os.getcwd())
    root = find_run(cwd) or find_run(os.environ.get("CLAUDE_PROJECT_DIR") or cwd)
    if root is None:
        return 0
    state_dir = root / ".crew"
    try:
        state = json.loads((state_dir / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    if (state.get("run") or {}).get("status") != "active":
        return 0
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import crew  # noqa: E402  (imported late so an idle project pays almost nothing)

    try:
        rules = crew.load_rules(state_dir)
    except Exception:
        rules = dict(crew.RULES_TEMPLATE)
    record_session(state_dir, payload.get("transcript_path"), payload.get("permission_mode"))
    role = role_of(payload.get("agent_type"), crew.ROLES)
    tool = str(payload.get("tool_name") or "")
    tool_input = payload.get("tool_input") or {}
    reason = None
    if tool in WRITE_TOOLS:
        target = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
        if target:
            reason = check_write(crew, root, state, rules, role, str(target), cwd)
    elif tool == "Read":
        target = str(tool_input.get("file_path") or "")
        zone, _issue, rel = locate(root, target, cwd) if target else ("outside", "", "")
        secret = crew.secret_hit(rel, rules) if zone in ("main", "worktree") and rel else None
        if secret:
            reason = (f"{rel} matches secret pattern {secret!r}. Secrets are never read without the "
                      "user's explicit yes (Team Rules allow_touch).")
    elif tool == "Bash":
        command = str(tool_input.get("command") or "")
        if command:
            reason = check_bash(crew, root, state, rules, role, command, cwd)
    return deny(reason) if reason else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:  # a broken guard must never block work; `crew prove` is the authority
        sys.exit(0)
