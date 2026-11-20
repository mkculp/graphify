"""The PreToolUse guard: fed the same JSON Claude Code sends on stdin."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import GUARD


def hook(project, tool, tool_input, agent_type=None, cwd=None, transcript=None):
    payload = {
        "session_id": "s1", "hook_event_name": "PreToolUse", "permission_mode": "default",
        "cwd": str(cwd or project.root), "tool_name": tool, "tool_input": tool_input,
        "transcript_path": transcript or "",
    }
    if agent_type:
        payload["agent_type"] = agent_type
        payload["agent_id"] = "a1"
    proc = subprocess.run([sys.executable, str(GUARD)], input=json.dumps(payload), capture_output=True,
                          text=True, cwd=str(cwd or project.root), env=project.env)
    return proc.returncode, proc.stderr.strip()


def write(project, path, agent_type=None, cwd=None):
    return hook(project, "Write", {"file_path": str(path), "content": "x"}, agent_type, cwd)


def bash(project, command, agent_type=None, cwd=None):
    return hook(project, "Bash", {"command": command}, agent_type, cwd)


@pytest.fixture
def run(project):
    """An active run with one sealed issue whose lane is calc.py."""
    project.add_issue("1", owns=["calc.py"])
    project.break_div()
    return project


def test_no_run_means_no_opinion(tmp_path, project):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    code, _ = hook(project, "Write", {"file_path": str(outside / "a.py"), "content": "x"}, cwd=outside)
    assert code == 0
    code, _ = hook(project, "Bash", {"command": "git push --force"}, cwd=outside)
    assert code == 0


def test_a_paused_or_stopped_run_stands_down(run):
    assert write(run, run.root / "calc.py")[0] == 2
    run.ok("pause")
    assert write(run, run.root / "calc.py")[0] == 0
    run.ok("resume")
    assert write(run, run.root / "calc.py")[0] == 2


def test_the_lead_writes_no_project_files(run):
    code, why = write(run, run.root / "calc.py")
    assert code == 2 and "does not write project files" in why
    code, why = write(run, run.wt("1") / "calc.py")
    assert code == 2 and "does not write feature code" in why
    assert write(run, run.root / ".crew" / "rules.json")[0] == 0
    assert write(run, run.root / ".crew" / "tmp" / "review.json")[0] == 0


def test_state_and_evidence_are_never_hand_edited(run):
    for agent in (None, "crew:builder", "crew:prover", "general-purpose"):
        for rel in ("state.json", "TASKS.md", "ledger.jsonl", "evidence/1/red.log", "bin/crew"):
            code, why = write(run, run.root / ".crew" / rel, agent)
            assert code == 2 and "only by the crew command" in why, (agent, rel)
    code, why = write(run, run.root / ".crew" / "rules.json", "crew:builder")
    assert code == 2 and "only the Lead writes the Team Rules" in why
    assert write(run, run.root / ".crew" / "NOTES.md", "crew:mapper")[0] == 0


@pytest.mark.parametrize("agent", ["crew:builder", "crew-builder", "builder"])
def test_a_builder_writes_only_inside_its_lane(run, agent):
    wt = run.wt("1")
    assert write(run, wt / "calc.py", agent, cwd=wt)[0] == 0
    assert write(run, wt / "tests" / "test_extra.py", agent, cwd=wt)[0] == 0, "builders may add tests"
    code, why = write(run, wt / "other.py", agent, cwd=wt)
    assert code == 2 and "outside the lane" in why and "widen the lane" in why
    code, why = write(run, run.root / "calc.py", agent, cwd=wt)
    assert code == 2 and "only inside an issue worktree" in why
    assert hook(run, "Edit", {"file_path": "calc.py", "old_string": "a", "new_string": "b"}, agent, cwd=wt)[0] == 0


def test_sealed_proving_tests_are_immutable_for_every_role(run):
    wt = run.wt("1")
    for agent in ("crew:builder", "crew:breaker", "general-purpose"):
        code, why = write(run, wt / "tests" / "test_div.py", agent, cwd=wt)
        assert code == 2 and "sealed proving test" in why, agent
    run.ok("unseal", "1", "--reason", "wrong exception")
    assert write(run, wt / "tests" / "test_div.py", "crew:breaker", cwd=wt)[0] == 0


def test_read_only_roles_cannot_write_code(run):
    wt = run.wt("1")
    for agent in ("crew:prover", "crew:reviewer", "crew:mapper"):
        code, why = write(run, wt / "calc.py", agent, cwd=wt)
        assert code == 2 and "read-only" in why, agent


def test_fenced_paths_are_not_written_and_secrets_are_not_read(run):
    wt = run.wt("1")
    for rel in (".env", "config/.env.production", "migrations/002.sql", "app/billing/invoice.py", "deploy/server.pem"):
        code, why = write(run, wt / rel, "crew:builder", cwd=wt)
        assert code == 2 and "never-touch" in why, rel
    assert write(run, wt / ".env.example", "crew:breaker", cwd=wt)[0] == 0
    code, why = hook(run, "Read", {"file_path": str(wt / ".env")}, "crew:builder", cwd=wt)
    assert code == 2 and "secret pattern" in why
    assert hook(run, "Read", {"file_path": str(run.root / ".env")})[0] == 2
    assert hook(run, "Read", {"file_path": str(wt / "migrations" / "002.sql")}, "crew:builder", cwd=wt)[0] == 0
    assert hook(run, "Read", {"file_path": str(wt / "calc.py")}, "crew:builder", cwd=wt)[0] == 0


def test_the_users_yes_lifts_a_fence(run):
    wt = run.wt("1")
    run.ok("own", "1", "--add", "migrations/")
    assert write(run, wt / "migrations" / "002.sql", "crew:builder", cwd=wt)[0] == 2
    run.set_rules(allow_touch=["migrations/"])
    assert write(run, wt / "migrations" / "002.sql", "crew:builder", cwd=wt)[0] == 0


@pytest.mark.parametrize("command", [
    "git push --force origin crew/1-x",
    "git push -f",
    "git push origin +main",
    "git push --force-with-lease",
    "cd repo && git push origin main --force",
    "git push origin --tags",
    "git push origin :old-branch",
    "git commit --no-verify -m wip",
    "git commit -n -m wip",
    "git commit -anm wip",
    "npm publish",
    "pnpm publish --access public",
    "twine upload dist/*",
    "docker push registry.example/app:1",
    "gh release create v1.0.0",
    "terraform apply -auto-approve",
    "kubectl delete pod web-1",
    "cat .env",
    "grep API_KEY config/.env.production",
    "source ./.env && run",
    "rm -rf migrations/",
    "sed -i s/a/b/ app/billing/price.py",
    "echo x > migrations/003.sql",
    "git checkout HEAD -- migrations/001.sql",
])
def test_bash_denies_for_the_lead(run, command):
    code, why = bash(run, command)
    assert code == 2, f"{command!r} was allowed"
    assert why.startswith("crew guard: ")


@pytest.mark.parametrize("command", [
    "git push -u origin crew/1-div",
    "git push -n origin main",
    "git commit -m 'docs: explain --force and -f flags; never --no-verify'",
    "git commit -m \"note: we do not npm publish here\"",
    "python -m pytest -q 2>&1 | tail -20",
    "ls migrations/ 2>&1",
    "git diff -- migrations/ > /dev/null",
    "cat migrations/001.sql",
    "rm -rf build dist && ls",
    "cat .env.example",
    "echo 'see .env for details'",
    "npm test -- --env=jsdom",
    "git log --oneline -n 5",
    "git status",
])
def test_bash_allows_ordinary_work(run, command):
    code, why = bash(run, command)
    assert code == 0, f"{command!r} was denied: {why}"


def test_only_the_lead_pushes_and_sealed_tests_survive_shell_edits(run):
    wt = run.wt("1")
    code, why = bash(run, "git push -u origin crew/1-div", "crew:builder", cwd=wt)
    assert code == 2 and "only the Lead pushes" in why
    code, why = bash(run, "sed -i s/ValueError/Exception/ tests/test_div.py", "crew:builder", cwd=wt)
    assert code == 2 and "sealed proving test" in why
    assert bash(run, "python -m unittest tests.test_div -q", "crew:builder", cwd=wt)[0] == 0
    assert bash(run, "git rm tests/test_div.py", "crew:builder", cwd=wt)[0] == 2


def test_the_transcript_is_recorded_for_measured_cost(run, tmp_path):
    transcript = str(tmp_path / "t.jsonl")
    hook(run, "Bash", {"command": "git status"}, transcript=transcript)
    hook(run, "Bash", {"command": "git status"}, transcript=transcript)
    assert json.loads((run.root / ".crew" / "sessions.json").read_text()) == [transcript]


def test_a_broken_payload_or_state_never_blocks(run):
    proc = subprocess.run([sys.executable, str(GUARD)], input="not json", capture_output=True, text=True, cwd=str(run.root))
    assert proc.returncode == 0
    state_path = run.root / ".crew" / "state.json"
    good = state_path.read_text()
    state_path.write_text("{ corrupt")
    assert write(run, run.root / "calc.py")[0] == 0
    malformed = json.loads(good)
    malformed["issues"] = "not a mapping"
    state_path.write_text(json.dumps(malformed))
    code, why = write(run, run.wt("1") / "calc.py", "crew:builder")
    assert code == 0 and why == "", "an internal error fails open; `crew prove` is the authority"


def test_the_permission_mode_is_recorded_for_the_plan(run):
    hook(run, "Bash", {"command": "git status"})
    assert json.loads((run.root / ".crew" / "host.json").read_text()) == {"permission_mode": "default"}
    assert write(run, run.root / ".crew" / "host.json", "crew:builder")[0] == 2


REVIEW_HEREDOC = """mkdir -p .crew/tmp && cat > .crew/tmp/review-1.json <<'JSON'
{
  "verdict": "approve",
  "why_break": "breaks if .env lacks API_KEY; a later migrations/ change could rm the column; npm publish is unaffected"
}
JSON
echo recorded"""


QUESTION_HEREDOC = """cat > .crew/tmp/question.md <<'EOF'
Should the loader read .env or config/secrets/prod.yml? Otherwise we would rm migrations/ by hand.
EOF"""


def test_heredoc_bodies_are_data_not_paths(run):
    assert bash(run, REVIEW_HEREDOC, "crew:reviewer")[0] == 0
    code, why = bash(run, QUESTION_HEREDOC)
    assert code == 0, f"an unquoted heredoc body was read as paths: {why}"
    code, why = bash(run, "cat > .env <<'EOF'\nKEY=1\nEOF")
    assert code == 2 and "secret pattern" in why, "the target of the redirect is still checked"
    code, why = bash(run, "cat <<EOF > notes.txt\nhello\nEOF\nrm -rf migrations/")
    assert code == 2 and "never-touch" in why, "commands after the heredoc are still checked"


def test_the_guard_stands_down_once_the_brief_is_sent(run):
    assert write(run, run.root / "calc.py")[0] == 2
    run.ok("brief")
    assert write(run, run.root / "calc.py")[0] == 0, "after the brief the user can edit their own project again"


LEAD_PLAN_COMMAND = """C=.crew/bin/crew; mkdir -p .crew/tmp
cat > .crew/tmp/s11.md <<'EOF'
Add a reserved column in migrations/001_init.sql.
BLOCKED: migrations/ is in the built-in fence.
EOF
$C add 11 --title "Track reserved stock" --spec-file .crew/tmp/s11.md --owns migrations/001_init.sql
$C plan"""


@pytest.mark.parametrize("command", [
    LEAD_PLAN_COMMAND,
    "cat migrations/001_init.sql > /tmp/schema-copy.sql",
    "cp migrations/001_init.sql /tmp/schema-copy.sql",
    "ls migrations/ && rm -rf build",
    "rm -rf build; git diff --stat -- migrations/",
    "grep -rn reserved migrations/ | tee /tmp/found.txt",
    "wc -l migrations/001_init.sql 2>/dev/null || true",
])
def test_bash_allows_commands_that_only_read_or_name_a_fenced_path(run, command):
    code, why = bash(run, command)
    assert code == 0, f"{command!r} was denied: {why}"


@pytest.mark.parametrize("command", [
    "cat /tmp/new.sql > migrations/002.sql",
    "cat /tmp/new.sql >> migrations/001_init.sql",
    "cp /tmp/new.sql migrations/002.sql",
    "rm -rf build migrations/",
    "sudo rm -rf migrations/",
    "echo ok; rm migrations/001_init.sql",
    "ls . && mv migrations/001_init.sql /tmp/gone.sql",
    "find . -name '*.pyc' | xargs rm migrations/001_init.sql",
    "git reset --hard HEAD -- migrations/",
    "cp .env /tmp/leak.txt",
])
def test_bash_denies_commands_that_write_a_fenced_path_or_touch_a_secret(run, command):
    code, why = bash(run, command)
    assert code == 2, f"{command!r} was allowed"
