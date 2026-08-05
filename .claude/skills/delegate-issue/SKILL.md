---
name: delegate-issue
description: Delegate well-scoped coding issues to a SWE-AF node as a cheap sub-harness via implement_issue, then collect and merge the returned branches. Use when the user asks to offload, delegate, or fan out scoped tasks to SWE-AF / swe-planner / swe-fast, or when several independent, fully-specified changes could run in parallel without burning main-harness tokens.
---

# Delegate scoped issues to SWE-AF

You are the main harness. SWE-AF is the sub-harness: it implements one
fully-scoped issue per call on an isolated branch, using cheap models, and
hands the branch back. You keep planning, merging, review, and CI.

## When to delegate (and when not to)

Delegate an issue only when ALL of these hold:

- The change is **fully specified**: you can name the files, the behavior, and
  the acceptance criteria without the sub-harness needing to plan anything.
- It is **independent** of your other in-flight edits (no shared files with
  another delegation or with your own uncommitted work).
- The repo is reachable by the SWE-AF node (same machine or shared volume) and
  has at least one commit.

Do NOT delegate: vague feature requests (use `swe-planner.build` instead),
changes to files you are editing yourself right now, or anything whose spec
you'd have to guess. Garbage spec in → garbage branch out.

## Preflight (once per session)

```bash
# Control plane up and the node registered?
curl -s ${AGENTFIELD_SERVER:-http://localhost:8080}/api/v1/nodes | grep -o '"swe-[a-z-]*"' | sort -u
```

Pick the node (`swe-planner`, `swe-fast`, or the `-go` twins — the
`implement_issue` surface is identical). If an `X-API-Key` is configured for
the control plane, add `-H "X-API-Key: $AGENTFIELD_API_KEY"` to every curl.

## Compose the issue

Write the spec from YOUR context — the whole point is that the sub-harness
skips planning. Required: `title`, `description`. Strongly recommended:
`acceptance_criteria` (verifier checks these), `files_to_modify` /
`files_to_create`, `testing_strategy`. Set `needs_deeper_qa: true` only for
risky/interface-heavy changes (doubles the review cost per iteration).

Commit (or at least be aware of) your local state first: the issue branch is
created from the **committed** base, so your uncommitted edits are invisible
to it.

## Fire the delegation (async, never sync)

```bash
curl -s -X POST ${AGENTFIELD_SERVER:-http://localhost:8080}/api/v1/execute/async/swe-planner.implement_issue \
  -H "Content-Type: application/json" \
  -d @issue.json
```

with `issue.json` shaped like:

```json
{
  "input": {
    "issue": {
      "title": "...",
      "description": "...",
      "acceptance_criteria": ["..."],
      "files_to_modify": ["..."],
      "testing_strategy": "..."
    },
    "repo_path": "/abs/path/to/checkout",
    "base_branch": "main",
    "config": { "models": { "default": "haiku" } }
  }
}
```

Capture `execution_id` from the 202 response. Track every in-flight
delegation in your todo list (one item per execution_id).

**Cap fan-out at 3 concurrent delegations per repo** unless the user
explicitly asks for more — each one is a paid multi-agent run, and an
unbounded loop of delegations is a surprise bill. Never re-fire a delegation
just because it is slow; poll it.

## Track progress

```bash
# Terminal status + result
curl -s $SERVER/api/v1/executions/<execution_id>
# Live agent notes (what the coder/reviewer are doing)
curl -s $SERVER/api/v1/executions/<execution_id>/notes
```

Poll on a backoff (30–60s is plenty; a typical issue lands in 10–30 min).
Batch-check several: `POST /api/v1/executions/batch-status` with
`{"execution_ids": [...]}`.

## Collect the result

The execution's `result` is an IssueBuildResult:

- `success: true` → `branch` holds the implementation
  (`issue/<build_id>-<slug>`). Review the diff yourself
  (`git diff main...<branch>`), run YOUR test gate, then merge:
  `git merge --no-ff <branch>` (or cherry-pick), and delete the branch.
- `success: false` with a `branch` → partial work was salvaged. Triage:
  read `verification` / `debt_items` / `error_message`, then either fix
  forward on the branch yourself, re-delegate with a sharpened spec, or drop
  the branch (`git branch -D`).
- `success: false` with `branch: ""` → nothing usable was produced; the
  sub-harness already cleaned up. Sharpen the spec before retrying — do not
  retry verbatim.

After merging all accepted branches, run the project's full test suite once
yourself before reporting done. The sub-harness verifier is a per-issue
check, not your integration gate.
