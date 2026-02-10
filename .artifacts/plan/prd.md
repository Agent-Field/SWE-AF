# Product Requirements Document: Production Quality Repo Cleanup for af-swe

## Executive Summary

The af-swe repository is a functional autonomous SWE pipeline that requires cleanup to meet production quality standards. This PRD defines the technical debt remediation across 8 categories identified through comprehensive code audit: module organization, error handling, type safety, documentation, configuration threading, testing, logging, and general code quality.

**Scope**: Fix existing code quality issues. No new features.

## Validated Description

Clean up the af-swe autonomous SWE pipeline codebase to production quality by addressing:

1. **Module Organization**: Remove dead code (`discussion/prototype.py`, `execution/_replanner_compat.py`, `claude_ai/client.py` aliases), fix incomplete `__init__.py` exports
2. **Error Handling**: Replace bare `except Exception:` with specific exception types, add missing error context propagation, handle edge cases in DAG executor
3. **Type Safety**: Add missing type hints to execution reasoners (return Pydantic models not `dict`), fix incomplete Pydantic model field definitions, eliminate `Any` usage where possible
4. **Documentation**: Add docstrings to `_execute_single_issue()`, document `resume_build()` parameters, add module docstrings where missing
5. **Configuration**: Add `permission_mode` field to `ExecutionConfig`, ensure all config fields thread through pipeline consistently
6. **Testing**: Add unit tests for `dag_utils.py` (recompute_levels, find_downstream, apply_replan), `envelope.py` (unwrap_call_result), and Pydantic schema validation
7. **Logging**: Add note_fn calls to checkpoint operations, DAG state mutations, and replanner direct invocation path
8. **Requirements**: Audit `requirements.txt` for completeness and pinned versions

## Acceptance Criteria

Each criterion is verifiable by running a command or inspecting specific files.

### AC1: Module Organization
```bash
# Verify dead code removed
test ! -f execution/_replanner_compat.py
test ! -f discussion/prototype.py
test ! -f claude_ai/client.py
```

### AC2: Error Handling Quality
```bash
# Verify no bare except clauses in critical execution paths
! rg 'except Exception:\s*$' execution/dag_executor.py execution/coding_loop.py
# Verify specific exception types used
rg 'except (ValueError|RuntimeError|TimeoutError|OSError):' execution/dag_executor.py | wc -l | awk '$1 >= 5'
```

### AC3: Type Safety
```bash
# Verify execution reasoners return typed models, not dict
rg 'async def run_.*\(.*\) -> (IssueResult|ReplanDecision|MergeResult|IntegrationTestResult|VerificationResult|RetryAdvice|IssueAdvisorDecision):' reasoners/execution_agents.py | wc -l | awk '$1 >= 7'
# Verify PlannedIssue model has dynamic fields
rg 'debt_notes: list\[str\] = \[\]' schemas.py
rg 'worktree_path: str \| None = None' schemas.py
```

### AC4: Configuration Threading
```bash
# Verify permission_mode in ExecutionConfig
rg 'permission_mode: str \| None = None' execution/schemas.py
# Verify permission_mode passed to execution reasoners
rg 'permission_mode=.*permission_mode' reasoners/execution_agents.py | wc -l | awk '$1 >= 3'
```

### AC5: Testing Coverage
```bash
# Verify unit tests exist for core utilities
test -f tests/test_dag_utils.py
test -f tests/test_envelope.py
test -f tests/test_schemas.py
# Verify tests pass
python -m pytest tests/test_dag_utils.py -v
python -m pytest tests/test_envelope.py -v
python -m pytest tests/test_schemas.py -v
```

### AC6: Documentation Completeness
```bash
# Verify key functions have docstrings
rg '""".*Issue Advisor adaptation loop' execution/dag_executor.py
rg '""".*checkpoint file.*expected' main.py
# Verify module docstrings present
head -5 agent_ai/providers/__init__.py | rg '"""'
```

### AC7: Logging Observability
```bash
# Verify checkpoint operations log via note_fn
rg 'note_fn.*checkpoint.*save' execution/dag_executor.py
rg 'note_fn.*checkpoint.*load' execution/dag_executor.py
# Verify replanner direct path uses note_fn (not logging module)
! rg 'import logging' execution/_replanner_compat.py 2>/dev/null || echo "Module removed"
```

### AC8: Requirements Pinned
```bash
# Verify requirements.txt has pinned versions
rg '^[a-z-]+==\d+\.\d+' requirements.txt | wc -l | awk '$1 >= 2'
# Verify no unpinned dependencies (except editable SDK)
! rg '^[a-z-]+>=\d+\.\d+$' requirements.txt
```

### AC9: Import Cleanup
```bash
# Verify no unused imports in critical paths
python -m pyflakes execution/dag_executor.py execution/coding_loop.py reasoners/execution_agents.py | wc -l | awk '$1 == 0'
```

### AC10: Schema Validation
```bash
# Verify Pydantic models validate properly
python -c "from execution.schemas import DAGState, IssueResult, ReplanDecision; print('Schemas import successfully')"
python -c "from schemas import PlannedIssue, PlanResult; PlannedIssue(name='test', title='Test', description='Test issue', acceptance_criteria=[]); print('PlannedIssue validates')"
```

## Must Have

### Code Organization (Priority: High)
- Remove `execution/_replanner_compat.py` (unused fallback path superseded by `dag_executor.py:_invoke_replanner_via_call`)
- Remove `discussion/prototype.py` (experimental code not integrated into pipeline)
- Remove `claude_ai/client.py` (deprecated backward-compatibility aliases)
- Add `__all__` exports to `discussion/__init__.py` for `prompts` module
- Add module docstring to `agent_ai/providers/__init__.py`

### Error Handling (Priority: Critical)
- Replace bare `except Exception:` in `execution/dag_executor.py` lines 319, 525, 751 with specific exception types (OSError, ValueError, KeyError)
- Replace bare `except Exception:` in `agent_ai/providers/codex/client.py` lines 71, 82, 265 with JSONDecodeError, OSError
- Replace bare `except Exception:` in `agent_ai/providers/claude/client.py` lines 112, 380, 466 with specific types
- Add error context preservation in `execution/coding_loop.py` line 148 (capture traceback for advisor)
- Add validation for `issue_by_name[name]` existence in `execution/dag_executor.py:1058` before access
- Add try/except around `_save_checkpoint()` in `execution/dag_executor.py:343` with note_fn logging on failure

### Type Safety (Priority: High)
- Add return type annotations to all execution reasoners in `reasoners/execution_agents.py`:
  - `run_retry_advisor() -> RetryAdvice`
  - `run_issue_advisor() -> IssueAdvisorDecision`
  - `run_replanner() -> ReplanDecision`
  - `run_issue_writer() -> dict` (structured dict with `success: bool`)
  - `run_verifier() -> VerificationResult`
  - `run_merger() -> MergeResult`
  - `run_integration_tester() -> IntegrationTestResult`
- Add missing fields to `schemas.PlannedIssue`:
  - `debt_notes: list[str] = []`
  - `failure_notes: list[str] = []`
  - `worktree_path: str | None = None`
  - `branch_name: str | None = None`
  - `integration_branch: str | None = None`
  - `retry_context: str = ""`
  - `approach_changes: list[str] = []`
  - `previous_error: str = ""`
  - `retry_diagnosis: str = ""`
- Replace `dict[str, Any]` in `execution/schemas.py:299` (CodeReviewResult.debt_items) with typed `DebtItem` Pydantic model
- Add validator for `RetryAdvice.confidence` to enforce range [0.0, 1.0]

### Configuration Threading (Priority: High)
- Add `permission_mode: str | None = None` field to `ExecutionConfig` in `execution/schemas.py`
- Pass `permission_mode` from `BuildConfig` to `ExecutionConfig` in `main.py:execute()` function
- Thread `permission_mode` to all execution agent calls in `reasoners/execution_agents.py`
- Unify `agent_timeout_seconds` between `BuildConfig` (line 369) and `ExecutionConfig` (line 408) — make configurable at top level

### Testing (Priority: Critical)
- Create `tests/test_dag_utils.py` with unit tests for:
  - `recompute_levels()`: happy path, cycle detection, completed deps handling
  - `find_downstream()`: single level, transitive deps, no deps
  - `apply_replan()`: MODIFY_DAG action, REDUCE_SCOPE, CONTINUE, ABORT, cycle rejection
- Create `tests/test_envelope.py` with unit tests for:
  - `unwrap_call_result()`: success case, failed status, error status, cancelled status, timeout status, non-dict input
- Create `tests/test_schemas.py` with unit tests for:
  - Pydantic model validation: `PlannedIssue`, `PlanResult`, `DAGState`, `IssueResult`, `ReplanDecision`
  - Enum transitions: `IssueOutcome`, `AdvisorAction`, `ReplanAction`
  - Field defaults and required fields

### Documentation (Priority: Medium)
- Add comprehensive docstring to `execution/dag_executor.py:_execute_single_issue()` (currently missing at line 428)
- Document `resume_build()` parameters in `main.py:505` (what checkpoint structure is expected, what happens if missing)
- Add module docstring to `agent_ai/providers/__init__.py`
- Expand `discussion/__init__.py` docstring to describe module purpose

### Logging/Observability (Priority: Medium)
- Add `note_fn` logging to `_save_checkpoint()` in `execution/dag_executor.py:343` on OSError
- Add `note_fn` logging to `_load_checkpoint()` in `execution/dag_executor.py:355` on success/failure
- Add `note_fn` logging to `_init_dag_state()` in `execution/dag_executor.py:364` when converting Pydantic models to dicts
- Add `note_fn` logging in `apply_replan()` in `execution/dag_utils.py:88` for what was added/removed/modified
- Replace `logging` module usage in `execution/_replanner_compat.py` with `note_fn` parameter (or remove file entirely)

### Requirements (Priority: Medium)
- Pin all versions in `requirements.txt` (currently `pydantic>=2.0` is unpinned)
- Audit transitive dependencies listed in comments (aiohttp, fal-client, etc.) — verify they're actually provided by agentfield SDK
- Add `mypy` and `ruff` to dev requirements for type checking and linting

## Nice to Have

### Enhanced Type Safety
- Replace remaining `Any` usage in `agent_ai/providers/codex/adapter.py:49` with structured types
- Add generic type parameters to `AgentResponse[T]` usage throughout codebase

### Additional Testing
- Add integration test for full DAG execution cycle (plan → execute → verify)
- Add property-based tests for DAG cycle detection using `hypothesis`
- Add tests for checkpoint save/load round-trip

### Documentation Enhancements
- Generate API documentation using `pdoc` or `sphinx`
- Add architecture diagram showing data flow through pipeline
- Document error handling strategy (when to raise vs return fallback)

### Code Quality Tools
- Add `ruff` configuration for auto-formatting
- Add `mypy` strict mode configuration
- Add pre-commit hooks for type checking and linting

### Performance
- Add metrics collection for issue execution time, advisor invocations, replan frequency
- Add structured logging output (JSON) for easier parsing

## Out of Scope

### New Features
- Adding new agent roles or pipeline phases
- Implementing new replanning strategies beyond existing CONTINUE/MODIFY_DAG/REDUCE_SCOPE/ABORT
- Adding new Issue Advisor actions beyond existing 5 (RETRY_MODIFIED, RETRY_APPROACH, SPLIT, ACCEPT_WITH_DEBT, ESCALATE_TO_REPLAN)
- Implementing parallel planning (currently sequential: PM → Architect → Tech Lead → Sprint Planner)

### Architecture Changes
- Replacing AgentField SDK with different orchestration framework
- Switching from Pydantic to different validation library
- Changing DAG execution model (currently level-by-level barrier synchronization)
- Implementing distributed execution across multiple machines

### UI/UX
- Building web dashboard for pipeline visualization
- Adding CLI commands beyond what AgentField SDK provides
- Creating interactive mode for user input during execution

### External Integrations
- Adding support for additional AI providers beyond Claude and Codex
- Integrating with issue tracking systems (Jira, Linear, etc.)
- Adding support for non-git version control systems

### Performance Optimization
- Implementing caching for LLM responses
- Optimizing DAG recomputation algorithms beyond Kahn's algorithm
- Parallelizing git operations within a single level

## Assumptions

1. **AgentField SDK Stability**: The agentfield SDK at `../../code/agentfield/sdk/python` is stable and provides all required transitive dependencies (aiohttp, fal-client, fastapi, litellm, psutil, PyYAML, requests, uvicorn, websockets).

2. **Python Version**: The codebase targets Python 3.13 (based on `.venv/lib/python3.13/` path). Type hints use modern syntax (`|` for Union, `list[T]` for List[T]).

3. **Testing Framework**: pytest is the standard testing framework; new tests should use pytest conventions (fixtures, parametrize, etc.).

4. **Backward Compatibility**: Removing `claude_ai/client.py` aliases will NOT break existing user code (assumption: internal-only usage, not public API).

5. **Git Workflow**: The git worktree-based isolation strategy is correct and should be preserved. Issues are that `discussion/prototype.py` and `execution/_replanner_compat.py` are genuinely unused (verified via grep for imports/calls).

6. **Error Handling Strategy**:
   - Planning agents (reasoners/pipeline.py) raise exceptions on failure → caller handles with try/except
   - Execution agents (reasoners/execution_agents.py) return fallback objects with `success: bool` flag
   - This is the intended design (not a bug to fix, just document).

7. **Pydantic Models**: All schema classes in `schemas.py` and `execution/schemas.py` are the source of truth for data contracts. Dynamic field addition (e.g., `debt_notes`) should be formalized in the model definitions.

8. **note_fn Availability**: All execution contexts have access to `note_fn` for observability. The `logging` module should NOT be used directly in pipeline code.

9. **Checkpoint Format**: The checkpoint JSON schema is defined by `DAGState.model_dump()`. Resume logic expects this exact structure; schema changes require migration strategy.

10. **Issue Sequencing**: Sequence numbers (e.g., `01-issue-name`) are assigned during planning and used for branch naming. This convention is stable and should not change.

## Risks

### Risk 1: Breaking Changes from Dead Code Removal
- **Likelihood**: Low
- **Impact**: Medium
- **Mitigation**: Grep entire codebase for imports of `_replanner_compat`, `prototype`, `claude_ai.client` before removal. Run full test suite after removal.

### Risk 2: Incomplete Test Coverage
- **Likelihood**: Medium
- **Impact**: High
- **Mitigation**: Focus unit tests on pure functions (dag_utils, envelope) first. Add integration tests as stretch goal. Use code coverage tool to identify gaps.

### Risk 3: Type Safety Breaking Existing Code
- **Likelihood**: Low
- **Impact**: Medium
- **Mitigation**: Adding type hints and Pydantic fields should not break runtime behavior (Pydantic Extra.allow for unknown fields). Test with actual execution run.

### Risk 4: Configuration Threading Complexity
- **Likelihood**: Medium
- **Impact**: Medium
- **Mitigation**: Add `permission_mode` to `ExecutionConfig` with default `None` to match `BuildConfig`. Thread through carefully, test with both values set and unset.

### Risk 5: Logging Changes Affecting Observability
- **Likelihood**: Low
- **Impact**: High (production debugging relies on logs)
- **Mitigation**: Add logging, don't remove existing logging. Test that all critical paths still emit observability events. Use structured tags consistently.

### Risk 6: Requirements Pinning Breaking SDK Compatibility
- **Likelihood**: Low
- **Impact**: Medium
- **Mitigation**: The agentfield SDK is editable install (`-e ../../code/agentfield/sdk/python`), so its dependencies take precedence. Pin only af-swe-specific deps (pydantic, claude-agent-sdk).

## Success Metrics

All acceptance criteria must pass (10/10 binary checks). Successful execution means:

1. `test ! -f execution/_replanner_compat.py` exits 0
2. `! rg 'except Exception:\s*$' execution/dag_executor.py` exits 0
3. Execution reasoners return typed Pydantic models (verifiable via grep pattern match count ≥ 7)
4. `permission_mode` field exists in `ExecutionConfig` schema
5. `tests/test_dag_utils.py`, `test_envelope.py`, `test_schemas.py` exist and pass
6. Key docstrings present (verifiable via grep for specific strings)
7. Checkpoint operations log via `note_fn` (verifiable via grep)
8. `requirements.txt` has ≥ 2 pinned versions (`==` syntax)
9. Pyflakes reports 0 unused imports in critical execution files
10. Pydantic schemas import and validate without errors

**Definition of Done**: An autonomous coding agent can execute each acceptance criterion command and all return exit code 0 (success).
