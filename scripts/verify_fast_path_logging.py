"""Verification script for fast-path observability logging.

This script validates that the coding loop contains:
1. Fast-path detection logic (iteration == 1 check) in the approve block
2. Fast-path logging with 'fast_path' tag for observability
"""

import re
import sys
from pathlib import Path


def test_fast_path_logging_exists():
    """Test that fast-path detection and logging are present in the approve block."""
    # Read the coding_loop.py file
    coding_loop_path = Path(__file__).parent.parent / "swe_af" / "execution" / "coding_loop.py"

    if not coding_loop_path.exists():
        raise FileNotFoundError(f"Could not find coding_loop.py at {coding_loop_path}")

    content = coding_loop_path.read_text()

    # Find the approve block in the "BRANCH ON ACTION" section
    # This is the second 'if action == "approve"' block after the comment
    branch_section_pattern = r'# --- 4\. BRANCH ON ACTION ---.*?if action == "approve":(.*?)(?=\n\s{8}if action ==)'
    approve_match = re.search(branch_section_pattern, content, re.DOTALL)

    if not approve_match:
        raise AssertionError("Could not find 'if action == \"approve\":' block in coding_loop.py")

    approve_block = approve_match.group(1)

    # Check for iteration == 1 condition
    iteration_check_pattern = r'if iteration == 1:'
    if not re.search(iteration_check_pattern, approve_block):
        raise AssertionError(
            "Fast-path detection missing: 'if iteration == 1:' condition not found in approve block"
        )

    # Check for fast_path tag in logging
    fast_path_tag_pattern = r'tags=\[.*?["\']fast_path["\'].*?\]'
    if not re.search(fast_path_tag_pattern, approve_block):
        raise AssertionError(
            "Fast-path logging missing: 'fast_path' tag not found in logging call within approve block"
        )

    print("✓ Fast-path detection logic present (iteration == 1 check)")
    print("✓ Fast-path logging with 'fast_path' tag present")
    print("\nAll verification checks passed!")


if __name__ == "__main__":
    try:
        test_fast_path_logging_exists()
        sys.exit(0)
    except (AssertionError, FileNotFoundError) as e:
        print(f"✗ Verification failed: {e}", file=sys.stderr)
        sys.exit(1)
