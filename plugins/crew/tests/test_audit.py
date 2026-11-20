"""The cost audit: instruction lint, settings, and transcript measurement."""
from __future__ import annotations

import datetime as dt
import json
import subprocess
import sys
from pathlib import Path

from conftest import AUDIT

import audit


def entry(request, model="claude-opus-5-5", effort="high", at="2026-01-01T10:00:00Z", inp=10, write=0, read=0, out=100, thinking=40):
    return {
        "type": "assistant", "requestId": request, "timestamp": at, "effort": effort, "cwd": "/proj",
        "message": {"model": model, "usage": {
            "input_tokens": inp, "cache_creation_input_tokens": write, "cache_read_input_tokens": read,
            "output_tokens": out, "output_tokens_details": {"thinking_tokens": thinking}}},
    }


def user(text="do it", at="2026-01-01T09:59:00Z"):
    return {"type": "user", "timestamp": at, "cwd": "/proj", "message": {"role": "user", "content": text}}


def write_jsonl(path: Path, rows) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return path


def test_lint_finds_filler_and_vague_lines_but_not_code():
    text = "\n".join([
        "# Rules",
        "Always think carefully before editing.",
        "Think step by step.",
        "When working on tasks, always apply the highest level of thinking depth.",
        "We don't mind consuming more tokens if it means a better output.",
        "Be thorough.",
        "Double-check everything before you finish.",
        "Run `uv run pytest tests/ -q` before every commit.",
        "```",
        "think carefully  # inside a code fence, so not a rule",
        "```",
        "Thinking about the problem domain: orders and refunds.",
    ])
    found = audit.lint_text(text)
    assert [(f["line"], f["kind"]) for f in found] == [
        (2, "filler"), (3, "filler"), (4, "filler"), (5, "filler"), (6, "vague"), (7, "vague")]
    assert found[4]["fix"].startswith("name the check")


def test_requests_are_counted_once_per_request_id(tmp_path):
    rows = [user(), entry("r1", out=100), entry("r1", out=100), entry("r1", out=100), entry("r2", out=50)]
    path = write_jsonl(tmp_path / "s.jsonl", rows)
    requests = audit.requests_in(path)
    assert len(requests) == 2 and sum(r["output"] for r in requests) == 150


def test_session_analysis_measures_switches_cold_requests_and_cache_hits(tmp_path):
    rows = [
        user(),
        entry("r1", write=50000, read=0, at="2026-01-01T10:00:00Z"),
        entry("r2", read=50000, at="2026-01-01T10:01:00Z"),
        entry("r3", model="claude-sonnet-5-5", write=51000, read=0, at="2026-01-01T10:02:00Z"),
        entry("r4", model="claude-sonnet-5-5", effort="medium", read=51000, at="2026-01-01T11:00:00Z"),
        {"type": "system", "subtype": "compact_boundary", "timestamp": "2026-01-01T11:00:01Z"},
        {"type": "user", "timestamp": "2026-01-01T10:00:30Z",
         "message": {"content": [{"type": "tool_result", "content": "ok"}]}},
    ]
    result = audit.analyze_session(write_jsonl(tmp_path / "s.jsonl", rows))
    assert result["requests"] == 4 and result["user_turns"] == 1
    assert result["model_switches"] == 1 and result["effort_switches"] == 1
    assert result["cold_requests"] == 1 and result["cold_tokens"] == 51010
    assert result["high_effort_requests"] == 3 and result["compactions"] == 1
    assert result["hours"] == 1.0
    assert result["cache_hit"] == round(101000 / (40 + 101000 + 101000), 3)


def test_usage_is_grouped_by_role_from_subagent_metadata(tmp_path):
    main = write_jsonl(tmp_path / "abc.jsonl", [user(), entry("r1", out=10, inp=5)])
    sub = tmp_path / "abc" / "subagents"
    write_jsonl(sub / "agent-1.jsonl", [entry("s1", model="claude-sonnet-5-5", out=200, inp=20)])
    (sub / "agent-1.meta.json").write_text(json.dumps({"agentType": "crew:builder", "description": "builder 42"}))
    write_jsonl(sub / "agent-2.jsonl", [entry("s2", model="claude-haiku-5-5", out=30, inp=3)])
    (sub / "agent-2.meta.json").write_text(json.dumps({"agentType": "general-purpose", "description": "prover 42"}))
    write_jsonl(sub / "agent-3.jsonl", [entry("s3", out=7, inp=1, at="2025-01-01T00:00:00Z")])
    (sub / "agent-3.meta.json").write_text(json.dumps({"agentType": "crew-reviewer"}))
    since = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    usage = {(r["role"], r["model"]): r for r in audit.transcript_usage(main, since)}
    assert set(usage) == {("lead", "claude-opus-5-5"), ("builder", "claude-sonnet-5-5"), ("prover", "claude-haiku-5-5")}
    assert usage[("builder", "claude-sonnet-5-5")]["total_tokens"] == 220
    assert ("reviewer", "claude-opus-5-5") in {(r["role"], r["model"]) for r in audit.transcript_usage(main)}


def test_price_uses_only_the_rates_the_user_supplied():
    row = {"model": "claude-sonnet-5-5", "input": 1_000_000, "output": 1_000_000, "cache_read": 1_000_000, "cache_write": 0}
    rates = {"sonnet": {"in": 3, "out": 15, "cache_read": 0.3, "cache_write": 3.75}}
    assert audit.price(row, rates) == 18.3
    assert audit.price(row, {"opus": {"in": 1, "out": 1}}) is None
    assert audit.price(row, {}) is None


def _home_with_session(tmp_path: Path, project: Path, rows, agents=()):
    slug = "".join(c if c.isalnum() else "-" for c in str(project))
    folder = tmp_path / "home" / ".claude" / "projects" / slug
    write_jsonl(folder / "sess1.jsonl", rows)
    for index, (kind, model, tokens) in enumerate(agents):
        write_jsonl(folder / "sess1" / "subagents" / f"agent-{index}.jsonl", [entry(f"a{index}", model=model, out=tokens)])
        (folder / "sess1" / "subagents" / f"agent-{index}.meta.json").write_text(json.dumps({"agentType": kind}))
    return tmp_path / "home"


def test_the_report_ranks_measured_leaks_and_lists_exact_lines(tmp_path):
    project = tmp_path / "proj"
    (project / ".claude").mkdir(parents=True)
    (project / "CLAUDE.md").write_text("# Project\nThink carefully.\nBe thorough.\nRun `make test`.\n")
    (project / ".claude" / "settings.json").write_text(json.dumps({"effortLevel": "xhigh", "model": "opus"}))
    rows = [user()] + [entry(f"r{i}", effort="xhigh", out=1000, read=60000 if i else 0, write=60000 if not i else 0,
                             at=f"2026-01-01T10:{i:02d}:00Z") for i in range(6)]
    rows.append(entry("r9", model="claude-sonnet-5-5", effort="xhigh", write=70000, read=0, at="2026-01-01T10:30:00Z"))
    home = _home_with_session(tmp_path, project, rows, agents=[("Explore", "claude-opus-5-5", 9000)])
    data = audit.build(project, home, 5)
    ranked = audit.leaks(data)
    by_name = {r["setting"]: r for r in ranked}
    assert {"default effort", "model and effort mid-session", "model for lookup subagents",
            "instruction-file filler", "vague guardrails"} <= set(by_name)
    effort = by_name["default effort"]
    assert "xhigh" in effort["old"] and effort["new"] == "medium" and "100% of 7 requests" in effort["why"]
    assert effort["exposure"] == (6 * 1000 + 100) * 5.0, "output tokens at high effort, weighted as output"
    cold = by_name["model and effort mid-session"]
    assert "70,010 prompt tokens" in cold["why"] and cold["exposure"] == 70010 * 1.25
    exposures = [r["exposure"] for r in ranked]
    assert exposures == sorted(exposures, reverse=True), "biggest measured exposure first"
    assert ranked[0]["setting"] == "model and effort mid-session", "the cache break outweighs the effort pin here"
    assert by_name["instruction-file filler"]["exposure"] == 0.0, "unmeasured leaks never outrank measured ones"
    report = audit.render(data)
    assert f"`{project / 'CLAUDE.md'}:2`: Think carefully." in report
    assert f"`{project / 'CLAUDE.md'}:3`: Be thorough. -> name the check" in report
    assert "| Setting | Old value | New value | Why |" in report
    assert "## TASKS.md rule to add" in report and report.rstrip().count("## WHAT STILL LEAKS?") == 1
    assert "(measured)" in report


def test_the_cli_runs_with_no_transcripts_and_compares_saved_runs(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    saved = tmp_path / "before.json"
    proc = subprocess.run([sys.executable, str(AUDIT), "--project", str(project), "--home", str(home), "--save", str(saved)],
                          capture_output=True, text=True)
    assert proc.returncode == 0 and "No transcripts found" in proc.stdout and "Nothing was measured" in proc.stdout
    assert json.loads(saved.read_text())["sessions"] == []
    proc = subprocess.run([sys.executable, str(AUDIT), "--project", str(project), "--home", str(home), "--compare", str(saved)],
                          capture_output=True, text=True)
    assert proc.returncode == 0 and "| Metric (latest session) | Before | After |" in proc.stdout


def test_the_brief_reports_measured_tokens_when_the_guard_recorded_a_transcript(project, tmp_path):
    started = project.state["run"]["started_at"]
    main = write_jsonl(tmp_path / "abc.jsonl", [entry("r1", out=1000, inp=500, at=started)])
    sub = tmp_path / "abc" / "subagents"
    write_jsonl(sub / "agent-1.jsonl", [entry("s1", model="claude-sonnet-5-5", out=4000, inp=1000, at=started)])
    (sub / "agent-1.meta.json").write_text(json.dumps({"agentType": "crew:builder"}))
    (project.root / ".crew" / "sessions.json").write_text(json.dumps([str(main)]))
    out = project.ok("budget")
    assert "6,500 (measured from the session transcript)" in out and "builder" in out and "lead" in out
    project.set_rules(rates_usd_per_mtok={"sonnet": {"in": 3, "out": 15}, "opus": {"in": 15, "out": 75}})
    out = project.ok("brief")
    assert "estimate: measured tokens x the rates in the Team Rules, 100% of tokens priced" in out
    assert "builder used 5,000 tokens (76% of the run) over 1 requests; 0 of them were the conversation" in out
