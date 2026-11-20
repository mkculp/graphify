#!/usr/bin/env python3
"""crew: proof harness and run ledger for the crew skill.

Every gate here is a tool run whose exit code decides the outcome. Agents make
claims; this file checks them. It needs only git and the Python standard
library, and never edits source files: it creates branches and worktrees, runs
the commands in the Team Rules, and records what happened under `.crew/`.

Exit codes: 0 ok / gate passed, 1 gate failed, 2 usage or harness error,
3 budget exceeded.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Iterator

STATE_DIR = ".crew"
ROLES = ("mapper", "breaker", "builder", "prover", "reviewer")
STATUSES = (
    "queued",    # registered, no branch yet
    "started",   # branch + worktree exist, no proving test sealed
    "red",       # proving test sealed and confirmed failing on base
    "proven",    # all gates green on a clean checkout at the recorded head
    "approved",  # reviewer approved the proven head
    "landed",    # single-branch delivery: integrated and re-proven
    "pr",        # pull request recorded
    "parked",    # one written question is waiting for the user
    "blocked",   # cannot proceed; reason recorded
)
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

# Secrets: no agent may read or write these without the user's explicit yes
# (rules.allow_touch).
DEFAULT_SECRETS = [
    ".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "*.jks", "*.keystore",
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", ".npmrc", ".pypirc", ".netrc",
    "credentials.json", "secrets.*", "secrets/",
]
# Fenced: readable for context, but no agent may change them without that yes.
DEFAULT_FENCED = [
    "migrations/", "alembic/versions/", "db/migrate/",  # migrations
    "billing/", "payments/",                            # billing
]
DEFAULT_PROTECTED = DEFAULT_SECRETS + DEFAULT_FENCED
# Template files that look like secrets but are meant to be committed.
PROTECTED_EXCEPTIONS = [".env.example", ".env.sample", ".env.template", ".env.dist"]

DEP_MANIFESTS = [
    "package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "bun.lockb",
    "requirements*.txt", "pyproject.toml", "poetry.lock", "uv.lock", "Pipfile",
    "Pipfile.lock", "setup.py", "setup.cfg", "Cargo.toml", "Cargo.lock", "go.mod",
    "go.sum", "Gemfile", "Gemfile.lock", "pom.xml", "build.gradle", "build.gradle.kts",
    "composer.json", "composer.lock", "*.csproj", "packages.lock.json", "mix.exs",
]
TEST_CONFIGS = [
    "pytest.ini", "tox.ini", "conftest.py", "jest.config.*", "vitest.config.*",
    ".mocharc.*", "karma.conf.*", "playwright.config.*", "phpunit.xml*", ".rspec",
]

_TEST_DIR_PARTS = {"test", "tests", "__tests__", "spec", "specs", "e2e", "testing"}
_TEST_FILE_RE = re.compile(
    r"(^test_.*\.py$|.*_test\.(py|go|rb|rs|exs?|cc|cpp|c)$|.*\.(test|spec)\.[cm]?[jt]sx?$"
    r"|.*Tests?\.(java|kt|cs|scala|swift|php)$|.*_spec\.rb$|.*Spec\.(scala|kt|groovy)$)"
)
# Added lines in test files that disable or narrow tests.
_SKIP_RE = re.compile(
    r"(@pytest\.mark\.(skip|skipif|xfail)|pytest\.(skip|xfail)\(|@unittest\.(skip|skipIf|skipUnless"
    r"|expectedFailure)|\.skipTest\(|\b(it|test|describe|context)\.(skip|todo|only|failing)\b"
    r"|\b(xit|xdescribe|xtest|fit|fdescribe)\s*\(|\bt\.Skip(Now|f)?\(|@Ignore\b|@Disabled\b"
    r"|#\[ignore\b|\[Ignore\b|\bSkip\s*=|^\s*(skip|pending)\b)"
)
# Lines that define a test case, used to detect removed tests.
_TESTDEF_RE = re.compile(
    r"(^\s*(async\s+)?def\s+test\w*\s*\(|\b(it|test)\s*(\.each\b.*)?\(\s*['\"`]|^\s*func\s+Test\w*\s*\("
    r"|@Test\b|#\[(tokio::)?test\b|^\s*(it|specify|scenario)\s+['\"]|\[(Fact|Theory|Test|TestMethod)\b)"
)
# A line that makes an assertion, across common test frameworks. Deliberately
# narrower than "mentions assert": `require('x')` and prose are not assertions.
_ASSERT_RE = re.compile(
    r"(\bassert\w*[\s!(.]|\bexpect\s*\(|\.should\b|\bshould\.\w|\brequire\.\w+\(|\bt\.(Error|Fatal|Fail)\w*\("
    r"|\b(EXPECT|ASSERT)_\w+\(|\bAssert\.\w+\(|\bverify\w*\s*\(|\braises\s*\()"
)
# Runner output that means a command exited 0 without running a single test.
_NO_TESTS_RE = re.compile(
    r"(\bRan 0 tests\b|\bcollected 0 items\b|\bno tests ran\b|\bNo tests found\b|\bno tests to run\b"
    r"|\[no test files\]|\b0 passing\b|\bTests:\s+0 total\b|\b0 tests? (run|executed|passed)\b"
    r"|\brunning 0 tests\b|\b0 examples, 0 failures\b|\bNo tests? (were )?(executed|collected)\b)",
    re.IGNORECASE,
)


class CrewError(Exception):
    """A usage or harness error (exit 2), as opposed to a failed gate (exit 1)."""


# --------------------------------------------------------------------------- git

def _git(args: list[str], cwd: Path, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, encoding="utf-8",
        errors="replace",
    )
    if check and proc.returncode != 0:
        raise CrewError(f"git {' '.join(args)} failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout


def _git_ok(args: list[str], cwd: Path) -> bool:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True).returncode == 0


def find_root(start: Path | None = None) -> Path:
    """The main checkout, even when called from a linked worktree."""
    start = start or Path.cwd()
    proc = subprocess.run(
        ["git", "worktree", "list", "--porcelain"], cwd=str(start), capture_output=True,
        text=True, encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0:
        raise CrewError("not inside a git repository")
    for line in proc.stdout.splitlines():
        if line.startswith("worktree "):
            return Path(line[len("worktree "):])
    raise CrewError("could not locate the main git worktree")


def rev(root: Path, ref: str) -> str:
    return _git(["rev-parse", "--verify", ref + "^{commit}"], root).strip()


def changed_files(root: Path, base: str, head: str) -> list[dict[str, str]]:
    """Files changed between two commits: [{status, path, old}] with renames detected."""
    out = _git(["diff", "--name-status", "-M", "-z", base, head], root)
    parts = out.split("\0")
    result: list[dict[str, str]] = []
    i = 0
    while i < len(parts) and parts[i]:
        status = parts[i]
        if status[0] in "RC" and i + 2 < len(parts):
            result.append({"status": status[0], "old": parts[i + 1], "path": parts[i + 2]})
            i += 3
        else:
            result.append({"status": status[0], "old": "", "path": parts[i + 1]})
            i += 2
    return result


def diff_lines(root: Path, base: str, head: str, path: str) -> tuple[list[str], list[str]]:
    """(added, removed) source lines for one file between two commits."""
    out = _git(["diff", "-U0", "--no-color", base, head, "--", path], root)
    added, removed = [], []
    for line in out.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            added.append(line[1:])
        elif line.startswith("-"):
            removed.append(line[1:])
    return added, removed


def numstat(root: Path, base: str, head: str) -> dict[str, tuple[str, str]]:
    out = _git(["diff", "--numstat", "--no-renames", "-z", base, head], root)
    stats: dict[str, tuple[str, str]] = {}
    for rec in out.split("\0"):
        bits = rec.split("\t")
        if len(bits) == 3:
            stats[bits[2]] = (bits[0], bits[1])
    return stats


# ------------------------------------------------------------------------- globs

def glob_to_re(pattern: str, ignore_case: bool = False) -> "re.Pattern[str]":
    """gitignore-style glob. A pattern with no slash matches at any depth; a
    match on a directory covers everything beneath it."""
    pat = pattern.strip().replace("\\", "/")
    if pat.startswith("./"):
        pat = pat[2:]
    anchored = pat.startswith("/") or "/" in pat.rstrip("/")
    pat = pat.strip("/")
    out: list[str] = []
    i = 0
    while i < len(pat):
        ch = pat[i]
        if ch == "*":
            if pat.startswith("**/", i):
                out.append("(?:.*/)?")
                i += 3
                continue
            if pat.startswith("**", i):
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
        elif ch == "?":
            out.append("[^/]")
        elif ch == "[":
            end = pat.find("]", i + 1)
            if end == -1:
                out.append(re.escape(ch))
            else:
                body = pat[i + 1:end]
                if body.startswith("!"):
                    body = "^" + body[1:]
                out.append("[" + body + "]")
                i = end
        else:
            out.append(re.escape(ch))
        i += 1
    prefix = "" if anchored else "(?:.*/)?"
    return re.compile("^" + prefix + "".join(out) + "(?:/.*)?$", re.IGNORECASE if ignore_case else 0)


def norm_rel(path: str) -> str:
    p = path.replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p.strip("/")


def matches_any(path: str, patterns: list[str], ignore_case: bool = False) -> str | None:
    """The first pattern that matches `path`, else None."""
    p = norm_rel(path)
    for pat in patterns:
        if pat.strip() and glob_to_re(pat, ignore_case).match(p):
            return pat
    return None


def is_test_path(path: str) -> bool:
    p = norm_rel(path)
    parts = p.split("/")
    if any(part.lower() in _TEST_DIR_PARTS for part in parts[:-1]):
        return True
    return bool(_TEST_FILE_RE.match(parts[-1]))


def protected_patterns(rules: dict[str, Any]) -> list[str]:
    return DEFAULT_PROTECTED + [str(p) for p in rules.get("never_touch") or []]


def protected_hit(path: str, rules: dict[str, Any]) -> str | None:
    """The pattern that fences `path`, or None when writing it is allowed."""
    p = norm_rel(path)
    if p == STATE_DIR or p.startswith(STATE_DIR + "/"):
        return None
    if matches_any(p, [str(a) for a in rules.get("allow_touch") or []], ignore_case=True):
        return None
    user_hit = matches_any(p, [str(x) for x in rules.get("never_touch") or []], ignore_case=True)
    if user_hit:
        return user_hit
    if matches_any(p, PROTECTED_EXCEPTIONS, ignore_case=True):
        return None
    return matches_any(p, DEFAULT_PROTECTED, ignore_case=True)


def secret_hit(path: str, rules: dict[str, Any]) -> str | None:
    """The secret pattern that forbids even reading `path`, or None."""
    p = norm_rel(path)
    if matches_any(p, [str(a) for a in rules.get("allow_touch") or []], ignore_case=True):
        return None
    if matches_any(p, PROTECTED_EXCEPTIONS, ignore_case=True):
        return None
    return matches_any(p, DEFAULT_SECRETS, ignore_case=True)


# ------------------------------------------------------------------- state files

def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _parse_ts(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except json.JSONDecodeError as exc:
        raise CrewError(f"{path} is not valid JSON: {exc}")


RULES_TEMPLATE: dict[str, Any] = {
    "version": 1,
    "done": "",
    "never_touch": [],
    "allow_touch": [],
    "commands": {"setup": "", "suite": "", "checks": {}},
    "env": {},
    "budget": {"hours": None, "usd": None, "deadline": ""},
    "limits": {"max_builders": 4, "stuck_minutes": 20, "command_timeout_s": 1800},
    "delivery": {"mode": "pr-per-issue", "base": "", "branch": ""},
    "rates_usd_per_mtok": {},
}


def load_rules(sd: Path) -> dict[str, Any]:
    rules = read_json(sd / "rules.json")
    if rules is None:
        raise CrewError(f"no Team Rules at {sd / 'rules.json'}; run `crew init` and fill them in")
    merged = json.loads(json.dumps(RULES_TEMPLATE))
    for key, value in rules.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key].update(value)
        else:
            merged[key] = value
    return merged


def rules_problems(rules: dict[str, Any]) -> list[str]:
    problems = []
    if not str(rules.get("done") or "").strip():
        problems.append('"done" is empty: write what done means for this run')
    cmds = rules.get("commands") or {}
    if not str(cmds.get("suite") or "").strip():
        problems.append('"commands.suite" is empty: the full-suite command is required')
    if not isinstance(cmds.get("checks") or {}, dict):
        problems.append('"commands.checks" must be an object of name -> command')
    mode = (rules.get("delivery") or {}).get("mode")
    if mode not in ("pr-per-issue", "single-branch"):
        problems.append('"delivery.mode" must be "pr-per-issue" or "single-branch"')
    if mode == "single-branch" and not (rules.get("delivery") or {}).get("branch"):
        problems.append('"delivery.branch" is required when delivery.mode is "single-branch"')
    for key in ("never_touch", "allow_touch"):
        if not isinstance(rules.get(key) or [], list):
            problems.append(f'"{key}" must be a list of path globs')
    limits = rules.get("limits") or {}
    for key in ("max_builders", "stuck_minutes", "command_timeout_s"):
        value = limits.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            problems.append(f'"limits.{key}" must be a positive integer')
    return problems


def load_state(sd: Path) -> dict[str, Any]:
    state = read_json(sd / "state.json")
    if state is None:
        raise CrewError(f"no run at {sd}; run `crew init` first")
    return state


@contextlib.contextmanager
def state_lock(sd: Path, timeout: float = 60.0) -> Iterator[None]:
    lock = sd / "state.lock"
    deadline = time.time() + timeout
    while True:
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            break
        except FileExistsError:
            try:
                stale = time.time() - lock.stat().st_mtime > 300
            except FileNotFoundError:
                continue
            if stale:
                with contextlib.suppress(FileNotFoundError):
                    lock.unlink()
                continue
            if time.time() > deadline:
                raise CrewError(f"state is locked by another crew command ({lock})")
            time.sleep(0.1)
    try:
        yield
    finally:
        with contextlib.suppress(FileNotFoundError):
            lock.unlink()


def log_lines(sd: Path, lines: list[str]) -> None:
    """Append to the `## Log` section of NOTES.md, the crew's crash-proof checkpoint."""
    if not lines:
        return
    path = sd / "NOTES.md"
    text = path.read_text(encoding="utf-8") if path.exists() else "# Notes\n"
    if not re.search(r"(?m)^## Log\s*$", text):
        text = text.rstrip("\n") + "\n\n## Log\n"
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%MZ")
    text = text.rstrip("\n") + "\n" + "".join(f"- {stamp} {' '.join(line.split())[:300]}\n" for line in lines)
    write_atomic(path, text)


def mutate(sd: Path, fn: Callable[[dict[str, Any]], Any]) -> Any:
    """Read-modify-write the state under a lock, refresh TASKS.md, and log what changed."""
    with state_lock(sd):
        state = load_state(sd)
        seen = {key: len(issue.get("history", [])) for key, issue in state.get("issues", {}).items()}
        run_before = (state.get("run") or {}).get("status")
        result = fn(state)
        write_atomic(sd / "state.json", json.dumps(state, indent=2, sort_keys=True) + "\n")
        write_atomic(sd / "TASKS.md", render_tasks(state))
        fresh = []
        for key in sorted(state.get("issues", {})):
            for event in state["issues"][key].get("history", [])[seen.get(key, 0):]:
                fresh.append(f"issue {key} {event['event']}" + (f": {event['detail']}" if event.get("detail") else ""))
        run_after = (state.get("run") or {}).get("status")
        if run_after != run_before:
            fresh.append(f"run {run_after}")
        log_lines(sd, fresh)
    return result


def issue_of(state: dict[str, Any], issue_id: str) -> dict[str, Any]:
    issue = state["issues"].get(issue_id)
    if issue is None:
        known = ", ".join(sorted(state["issues"])) or "none"
        raise CrewError(f"unknown issue {issue_id!r} (known: {known})")
    return issue


def note(issue: dict[str, Any], event: str, detail: str = "") -> None:
    issue.setdefault("history", []).append({"at": now(), "event": event, "detail": detail})


def reject(issue: dict[str, Any], by: str, reason: str, head: str = "") -> None:
    issue.setdefault("rejections", []).append(
        {"at": now(), "by": by, "reason": reason, "head": head, "resolved": ""}
    )


def resolve_rejections(issue: dict[str, Any], how: str) -> None:
    for item in issue.get("rejections", []):
        if not item.get("resolved"):
            item["resolved"] = how


def next_step(issue: dict[str, Any], mode: str) -> str:
    status = issue["status"]
    open_rej = [r for r in issue.get("rejections", []) if not r.get("resolved")]
    if status == "queued":
        return "crew start, then Breaker writes the failing test"
    if status == "started":
        return "Breaker: commit the failing test, then crew seal"
    if status == "red":
        if open_rej:
            return f"Builder: fix the {open_rej[-1]['by']} rejection, then Prover"
        return "Builder: smallest change that turns the test green, then Prover"
    if status == "proven":
        return "Reviewer, then crew review"
    if status == "approved":
        return "crew land" if mode == "single-branch" else "push the branch, open the PR, crew pr"
    if status == "landed":
        return "push the delivery branch; PR body from crew pr-body --all"
    if status == "pr":
        return "waiting for the user's merge decision"
    if status == "parked":
        return "needs the user's answer: " + str((issue.get("parked") or {}).get("question", ""))[:120]
    return str((issue.get("blocked") or {}).get("reason", ""))[:120]


def render_tasks(state: dict[str, Any]) -> str:
    run = state.get("run", {})
    mode = run.get("mode", "pr-per-issue")
    lines = [
        "# Crew tasks",
        "",
        f"Run {run.get('id', '?')} is {run.get('status', '?')}. Generated by `crew`; do not edit by hand.",
        "",
        "| Issue | Status | Wave | Next step |",
        "|---|---|---|---|",
    ]
    for issue_id in sorted(state.get("issues", {})):
        issue = state["issues"][issue_id]
        title = str(issue.get("title", "")).replace("|", "/")
        lines.append(
            f"| {issue_id}: {title} | {issue['status']} | {issue.get('wave') or '-'} "
            f"| {next_step(issue, mode).replace('|', '/')} |"
        )
    halt = run.get("halt")
    if halt:
        lines += ["", f"HALT: {halt}. No new work until this is green."]
    return "\n".join(lines) + "\n"


# ----------------------------------------------------------------- running tools

def tail_of(path: Path, lines: int = 40, max_chars: int = 4000) -> str:
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return ""
    text = data[-max_chars * 4:].decode("utf-8", errors="replace")
    out = "\n".join(text.splitlines()[-lines:])
    return out[-max_chars:]


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_cmd(cmd: str, cwd: Path, log: Path, timeout: int, env_extra: dict[str, str] | None = None) -> dict[str, Any]:
    """Run one shell command, capturing all output to `log`. Never raises on failure."""
    log.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.update({str(k): str(v) for k, v in (env_extra or {}).items()})
    started = time.time()
    timed_out = False
    code: int | None
    with open(log, "wb") as handle:
        handle.write(f"$ {cmd}\n".encode("utf-8"))
        handle.flush()
        kwargs: dict[str, Any] = {}
        if os.name == "posix":
            kwargs["start_new_session"] = True
        proc = subprocess.Popen(
            cmd, shell=True, cwd=str(cwd), stdout=handle, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, env=env, **kwargs,
        )
        try:
            code = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            code = None
            if os.name == "posix":
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(proc.pid, signal.SIGKILL)
            else:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True
                )
            with contextlib.suppress(Exception):
                proc.wait(timeout=10)
    return {
        "cmd": cmd,
        "exit": code,
        "timed_out": timed_out,
        "seconds": round(time.time() - started, 2),
        "log": str(log),
        "log_sha256": sha256_of(log),
        "tail": _relative_paths(tail_of(log), cwd),
    }


def _relative_paths(text: str, cwd: Path) -> str:
    """Rewrite the checkout's absolute path to `.`: tails end up in PR bodies."""
    for prefix in {str(cwd), os.path.realpath(str(cwd))}:
        text = text.replace(prefix + os.sep, "").replace(prefix, ".")
    return text


def passed(result: dict[str, Any]) -> bool:
    return result.get("exit") == 0 and not result.get("timed_out")


def valid_red(result: dict[str, Any]) -> tuple[bool, str]:
    """A red run counts only if the command actually ran and failed."""
    if result.get("timed_out"):
        return False, "the command timed out, which proves nothing"
    code = result.get("exit")
    if code == 0:
        return False, "the command passes on the base code, so it does not capture the issue"
    if code in (126, 127):
        return False, f"the command could not be executed (exit {code}); fix the command"
    return True, ""


def _can_execute_in(folder: Path) -> bool:
    """False where the folder is mounted noexec, which would break compiled test runs."""
    if os.name != "posix":
        return True
    probe = folder / "exec-probe.sh"
    try:
        probe.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        probe.chmod(0o700)
        return subprocess.run([str(probe)], capture_output=True).returncode == 0
    except OSError:
        return False
    finally:
        with contextlib.suppress(OSError):
            probe.unlink()


def proof_parent(sd: Path, label: str) -> Path:
    """A private folder for one throwaway checkout, outside the repository.

    A checkout nested inside the repository is not clean: tools that look upward
    (Node's module resolution, config discovery) would find the parent's
    node_modules and settings. So proofs go to the system temp folder, or to
    CREW_PROOF_DIR when set. If neither can run programs, fall back to .crew/proof.
    """
    for base in (os.environ.get("CREW_PROOF_DIR"), tempfile.gettempdir()):
        if not base:
            continue
        try:
            Path(base).mkdir(parents=True, exist_ok=True)
            folder = Path(tempfile.mkdtemp(prefix=f"crew-{label}-", dir=base))
        except OSError:
            continue
        if _can_execute_in(folder):
            return folder
        shutil.rmtree(folder, ignore_errors=True)
    folder = sd / "proof" / f"{label}-{uuid.uuid4().hex[:8]}"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


@contextlib.contextmanager
def proof_tree(root: Path, sd: Path, sha: str, label: str) -> Iterator[Path]:
    """A throwaway clean checkout of `sha`: nothing from any working tree leaks in."""
    parent = proof_parent(sd, label)
    path = parent / "checkout"
    for attempt in range(3):
        if _git_ok(["worktree", "add", "--detach", "--force", str(path), sha], root):
            break
        time.sleep(0.5 * (attempt + 1))  # parallel proofs can collide on git's worktree lock
    else:
        shutil.rmtree(parent, ignore_errors=True)
        _git(["worktree", "add", "--detach", "--force", str(path), sha], root)
    try:
        yield path
    finally:
        if not _git_ok(["worktree", "remove", "--force", str(path)], root):
            shutil.rmtree(path, ignore_errors=True)
        shutil.rmtree(parent, ignore_errors=True)
        _git(["worktree", "prune"], root, check=False)


def run_setup(rules: dict[str, Any], tree: Path, log: Path) -> dict[str, Any] | None:
    setup = str((rules.get("commands") or {}).get("setup") or "").strip()
    if not setup:
        return None
    result = run_cmd(setup, tree, log, rules["limits"]["command_timeout_s"], rules.get("env"))
    if not passed(result):
        raise CrewError(
            f"setup command failed (exit {result['exit']}): {setup}\n{result['tail']}\nlog: {log}"
        )
    return result


# ------------------------------------------------------------------ context/init

class Ctx:
    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or find_root()).resolve()
        self.sd = self.root / STATE_DIR

    @property
    def rules(self) -> dict[str, Any]:
        return load_rules(self.sd)

    @property
    def state(self) -> dict[str, Any]:
        return load_state(self.sd)

    def evidence(self, issue_id: str) -> Path:
        return self.sd / "evidence" / issue_id

    def rel(self, path: Path | str) -> str:
        try:
            return Path(path).resolve().relative_to(self.root).as_posix()
        except ValueError:
            return str(path)


def _exclude_locally(root: Path, entries: list[str]) -> list[str]:
    """Hide crew's own directories from git without touching the repo's .gitignore."""
    common = Path(_git(["rev-parse", "--git-common-dir"], root).strip())
    if not common.is_absolute():
        common = root / common
    exclude = common / "info" / "exclude"
    existing = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
    added = []
    for entry in entries:
        probe = entry.rstrip("/") + "/probe"
        if entry in existing.splitlines() or _git_ok(["check-ignore", "-q", probe], root):
            continue
        added.append(entry)
    if added:
        exclude.parent.mkdir(parents=True, exist_ok=True)
        sep = "" if existing.endswith("\n") or not existing else "\n"
        exclude.write_text(existing + sep + "\n".join(added) + "\n", encoding="utf-8")
    return added


def cmd_init(args: argparse.Namespace) -> int:
    root = find_root().resolve()
    sd = root / STATE_DIR
    if not _git_ok(["rev-parse", "--verify", "--quiet", "HEAD"], root):
        raise CrewError("this repository has no commits yet. The crew proves every change against a "
                        "base commit, so commit the starting point first.")
    for sub in ("evidence", "issues", "bin"):
        (sd / sub).mkdir(parents=True, exist_ok=True)
    excluded = _exclude_locally(root, [STATE_DIR + "/", "graphify-out/"])
    rules_path = sd / "rules.json"
    if not rules_path.exists():
        write_atomic(rules_path, json.dumps(RULES_TEMPLATE, indent=2) + "\n")
    script = Path(__file__).resolve()
    wrapper = sd / "bin" / "crew"
    write_atomic(wrapper, f"#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(script))} \"$@\"\n")
    with contextlib.suppress(OSError):
        wrapper.chmod(0o755)
    write_atomic(sd / "bin" / "crew.cmd", f'@"{sys.executable}" "{script}" %*\r\n')
    if not (sd / "state.json").exists():
        head = _git(["rev-parse", "HEAD"], root).strip()
        state = {
            "version": 1,
            "run": {
                "id": dt.datetime.now().strftime("%Y%m%d-%H%M"),
                "status": "active",
                "started_at": now(),
                "base_sha": head,
                "mode": "pr-per-issue",
            },
            "issues": {},
        }
        write_atomic(sd / "state.json", json.dumps(state, indent=2, sort_keys=True) + "\n")
        write_atomic(sd / "TASKS.md", render_tasks(state))
    print(f"crew state: {sd}")
    print(f"crew command: {wrapper}")
    if excluded:
        print("hidden from git via .git/info/exclude: " + ", ".join(excluded))
    print(f"Team Rules: {rules_path} (fill in after intake, then `crew rules`)")
    return 0


def cmd_rules(args: argparse.Namespace) -> int:
    ctx = Ctx()
    rules = ctx.rules
    problems = rules_problems(rules)
    delivery = rules["delivery"]

    def apply(state: dict[str, Any]) -> None:
        run = state["run"]
        run["mode"] = delivery["mode"]
        if delivery.get("base"):
            run["base_sha"] = rev(ctx.root, delivery["base"])
            run["base_ref"] = delivery["base"]

    if not problems:
        mutate(ctx.sd, apply)
    tracked = _git(["ls-files"], ctx.root).splitlines()
    fenced = [p for p in tracked if protected_hit(p, rules)]
    cmds = rules["commands"]
    print("TEAM RULES")
    print(f"  done:        {rules['done'] or '(missing)'}")
    print(f"  setup:       {cmds.get('setup') or '(none)'}")
    print(f"  full suite:  {cmds.get('suite') or '(missing)'}")
    for name, cmd in (cmds.get("checks") or {}).items():
        print(f"  check {name}: {cmd}")
    budget = rules["budget"]
    print(f"  budget:      hours={budget.get('hours')} usd={budget.get('usd')} deadline={budget.get('deadline') or '-'}")
    limits = rules["limits"]
    print(f"  limits:      max_builders={limits['max_builders']} stuck_minutes={limits['stuck_minutes']} "
          f"command_timeout_s={limits['command_timeout_s']}")
    print(f"  delivery:    {delivery['mode']} base={delivery.get('base') or 'HEAD at init'} "
          f"branch={delivery.get('branch') or '-'}")
    print(f"  never touch: {len(DEFAULT_PROTECTED)} built-in globs + {rules.get('never_touch') or []}")
    print(f"  allowed:     {rules.get('allow_touch') or []}")
    print(f"  fenced now:  {len(fenced)} tracked file(s)" + (": " + ", ".join(fenced[:8]) if fenced else ""))
    if len(fenced) > 8:
        print(f"               ... and {len(fenced) - 8} more")
    if problems:
        print("\nINVALID:")
        for problem in problems:
            print("  - " + problem)
        return 1
    print("\nrules valid")
    return 0


def cmd_baseline(args: argparse.Namespace) -> int:
    """Prove the Team Rules commands work on the untouched base before any work starts."""
    ctx = Ctx()
    rules = ctx.rules
    problems = rules_problems(rules)
    if problems:
        raise CrewError("Team Rules are invalid: " + "; ".join(problems))
    base = ctx.state["run"]["base_sha"]
    ev = ctx.evidence("_baseline")
    results: dict[str, Any] = {"base": base, "at": now(), "checks": {}}
    with proof_tree(ctx.root, ctx.sd, base, "baseline") as tree:
        setup = run_setup(rules, tree, ev / "setup.log")
        if setup:
            results["setup"] = setup
        timeout = rules["limits"]["command_timeout_s"]
        results["suite"] = run_cmd(rules["commands"]["suite"], tree, ev / "suite.log", timeout, rules.get("env"))
        for name, cmd in (rules["commands"].get("checks") or {}).items():
            results["checks"][name] = run_cmd(cmd, tree, ev / f"check-{name}.log", timeout, rules.get("env"))
    red = [] if passed(results["suite"]) else ["suite"]
    red += [name for name, res in results["checks"].items() if not passed(res)]
    results["green"] = not red
    write_atomic(ev / "baseline.json", json.dumps(results, indent=2) + "\n")

    def apply(state: dict[str, Any]) -> None:
        state["run"]["baseline"] = {"green": not red, "red": red, "at": results["at"], "sha": base}
        if red:
            state["run"]["halt"] = "baseline is red on the untouched base: " + ", ".join(red)
        else:
            state["run"].pop("halt", None)

    mutate(ctx.sd, apply)
    log_lines(ctx.sd, [f"baseline {'green' if not red else 'RED: ' + ', '.join(red)} at {base[:10]}"])
    print(f"BASELINE at {base[:10]} (clean checkout)")
    print(f"  suite: exit {results['suite']['exit']} in {results['suite']['seconds']}s")
    for name, res in results["checks"].items():
        print(f"  check {name}: exit {res['exit']} in {res['seconds']}s")
    if red:
        print("\nRED: " + ", ".join(red))
        for name in red:
            res = results["suite"] if name == "suite" else results["checks"][name]
            print(f"--- {name} (last lines) ---\n{res['tail']}")
        print("\nNo issue can be proven on a red base. Fix this first or correct the commands.")
        return 1
    print("\nbaseline green")
    return 0


# ---------------------------------------------------------------------- issues

def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:40].strip("-") or "issue"


def cmd_add(args: argparse.Namespace) -> int:
    ctx = Ctx()
    if not ID_RE.match(args.id):
        raise CrewError("issue id must match [A-Za-z0-9][A-Za-z0-9._-]{0,63}")
    spec = ""
    if args.spec_file:
        spec = Path(args.spec_file).read_text(encoding="utf-8")
    elif args.spec:
        spec = args.spec
    if not spec.strip():
        raise CrewError("an issue needs a spec: pass --spec-file (preferred) or --spec")
    spec_path = ctx.sd / "issues" / f"{args.id}.md"

    def apply(state: dict[str, Any]) -> None:
        if args.id in state["issues"]:
            raise CrewError(f"issue {args.id} already exists")
        if args.on and args.on not in state["issues"]:
            raise CrewError(f"--on {args.on}: no such issue")
        issue = {
            "id": args.id,
            "title": args.title,
            "source": args.source or "",
            "spec": ctx.rel(spec_path),
            "status": "queued",
            "owns": [norm_rel(o) for o in (args.owns or [])],
            "on": args.on or "",
            "fixes_baseline": bool(args.fixes_baseline),
            "rejections": [],
            "waivers": [],
            "history": [],
        }
        note(issue, "added")
        state["issues"][args.id] = issue

    write_atomic(spec_path, spec if spec.endswith("\n") else spec + "\n")
    mutate(ctx.sd, apply)
    print(f"added issue {args.id}: {args.title} (spec: {ctx.rel(spec_path)})")
    return 0


def cmd_start(args: argparse.Namespace) -> int:
    ctx = Ctx()
    state = ctx.state
    issue = issue_of(state, args.id)
    if issue["status"] != "queued":
        print(f"issue {args.id} already started: worktree {issue.get('worktree')} branch {issue.get('branch')}")
        return 0
    rules = ctx.rules
    if state["run"].get("halt") and not issue.get("fixes_baseline"):
        raise CrewError("HALT: " + state["run"]["halt"] + ". Only an issue added with --fixes-baseline may start.")
    if issue.get("on") and rules["delivery"]["mode"] == "pr-per-issue":
        dep = issue_of(state, issue["on"])
        if dep["status"] not in ("approved", "pr"):
            raise CrewError(f"issue {args.id} builds on issue {issue['on']}, which is {dep['status']}. "
                            "Start it once that issue is approved, so it is cut from finished work.")
        base = rev(ctx.root, dep["branch"])
    elif rules["delivery"]["mode"] == "single-branch":
        base = rev(ctx.root, rules["delivery"]["branch"])
    else:
        base = state["run"]["base_sha"]
    branch = f"crew/{args.id}-{_slug(issue['title'])}"
    worktree = ctx.sd / "wt" / args.id
    worktree.parent.mkdir(parents=True, exist_ok=True)
    if _git_ok(["rev-parse", "--verify", "--quiet", "refs/heads/" + branch], ctx.root):
        _git(["worktree", "add", str(worktree), branch], ctx.root)
    else:
        _git(["worktree", "add", "-b", branch, str(worktree), base], ctx.root)

    def apply(st: dict[str, Any]) -> None:
        it = issue_of(st, args.id)
        it.update({"status": "started", "branch": branch, "worktree": ctx.rel(worktree), "base": base})
        note(it, "started", f"branch {branch} from {base[:10]}")

    mutate(ctx.sd, apply)
    print(f"issue {args.id}: branch {branch} from {base[:10]}")
    print(f"worktree: {worktree}")
    return 0


def _issue_head(ctx: Ctx, issue: dict[str, Any]) -> str:
    if not issue.get("branch"):
        raise CrewError(f"issue {issue['id']} has not been started")
    return rev(ctx.root, issue["branch"])


def _dirty(ctx: Ctx, issue: dict[str, Any]) -> list[str]:
    worktree = ctx.root / issue["worktree"]
    if not worktree.exists():
        return []
    out = _git(["status", "--porcelain"], worktree)
    return [line[3:] for line in out.splitlines() if line.strip()]


def cmd_seal(args: argparse.Namespace) -> int:
    """Record the proving test and confirm it fails on the base code."""
    ctx = Ctx()
    rules = ctx.rules
    issue = issue_of(ctx.state, args.id)
    if issue["status"] not in ("started", "red"):
        raise CrewError(f"issue {args.id} is {issue['status']}; seal applies to started or red issues")
    command = (args.cmd or "").strip()
    if not command:
        raise CrewError("--cmd is required: the exact command that runs the proving test")
    files = sorted({norm_rel(f) for f in (args.test_file or [])})
    head = _issue_head(ctx, issue)
    base = issue["base"]
    dirty = _dirty(ctx, issue)
    if dirty:
        raise CrewError("commit the proving test first; uncommitted in the worktree: " + ", ".join(dirty[:10]))
    for path in files:
        if not _git_ok(["cat-file", "-e", f"{head}:{path}"], ctx.root):
            raise CrewError(f"proving file {path} does not exist on {issue['branch']}")
        fence = protected_hit(path, rules)
        if fence:
            raise CrewError(f"proving file {path} is fenced by never-touch pattern {fence!r}")
    changed = {c["path"] for c in changed_files(ctx.root, base, head)}
    unchanged = [f for f in files if f not in changed]
    if unchanged:
        raise CrewError("declared proving files are identical to the base: " + ", ".join(unchanged))
    others = sorted(changed - set(files))
    ev = ctx.evidence(args.id)
    with proof_tree(ctx.root, ctx.sd, base, f"red-{args.id}") as tree:
        if files:
            _git(["checkout", head, "--", *files], tree)
        run_setup(rules, tree, ev / "red-setup.log")
        red = run_cmd(command, tree, ev / "red.log", rules["limits"]["command_timeout_s"], rules.get("env"))
    ok, why = valid_red(red)
    blobs = {p: _git(["rev-parse", f"{head}:{p}"], ctx.root).strip() for p in files}

    def apply(state: dict[str, Any]) -> None:
        it = issue_of(state, args.id)
        if ok:
            it["tests"] = {"files": blobs, "command": command, "sealed_at": now(), "base": base, "red": red}
            it["status"] = "red"
            it.pop("proof", None)
            it.pop("review", None)
            note(it, "sealed", f"red exit {red['exit']} on {base[:10]} + {len(files)} proving file(s)")
        else:
            reject(it, "seal", why, head)
            note(it, "seal-rejected", why)

    mutate(ctx.sd, apply)
    print(f"SEAL {args.id}: `{command}` on base {base[:10]} + {len(files)} proving file(s)")
    print(f"  exit {red['exit']} in {red['seconds']}s{' (timed out)' if red['timed_out'] else ''}")
    if others:
        print("  note: other changes on the branch were left out of the red run: " + ", ".join(others[:8]))
    print(f"--- last lines ({ctx.rel(red['log'])}) ---\n{red['tail']}")
    if not ok:
        print(f"\nREJECTED: {why}")
        return 1
    print("\nRED CONFIRMED. Check the failure above is the issue's failure, not a typo or import error.")
    return 0


def cmd_unseal(args: argparse.Namespace) -> int:
    """Reopen the proving test for revision. Sealed files are immutable until this runs."""
    ctx = Ctx()

    def apply(state: dict[str, Any]) -> None:
        issue = issue_of(state, args.id)
        if not issue.get("tests"):
            raise CrewError(f"issue {args.id} has no sealed test")
        issue.pop("tests")
        issue.pop("proof", None)
        issue.pop("review", None)
        issue["status"] = "started"
        note(issue, "unsealed", args.reason)

    mutate(ctx.sd, apply)
    print(f"issue {args.id} unsealed ({args.reason}); the Breaker revises the test, then seal again")
    return 0


# ------------------------------------------------------------------------ gates

def static_gates(ctx: Ctx, rules: dict[str, Any], issue: dict[str, Any], head: str) -> list[dict[str, Any]]:
    """Checks that need no test run: what changed, and whether it was allowed to."""
    base = issue["base"]
    tests = issue.get("tests") or {}
    sealed: dict[str, str] = tests.get("files") or {}
    waived = {w["gate"] for w in issue.get("waivers", [])}
    changed = changed_files(ctx.root, base, head)
    gates: list[dict[str, Any]] = []

    def gate(name: str, ok: bool, detail: str, warn: bool = False) -> None:
        status = "pass" if ok else ("waived" if name in waived else ("warn" if warn else "fail"))
        gates.append({"gate": name, "status": status, "detail": detail})

    dirty = _dirty(ctx, issue)
    gate("committed", not dirty, "worktree clean" if not dirty else "uncommitted or untracked: " + ", ".join(dirty[:10]))

    moved = []
    for path, blob in sealed.items():
        current = _git(["rev-parse", "--verify", "--quiet", f"{head}:{path}"], ctx.root, check=False).strip()
        if current != blob:
            moved.append(path)
    gate("sealed-tests", not moved,
         f"{len(sealed)} proving file(s) unchanged since seal" if not moved
         else "proving files changed after seal (re-seal through the Breaker): " + ", ".join(moved))

    fenced = [f"{c['path']} ({protected_hit(c['path'], rules)})" for c in changed if protected_hit(c["path"], rules)]
    fenced += [f"{c['old']} ({protected_hit(c['old'], rules)})" for c in changed if c["old"] and protected_hit(c["old"], rules)]
    gate("never-touch", not fenced, "no fenced path touched" if not fenced else "fenced paths touched: " + ", ".join(fenced))

    owns = issue.get("owns") or []
    if owns:
        outside = [
            c["path"] for c in changed
            if c["path"] not in sealed and not is_test_path(c["path"]) and not matches_any(c["path"], owns)
        ]
        gate("lane", not outside, f"all changes inside the lane {owns}" if not outside
             else "changes outside the lane (ask the Lead to widen it, or revert): " + ", ".join(outside))
    else:
        gate("lane", True, "no lane set; scope is checked by the Reviewer")

    # Who may change what a test expects: the Breaker, in the sealed files, where
    # every rewritten assertion is flagged for the Reviewer. A Builder may add
    # tests but may not remove or rewrite an existing assertion to get green.
    deleted = [c["path"] for c in changed if c["status"] == "D" and is_test_path(c["path"])]
    skips: list[str] = []
    removed_tests: list[str] = []
    sealed_asserts: list[str] = []
    rewritten: list[str] = []
    for c in changed:
        path = c["path"]
        if c["status"] == "D" or not is_test_path(path):
            continue
        added, removed = diff_lines(ctx.root, base, head, path)
        skips += [f"{path}: {line.strip()[:80]}" for line in added if _SKIP_RE.search(line)]
        if c["status"] == "A":
            continue
        lost = sum(1 for line in removed if _TESTDEF_RE.search(line)) - sum(1 for line in added if _TESTDEF_RE.search(line))
        if lost > 0:
            removed_tests.append(f"{path} (-{lost})")
        gone = sum(1 for line in removed if _ASSERT_RE.search(line))
        if gone:
            (sealed_asserts if path in sealed else rewritten).append(f"{path} ({gone})")
    gate("tests-deleted", not deleted, "no test file deleted" if not deleted else "test files deleted: " + ", ".join(deleted))
    gate("tests-skipped", not skips, "no skip/only/xfail marker added" if not skips else "tests disabled or narrowed: " + "; ".join(skips[:6]))
    gate("tests-removed", not removed_tests, "no test case removed" if not removed_tests else "test cases removed: " + ", ".join(removed_tests))
    gate("tests-rewritten", not rewritten, "no existing assertion changed outside the proving files" if not rewritten
         else "existing assertions removed or rewritten outside the sealed proving files: " + ", ".join(rewritten)
         + ". Only the Breaker changes what tests expect: hand back, unseal, and let the Breaker rewrite them")
    gate("assertions", not sealed_asserts, "no existing assertion rewritten in the proving files" if not sealed_asserts
         else "existing assertions removed or rewritten in the proving files (the Reviewer must tie each to the issue's own words): "
         + ", ".join(sealed_asserts), warn=True)

    configs = [c["path"] for c in changed if matches_any(c["path"], TEST_CONFIGS)]
    gate("test-config", not configs, "test configuration untouched" if not configs
         else "test configuration changed (Reviewer must justify): " + ", ".join(configs), warn=True)
    deps = [c["path"] for c in changed if matches_any(c["path"], DEP_MANIFESTS)]
    gate("dependencies", not deps, "no dependency manifest changed" if not deps
         else "dependency manifests changed (the PR must give the reason): " + ", ".join(deps), warn=True)

    real = [c["path"] for c in changed if c["path"] not in sealed]
    gate("has-change", bool(real) or not sealed, f"{len(real)} file(s) changed besides the proving test" if real
         else "nothing changed besides the proving test")
    return gates


def print_gates(gates: list[dict[str, Any]]) -> None:
    for item in gates:
        print(f"  [{item['status'].upper():6}] {item['gate']}: {item['detail']}")


def cmd_check(args: argparse.Namespace) -> int:
    """Static gates only: fast enough for a builder to run before every handoff."""
    ctx = Ctx()
    issue = issue_of(ctx.state, args.id)
    head = _issue_head(ctx, issue)
    gates = static_gates(ctx, ctx.rules, issue, head)
    print(f"CHECK {args.id} at {head[:10]} (static gates; the Prover runs the full proof)")
    print_gates(gates)
    return 1 if any(g["status"] == "fail" for g in gates) else 0


def cmd_prove(args: argparse.Namespace) -> int:
    """Re-run every claim on a clean checkout. The result is final for this head."""
    ctx = Ctx()
    rules = ctx.rules
    issue = issue_of(ctx.state, args.id)
    if issue["status"] not in ("red", "proven", "approved"):
        raise CrewError(f"issue {args.id} is {issue['status']}; seal a failing test before proving")
    tests = issue["tests"]
    head = _issue_head(ctx, issue)
    base = issue["base"]
    ev = ctx.evidence(args.id)
    tag = f"proof-{head[:10]}"
    timeout = rules["limits"]["command_timeout_s"]
    gates = static_gates(ctx, rules, issue, head)
    runs: dict[str, Any] = {"checks": {}}

    if args.recheck_red:
        with proof_tree(ctx.root, ctx.sd, base, f"red-{args.id}") as tree:
            if tests["files"]:
                _git(["checkout", head, "--", *sorted(tests["files"])], tree)
            run_setup(rules, tree, ev / f"{tag}-red-setup.log")
            runs["red"] = run_cmd(tests["command"], tree, ev / f"{tag}-red.log", timeout, rules.get("env"))
        ok, why = valid_red(runs["red"])
        gates.append({"gate": "red-on-base", "status": "pass" if ok else "fail",
                      "detail": f"exit {runs['red']['exit']} on the base" if ok else why})
    else:
        red = tests["red"]
        gates.append({"gate": "red-on-base", "status": "pass",
                      "detail": f"exit {red['exit']} on {tests['base'][:10]} at seal ({ctx.rel(red['log'])})"})

    with proof_tree(ctx.root, ctx.sd, head, f"green-{args.id}") as tree:
        setup = run_setup(rules, tree, ev / f"{tag}-setup.log")
        if setup:
            runs["setup"] = setup
        runs["green"] = run_cmd(tests["command"], tree, ev / f"{tag}-green.log", timeout, rules.get("env"))
        runs["suite"] = run_cmd(rules["commands"]["suite"], tree, ev / f"{tag}-suite.log", timeout, rules.get("env"))
        for name, cmd in (rules["commands"].get("checks") or {}).items():
            runs["checks"][name] = run_cmd(cmd, tree, ev / f"{tag}-check-{name}.log", timeout, rules.get("env"))

    def run_gate(name: str, result: dict[str, Any]) -> None:
        detail = f"exit {result['exit']} in {result['seconds']}s" + (" (timed out)" if result["timed_out"] else "")
        gates.append({"gate": name, "status": "pass" if passed(result) else "fail", "detail": detail})

    run_gate("green-on-head", runs["green"])
    try:
        green_text = Path(runs["green"]["log"]).read_text(encoding="utf-8", errors="replace")
    except OSError:
        green_text = ""
    empty = _NO_TESTS_RE.search(green_text)
    gates.append({"gate": "tests-ran", "status": "fail" if empty else "pass",
                  "detail": f"the proving command reported {empty.group(0)!r}: it ran no test" if empty
                  else "no zero-test marker in the proving output"})
    run_gate("full-suite", runs["suite"])
    for name, result in runs["checks"].items():
        run_gate("check:" + name, result)

    failed = [g for g in gates if g["status"] == "fail"]
    verdict = "pass" if not failed else "fail"
    proof = {
        "issue": args.id, "verdict": verdict, "at": now(), "base": base, "head": head,
        "command": tests["command"], "gates": gates, "runs": runs,
        "files": changed_files(ctx.root, base, head),
    }
    proof_path = ev / f"{tag}.json"
    write_atomic(proof_path, json.dumps(proof, indent=2) + "\n")

    def apply(state: dict[str, Any]) -> None:
        it = issue_of(state, args.id)
        it["proof"] = {"verdict": verdict, "head": head, "at": proof["at"], "file": ctx.rel(proof_path)}
        it.pop("review", None)
        if verdict == "pass":
            it["status"] = "proven"
            resolve_rejections(it, f"re-proven at {head[:10]}")
            note(it, "proven", head[:10])
            if it.get("fixes_baseline") and state["run"].get("mode") != "single-branch":
                state["run"].pop("halt", None)
                state["run"]["base_sha"] = head
                note(it, "baseline-fixed", f"later issues are cut from {head[:10]}")
        else:
            it["status"] = "red"
            reason = "; ".join(f"{g['gate']}: {g['detail']}" for g in failed)
            reject(it, "prover", reason, head)
            note(it, "proof-rejected", reason)

    mutate(ctx.sd, apply)
    print(f"PROVE {args.id} at {head[:10]} on a clean checkout (base {base[:10]})")
    print_gates(gates)
    for name in ("green", "suite"):
        if not passed(runs[name]):
            print(f"--- {name} last lines ({ctx.rel(runs[name]['log'])}) ---\n{runs[name]['tail']}")
    for name, result in runs["checks"].items():
        if not passed(result):
            print(f"--- check {name} last lines ({ctx.rel(result['log'])}) ---\n{result['tail']}")
    print(f"\nevidence: {ctx.rel(proof_path)}")
    print("VERDICT: " + ("PROVEN" if verdict == "pass" else "REJECTED (final for this head; fix and re-prove)"))
    return 0 if verdict == "pass" else 1


def _load_proof(ctx: Ctx, issue: dict[str, Any]) -> dict[str, Any]:
    proof = issue.get("proof")
    if not proof:
        raise CrewError(f"issue {issue['id']} has no proof")
    data = read_json(ctx.root / proof["file"])
    if data is None:
        raise CrewError(f"evidence file missing: {proof['file']}")
    return data


def _require_current_proof(ctx: Ctx, issue: dict[str, Any]) -> str:
    head = _issue_head(ctx, issue)
    proof = issue.get("proof") or {}
    if proof.get("verdict") != "pass" or proof.get("head") != head:
        raise CrewError(
            f"issue {issue['id']}: the branch head {head[:10]} has no passing proof "
            f"(last proof: {proof.get('verdict', 'none')} at {str(proof.get('head', ''))[:10]}); run prove"
        )
    return head


def cmd_review(args: argparse.Namespace) -> int:
    ctx = Ctx()
    issue = issue_of(ctx.state, args.id)
    if issue["status"] not in ("proven", "approved"):
        raise CrewError(f"issue {args.id} is {issue['status']}; only a proven issue can be reviewed")
    head = _require_current_proof(ctx, issue)
    data = read_json(Path(args.file))
    if not isinstance(data, dict):
        raise CrewError(f"{args.file} must contain a JSON object")
    verdict = data.get("verdict")
    if verdict not in ("approve", "reject"):
        raise CrewError('review "verdict" must be "approve" or "reject"')
    findings = [str(f) for f in data.get("findings") or []]
    if verdict == "approve":
        missing = [k for k in ("summary", "risk", "rollback", "why_break") if not str(data.get(k) or "").strip()]
        if missing:
            raise CrewError("an approval needs: " + ", ".join(missing))
        if data["risk"] not in ("low", "medium", "high"):
            raise CrewError('"risk" must be low, medium or high')
        proof = _load_proof(ctx, issue)
        warned = {g["gate"]: g["detail"] for g in proof["gates"] if g["status"] == "warn"}
        needs = {"dependencies": "deps_reason", "assertions": "assertions_reason", "test-config": "test_config_reason"}
        unexplained = [field for gate, field in needs.items() if gate in warned and not str(data.get(field) or "").strip()]
        if unexplained:
            raise CrewError("the proof raised warnings that the review must explain: " + ", ".join(unexplained))
    elif not findings:
        raise CrewError("a rejection needs at least one finding")

    def apply(state: dict[str, Any]) -> None:
        it = issue_of(state, args.id)
        it["review"] = {
            "verdict": verdict, "head": head, "at": now(), "findings": findings,
            **{k: str(data.get(k) or "") for k in (
                "summary", "risk", "risk_reason", "rollback", "why_break", "deps_reason",
                "assertions_reason", "test_config_reason")},
        }
        if verdict == "approve":
            it["status"] = "approved"
            note(it, "approved", head[:10])
        else:
            it["status"] = "red"
            reject(it, "reviewer", "; ".join(findings), head)
            note(it, "review-rejected", "; ".join(findings))

    mutate(ctx.sd, apply)
    print(f"REVIEW {args.id} at {head[:10]}: {verdict.upper()}")
    for finding in findings:
        print("  - " + finding)
    return 0 if verdict == "approve" else 1


def cmd_reject(args: argparse.Namespace) -> int:
    """Send an issue back to the builder for a reason the gates cannot see."""
    ctx = Ctx()
    reason = Path(args.reason_file).read_text(encoding="utf-8").strip() if args.reason_file else (args.reason or "").strip()
    if not reason:
        raise CrewError("reject needs a reason: --reason-file or --reason")

    def apply(state: dict[str, Any]) -> None:
        issue = issue_of(state, args.id)
        if issue["status"] not in ("red", "proven", "approved"):
            raise CrewError(f"issue {args.id} is {issue['status']}; there is nothing to reject yet")
        head = (issue.get("proof") or {}).get("head", "")
        issue["status"] = "red"
        issue.pop("review", None)
        if issue.get("proof"):
            issue["proof"]["verdict"] = "rejected"
        reject(issue, args.by, reason, head)
        note(issue, "rejected", f"{args.by}: {reason}")

    mutate(ctx.sd, apply)
    print(f"issue {args.id} rejected by {args.by}: {reason}")
    return 0


def cmd_land(args: argparse.Namespace) -> int:
    """Single-branch delivery: integrate on a scratch checkout, re-prove, then fast-forward."""
    ctx = Ctx()
    rules = ctx.rules
    if rules["delivery"]["mode"] != "single-branch":
        raise CrewError("land applies to single-branch delivery; in pr-per-issue mode push the issue branch")
    state = ctx.state
    issue = issue_of(state, args.id)
    if issue["status"] != "approved":
        raise CrewError(f"issue {args.id} is {issue['status']}; only an approved issue can land")
    head = _require_current_proof(ctx, issue)
    if (issue.get("review") or {}).get("head") != head:
        raise CrewError("the approval is for an older head; review again")
    branch = rules["delivery"]["branch"]
    current = _git(["rev-parse", "--abbrev-ref", "HEAD"], ctx.root).strip()
    if current != branch:
        raise CrewError(f"the main checkout is on {current}, not the delivery branch {branch}")
    if _git(["status", "--porcelain", "--untracked-files=no"], ctx.root).strip():
        raise CrewError("the main checkout has uncommitted changes; commit or stash them first")
    target = rev(ctx.root, branch)
    landed = [i for i in state["issues"].values() if i["status"] in ("landed", "pr") and i.get("tests")]
    ev = ctx.evidence(args.id)
    tag = f"land-{head[:10]}"
    timeout = rules["limits"]["command_timeout_s"]
    runs: dict[str, Any] = {"proving": {}, "checks": {}}
    new_sha = ""
    conflict = ""
    with proof_tree(ctx.root, ctx.sd, target, f"land-{args.id}") as tree:
        ident = [] if _git_ok(["config", "user.email"], tree) else [
            "-c", "user.name=crew", "-c", "user.email=crew@localhost"]
        pick = subprocess.run(
            ["git", *ident, "cherry-pick", "--allow-empty", "--keep-redundant-commits",
             f"{issue['base']}..{head}"],
            cwd=str(tree), capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if pick.returncode != 0:
            conflict = (pick.stdout + pick.stderr).strip()[-1500:]
            _git(["cherry-pick", "--abort"], tree, check=False)
        else:
            new_sha = _git(["rev-parse", "HEAD"], tree).strip()
            run_setup(rules, tree, ev / f"{tag}-setup.log")
            for other in [issue, *landed]:
                runs["proving"][other["id"]] = run_cmd(
                    other["tests"]["command"], tree, ev / f"{tag}-proving-{other['id']}.log", timeout, rules.get("env"))
            runs["suite"] = run_cmd(rules["commands"]["suite"], tree, ev / f"{tag}-suite.log", timeout, rules.get("env"))
            for name, cmd in (rules["commands"].get("checks") or {}).items():
                runs["checks"][name] = run_cmd(cmd, tree, ev / f"{tag}-check-{name}.log", timeout, rules.get("env"))
    failed: list[str] = []
    if conflict:
        failed.append("cherry-pick conflict onto " + branch)
    else:
        failed += [f"proving test of {k}" for k, r in runs["proving"].items() if not passed(r)]
        if not passed(runs["suite"]):
            failed.append("full suite")
        failed += [f"check {k}" for k, r in runs["checks"].items() if not passed(r)]
    if not failed:
        _git(["merge", "--ff-only", new_sha], ctx.root)
    write_atomic(ev / f"{tag}.json", json.dumps(
        {"issue": args.id, "target": target, "head": head, "landed_sha": new_sha if not failed else "",
         "failed": failed, "conflict": conflict, "runs": runs, "at": now()}, indent=2) + "\n")

    def apply(st: dict[str, Any]) -> None:
        it = issue_of(st, args.id)
        if failed:
            it["status"] = "red"
            reason = "integration onto " + branch + " failed: " + ", ".join(failed)
            reject(it, "land", reason, head)
            note(it, "land-rejected", reason)
        else:
            it["status"] = "landed"
            it["landed"] = {"sha": new_sha, "at": now(), "onto": target, "file": ctx.rel(ev / f"{tag}.json")}
            note(it, "landed", new_sha[:10])
            if it.get("fixes_baseline"):
                st["run"].pop("halt", None)
                note(it, "baseline-fixed", f"{branch} is green at {new_sha[:10]}")

    mutate(ctx.sd, apply)
    print(f"LAND {args.id}: {head[:10]} onto {branch} ({target[:10]})")
    if failed:
        print("REJECTED: " + ", ".join(failed))
        if conflict:
            print("--- cherry-pick output ---\n" + conflict)
        for name, result in [*runs["proving"].items(), ("suite", runs.get("suite")), *runs["checks"].items()]:
            if result and not passed(result):
                print(f"--- {name} last lines ---\n{result['tail']}")
        print(f"{branch} was not moved. Rebase the issue branch onto {branch}, then prove and review again.")
        return 1
    print(f"integrated head {new_sha[:10]}: proving tests, full suite and checks green; {branch} fast-forwarded")
    return 0


# -------------------------------------------------------------------- reporting

def _fence(text: str) -> str:
    return "```text\n" + (text.strip() or "(no output)") + "\n```"


def pr_section(ctx: Ctx, rules: dict[str, Any], issue: dict[str, Any]) -> str:
    proof = _load_proof(ctx, issue)
    review = issue.get("review") or {}
    tests = issue["tests"]
    stats = numstat(ctx.root, proof["base"], proof["head"])
    files = []
    for item in proof["files"]:
        plus, minus = stats.get(item["path"], ("0", "0"))
        files.append(f"- `{item['path']}` ({item['status']}, +{plus} -{minus})")
    landed_line = []
    landed = issue.get("landed")
    if landed:
        shas = _git(["rev-list", "--reverse", "--abbrev-commit", f"{landed['onto']}..{landed['sha']}"], ctx.root).split()
        landed_line = [f"**Commits on {rules['delivery']['branch']}:** " + " ".join(f"`{sha}`" for sha in shas)
                       + " (revert these to roll back)"]
    stacked_line = []
    if issue.get("on") and not landed:
        dep = ctx.state["issues"].get(issue["on"], {})
        stacked_line = [f"**Stacked on:** issue {issue['on']} (`{dep.get('branch', '')}`). Merge that first; "
                        "this diff is shown against it."]
    lines = [
        f"## {review.get('summary') or issue['title']}",
        "",
        f"**Closes:** {issue.get('source') or 'issue ' + issue['id']}",
        f"**Proving test:** `{tests['command']}`"
        + (" (" + ", ".join(f"`{p}`" for p in sorted(tests["files"])) + ")" if tests["files"] else ""),
        f"**Risk:** {review.get('risk', 'unrated')}" + (f" ({review['risk_reason']})" if review.get("risk_reason") else ""),
        f"**Rollback:** {review.get('rollback', '')}",
        *landed_line,
        *stacked_line,
        "",
        f"### Output before (base {proof['base'][:10]} plus the proving test, exit {tests['red']['exit']})",
        _fence(tests["red"]["tail"]),
        f"### Output after ({proof['head'][:10]} on a clean checkout, exit {proof['runs']['green']['exit']})",
        _fence(proof["runs"]["green"]["tail"]),
        "### Verification",
        f"- Full suite `{rules['commands']['suite']}`: exit {proof['runs']['suite']['exit']} in {proof['runs']['suite']['seconds']}s",
    ]
    for name, result in proof["runs"]["checks"].items():
        lines.append(f"- Check `{name}` (`{result['cmd']}`): exit {result['exit']} in {result['seconds']}s")
    for gate in proof["gates"]:
        if gate["status"] in ("warn", "waived"):
            lines.append(f"- {gate['status'].upper()} {gate['gate']}: {gate['detail']}")
    lines += ["", "### Files touched", *files]
    for label, field in (("New dependencies", "deps_reason"), ("Assertion changes", "assertions_reason"),
                         ("Test configuration changes", "test_config_reason")):
        if review.get(field):
            lines += ["", f"### {label}", review[field]]
    if issue.get("waivers"):
        lines += ["", "### Waivers granted by the user"]
        lines += [f"- {w['gate']}: {w['reason']}" for w in issue["waivers"]]
    lines += ["", "### WHY THIS COULD BREAK", review.get("why_break") or "(not reviewed yet)"]
    return "\n".join(lines) + "\n"


def cmd_pr_body(args: argparse.Namespace) -> int:
    ctx = Ctx()
    state = ctx.state
    rules = ctx.rules
    if args.all:
        issues = [i for i in state["issues"].values() if i["status"] in ("landed", "pr")]
        if not issues:
            raise CrewError("no landed issues to describe")
    else:
        if not args.id:
            raise CrewError("give an issue id or --all")
        issue = issue_of(state, args.id)
        if issue["status"] not in ("approved", "landed", "pr"):
            raise CrewError(f"issue {args.id} is {issue['status']}; a PR needs an approved proof")
        issues = [issue]
    for issue in issues:
        if issue["status"] == "approved":
            _require_current_proof(ctx, issue)
    body = "\n---\n\n".join(pr_section(ctx, rules, issue) for issue in sorted(issues, key=lambda i: i["id"]))
    if args.out:
        write_atomic(Path(args.out), body)
        print(f"wrote {args.out}")
    else:
        print(body)
    return 0


def cmd_pr(args: argparse.Namespace) -> int:
    ctx = Ctx()

    def apply(state: dict[str, Any]) -> None:
        issue = issue_of(state, args.id)
        if issue["status"] not in ("approved", "landed", "pr"):
            raise CrewError(f"issue {args.id} is {issue['status']}; only approved or landed work gets a PR")
        issue["pr"] = {"url": args.url, "at": now()}
        issue["status"] = "pr"
        note(issue, "pr", args.url)

    mutate(ctx.sd, apply)
    print(f"issue {args.id}: PR recorded {args.url}")
    return 0


def cmd_park(args: argparse.Namespace) -> int:
    ctx = Ctx()
    text = Path(args.question_file).read_text(encoding="utf-8").strip() if args.question_file else (args.question or "").strip()
    if not text:
        raise CrewError("park needs exactly one question: --question-file or --question")

    def apply(state: dict[str, Any]) -> None:
        issue = issue_of(state, args.id)
        issue["parked"] = {"question": text, "at": now(), "from": issue["status"]}
        issue["status"] = "parked"
        note(issue, "parked", text)

    mutate(ctx.sd, apply)
    print(f"issue {args.id} parked with one question for the user")
    return 0


def cmd_unpark(args: argparse.Namespace) -> int:
    ctx = Ctx()

    def apply(state: dict[str, Any]) -> str:
        issue = issue_of(state, args.id)
        if issue["status"] != "parked":
            raise CrewError(f"issue {args.id} is not parked")
        back = issue["parked"].get("from", "queued")
        issue["parked"]["answer"] = args.answer or ""
        issue["status"] = back
        note(issue, "unparked", args.answer or "")
        return back

    back = mutate(ctx.sd, apply)
    print(f"issue {args.id} resumed at {back}")
    return 0


def cmd_block(args: argparse.Namespace) -> int:
    ctx = Ctx()

    def apply(state: dict[str, Any]) -> None:
        issue = issue_of(state, args.id)
        issue["blocked"] = {"reason": args.reason, "at": now(), "from": issue["status"]}
        issue["status"] = "blocked"
        note(issue, "blocked", args.reason)

    mutate(ctx.sd, apply)
    print(f"issue {args.id} blocked: {args.reason}")
    return 0


def cmd_waive(args: argparse.Namespace) -> int:
    """Record the user's explicit yes for one gate on one issue. Never self-granted."""
    ctx = Ctx()
    allowed = ("tests-deleted", "tests-skipped", "tests-removed", "tests-rewritten", "lane")
    if args.gate not in allowed:
        raise CrewError("only these gates can be waived, and only on the user's yes: " + ", ".join(allowed))

    def apply(state: dict[str, Any]) -> None:
        issue = issue_of(state, args.id)
        issue.setdefault("waivers", []).append({"gate": args.gate, "reason": args.reason, "at": now()})
        note(issue, "waived", f"{args.gate}: {args.reason}")

    mutate(ctx.sd, apply)
    print(f"issue {args.id}: gate {args.gate} waived ({args.reason}); it will be listed in the PR and the brief")
    return 0


def cmd_own(args: argparse.Namespace) -> int:
    ctx = Ctx()

    def apply(state: dict[str, Any]) -> list[str]:
        issue = issue_of(state, args.id)
        owns = [o for o in issue.get("owns", []) if o not in (args.remove or [])]
        owns += [norm_rel(o) for o in (args.add or []) if norm_rel(o) not in owns]
        issue["owns"] = owns
        note(issue, "lane", ", ".join(owns))
        return owns

    owns = mutate(ctx.sd, apply)
    print(f"issue {args.id} lane: {owns}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    ctx = Ctx()
    state = ctx.state
    write_atomic(ctx.sd / "TASKS.md", render_tasks(state))
    run = state["run"]
    mode = run.get("mode", "pr-per-issue")
    if args.json:
        print(json.dumps(state, indent=2, sort_keys=True))
        return 0
    print(f"RUN {run['id']} {run['status']} ({mode}, base {run['base_sha'][:10]}, started {run['started_at']})")
    if run.get("halt"):
        print("HALT: " + run["halt"])
    for issue_id in sorted(state["issues"]):
        issue = state["issues"][issue_id]
        print(f"  {issue_id:<10} {issue['status']:<9} wave {issue.get('wave') or '-'}  {issue['title']}")
        print(f"             next: {next_step(issue, mode)}")
        open_rej = [r for r in issue.get("rejections", []) if not r.get("resolved")]
        if open_rej:
            print(f"             open rejection ({open_rej[-1]['by']}): {open_rej[-1]['reason'][:200]}")
    if not state["issues"]:
        print("  (no issues yet)")
    return 0


def remove_finished_worktrees(ctx: Ctx) -> tuple[list[str], list[str]]:
    """Remove the worktrees of delivered issues. Branches and evidence stay.
    A worktree with uncommitted files is left alone and reported."""
    removed, kept = [], []
    for issue in ctx.state["issues"].values():
        path = ctx.root / issue["worktree"] if issue.get("worktree") else None
        if issue["status"] not in ("landed", "pr") or path is None or not path.exists():
            continue
        if _git_ok(["worktree", "remove", str(path)], ctx.root):
            removed.append(issue["id"])
        else:
            kept.append(issue["id"])
    _git(["worktree", "prune"], ctx.root, check=False)
    return removed, kept


def end_run(ctx: Ctx, status: str) -> str:
    def apply(state: dict[str, Any]) -> None:
        state["run"]["status"] = status
        state["run"].setdefault("ended_at", now())

    mutate(ctx.sd, apply)
    removed, kept = remove_finished_worktrees(ctx)
    message = f"run is {status}; the guard hook stands down"
    if removed:
        message += "; removed the worktrees of delivered issues " + ", ".join(sorted(removed))
    if kept:
        message += "; kept worktrees with uncommitted files: " + ", ".join(sorted(kept))
    return message


def cmd_run_status(args: argparse.Namespace) -> int:
    ctx = Ctx()
    if args.command == "stop":
        print(end_run(ctx, "stopped"))
        return 0
    target = {"pause": "paused", "resume": "active"}[args.command]

    def apply(state: dict[str, Any]) -> None:
        state["run"]["status"] = target
        if target == "active":
            state["run"].pop("ended_at", None)

    mutate(ctx.sd, apply)
    print(f"run is {target}" + ("; the guard hook stands down" if target != "active" else ""))
    return 0


# ---------------------------------------------------------------- cost and time

def _ledger(sd: Path) -> list[dict[str, Any]]:
    path = sd / "ledger.jsonl"
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        with contextlib.suppress(json.JSONDecodeError):
            rows.append(json.loads(line))
    return rows


def cmd_log(args: argparse.Namespace) -> int:
    ctx = Ctx()
    row = {"at": now(), "role": args.role, "issue": args.issue or "", "tokens": args.tokens,
           "ms": args.ms or 0, "model": args.model or "", "note": args.note or ""}
    path = ctx.sd / "ledger.jsonl"
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(row) + "\n")
    print(f"logged {args.tokens} tokens for {args.role}" + (f" on {args.issue}" if args.issue else ""))
    return 0


def _deadline(rules: dict[str, Any], started: dt.datetime) -> dt.datetime | None:
    text = str(rules["budget"].get("deadline") or "").strip()
    match = re.match(r"^(\d{1,2}):(\d{2})$", text)
    if not match:
        with contextlib.suppress(ValueError):
            parsed = _parse_ts(text)
            return parsed if parsed.tzinfo else parsed.astimezone()
        return None
    local_start = started.astimezone()
    candidate = local_start.replace(hour=int(match.group(1)), minute=int(match.group(2)), second=0, microsecond=0)
    if candidate <= local_start:
        candidate += dt.timedelta(days=1)
    return candidate


def usage_summary(ctx: Ctx, state: dict[str, Any], rules: dict[str, Any]) -> dict[str, Any]:
    """Tokens by role. Measured from the session transcript when the guard hook
    recorded one; otherwise from what the Lead logged with `crew log`."""
    started = _parse_ts(state["run"]["started_at"])
    sessions = read_json(ctx.sd / "sessions.json", default=[]) or []
    by_role: dict[str, dict[str, float]] = {}
    source = "none"
    if sessions:
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            import audit  # type: ignore
            for path in sessions:
                for row in audit.transcript_usage(Path(path), since=started):
                    slot = by_role.setdefault(row["role"], {"tokens": 0, "usd": 0.0, "priced": 0, "requests": 0, "cache_read": 0})
                    slot["tokens"] += row["total_tokens"]
                    slot["requests"] += row["requests"]
                    slot["cache_read"] += row["cache_read"]
                    usd = audit.price(row, rules.get("rates_usd_per_mtok") or {})
                    if usd is not None:
                        slot["usd"] += usd
                        slot["priced"] += row["total_tokens"]
            source = "transcript" if by_role else "none"
        except Exception as exc:  # a broken transcript must not break the brief
            by_role = {}
            source = f"none (transcript unreadable: {exc})"
    if not by_role:
        for row in _ledger(ctx.sd):
            slot = by_role.setdefault(row.get("role") or "unknown", {"tokens": 0, "usd": 0.0, "priced": 0, "requests": 0, "cache_read": 0})
            slot["tokens"] += int(row.get("tokens") or 0)
        if by_role:
            source = "ledger"
    total = sum(v["tokens"] for v in by_role.values())
    priced = sum(v["priced"] for v in by_role.values())
    usd = sum(v["usd"] for v in by_role.values())
    return {
        "source": source, "by_role": by_role, "total_tokens": int(total),
        "usd": round(usd, 2) if priced else None,
        "priced_fraction": round(priced / total, 3) if total else 0.0,
    }


def budget_report(ctx: Ctx) -> dict[str, Any]:
    state, rules = ctx.state, ctx.rules
    started = _parse_ts(state["run"]["started_at"])
    ended = _parse_ts(state["run"]["ended_at"]) if state["run"].get("ended_at") else dt.datetime.now(dt.timezone.utc)
    elapsed_h = (ended - started).total_seconds() / 3600
    usage = usage_summary(ctx, state, rules)
    budget = rules["budget"]
    over = []
    if budget.get("hours") and elapsed_h > float(budget["hours"]):
        over.append(f"time: {elapsed_h:.2f}h of {budget['hours']}h")
    if budget.get("usd") and usage["usd"] is not None and usage["usd"] > float(budget["usd"]):
        over.append(f"cost: ${usage['usd']} of ${budget['usd']}")
    deadline = _deadline(rules, started)
    if deadline and dt.datetime.now().astimezone() > deadline:
        over.append(f"deadline {budget['deadline']} has passed")
    return {"elapsed_hours": round(elapsed_h, 2), "usage": usage, "over": over,
            "deadline": deadline.isoformat(timespec="minutes") if deadline else ""}


def _cost_lines(report: dict[str, Any], rules: dict[str, Any]) -> list[str]:
    usage = report["usage"]
    budget = rules["budget"]
    lines = [f"Time: {report['elapsed_hours']}h elapsed (measured)"
             + (f" of a {budget['hours']}h budget" if budget.get("hours") else "")
             + (f"; deadline {report['deadline']}" if report["deadline"] else "")]
    if usage["source"] == "transcript":
        lines.append(f"Tokens: {usage['total_tokens']:,} (measured from the session transcript)")
    elif usage["source"] == "ledger":
        lines.append(f"Tokens: {usage['total_tokens']:,} (subagent totals logged by the Lead; the Lead's own tokens are not included)")
    else:
        lines.append("Tokens: not measured (" + usage["source"] + ")")
    if usage["usd"] is not None:
        pct = int(usage["priced_fraction"] * 100)
        lines.append(f"Cost: ${usage['usd']} (estimate: measured tokens x the rates in the Team Rules, {pct}% of tokens priced)"
                     + (f" of a ${budget['usd']} budget" if budget.get("usd") else ""))
    else:
        lines.append("Cost: not priced (add rates_usd_per_mtok to the Team Rules to convert tokens to dollars)")
    return lines


def cmd_budget(args: argparse.Namespace) -> int:
    ctx = Ctx()
    report = budget_report(ctx)
    for line in _cost_lines(report, ctx.rules):
        print(line)
    for role, slot in sorted(report["usage"]["by_role"].items(), key=lambda kv: -kv[1]["tokens"]):
        print(f"  {role:<12} {int(slot['tokens']):>12,} tokens")
    if report["usage"]["source"] != "transcript":
        print("no transcript recorded: after each dispatch run `crew log --role R --tokens N` with the reported usage")
    if report["over"]:
        print("OVER BUDGET: " + "; ".join(report["over"]) + ". Stop dispatching and write the brief.")
        return 3
    print("within budget")
    return 0


def _cell(value: Any) -> str:
    return str(value).replace("|", "/").replace("\n", " ")[:300]


def cmd_brief(args: argparse.Namespace) -> int:
    ctx = Ctx()
    state, rules = ctx.state, ctx.rules
    report = budget_report(ctx)
    lines = [f"# Crew brief: run {state['run']['id']}", "",
             "| Issue | Proving test | Status | Reason |", "|---|---|---|---|"]
    merge, retry, drop = [], [], []
    for issue_id in sorted(state["issues"]):
        issue = state["issues"][issue_id]
        status = issue["status"]
        proof = issue.get("proof") or {}
        tests = issue.get("tests") or {}
        if status in ("approved", "landed", "pr") and proof.get("verdict") == "pass":
            label = "proven"
            reason = (f"green at {proof['head'][:10]}, reviewed"
                      + (f", landed as {issue['landed']['sha'][:10]}" if issue.get("landed") else "")
                      + (f", {issue['pr']['url']}" if issue.get("pr") else ""))
            merge.append(issue_id)
        elif status == "proven":
            label, reason = "proven", f"green at {proof['head'][:10]}, review not finished"
            retry.append(issue_id)
        elif status == "parked":
            label, reason = "parked", "question: " + issue["parked"]["question"]
            retry.append(issue_id)
        elif status == "blocked":
            label, reason = "blocked", issue["blocked"]["reason"]
            drop.append(issue_id)
        else:
            open_rej = [r for r in issue.get("rejections", []) if not r.get("resolved")]
            label = "blocked"
            reason = (f"{open_rej[-1]['by']} rejection open: {open_rej[-1]['reason']}" if open_rej
                      else f"not finished (stopped at {status})")
            retry.append(issue_id)
        test = f"`{tests['command']}`" if tests.get("command") else "none yet"
        lines.append(f"| {issue_id}: {_cell(issue['title'])} | {_cell(test)} | {label} | {_cell(reason)} |")
    lines += ["", "## Time and cost", *("- " + line for line in _cost_lines(report, rules))]
    for role, slot in sorted(report["usage"]["by_role"].items(), key=lambda kv: -kv[1]["tokens"]):
        lines.append(f"  - {role}: {int(slot['tokens']):,} tokens")
    lines += ["", "## Rejections and how each was resolved"]
    any_rej = False
    for issue_id in sorted(state["issues"]):
        for item in state["issues"][issue_id].get("rejections", []):
            any_rej = True
            lines.append(f"- {issue_id} by {item['by']} at {item['at']}: {item['reason'][:400]} -> "
                         + (item["resolved"] or "UNRESOLVED"))
    if not any_rej:
        lines.append("- none")
    waivers = [(i, w) for i in sorted(state["issues"]) for w in state["issues"][i].get("waivers", [])]
    if waivers:
        lines += ["", "## Waivers used"] + [f"- {i}: {w['gate']} ({w['reason']})" for i, w in waivers]
    lines += ["", "## One decision for you",
              f"- merge: {', '.join(merge) or 'none'}",
              f"- retry: {', '.join(retry) or 'none'}",
              f"- drop: {', '.join(drop) or 'none'}",
              "", "## WHAT STILL LEAKS?"]
    by_role = report["usage"]["by_role"]
    total = report["usage"]["total_tokens"]
    if total:
        top_role, top = max(by_role.items(), key=lambda kv: kv[1]["tokens"])
        line = f"- {top_role} used {int(top['tokens']):,} tokens ({int(100 * top['tokens'] / total)}% of the run)"
        if top.get("requests"):
            line += (f" over {int(top['requests'])} requests; {int(top['cache_read']):,} of them were the conversation "
                     "being re-read from cache, once per request. Fewer, larger steps cut this")
        lines.append(line + ".")
    rework = sum(len(i.get("rejections", [])) for i in state["issues"].values())
    lines.append(f"- {rework} rejection(s) meant rework." if rework else "- No rework: nothing was rejected.")
    if report["over"]:
        lines.append("- Over budget: " + "; ".join(report["over"]))
    text = "\n".join(lines) + "\n"
    write_atomic(ctx.sd / "BRIEF.md", text)
    print(text)
    if state["run"]["status"] == "active":
        print("(" + end_run(ctx, "done") + ". `crew resume` reopens it.)")
    return 0


# ------------------------------------------------------------------ graph, lanes

def graphify_bin() -> str | None:
    return os.environ.get("CREW_GRAPHIFY") or shutil.which("graphify")


def cmd_graph(args: argparse.Namespace) -> int:
    ctx = Ctx()
    binary = graphify_bin()
    if not binary and args.install and shutil.which("uv"):
        subprocess.run(["uv", "tool", "install", "graphifyy"], capture_output=True)
        binary = graphify_bin() or shutil.which("graphify", path=str(Path.home() / ".local" / "bin"))
    graph = ctx.root / "graphify-out" / "graph.json"
    info: dict[str, Any] = {"available": bool(binary), "binary": binary or "", "graph": str(graph), "built": False}
    if not binary:
        info["hint"] = "graphify is not installed: `uv tool install graphifyy` (or pipx). Map with rg and Read instead."
        print(json.dumps(info, indent=2))
        return 1
    proc = subprocess.run([binary, "update", "."], cwd=str(ctx.root), capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    info["built"] = proc.returncode == 0 and graph.exists()
    info["output"] = "\n".join((proc.stdout + proc.stderr).strip().splitlines()[-4:])
    if info["built"]:
        info["report"] = str(ctx.root / "graphify-out" / "GRAPH_REPORT.md")
    print(json.dumps(info, indent=2))
    return 0 if info["built"] else 1


_AFFECTED_RE = re.compile(r"^- .* \[[^\]]+\] (.+?)(?::L\d+.*)?$")


def affected_files(root: Path, binary: str, target: str, depth: int = 1) -> set[str]:
    """Files that depend on `target`, from `graphify affected`. Empty when unknown."""
    proc = subprocess.run([binary, "affected", target, "--depth", str(depth)], cwd=str(root),
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        return set()
    found = set()
    for line in proc.stdout.splitlines():
        match = _AFFECTED_RE.match(line)
        if match and match.group(1) != "-":
            found.add(norm_rel(match.group(1)))
    return found


def cmd_waves(args: argparse.Namespace) -> int:
    """Group open issues into waves that are safe to build in parallel."""
    ctx = Ctx()
    state, rules = ctx.state, ctx.rules
    tracked = _git(["ls-files"], ctx.root).splitlines()
    issues = [i for i in state["issues"].values() if i["status"] in ("queued", "started", "red")]
    binary = graphify_bin() if (ctx.root / "graphify-out" / "graph.json").exists() else None
    owned: dict[str, set[str]] = {}
    reach: dict[str, set[str]] = {}
    for issue in issues:
        files = {p for p in tracked if matches_any(p, issue.get("owns") or [])}
        owned[issue["id"]] = files
        deps: set[str] = set()
        if binary:
            for path in sorted(files)[:40]:
                deps |= affected_files(ctx.root, binary, path)
        reach[issue["id"]] = deps
    conflicts: dict[str, dict[str, str]] = {i["id"]: {} for i in issues}
    for a in issues:
        for b in issues:
            if a["id"] >= b["id"]:
                continue
            why = ""
            if not a.get("owns") or not b.get("owns"):
                why = "no lane declared, so independence is unknown"
            elif owned[a["id"]] & owned[b["id"]]:
                why = "both own " + ", ".join(sorted(owned[a["id"]] & owned[b["id"]])[:3])
            elif any(matches_any(pa, b["owns"]) or matches_any(pb, a["owns"]) for pa in a["owns"] for pb in b["owns"]):
                why = "lane patterns overlap"
            elif owned[a["id"]] & reach[b["id"]] or owned[b["id"]] & reach[a["id"]]:
                hit = sorted((owned[a["id"]] & reach[b["id"]]) | (owned[b["id"]] & reach[a["id"]]))
                why = "graph: one lane depends on the other via " + ", ".join(hit[:3])
            if a.get("on") == b["id"] or b.get("on") == a["id"]:
                why = "stacked: one is built on the other"
            if why:
                conflicts[a["id"]][b["id"]] = why
                conflicts[b["id"]][a["id"]] = why
    cap = rules["limits"]["max_builders"]
    waves: list[list[str]] = []
    for issue in sorted(issues, key=lambda i: (-len(conflicts[i["id"]]), i["id"])):
        for wave in waves:
            if len(wave) < cap and not any(other in conflicts[issue["id"]] for other in wave):
                wave.append(issue["id"])
                break
        else:
            waves.append([issue["id"]])

    # In pr-per-issue delivery two coupled issues cut from the same base produce
    # PRs that conflict, so the later one is built on top of the earlier one.
    stacks: dict[str, str] = {}
    if rules["delivery"]["mode"] == "pr-per-issue":
        wave_of = {issue_id: index for index, wave in enumerate(waves, 1) for issue_id in wave}
        for issue in issues:
            earlier = [other for other in conflicts[issue["id"]] if wave_of[other] < wave_of[issue["id"]]]
            if earlier and not issue.get("on") and issue["status"] == "queued":
                stacks[issue["id"]] = max(earlier, key=lambda other: (wave_of[other], other))

    def apply(st: dict[str, Any]) -> None:
        for index, wave in enumerate(waves, 1):
            for issue_id in wave:
                st["issues"][issue_id]["wave"] = index
        for issue_id, dep in stacks.items():
            st["issues"][issue_id]["on"] = dep

    mutate(ctx.sd, apply)
    print("WAVES (issues in one wave can be built in parallel, at most "
          f"{cap} builders; coupling from " + ("the graph and lanes" if binary else "lanes only, no graph") + ")")
    for index, wave in enumerate(waves, 1):
        print(f"  wave {index}: {', '.join(wave)}")
    for a in sorted(conflicts):
        for b, why in sorted(conflicts[a].items()):
            if a < b:
                print(f"  {a} x {b}: {why}")
    for issue_id, dep in sorted(stacks.items()):
        print(f"  {issue_id} is built on top of {dep}: its branch is cut from {dep}'s once {dep} is approved, "
              "and its PR is merged after it")
    if not issues:
        print("  (no open issues)")
    return 0


ROUTING = [
    ("Lead", "session model", "session", "plans, assigns, verifies; writes no feature code; the only one who messages you"),
    ("Mapper", "sonnet", "medium", "retrieval over the code graph; writes .crew/NOTES.md"),
    ("Breaker", "session model", "high", "turns each issue into a failing test; a wrong test poisons everything after it"),
    ("Builder", "session model", "medium", "smallest change against a sealed test; raised to high after a second rejection"),
    ("Prover", "haiku", "default", "runs the proof on a clean checkout and reports what the machine said"),
    ("Reviewer", "session model", "high", "reads the diff against the spec like a stranger who expects it to be wrong"),
]


def cmd_plan(args: argparse.Namespace) -> int:
    """Everything the user needs to say go: rules, baseline, crew, waves."""
    ctx = Ctx()
    code = cmd_rules(args)
    state = ctx.state
    baseline = state["run"].get("baseline")
    print("\nBASELINE")
    if baseline:
        print(f"  {'green' if baseline['green'] else 'RED: ' + ', '.join(baseline['red'])} at {baseline['sha'][:10]} ({baseline['at']})")
    else:
        print("  not run yet: `crew baseline` must pass before the plan is shown")
        code = code or 1
    print("\nCREW")
    for role, model, effort, job in ROUTING:
        print(f"  {role:<9} {model:<14} effort {effort:<8} {job}")
    print()
    cmd_waves(args)
    host = read_json(ctx.sd / "host.json", default={}) or {}
    mode = host.get("permission_mode")
    if mode in ("default", "plan"):
        print(f"\nNOTE: this session's permission mode is {mode!r}; it will stop to ask before commands and edits. "
              "An unattended run needs a mode that does not prompt.")
    return code


def cmd_role(args: argparse.Namespace) -> int:
    """Print a role's brief, for hosts where the crew agent types are not registered."""
    base = Path(__file__).resolve().parents[3] / "agents"
    for candidate in (base / f"{args.name}.md", base / f"crew-{args.name}.md"):
        if candidate.exists():
            text = candidate.read_text(encoding="utf-8")
            if text.startswith("---"):
                text = text.split("---", 2)[-1]
            print(text.strip())
            return 0
    raise CrewError(f"no brief found for role {args.name} under {base}")


def cmd_where(args: argparse.Namespace) -> int:
    ctx = Ctx()
    print(json.dumps({"root": str(ctx.root), "state": str(ctx.sd), "crew": str(ctx.sd / "bin" / "crew"),
                      "script": str(Path(__file__).resolve()), "graphify": graphify_bin() or ""}, indent=2))
    return 0


# -------------------------------------------------------------------------- cli

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="crew", description=(__doc__ or "").split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name: str, fn: Callable[[argparse.Namespace], int], help_text: str) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help_text)
        p.set_defaults(fn=fn)
        return p

    add("init", cmd_init, "create .crew/, hide it from git, write the rules template")
    add("rules", cmd_rules, "validate and print the Team Rules")
    add("baseline", cmd_baseline, "run setup, suite and checks on the untouched base")
    p = add("add", cmd_add, "register an issue")
    p.add_argument("id")
    p.add_argument("--title", required=True)
    p.add_argument("--spec-file")
    p.add_argument("--spec")
    p.add_argument("--source")
    p.add_argument("--owns", nargs="*")
    p.add_argument("--on", help="build on top of another issue's branch")
    p.add_argument("--fixes-baseline", action="store_true",
                   help="this issue makes a red baseline green; it alone may start under a halt")
    add("start", cmd_start, "create the issue's branch and worktree").add_argument("id")
    p = add("seal", cmd_seal, "record the proving test and confirm it fails on the base")
    p.add_argument("id")
    p.add_argument("--cmd", required=True)
    p.add_argument("--test-file", nargs="*")
    p = add("unseal", cmd_unseal, "reopen the proving test for revision")
    p.add_argument("id")
    p.add_argument("--reason", required=True)
    add("check", cmd_check, "static gates only (fast)").add_argument("id")
    p = add("prove", cmd_prove, "run every gate on a clean checkout")
    p.add_argument("id")
    p.add_argument("--recheck-red", action="store_true", help="also re-run the proving test on the base")
    p = add("review", cmd_review, "record the Reviewer's verdict from a JSON file")
    p.add_argument("id")
    p.add_argument("--file", required=True)
    p = add("reject", cmd_reject, "send an issue back for a reason the gates cannot see")
    p.add_argument("id")
    p.add_argument("--by", required=True, choices=("prover", "reviewer", "lead"))
    p.add_argument("--reason")
    p.add_argument("--reason-file")
    add("land", cmd_land, "single-branch: integrate, re-prove, fast-forward").add_argument("id")
    p = add("pr-body", cmd_pr_body, "render the PR description from the evidence")
    p.add_argument("id", nargs="?")
    p.add_argument("--all", action="store_true")
    p.add_argument("--out")
    p = add("pr", cmd_pr, "record the PR url")
    p.add_argument("id")
    p.add_argument("--url", required=True)
    p = add("park", cmd_park, "park an issue with one question")
    p.add_argument("id")
    p.add_argument("--question")
    p.add_argument("--question-file")
    p = add("unpark", cmd_unpark, "resume a parked issue")
    p.add_argument("id")
    p.add_argument("--answer")
    p = add("block", cmd_block, "mark an issue blocked")
    p.add_argument("id")
    p.add_argument("--reason", required=True)
    p = add("waive", cmd_waive, "record the user's yes for one gate")
    p.add_argument("id")
    p.add_argument("--gate", required=True)
    p.add_argument("--reason", required=True)
    p = add("own", cmd_own, "change an issue's lane")
    p.add_argument("id")
    p.add_argument("--add", nargs="*")
    p.add_argument("--remove", nargs="*")
    add("status", cmd_status, "show every issue and its next step").add_argument("--json", action="store_true")
    for name in ("stop", "pause", "resume"):
        add(name, cmd_run_status, f"{name} the run")
    p = add("log", cmd_log, "log a dispatch's token usage (fallback when no transcript)")
    p.add_argument("--role", required=True)
    p.add_argument("--tokens", type=int, required=True)
    p.add_argument("--issue")
    p.add_argument("--ms", type=int)
    p.add_argument("--model")
    p.add_argument("--note")
    add("budget", cmd_budget, "time and tokens against the budget")
    add("brief", cmd_brief, "render the brief from the recorded evidence")
    add("graph", cmd_graph, "build or refresh the graphify graph").add_argument("--install", action="store_true")
    add("waves", cmd_waves, "group issues into parallel-safe waves")
    add("plan", cmd_plan, "rules, baseline, crew and waves: what the user approves")
    add("role", cmd_role, "print a role's brief").add_argument("name", choices=ROLES)
    add("where", cmd_where, "print paths")
    return parser


def main(argv: list[str] | None = None) -> int:
    with contextlib.suppress(Exception):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    args = build_parser().parse_args(argv)
    try:
        return int(args.fn(args))
    except CrewError as exc:
        print(f"crew: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
