#!/usr/bin/env python3
"""
SWE Pipeline Log Analyzer
=========================
Parses JSONL execution logs from an autonomous SWE pipeline building a Rust project.
Computes timing, cost, turn-count, and QA-pass/fail metrics for every agent,
grouped by pipeline phase and by issue. Prints a text summary and generates
a self-contained Jupyter notebook with matplotlib visualizations.
"""

import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
LOG_DIR = Path(
    "/Users/santoshkumarradha/Documents/agentfield/code/int-agentfield-examples/"
    "af-swe/example-diagrams/.artifacts/logs"
)
NOTEBOOK_PATH = Path(
    "/Users/santoshkumarradha/Documents/agentfield/code/int-agentfield-examples/"
    "af-swe/example-diagrams/pipeline_analysis.ipynb"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def parse_log(filepath):
    """Return list of parsed JSON events from a JSONL file."""
    events = []
    with open(filepath) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    print(
                        f"Warning: skipping malformed JSONL line in {filepath}: "
                        f"{line[:200]!r}",
                        file=sys.stderr,
                    )
    return events


def ts_to_str(ts):
    """Convert unix timestamp to human-readable HH:MM:SS."""
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S")


def fmt_duration(seconds):
    """Format seconds as Xm Ys."""
    m, s = divmod(int(seconds), 60)
    if m > 0:
        return f"{m}m {s}s"
    return f"{s}s"


def classify_log(filename):
    """
    Classify a log file into a category and extract issue name + iteration info.
    Returns (category, issue_name, iteration_id_or_num).
    Categories: planning, issue_writer, coder, reviewer, qa, synthesizer,
                merger, integration_tester, workspace_setup, workspace_cleanup
    """
    stem = filename.replace(".jsonl", "")

    # Planning agents
    if stem in ("product_manager", "architect", "tech_lead", "sprint_planner"):
        return ("planning", stem, None)

    # Issue writers
    if stem.startswith("issue_writer_"):
        issue = stem[len("issue_writer_"):]
        return ("issue_writer", issue, None)

    # Coder
    m = re.match(r"coder_(.+)_iter_(\d+)", stem)
    if m:
        return ("coder", m.group(1), int(m.group(2)))

    # Reviewer
    m = re.match(r"reviewer_(.+)_iter_([a-f0-9]+)", stem)
    if m:
        return ("reviewer", m.group(1), m.group(2))

    # QA
    m = re.match(r"qa_(.+)_iter_([a-f0-9]+)", stem)
    if m:
        return ("qa", m.group(1), m.group(2))

    # Synthesizer
    m = re.match(r"synthesizer_(.+)_iter_([a-f0-9]+)", stem)
    if m:
        return ("synthesizer", m.group(1), m.group(2))

    # Merger
    m = re.match(r"merger_level_(\d+)", stem)
    if m:
        return ("merger", f"level_{m.group(1)}", int(m.group(1)))

    # Integration tester
    m = re.match(r"integration_tester_level_(\d+)", stem)
    if m:
        return ("integration_tester", f"level_{m.group(1)}", int(m.group(1)))

    # Workspace setup/cleanup
    m = re.match(r"workspace_(setup|cleanup)_level_(\d+)", stem)
    if m:
        return (f"workspace_{m.group(1)}", f"level_{m.group(2)}", int(m.group(2)))

    return ("unknown", stem, None)


def extract_qa_verdict(events):
    """
    Scan QA log events for pass/fail verdict.
    Returns 'pass', 'fail', or 'unknown'.
    Also returns a list of failure descriptions found during testing.
    """
    failures = []
    final_verdict = "unknown"

    for ev in events:
        if ev.get("event") != "assistant":
            continue
        content = ev.get("content", [])
        if not isinstance(content, list):
            continue
        for c in content:
            if c.get("type") != "text":
                continue
            txt = c["text"].lower()
            # Look for clear final verdicts (usually near end of QA)
            if any(phrase in txt for phrase in [
                "all tests pass", "all tests passed", "passed - all",
                "result: passed", "result: all", "result: ok",
                "passed - all tests successful",
            ]):
                final_verdict = "pass"
            # Detect actual test failures (not just mentions of writing a failures report)
            # Only flag if the message indicates actual test failures found
            if re.search(r'(?:found|have|got|revealed)\s+\d+\s+test\s+failure', txt):
                snippet = c["text"][:200].replace("\n", " ").strip()
                failures.append(snippet)
            elif re.search(r'\d+\s+(?:tests?\s+)?(?:failed|failing)', txt):
                # Skip false positives like "write the test failure report" or "failures file (empty"
                if not re.search(r'(?:write|create|artifact|report|empty|which will be)', txt):
                    snippet = c["text"][:200].replace("\n", " ").strip()
                    failures.append(snippet)

    return final_verdict, failures


def count_tool_calls(events):
    """Count how many tool_use calls appear in assistant events."""
    count = 0
    for ev in events:
        if ev.get("event") != "assistant":
            continue
        content = ev.get("content", [])
        if not isinstance(content, list):
            continue
        for c in content:
            if c.get("type") == "tool_use":
                count += 1
    return count


def count_assistant_turns(events):
    """Count distinct assistant turns."""
    turns = set()
    for ev in events:
        if ev.get("event") == "assistant" and "turn" in ev:
            turns.add(ev["turn"])
    return len(turns)


# ---------------------------------------------------------------------------
# Main analysis
# ---------------------------------------------------------------------------
def run_analysis():
    log_files = sorted(LOG_DIR.glob("*.jsonl"))
    print(f"Found {len(log_files)} log files in {LOG_DIR}\n")

    # Storage
    all_logs = {}          # filename -> {events, category, issue, iter, ...}
    planning_data = {}     # agent_name -> metrics dict
    issue_writer_data = {} # issue -> metrics dict
    # For coding pipeline, we group by issue
    # Each issue can have multiple iterations
    issue_pipeline = defaultdict(lambda: {
        "coder_iters": [],      # list of (iter_num, metrics)
        "reviewer_iters": [],   # list of (iter_id, metrics)
        "qa_iters": [],         # list of (iter_id, metrics, verdict, failures)
        "synthesizer_iters": [],# list of (iter_id, metrics)
    })
    merger_data = {}
    integration_data = {}
    workspace_data = {"setup": {}, "cleanup": {}}

    for logfile in log_files:
        fname = logfile.name
        events = parse_log(logfile)
        if not events:
            continue

        category, issue, iter_info = classify_log(fname)

        # Extract basic metrics
        first_ts = events[0].get("ts", 0)
        last_ts = events[-1].get("ts", 0)
        duration_s = last_ts - first_ts

        # From result event
        result_ev = None
        end_ev = None
        for ev in events:
            if ev.get("event") == "result":
                result_ev = ev
            if ev.get("event") == "end":
                end_ev = ev

        num_turns = 0
        cost_usd = 0.0
        duration_ms_reported = 0
        is_error = False
        model = "unknown"

        if result_ev:
            num_turns = result_ev.get("num_turns", 0)
            cost_usd = result_ev.get("cost_usd", 0.0)
            duration_ms_reported = result_ev.get("duration_ms", 0)
        if end_ev:
            is_error = end_ev.get("is_error", False)

        # Get model from start event
        start_ev = events[0] if events[0].get("event") == "start" else None
        if start_ev:
            model = start_ev.get("model", "unknown")

        tool_calls = count_tool_calls(events)
        assistant_turns = count_assistant_turns(events)

        metrics = {
            "file": fname,
            "category": category,
            "issue": issue,
            "iter": iter_info,
            "first_ts": first_ts,
            "last_ts": last_ts,
            "duration_s": duration_s,
            "duration_ms_reported": duration_ms_reported,
            "num_turns": num_turns,
            "assistant_turns": assistant_turns,
            "tool_calls": tool_calls,
            "cost_usd": cost_usd,
            "is_error": is_error,
            "model": model,
        }

        all_logs[fname] = metrics

        if category == "planning":
            planning_data[issue] = metrics

        elif category == "issue_writer":
            issue_writer_data[issue] = metrics

        elif category == "coder":
            issue_pipeline[issue]["coder_iters"].append((iter_info, metrics))

        elif category == "reviewer":
            issue_pipeline[issue]["reviewer_iters"].append((iter_info, metrics))

        elif category == "qa":
            verdict, failures = extract_qa_verdict(events)
            metrics["qa_verdict"] = verdict
            metrics["qa_failures"] = failures
            issue_pipeline[issue]["qa_iters"].append((iter_info, metrics))

        elif category == "synthesizer":
            issue_pipeline[issue]["synthesizer_iters"].append((iter_info, metrics))

        elif category == "merger":
            merger_data[issue] = metrics

        elif category == "integration_tester":
            integration_data[issue] = metrics

        elif category == "workspace_setup":
            workspace_data["setup"][issue] = metrics

        elif category == "workspace_cleanup":
            workspace_data["cleanup"][issue] = metrics

    # -----------------------------------------------------------------------
    # TEXT SUMMARY
    # -----------------------------------------------------------------------
    sep = "=" * 80
    print(sep)
    print("  AUTONOMOUS SWE PIPELINE -- EXECUTION ANALYSIS")
    print(sep)

    # Overall timeline
    all_ts = [m["first_ts"] for m in all_logs.values()] + [m["last_ts"] for m in all_logs.values()]
    pipeline_start = min(all_ts)
    pipeline_end = max(all_ts)
    total_wall = pipeline_end - pipeline_start
    total_cost = sum(m["cost_usd"] for m in all_logs.values())
    total_turns = sum(m["num_turns"] for m in all_logs.values())
    total_tool_calls = sum(m["tool_calls"] for m in all_logs.values())

    print(f"\nPipeline start : {ts_to_str(pipeline_start)}")
    print(f"Pipeline end   : {ts_to_str(pipeline_end)}")
    print(f"Total wall time: {fmt_duration(total_wall)}")
    print(f"Total LLM cost : ${total_cost:.2f}")
    print(f"Total turns    : {total_turns}")
    print(f"Total tool calls: {total_tool_calls}")
    print(f"Log files      : {len(all_logs)}")

    # Planning phases
    print(f"\n{sep}")
    print("  PLANNING PHASES")
    print(sep)
    planning_order = ["product_manager", "architect", "tech_lead", "sprint_planner"]
    for agent in planning_order:
        if agent in planning_data:
            m = planning_data[agent]
            print(f"  {agent:20s}  {fmt_duration(m['duration_s']):>8s}  "
                  f"${m['cost_usd']:.2f}  turns={m['num_turns']:2d}  "
                  f"tools={m['tool_calls']:2d}  model={m['model']}")

    total_planning = sum(m["duration_s"] for m in planning_data.values())
    print(f"  {'TOTAL':20s}  {fmt_duration(total_planning):>8s}  "
          f"${sum(m['cost_usd'] for m in planning_data.values()):.2f}")

    # Issue writers
    print(f"\n{sep}")
    print("  ISSUE WRITERS (parallel)")
    print(sep)
    if issue_writer_data:
        for issue_name in sorted(issue_writer_data.keys()):
            m = issue_writer_data[issue_name]
            print(f"  {issue_name:40s}  {fmt_duration(m['duration_s']):>8s}  "
                  f"${m['cost_usd']:.2f}  turns={m['num_turns']:2d}")
        iw_times = [m["duration_s"] for m in issue_writer_data.values()]
        print(f"  {'Wall time (max of parallel)':40s}  {fmt_duration(max(iw_times)):>8s}")
        print(f"  {'Sum of all issue writers':40s}  {fmt_duration(sum(iw_times)):>8s}  "
              f"${sum(m['cost_usd'] for m in issue_writer_data.values()):.2f}")

    # Coding pipeline per issue
    print(f"\n{sep}")
    print("  CODING PIPELINE PER ISSUE")
    print(sep)

    # Collect per-issue totals for chart data
    issue_totals = {}
    for issue_name in sorted(issue_pipeline.keys()):
        data = issue_pipeline[issue_name]
        print(f"\n  Issue: {issue_name}")
        print(f"  {'-' * 70}")

        # Sort iterations by timestamp
        coder_iters = sorted(data["coder_iters"], key=lambda x: x[1]["first_ts"])
        reviewer_iters = sorted(data["reviewer_iters"], key=lambda x: x[1]["first_ts"])
        qa_iters = sorted(data["qa_iters"], key=lambda x: x[1]["first_ts"])
        synth_iters = sorted(data["synthesizer_iters"], key=lambda x: x[1]["first_ts"])

        num_iterations = len(coder_iters)
        total_coder = sum(m["duration_s"] for _, m in coder_iters)
        total_reviewer = sum(m["duration_s"] for _, m in reviewer_iters)
        total_qa = sum(m["duration_s"] for _, m in qa_iters)
        total_synth = sum(m["duration_s"] for _, m in synth_iters)
        issue_total = total_coder + total_reviewer + total_qa + total_synth
        issue_cost = (sum(m["cost_usd"] for _, m in coder_iters) +
                      sum(m["cost_usd"] for _, m in reviewer_iters) +
                      sum(m["cost_usd"] for _, m in qa_iters) +
                      sum(m["cost_usd"] for _, m in synth_iters))

        issue_totals[issue_name] = {
            "coding": total_coder,
            "review": total_reviewer,
            "qa": total_qa,
            "synthesis": total_synth,
            "total": issue_total,
            "cost": issue_cost,
            "iterations": num_iterations,
        }

        # QA verdicts
        qa_verdicts = [(iter_id, m.get("qa_verdict", "unknown"), m.get("qa_failures", []))
                       for iter_id, m in qa_iters]

        print(f"    Iterations   : {num_iterations}")
        print(f"    Coding total : {fmt_duration(total_coder)}")
        print(f"    Review total : {fmt_duration(total_reviewer)}")
        print(f"    QA total     : {fmt_duration(total_qa)}")
        print(f"    Synthesis    : {fmt_duration(total_synth)}")
        print(f"    ISSUE TOTAL  : {fmt_duration(issue_total)}  cost=${issue_cost:.2f}")

        for i, (iter_num, m) in enumerate(coder_iters, 1):
            print(f"    Coder iter {iter_num}: {fmt_duration(m['duration_s']):>8s}  "
                  f"turns={m['num_turns']:2d}  tools={m['tool_calls']:2d}  cost=${m['cost_usd']:.2f}")

        for i, (iter_id, m) in enumerate(reviewer_iters, 1):
            print(f"    Reviewer  {i}  : {fmt_duration(m['duration_s']):>8s}  "
                  f"turns={m['num_turns']:2d}  tools={m['tool_calls']:2d}  cost=${m['cost_usd']:.2f}")

        for i, (iter_id, m) in enumerate(qa_iters, 1):
            verdict = m.get("qa_verdict", "unknown")
            v_sym = "PASS" if verdict == "pass" else ("FAIL" if verdict == "fail" else "???")
            print(f"    QA        {i}  : {fmt_duration(m['duration_s']):>8s}  "
                  f"turns={m['num_turns']:2d}  tools={m['tool_calls']:2d}  "
                  f"cost=${m['cost_usd']:.2f}  verdict={v_sym}")
            if m.get("qa_failures"):
                for fail_msg in m["qa_failures"]:
                    print(f"      FAILURE: {fail_msg[:120]}")

        for i, (iter_id, m) in enumerate(synth_iters, 1):
            print(f"    Synth     {i}  : {fmt_duration(m['duration_s']):>8s}  "
                  f"turns={m['num_turns']:2d}  tools={m['tool_calls']:2d}  cost=${m['cost_usd']:.2f}")

    # Mergers
    print(f"\n{sep}")
    print("  MERGERS")
    print(sep)
    for key in sorted(merger_data.keys()):
        m = merger_data[key]
        print(f"  {key:20s}  {fmt_duration(m['duration_s']):>8s}  "
              f"${m['cost_usd']:.2f}  turns={m['num_turns']:2d}  tools={m['tool_calls']:2d}")

    # Integration testers
    if integration_data:
        print(f"\n{sep}")
        print("  INTEGRATION TESTERS")
        print(sep)
        for key in sorted(integration_data.keys()):
            m = integration_data[key]
            print(f"  {key:20s}  {fmt_duration(m['duration_s']):>8s}  "
                  f"${m['cost_usd']:.2f}  turns={m['num_turns']:2d}  tools={m['tool_calls']:2d}")

    # Workspace ops
    print(f"\n{sep}")
    print("  WORKSPACE SETUP / CLEANUP")
    print(sep)
    for kind in ("setup", "cleanup"):
        for key in sorted(workspace_data[kind].keys()):
            m = workspace_data[kind][key]
            print(f"  {kind:8s} {key:12s}  {fmt_duration(m['duration_s']):>8s}  "
                  f"${m['cost_usd']:.2f}  turns={m['num_turns']:2d}")

    # Agent turn counts summary
    print(f"\n{sep}")
    print("  AGENT TURN COUNTS & TOOL CALLS (all agents)")
    print(sep)
    sorted_by_turns = sorted(all_logs.values(), key=lambda x: x["assistant_turns"], reverse=True)
    for m in sorted_by_turns[:30]:
        print(f"  {m['file']:55s}  turns={m['assistant_turns']:3d}  "
              f"tools={m['tool_calls']:3d}  dur={fmt_duration(m['duration_s']):>8s}  "
              f"cost=${m['cost_usd']:.2f}")

    # Cost breakdown by category
    print(f"\n{sep}")
    print("  COST BREAKDOWN BY CATEGORY")
    print(sep)
    cost_by_cat = defaultdict(float)
    for m in all_logs.values():
        cost_by_cat[m["category"]] += m["cost_usd"]
    for cat in sorted(cost_by_cat.keys(), key=lambda c: -cost_by_cat[c]):
        print(f"  {cat:25s}  ${cost_by_cat[cat]:.2f}")
    print(f"  {'TOTAL':25s}  ${total_cost:.2f}")

    # Identify issues needing re-coding (iterations > 1)
    multi_iter_issues = {k: v for k, v in issue_totals.items() if v["iterations"] > 1}
    print(f"\n{sep}")
    print("  ISSUES WITH MULTIPLE ITERATIONS (QA-triggered re-coding)")
    print(sep)
    if multi_iter_issues:
        for issue, data in sorted(multi_iter_issues.items()):
            print(f"  {issue:40s}  iterations={data['iterations']}  "
                  f"total={fmt_duration(data['total'])}")
    else:
        print("  (None -- all issues passed on first iteration)")

    # -----------------------------------------------------------------------
    # Prepare data for notebook
    # -----------------------------------------------------------------------
    notebook_data = {
        "pipeline_start": pipeline_start,
        "pipeline_end": pipeline_end,
        "total_wall_s": total_wall,
        "total_cost": total_cost,
        "total_turns": total_turns,
        "total_tool_calls": total_tool_calls,
        "planning": {k: {"duration_s": v["duration_s"], "cost": v["cost_usd"],
                         "turns": v["num_turns"], "tool_calls": v["tool_calls"]}
                     for k, v in planning_data.items()},
        "issue_writers": {k: {"duration_s": v["duration_s"], "cost": v["cost_usd"],
                              "turns": v["num_turns"]}
                         for k, v in issue_writer_data.items()},
        "issue_totals": issue_totals,
        "merger": {k: {"duration_s": v["duration_s"], "cost": v["cost_usd"],
                       "turns": v["num_turns"], "tool_calls": v["tool_calls"]}
                  for k, v in merger_data.items()},
        "integration": {k: {"duration_s": v["duration_s"], "cost": v["cost_usd"],
                            "turns": v["num_turns"], "tool_calls": v["tool_calls"]}
                       for k, v in integration_data.items()},
        "all_agents": [
            {"file": m["file"], "category": m["category"], "issue": m["issue"],
             "duration_s": m["duration_s"], "cost": m["cost_usd"],
             "turns": m["assistant_turns"], "tool_calls": m["tool_calls"],
             "model": m["model"]}
            for m in all_logs.values()
        ],
        "cost_by_category": dict(cost_by_cat),
    }

    return notebook_data


# ---------------------------------------------------------------------------
# Notebook generation
# ---------------------------------------------------------------------------
def make_notebook(data):
    """Generate a self-contained Jupyter notebook with embedded data."""

    data_json = json.dumps(data, indent=2)

    cells = []

    def md(source):
        cells.append({
            "cell_type": "markdown",
            "metadata": {},
            "source": source.split("\n"),
        })

    def code(source):
        cells.append({
            "cell_type": "code",
            "metadata": {},
            "source": [line + "\n" for line in source.split("\n")],
            "execution_count": None,
            "outputs": [],
        })

    # --- Title ---
    md("# Autonomous SWE Pipeline -- Execution Analysis\n\n"
       "This notebook visualizes the execution of an autonomous SWE pipeline that built a Rust CLI project.\n\n"
       "Pipeline stages: **product_manager** -> **architect** -> **tech_lead** -> **sprint_planner** -> "
       "**issue_writers** (parallel) -> per-issue **coder** -> **reviewer** -> **QA** -> **synthesizer** -> **merger**")

    # --- Data cell ---
    code(f"import json\n\nDATA = json.loads('''\n{data_json}\n''')")

    # --- Setup ---
    code("""%matplotlib inline
import matplotlib
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np
from collections import defaultdict

plt.rcParams['figure.figsize'] = (14, 6)
plt.rcParams['figure.dpi'] = 120
plt.rcParams['font.size'] = 10
plt.rcParams['axes.titlesize'] = 13
plt.rcParams['axes.labelsize'] = 11

# Color palette
COLORS = {
    'coding': '#4C72B0',
    'review': '#55A868',
    'qa': '#C44E52',
    'synthesis': '#8172B2',
    'planning': '#CCB974',
    'merger': '#64B5CD',
    'issue_writer': '#DA8BC3',
    'integration': '#8C8C8C',
}

def fmt_dur(s):
    m, sec = divmod(int(s), 60)
    return f'{m}m {sec}s' if m else f'{sec}s'
""")

    # --- Chart 1: Planning durations ---
    md("## 1. Planning Phase Durations")
    code("""planning = DATA['planning']
order = ['product_manager', 'architect', 'tech_lead', 'sprint_planner']
agents = [a for a in order if a in planning]
durations = [planning[a]['duration_s'] for a in agents]
costs = [planning[a]['cost'] for a in agents]

fig, ax = plt.subplots(figsize=(10, 5))
bars = ax.barh(agents, durations, color=COLORS['planning'], edgecolor='white', height=0.6)
for bar, dur, cost in zip(bars, durations, costs):
    ax.text(bar.get_width() + 2, bar.get_y() + bar.get_height()/2,
            f'{fmt_dur(dur)}  (${cost:.2f})', va='center', fontsize=10)
ax.set_xlabel('Duration (seconds)')
ax.set_title('Planning Phase Durations')
ax.invert_yaxis()
plt.tight_layout()
plt.savefig('_chart_planning.png', bbox_inches='tight')
plt.show()
""")

    # --- Chart 2: Issue writer durations ---
    md("## 2. Issue Writer Durations (ran in parallel)")
    code("""iw = DATA['issue_writers']
issues = sorted(iw.keys(), key=lambda k: -iw[k]['duration_s'])
durs = [iw[i]['duration_s'] for i in issues]

fig, ax = plt.subplots(figsize=(12, 6))
bars = ax.barh(issues, durs, color=COLORS['issue_writer'], edgecolor='white', height=0.7)
for bar, dur in zip(bars, durs):
    ax.text(bar.get_width() + 0.5, bar.get_y() + bar.get_height()/2,
            fmt_dur(dur), va='center', fontsize=9)
ax.set_xlabel('Duration (seconds)')
ax.set_title('Issue Writer Durations (all ran in parallel)')
ax.invert_yaxis()
plt.tight_layout()
plt.savefig('_chart_issue_writers.png', bbox_inches='tight')
plt.show()
""")

    # --- Chart 3: Stacked bar per issue ---
    md("## 3. Per-Issue Coding Pipeline Duration (stacked by phase)")
    code("""it = DATA['issue_totals']
issues = sorted(it.keys(), key=lambda k: -it[k]['total'])
coding = [it[i]['coding'] for i in issues]
review = [it[i]['review'] for i in issues]
qa = [it[i]['qa'] for i in issues]
synth = [it[i]['synthesis'] for i in issues]

fig, ax = plt.subplots(figsize=(14, 7))
y = np.arange(len(issues))
h = 0.7

b1 = ax.barh(y, coding, height=h, label='Coding', color=COLORS['coding'])
b2 = ax.barh(y, review, height=h, left=coding, label='Review', color=COLORS['review'])
left2 = [c+r for c, r in zip(coding, review)]
b3 = ax.barh(y, qa, height=h, left=left2, label='QA', color=COLORS['qa'])
left3 = [l+q for l, q in zip(left2, qa)]
b4 = ax.barh(y, synth, height=h, left=left3, label='Synthesis', color=COLORS['synthesis'])

# Total labels
for i, issue in enumerate(issues):
    total = it[issue]['total']
    iters = it[issue]['iterations']
    label = f'{fmt_dur(total)}'
    if iters > 1:
        label += f'  ({iters} iters)'
    ax.text(total + 3, i, label, va='center', fontsize=9)

ax.set_yticks(y)
ax.set_yticklabels(issues)
ax.set_xlabel('Duration (seconds)')
ax.set_title('Per-Issue Pipeline Duration (Coding + Review + QA + Synthesis)')
ax.legend(loc='lower right')
ax.invert_yaxis()
plt.tight_layout()
plt.savefig('_chart_issue_stacked.png', bbox_inches='tight')
plt.show()
""")

    # --- Chart 4: Phase duration distribution ---
    md("## 4. Distribution of Phase Durations Across All Issues")
    code("""it = DATA['issue_totals']
phases = ['coding', 'review', 'qa', 'synthesis']
phase_data = {p: [it[i][p] for i in it] for p in phases}

fig, axes = plt.subplots(2, 2, figsize=(12, 8))
for ax, phase in zip(axes.flat, phases):
    vals = phase_data[phase]
    ax.hist(vals, bins=8, color=COLORS[phase], edgecolor='white', alpha=0.85)
    ax.axvline(np.mean(vals), color='black', linestyle='--', linewidth=1,
               label=f'mean={np.mean(vals):.0f}s')
    ax.set_title(f'{phase.capitalize()} Duration Distribution')
    ax.set_xlabel('Seconds')
    ax.set_ylabel('Count')
    ax.legend(fontsize=9)
plt.suptitle('Phase Duration Distributions Across Issues', fontsize=14, y=1.01)
plt.tight_layout()
plt.savefig('_chart_phase_dist.png', bbox_inches='tight')
plt.show()
""")

    # --- Chart 5: QA iterations ---
    md("## 5. QA Iterations per Issue (issues needing re-coding)")
    code("""it = DATA['issue_totals']
issues = sorted(it.keys(), key=lambda k: -it[k]['iterations'])
iters = [it[i]['iterations'] for i in issues]

fig, ax = plt.subplots(figsize=(12, 5))
colors = [COLORS['qa'] if n > 1 else COLORS['coding'] for n in iters]
bars = ax.barh(issues, iters, color=colors, edgecolor='white', height=0.7)
for bar, n in zip(bars, iters):
    if n > 1:
        ax.text(bar.get_width() + 0.05, bar.get_y() + bar.get_height()/2,
                f'{n} iterations', va='center', fontsize=9, color=COLORS['qa'], fontweight='bold')
ax.set_xlabel('Number of Coder Iterations')
ax.set_title('QA Iterations per Issue (red = required re-coding)')
ax.axvline(1, color='gray', linestyle=':', alpha=0.5)
ax.invert_yaxis()
plt.tight_layout()
plt.savefig('_chart_qa_iterations.png', bbox_inches='tight')
plt.show()
""")

    # --- Chart 6: Agent turn counts ---
    md("## 6. Agent Turn Counts (LLM calls per agent)")
    code("""agents = DATA['all_agents']
# Sort by turns descending, take top 25
top = sorted(agents, key=lambda a: -a['turns'])[:25]
names = [a['file'].replace('.jsonl', '') for a in top]
turns = [a['turns'] for a in top]
tools = [a['tool_calls'] for a in top]

fig, ax = plt.subplots(figsize=(14, 8))
y = np.arange(len(names))
h = 0.35
ax.barh(y - h/2, turns, height=h, label='Assistant Turns', color=COLORS['coding'])
ax.barh(y + h/2, tools, height=h, label='Tool Calls', color=COLORS['review'])
ax.set_yticks(y)
ax.set_yticklabels(names, fontsize=8)
ax.set_xlabel('Count')
ax.set_title('Top 25 Agents by Turn Count')
ax.legend()
ax.invert_yaxis()
plt.tight_layout()
plt.savefig('_chart_agent_turns.png', bbox_inches='tight')
plt.show()
""")

    # --- Chart 7: Cost breakdown ---
    md("## 7. Cost Breakdown by Category")
    code("""cats = DATA['cost_by_category']
labels = sorted(cats.keys(), key=lambda k: -cats[k])
values = [cats[k] for k in labels]
colors_list = [COLORS.get(k, '#8C8C8C') for k in labels]

fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 6))

# Pie chart
wedges, texts, autotexts = ax1.pie(values, labels=labels, autopct='%1.1f%%',
                                     startangle=90, colors=colors_list)
ax1.set_title('Cost Distribution by Category')

# Bar chart
ax2.barh(labels, values, color=colors_list, edgecolor='white')
for i, (label, val) in enumerate(zip(labels, values)):
    ax2.text(val + 0.02, i, f'${val:.2f}', va='center', fontsize=10)
ax2.set_xlabel('Cost (USD)')
ax2.set_title('Cost by Category')
ax2.invert_yaxis()

plt.tight_layout()
plt.savefig('_chart_cost.png', bbox_inches='tight')
plt.show()
""")

    # --- Chart 8: Timeline / Gantt-like ---
    md("## 8. Pipeline Timeline (wall-clock execution)")
    code("""agents = DATA['all_agents']
start0 = DATA['pipeline_start']

# Group by category
cat_order = ['planning', 'issue_writer', 'coder', 'reviewer', 'qa',
             'synthesizer', 'merger', 'integration_tester',
             'workspace_setup', 'workspace_cleanup']
cat_colors = {
    'planning': '#CCB974', 'issue_writer': '#DA8BC3',
    'coder': '#4C72B0', 'reviewer': '#55A868', 'qa': '#C44E52',
    'synthesizer': '#8172B2', 'merger': '#64B5CD',
    'integration_tester': '#8C8C8C', 'workspace_setup': '#AEC7E8',
    'workspace_cleanup': '#FFBB78',
}

# Sort all agents by start time
sorted_agents = sorted(agents, key=lambda a: a.get('duration_s', 0), reverse=True)
# Actually sort by something more meaningful - category then start
# Let's group by category
from collections import defaultdict
by_cat = defaultdict(list)
for a in agents:
    by_cat[a['category']].append(a)

fig, ax = plt.subplots(figsize=(16, 10))
yticks = []
ylabels = []
y = 0
for cat in cat_order:
    if cat not in by_cat:
        continue
    items = sorted(by_cat[cat], key=lambda a: a.get('duration_s', 0), reverse=True)
    for a in items:
        # We don't have absolute start times in the serialized data, so use duration
        dur = a['duration_s']
        color = cat_colors.get(cat, '#8C8C8C')
        name = a['file'].replace('.jsonl', '')
        ax.barh(y, dur, left=0, height=0.7, color=color, alpha=0.8, edgecolor='white')
        if dur > 20:
            ax.text(dur + 2, y, fmt_dur(dur), va='center', fontsize=7)
        yticks.append(y)
        ylabels.append(name[:35])
        y += 1

ax.set_yticks(yticks)
ax.set_yticklabels(ylabels, fontsize=6)
ax.set_xlabel('Duration (seconds)')
ax.set_title('All Agent Durations (grouped by category)')

# Legend
patches = [mpatches.Patch(color=cat_colors.get(c, '#8C8C8C'), label=c)
           for c in cat_order if c in by_cat]
ax.legend(handles=patches, loc='lower right', fontsize=8)
ax.invert_yaxis()
plt.tight_layout()
plt.savefig('_chart_timeline.png', bbox_inches='tight')
plt.show()
""")

    # --- Chart 9: Cost vs Duration scatter ---
    md("## 9. Cost vs Duration per Agent (scatter)")
    code("""agents = DATA['all_agents']
cats = list(set(a['category'] for a in agents))
cat_colors_map = {
    'planning': '#CCB974', 'issue_writer': '#DA8BC3',
    'coder': '#4C72B0', 'reviewer': '#55A868', 'qa': '#C44E52',
    'synthesizer': '#8172B2', 'merger': '#64B5CD',
    'integration_tester': '#8C8C8C', 'workspace_setup': '#AEC7E8',
    'workspace_cleanup': '#FFBB78', 'unknown': '#999999',
}

fig, ax = plt.subplots(figsize=(12, 7))
for cat in cats:
    subset = [a for a in agents if a['category'] == cat]
    durs = [a['duration_s'] for a in subset]
    costs = [a['cost'] for a in subset]
    color = cat_colors_map.get(cat, '#999999')
    ax.scatter(durs, costs, label=cat, color=color, s=60, alpha=0.7, edgecolors='white')

ax.set_xlabel('Duration (seconds)')
ax.set_ylabel('Cost (USD)')
ax.set_title('Cost vs Duration per Agent Invocation')
ax.legend(fontsize=8)
plt.tight_layout()
plt.savefig('_chart_cost_vs_dur.png', bbox_inches='tight')
plt.show()
""")

    # --- Summary stats ---
    md("## 10. Summary Statistics")
    code("""from datetime import datetime

start_str = datetime.fromtimestamp(DATA['pipeline_start']).strftime('%H:%M:%S')
end_str = datetime.fromtimestamp(DATA['pipeline_end']).strftime('%H:%M:%S')

print(f"Pipeline Start  : {start_str}")
print(f"Pipeline End    : {end_str}")
print(f"Total Wall Time : {fmt_dur(DATA['total_wall_s'])}")
print(f"Total LLM Cost  : ${DATA['total_cost']:.2f}")
print(f"Total Turns     : {DATA['total_turns']}")
print(f"Total Tool Calls: {DATA['total_tool_calls']}")
print()

# Per-issue summary table
print(f"{'Issue':<40s} {'Total':>8s} {'Code':>7s} {'Review':>7s} {'QA':>7s} {'Synth':>7s} {'Iters':>5s} {'Cost':>7s}")
print('-' * 90)
it = DATA['issue_totals']
for issue in sorted(it.keys(), key=lambda k: -it[k]['total']):
    d = it[issue]
    print(f"{issue:<40s} {fmt_dur(d['total']):>8s} {fmt_dur(d['coding']):>7s} "
          f"{fmt_dur(d['review']):>7s} {fmt_dur(d['qa']):>7s} {fmt_dur(d['synthesis']):>7s} "
          f"{d['iterations']:>5d} ${d['cost']:.2f}")
""")

    # --- Assemble notebook ---
    notebook = {
        "nbformat": 4,
        "nbformat_minor": 5,
        "metadata": {
            "kernelspec": {
                "display_name": "Python 3",
                "language": "python",
                "name": "python3"
            },
            "language_info": {
                "name": "python",
                "version": "3.11.0"
            }
        },
        "cells": cells,
    }

    with open(NOTEBOOK_PATH, "w") as f:
        json.dump(notebook, f, indent=1)

    print(f"\nNotebook written to: {NOTEBOOK_PATH}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    data = run_analysis()
    make_notebook(data)
    print("\nDone.")
