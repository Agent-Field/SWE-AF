"""Verification script for turn budget right-sizing.

Tests that all 17 execution agent functions in execution_agents.py have the
correct role-specific max_turns values per the architecture specification.
"""

import re
import subprocess
import sys
from pathlib import Path


def test_all_turn_budgets_match_specification():
    """Verify all 17 agent functions have correct max_turns values."""
    # Expected turn budgets per architecture specification
    expected = {
        'run_retry_advisor': 20,
        'run_issue_advisor': 20,
        'run_replanner': 30,
        'run_issue_writer': 20,
        'run_verifier': 20,
        'run_git_init': 10,
        'run_workspace_setup': 10,
        'run_merger': 10,
        'run_integration_tester': 10,
        'run_workspace_cleanup': 10,
        'run_coder': 50,
        'run_qa': 20,
        'run_code_reviewer': 20,
        'run_qa_synthesizer': 10,
        'generate_fix_issues': 20,
        'run_repo_finalize': 10,
        'run_github_pr': 10,
    }

    # Read the execution_agents.py file
    file_path = Path(__file__).parent.parent / 'swe_af' / 'reasoners' / 'execution_agents.py'
    with open(file_path) as f:
        content = f.read()

    errors = []
    for func_name, expected_turns in expected.items():
        # Find function definition and extract max_turns value
        # Pattern matches: async def func_name(...) followed by AgentAI(AgentAIConfig(...max_turns=N...))
        pattern = rf'async def {func_name}\(.*?max_turns=(\d+)'
        match = re.search(pattern, content, re.DOTALL)

        if not match:
            errors.append(f'{func_name}: max_turns assignment not found in AgentAIConfig')
            continue

        actual_turns = int(match.group(1))
        if actual_turns != expected_turns:
            errors.append(
                f'{func_name}: expected max_turns={expected_turns}, got {actual_turns}'
            )

    # Assert no errors
    if errors:
        error_msg = '\n'.join(f'  - {e}' for e in errors)
        raise AssertionError(f'Turn budget verification failed:\n{error_msg}')

    print(f'✓ All {len(expected)} turn budgets match specification')


def test_no_default_agent_max_turns_usage():
    """Verify DEFAULT_AGENT_MAX_TURNS is not used in agent configs (except import)."""
    file_path = Path(__file__).parent.parent / 'swe_af' / 'reasoners' / 'execution_agents.py'

    # Run grep to find all occurrences, excluding line 16 (the import)
    result = subprocess.run(
        ['grep', '-n', 'DEFAULT_AGENT_MAX_TURNS', str(file_path)],
        capture_output=True,
        text=True
    )

    if result.returncode != 0:
        # No matches found - this is good
        print('✓ No DEFAULT_AGENT_MAX_TURNS usage in agent configs')
        return

    # Filter out line 16 (import line)
    lines = [line for line in result.stdout.strip().split('\n') if not line.startswith('16:')]

    if lines:
        error_msg = '\n'.join(f'  Line {line}' for line in lines)
        raise AssertionError(
            f'DEFAULT_AGENT_MAX_TURNS still used in agent configs:\n{error_msg}'
        )

    print('✓ No DEFAULT_AGENT_MAX_TURNS usage in agent configs')


def test_all_17_functions_checked():
    """Verify we're checking exactly 17 functions (edge case test)."""
    expected_count = 17
    expected = {
        'run_retry_advisor': 20,
        'run_issue_advisor': 20,
        'run_replanner': 30,
        'run_issue_writer': 20,
        'run_verifier': 20,
        'run_git_init': 10,
        'run_workspace_setup': 10,
        'run_merger': 10,
        'run_integration_tester': 10,
        'run_workspace_cleanup': 10,
        'run_coder': 50,
        'run_qa': 20,
        'run_code_reviewer': 20,
        'run_qa_synthesizer': 10,
        'generate_fix_issues': 20,
        'run_repo_finalize': 10,
        'run_github_pr': 10,
    }

    actual_count = len(expected)
    if actual_count != expected_count:
        raise AssertionError(
            f'Expected to check {expected_count} functions, but only checking {actual_count}'
        )

    print(f'✓ Checking exactly {expected_count} functions as specified')


if __name__ == '__main__':
    # Run all tests
    try:
        test_all_17_functions_checked()
        test_all_turn_budgets_match_specification()
        test_no_default_agent_max_turns_usage()
        print('\n✅ All verification tests passed!')
        sys.exit(0)
    except AssertionError as e:
        print(f'\n❌ Verification failed:\n{e}')
        sys.exit(1)
