"""Unit tests for the ``resolve`` entry reasoner and its support helpers.

The heavy work in ``resolve()`` is shelled out to ``subprocess`` (clone,
fetch, merge, push, gh CLI) and to a single ``run_pr_resolver`` reasoner
call. These tests mock those boundaries so the orchestration shape — input
validation, merge-state classification, agent invocation kwargs, thread
reply pass — can be exercised without touching the network or the harness.
"""
from __future__ import annotations

import os
import subprocess
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("AGENTFIELD_SERVER", "http://localhost:9999")


# ---------------------------------------------------------------------------
# Schema sanity
# ---------------------------------------------------------------------------


class TestResolveSchemas:
    def test_review_comment_ref_round_trip(self) -> None:
        from swe_af.execution.schemas import ReviewCommentRef

        rc = ReviewCommentRef(
            comment_id=42,
            thread_id="PRRT_xyz",
            path="foo/bar.py",
            line=17,
            author="alice",
            body="please rename this",
            url="https://example/comments/42",
        )
        assert rc.model_dump()["comment_id"] == 42
        assert ReviewCommentRef(**rc.model_dump()) == rc

    def test_review_comment_ref_defaults_for_non_review_comment(self) -> None:
        """PR-conversation comments have no thread or anchor — defaults must allow that."""
        from swe_af.execution.schemas import ReviewCommentRef

        rc = ReviewCommentRef(author="alice", body="thoughts?")
        assert rc.comment_id == 0
        assert rc.thread_id == ""
        assert rc.path == ""
        assert rc.line == 0

    def test_pr_resolve_result_defaults(self) -> None:
        from swe_af.execution.schemas import PRResolveResult

        rr = PRResolveResult(fixed=True)
        # All optional fields default to falsy/empty so a minimal agent
        # response still parses.
        assert rr.merge_resolved is False
        assert rr.commit_shas == []
        assert rr.addressed_comments == []
        assert rr.pushed is False

    def test_addressed_comment_round_trip(self) -> None:
        from swe_af.execution.schemas import AddressedComment

        ac = AddressedComment(
            comment_id=1,
            thread_id="PRRT_x",
            addressed=True,
            note="Fixed by renaming foo→bar",
        )
        assert ac.addressed is True
        assert AddressedComment(**ac.model_dump()) == ac


# ---------------------------------------------------------------------------
# _attempt_base_merge — merge-state classification
# ---------------------------------------------------------------------------


def _make_completed_process(returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr,
    )


class TestAttemptBaseMerge:
    """The helper must classify merge state without relying on a real git repo."""

    def test_skipped_when_fetch_fails(self) -> None:
        from swe_af.app import _attempt_base_merge

        with patch("swe_af.app.subprocess.run") as run:
            run.return_value = _make_completed_process(1, stderr="no remote")
            state, conflicts = _attempt_base_merge(
                repo_path="/tmp/x", base_branch="main",
            )
        assert state == "skipped"
        assert conflicts == []
        # Only the fetch call was made — we shouldn't try to merge if fetch
        # didn't even succeed.
        assert run.call_count == 1

    def test_clean_when_base_already_ancestor(self) -> None:
        from swe_af.app import _attempt_base_merge

        # Sequence: fetch ok, merge-base --is-ancestor returns 0 ("yes").
        with patch("swe_af.app.subprocess.run") as run:
            run.side_effect = [
                _make_completed_process(0),  # fetch
                _make_completed_process(0),  # merge-base --is-ancestor (0 == is-ancestor)
            ]
            state, conflicts = _attempt_base_merge(
                repo_path="/tmp/x", base_branch="main",
            )
        assert state == "clean"
        assert conflicts == []

    def test_merged_when_merge_succeeds(self) -> None:
        from swe_af.app import _attempt_base_merge

        with patch("swe_af.app.subprocess.run") as run:
            run.side_effect = [
                _make_completed_process(0),  # fetch
                _make_completed_process(1),  # merge-base --is-ancestor (1 == not-ancestor)
                _make_completed_process(0),  # git merge succeeds
            ]
            state, conflicts = _attempt_base_merge(
                repo_path="/tmp/x", base_branch="main",
            )
        assert state == "merged"
        assert conflicts == []

    def test_conflict_lists_unmerged_files(self) -> None:
        from swe_af.app import _attempt_base_merge

        with patch("swe_af.app.subprocess.run") as run:
            run.side_effect = [
                _make_completed_process(0),  # fetch
                _make_completed_process(1),  # not ancestor
                _make_completed_process(1, stderr="CONFLICT"),  # merge fails
                _make_completed_process(
                    0,
                    stdout="src/a.py\nsrc/b.py\n\n",
                ),  # git diff --name-only --diff-filter=U
            ]
            state, conflicts = _attempt_base_merge(
                repo_path="/tmp/x", base_branch="main",
            )
        assert state == "conflict"
        assert conflicts == ["src/a.py", "src/b.py"]


# ---------------------------------------------------------------------------
# resolve() input validation
# ---------------------------------------------------------------------------


class TestResolveInputValidation:
    @pytest.mark.asyncio
    async def test_missing_required_args_raises(self) -> None:
        import swe_af.app as app_mod

        with pytest.raises(ValueError, match="non-empty pr_url"):
            await app_mod.resolve(
                pr_url="",
                pr_number=1,
                repo_url="https://github.com/o/r.git",
                head_branch="feature/x",
            )

        with pytest.raises(ValueError):
            await app_mod.resolve(
                pr_url="https://github.com/o/r/pull/1",
                pr_number=0,
                repo_url="https://github.com/o/r.git",
                head_branch="feature/x",
            )

        with pytest.raises(ValueError):
            await app_mod.resolve(
                pr_url="https://github.com/o/r/pull/1",
                pr_number=1,
                repo_url="https://github.com/o/r.git",
                head_branch="",
            )


# ---------------------------------------------------------------------------
# resolve() orchestration shape
# ---------------------------------------------------------------------------


class TestResolveOrchestration:
    """The end-to-end happy path: clone → checkout → merge → resolver → push → CI gate → threads."""

    @pytest.mark.asyncio
    async def test_resolve_calls_resolver_with_merge_state_and_pushes_threads(
        self, tmp_path, monkeypatch
    ) -> None:
        """resolve() must:
        - clone the repo,
        - run the merge helper,
        - call run_pr_resolver with merge_state + conflicted_files + ci_failures + review_comments,
        - skip PR creation (no run_github_pr call),
        - run the CI gate,
        - post replies + resolve threads for every addressed comment.
        """
        import swe_af.app as app_mod
        from swe_af.execution.schemas import PRResolveResult

        # 1. Mock the merge helper so we can pin merge_state in the test.
        monkeypatch.setattr(
            app_mod,
            "_attempt_base_merge",
            lambda *, repo_path, base_branch: ("merged", []),
        )

        # 2. Mock subprocess so clone / checkout / fetch / push / gh-api all
        #    succeed without touching the filesystem or network. We also
        #    capture the calls so we can assert "no `gh pr create`" later.
        captured_subprocess: list[list[str]] = []

        def fake_run(cmd, *args, **kwargs):
            captured_subprocess.append(list(cmd))
            # `git rev-parse HEAD` must return the new head sha so the gate
            # can anchor the watcher; everything else returns empty stdout.
            if cmd[:3] == ["git", "rev-parse", "HEAD"]:
                return _make_completed_process(0, stdout="newsha-abc\n", stderr="")
            return _make_completed_process(0, stdout="", stderr="")

        monkeypatch.setattr(app_mod.subprocess, "run", fake_run)

        # 3. Mock os.makedirs + asyncio.sleep so we don't actually wait the
        #    startup grace period in tests.
        monkeypatch.setattr(app_mod.os, "makedirs", lambda *a, **k: None)
        slept: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            slept.append(seconds)

        monkeypatch.setattr(app_mod.asyncio, "sleep", fake_sleep)

        # 4. Mock the resolver agent reasoner. The reply pass keys off
        #    addressed_comments — return one addressed and one not.
        resolver_payload = PRResolveResult(
            fixed=True,
            merge_resolved=True,
            files_changed=["src/a.py"],
            commit_shas=["abc123"],
            pushed=True,
            addressed_comments=[
                {
                    "comment_id": 11,
                    "thread_id": "PRRT_aaa",
                    "addressed": True,
                    "note": "Renamed foo to bar",
                },
                {
                    "comment_id": 22,
                    "thread_id": "PRRT_bbb",
                    "addressed": False,
                    "note": "Not actionable: question only",
                },
            ],
            summary="resolver summary",
        ).model_dump()

        # 5. Mock the CI gate so we don't poll a real PR.
        captured_ci_gate_kwargs: dict = {}

        async def fake_ci_gate(**kwargs):
            captured_ci_gate_kwargs.update(kwargs)
            return {"final_status": "passed", "fix_attempts": [], "watch": {}}

        monkeypatch.setattr(app_mod, "_run_ci_gate", fake_ci_gate)

        # 6. Mock app.call so the resolver invocation returns our payload, and
        #    so we can assert run_github_pr is NEVER called.
        captured_calls: list[tuple[str, dict]] = []

        async def fake_call(target, **kwargs):
            captured_calls.append((target, kwargs))
            if "run_pr_resolver" in target:
                return {"result": resolver_payload}
            return {"result": {}}

        monkeypatch.setattr(app_mod.app, "call", fake_call)
        monkeypatch.setattr(app_mod.app, "note", lambda *a, **k: None)

        def mock_unwrap(raw, name):
            if isinstance(raw, dict) and "result" in raw:
                return raw["result"]
            return raw

        monkeypatch.setattr(app_mod, "_unwrap", mock_unwrap)

        # ---- act ----------------------------------------------------------
        result = await app_mod.resolve(
            pr_url="https://github.com/o/r/pull/7",
            pr_number=7,
            repo_url="https://github.com/o/r.git",
            head_branch="feature/x",
            base_branch="main",
            ci_failures=[
                {"name": "tests", "logs_excerpt": "AssertionError"},
            ],
            review_comments=[
                {
                    "comment_id": 11,
                    "thread_id": "PRRT_aaa",
                    "path": "src/a.py",
                    "line": 5,
                    "author": "alice",
                    "body": "please rename this",
                },
                {
                    "comment_id": 22,
                    "thread_id": "PRRT_bbb",
                    "path": "src/b.py",
                    "line": 10,
                    "author": "bob",
                    "body": "what about this?",
                },
            ],
        )

        # ---- assert: resolver was called with the right kwargs ------------
        resolver_calls = [
            (t, kw) for t, kw in captured_calls if "run_pr_resolver" in t
        ]
        assert len(resolver_calls) == 1
        _, kw = resolver_calls[0]
        assert kw["pr_number"] == 7
        assert kw["head_branch"] == "feature/x"
        assert kw["base_branch"] == "main"
        assert kw["merge_state"] == "merged"
        assert kw["conflicted_files"] == []
        assert len(kw["failed_checks"]) == 1
        assert len(kw["review_comments"]) == 2

        # ---- assert: PR creation was NOT triggered ------------------------
        github_pr_calls = [
            t for t, _ in captured_calls if "run_github_pr" in t
        ]
        assert github_pr_calls == [], (
            f"resolve() must never create a PR; got: {github_pr_calls}"
        )
        # And no shell-level `gh pr create` either.
        for cmd in captured_subprocess:
            assert not (cmd[:3] == ["gh", "pr", "create"]), cmd

        # ---- assert: CI gate ran with PR number + head as integration ----
        assert captured_ci_gate_kwargs["pr_number"] == 7
        assert captured_ci_gate_kwargs["integration_branch"] == "feature/x"
        assert captured_ci_gate_kwargs["base_branch"] == "main"
        # SHA-anchor: must be the post-push HEAD captured from `git rev-parse`.
        assert captured_ci_gate_kwargs["head_sha"] == "newsha-abc"

        # ---- assert: startup grace fired before the gate ran -------------
        # The grace sleep is the only asyncio.sleep we call in resolve(),
        # and it must fire before _run_ci_gate. Default is 30s.
        assert any(s >= 30 for s in slept), (
            f"expected a >=30s grace sleep before the CI gate; got {slept}"
        )

        # ---- assert: thread replies posted only for addressed=true -------
        assert len(result["thread_replies"]) == 1
        reply = result["thread_replies"][0]
        assert reply["comment_id"] == 11
        assert reply["thread_id"] == "PRRT_aaa"
        assert reply["replied"] is True
        assert reply["resolved"] is True

        # ---- assert: gh subprocess saw the inline reply + GraphQL mutation
        gh_api_cmds = [
            cmd for cmd in captured_subprocess
            if cmd and cmd[0] == "gh" and cmd[1] == "api"
        ]
        # One reply POST + one graphql mutation (skipped for the not-addressed comment).
        assert len(gh_api_cmds) == 2
        # The reply path must include the comment id of the addressed one.
        reply_cmd = next(c for c in gh_api_cmds if "replies" in " ".join(c))
        assert "/comments/11/replies" in " ".join(reply_cmd)
        # The graphql mutation must reference the resolved thread id.
        graphql_cmd = next(c for c in gh_api_cmds if "graphql" in c)
        assert "id=PRRT_aaa" in graphql_cmd

        # ---- assert: top-level shape -------------------------------------
        assert result["pr_number"] == 7
        assert result["head_branch"] == "feature/x"
        assert result["merge_state"] == "merged"
        assert result["success"] is True
        assert result["resolve_result"]["pushed"] is True
        assert result["ci_gate"]["final_status"] == "passed"


class TestResolvePushFallback:
    """If the agent committed but didn't push, resolve() must push for it."""

    @pytest.mark.asyncio
    async def test_resolve_pushes_when_agent_committed_but_didnt_push(
        self, tmp_path, monkeypatch
    ) -> None:
        import swe_af.app as app_mod
        from swe_af.execution.schemas import PRResolveResult

        monkeypatch.setattr(
            app_mod,
            "_attempt_base_merge",
            lambda *, repo_path, base_branch: ("clean", []),
        )

        push_invocations: list[list[str]] = []

        def fake_run(cmd, *args, **kwargs):
            if cmd and cmd[:2] == ["git", "push"]:
                push_invocations.append(list(cmd))
            return _make_completed_process(0)

        monkeypatch.setattr(app_mod.subprocess, "run", fake_run)
        monkeypatch.setattr(app_mod.os, "makedirs", lambda *a, **k: None)
        # The startup grace period adds an asyncio.sleep before the gate;
        # mock so the test doesn't wait 30s in real time.
        async def _no_sleep(_seconds: float) -> None:
            return None
        monkeypatch.setattr(app_mod.asyncio, "sleep", _no_sleep)

        # Resolver returned commits but pushed=False.
        resolver_payload = PRResolveResult(
            fixed=True,
            commit_shas=["abc"],
            pushed=False,
        ).model_dump()

        async def fake_call(target, **kwargs):
            if "run_pr_resolver" in target:
                return {"result": resolver_payload}
            return {"result": {}}

        async def fake_ci_gate(**kwargs):
            return {"final_status": "passed", "fix_attempts": [], "watch": {}}

        monkeypatch.setattr(app_mod.app, "call", fake_call)
        monkeypatch.setattr(app_mod.app, "note", lambda *a, **k: None)
        monkeypatch.setattr(app_mod, "_run_ci_gate", fake_ci_gate)

        def mock_unwrap(raw, name):
            return raw["result"] if isinstance(raw, dict) and "result" in raw else raw

        monkeypatch.setattr(app_mod, "_unwrap", mock_unwrap)

        result = await app_mod.resolve(
            pr_url="https://github.com/o/r/pull/9",
            pr_number=9,
            repo_url="https://github.com/o/r.git",
            head_branch="feature/y",
            base_branch="main",
        )

        # The orchestrator pushed exactly once on the agent's behalf.
        assert any(
            cmd[:4] == ["git", "push", "origin", "feature/y"]
            for cmd in push_invocations
        ), push_invocations
        assert result["resolve_result"]["pushed"] is True


# ---------------------------------------------------------------------------
# pr_resolver_task_prompt — goal field rendering
# ---------------------------------------------------------------------------


class TestPrResolverTaskPromptGoal:
    """The optional `goal` field reframes the resolver's primary task.

    When the caller (e.g. github-buddy's `make_changes` command) passes a
    free-form instruction, it must appear as the headline task with CI /
    comments demoted to secondary work. When omitted, output must be
    unchanged from the prior comments-and-CI-only flow.
    """

    def _build(self, **overrides):
        from swe_af.prompts.pr_resolver import pr_resolver_task_prompt

        defaults = dict(
            repo_path="/workspaces/r",
            pr_number=7,
            pr_url="https://github.com/o/r/pull/7",
            head_branch="feature/x",
            base_branch="main",
            merge_state="clean",
            conflicted_files=[],
            failed_checks=[],
            review_comments=[],
        )
        defaults.update(overrides)
        return pr_resolver_task_prompt(**defaults)

    def test_no_goal_omits_user_request_section(self) -> None:
        prompt = self._build()
        assert "User-requested change" not in prompt
        assert "Apply the user-requested change" not in prompt
        # Without a goal the first numbered task is still merge completion.
        assert "1. Complete any in-progress merge from base." in prompt

    def test_goal_renders_section_and_promotes_to_step_one(self) -> None:
        prompt = self._build(goal="Rename foo() to bar() across the package.")

        assert "### User-requested change (primary instruction)" in prompt
        assert "Rename foo() to bar() across the package." in prompt
        assert "1. Apply the user-requested change described above." in prompt
        # CI / comments / merge are demoted to later steps.
        assert "2. Complete any in-progress merge from base." in prompt

    def test_goal_coexists_with_ci_failures_and_comments(self) -> None:
        from swe_af.execution.schemas import CIFailedCheck, ReviewCommentRef

        prompt = self._build(
            goal="Fix the typo on line 42 of README.md.",
            failed_checks=[
                CIFailedCheck(
                    name="unit",
                    workflow="CI",
                    conclusion="failure",
                    details_url="https://example.com/run/1",
                    logs_excerpt="AssertionError: expected 1 got 2",
                ),
            ],
            review_comments=[
                ReviewCommentRef(
                    comment_id=42,
                    thread_id="T_abc",
                    path="src/a.py",
                    line=10,
                    author="alice",
                    body="please rename",
                    url="https://github.com/o/r/pull/7#discussion_r42",
                ),
            ],
        )

        assert "### User-requested change (primary instruction)" in prompt
        assert "Fix the typo on line 42 of README.md." in prompt
        assert "### Failing CI checks" in prompt
        assert "AssertionError" in prompt
        assert "### Review comments to address" in prompt
        assert "please rename" in prompt
        # User-requested change is rendered before CI / comments in document order.
        goal_at = prompt.index("User-requested change")
        ci_at = prompt.index("Failing CI checks")
        comments_at = prompt.index("Review comments to address")
        assert goal_at < ci_at < comments_at
