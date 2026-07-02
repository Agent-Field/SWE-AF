from swe_af.prompts.repo_finalize import SYSTEM_PROMPT, repo_finalize_task_prompt


def test_repo_finalize_prompt_preserves_swe_af_handoff_artifacts():
    prompt = repo_finalize_task_prompt("/tmp/repo")
    combined = SYSTEM_PROMPT + "\n" + prompt

    assert "git ls-files" in combined
    assert "Never delete tracked files" in combined
    assert ".artifacts/plan/" in combined
    assert ".artifacts/execution/" in combined
    assert ".artifacts/verification/" in combined
    assert ".artifacts/build_state.json" in combined
    assert "untracked or ignored" in combined
