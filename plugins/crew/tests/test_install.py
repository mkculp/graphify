"""The project installer: copies the crew into .claude/ and merges one hook."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from conftest import INSTALL, SCRIPTS

import install


def run(project: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(INSTALL), "--project", str(project), *args], capture_output=True, text=True)


def test_install_copies_the_skill_renames_agents_and_adds_the_hook(tmp_path):
    proc = run(tmp_path)
    assert proc.returncode == 0, proc.stderr
    skill = tmp_path / ".claude" / "skills" / "crew"
    assert (skill / "SKILL.md").read_text() == (SCRIPTS.parent / "SKILL.md").read_text()
    for name in ("crew.py", "guard.py", "audit.py"):
        assert (skill / "scripts" / name).is_file()
    assert not (skill / "scripts" / "install.py").exists()
    assert sorted(p.name for p in (skill / "references").iterdir()) == [
        "audit.md", "delivery.md", "graph.md", "handoff.md", "rules.md"]
    for role in install.ROLES:
        text = (tmp_path / ".claude" / "agents" / f"crew-{role}.md").read_text()
        assert f"\nname: crew-{role}\n" in text and f"\nname: {role}\n" not in text
    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    command = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert command == 'python3 "$CLAUDE_PROJECT_DIR/.claude/skills/crew/scripts/guard.py"'


def test_install_keeps_existing_settings_and_is_idempotent(tmp_path):
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir()
    existing = {
        "permissions": {"allow": ["Bash(npm test:*)"]},
        "hooks": {
            "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "./lint.sh"}]}],
            "Stop": [{"hooks": [{"type": "command", "command": "./notify.sh"}]}],
        },
    }
    settings_path.write_text(json.dumps(existing))
    assert run(tmp_path).returncode == 0
    first = settings_path.read_text()
    assert run(tmp_path).returncode == 0
    assert settings_path.read_text() == first, "a second install changes nothing"
    settings = json.loads(first)
    assert settings["permissions"] == existing["permissions"]
    assert settings["hooks"]["Stop"] == existing["hooks"]["Stop"]
    groups = settings["hooks"]["PreToolUse"]
    assert groups[0] == existing["hooks"]["PreToolUse"][0] and len(groups) == 2


def test_uninstall_removes_only_what_install_added(tmp_path):
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir()
    settings_path.write_text(json.dumps({"permissions": {"allow": []}}))
    (tmp_path / ".claude" / "agents").mkdir()
    (tmp_path / ".claude" / "agents" / "mine.md").write_text("---\nname: mine\ndescription: x\n---\n")
    run(tmp_path)
    proc = run(tmp_path, "--uninstall")
    assert proc.returncode == 0
    assert not (tmp_path / ".claude" / "skills").exists()
    assert [p.name for p in (tmp_path / ".claude" / "agents").iterdir()] == ["mine.md"]
    assert json.loads(settings_path.read_text()) == {"permissions": {"allow": []}}
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    run(fresh)
    run(fresh, "--uninstall")
    assert not (fresh / ".claude").exists(), "an install into an empty project leaves nothing behind"


def test_install_refuses_to_overwrite_broken_settings(tmp_path):
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir()
    settings_path.write_text("{ not json")
    proc = run(tmp_path)
    assert proc.returncode != 0 and "not valid JSON" in proc.stderr
    assert settings_path.read_text() == "{ not json"


def test_the_installed_copy_runs_a_full_proof_and_finds_its_role_briefs(tmp_path, project):
    """The vendored scripts must work from .claude/, not only from the plugin source."""
    assert run(project.root).returncode == 0
    vendored = project.root / ".claude" / "skills" / "crew" / "scripts" / "crew.py"

    def crew(*args):
        return subprocess.run([sys.executable, str(vendored), *args], cwd=str(project.root), capture_output=True, text=True)

    assert crew("init").returncode == 0
    role = crew("role", "prover")
    assert role.returncode == 0 and "You are the Prover" in role.stdout and "name:" not in role.stdout
    wrapper = (project.root / ".crew" / "bin" / "crew").read_text()
    assert str(vendored) in wrapper, "init re-points the crew command at the copy that ran it"
    project.add_issue("1")
    project.break_div()
    project.fix_div()
    proved = crew("prove", "1")
    assert proved.returncode == 0 and "VERDICT: PROVEN" in proved.stdout
    hook = subprocess.run(
        [sys.executable, str(vendored.with_name("guard.py"))], cwd=str(project.root), capture_output=True, text=True,
        input=json.dumps({"cwd": str(project.root), "tool_name": "Write", "agent_type": "crew-prover",
                          "tool_input": {"file_path": str(project.wt("1") / "calc.py")}}))
    assert hook.returncode == 2 and "read-only" in hook.stderr
