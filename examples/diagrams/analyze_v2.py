#!/usr/bin/env python3
"""
SWE Pipeline Deep Analyzer v2
==============================
Parses JSONL execution logs from an autonomous SWE pipeline, computes
deep metrics (tokens, cost, duration, parallelism, throughput), and
generates a self-contained Jupyter notebook with 13+ seaborn-based
visualizations -- each as a SEPARATE figure.
"""

import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime
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
    "af-swe/example-diagrams/pipeline_deep_analysis.ipynb"
)
OUTPUT_DIR = Path(
    "/Users/santoshkumarradha/Documents/agentfield/code/int-agentfield-examples/"
    "af-swe/example-diagrams"
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


def classify_log(filename):
    """
    Classify a log file into a category and extract issue name + iteration info.
    Returns (category, issue_name, iteration_id_or_num).
    """
    stem = filename.replace(".jsonl", "")

    if stem in ("product_manager", "architect", "tech_lead", "sprint_planner"):
        return ("planning", stem, None)

    if stem.startswith("issue_writer_"):
        issue = stem[len("issue_writer_"):]
        return ("issue_writer", issue, None)

    m = re.match(r"coder_(.+)_iter_(\d+)", stem)
    if m:
        return ("coder", m.group(1), int(m.group(2)))

    m = re.match(r"reviewer_(.+)_iter_([a-f0-9]+)", stem)
    if m:
        return ("reviewer", m.group(1), m.group(2))

    m = re.match(r"qa_(.+)_iter_([a-f0-9]+)", stem)
    if m:
        return ("qa", m.group(1), m.group(2))

    m = re.match(r"synthesizer_(.+)_iter_([a-f0-9]+)", stem)
    if m:
        return ("synthesizer", m.group(1), m.group(2))

    m = re.match(r"merger_level_(\d+)", stem)
    if m:
        return ("merger", f"level_{m.group(1)}", int(m.group(1)))

    m = re.match(r"integration_tester_level_(\d+)", stem)
    if m:
        return ("integration_tester", f"level_{m.group(1)}", int(m.group(1)))

    m = re.match(r"workspace_(setup|cleanup)_level_(\d+)", stem)
    if m:
        return (f"workspace", f"{m.group(1)}_level_{m.group(2)}", int(m.group(2)))

    return ("unknown", stem, None)


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


def count_text_chars(events):
    """Count total characters in assistant text blocks (proxy for output tokens)."""
    total = 0
    for ev in events:
        if ev.get("event") != "assistant":
            continue
        content = ev.get("content", [])
        if not isinstance(content, list):
            continue
        for c in content:
            if c.get("type") == "text":
                total += len(c.get("text", ""))
    return total


def count_assistant_turns(events):
    """Count distinct assistant turns."""
    turns = set()
    for ev in events:
        if ev.get("event") == "assistant" and "turn" in ev:
            turns.add(ev["turn"])
    return len(turns)


def estimate_tokens_from_cost(cost_usd, model="sonnet"):
    """
    Estimate input and output tokens from cost using Claude Sonnet 4 pricing.
    Sonnet: $3/M input, $15/M output.
    Assume ~80% input, ~20% output by token count (typical for tool-heavy agents).
    With that ratio: cost = input_tokens * 3/1e6 + output_tokens * 15/1e6
    If output_tokens = 0.2 * total and input_tokens = 0.8 * total:
       cost = total * (0.8 * 3 + 0.2 * 15) / 1e6 = total * 5.4 / 1e6
       total = cost * 1e6 / 5.4
    """
    if cost_usd <= 0:
        return 0, 0
    total_tokens = cost_usd * 1_000_000 / 5.4
    input_tokens = int(total_tokens * 0.8)
    output_tokens = int(total_tokens * 0.2)
    return input_tokens, output_tokens


def get_phase_label(category):
    """Map category to a broader pipeline phase for heatmap."""
    mapping = {
        "planning": "Planning",
        "issue_writer": "Issue Writing",
        "coder": "Coding",
        "reviewer": "Review",
        "qa": "QA",
        "synthesizer": "Synthesis",
        "merger": "Merge",
        "integration_tester": "Integration",
        "workspace": "Workspace",
        "unknown": "Other",
    }
    return mapping.get(category, "Other")


# ---------------------------------------------------------------------------
# Main data extraction
# ---------------------------------------------------------------------------
def extract_all_data():
    """Parse all log files and return structured data for the notebook."""
    log_files = sorted(LOG_DIR.glob("*.jsonl"))
    print(f"Found {len(log_files)} log files in {LOG_DIR}")

    records = []  # flat list of dicts, one per log file

    for logfile in log_files:
        fname = logfile.name
        events = parse_log(logfile)
        if not events:
            continue

        category, issue, iter_info = classify_log(fname)

        # Timestamps
        first_ts = events[0].get("ts", 0)
        last_ts = events[-1].get("ts", 0)
        duration_s = last_ts - first_ts

        # Result event
        result_ev = None
        end_ev = None
        for ev in events:
            if ev.get("event") == "result":
                result_ev = ev
            if ev.get("event") == "end":
                end_ev = ev

        num_turns = 0
        cost_usd = 0.0
        duration_ms = 0
        is_error = False

        if result_ev:
            num_turns = result_ev.get("num_turns", 0)
            cost_usd = result_ev.get("cost_usd", 0.0)
            duration_ms = result_ev.get("duration_ms", 0)
        if end_ev:
            is_error = end_ev.get("is_error", False)

        # Model from start event
        model = "unknown"
        start_ev = events[0] if events[0].get("event") == "start" else None
        if start_ev:
            model = start_ev.get("model", "unknown")

        tool_calls = count_tool_calls(events)
        assistant_turns = count_assistant_turns(events)
        text_chars = count_text_chars(events)

        # Estimate tokens from cost
        input_tokens, output_tokens = estimate_tokens_from_cost(cost_usd, model)

        # Also use text_chars as a secondary output token estimate
        # (~4 chars per token for English text)
        output_tokens_from_text = int(text_chars / 4)

        phase = get_phase_label(category)

        records.append({
            "file": fname,
            "category": category,
            "phase": phase,
            "issue": issue,
            "iter": str(iter_info) if iter_info is not None else "",
            "first_ts": first_ts,
            "last_ts": last_ts,
            "duration_s": round(duration_s, 2),
            "duration_ms": duration_ms,
            "num_turns": num_turns,
            "assistant_turns": assistant_turns,
            "tool_calls": tool_calls,
            "cost_usd": round(cost_usd, 6),
            "is_error": is_error,
            "model": model,
            "text_chars": text_chars,
            "input_tokens_est": input_tokens,
            "output_tokens_est": output_tokens,
            "output_tokens_from_text": output_tokens_from_text,
        })

    # Sort by start time
    records.sort(key=lambda r: r["first_ts"])

    # Compute pipeline-level stats
    all_start = min(r["first_ts"] for r in records)
    all_end = max(r["last_ts"] for r in records)

    summary = {
        "pipeline_start": all_start,
        "pipeline_end": all_end,
        "total_wall_s": round(all_end - all_start, 2),
        "total_cost": round(sum(r["cost_usd"] for r in records), 4),
        "total_agents": len(records),
        "total_turns": sum(r["num_turns"] for r in records),
        "total_tool_calls": sum(r["tool_calls"] for r in records),
        "total_input_tokens_est": sum(r["input_tokens_est"] for r in records),
        "total_output_tokens_est": sum(r["output_tokens_est"] for r in records),
    }

    print(f"Pipeline wall time: {summary['total_wall_s']:.0f}s")
    print(f"Total cost: ${summary['total_cost']:.2f}")
    print(f"Total agents: {summary['total_agents']}")

    return records, summary


# ---------------------------------------------------------------------------
# Notebook generation
# ---------------------------------------------------------------------------
def make_notebook(records, summary):
    """Generate a self-contained Jupyter notebook with seaborn visualizations."""

    data_json = json.dumps({"records": records, "summary": summary}, indent=2)

    cells = []

    def md(source):
        cells.append({
            "cell_type": "markdown",
            "metadata": {},
            "source": [line + "\n" for line in source.split("\n")],
        })

    def code(source):
        cells.append({
            "cell_type": "code",
            "metadata": {},
            "source": [line + "\n" for line in source.split("\n")],
            "execution_count": None,
            "outputs": [],
        })

    # ===== Title =====
    md("""# Autonomous SWE Pipeline -- Deep Analysis

This notebook provides a comprehensive analysis of an autonomous SWE pipeline
that built a Rust CLI project using multiple AI agents.

Pipeline stages: **Planning** -> **Issue Writing** -> **Coding** -> **Review** -> **QA** -> **Synthesis** -> **Merge** -> **Integration Testing**

Each visualization is a separate figure for clarity.""")

    # ===== Data cell =====
    code(f"""import json

_RAW = json.loads('''{data_json}''')
RECORDS = _RAW['records']
SUMMARY = _RAW['summary']
print(f"Loaded {{len(RECORDS)}} agent records")
print(f"Pipeline wall time: {{SUMMARY['total_wall_s']:.0f}}s, Total cost: ${{SUMMARY['total_cost']:.2f}}")""")

    # ===== Setup cell =====
    code("""%matplotlib inline
import matplotlib
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import matplotlib.ticker as mticker
import numpy as np
import pandas as pd
import seaborn as sns
from datetime import datetime

# Seaborn theme
sns.set_theme(style='whitegrid', palette='deep')

# Build DataFrame
df = pd.DataFrame(RECORDS)

# Compute derived columns
df['total_tokens_est'] = df['input_tokens_est'] + df['output_tokens_est']
df['cost_per_1k_tokens'] = np.where(
    df['total_tokens_est'] > 0,
    df['cost_usd'] / (df['total_tokens_est'] / 1000),
    0
)
df['tokens_per_second'] = np.where(
    df['duration_s'] > 0,
    df['total_tokens_est'] / df['duration_s'],
    0
)
df['tool_to_turn_ratio'] = np.where(
    df['assistant_turns'] > 0,
    df['tool_calls'] / df['assistant_turns'],
    0
)
# Relative start time (seconds from pipeline start)
df['rel_start'] = df['first_ts'] - SUMMARY['pipeline_start']
df['rel_end'] = df['last_ts'] - SUMMARY['pipeline_start']

# Category ordering for consistent plots
CAT_ORDER = ['planning', 'issue_writer', 'coder', 'reviewer', 'qa',
             'synthesizer', 'merger', 'integration_tester', 'workspace']
PHASE_ORDER = ['Planning', 'Issue Writing', 'Coding', 'Review', 'QA',
               'Synthesis', 'Merge', 'Integration', 'Workspace']

# Color palette for categories
CAT_PALETTE = {
    'planning': '#E69F00', 'issue_writer': '#CC79A7',
    'coder': '#0072B2', 'reviewer': '#009E73', 'qa': '#D55E00',
    'synthesizer': '#56B4E9', 'merger': '#F0E442',
    'integration_tester': '#999999', 'workspace': '#666666',
}
PHASE_PALETTE = {
    'Planning': '#E69F00', 'Issue Writing': '#CC79A7',
    'Coding': '#0072B2', 'Review': '#009E73', 'QA': '#D55E00',
    'Synthesis': '#56B4E9', 'Merge': '#F0E442',
    'Integration': '#999999', 'Workspace': '#666666', 'Other': '#AAAAAA',
}

def fmt_dur(s):
    m, sec = divmod(int(s), 60)
    return f'{m}m {sec}s' if m else f'{sec}s'

def fmt_tokens(n):
    if n >= 1_000_000:
        return f'{n/1e6:.1f}M'
    elif n >= 1_000:
        return f'{n/1e3:.1f}K'
    return str(n)

# Filter to main agent categories (exclude workspace for most plots)
df_agents = df[df['category'].isin(['planning', 'issue_writer', 'coder', 'reviewer',
                                     'qa', 'synthesizer', 'merger', 'integration_tester'])].copy()

print(f"DataFrame shape: {df.shape}")
print(f"Categories: {df['category'].value_counts().to_dict()}")
print()
print(df[['category', 'cost_usd', 'duration_s', 'input_tokens_est', 'output_tokens_est']].groupby('category').sum().round(2))""")

    # ===== SECTION: Token Analysis =====
    md("""---
## Token Analysis

Tokens are estimated from cost using Claude Sonnet pricing ($3/M input, $15/M output).
Text character counts provide a secondary estimate for output tokens.""")

    # ----- Plot 1: Token distribution by category (Violin) -----
    md("### Plot 1: Token Distribution by Agent Category (Violin Plots)")
    code("""plt.figure(figsize=(14, 6))

# Melt to long format for violin plot
token_data = df_agents[['category', 'input_tokens_est', 'output_tokens_est']].copy()
token_data = token_data.melt(id_vars='category', var_name='token_type', value_name='tokens')
token_data['token_type'] = token_data['token_type'].map({
    'input_tokens_est': 'Input Tokens',
    'output_tokens_est': 'Output Tokens'
})

# Order categories by median total tokens
cat_order_by_tokens = (df_agents.groupby('category')['total_tokens_est']
                        .median().sort_values(ascending=False).index.tolist())

ax = sns.violinplot(
    data=token_data, x='category', y='tokens', hue='token_type',
    split=True, inner='quart', linewidth=1.2,
    order=[c for c in cat_order_by_tokens if c in token_data['category'].unique()],
    palette={'Input Tokens': '#0072B2', 'Output Tokens': '#D55E00'},
    cut=0
)

plt.title('Token Distribution by Agent Category', fontsize=14, fontweight='bold')
plt.xlabel('Agent Category', fontsize=12)
plt.ylabel('Estimated Tokens', fontsize=12)
plt.xticks(rotation=30, ha='right')
plt.legend(title='Token Type', fontsize=10)
ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: fmt_tokens(x)))
plt.tight_layout()
plt.savefig('plot_01_token_violin.png', dpi=150, bbox_inches='tight')
plt.show()
print("Saved: plot_01_token_violin.png")""")

    # ----- Plot 2: Token efficiency scatter -----
    md("### Plot 2: Token Efficiency -- Output Tokens vs Duration")
    code("""plt.figure(figsize=(12, 7))

ax = sns.scatterplot(
    data=df_agents, x='duration_s', y='output_tokens_est',
    hue='category', style='category', s=100, alpha=0.8,
    palette=CAT_PALETTE,
    hue_order=[c for c in CAT_ORDER if c in df_agents['category'].unique()]
)

# Add regression line
from numpy.polynomial.polynomial import polyfit
mask = (df_agents['duration_s'] > 0) & (df_agents['output_tokens_est'] > 0)
if mask.sum() > 2:
    x_reg = df_agents.loc[mask, 'duration_s'].values
    y_reg = df_agents.loc[mask, 'output_tokens_est'].values
    b, m_coef = polyfit(x_reg, y_reg, 1)
    x_line = np.linspace(x_reg.min(), x_reg.max(), 100)
    plt.plot(x_line, b + m_coef * x_line, 'k--', alpha=0.5, linewidth=2, label='Trend line')

plt.title('Token Efficiency: Output Tokens vs Duration', fontsize=14, fontweight='bold')
plt.xlabel('Duration (seconds)', fontsize=12)
plt.ylabel('Estimated Output Tokens', fontsize=12)
ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: fmt_tokens(x)))
plt.legend(fontsize=9, bbox_to_anchor=(1.02, 1), loc='upper left')
plt.tight_layout()
plt.savefig('plot_02_token_efficiency.png', dpi=150, bbox_inches='tight')
plt.show()
print("Saved: plot_02_token_efficiency.png")""")

    # ----- Plot 3: Total tokens by issue (stacked bar) -----
    md("### Plot 3: Total Tokens by Issue (Stacked Input/Output)")
    code("""# Aggregate tokens by issue (only issues from coding pipeline)
issue_cats = ['coder', 'reviewer', 'qa', 'synthesizer']
df_issues = df_agents[df_agents['category'].isin(issue_cats)].copy()

if len(df_issues) > 0:
    issue_tokens = df_issues.groupby('issue').agg(
        input_tokens=('input_tokens_est', 'sum'),
        output_tokens=('output_tokens_est', 'sum')
    ).sort_values('input_tokens', ascending=True)

    plt.figure(figsize=(12, max(6, len(issue_tokens) * 0.5)))

    y_pos = np.arange(len(issue_tokens))
    bars1 = plt.barh(y_pos, issue_tokens['input_tokens'], height=0.6,
                      label='Input Tokens', color='#0072B2', alpha=0.85)
    bars2 = plt.barh(y_pos, issue_tokens['output_tokens'], height=0.6,
                      left=issue_tokens['input_tokens'],
                      label='Output Tokens', color='#D55E00', alpha=0.85)

    # Labels
    for i, (inp, out) in enumerate(zip(issue_tokens['input_tokens'], issue_tokens['output_tokens'])):
        total = inp + out
        plt.text(total + total * 0.01, i, fmt_tokens(total), va='center', fontsize=9)

    plt.yticks(y_pos, issue_tokens.index)
    plt.xlabel('Estimated Tokens', fontsize=12)
    plt.title('Total Tokens by Issue (Input + Output)', fontsize=14, fontweight='bold')
    plt.legend(fontsize=11)
    ax = plt.gca()
    ax.xaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: fmt_tokens(x)))
    plt.tight_layout()
    plt.savefig('plot_03_tokens_by_issue.png', dpi=150, bbox_inches='tight')
    plt.show()
    print("Saved: plot_03_tokens_by_issue.png")
else:
    print("No issue-level data found for token breakdown.")""")

    # ===== SECTION: Cost Analysis =====
    md("""---
## Cost Analysis

Analyzing cost distribution, burn rate, and cost efficiency across the pipeline.""")

    # ----- Plot 4: Cost per category (bar + swarm) -----
    md("### Plot 4: Cost per Category (Mean + Individual Points)")
    code("""plt.figure(figsize=(12, 6))

cat_order_cost = (df_agents.groupby('category')['cost_usd']
                   .mean().sort_values(ascending=False).index.tolist())

# Bar plot for mean
ax = sns.barplot(
    data=df_agents, x='category', y='cost_usd',
    order=cat_order_cost, palette=CAT_PALETTE,
    errorbar='sd', capsize=0.1, alpha=0.7, edgecolor='black', linewidth=0.8
)

# Overlay individual points
sns.stripplot(
    data=df_agents, x='category', y='cost_usd',
    order=cat_order_cost, color='black', alpha=0.5, size=5,
    jitter=True, ax=ax
)

plt.title('Cost per Agent Category (bar=mean, dots=individual agents)', fontsize=14, fontweight='bold')
plt.xlabel('Agent Category', fontsize=12)
plt.ylabel('Cost (USD)', fontsize=12)
plt.xticks(rotation=30, ha='right')

# Annotate means
for i, cat in enumerate(cat_order_cost):
    mean_val = df_agents[df_agents['category'] == cat]['cost_usd'].mean()
    plt.text(i, mean_val + 0.02, f'${mean_val:.3f}', ha='center', fontsize=9, fontweight='bold')

plt.tight_layout()
plt.savefig('plot_04_cost_per_category.png', dpi=150, bbox_inches='tight')
plt.show()
print("Saved: plot_04_cost_per_category.png")""")

    # ----- Plot 5: Cumulative cost over time -----
    md("### Plot 5: Cumulative Cost Over Time (Burn Rate)")
    code("""plt.figure(figsize=(14, 6))

# Sort all agents by start time and compute cumulative cost
df_sorted = df.sort_values('first_ts').copy()
df_sorted['cumulative_cost'] = df_sorted['cost_usd'].cumsum()
df_sorted['elapsed_min'] = (df_sorted['first_ts'] - SUMMARY['pipeline_start']) / 60

plt.fill_between(df_sorted['elapsed_min'], df_sorted['cumulative_cost'],
                  alpha=0.3, color='#0072B2')
plt.plot(df_sorted['elapsed_min'], df_sorted['cumulative_cost'],
         color='#0072B2', linewidth=2.5, marker='o', markersize=4)

# Mark phase transitions with vertical lines
phase_starts = df.groupby('phase')['rel_start'].min() / 60
for phase, start_min in phase_starts.items():
    if phase in PHASE_PALETTE and start_min > 0.5:
        plt.axvline(x=start_min, color=PHASE_PALETTE.get(phase, 'gray'),
                     linestyle='--', alpha=0.6, linewidth=1)
        plt.text(start_min + 0.1, plt.ylim()[1] * 0.95, phase,
                 fontsize=8, rotation=90, va='top', color=PHASE_PALETTE.get(phase, 'gray'))

plt.title('Cumulative Cost Over Time (Burn Rate)', fontsize=14, fontweight='bold')
plt.xlabel('Elapsed Time (minutes)', fontsize=12)
plt.ylabel('Cumulative Cost (USD)', fontsize=12)
plt.grid(True, alpha=0.3)

# Annotate total
total_cost = df_sorted['cumulative_cost'].iloc[-1]
plt.annotate(f'Total: ${total_cost:.2f}',
             xy=(df_sorted['elapsed_min'].iloc[-1], total_cost),
             xytext=(-80, 20), textcoords='offset points',
             fontsize=12, fontweight='bold',
             arrowprops=dict(arrowstyle='->', color='black'))

plt.tight_layout()
plt.savefig('plot_05_cumulative_cost.png', dpi=150, bbox_inches='tight')
plt.show()
print("Saved: plot_05_cumulative_cost.png")""")

    # ----- Plot 6: Cost per 1K tokens by category -----
    md("### Plot 6: Cost per 1K Tokens by Category (Efficiency)")
    code("""plt.figure(figsize=(12, 6))

# Aggregate: total cost / total tokens per category
cost_eff = df_agents.groupby('category').agg(
    total_cost=('cost_usd', 'sum'),
    total_tokens=('total_tokens_est', 'sum')
).reset_index()
cost_eff['cost_per_1k'] = np.where(
    cost_eff['total_tokens'] > 0,
    cost_eff['total_cost'] / (cost_eff['total_tokens'] / 1000),
    0
)
cost_eff = cost_eff.sort_values('cost_per_1k', ascending=True)

colors = [CAT_PALETTE.get(c, '#999999') for c in cost_eff['category']]
bars = plt.barh(cost_eff['category'], cost_eff['cost_per_1k'],
                color=colors, edgecolor='black', linewidth=0.5, height=0.6)

for bar, val in zip(bars, cost_eff['cost_per_1k']):
    plt.text(bar.get_width() + 0.0001, bar.get_y() + bar.get_height() / 2,
             f'${val:.4f}', va='center', fontsize=10)

plt.title('Cost per 1K Tokens by Category', fontsize=14, fontweight='bold')
plt.xlabel('Cost per 1K Tokens (USD)', fontsize=12)
plt.ylabel('Category', fontsize=12)
plt.tight_layout()
plt.savefig('plot_06_cost_per_token.png', dpi=150, bbox_inches='tight')
plt.show()
print("Saved: plot_06_cost_per_token.png")""")

    # ===== SECTION: Duration & Parallelism =====
    md("""---
## Duration & Parallelism Analysis

Understanding how agents execute in parallel and where time is spent.""")

    # ----- Plot 7: Concurrent agents over time -----
    md("### Plot 7: Concurrent Agents Over Time (Parallelism)")
    code("""plt.figure(figsize=(14, 6))

# Build timeline events: +1 at start, -1 at end
timeline_events = []
for _, row in df.iterrows():
    t_start = row['rel_start']
    t_end = row['rel_end']
    if t_end > t_start:
        timeline_events.append((t_start, +1, row['category']))
        timeline_events.append((t_end, -1, row['category']))

timeline_events.sort(key=lambda x: (x[0], x[1]))

times = []
concurrency = []
current = 0
for t, delta, _ in timeline_events:
    times.append(t / 60)  # convert to minutes
    concurrency.append(current)
    current += delta
    times.append(t / 60)
    concurrency.append(current)

plt.fill_between(times, concurrency, alpha=0.4, color='#0072B2', step='pre')
plt.plot(times, concurrency, color='#0072B2', linewidth=1.5)

# Highlight peak
peak_idx = np.argmax(concurrency)
peak_val = concurrency[peak_idx]
peak_time = times[peak_idx]
plt.annotate(f'Peak: {peak_val} agents',
             xy=(peak_time, peak_val),
             xytext=(20, 10), textcoords='offset points',
             fontsize=11, fontweight='bold', color='#D55E00',
             arrowprops=dict(arrowstyle='->', color='#D55E00'))

plt.title('Concurrent Agents Over Time', fontsize=14, fontweight='bold')
plt.xlabel('Elapsed Time (minutes)', fontsize=12)
plt.ylabel('Number of Concurrent Agents', fontsize=12)
plt.grid(True, alpha=0.3)
plt.tight_layout()
plt.savefig('plot_07_parallelism.png', dpi=150, bbox_inches='tight')
plt.show()
print("Saved: plot_07_parallelism.png")""")

    # ----- Plot 8: Duration heatmap -----
    md("### Plot 8: Duration Heatmap (Issues x Phases)")
    code("""# Build pivot table: issues as rows, phases as columns
issue_cats = ['coder', 'reviewer', 'qa', 'synthesizer']
df_issue_phases = df_agents[df_agents['category'].isin(issue_cats)].copy()

if len(df_issue_phases) > 0:
    # Sum duration per issue per phase
    pivot = df_issue_phases.pivot_table(
        index='issue', columns='phase', values='duration_s',
        aggfunc='sum', fill_value=0
    )

    # Reorder columns
    phase_cols = [p for p in ['Coding', 'Review', 'QA', 'Synthesis'] if p in pivot.columns]
    pivot = pivot[phase_cols]

    # Sort rows by total duration
    pivot['_total'] = pivot.sum(axis=1)
    pivot = pivot.sort_values('_total', ascending=False).drop('_total', axis=1)

    plt.figure(figsize=(10, max(6, len(pivot) * 0.5)))

    # Convert to minutes for readability
    pivot_min = pivot / 60

    ax = sns.heatmap(
        pivot_min, annot=True, fmt='.1f', cmap='YlOrRd',
        linewidths=0.5, linecolor='white',
        cbar_kws={'label': 'Duration (minutes)'},
        annot_kws={'fontsize': 10}
    )

    plt.title('Duration Heatmap: Issues x Pipeline Phases (minutes)',
              fontsize=14, fontweight='bold')
    plt.xlabel('Pipeline Phase', fontsize=12)
    plt.ylabel('Issue', fontsize=12)
    plt.yticks(rotation=0)
    plt.tight_layout()
    plt.savefig('plot_08_duration_heatmap.png', dpi=150, bbox_inches='tight')
    plt.show()
    print("Saved: plot_08_duration_heatmap.png")
else:
    print("No issue-phase data for heatmap.")""")

    # ----- Plot 9: Phase duration violin -----
    md("### Plot 9: Phase Duration Violin Plots")
    code("""plt.figure(figsize=(12, 6))

df_for_violin = df_agents[df_agents['phase'].isin(
    ['Coding', 'Review', 'QA', 'Synthesis', 'Planning', 'Issue Writing', 'Merge', 'Integration']
)].copy()
df_for_violin['duration_min'] = df_for_violin['duration_s'] / 60

phase_order_violin = [p for p in PHASE_ORDER if p in df_for_violin['phase'].unique()]

ax = sns.violinplot(
    data=df_for_violin, x='phase', y='duration_min',
    order=phase_order_violin, palette=PHASE_PALETTE,
    inner='box', cut=0, linewidth=1.2
)

# Overlay individual points
sns.stripplot(
    data=df_for_violin, x='phase', y='duration_min',
    order=phase_order_violin, color='black', alpha=0.4, size=4,
    jitter=True, ax=ax
)

plt.title('Phase Duration Distribution (Violin Plots)', fontsize=14, fontweight='bold')
plt.xlabel('Pipeline Phase', fontsize=12)
plt.ylabel('Duration (minutes)', fontsize=12)
plt.xticks(rotation=30, ha='right')
plt.tight_layout()
plt.savefig('plot_09_phase_violin.png', dpi=150, bbox_inches='tight')
plt.show()
print("Saved: plot_09_phase_violin.png")""")

    # ===== SECTION: Pipeline Efficiency =====
    md("""---
## Pipeline Efficiency

Analyzing throughput, rework, and agent behavior patterns.""")

    # ----- Plot 10: Time in pipeline stages (waterfall) -----
    md("### Plot 10: Time in Pipeline Stages (Waterfall)")
    code("""plt.figure(figsize=(12, 7))

# For each phase, compute: wall-clock span (first start to last end)
# and total agent-time
phase_stats = []
for phase in PHASE_ORDER:
    mask = df['phase'] == phase
    if mask.sum() == 0:
        continue
    sub = df[mask]
    wall_start = sub['rel_start'].min()
    wall_end = sub['rel_end'].max()
    wall_span = wall_end - wall_start
    agent_time = sub['duration_s'].sum()
    total_cost = sub['cost_usd'].sum()
    phase_stats.append({
        'phase': phase,
        'wall_span_min': wall_span / 60,
        'agent_time_min': agent_time / 60,
        'cost': total_cost,
        'n_agents': mask.sum()
    })

ps_df = pd.DataFrame(phase_stats)

x = np.arange(len(ps_df))
width = 0.35

fig, ax1 = plt.subplots(figsize=(13, 7))

bars1 = ax1.bar(x - width/2, ps_df['wall_span_min'], width,
                label='Wall-clock Span', color='#0072B2', alpha=0.8, edgecolor='black', linewidth=0.5)
bars2 = ax1.bar(x + width/2, ps_df['agent_time_min'], width,
                label='Total Agent Time', color='#D55E00', alpha=0.8, edgecolor='black', linewidth=0.5)

ax1.set_ylabel('Time (minutes)', fontsize=12)
ax1.set_xlabel('Pipeline Phase', fontsize=12)
ax1.set_title('Time in Pipeline Stages: Wall-Clock vs Agent Time', fontsize=14, fontweight='bold')
ax1.set_xticks(x)
ax1.set_xticklabels(ps_df['phase'], rotation=30, ha='right')

# Annotate with cost and agent count
for i, row in ps_df.iterrows():
    ax1.text(i - width/2, row['wall_span_min'] + 0.1,
             f"${row['cost']:.2f}\\n({int(row['n_agents'])} agents)",
             ha='center', fontsize=8, color='#333333')

ax1.legend(fontsize=11)
plt.tight_layout()
plt.savefig('plot_10_pipeline_waterfall.png', dpi=150, bbox_inches='tight')
plt.show()
print("Saved: plot_10_pipeline_waterfall.png")""")

    # ----- Plot 11: Rework analysis -----
    md("### Plot 11: Rework Analysis (Multiple Iterations)")
    code("""plt.figure(figsize=(12, 6))

# Find issues with multiple coder iterations
coder_df = df_agents[df_agents['category'] == 'coder'].copy()
if len(coder_df) > 0:
    iter_counts = coder_df.groupby('issue').agg(
        n_iters=('file', 'count'),
        total_cost=('cost_usd', 'sum'),
        total_duration_min=('duration_s', lambda x: x.sum() / 60)
    ).reset_index()

    # Split into first iteration cost and rework cost
    rework_data = []
    for issue in iter_counts['issue']:
        issue_coder = coder_df[coder_df['issue'] == issue].sort_values('first_ts')
        first_iter_cost = issue_coder.iloc[0]['cost_usd']
        first_iter_dur = issue_coder.iloc[0]['duration_s'] / 60
        rework_cost = issue_coder.iloc[1:]['cost_usd'].sum() if len(issue_coder) > 1 else 0
        rework_dur = issue_coder.iloc[1:]['duration_s'].sum() / 60 if len(issue_coder) > 1 else 0
        rework_data.append({
            'issue': issue,
            'first_iter_cost': first_iter_cost,
            'rework_cost': rework_cost,
            'first_iter_dur': first_iter_dur,
            'rework_dur': rework_dur,
            'n_iters': len(issue_coder)
        })

    rw_df = pd.DataFrame(rework_data).sort_values('rework_cost', ascending=True)

    y_pos = np.arange(len(rw_df))
    plt.barh(y_pos, rw_df['first_iter_cost'], height=0.6,
             label='First Iteration', color='#009E73', alpha=0.85)
    plt.barh(y_pos, rw_df['rework_cost'], height=0.6,
             left=rw_df['first_iter_cost'],
             label='Rework Cost', color='#D55E00', alpha=0.85)

    # Labels
    for i, row in rw_df.iterrows():
        idx = rw_df.index.get_loc(i)
        total = row['first_iter_cost'] + row['rework_cost']
        label = f"${total:.3f}"
        if row['n_iters'] > 1:
            label += f" ({int(row['n_iters'])} iters)"
        plt.text(total + 0.005, idx, label, va='center', fontsize=9)

    plt.yticks(y_pos, rw_df['issue'])
    plt.xlabel('Cost (USD)', fontsize=12)
    plt.title('Rework Analysis: First Iteration vs Additional Iterations (Coder)',
              fontsize=14, fontweight='bold')
    plt.legend(fontsize=11)
    plt.tight_layout()
    plt.savefig('plot_11_rework.png', dpi=150, bbox_inches='tight')
    plt.show()
    print("Saved: plot_11_rework.png")
else:
    print("No coder data found for rework analysis.")""")

    # ----- Plot 12: Tool-to-turn ratio -----
    md("### Plot 12: Agent Tool-to-Turn Ratio (Tool-Heavy vs Thinking-Heavy)")
    code("""plt.figure(figsize=(12, 7))

ratio_df = df_agents[df_agents['assistant_turns'] > 0].copy()
ratio_df['tool_ratio'] = ratio_df['tool_calls'] / ratio_df['assistant_turns']

ax = sns.boxplot(
    data=ratio_df, x='category', y='tool_ratio',
    order=[c for c in CAT_ORDER if c in ratio_df['category'].unique()],
    palette=CAT_PALETTE, linewidth=1.2, fliersize=4
)

sns.stripplot(
    data=ratio_df, x='category', y='tool_ratio',
    order=[c for c in CAT_ORDER if c in ratio_df['category'].unique()],
    color='black', alpha=0.4, size=5, jitter=True, ax=ax
)

plt.axhline(y=1.0, color='gray', linestyle='--', alpha=0.5, linewidth=1)
plt.text(plt.xlim()[1] * 0.02, 1.02, 'Balanced (1 tool per turn)', fontsize=9, color='gray')

plt.title('Tool-to-Turn Ratio by Category\\n(>1 = tool-heavy, <1 = thinking-heavy)',
          fontsize=14, fontweight='bold')
plt.xlabel('Agent Category', fontsize=12)
plt.ylabel('Tool Calls / Assistant Turns', fontsize=12)
plt.xticks(rotation=30, ha='right')
plt.tight_layout()
plt.savefig('plot_12_tool_ratio.png', dpi=150, bbox_inches='tight')
plt.show()
print("Saved: plot_12_tool_ratio.png")""")

    # ===== SECTION: Summary =====
    md("""---
## Summary Dashboard & Key Metrics""")

    # ----- Plot 13: KPI Dashboard -----
    md("### Plot 13: Key Metrics Dashboard")
    code("""fig, axes = plt.subplots(2, 2, figsize=(12, 8))
fig.suptitle('Pipeline Key Performance Indicators', fontsize=16, fontweight='bold', y=1.02)

# KPI 1: Total Cost
ax = axes[0, 0]
ax.text(0.5, 0.55, f"${SUMMARY['total_cost']:.2f}", transform=ax.transAxes,
        fontsize=36, ha='center', va='center', fontweight='bold', color='#0072B2')
ax.text(0.5, 0.2, 'Total Cost (USD)', transform=ax.transAxes,
        fontsize=14, ha='center', va='center', color='#555555')
ax.set_xlim(0, 1)
ax.set_ylim(0, 1)
ax.axis('off')

# KPI 2: Total Wall Time
ax = axes[0, 1]
wall_min = SUMMARY['total_wall_s'] / 60
ax.text(0.5, 0.55, f"{wall_min:.1f} min", transform=ax.transAxes,
        fontsize=36, ha='center', va='center', fontweight='bold', color='#D55E00')
ax.text(0.5, 0.2, 'Wall-Clock Time', transform=ax.transAxes,
        fontsize=14, ha='center', va='center', color='#555555')
ax.set_xlim(0, 1)
ax.set_ylim(0, 1)
ax.axis('off')

# KPI 3: Total Tokens
ax = axes[1, 0]
total_tokens = SUMMARY['total_input_tokens_est'] + SUMMARY['total_output_tokens_est']
ax.text(0.5, 0.55, fmt_tokens(total_tokens), transform=ax.transAxes,
        fontsize=36, ha='center', va='center', fontweight='bold', color='#009E73')
ax.text(0.5, 0.2, f"Total Tokens (est.)\\n{fmt_tokens(SUMMARY['total_input_tokens_est'])} in / {fmt_tokens(SUMMARY['total_output_tokens_est'])} out",
        transform=ax.transAxes, fontsize=12, ha='center', va='center', color='#555555')
ax.set_xlim(0, 1)
ax.set_ylim(0, 1)
ax.axis('off')

# KPI 4: Avg Cost per Issue
ax = axes[1, 1]
issue_costs = df_agents[df_agents['category'].isin(['coder', 'reviewer', 'qa', 'synthesizer'])].groupby('issue')['cost_usd'].sum()
avg_cost = issue_costs.mean() if len(issue_costs) > 0 else 0
ax.text(0.5, 0.55, f"${avg_cost:.3f}", transform=ax.transAxes,
        fontsize=36, ha='center', va='center', fontweight='bold', color='#CC79A7')
ax.text(0.5, 0.2, f'Avg Cost per Issue\\n({len(issue_costs)} issues)', transform=ax.transAxes,
        fontsize=12, ha='center', va='center', color='#555555')
ax.set_xlim(0, 1)
ax.set_ylim(0, 1)
ax.axis('off')

# Add subtle borders
for ax in axes.flat:
    for spine in ax.spines.values():
        spine.set_visible(True)
        spine.set_color('#DDDDDD')
        spine.set_linewidth(2)

plt.tight_layout()
plt.savefig('plot_13_dashboard.png', dpi=150, bbox_inches='tight')
plt.show()
print("Saved: plot_13_dashboard.png")""")

    # ----- Summary text table -----
    md("### Comprehensive Summary Table")
    code("""print("=" * 100)
print("  PIPELINE EXECUTION SUMMARY")
print("=" * 100)
print()

# Overall
print(f"  Total wall time   : {fmt_dur(SUMMARY['total_wall_s'])}")
print(f"  Total cost        : ${SUMMARY['total_cost']:.2f}")
print(f"  Total agents      : {SUMMARY['total_agents']}")
print(f"  Total turns       : {SUMMARY['total_turns']}")
print(f"  Total tool calls  : {SUMMARY['total_tool_calls']}")
print(f"  Est. input tokens : {fmt_tokens(SUMMARY['total_input_tokens_est'])}")
print(f"  Est. output tokens: {fmt_tokens(SUMMARY['total_output_tokens_est'])}")
print()

# Per-category breakdown
print("-" * 100)
print(f"  {'Category':<20s} {'Count':>6s} {'Dur(total)':>12s} {'Dur(mean)':>12s} {'Cost':>10s} {'Tokens(est)':>12s} {'Tools':>8s}")
print("-" * 100)

cat_summary = df.groupby('category').agg(
    count=('file', 'count'),
    total_dur=('duration_s', 'sum'),
    mean_dur=('duration_s', 'mean'),
    total_cost=('cost_usd', 'sum'),
    total_tokens=('total_tokens_est', 'sum'),
    total_tools=('tool_calls', 'sum')
).sort_values('total_cost', ascending=False)

for cat, row in cat_summary.iterrows():
    print(f"  {cat:<20s} {int(row['count']):>6d} {fmt_dur(row['total_dur']):>12s} "
          f"{fmt_dur(row['mean_dur']):>12s} ${row['total_cost']:>9.2f} "
          f"{fmt_tokens(int(row['total_tokens'])):>12s} {int(row['total_tools']):>8d}")

print()
print("-" * 100)
totals = cat_summary.sum()
print(f"  {'TOTAL':<20s} {int(totals['count']):>6d} {fmt_dur(totals['total_dur']):>12s} "
      f"{'':>12s} ${totals['total_cost']:>9.2f} "
      f"{fmt_tokens(int(totals['total_tokens'])):>12s} {int(totals['total_tools']):>8d}")

print()
print()

# Per-issue table
print("=" * 100)
print("  PER-ISSUE BREAKDOWN (coding pipeline only)")
print("=" * 100)
issue_cats = ['coder', 'reviewer', 'qa', 'synthesizer']
df_ic = df_agents[df_agents['category'].isin(issue_cats)].copy()

if len(df_ic) > 0:
    issue_summary = df_ic.groupby('issue').agg(
        total_cost=('cost_usd', 'sum'),
        total_dur=('duration_s', 'sum'),
        total_tokens=('total_tokens_est', 'sum'),
        n_agents=('file', 'count'),
        total_tools=('tool_calls', 'sum')
    ).sort_values('total_cost', ascending=False)

    print(f"  {'Issue':<40s} {'Cost':>8s} {'Duration':>10s} {'Tokens':>10s} {'Agents':>7s} {'Tools':>7s}")
    print("-" * 100)
    for issue, row in issue_summary.iterrows():
        print(f"  {issue:<40s} ${row['total_cost']:>7.3f} {fmt_dur(row['total_dur']):>10s} "
              f"{fmt_tokens(int(row['total_tokens'])):>10s} {int(row['n_agents']):>7d} {int(row['total_tools']):>7d}")

print()
print("Analysis complete.")""")

    # ===== Assemble notebook =====
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
    print(f"Open with: jupyter notebook {NOTEBOOK_PATH}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    records, summary = extract_all_data()
    make_notebook(records, summary)
    print("\nDone. Generated notebook with 13 visualizations.")
