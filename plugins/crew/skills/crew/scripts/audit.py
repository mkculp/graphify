#!/usr/bin/env python3
"""Cost audit for a Claude Code setup: measure first, then name the leaks.

Reads three things and changes none of them:
  * instruction files (CLAUDE.md, AGENTS.md): filler lines that raise effort on
    every turn, and vague guardrails that should be named rules;
  * settings: a pinned model or effort level;
  * session transcripts: tokens actually spent, cache hits, model and effort
    switches, session length, and what subagents cost by type.

Token counts come from the transcripts and are measured. Anything derived from
price ratios is labelled an estimate. Standard library only.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Iterable, Iterator

FILLER = [
    r"\bultrathink\b",
    r"\bthink(ing)?\s+(very\s+|really\s+|extra\s+|more\s+)?(carefully|hard(er)?|deep(ly|er)?|thoroughly|long(er)?)\b",
    r"\bthink\s+(it\s+|this\s+)?(through\s+)?step[- ]by[- ]step\b",
    r"\btake\s+(a\s+deep\s+breath|your\s+time)\b",
    r"\b(highest|maximum|max|maximal|deepest)\s+(possible\s+)?(level\s+of\s+)?(thinking|reasoning|effort)\b",
    r"\bthinking\s+depth\b",
    r"\b(don'?t|do\s+not)\s+mind\s+(consuming|spending|using|burning)\s+more\s+tokens\b",
    r"\breasoning\s+loops?\b",
]
VAGUE = [
    (r"\bbe\s+(very\s+|extra\s+)?(thorough|careful|meticulous|diligent|rigorous)\b",
     "name the check: which command must pass, which file must be read first"),
    (r"\bdouble[- ]check\s+(everything|all|your\s+work)\b",
     "name what to re-run: the test command, the type check, the diff"),
    (r"\b(don'?t|do\s+not)\s+be\s+lazy\b", "name the step that must not be skipped"),
    (r"\bdo\s+your\s+best\b", "state the acceptance test"),
    (r"\b(highest|top|high)[- ]quality\b", "state the acceptance test"),
    (r"\bmake\s+sure\s+(it|everything|that\s+everything)\s+(works|is\s+correct)\b",
     "name the command whose exit code decides it"),
    (r"\bnever\s+make\s+(a\s+|any\s+)?mistakes?\b", "name the gate that catches the mistake"),
    (r"\bas\s+(thorough|detailed|comprehensive)\s+as\s+possible\b", "state the scope and where to stop"),
]
INSTRUCTION_FILES = ["CLAUDE.md", "CLAUDE.local.md", "AGENTS.md", ".claude/CLAUDE.md"]
HIGH_EFFORT = {"high", "xhigh", "max"}
LOOKUP_AGENTS = {"explore", "claude-code-guide", "web-fetch", "statusline-setup"}
TOP_MODELS = ("opus", "fable", "mythos")
# Standard price ratios relative to one input token. Used only to rank leaks.
WEIGHT = {"input": 1.0, "cache_write": 1.25, "cache_read": 0.1, "output": 5.0}


# ------------------------------------------------------------ instruction files

def lint_text(text: str) -> list[dict[str, Any]]:
    """Filler and vague lines in an instruction file, skipping fenced code."""
    findings = []
    fenced = False
    for number, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if fenced or not line.strip():
            continue
        if any(re.search(p, line, re.IGNORECASE) for p in FILLER):
            findings.append({"line": number, "kind": "filler", "text": line.strip(), "fix": "remove"})
            continue
        for pattern, fix in VAGUE:
            if re.search(pattern, line, re.IGNORECASE):
                findings.append({"line": number, "kind": "vague", "text": line.strip(), "fix": fix})
                break
    return findings


def lint_instructions(project: Path, home: Path) -> list[dict[str, Any]]:
    files = [project / name for name in INSTRUCTION_FILES] + [home / ".claude" / "CLAUDE.md"]
    report = []
    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        report.append({
            "file": str(path), "chars": len(text), "lines": len(text.splitlines()),
            "tokens_estimate": len(text) // 4, "findings": lint_text(text),
        })
    return report


# -------------------------------------------------------------------- settings

def read_settings(project: Path, home: Path) -> list[dict[str, Any]]:
    """Model and effort pins from settings files and the environment."""
    found = []
    for path in (home / ".claude" / "settings.json", project / ".claude" / "settings.json",
                 project / ".claude" / "settings.local.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        raw_env = data.get("env")
        env: dict[str, Any] = raw_env if isinstance(raw_env, dict) else {}
        for key in ("model", "effortLevel", "alwaysThinkingEnabled"):
            if key in data:
                found.append({"where": str(path), "key": key, "value": data[key]})
        for key in ("CLAUDE_CODE_EFFORT_LEVEL", "CLAUDE_CODE_SUBAGENT_MODEL", "ANTHROPIC_MODEL", "MAX_THINKING_TOKENS"):
            if key in env:
                found.append({"where": str(path), "key": "env." + key, "value": env[key]})
    for key in ("CLAUDE_CODE_EFFORT_LEVEL", "CLAUDE_CODE_SUBAGENT_MODEL", "ANTHROPIC_MODEL", "MAX_THINKING_TOKENS"):
        if os.environ.get(key):
            found.append({"where": "environment", "key": "env." + key, "value": os.environ[key]})
    return found


# ----------------------------------------------------------------- transcripts

def _entries(path: Path) -> Iterator[dict[str, Any]]:
    try:
        handle = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return
    with handle:
        for line in handle:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if isinstance(entry, dict):
                yield entry


def _ts(entry: dict[str, Any]) -> dt.datetime | None:
    value = entry.get("timestamp")
    if not isinstance(value, str):
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _effort(entry: dict[str, Any]) -> str:
    value = entry.get("effort")
    if isinstance(value, dict):
        value = value.get("level")
    return str(value) if value else ""


def requests_in(path: Path, since: dt.datetime | None = None) -> list[dict[str, Any]]:
    """One row per API request. A request is logged once per content block with
    the same usage, so rows are de-duplicated by request id."""
    seen: dict[str, dict[str, Any]] = {}
    for entry in _entries(path):
        message = entry.get("message")
        if entry.get("type") != "assistant" or not isinstance(message, dict):
            continue
        usage = message.get("usage")
        if not isinstance(usage, dict):
            continue
        when = _ts(entry)
        if since and when and when < since:
            continue
        key = str(entry.get("requestId") or message.get("id") or entry.get("uuid"))
        raw_details = usage.get("output_tokens_details")
        details: dict[str, Any] = raw_details if isinstance(raw_details, dict) else {}
        seen[key] = {
            "at": when,
            "model": str(message.get("model") or ""),
            "effort": _effort(entry),
            "input": int(usage.get("input_tokens") or 0),
            "cache_write": int(usage.get("cache_creation_input_tokens") or 0),
            "cache_read": int(usage.get("cache_read_input_tokens") or 0),
            "output": int(usage.get("output_tokens") or 0),
            "thinking": int(details.get("thinking_tokens") or 0),
        }
    return list(seen.values())


def _total(rows: Iterable[dict[str, Any]], key: str) -> int:
    return sum(int(r.get(key) or 0) for r in rows)


def subagents_of(transcript: Path) -> list[dict[str, Any]]:
    """Subagent transcripts of a session, with the agent type from the meta file."""
    folder = transcript.with_suffix("") / "subagents"
    result = []
    if not folder.is_dir():
        return result
    for path in sorted(folder.glob("agent-*.jsonl")):
        meta: dict[str, Any] = {}
        try:
            meta = json.loads(path.with_suffix("").with_suffix(".meta.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        result.append({"path": path, "type": str(meta.get("agentType") or "subagent"),
                       "description": str(meta.get("description") or "")})
    return result


def _role(agent_type: str, description: str) -> str:
    name = agent_type.split(":")[-1].lower()
    if name.startswith("crew-"):
        name = name[5:]
    if name in ("mapper", "breaker", "builder", "prover", "reviewer"):
        return name
    first = description.strip().lower().split(" ")[0] if description.strip() else ""
    return first if first in ("mapper", "breaker", "builder", "prover", "reviewer") else (name or "subagent")


def transcript_usage(transcript: Path, since: dt.datetime | None = None) -> list[dict[str, Any]]:
    """Measured tokens per (role, model) for a session and its subagents."""
    groups: dict[tuple[str, str], dict[str, Any]] = {}

    def add(role: str, rows: list[dict[str, Any]]) -> None:
        for row in rows:
            slot = groups.setdefault((role, row["model"]), {
                "role": role, "model": row["model"], "requests": 0,
                "input": 0, "cache_write": 0, "cache_read": 0, "output": 0})
            slot["requests"] += 1
            for key in ("input", "cache_write", "cache_read", "output"):
                slot[key] += row[key]

    add("lead", requests_in(transcript, since))
    for agent in subagents_of(transcript):
        add(_role(agent["type"], agent["description"]), requests_in(agent["path"], since))
    for slot in groups.values():
        slot["total_tokens"] = slot["input"] + slot["cache_write"] + slot["cache_read"] + slot["output"]
    return list(groups.values())


def price(row: dict[str, Any], rates: dict[str, Any]) -> float | None:
    """Dollars for a usage row from user-supplied rates keyed by a model-name
    fragment, e.g. {"sonnet": {"in": 3, "out": 15, "cache_read": 0.3, "cache_write": 3.75}}."""
    model = str(row.get("model") or "").lower()
    for fragment, rate in (rates or {}).items():
        if str(fragment).lower() in model and isinstance(rate, dict):
            try:
                return (row["input"] * float(rate.get("in", 0)) + row["output"] * float(rate.get("out", 0))
                        + row["cache_read"] * float(rate.get("cache_read", 0))
                        + row["cache_write"] * float(rate.get("cache_write", rate.get("in", 0)))) / 1_000_000
            except (TypeError, ValueError):
                return None
    return None


def analyze_session(transcript: Path) -> dict[str, Any] | None:
    rows = requests_in(transcript)
    if not rows:
        return None
    rows.sort(key=lambda r: r["at"] or dt.datetime.min.replace(tzinfo=dt.timezone.utc))
    turns = compactions = 0
    for entry in _entries(transcript):
        if entry.get("subtype") == "compact_boundary" or entry.get("isCompactSummary"):
            compactions += 1
        message = entry.get("message")
        if entry.get("type") != "user" or entry.get("isMeta") or not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, str) or (isinstance(content, list) and not any(
                isinstance(block, dict) and block.get("type") == "tool_result" for block in content)):
            turns += 1
    model_switches = sum(1 for a, b in zip(rows, rows[1:]) if a["model"] and b["model"] and a["model"] != b["model"])
    effort_switches = sum(1 for a, b in zip(rows, rows[1:]) if a["effort"] and b["effort"] and a["effort"] != b["effort"])
    cold = [r for r in rows[1:] if r["cache_read"] == 0 and r["input"] + r["cache_write"] > 20000]
    prompt = _total(rows, "input") + _total(rows, "cache_write") + _total(rows, "cache_read")
    stamps = [r["at"] for r in rows if r["at"]]
    hours = (max(stamps) - min(stamps)).total_seconds() / 3600 if len(stamps) > 1 else 0.0
    agents = []
    for agent in subagents_of(transcript):
        agent_rows = requests_in(agent["path"])
        if agent_rows:
            agents.append({
                "type": agent["type"], "model": agent_rows[-1]["model"],
                "tokens": sum(_total(agent_rows, k) for k in ("input", "cache_write", "cache_read", "output")),
                "weighted": sum(_total(agent_rows, k) * w for k, w in WEIGHT.items()),
            })
    efforts: dict[str, int] = {}
    for row in rows:
        efforts[row["effort"] or "unrecorded"] = efforts.get(row["effort"] or "unrecorded", 0) + 1
    high_rows = [r for r in rows if r["effort"] in HIGH_EFFORT]
    return {
        "session": transcript.stem, "started": min(stamps).isoformat(timespec="minutes") if stamps else "",
        "hours": round(hours, 2), "user_turns": turns, "requests": len(rows),
        "input": _total(rows, "input"), "cache_write": _total(rows, "cache_write"),
        "cache_read": _total(rows, "cache_read"), "output": _total(rows, "output"),
        "thinking": _total(rows, "thinking"),
        "cache_hit": round(_total(rows, "cache_read") / prompt, 3) if prompt else 0.0,
        "models": sorted({r["model"] for r in rows if r["model"]}),
        "model_switches": model_switches, "effort_switches": effort_switches, "efforts": efforts,
        "high_effort_requests": len(high_rows), "high_effort_output": _total(high_rows, "output"),
        "cold_requests": len(cold), "cold_tokens": sum(r["input"] + r["cache_write"] for r in cold),
        "compactions": compactions, "subagents": agents,
    }


def find_transcripts(project: Path, home: Path, limit: int) -> list[Path]:
    base = home / ".claude" / "projects"
    slug = re.sub(r"[^A-Za-z0-9]", "-", str(project))
    folders = [base / slug] if (base / slug).is_dir() else []
    if not folders and base.is_dir():
        for folder in base.iterdir():
            first = next(iter(sorted(folder.glob("*.jsonl"))), None) if folder.is_dir() else None
            if first is None:
                continue
            for entry in _entries(first):
                if entry.get("cwd"):
                    if Path(str(entry["cwd"])) == project:
                        folders.append(folder)
                    break
    files = [p for folder in folders for p in folder.glob("*.jsonl")]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return files[:limit]


# ---------------------------------------------------------------------- report

def build(project: Path, home: Path, limit: int) -> dict[str, Any]:
    sessions = [s for s in (analyze_session(p) for p in find_transcripts(project, home, limit)) if s]
    return {"project": str(project), "instructions": lint_instructions(project, home),
            "settings": read_settings(project, home), "sessions": sessions}


def leaks(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Leaks ranked by measured token exposure, weighted by standard price ratios."""
    sessions = data["sessions"]
    found: list[dict[str, Any]] = []
    requests = sum(s["requests"] for s in sessions)

    pins = [s for s in data["settings"] if s["key"] in ("effortLevel", "env.CLAUDE_CODE_EFFORT_LEVEL")]
    high_pin = next((s for s in pins if str(s["value"]).lower() in HIGH_EFFORT), None)
    high_requests = sum(s["high_effort_requests"] for s in sessions)
    high_output = sum(s["high_effort_output"] for s in sessions)
    if high_pin or (requests and high_requests / requests > 0.5):
        old = f"{high_pin['value']} ({high_pin['where']})" if high_pin else "high or above (observed in transcripts)"
        share = f"{int(100 * high_requests / requests)}% of {requests} requests ran at high or above" if requests else "no transcript yet"
        found.append({
            "setting": "default effort", "old": old, "new": "medium",
            "why": f"{share}; {high_output:,} output tokens at that level (measured). Raise effort per task only after a "
                   "measured worse answer, a long dependent chain, or a high-stakes review.",
            "exposure": high_output * WEIGHT["output"]})

    cold_tokens = sum(s["cold_tokens"] for s in sessions)
    switches = sum(s["model_switches"] for s in sessions)
    if cold_tokens or switches:
        found.append({
            "setting": "model and effort mid-session", "old": f"{switches} model switch(es), "
            f"{sum(s['effort_switches'] for s in sessions)} effort switch(es), {sum(s['cold_requests'] for s in sessions)} cold request(s)",
            "new": "choose once per session, then leave alone",
            "why": f"{cold_tokens:,} prompt tokens were re-read with no cache hit (measured). Each model has its own cache.",
            "exposure": cold_tokens * WEIGHT["cache_write"]})

    long_sessions = [s for s in sessions if s["user_turns"] > 30 or s["compactions"] > 0]
    if long_sessions:
        reread = sum(s["cache_read"] for s in long_sessions)
        worst = max(long_sessions, key=lambda s: s["user_turns"])
        found.append({
            "setting": "session length", "old": f"{len(long_sessions)} long session(s); longest {worst['user_turns']} turns, "
            f"{worst['compactions']} compaction(s)", "new": "/clear between unrelated tasks; task list in TASKS.md",
            "why": f"every turn re-reads the whole conversation: {reread:,} cache-read tokens in those sessions (measured).",
            "exposure": reread * WEIGHT["cache_read"]})

    lookups = [a for s in sessions for a in s["subagents"]
               if a["type"].split(":")[-1].lower() in LOOKUP_AGENTS and any(t in a["model"].lower() for t in TOP_MODELS)]
    if lookups:
        tokens = sum(a["tokens"] for a in lookups)
        found.append({
            "setting": "model for lookup subagents", "old": f"{lookups[0]['model']} for {len(lookups)} lookup agent(s)",
            "new": "sonnet or haiku", "why": f"{tokens:,} tokens spent on file lookups by the top model (measured). "
            "Route by difficulty: lookups and quick edits to a small model, hard logic to the top one.",
            "exposure": sum(a["weighted"] for a in lookups)})

    filler = [(f["file"], x) for f in data["instructions"] for x in f["findings"] if x["kind"] == "filler"]
    vague = [(f["file"], x) for f in data["instructions"] for x in f["findings"] if x["kind"] == "vague"]
    if filler:
        found.append({"setting": "instruction-file filler", "old": f"{len(filler)} line(s)", "new": "removed",
                      "why": "lines such as \"think carefully\" push effort up on every turn without naming a check. "
                             "Exposure is behavioural, not measured.", "exposure": 0.0})
    if vague:
        found.append({"setting": "vague guardrails", "old": f"{len(vague)} line(s)", "new": "named, specific rules",
                      "why": "a rule the model cannot check does not change what it does. Exposure not measured.",
                      "exposure": 0.0})
    big = [f for f in data["instructions"] if f["tokens_estimate"] > 2500]
    if big:
        found.append({"setting": "instruction-file size", "old": ", ".join(f"{Path(f['file']).name} ~{f['tokens_estimate']:,} tokens (estimate)" for f in big),
                      "new": "short file; move detail into skills loaded on demand",
                      "why": f"loaded into every request ({requests} measured requests here).",
                      "exposure": sum(f["tokens_estimate"] for f in big) * requests * WEIGHT["cache_read"]})
    found.sort(key=lambda item: -item["exposure"])
    return found


def render(data: dict[str, Any]) -> str:
    sessions = data["sessions"]
    out = [f"# Claude Code cost audit: {data['project']}", ""]
    out += ["## Measured from transcripts", ""]
    if sessions:
        out += ["| Session | Started | Hours | User turns | Requests | Input | Cache write | Cache read | Output (thinking) | Cache hit | Effort | Model switches | Compactions |",
                "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for s in sessions:
            effort = ", ".join(f"{k}:{v}" for k, v in sorted(s["efforts"].items()))
            out.append(f"| {s['session'][:8]} | {s['started']} | {s['hours']} | {s['user_turns']} | {s['requests']} | {s['input']:,} | "
                       f"{s['cache_write']:,} | {s['cache_read']:,} | {s['output']:,} ({s['thinking']:,}) | {int(s['cache_hit'] * 100)}% | "
                       f"{effort} | {s['model_switches']} | {s['compactions']} |")
        agents: dict[tuple[str, str], int] = {}
        for s in sessions:
            for a in s["subagents"]:
                agents[(a["type"], a["model"])] = agents.get((a["type"], a["model"]), 0) + a["tokens"]
        if agents:
            out += ["", "Subagents (measured): " + "; ".join(
                f"{kind} on {model}: {tokens:,} tokens" for (kind, model), tokens in sorted(agents.items(), key=lambda kv: -kv[1]))]
    else:
        out += ["No transcripts found for this project. Run one real task, then audit again: every number "
                "below needs a before and an after on the same task."]
    out += ["", "## Settings found", ""]
    out += [f"- `{s['key']}` = `{s['value']}` ({s['where']})" for s in data["settings"]] or ["- no model or effort pin in settings or environment"]
    ranked = leaks(data)
    out += ["", "## Before and after (biggest measured exposure first)", "",
            "| Setting | Old value | New value | Why |", "|---|---|---|---|"]
    out += [f"| {r['setting']} | {r['old']} | {r['new']} | {r['why']} |" for r in ranked] or ["| nothing to change | - | - | no leak found by these checks |"]
    out += ["", "## Exact lines to remove", ""]
    filler = [(f["file"], x) for f in data["instructions"] for x in f["findings"] if x["kind"] == "filler"]
    out += [f"- `{path}:{x['line']}`: {x['text']}" for path, x in filler] or ["- none"]
    out += ["", "## Vague guardrails to replace with named rules", ""]
    vague = [(f["file"], x) for f in data["instructions"] for x in f["findings"] if x["kind"] == "vague"]
    out += [f"- `{path}:{x['line']}`: {x['text']} -> {x['fix']}" for path, x in vague] or ["- none"]
    out += ["", "## TASKS.md rule to add", "",
            "> Keep the task list in TASKS.md and update it when a task starts or finishes. After a /clear or a "
            "context summary, read TASKS.md first and continue from the first unfinished task.",
            "", "## WHAT STILL LEAKS?", "",
            "- Reasoning restated in replies is not measurable from token counts. Read three long replies: if the answer "
            "repeats the thinking, ask for the result only.",
            "- Savings are not predicted here. Re-run this audit after one real task with the changes applied and compare "
            "with `--compare`."]
    if not sessions:
        out.append("- Nothing was measured: there is no transcript for this project yet.")
    return "\n".join(out) + "\n"


def compare(before: dict[str, Any], after: dict[str, Any]) -> str:
    def latest(data: dict[str, Any]) -> dict[str, Any]:
        return data["sessions"][0] if data.get("sessions") else {}
    b, a = latest(before), latest(after)
    rows = ["| Metric (latest session) | Before | After |", "|---|---|---|"]
    for key in ("requests", "user_turns", "input", "cache_write", "cache_read", "output", "thinking",
                "cache_hit", "high_effort_requests", "model_switches", "cold_tokens", "compactions"):
        rows.append(f"| {key} | {b.get(key, '-')} | {a.get(key, '-')} |")
    if b.get("session") and b.get("session") == a.get("session"):
        rows.append("")
        rows.append("Both columns are the same session: run the task again in a fresh session before comparing.")
    return "\n".join(rows) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--project", default=".")
    parser.add_argument("--home", default=str(Path.home()))
    parser.add_argument("--sessions", type=int, default=5, help="how many recent sessions to measure")
    parser.add_argument("--json", action="store_true", help="print the raw measurements")
    parser.add_argument("--save", help="write the measurements to this file, for a later --compare")
    parser.add_argument("--compare", help="a file written earlier by --save")
    args = parser.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:
        pass
    data = build(Path(args.project).resolve(), Path(args.home), args.sessions)
    if args.save:
        Path(args.save).write_text(json.dumps(data, indent=2), encoding="utf-8")
    if args.compare:
        print(compare(json.loads(Path(args.compare).read_text(encoding="utf-8")), data))
        return 0
    print(json.dumps(data, indent=2) if args.json else render(data))
    return 0


if __name__ == "__main__":
    sys.exit(main())
