"""Shared fixtures: every test runs the real scripts against a real git repo."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "crew" / "scripts"
CREW = SCRIPTS / "crew.py"
GUARD = SCRIPTS / "guard.py"
AUDIT = SCRIPTS / "audit.py"
INSTALL = SCRIPTS / "install.py"
SUITE = f'"{sys.executable}" -m unittest discover -s tests -q'

sys.path.insert(0, str(SCRIPTS))


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True)
    assert proc.returncode == 0, f"git {' '.join(args)}: {proc.stderr}"
    return proc.stdout.strip()


def commit_all(repo: Path, message: str) -> str:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


class Project:
    """A throwaway repo with a small calculator and one passing test."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("CREW_")}

    def crew(self, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(CREW), *args], cwd=str(cwd or self.root), capture_output=True,
            text=True, env=self.env,
        )

    def ok(self, *args: str, cwd: Path | None = None) -> str:
        proc = self.crew(*args, cwd=cwd)
        assert proc.returncode == 0, f"crew {' '.join(args)} exited {proc.returncode}\n{proc.stdout}\n{proc.stderr}"
        return proc.stdout

    @property
    def state(self) -> dict:
        return json.loads((self.root / ".crew" / "state.json").read_text())

    def issue(self, issue_id: str) -> dict:
        return self.state["issues"][issue_id]

    def set_rules(self, **overrides) -> None:
        path = self.root / ".crew" / "rules.json"
        rules = json.loads(path.read_text())
        rules["done"] = "the proving test passes and the full suite is green"
        rules["commands"]["suite"] = SUITE
        for key, value in overrides.items():
            if isinstance(value, dict) and isinstance(rules.get(key), dict):
                rules[key].update(value)
            else:
                rules[key] = value
        path.write_text(json.dumps(rules, indent=2))

    def wt(self, issue_id: str) -> Path:
        return self.root / ".crew" / "wt" / issue_id

    def add_issue(self, issue_id: str, title: str = "div by zero raises ValueError", owns: list[str] | None = None,
                  on: str | None = None) -> Path:
        spec = self.root / ".crew" / f"spec-{issue_id}.md"
        spec.write_text(f"{title}\n")
        args = ["add", issue_id, "--title", title, "--spec-file", str(spec), "--source", f"#{issue_id}"]
        if owns:
            args += ["--owns", *owns]
        if on:
            args += ["--on", on]
        self.ok(*args)
        self.ok("start", issue_id)
        return self.wt(issue_id)

    def break_div(self, issue_id: str = "1") -> str:
        """Play the Breaker: commit a test that fails on the base, then seal it."""
        wt = self.wt(issue_id)
        (wt / "tests" / "test_div.py").write_text(DIV_TEST)
        commit_all(wt, "test: div by zero must raise ValueError")
        return self.ok("seal", issue_id, "--cmd", DIV_CMD, "--test-file", "tests/test_div.py")

    def fix_div(self, issue_id: str = "1") -> None:
        """Play the Builder: the smallest change that turns the proving test green."""
        wt = self.wt(issue_id)
        (wt / "calc.py").write_text(CALC_FIXED)
        commit_all(wt, "fix: div raises ValueError on zero divisor")

    def approve(self, issue_id: str = "1", **extra) -> subprocess.CompletedProcess:
        review = {
            "verdict": "approve", "summary": "div raises ValueError on a zero divisor", "risk": "low",
            "risk_reason": "one guarded branch", "rollback": "revert the fix commit",
            "why_break": "callers that caught ZeroDivisionError no longer catch this error",
        }
        review.update(extra)
        path = self.root / ".crew" / f"review-{issue_id}.json"
        path.write_text(json.dumps(review))
        return self.crew("review", issue_id, "--file", str(path))


CALC = '''def add(a, b):
    return a + b


def div(a, b):
    return a / b
'''

CALC_FIXED = '''def add(a, b):
    return a + b


def div(a, b):
    if b == 0:
        raise ValueError("division by zero")
    return a / b
'''

CALC_TEST = '''import unittest

from calc import add, div


class CalcTest(unittest.TestCase):
    def test_add(self):
        self.assertEqual(add(1, 2), 3)

    def test_div(self):
        self.assertEqual(div(6, 3), 2)
'''

DIV_TEST = '''import unittest

from calc import div


class DivByZeroTest(unittest.TestCase):
    def test_div_by_zero_raises_value_error(self):
        with self.assertRaises(ValueError):
            div(1, 0)
'''

DIV_CMD = f'"{sys.executable}" -m unittest tests.test_div -q'


@pytest.fixture
def project(tmp_path: Path) -> Project:
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "dev@example.com")
    git(root, "config", "user.name", "Dev")
    git(root, "config", "commit.gpgsign", "false")
    (root / "calc.py").write_text(CALC)
    (root / "tests" / "__init__.py").write_text("")
    (root / "tests" / "test_calc.py").write_text(CALC_TEST)
    commit_all(root, "initial")
    proj = Project(root)
    proj.ok("init")
    proj.set_rules()
    proj.ok("rules")
    return proj
