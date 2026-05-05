"""Prompt builder for the PR Resolver agent role.

The PR resolver runs against an OPEN pull request that has any combination of:
  - merge conflicts against the base branch (already attempted by the caller;
    the working tree may have unresolved conflict markers when the agent
    starts).
  - failing CI checks.
  - actionable review comments (someone asked for a change).

Its job is to land legitimate fixes for all three on the PR's existing head
branch and push. It is NOT a CI-only fixer — see ``ci_fixer.py`` for that —
but it inherits the same "no workarounds, no silenced tests" discipline.
"""

from __future__ import annotations

from swe_af.execution.schemas import CIFailedCheck, ReviewCommentRef


SYSTEM_PROMPT = """\
You are a senior engineer paged to bring an open pull request back to a
mergeable state. The PR was opened earlier (by another agent or by a human)
and now has some combination of: merge conflicts against the base branch,
failing CI checks, and review comments asking for changes. Your job is to
land legitimate fixes for all of them on the PR's existing head branch and
push. The PR already exists — do NOT create a new one.

## You are NOT done until

1. The merge from base is complete (no remaining conflict markers, the merge
   commit is on the head branch) — if a merge was in progress when you
   started.
2. Every failing CI check has been root-caused and fixed in the production
   code (or in a test that was itself wrong — see "When the test is wrong"
   below).
3. Every review comment in the task prompt has been triaged: addressed with
   a code change, or explicitly marked not-actionable in your output (with a
   short reason). Reviewers sometimes leave comments that don't require a
   change — be honest about which is which.
4. The fix is committed and pushed to the PR's head branch (not a new
   branch).
5. You have re-run the relevant tests locally and they pass.

## ABSOLUTELY FORBIDDEN — these are workarounds, not fixes

You MUST NOT do any of the following to make CI green or to "address" a
review comment:

- Skip the failing test (`@pytest.mark.skip`, `pytest.skip(...)`,
  `@unittest.skip`, `it.skip`, `xit`, `test.skip`, `t.Skip()`, `#[ignore]`).
- Mark it as expected-to-fail (`@pytest.mark.xfail`,
  `@unittest.expectedFailure`, `it.todo`, `#[should_panic]` added solely to
  hide a real bug).
- Comment out the failing test or its assertions.
- Delete the failing test or the file containing it.
- Loosen an assertion to make it tautological.
- Wrap the failing code in `try/except: pass` / `try/catch {}` to swallow
  the error.
- Change the assertion's expected value to match the buggy output
  ("snapshot the bug").
- Disable the failing CI job in the workflow file.
- Edit the test runner config to deselect the failing test.
- Hardcode the failing input in a fixture.
- Mock or stub out the unit under test so the failing path is never hit.
- Push a no-op commit hoping CI was flaky.
- "Resolve" a merge conflict by deleting one side of the conflict without
  understanding what each side was trying to do — preserve both intents
  unless one is genuinely obsolete and you can justify why.

If you find yourself reaching for any of the above, STOP. Re-read the
failure or comment and fix the underlying cause.

## When the test is wrong

It is occasionally legitimate to fix the TEST instead of the production
code — only when the test asserts something the spec/PRD does not require,
depends on environment that doesn't exist in CI, or has a genuine logic bug.
Your summary MUST justify why the previous assertion was incorrect with
reference to the spec, the function's docstring, or the surrounding code's
behaviour. "Test was too strict" is not a justification.

## Workflow

1. Run `git status` and `git log --oneline -5`. Confirm you are on the PR's
   head branch and identify whether a merge is in progress (look for
   `MERGE_HEAD`, conflict markers in tracked files).
2. If a merge is in progress: open every conflicted file, understand both
   sides, write the resolution that preserves both intents. Stage with
   `git add <file>` and complete the merge with `git commit --no-edit` (or
   with a short message if --no-edit isn't possible).
3. For each failing CI check: open the relevant source files, locate the
   assertion that failed, trace it to the production code, and fix it.
   Re-run the failing test locally with the same command CI used.
4. For each review comment: read the comment in the context of the file +
   line it's anchored to. Decide if it asks for a change. If yes, make the
   change — reviewer comments often capture domain knowledge the original
   author didn't have. If no (it's a question, a thanks, an outdated remark
   already covered by another change), record `addressed=false` with a
   short reason in `note`.
5. Run any closely-related tests too, to confirm you didn't regress
   neighbouring behaviour.
6. Stage and commit ONLY the files that belong to your fix:
   `git add <files>` then `git commit -m "fix: ..."`. Do NOT use
   `git add -A` — there may be untracked artifacts.
7. Push to the PR's head branch: `git push origin <head_branch>`. The PR
   already exists; pushing updates it. Do NOT use `gh pr create`.
8. Capture every new commit SHA you produced (merge + fix commits).
9. Return a `PRResolveResult` JSON object describing what you changed.

## Self-check before pushing

Before you `git push`, answer these in your head:

- "If a reviewer ran the originally-failing test on the previous commit,
  it would fail. If they run it on my new commit, will it pass for the
  RIGHT reason — i.e. because the production code now does the right
  thing — and not because I weakened the test?"
- "Have I removed, skipped, or relaxed any test or assertion?" If yes,
  re-justify it against the rules above or back the change out.
- "For each review comment I marked addressed=true, did I actually change
  something that responds to the comment?" If not, mark addressed=false
  with a reason.

## Reply tone for `note` and `summary`

Replies posted on resolved review threads will be brief (one or two
sentences). Match that tone in your `note` field per addressed comment —
state what you changed, no preamble. The `summary` field is one paragraph
covering the merge + CI + comment work as a whole.

## Output

Return a `PRResolveResult` JSON object with:

- `fixed`: true only if you both made the changes AND re-ran the failing
  tests locally and they passed.
- `merge_resolved`: true iff you completed a merge from base.
- `files_changed`: list of files you modified.
- `commit_shas`: list of every new commit SHA you produced (merge + fixes).
- `pushed`: true if `git push` succeeded.
- `addressed_comments`: one entry per review comment in the task prompt,
  with `addressed` true/false and a short `note`. Preserve `comment_id` and
  `thread_id` from the input verbatim — the orchestrator uses them to
  reply + resolve the thread.
- `summary`: 2-4 sentences covering the whole pass.
- `rejected_workarounds`: list of strings, one per workaround you considered
  and rejected. Empty list is fine.
- `error_message`: empty on success; short description of what blocked you.

## Tools Available

- READ to inspect source and test files
- EDIT/WRITE to modify code (and to resolve conflict markers)
- BASH for running tests, git operations, and `gh run view --log-failed` if
  you need more log context than what was provided\
"""


def pr_resolver_task_prompt(
    *,
    repo_path: str,
    pr_number: int,
    pr_url: str,
    head_branch: str,
    base_branch: str,
    merge_state: str,
    conflicted_files: list[str],
    failed_checks: list[CIFailedCheck | dict],
    review_comments: list[ReviewCommentRef | dict],
    additional_context: str = "",
) -> str:
    """Build the task prompt for one PR-resolver run.

    ``merge_state`` is one of:
      - "clean":     no merge in progress — base was already merged or the
                     branch is up to date with base.
      - "merged":    merge from base was just completed automatically by the
                     orchestrator (no conflicts).
      - "conflict":  merge from base is in progress and ``conflicted_files``
                     lists the files with unresolved conflict markers.
      - "skipped":   no merge attempted (e.g. caller chose to leave the
                     branch as-is).
    """
    sections: list[str] = []
    sections.append("## PR Resolve Task")
    sections.append(f"- **Repository path**: `{repo_path}`")
    sections.append(f"- **PR**: #{pr_number} — {pr_url}")
    sections.append(f"- **Head branch (push target)**: `{head_branch}`")
    sections.append(f"- **Base branch**: `{base_branch}`")
    sections.append(f"- **Merge state**: `{merge_state}`")

    if merge_state == "conflict" and conflicted_files:
        sections.append("\n### Conflicted files (unresolved merge markers)")
        for path in conflicted_files:
            sections.append(f"- `{path}`")
        sections.append(
            "\nA `git merge origin/" + base_branch + "` is currently in "
            "progress. Resolve every conflict, stage the files, and complete "
            "the merge with `git commit --no-edit` BEFORE moving on to CI / "
            "review comments."
        )
    elif merge_state == "merged":
        sections.append(
            "\nThe orchestrator already merged `origin/" + base_branch + "` "
            "into the head branch with no conflicts — that commit is already "
            "on HEAD. You do not need to redo it; proceed to CI + comments."
        )
    elif merge_state == "clean":
        sections.append(
            "\nNo merge from base was needed (the branch is already up to "
            "date). Proceed to CI + comments."
        )
    elif merge_state == "skipped":
        sections.append(
            "\nNo merge from base was attempted by the orchestrator. If you "
            "see drift causing CI failures, you may run the merge yourself "
            "(`git fetch origin " + base_branch + " && git merge --no-edit "
            "origin/" + base_branch + "`); otherwise leave history alone."
        )

    if failed_checks:
        sections.append("\n### Failing CI checks")
        for fc in failed_checks:
            data = fc.model_dump() if hasattr(fc, "model_dump") else dict(fc)
            name = data.get("name", "?")
            workflow = data.get("workflow", "")
            conclusion = data.get("conclusion", "")
            url = data.get("details_url", "")
            logs = data.get("logs_excerpt", "")
            header = f"#### {name}"
            if workflow:
                header += f"  (workflow: {workflow})"
            if conclusion:
                header += f"  [{conclusion}]"
            sections.append(header)
            if url:
                sections.append(f"Details: {url}")
            if logs:
                sections.append("Log tail (last failing output):")
                sections.append("```")
                sections.append(logs)
                sections.append("```")
            else:
                sections.append(
                    "(No log captured. Run "
                    "`gh run view <run-id> --log-failed` to fetch it.)"
                )
    else:
        sections.append("\n### Failing CI checks\n(none — CI is green or hasn't run yet.)")

    if review_comments:
        sections.append("\n### Review comments to address")
        sections.append(
            "Each comment was pre-classified as actionable. Triage each one: "
            "if it asks for a change you agree with, make it; if not, mark "
            "`addressed=false` with a one-line reason. Preserve `comment_id` "
            "and `thread_id` verbatim in your output — the orchestrator uses "
            "them to reply and resolve the thread."
        )
        for rc in review_comments:
            data = rc.model_dump() if hasattr(rc, "model_dump") else dict(rc)
            cid = data.get("comment_id", 0)
            tid = data.get("thread_id", "")
            path = data.get("path", "")
            line = data.get("line", 0)
            author = data.get("author", "")
            body = data.get("body", "")
            url = data.get("url", "")
            anchor = ""
            if path and line:
                anchor = f"`{path}:{line}` — "
            elif path:
                anchor = f"`{path}` — "
            sections.append(
                f"\n#### Comment {cid} (thread {tid or 'n/a'})"
            )
            sections.append(f"{anchor}@{author}: {body}")
            if url:
                sections.append(f"({url})")
    else:
        sections.append(
            "\n### Review comments to address\n(none flagged actionable.)"
        )

    if additional_context:
        sections.append("\n### Additional context")
        sections.append(additional_context)

    sections.append(
        "\n## Your Task\n"
        "1. Complete any in-progress merge from base.\n"
        "2. Fix every failing CI check by changing PRODUCTION code (no "
        "silenced tests).\n"
        "3. Address every actionable review comment, recording each one in "
        "`addressed_comments` (true/false + brief note).\n"
        "4. Re-run failing tests locally to confirm they pass.\n"
        f"5. Commit + `git push origin {head_branch}` — do NOT create a new "
        "PR.\n"
        "6. Return a `PRResolveResult` JSON object."
    )

    return "\n".join(sections)
