"""The proof harness: every gate is exercised against a real git repository."""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import pytest

from conftest import CALC_FIXED, DIV_CMD, DIV_TEST, SUITE, commit_all, git

import crew


# ------------------------------------------------------------------ pure logic

@pytest.mark.parametrize("pattern,path,expected", [
    (".env", ".env", True),
    (".env", "services/api/.env", True),
    (".env", "src/env.py", False),
    (".env.*", ".env.production", True),
    ("*.pem", "certs/server.pem", True),
    ("migrations/", "app/migrations/0001_init.py", True),
    ("migrations/", "docs/migrations.md", False),
    ("src/api/**", "src/api/v1/users.py", True),
    ("src/api/**", "src/apix/users.py", False),
    ("src/api", "src/api/users.py", True),
    ("src/*.py", "src/a.py", True),
    ("src/*.py", "src/sub/a.py", False),
    ("**/fixtures/*.json", "tests/fixtures/a.json", True),
    ("calc.py", "calc.py", True),
    ("calc.py", "lib/calc.py", True),
    ("/calc.py", "lib/calc.py", False),
    ("a[0-9].txt", "a7.txt", True),
])
def test_glob_semantics(pattern, path, expected):
    assert bool(crew.glob_to_re(pattern).match(path)) is expected


def test_protected_paths_respect_exceptions_and_allow_list():
    rules = {"never_touch": ["infra/"], "allow_touch": ["db/migrate/"]}
    assert crew.protected_hit(".env", rules)
    assert crew.protected_hit("config/.ENV.local", rules), "secrets match regardless of case"
    assert crew.protected_hit(".env.example", rules) is None
    assert crew.protected_hit("infra/main.tf", rules) == "infra/"
    assert crew.protected_hit("db/migrate/001.rb", rules) is None, "the user's yes lifts a built-in fence"
    assert crew.protected_hit("app/billing/invoice.py", rules)
    assert crew.protected_hit("src/app.py", rules) is None
    assert crew.protected_hit(".crew/NOTES.md", rules) is None
    assert crew.secret_hit(".env", rules)
    assert crew.secret_hit("app/migrations/0001.py", rules) is None, "fenced paths stay readable"


@pytest.mark.parametrize("path,expected", [
    ("tests/test_x.py", True), ("src/__tests__/a.ts", True), ("pkg/foo_test.go", True),
    ("web/a.spec.tsx", True), ("src/FooTest.java", True), ("spec/models/user_spec.rb", True),
    ("src/contest.py", False), ("src/latest.ts", False), ("src/testimony.py", False),
])
def test_is_test_path(path, expected):
    assert crew.is_test_path(path) is expected


# ------------------------------------------------------------------ init/rules

def test_init_hides_state_from_git_and_writes_a_working_wrapper(project):
    assert git(project.root, "status", "--porcelain") == ""
    assert (project.root / ".crew" / "TASKS.md").exists()
    assert not (project.root / ".gitignore").exists(), "the repo's own ignore file is left alone"
    wrapper = project.root / ".crew" / "bin" / "crew"
    import subprocess
    out = subprocess.run([str(wrapper), "where"], cwd=project.root, capture_output=True, text=True)
    assert out.returncode == 0 and json.loads(out.stdout)["root"] == str(project.root.resolve())


def test_rules_are_rejected_until_done_and_suite_are_set(project):
    path = project.root / ".crew" / "rules.json"
    rules = json.loads(path.read_text())
    rules["commands"]["suite"] = ""
    rules["done"] = ""
    path.write_text(json.dumps(rules))
    proc = project.crew("rules")
    assert proc.returncode == 1
    assert "commands.suite" in proc.stdout and '"done" is empty' in proc.stdout


def test_baseline_green_then_red_halts_new_work(project):
    assert "baseline green" in project.ok("baseline")
    project.set_rules(commands={"suite": f'"{sys.executable}" -c "import sys; print(\'boom\'); sys.exit(3)"'})
    proc = project.crew("baseline")
    assert proc.returncode == 1 and "boom" in proc.stdout
    assert "baseline is red" in project.state["run"]["halt"]
    spec = project.root / ".crew" / "s.md"
    spec.write_text("x\n")
    project.ok("add", "9", "--title", "x", "--spec-file", str(spec))
    proc = project.crew("start", "9")
    assert proc.returncode == 2 and "HALT" in proc.stderr


def test_commands_run_from_a_linked_worktree_find_the_main_state(project):
    wt = project.add_issue("1")
    out = project.ok("status", cwd=wt)
    assert "1 " in out and "started" in out


# ------------------------------------------------------------------ happy path

def test_red_to_green_to_reviewed_pr_body_and_brief(project):
    project.add_issue("1", owns=["calc.py"])
    sealed = project.break_div()
    assert "RED CONFIRMED" in sealed
    assert project.issue("1")["status"] == "red"
    project.fix_div()
    assert project.crew("check", "1").returncode == 0
    proved = project.ok("prove", "1")
    assert "VERDICT: PROVEN" in proved
    issue = project.issue("1")
    assert issue["status"] == "proven"
    proof = json.loads((project.root / issue["proof"]["file"]).read_text())
    assert proof["verdict"] == "pass"
    assert {g["gate"] for g in proof["gates"]} >= {
        "committed", "sealed-tests", "never-touch", "lane", "tests-deleted", "tests-skipped",
        "tests-removed", "red-on-base", "green-on-head", "full-suite"}
    assert proof["runs"]["green"]["exit"] == 0 and proof["runs"]["suite"]["exit"] == 0
    assert issue["tests"]["red"]["exit"] != 0

    assert project.approve().returncode == 0
    assert project.issue("1")["status"] == "approved"
    body = project.ok("pr-body", "1")
    for needle in ("div raises ValueError on a zero divisor", "**Closes:** #1", "**Proving test:**",
                   "**Risk:** low", "**Rollback:**", "### Output before", "### Output after",
                   "### Files touched", "`calc.py`", "`tests/test_div.py`", "### WHY THIS COULD BREAK"):
        assert needle in body, needle
    assert "AssertionError" in body or "ZeroDivisionError" in body, "the red output is quoted, not paraphrased"

    project.ok("pr", "1", "--url", "https://example.test/pr/1")
    brief = project.ok("brief")
    assert "| 1: div by zero raises ValueError |" in brief and "| proven |" in brief
    assert "- merge: 1" in brief and "WHAT STILL LEAKS?" in brief
    assert (project.root / ".crew" / "BRIEF.md").exists()
    assert git(project.root, "status", "--porcelain") == "", "the main checkout is untouched"


def test_proof_trees_are_removed_after_every_run(project):
    project.add_issue("1")
    project.break_div()
    project.fix_div()
    project.ok("prove", "1")
    leftovers = list((project.root / ".crew" / "proof").glob("*")) if (project.root / ".crew" / "proof").exists() else []
    assert leftovers == []
    assert git(project.root, "worktree", "list").count("\n") == 1, "only the main checkout and the issue worktree remain"


# ------------------------------------------------------------------------ seal

def test_seal_rejects_a_test_that_passes_on_the_base(project):
    wt = project.add_issue("1")
    (wt / "tests" / "test_div.py").write_text(DIV_TEST.replace("div(1, 0)", "raise ValueError()"))
    commit_all(wt, "test: not really a failing test")
    proc = project.crew("seal", "1", "--cmd", DIV_CMD, "--test-file", "tests/test_div.py")
    assert proc.returncode == 1 and "does not capture the issue" in proc.stdout
    issue = project.issue("1")
    assert issue["status"] == "started" and issue["rejections"][0]["by"] == "seal"


def test_seal_needs_a_committed_test_and_a_runnable_command(project):
    wt = project.add_issue("1")
    (wt / "tests" / "test_div.py").write_text(DIV_TEST)
    proc = project.crew("seal", "1", "--cmd", DIV_CMD, "--test-file", "tests/test_div.py")
    assert proc.returncode == 2 and "commit the proving test first" in proc.stderr
    commit_all(wt, "test: div")
    proc = project.crew("seal", "1", "--cmd", "crew-no-such-binary-xyz tests", "--test-file", "tests/test_div.py")
    assert proc.returncode == 1 and "could not be executed" in proc.stdout
    proc = project.crew("seal", "1", "--cmd", DIV_CMD, "--test-file", "tests/missing.py")
    assert proc.returncode == 2 and "does not exist" in proc.stderr


def test_seal_runs_red_on_base_plus_tests_only_ignoring_other_branch_changes(project):
    wt = project.add_issue("1")
    (wt / "tests" / "test_div.py").write_text(DIV_TEST)
    (wt / "calc.py").write_text(CALC_FIXED)
    commit_all(wt, "test and fix together")
    out = project.ok("seal", "1", "--cmd", DIV_CMD, "--test-file", "tests/test_div.py")
    assert "RED CONFIRMED" in out and "left out of the red run: calc.py" in out


def test_a_timeout_is_neither_green_nor_a_valid_red(project):
    project.set_rules(limits={"command_timeout_s": 1})
    wt = project.add_issue("1")
    (wt / "tests" / "test_div.py").write_text(DIV_TEST)
    commit_all(wt, "test: div")
    proc = project.crew("seal", "1", "--cmd", f'"{sys.executable}" -c "import time; time.sleep(30)"',
                        "--test-file", "tests/test_div.py")
    assert proc.returncode == 1 and "timed out" in proc.stdout


def test_unseal_reopens_the_proving_test(project):
    project.add_issue("1")
    project.break_div()
    project.ok("unseal", "1", "--reason", "asserted the wrong exception")
    issue = project.issue("1")
    assert issue["status"] == "started" and "tests" not in issue


# ----------------------------------------------------------------------- prove

def _proof(project, issue_id="1"):
    proc = project.crew("prove", issue_id)
    data = json.loads((project.root / project.issue(issue_id)["proof"]["file"]).read_text())
    return proc, {g["gate"]: g for g in data["gates"]}


def test_prove_rejects_an_edited_sealed_test(project):
    wt = project.add_issue("1")
    project.break_div()
    (wt / "tests" / "test_div.py").write_text(DIV_TEST.replace("div(1, 0)", "raise ValueError()"))
    commit_all(wt, "make the test pass by editing the test")
    proc, gates = _proof(project)
    assert proc.returncode == 1 and gates["sealed-tests"]["status"] == "fail"
    assert gates["green-on-head"]["status"] == "pass", "the weakened test passes, which is why the hash gate exists"
    assert project.issue("1")["status"] == "red"


def test_prove_rejects_deleted_skipped_and_removed_tests(project):
    wt = project.add_issue("1")
    project.break_div()
    project.fix_div()
    test_calc = wt / "tests" / "test_calc.py"
    original = test_calc.read_text()

    test_calc.write_text(original.replace("    def test_div(self):", "    @unittest.skip('later')\n    def test_div(self):"))
    commit_all(wt, "skip a test")
    proc, gates = _proof(project)
    assert proc.returncode == 1 and gates["tests-skipped"]["status"] == "fail"

    test_calc.write_text(original.split("    def test_div")[0])
    commit_all(wt, "remove a test case")
    proc, gates = _proof(project)
    assert proc.returncode == 1 and gates["tests-removed"]["status"] == "fail"
    assert gates["tests-skipped"]["status"] == "pass"

    git(wt, "rm", "-q", "tests/test_calc.py")
    commit_all(wt, "delete a test file")
    proc, gates = _proof(project)
    assert proc.returncode == 1 and gates["tests-deleted"]["status"] == "fail"


def test_prove_rejects_lane_violations_and_fenced_paths(project):
    wt = project.add_issue("1", owns=["calc.py"])
    project.break_div()
    project.fix_div()
    (wt / "other.py").write_text("X = 1\n")
    (wt / "migrations").mkdir()
    (wt / "migrations" / "001.sql").write_text("select 1;\n")
    commit_all(wt, "wander outside the lane")
    proc, gates = _proof(project)
    assert proc.returncode == 1
    assert gates["lane"]["status"] == "fail" and "other.py" in gates["lane"]["detail"]
    assert gates["never-touch"]["status"] == "fail" and "migrations/001.sql" in gates["never-touch"]["detail"]

    project.ok("own", "1", "--add", "other.py", "migrations/")
    project.set_rules(allow_touch=["migrations/"])
    proc, gates = _proof(project)
    assert proc.returncode == 0, proc.stdout
    assert gates["lane"]["status"] == "pass" and gates["never-touch"]["status"] == "pass"


def test_prove_rejects_a_red_suite_then_records_how_it_was_resolved(project):
    wt = project.add_issue("1")
    project.break_div()
    (wt / "calc.py").write_text(CALC_FIXED.replace("return a + b", "return a - b"))
    commit_all(wt, "fix div but break add")
    proc, gates = _proof(project)
    assert proc.returncode == 1
    assert gates["green-on-head"]["status"] == "pass" and gates["full-suite"]["status"] == "fail"
    assert "test_add" in proc.stdout, "the failing output is shown to the builder verbatim"
    rejection = project.issue("1")["rejections"][-1]
    assert rejection["by"] == "prover" and rejection["resolved"] == ""

    project.fix_div()
    assert project.crew("prove", "1").returncode == 0
    rejection = project.issue("1")["rejections"][-1]
    assert rejection["resolved"].startswith("re-proven at ")
    brief = project.ok("brief")
    assert "by prover" in brief and "re-proven at" in brief


def test_prove_only_sees_what_is_committed(project):
    wt = project.add_issue("1")
    project.break_div()
    (wt / "calc.py").write_text(CALC_FIXED)
    proc, gates = _proof(project)
    assert proc.returncode == 1
    assert gates["committed"]["status"] == "fail"
    assert gates["green-on-head"]["status"] == "fail", "the clean checkout does not contain the uncommitted fix"


def test_a_new_commit_invalidates_the_proof(project):
    wt = project.add_issue("1")
    project.break_div()
    project.fix_div()
    project.ok("prove", "1")
    (wt / "calc.py").write_text(CALC_FIXED + "\n# tweak after proof\n")
    commit_all(wt, "tweak after proof")
    proc = project.approve()
    assert proc.returncode == 2 and "no passing proof" in proc.stderr
    assert project.crew("pr-body", "1").returncode == 2


def test_recheck_red_reruns_the_proving_test_on_the_base(project):
    project.add_issue("1")
    project.break_div()
    project.fix_div()
    out = project.ok("prove", "1", "--recheck-red")
    assert "red-on-base" in out
    assert list((project.root / ".crew" / "evidence" / "1").glob("proof-*-red.log"))


def test_extra_checks_are_gates(project):
    project.set_rules(commands={"checks": {"lint": f'"{sys.executable}" -c "import sys; print(\'lint failed\'); sys.exit(1)"'}})
    project.add_issue("1")
    project.break_div()
    project.fix_div()
    proc, gates = _proof(project)
    assert proc.returncode == 1 and gates["check:lint"]["status"] == "fail" and "lint failed" in proc.stdout


def test_setup_runs_in_the_clean_checkout(project):
    project.set_rules(commands={"setup": f'"{sys.executable}" -c "open(\'SETUP_RAN\', \'w\').write(\'1\')"',
                                "suite": f'"{sys.executable}" -c "import os, sys; sys.exit(0 if os.path.exists(\'SETUP_RAN\') else 7)"'})
    project.add_issue("1")
    project.break_div()
    project.fix_div()
    assert project.crew("prove", "1").returncode == 0
    assert not (project.root / "SETUP_RAN").exists()


# ---------------------------------------------------------------------- review

def test_review_needs_every_pr_field_and_a_reason_for_new_dependencies(project):
    wt = project.add_issue("1")
    project.break_div()
    project.fix_div()
    (wt / "requirements.txt").write_text("requests==2.32.0\n")
    commit_all(wt, "add a dependency")
    proc, gates = _proof(project)
    assert proc.returncode == 0 and gates["dependencies"]["status"] == "warn"
    proc = project.approve(why_break="")
    assert proc.returncode == 2 and "why_break" in proc.stderr
    proc = project.approve()
    assert proc.returncode == 2 and "deps_reason" in proc.stderr
    assert project.approve(deps_reason="requests is needed for the retry adapter").returncode == 0
    body = project.ok("pr-body", "1")
    assert "### New dependencies" in body and "retry adapter" in body and "WARN dependencies" in body


def test_a_rejecting_review_sends_the_issue_back(project):
    project.add_issue("1")
    project.break_div()
    project.fix_div()
    project.ok("prove", "1")
    path = project.root / ".crew" / "r.json"
    path.write_text(json.dumps({"verdict": "reject", "findings": ["div(0, 0) is not covered"]}))
    proc = project.crew("review", "1", "--file", str(path))
    assert proc.returncode == 1
    issue = project.issue("1")
    assert issue["status"] == "red" and issue["rejections"][-1]["by"] == "reviewer"
    path.write_text(json.dumps({"verdict": "reject", "findings": []}))
    project.ok("prove", "1")
    assert project.crew("review", "1", "--file", str(path)).returncode == 2


def test_waivers_are_limited_to_named_gates_and_show_in_the_pr(project):
    wt = project.add_issue("1")
    project.break_div()
    project.fix_div()
    git(wt, "rm", "-q", "tests/test_calc.py")
    commit_all(wt, "delete an obsolete test")
    assert project.crew("prove", "1").returncode == 1
    assert project.crew("waive", "1", "--gate", "full-suite", "--reason", "no").returncode == 2
    project.ok("waive", "1", "--gate", "tests-deleted", "--reason", "user approved: test_calc is obsolete")
    proc, gates = _proof(project)
    assert proc.returncode == 0 and gates["tests-deleted"]["status"] == "waived"
    project.approve()
    assert "Waivers granted by the user" in project.ok("pr-body", "1")


# -------------------------------------------------------------- single branch

def _approved(project, issue_id, filename, owns=None):
    """A complete proven and approved issue that adds one function in its own file."""
    wt = project.add_issue(issue_id, title=f"add {filename}", owns=owns or [f"{filename}.py"])
    (wt / "tests" / f"test_{filename}.py").write_text(
        f"import unittest\nfrom {filename} import value\n\n\nclass T(unittest.TestCase):\n"
        f"    def test_value(self):\n        self.assertEqual(value(), '{filename}')\n")
    commit_all(wt, f"test: {filename}")
    cmd = f'"{sys.executable}" -m unittest tests.test_{filename} -q'
    project.ok("seal", issue_id, "--cmd", cmd, "--test-file", f"tests/test_{filename}.py")
    (wt / f"{filename}.py").write_text(f"def value():\n    return '{filename}'\n")
    commit_all(wt, f"feat: {filename}")
    project.ok("prove", issue_id)
    assert project.approve(issue_id).returncode == 0
    return wt


def test_single_branch_delivery_lands_reproves_and_refuses_conflicts(project):
    project.set_rules(delivery={"mode": "single-branch", "branch": "main"})
    project.ok("rules")
    base = git(project.root, "rev-parse", "main")
    _approved(project, "a", "alpha")
    _approved(project, "b", "beta")
    wt_c = project.add_issue("c", title="conflicting alpha", owns=["alpha.py"])

    out = project.ok("land", "a")
    assert "fast-forwarded" in out and project.issue("a")["status"] == "landed"
    assert git(project.root, "rev-parse", "main") != base
    assert (project.root / "alpha.py").exists()

    out = project.ok("land", "b")
    log = json.loads((project.root / project.issue("b")["landed"]["file"]).read_text())
    assert set(log["runs"]["proving"]) == {"a", "b"}, "earlier proving tests are re-run on the integrated head"
    assert (project.root / "beta.py").exists()

    (wt_c / "tests" / "test_gamma.py").write_text(
        "import unittest\nfrom alpha import value\n\n\nclass T(unittest.TestCase):\n"
        "    def test_value(self):\n        self.assertEqual(value(), 'gamma')\n")
    commit_all(wt_c, "test: gamma")
    project.ok("seal", "c", "--cmd", f'"{sys.executable}" -m unittest tests.test_gamma -q', "--test-file", "tests/test_gamma.py")
    (wt_c / "alpha.py").write_text("def value():\n    return 'gamma'\n")
    commit_all(wt_c, "feat: a different alpha")
    project.ok("prove", "c")
    project.approve("c")
    before = git(project.root, "rev-parse", "main")
    proc = project.crew("land", "c")
    assert proc.returncode == 1 and "main was not moved" in proc.stdout
    assert git(project.root, "rev-parse", "main") == before
    assert project.issue("c")["status"] == "red" and project.issue("c")["rejections"][-1]["by"] == "land"

    body = project.ok("pr-body", "--all")
    assert "add alpha" in body or "alpha.py" in body
    assert body.count("### WHY THIS COULD BREAK") == 2
    assert git(project.root, "status", "--porcelain") == ""


def test_land_refuses_a_clean_pick_that_turns_the_integrated_suite_red(project):
    """Two issues that are each green alone but red together must not both reach the branch."""
    project.set_rules(delivery={"mode": "single-branch", "branch": "main"})
    project.ok("rules")
    _approved(project, "a", "alpha")
    wt = project.add_issue("b", title="beta assumes no alpha", owns=["beta.py"])
    (wt / "tests" / "test_beta.py").write_text(
        "import os\nimport unittest\nfrom beta import value\n\n\nclass T(unittest.TestCase):\n"
        "    def test_value(self):\n        self.assertEqual(value(), 'beta')\n"
        "        self.assertFalse(os.path.exists('alpha.py'))\n")
    commit_all(wt, "test: beta")
    project.ok("seal", "b", "--cmd", f'"{sys.executable}" -m unittest tests.test_beta -q', "--test-file", "tests/test_beta.py")
    (wt / "beta.py").write_text("def value():\n    return 'beta'\n")
    commit_all(wt, "feat: beta")
    project.ok("prove", "b")
    project.approve("b")
    project.ok("land", "a")
    before = git(project.root, "rev-parse", "main")
    proc = project.crew("land", "b")
    assert proc.returncode == 1 and "proving test of b" in proc.stdout and "full suite" in proc.stdout
    assert git(project.root, "rev-parse", "main") == before, "a red integration never moves the delivery branch"
    assert not (project.root / "beta.py").exists()
    assert project.issue("b")["status"] == "red"


def test_land_refuses_unapproved_work_and_the_wrong_mode(project):
    project.add_issue("1")
    project.break_div()
    project.fix_div()
    project.ok("prove", "1")
    proc = project.crew("land", "1")
    assert proc.returncode == 2 and "single-branch" in proc.stderr
    project.set_rules(delivery={"mode": "single-branch", "branch": "main"})
    proc = project.crew("land", "1")
    assert proc.returncode == 2 and "only an approved issue can land" in proc.stderr


def test_a_stacked_issue_waits_for_approval_then_starts_from_the_issue_it_builds_on(project):
    project.add_issue("1")
    project.break_div()
    project.fix_div()
    spec = project.root / ".crew" / "s2.md"
    spec.write_text("builds on one\n")
    project.ok("add", "2", "--title", "builds on one", "--spec-file", str(spec), "--on", "1")
    proc = project.crew("start", "2")
    assert proc.returncode == 2 and "builds on issue 1, which is red" in proc.stderr
    project.ok("prove", "1")
    project.approve()
    project.ok("start", "2")
    assert (project.wt("2") / "tests" / "test_div.py").exists()
    assert project.issue("2")["base"] == git(project.root, "rev-parse", project.issue("1")["branch"])


# ------------------------------------------------------------- waves and graph

def _queue(project, issue_id, owns):
    spec = project.root / ".crew" / f"s-{issue_id}.md"
    spec.write_text("x\n")
    project.ok("add", issue_id, "--title", f"issue {issue_id}", "--spec-file", str(spec), "--owns", *owns)


def test_waves_separate_overlapping_lanes_and_cap_parallelism(project):
    (project.root / "app.py").write_text("import calc\n")
    (project.root / "util.py").write_text("X = 1\n")
    (project.root / "extra.py").write_text("Y = 1\n")
    commit_all(project.root, "more files")
    project.set_rules(limits={"max_builders": 2})
    _queue(project, "a", ["calc.py"])
    _queue(project, "b", ["calc.py", "app.py"])
    _queue(project, "c", ["util.py"])
    _queue(project, "d", ["extra.py"])
    out = project.ok("waves")
    waves = {i: project.issue(i)["wave"] for i in "abcd"}
    assert waves["a"] != waves["b"], out
    assert "a x b: both own calc.py" in out
    sizes = [list(waves.values()).count(w) for w in set(waves.values())]
    assert max(sizes) <= 2


def test_an_issue_without_a_lane_never_shares_a_wave(project):
    _queue(project, "a", ["calc.py"])
    spec = project.root / ".crew" / "s-b.md"
    spec.write_text("x\n")
    project.ok("add", "b", "--title", "no lane", "--spec-file", str(spec))
    out = project.ok("waves")
    assert project.issue("a")["wave"] != project.issue("b")["wave"]
    assert "independence is unknown" in out


GRAPHIFY = shutil.which("graphify") or str(Path(sys.executable).with_name("graphify"))


@pytest.mark.skipif(not Path(GRAPHIFY).exists(), reason="graphify CLI not installed")
def test_graph_build_and_graph_coupling_split_dependent_lanes(project):
    (project.root / "app.py").write_text("from calc import add\n\n\ndef run():\n    return add(1, 2)\n")
    (project.root / "util.py").write_text("def helper():\n    return 1\n")
    commit_all(project.root, "app depends on calc")
    project.env["CREW_GRAPHIFY"] = GRAPHIFY
    info = json.loads(project.ok("graph"))
    assert info["built"] and Path(info["graph"]).exists()
    assert git(project.root, "status", "--porcelain") == "", "graphify-out is hidden from git"
    assert "app.py" in crew.affected_files(project.root, GRAPHIFY, "calc.py")
    _queue(project, "a", ["calc.py"])
    _queue(project, "b", ["app.py"])
    _queue(project, "c", ["util.py"])
    out = project.ok("waves")
    assert project.issue("a")["wave"] != project.issue("b")["wave"], out
    assert "graph: one lane depends on the other" in out
    assert project.issue("c")["wave"] == 1


def test_graph_reports_unavailable_without_failing_the_run(project):
    project.env["PATH"] = os.pathsep.join(p for p in project.env["PATH"].split(os.pathsep) if not Path(p, "graphify").exists())
    proc = project.crew("graph")
    if shutil.which("graphify", path=project.env["PATH"]):
        pytest.skip("graphify is on every PATH entry")
    assert proc.returncode == 1 and json.loads(proc.stdout)["available"] is False


# ------------------------------------------------------------ parking, budget

def test_park_unpark_block_and_the_brief_names_one_decision(project):
    project.add_issue("1")
    project.add_issue("2", title="unclear")
    project.add_issue("3", title="impossible")
    project.break_div()
    project.fix_div()
    project.ok("prove", "1")
    project.approve()
    q = project.root / ".crew" / "q.md"
    q.write_text("Should div(0, 0) return 0 or raise?\n")
    project.ok("park", "2", "--question-file", str(q))
    project.ok("block", "3", "--reason", "needs a staging database")
    brief = project.ok("brief")
    assert "| parked | question: Should div(0, 0) return 0 or raise? |" in brief
    assert "- merge: 1" in brief and "- retry: 2" in brief and "- drop: 3" in brief
    tasks = (project.root / ".crew" / "TASKS.md").read_text()
    assert "needs the user's answer" in tasks
    project.ok("unpark", "2", "--answer", "raise")
    assert project.issue("2")["status"] == "started"


def test_budget_reports_measured_time_logged_tokens_and_exit_3_when_over(project):
    project.ok("log", "--role", "builder", "--issue", "1", "--tokens", "120000", "--ms", "60000")
    project.ok("log", "--role", "prover", "--tokens", "8000")
    out = project.ok("budget")
    assert "128,000" in out and "builder" in out and "not priced" in out and "within budget" in out
    project.set_rules(budget={"hours": 0.0000001})
    proc = project.crew("budget")
    assert proc.returncode == 3 and "OVER BUDGET" in proc.stdout
    project.set_rules(budget={"hours": None, "deadline": "2001-01-01T00:00:00+00:00"})
    proc = project.crew("budget")
    assert proc.returncode == 3 and "deadline" in proc.stdout


def test_stop_pause_resume_and_unknown_issue(project):
    project.ok("pause")
    assert project.state["run"]["status"] == "paused"
    project.ok("resume")
    assert project.state["run"]["status"] == "active"
    project.ok("stop")
    assert project.state["run"]["status"] == "stopped"
    proc = project.crew("prove", "nope")
    assert proc.returncode == 2 and "unknown issue" in proc.stderr


def test_issue_ids_are_validated(project):
    spec = project.root / ".crew" / "s.md"
    spec.write_text("x\n")
    proc = project.crew("add", "../evil", "--title", "x", "--spec-file", str(spec))
    assert proc.returncode == 2 and "issue id must match" in proc.stderr


# ------------------------------------------------- zero tests, reject, plan

def test_a_green_run_that_executed_no_test_is_rejected(project):
    wt = project.add_issue("1")
    (wt / "tests" / "test_div.py").write_text(DIV_TEST)
    commit_all(wt, "test: div")
    selector = f'"{sys.executable}" -m unittest -k no_such_test_name -q'
    project.ok("seal", "1", "--cmd", f'{selector}; "{sys.executable}" -m unittest tests.test_div -q',
               "--test-file", "tests/test_div.py")
    state_path = project.root / ".crew" / "state.json"
    state = json.loads(state_path.read_text())
    state["issues"]["1"]["tests"]["command"] = f'"{sys.executable}" -c "print(\'Ran 0 tests in 0.000s\'); print(\'OK\')"'
    state_path.write_text(json.dumps(state))
    project.fix_div()
    proc, gates = _proof(project)
    assert proc.returncode == 1
    assert gates["green-on-head"]["status"] == "pass" and gates["tests-ran"]["status"] == "fail"
    assert "Ran 0 tests" in gates["tests-ran"]["detail"]


@pytest.mark.parametrize("line,empty", [
    ("Ran 0 tests in 0.000s", True), ("collected 0 items", True), ("no tests ran in 0.01s", True),
    ("No tests found, exiting with code 0", True), ("?   example.com/pkg [no test files]", True),
    ("testing: warning: no tests to run", True), ("  0 passing (2ms)", True), ("Tests:       0 total", True),
    ("running 0 tests", True), ("0 examples, 0 failures", True),
    ("Ran 12 tests in 0.004s", False), ("collected 10 items", False), ("10 passing (20ms)", False),
    ("Tests:       3 passed, 3 total", False), ("running 20 tests", False), ("30 examples, 0 failures", False),
])
def test_zero_test_markers(line, empty):
    assert bool(crew._NO_TESTS_RE.search(line)) is empty


def test_a_judgement_rejection_is_recorded_and_sends_the_issue_back(project):
    project.add_issue("1")
    project.break_div()
    project.fix_div()
    project.ok("prove", "1")
    reason = project.root / ".crew" / "why.md"
    reason.write_text("the proving test fails on an ImportError, not on the behaviour in the spec\n")
    project.ok("reject", "1", "--by", "prover", "--reason-file", str(reason))
    issue = project.issue("1")
    assert issue["status"] == "red" and issue["rejections"][-1]["by"] == "prover"
    assert "ImportError" in issue["rejections"][-1]["reason"]
    assert project.approve().returncode == 2, "a rejected proof cannot be approved"
    assert "UNRESOLVED" in project.ok("brief")
    assert project.crew("reject", "1", "--by", "builder", "--reason", "x").returncode == 2


def test_plan_shows_rules_baseline_crew_and_waves_and_fails_without_a_baseline(project):
    _queue(project, "a", ["calc.py"])
    proc = project.crew("plan")
    assert proc.returncode == 1 and "not run yet" in proc.stdout
    project.ok("baseline")
    (project.root / ".crew" / "host.json").write_text(json.dumps({"permission_mode": "default"}))
    out = project.ok("plan")
    for needle in ("TEAM RULES", "BASELINE", "green at", "CREW", "Breaker", "Reviewer", "wave 1: a",
                   "permission mode is 'default'"):
        assert needle in out, needle


# ------------------------------------------ checkpoints, run end, publishable output

def test_every_transition_is_checkpointed_to_notes_by_the_tool(project):
    notes = project.root / ".crew" / "NOTES.md"
    notes.write_text("# Notes\n## Commands\n- full suite: `x`\n")
    project.ok("baseline")
    project.add_issue("1")
    project.break_div()
    project.fix_div()
    project.ok("prove", "1")
    project.approve()
    text = notes.read_text()
    assert text.startswith("# Notes\n## Commands\n- full suite: `x`\n"), "the Mapper's notes are kept"
    log = text.split("## Log\n", 1)[1].splitlines()
    events = [line.split("Z ", 1)[1] for line in log]
    assert events[0].startswith("baseline green at ")
    for expected in ("issue 1 added", "issue 1 started: branch crew/1-", "issue 1 sealed: red exit 1",
                     "issue 1 proven: ", "issue 1 approved: "):
        assert any(e.startswith(expected) for e in events), (expected, events)
    assert all(line.startswith("- 20") for line in log)
    project.ok("stop")
    assert notes.read_text().rstrip().endswith("run stopped")


def test_the_brief_ends_the_run_and_removes_delivered_worktrees_only(project):
    project.set_rules(delivery={"mode": "single-branch", "branch": "main"})
    project.ok("rules")
    _approved(project, "a", "alpha")
    wt_b = _approved(project, "b", "beta")
    project.add_issue("c", title="still open")
    project.ok("land", "a")
    project.ok("land", "b")
    (wt_b / "scratch.txt").write_text("uncommitted\n")
    out = project.ok("brief")
    assert "run is done; the guard hook stands down" in out
    assert "removed the worktrees of delivered issues a" in out
    assert "kept worktrees with uncommitted files: b" in out
    run = project.state["run"]
    assert run["status"] == "done" and run["ended_at"]
    assert not project.wt("a").exists() and project.wt("b").exists() and project.wt("c").exists()
    branches = git(project.root, "branch", "--list", "crew/*")
    assert "crew/a-add-alpha" in branches and "crew/b-add-beta" in branches, "branches are never deleted"
    assert "landed as " in out
    again = project.ok("brief")
    assert "run is done" not in again, "a second brief does not end the run twice"
    project.ok("resume")
    assert project.state["run"]["status"] == "active" and "ended_at" not in project.state["run"]


def test_evidence_tails_carry_no_machine_paths(project):
    project.add_issue("1")
    project.break_div()
    project.fix_div()
    project.ok("prove", "1")
    project.approve()
    issue = project.issue("1")
    red_tail = issue["tests"]["red"]["tail"]
    assert "Traceback" in red_tail
    assert 'File "tests/test_div.py", line' in red_tail, "the checkout path is rewritten to a relative one"
    body = project.ok("pr-body", "1")
    import tempfile
    for text in (red_tail, body):
        assert str(project.root) not in text and ".crew/proof" not in text
        assert tempfile.gettempdir() not in text and "crew-red-" not in text and "/checkout/" not in text
    raw_log = (project.root / ".crew" / "evidence" / "1" / "red.log").read_text()
    assert "/checkout/tests/test_div.py" in raw_log, "the full log on disk keeps the real path"


def test_pr_body_names_the_commits_that_actually_landed(project):
    project.set_rules(delivery={"mode": "single-branch", "branch": "main"})
    project.ok("rules")
    before = git(project.root, "rev-parse", "main")
    _approved(project, "a", "alpha")
    project.ok("land", "a")
    landed = git(project.root, "rev-list", "--reverse", "--abbrev-commit", f"{before}..main").split()
    assert len(landed) == 2
    body = project.ok("pr-body", "--all")
    assert "**Commits on main:** " + " ".join(f"`{sha}`" for sha in landed) + " (revert these to roll back)" in body
    branch_head = git(project.root, "rev-parse", "--short", project.issue("a")["branch"])
    assert branch_head not in landed, "cherry-picked commits have new ids, which is why the body lists them"


# ------------------------------------------------ isolation and red baselines

def test_proofs_run_outside_the_repository_tree(project):
    """A checkout nested in the repo could resolve the parent's node_modules or config."""
    project.set_rules(commands={"checks": {"where": f'"{sys.executable}" -c "import os; print(\'CWD=\' + os.getcwd())"'}})
    project.add_issue("1")
    project.break_div()
    project.fix_div()
    project.ok("prove", "1")
    logs = list((project.root / ".crew" / "evidence" / "1").glob("proof-*-check-where.log"))
    cwd = Path(logs[0].read_text().split("CWD=", 1)[1].strip().splitlines()[0])
    root = project.root.resolve()
    assert root not in cwd.resolve().parents and cwd.resolve() != root, f"the proof ran inside the repo: {cwd}"
    assert not cwd.exists(), "and the checkout was removed afterwards"
    red_log = (project.root / ".crew" / "evidence" / "1" / "red.log").read_text()
    assert str(root / ".crew" / "proof") not in red_log


def _red_base(project):
    """Put a failing test on main and make it the run's base."""
    (project.root / "tests" / "test_broken.py").write_text(
        "import unittest\n\n\nclass Broken(unittest.TestCase):\n    def test_broken(self):\n        self.assertEqual(1, 2)\n")
    commit_all(project.root, "a test that fails on main")
    project.set_rules(delivery={"base": "main"})
    project.ok("rules")
    assert project.crew("baseline").returncode == 1


def test_only_a_baseline_fix_may_start_on_a_red_base_and_proving_it_lifts_the_halt(project):
    _red_base(project)
    spec = project.root / ".crew" / "s.md"
    spec.write_text("the suite must be green\n")
    project.ok("add", "7", "--title", "ordinary work", "--spec-file", str(spec))
    proc = project.crew("start", "7")
    assert proc.returncode == 2 and "HALT" in proc.stderr
    project.ok("add", "0", "--title", "make the suite green", "--spec-file", str(spec), "--fixes-baseline")
    project.ok("start", "0")
    project.ok("seal", "0", "--cmd", SUITE)
    wt = project.wt("0")
    (wt / "tests" / "test_broken.py").write_text(
        "import unittest\n\n\nclass Broken(unittest.TestCase):\n    def test_broken(self):\n        self.assertEqual(2, 2)\n")
    commit_all(wt, "fix the broken test's expectation")
    assert project.state["run"]["halt"]
    proc = project.crew("prove", "0")
    assert proc.returncode == 1 and "tests-rewritten" in proc.stdout, "even a baseline fix may not quietly rewrite a test"
    project.ok("waive", "0", "--gate", "tests-rewritten", "--reason", "user approved: the test itself was wrong")
    assert project.state["run"]["halt"]
    project.ok("prove", "0")
    run = project.state["run"]
    assert "halt" not in run
    head = git(project.root, "rev-parse", project.issue("0")["branch"])
    assert run["base_sha"] == head, "later issues are cut from the fixed base"
    project.ok("start", "7")
    assert project.issue("7")["base"] == head


def test_init_explains_a_repository_without_commits(tmp_path):
    import subprocess
    from conftest import CREW
    repo = tmp_path / "empty"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    proc = subprocess.run([sys.executable, str(CREW), "init"], cwd=str(repo), capture_output=True, text=True)
    assert proc.returncode == 2 and "no commits" in proc.stderr
    outside = tmp_path / "plain"
    outside.mkdir()
    proc = subprocess.run([sys.executable, str(CREW), "init"], cwd=str(outside), capture_output=True, text=True)
    assert proc.returncode == 2 and "not inside a git repository" in proc.stderr


# -------------------------------------------- who may change what tests expect

@pytest.mark.parametrize("line,is_assertion", [
    ("        self.assertEqual(a, b)", True), ("    assert total == 3", True), ("  expect(sum(1, 2)).toBe(3);", True),
    ("    assert_eq!(a, b);", True), ("\trequire.Equal(t, 1, got)", True), ("\tt.Fatalf(\"got %d\", n)", True),
    ("  EXPECT_EQ(a, b);", True), ("    Assert.AreEqual(1, n);", True), ("    result.should eq(3)", True),
    ("    with pytest.raises(ValueError):", True), ("        with self.assertRaises(ValueError):", True),
    ("const cart = require('./cart');", False), ("# this should never happen", False),
    ("import unittest", False), ("    cart = Cart()", False), ("    verify_ssl = False", False),
])
def test_assertion_lines_are_recognised_without_matching_imports_or_prose(line, is_assertion):
    assert bool(crew._ASSERT_RE.search(line)) is is_assertion


def test_a_builder_may_add_tests_but_not_rewrite_an_existing_assertion(project):
    wt = project.add_issue("1")
    project.break_div()
    project.fix_div()
    test_calc = wt / "tests" / "test_calc.py"
    original = test_calc.read_text()
    test_calc.write_text(original + "\n    def test_add_negative(self):\n        self.assertEqual(add(-1, 1), 0)\n")
    commit_all(wt, "builder adds a test")
    proc, gates = _proof(project)
    assert proc.returncode == 0 and gates["tests-rewritten"]["status"] == "pass"

    test_calc.write_text(original.replace("self.assertEqual(div(6, 3), 2)", "self.assertTrue(div(6, 3))"))
    commit_all(wt, "builder loosens an existing assertion")
    proc, gates = _proof(project)
    assert proc.returncode == 1 and gates["tests-rewritten"]["status"] == "fail"
    assert "Only the Breaker changes what tests expect" in gates["tests-rewritten"]["detail"]
    assert gates["tests-removed"]["status"] == "pass" and gates["green-on-head"]["status"] == "pass"
    project.ok("waive", "1", "--gate", "tests-rewritten", "--reason", "user approved: the old assertion was too strict")
    proc, gates = _proof(project)
    assert proc.returncode == 0 and gates["tests-rewritten"]["status"] == "waived"


def test_a_breaker_rewriting_a_pinned_test_is_flagged_for_the_reviewer(project):
    """The issue changes behaviour an existing test pins: the rewrite must be visible, not silent."""
    wt = project.add_issue("1", title="div by zero returns None")
    test_calc = wt / "tests" / "test_calc.py"
    test_calc.write_text(test_calc.read_text().replace(
        "        self.assertEqual(div(6, 3), 2)", "        self.assertEqual(div(6, 3), 2)\n        self.assertIsNone(div(1, 0))"
    ).replace("self.assertEqual(add(1, 2), 3)", "self.assertEqual(add(2, 2), 4)"))
    commit_all(wt, "test: div by zero returns None; rewrite the add expectation")
    cmd = f'"{sys.executable}" -m unittest tests.test_calc -q'
    project.ok("seal", "1", "--cmd", cmd, "--test-file", "tests/test_calc.py")
    (wt / "calc.py").write_text(CALC_FIXED.replace('raise ValueError("division by zero")', "return None"))
    commit_all(wt, "fix: div returns None on zero")
    proc, gates = _proof(project)
    assert proc.returncode == 0, proc.stdout
    assert gates["assertions"]["status"] == "warn" and "tests/test_calc.py (1)" in gates["assertions"]["detail"]
    assert gates["tests-rewritten"]["status"] == "pass", "the sealed file is the Breaker's to change"
    proc = project.approve()
    assert proc.returncode == 2 and "assertions_reason" in proc.stderr
    assert project.approve(assertions_reason="the issue asks for a different add example").returncode == 0
    assert "### Assertion changes" in project.ok("pr-body", "1")


def test_a_breaker_cannot_seal_a_file_whose_failing_case_was_dropped(project):
    wt = project.add_issue("1")
    test_calc = wt / "tests" / "test_calc.py"
    kept = test_calc.read_text().split("    def test_div")[0]
    test_calc.write_text(kept + "    def test_div_by_zero(self):\n        with self.assertRaises(ValueError):\n            div(1, 0)\n")
    commit_all(wt, "replace test_div with a failing case")
    cmd = f'"{sys.executable}" -m unittest tests.test_calc -q'
    project.ok("seal", "1", "--cmd", cmd, "--test-file", "tests/test_calc.py")
    project.fix_div()
    proc, gates = _proof(project)
    assert proc.returncode == 0, "one case replaced by one case is a rewrite"
    assert gates["assertions"]["status"] == "warn", "and the rewrite is flagged"
    assert gates["tests-removed"]["status"] == "pass"
    project.ok("unseal", "1", "--reason", "revise")
    test_calc.write_text(kept.rstrip() + "\n")
    commit_all(wt, "drop the case entirely")
    proc = project.crew("seal", "1", "--cmd", cmd, "--test-file", "tests/test_calc.py")
    assert proc.returncode == 1 and "does not capture the issue" in proc.stdout


# ------------------------------------------------------------ stacked issues

def test_coupled_issues_are_stacked_when_each_gets_its_own_pr(project):
    _queue(project, "a", ["calc.py"])
    _queue(project, "b", ["calc.py"])
    _queue(project, "c", ["other.py"])
    out = project.ok("waves")
    assert project.issue("b")["on"] == "a" and project.issue("c")["on"] == "" and project.issue("a")["on"] == ""
    assert "b is built on top of a" in out
    proc = project.crew("start", "b")
    assert proc.returncode == 2 and "builds on issue a, which is queued" in proc.stderr


def test_single_branch_delivery_does_not_stack(project):
    project.set_rules(delivery={"mode": "single-branch", "branch": "main"})
    project.ok("rules")
    _queue(project, "a", ["calc.py"])
    _queue(project, "b", ["calc.py"])
    out = project.ok("waves")
    assert project.issue("b")["on"] == "" and "built on top of" not in out
    assert project.issue("a")["wave"] != project.issue("b")["wave"]


def test_a_stacked_pr_says_what_it_is_stacked_on(project):
    _approved(project, "a", "alpha")
    spec = project.root / ".crew" / "sb.md"
    spec.write_text("beta builds on alpha\n")
    project.ok("add", "b", "--title", "add beta", "--spec-file", str(spec), "--on", "a", "--owns", "beta.py")
    project.ok("start", "b")
    wt_b = project.wt("b")
    assert (wt_b / "alpha.py").exists()
    (wt_b / "tests" / "test_beta.py").write_text(
        "import unittest\nfrom alpha import value\nfrom beta import both\n\n\nclass T(unittest.TestCase):\n"
        "    def test_both(self):\n        self.assertEqual(both(), value() + 'beta')\n")
    commit_all(wt_b, "test: beta")
    project.ok("seal", "b", "--cmd", f'"{sys.executable}" -m unittest tests.test_beta -q', "--test-file", "tests/test_beta.py")
    (wt_b / "beta.py").write_text("from alpha import value\n\n\ndef both():\n    return value() + 'beta'\n")
    commit_all(wt_b, "feat: beta")
    project.ok("prove", "b")
    project.approve("b")
    body = project.ok("pr-body", "b")
    assert "**Stacked on:** issue a (`crew/a-add-alpha`). Merge that first" in body
    assert "`beta.py`" in body and "`alpha.py`" not in body, "the diff is shown against the issue it builds on"


def test_single_branch_baseline_fix_lifts_the_halt_when_it_lands(project):
    _red_base(project)
    project.set_rules(delivery={"mode": "single-branch", "branch": "main", "base": "main"})
    project.ok("rules")
    spec = project.root / ".crew" / "s.md"
    spec.write_text("the suite must be green\n")
    project.ok("add", "0", "--title", "make the suite green", "--spec-file", str(spec), "--fixes-baseline")
    project.ok("start", "0")
    project.ok("seal", "0", "--cmd", SUITE)
    wt = project.wt("0")
    (wt / "tests" / "test_broken.py").write_text(
        "import unittest\n\n\nclass Broken(unittest.TestCase):\n    def test_broken(self):\n        self.assertEqual(2, 2)\n")
    commit_all(wt, "fix the broken expectation")
    project.ok("waive", "0", "--gate", "tests-rewritten", "--reason", "user approved: the test itself was wrong")
    project.ok("prove", "0")
    assert project.state["run"]["halt"], "on a single branch the fix is not on the branch until it lands"
    project.approve("0")
    project.ok("land", "0")
    assert "halt" not in project.state["run"]
