//! Smoke tests for the llm-pyexec library.
//!
//! These tests verify the four most important correctness invariants:
//! 1. stdout capture works correctly
//! 2. syntax errors are detected and returned as `ExecutionError::SyntaxError`
//! 3. disallowed module imports return `ExecutionError::ModuleNotAllowed`
//! 4. execution duration is measured (nonzero)
//!
//! Run with: `cargo test -p llm-pyexec --test smoke_test`

use llm_pyexec::{execute, ExecutionError, ExecutionSettings};

/// Verify that `print("hi")` produces `"hi\n"` on stdout.
#[test]
fn test_execute_hello_world() {
    let result = execute(r#"print("hi")"#, ExecutionSettings::default());
    assert_eq!(
        result.stdout, "hi\n",
        "expected stdout to be 'hi\\n', got {:?}",
        result.stdout
    );
    assert!(result.error.is_none(), "expected no error, got {:?}", result.error);
}

/// Verify that a syntax error in the source returns `ExecutionError::SyntaxError`.
#[test]
fn test_execute_syntax_error() {
    let result = execute("def f(:", ExecutionSettings::default());
    assert!(
        matches!(result.error, Some(ExecutionError::SyntaxError { .. })),
        "expected SyntaxError, got {:?}",
        result.error
    );
}

/// Verify that importing a disallowed module returns `ExecutionError::ModuleNotAllowed`.
#[test]
fn test_execute_module_denied() {
    let result = execute("import socket", ExecutionSettings::default());
    assert!(
        matches!(result.error, Some(ExecutionError::ModuleNotAllowed { .. })),
        "expected ModuleNotAllowed, got {:?}",
        result.error
    );
}

/// Verify that the measured execution duration is nonzero.
#[test]
fn test_execute_duration_nonzero() {
    let result = execute("x = 1", ExecutionSettings::default());
    assert!(
        result.duration_ns > 0,
        "expected duration_ns > 0, got {}",
        result.duration_ns
    );
}
