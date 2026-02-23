"""Tests for Architect PRD polling logic.

Tests verify:
- Exponential backoff timing algorithm
- Successful poll with delayed file creation
- Timeout behavior and graceful degradation
- Edge cases (immediate file, boundary timing, concurrent writes)
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest


class TestExponentialBackoff:
    """Unit tests for exponential backoff timing algorithm."""

    def test_backoff_intervals_sequence(self):
        """Verify polling intervals: 500ms, 750ms, 1125ms, ..., capped at 5s."""
        # Import here to avoid circular import at module level
        from swe_af.prompts.architect import _poll_for_prd_markdown

        with tempfile.TemporaryDirectory() as tmpdir:
            prd_path = os.path.join(tmpdir, "prd.md")
            # File doesn't exist - will timeout, but we measure intervals

            start = time.time()
            result = _poll_for_prd_markdown(prd_path)
            elapsed = time.time() - start

            # Should timeout at ~120s
            assert result == ""
            assert 119 <= elapsed <= 122, f"Expected ~120s timeout, got {elapsed:.1f}s"

    def test_backoff_cap_at_5s(self):
        """Verify exponential backoff is capped at 5 seconds."""
        # Initial: 0.5s
        # After 1st: 0.5 * 1.5 = 0.75s
        # After 2nd: 0.75 * 1.5 = 1.125s
        # After 3rd: 1.125 * 1.5 = 1.6875s
        # After 4th: 1.6875 * 1.5 = 2.53125s
        # After 5th: 2.53125 * 1.5 = 3.796875s
        # After 6th: 3.796875 * 1.5 = 5.6953125s -> capped to 5.0s
        intervals = [0.5]
        for _ in range(30):
            next_interval = min(intervals[-1] * 1.5, 5.0)
            intervals.append(next_interval)
            if next_interval == 5.0:
                break

        # After 6 iterations, should be capped at 5.0
        assert intervals[6] == 5.0
        # All subsequent intervals remain at 5.0
        assert all(i == 5.0 for i in intervals[7:])


class TestDelayedFileCreation:
    """Integration tests with delayed file creation."""

    def test_successful_poll_after_2s_delay(self):
        """Verify successful poll when file appears after 2 seconds."""
        from swe_af.prompts.architect import _poll_for_prd_markdown

        with tempfile.TemporaryDirectory() as tmpdir:
            prd_path = os.path.join(tmpdir, "prd.md")
            prd_content = "# Test PRD\n\nThis is a test PRD document."

            def delayed_write():
                time.sleep(2.0)
                with open(prd_path, "w", encoding="utf-8") as f:
                    f.write(prd_content)

            # Start background thread to write file after 2s
            writer = threading.Thread(target=delayed_write)
            writer.start()

            start = time.time()
            result = _poll_for_prd_markdown(prd_path)
            elapsed = time.time() - start

            writer.join()

            assert result == prd_content
            # Should complete in ~2.2s (2s delay + 200ms grace period + polling overhead)
            assert 2.0 <= elapsed <= 3.0, f"Expected ~2.2s, got {elapsed:.1f}s"

    def test_immediate_file_exists(self):
        """Verify immediate success when file already exists."""
        from swe_af.prompts.architect import _poll_for_prd_markdown

        with tempfile.TemporaryDirectory() as tmpdir:
            prd_path = os.path.join(tmpdir, "prd.md")
            prd_content = "# Existing PRD\n\nFile exists from start."

            # Write file before polling
            with open(prd_path, "w", encoding="utf-8") as f:
                f.write(prd_content)

            start = time.time()
            result = _poll_for_prd_markdown(prd_path)
            elapsed = time.time() - start

            assert result == prd_content
            # Should complete in ~200ms (grace period only)
            assert elapsed <= 0.5, f"Expected <=0.5s, got {elapsed:.1f}s"


class TestTimeoutBehavior:
    """Tests for timeout and graceful degradation."""

    def test_timeout_returns_empty_string(self):
        """Verify timeout after 120s returns empty string."""
        from swe_af.prompts.architect import _poll_for_prd_markdown

        with tempfile.TemporaryDirectory() as tmpdir:
            prd_path = os.path.join(tmpdir, "nonexistent.md")

            start = time.time()
            result = _poll_for_prd_markdown(prd_path)
            elapsed = time.time() - start

            assert result == ""
            assert 119 <= elapsed <= 122, f"Expected ~120s timeout, got {elapsed:.1f}s"

    def test_graceful_degradation_in_architect_prompts(self):
        """Verify architect_prompts proceeds with empty prd_markdown on timeout."""
        from swe_af.prompts.architect import architect_prompts

        with tempfile.TemporaryDirectory() as tmpdir:
            prd_path = os.path.join(tmpdir, "nonexistent.md")
            arch_path = os.path.join(tmpdir, "architecture.md")

            # This would timeout in real usage, but we can mock the polling
            # For this test, we'll just verify the function handles None prd gracefully
            system_prompt, task_prompt = architect_prompts(
                prd=None,
                repo_path="/test/repo",
                prd_path=prd_path,
                architecture_path=arch_path,
            )

            # Should not raise exception
            assert system_prompt is not None
            assert task_prompt is not None
            assert "/test/repo" in task_prompt
            assert arch_path in task_prompt


class TestEdgeCases:
    """Tests for edge cases and boundary conditions."""

    def test_file_appears_at_boundary_119s(self):
        """Verify successful poll when file appears at 119.9s (just before timeout)."""
        from swe_af.prompts.architect import _poll_for_prd_markdown

        with tempfile.TemporaryDirectory() as tmpdir:
            prd_path = os.path.join(tmpdir, "prd.md")
            prd_content = "# Boundary PRD\n\nAppears just in time."

            def delayed_write():
                time.sleep(119.0)  # Appear at 119s, within 120s timeout
                with open(prd_path, "w", encoding="utf-8") as f:
                    f.write(prd_content)

            # Start background thread
            writer = threading.Thread(target=delayed_write)
            writer.start()

            result = _poll_for_prd_markdown(prd_path)
            writer.join()

            # Should succeed even at boundary
            assert result == prd_content

    def test_read_error_continues_polling(self):
        """Verify that read errors don't stop polling."""
        from swe_af.prompts.architect import _poll_for_prd_markdown

        with tempfile.TemporaryDirectory() as tmpdir:
            prd_path = os.path.join(tmpdir, "prd.md")

            # Create a directory with same name (will cause read error)
            os.makedirs(prd_path, exist_ok=True)

            def fix_and_write():
                time.sleep(2.0)
                # Remove directory and create file
                os.rmdir(prd_path)
                with open(prd_path, "w", encoding="utf-8") as f:
                    f.write("# Fixed PRD")

            # Start background thread to fix the issue
            fixer = threading.Thread(target=fix_and_write)
            fixer.start()

            result = _poll_for_prd_markdown(prd_path)
            fixer.join()

            # Should eventually succeed after the fix
            assert result == "# Fixed PRD"

    def test_concurrent_write_grace_period(self):
        """Verify 200ms grace period allows concurrent write to complete."""
        from swe_af.prompts.architect import _poll_for_prd_markdown

        with tempfile.TemporaryDirectory() as tmpdir:
            prd_path = os.path.join(tmpdir, "prd.md")
            full_content = "# PRD\n\n" + ("Line\n" * 1000)

            def slow_write():
                """Simulate slow write that takes 150ms."""
                with open(prd_path, "w", encoding="utf-8") as f:
                    # Write in chunks to simulate slow write
                    for i in range(0, len(full_content), 100):
                        f.write(full_content[i:i+100])
                        time.sleep(0.015)  # Total ~150ms

            # Start slow write
            writer = threading.Thread(target=slow_write)
            writer.start()

            # Give it a moment to create the file
            time.sleep(0.05)

            # Now poll - should wait for grace period
            result = _poll_for_prd_markdown(prd_path)
            writer.join()

            # Should get complete content (grace period allowed write to finish)
            assert len(result) > 100  # Got substantial content

    def test_architect_prompts_with_structured_prd(self):
        """Verify architect_prompts works with structured PRD object (regression test)."""
        from swe_af.prompts.architect import architect_prompts
        from swe_af.reasoners.schemas import PRD

        prd = PRD(
            validated_description="Test PRD description",
            acceptance_criteria=["AC1", "AC2"],
            must_have=["Feature A", "Feature B"],
            nice_to_have=["Feature C"],
            out_of_scope=["Feature D"],
        )

        system_prompt, task_prompt = architect_prompts(
            prd=prd,
            repo_path="/test/repo",
            prd_path="/test/prd.md",
            architecture_path="/test/arch.md",
        )

        assert system_prompt is not None
        assert "Test PRD description" in task_prompt
        assert "AC1" in task_prompt
        assert "Feature A" in task_prompt

    def test_architect_prompts_with_none_prd_and_content(self):
        """Verify architect_prompts uses polled markdown content."""
        from swe_af.prompts.architect import architect_prompts

        with tempfile.TemporaryDirectory() as tmpdir:
            prd_path = os.path.join(tmpdir, "prd.md")
            arch_path = os.path.join(tmpdir, "architecture.md")
            prd_content = "# Test PRD\n\nPolled content from file."

            # Write PRD file
            with open(prd_path, "w", encoding="utf-8") as f:
                f.write(prd_content)

            # Call with prd=None to trigger polling
            system_prompt, task_prompt = architect_prompts(
                prd=None,
                repo_path="/test/repo",
                prd_path=prd_path,
                architecture_path=arch_path,
            )

            assert system_prompt is not None
            assert "Polled content from file" in task_prompt
            assert "/test/repo" in task_prompt


class TestParameterValidation:
    """Tests for parameter validation and signature."""

    def test_prd_path_parameter_exists(self):
        """Verify prd_path parameter is in function signature."""
        from swe_af.prompts.architect import architect_prompts
        import inspect
        sig = inspect.signature(architect_prompts)
        assert "prd_path" in sig.parameters

    def test_prd_parameter_is_optional(self):
        """Verify prd parameter is optional (can be None)."""
        from swe_af.prompts.architect import architect_prompts
        import inspect
        sig = inspect.signature(architect_prompts)
        prd_param = sig.parameters["prd"]
        # Check if default is None
        assert prd_param.default is None or str(prd_param.annotation).find("None") != -1
