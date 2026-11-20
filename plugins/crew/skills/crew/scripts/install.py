#!/usr/bin/env python3
"""Install the crew into one project's `.claude/` directory.

Claude Code on the web does not load marketplace plugins, but it does read a
repository's `.claude/skills`, `.claude/agents` and `.claude/settings.json`.
This copies the crew there so it works in any session opened on the project:

    python3 install.py --project /path/to/project
    python3 install.py --project /path/to/project --uninstall

It writes only under `<project>/.claude/`, never overwrites a setting it does
not own, and is safe to run again after an update. Standard library only.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

ROLES = ("mapper", "breaker", "builder", "prover", "reviewer")
MARKER = ".claude/skills/crew/scripts/guard.py"
HOOK_COMMAND = 'python3 "$CLAUDE_PROJECT_DIR/' + MARKER + '"'
HOOK_ENTRY = {
    "matcher": "Edit|Write|MultiEdit|NotebookEdit|Bash|Read",
    "hooks": [{"type": "command", "command": HOOK_COMMAND, "timeout": 10}],
}


def source_dirs() -> tuple[Path, Path]:
    skill = Path(__file__).resolve().parents[1]
    agents = Path(__file__).resolve().parents[3] / "agents"
    if not (skill / "SKILL.md").is_file() or not all((agents / f"{r}.md").is_file() for r in ROLES):
        raise SystemExit(
            "install.py must run from the crew plugin source (plugins/crew/skills/crew/scripts/), "
            f"but found no skill at {skill} or no agents at {agents}")
    return skill, agents


def _is_crew_hook(group: object) -> bool:
    return isinstance(group, dict) and any(
        isinstance(h, dict) and MARKER in str(h.get("command", "")) for h in group.get("hooks") or [])


def load_settings(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise SystemExit(f"{path} is not valid JSON ({exc}); fix it before installing the crew hook")
    if not isinstance(data, dict):
        raise SystemExit(f"{path} must contain a JSON object")
    return data


def set_hook(settings_path: Path, present: bool) -> bool:
    """Add or remove the guard hook, leaving every other setting as it was."""
    settings = load_settings(settings_path)
    hooks = settings.get("hooks")
    if hooks is None:
        hooks = {}
    if not isinstance(hooks, dict):
        raise SystemExit(f'{settings_path}: "hooks" is not an object; fix it before installing the crew hook')
    groups = hooks.get("PreToolUse") or []
    if not isinstance(groups, list):
        raise SystemExit(f'{settings_path}: "hooks.PreToolUse" is not a list; fix it before installing the crew hook')
    kept = [g for g in groups if not _is_crew_hook(g)]
    after = kept + [HOOK_ENTRY] if present else kept
    if after == groups:
        return False
    if after:
        hooks["PreToolUse"] = after
    else:
        hooks.pop("PreToolUse", None)
    if hooks:
        settings["hooks"] = hooks
    else:
        settings.pop("hooks", None)
    if not settings and not present:
        settings_path.unlink()
        return True
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    return True


def project_agent(text: str, role: str) -> str:
    """Rename a plugin agent for project scope: `crew:mapper` becomes `crew-mapper`."""
    renamed, count = re.subn(rf"(?m)^name:\s*{role}\s*$", f"name: crew-{role}", text, count=1)
    if count != 1:
        raise SystemExit(f"agent {role}.md has no `name: {role}` line in its frontmatter")
    return renamed


def install(project: Path) -> list[str]:
    skill_src, agents_src = source_dirs()
    claude = project / ".claude"
    skill_dst = claude / "skills" / "crew"
    if skill_dst.exists():
        shutil.rmtree(skill_dst)
    shutil.copytree(skill_src, skill_dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "install.py"))
    written = [str(skill_dst.relative_to(project))]
    (claude / "agents").mkdir(parents=True, exist_ok=True)
    for role in ROLES:
        target = claude / "agents" / f"crew-{role}.md"
        target.write_text(project_agent((agents_src / f"{role}.md").read_text(encoding="utf-8"), role), encoding="utf-8")
        written.append(str(target.relative_to(project)))
    set_hook(claude / "settings.json", present=True)
    written.append(".claude/settings.json")
    return written


def uninstall(project: Path) -> list[str]:
    claude = project / ".claude"
    removed = []
    if (claude / "skills" / "crew").exists():
        shutil.rmtree(claude / "skills" / "crew")
        removed.append(".claude/skills/crew")
    for role in ROLES:
        target = claude / "agents" / f"crew-{role}.md"
        if target.exists():
            target.unlink()
            removed.append(str(target.relative_to(project)))
    if (claude / "settings.json").exists() and set_hook(claude / "settings.json", present=False):
        removed.append(".claude/settings.json (guard hook)")
    for folder in (claude / "skills", claude / "agents", claude):
        if folder.is_dir() and not any(folder.iterdir()):
            folder.rmdir()
    return removed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--project", default=".", help="the project to install into (default: current directory)")
    parser.add_argument("--uninstall", action="store_true")
    args = parser.parse_args(argv)
    project = Path(args.project).resolve()
    if not project.is_dir():
        raise SystemExit(f"{project} is not a directory")
    if args.uninstall:
        removed = uninstall(project)
        print("removed:\n  " + "\n  ".join(removed) if removed else "nothing to remove")
        return 0
    written = install(project)
    print(f"crew installed into {project}:")
    for item in written:
        print("  " + item)
    print("\nStart a new Claude Code session in the project and run /crew.")
    print("To share it with everyone who opens the repository (and with cloud sessions), commit it:")
    print("  git add .claude/skills/crew .claude/agents/crew-*.md .claude/settings.json")
    print("If the project ignores .claude/, add the files with `git add -f` or adjust the ignore rule.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
