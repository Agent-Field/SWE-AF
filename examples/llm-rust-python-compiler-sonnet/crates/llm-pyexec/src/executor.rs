//! Execute Python source code strings via the RustPython VM.
//!
//! This module is the top-level orchestrator for a single Python execution:
//! 1. Applies [`maybe_wrap_last_expr`] to the source so bare expressions yield a
//!    return value via the `__result__` convention.
//! 2. Computes a SHA-256 cache key and warms the [`BytecodeCache`] LRU entry.
//! 3. Creates a fresh [`OutputBuffer`] sized to `settings.max_output_bytes`.
//! 4. Builds the module allowlist with [`build_allowed_set`].
//! 5. Attempts to dispatch work to the [`InterpreterPool`] (warm path).
//!    - On success: waits on per-call response channel with execution timeout.
//!    - On pool exhaustion: falls back to [`run_with_timeout`] with a fresh interpreter.
//! 6. Maps the result into an [`ExecutionResult`], filling in `error = Some(Timeout { .. })`
//!    on timeout, and inserts into the bytecode cache on non-SyntaxError results.
//!
//! ## Thread safety
//!
//! Each call to [`execute`] is fully independent: it creates new instances of
//! every resource (OutputBuffer, HashSet, per-call channel).  The pool and cache
//! singletons are internally synchronized. The function is safe to call from
//! many threads simultaneously (AC-14).
//!
//! ## Zero unsafe blocks (AC-18)
//!
//! This file contains no `unsafe` code.

use std::sync::Arc;
use std::time::{Duration, Instant};

use crate::cache::{BytecodeCache, cache_key};
use crate::modules::build_allowed_set;
use crate::output::OutputBuffer;
use crate::pool::{InterpreterPool, WorkItem};
use crate::timeout::run_with_timeout;
use crate::types::{ExecutionError, ExecutionResult, ExecutionSettings};
use crate::vm::{build_interpreter, run_code, VmRunResult};

/// Timeout used when waiting for an available pool slot.
/// 30 seconds — gives all pool slots time to finish current work before falling back.
const POOL_CHECKOUT_TIMEOUT: Duration = Duration::from_secs(30);

// ── Public API ────────────────────────────────────────────────────────────────

/// Execute a Python source string and return a structured result.
///
/// # Parameters
/// - `code`: Python source text.  The last statement, if it is a bare
///   expression (not a keyword statement or a function call), is automatically
///   wrapped as `__result__ = <expr>` so callers can retrieve a return value.
/// - `settings`: timeout, output limit, and module allowlist configuration.
///
/// # Returns
/// An [`ExecutionResult`] with stdout, stderr, optional return value, optional
/// error, and elapsed wall-clock time.
///
/// # Thread safety
/// Each call is completely independent.  No shared mutable state exists between
/// concurrent calls.
pub fn execute(code: &str, settings: ExecutionSettings) -> ExecutionResult {
    let start = Instant::now();

    let wrapped = maybe_wrap_last_expr(code);
    let timeout_ns = settings.timeout_ns;
    let max_output_bytes = settings.max_output_bytes;

    // Compute SHA-256 cache key and warm the LRU entry (AC: get() before execution).
    let key = cache_key(&wrapped);
    let _ = BytecodeCache::global().get(&key);

    // Build the allowlist set once, before spawning the VM thread.
    let allowed_set = Arc::new(build_allowed_set(&settings));

    // Create the output buffer that will be shared between executor and VM.
    let output = OutputBuffer::new(max_output_bytes);

    // Per-call one-shot response channel (must be created before building WorkItem).
    let (response_tx, response_rx) = std::sync::mpsc::sync_channel::<VmRunResult>(1);

    // Build WorkItem with all Send fields.
    let work = WorkItem {
        wrapped_source: wrapped.clone(),
        output: output.clone(),
        allowed_set: Arc::clone(&allowed_set),
        response: response_tx,
    };

    // Try to dispatch to the pool (warm path).
    let vm_result: Option<VmRunResult> =
        if InterpreterPool::global().dispatch_work(work, POOL_CHECKOUT_TIMEOUT) {
            // Pool accepted the work item. Wait for the result with execution timeout.
            let execution_timeout = Duration::from_nanos(timeout_ns);
            match response_rx.recv_timeout(execution_timeout) {
                Ok(result) => Some(result),
                Err(_) => {
                    // Timeout (or channel disconnect): treat as a timeout.
                    None
                }
            }
        } else {
            // Pool exhausted — fall back to a fresh interpreter on a new thread.
            // Clone output for the VM thread (executor retains its own handle).
            let output_for_vm = output.clone();
            let allowed_set_inner = (*allowed_set).clone();
            let wrapped_for_vm = wrapped.clone();
            run_with_timeout(
                move || {
                    let interp = build_interpreter(allowed_set_inner, output_for_vm.clone());
                    run_code(&interp, &wrapped_for_vm, output_for_vm)
                },
                timeout_ns,
            )
        };

    let duration_ns = start.elapsed().as_nanos() as u64;

    match vm_result {
        Some(result) => {
            // Cache the wrapped source on successful (non-SyntaxError) results.
            let is_syntax_error = matches!(result.error, Some(ExecutionError::SyntaxError { .. }));
            if !is_syntax_error {
                BytecodeCache::global().insert(key, wrapped);
            }

            // Check if the output buffer limit was exceeded.
            let limit_exceeded = output.is_limit_exceeded();
            let error = if limit_exceeded {
                // An output limit was hit; return the canonical error variant
                // regardless of what runtime error the VM produced internally.
                Some(ExecutionError::OutputLimitExceeded {
                    limit_bytes: max_output_bytes,
                })
            } else {
                result.error
            };
            ExecutionResult {
                stdout: result.stdout,
                stderr: result.stderr,
                return_value: result.return_value,
                error,
                duration_ns,
            }
        }
        None => {
            // Timeout: read whatever partial output the VM produced.
            let (stdout, stderr) = output.into_strings();
            ExecutionResult {
                stdout,
                stderr,
                return_value: None,
                error: Some(ExecutionError::Timeout { limit_ns: timeout_ns }),
                duration_ns,
            }
        }
    }
}

// ── Source-level expression wrapper ──────────────────────────────────────────

/// Heuristically wrap the last line of `code` as `__result__ = <last_line>`
/// if the last line looks like a bare value-producing expression rather than a
/// statement or a side-effecting call.
///
/// # Rules (in order of evaluation)
///
/// The last non-empty line is **left unchanged** when:
/// - The code is empty or all whitespace/blank lines.
/// - The last non-empty line is indented (inside a block).
/// - The last non-empty line starts with any statement keyword from the
///   architecture §4.7 list:
///   `def`, `class`, `if`, `elif`, `else`, `for`, `while`, `try`, `except`,
///   `finally`, `with`, `import`, `from`, `return`, `pass`, `break`,
///   `continue`, `raise`, `assert`, `del`, `global`, `nonlocal`, `yield`,
///   `async`, `await`, `match`, `case`, `@`.
/// - The last non-empty line contains a bare assignment `=` (not `==`, `!=`,
///   `<=`, `>=`, or compound assignments like `+=`, `-=`, etc.).
/// - The last non-empty line looks like a function/method call (the trimmed
///   line ends with `)` at balanced nesting depth).
///
/// Otherwise the line is wrapped as `__result__ = <line>`.
///
/// # Examples
/// ```
/// use llm_pyexec::executor::maybe_wrap_last_expr;
/// assert_eq!(maybe_wrap_last_expr("1 + 1"), "__result__ = 1 + 1");
/// assert_eq!(maybe_wrap_last_expr("x = 1\nprint(x)"), "x = 1\nprint(x)");
/// assert_eq!(maybe_wrap_last_expr(""), "");
/// ```
pub fn maybe_wrap_last_expr(code: &str) -> String {
    // Statement-keyword prefixes that indicate the last line is NOT a bare expr.
    // Architecture §4.7 list.
    const STATEMENT_PREFIXES: &[&str] = &[
        "def ",
        "class ",
        "if ",
        "elif ",
        "else:",
        "else :",
        "for ",
        "while ",
        "try:",
        "try :",
        "except",
        "finally:",
        "finally :",
        "with ",
        "import ",
        "from ",
        "return ",
        "return\n",
        "return\r",
        "pass",
        "break",
        "continue",
        "raise ",
        "raise\n",
        "raise\r",
        "assert ",
        "del ",
        "global ",
        "nonlocal ",
        "yield ",
        "yield\n",
        "yield\r",
        "async ",
        "await ",
        "match ",
        "case ",
        "@",
        "#",
    ];

    // Bare keywords that stand alone on a line (no trailing space needed).
    const BARE_KEYWORDS: &[&str] = &[
        "pass", "break", "continue", "return", "yield", "raise", "else:", "finally:", "try:",
    ];

    // Split on newlines preserving structure.
    let lines: Vec<&str> = code.split('\n').collect();

    // Find index of last non-empty (non-whitespace) line.
    let last_idx = match lines
        .iter()
        .enumerate()
        .rev()
        .find(|(_, l)| !l.trim().is_empty())
        .map(|(i, _)| i)
    {
        Some(i) => i,
        None => return code.to_string(), // empty or all whitespace
    };

    let original_last_line = lines[last_idx];
    let last_line = original_last_line.trim();

    // If indented, it's inside a block — don't wrap.
    let leading = original_last_line.len() - original_last_line.trim_start().len();
    if leading > 0 {
        return code.to_string();
    }

    // Check bare keyword exact matches.
    for kw in BARE_KEYWORDS {
        if last_line == *kw {
            return code.to_string();
        }
    }

    // Check statement keyword prefixes.
    for prefix in STATEMENT_PREFIXES {
        if last_line.starts_with(prefix) {
            return code.to_string();
        }
    }

    // Check assignment: line contains bare '=' (not '==', '!=', '<=', '>=',
    // compound '+=', '-=', etc.).
    if looks_like_assignment(last_line) {
        return code.to_string();
    }

    // Check if last line is a call expression (ends with ')' at balanced depth).
    // Function calls are statement-like and typically produce None; don't wrap.
    if is_call_statement(last_line) {
        return code.to_string();
    }

    // Wrap: replace the last non-empty line.
    let formatted = format!("__result__ = {last_line}");
    let mut new_lines: Vec<String> = lines.iter().map(|l| l.to_string()).collect();
    new_lines[last_idx] = formatted;
    new_lines.join("\n")
}

/// Returns `true` if `line` looks like an assignment statement.
///
/// Detects:
/// - Simple assignment: `x = expr` (bare `=` not preceded by `!<>=+-*/&|^~`)
/// - Augmented assignment: `x += expr`, `x -= expr`, `x *= expr`, etc.
///   (a `=` preceded by `+`, `-`, `*`, `/`, `%`, `&`, `|`, `^`, `~` counts as
///   augmented assignment, which is still an assignment statement)
///
/// Does NOT match:
/// - `==`, `!=`, `<=`, `>=` comparisons
fn looks_like_assignment(line: &str) -> bool {
    let chars: Vec<char> = line.chars().collect();
    let n = chars.len();

    for i in 0..n {
        if chars[i] != '=' {
            continue;
        }
        // '==' — skip (comparison, not assignment)
        if i + 1 < n && chars[i + 1] == '=' {
            continue;
        }
        // Check character before '='.
        if i > 0 {
            let prev = chars[i - 1];
            match prev {
                // '!', '<', '>' or '=' before '=' → comparison operator, skip.
                '!' | '<' | '>' | '=' => continue,
                // '+', '-', '*', '/', '%', '&', '|', '^', '~' before '=' → augmented assignment.
                // Augmented assignment IS a statement — return true.
                '+' | '-' | '*' | '/' | '%' | '&' | '|' | '^' | '~' => return true,
                // Anything else before '=' → simple assignment.
                _ => return true,
            }
        } else {
            // '=' at position 0 with no preceding char — bare '=' (unusual but treat as assignment).
            return true;
        }
    }
    false
}

/// Returns `true` if `line` is a top-level function/method call expression.
///
/// Heuristic: the trimmed line ends with `)` and the parentheses are balanced.
/// This catches `print(x)`, `foo.bar(baz)`, `f()`, etc.
/// It does NOT catch expressions like `(1 + 2)` — those should be wrapped.
///
/// The rule: if the line ends with `)` at balanced depth AND there is a `(`
/// somewhere in the line, it's treated as a call statement.
fn is_call_statement(line: &str) -> bool {
    if !line.ends_with(')') {
        return false;
    }

    // Check parentheses are balanced.
    let mut depth: i32 = 0;
    for ch in line.chars() {
        match ch {
            '(' => depth += 1,
            ')' => depth -= 1,
            _ => {}
        }
    }
    if depth != 0 {
        return false;
    }

    // The line ends with ')' and parens are balanced.
    // Distinguish call expressions from grouping expressions like `(1 + 2)`.
    // A call has an identifier (or attribute access) immediately before `(`.
    // A bare `(expr)` grouping starts with `(`.
    // Heuristic: if the first non-whitespace character is `(`, it's grouping.
    if line.starts_with('(') {
        return false;
    }

    true
}

// ── Tests ─────────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;
    use crate::types::ExecutionSettings;
    use std::time::Instant;

    // ── maybe_wrap_last_expr unit tests ───────────────────────────────────────

    /// (1) Bare expression last line gets wrapped.
    #[test]
    fn test_wrap_bare_expression() {
        assert_eq!(maybe_wrap_last_expr("1 + 1"), "__result__ = 1 + 1");
    }

    /// Assignment last line is unchanged.
    #[test]
    fn test_no_wrap_assignment() {
        assert_eq!(maybe_wrap_last_expr("x = 1"), "x = 1");
    }

    /// Multiline code where last line is expression gets wrapped.
    #[test]
    fn test_wrap_multiline_last_expr() {
        let code = "x = 5\nx * 2";
        let result = maybe_wrap_last_expr(code);
        assert_eq!(result, "x = 5\n__result__ = x * 2");
    }

    /// Multiline code where last line is a call (e.g. print(x)) — unchanged.
    /// Per AC: "maybe_wrap_last_expr leaves 'x = 1\nprint(x)' unchanged
    ///  (last line is a call, not a bare expression with assignment context
    ///   — behavior per heuristic)"
    #[test]
    fn test_no_wrap_call_print() {
        let code = "x = 1\nprint(x)";
        let result = maybe_wrap_last_expr(code);
        assert_eq!(result, "x = 1\nprint(x)");
    }

    /// Empty string is unchanged.
    #[test]
    fn test_no_wrap_empty() {
        assert_eq!(maybe_wrap_last_expr(""), "");
    }

    /// All-whitespace string is unchanged.
    #[test]
    fn test_no_wrap_whitespace_only() {
        assert_eq!(maybe_wrap_last_expr("   \n   \n"), "   \n   \n");
    }

    // ── Statement keyword tests (architecture §4.7) ───────────────────────────

    /// def as last line — unchanged.
    #[test]
    fn test_no_wrap_def() {
        assert_eq!(maybe_wrap_last_expr("def f(): pass"), "def f(): pass");
    }

    /// class as last line — unchanged.
    #[test]
    fn test_no_wrap_class() {
        assert_eq!(maybe_wrap_last_expr("class Foo: pass"), "class Foo: pass");
    }

    /// if as last line — unchanged.
    #[test]
    fn test_no_wrap_if() {
        assert_eq!(maybe_wrap_last_expr("if True: pass"), "if True: pass");
    }

    /// for as last line — unchanged.
    #[test]
    fn test_no_wrap_for() {
        assert_eq!(maybe_wrap_last_expr("for x in []: pass"), "for x in []: pass");
    }

    /// while as last line — unchanged.
    #[test]
    fn test_no_wrap_while() {
        assert_eq!(maybe_wrap_last_expr("while False: pass"), "while False: pass");
    }

    /// try block last line (indented pass) — unchanged.
    #[test]
    fn test_no_wrap_try() {
        let code = "try:\n    pass\nexcept:\n    pass";
        assert_eq!(maybe_wrap_last_expr(code), code);
    }

    /// with block — unchanged.
    #[test]
    fn test_no_wrap_with() {
        let code = "with open('f') as f:\n    pass";
        assert_eq!(maybe_wrap_last_expr(code), code);
    }

    /// import as last line — unchanged.
    #[test]
    fn test_no_wrap_import() {
        assert_eq!(maybe_wrap_last_expr("import math"), "import math");
    }

    /// from ... import as last line — unchanged.
    #[test]
    fn test_no_wrap_from() {
        assert_eq!(maybe_wrap_last_expr("from math import sqrt"), "from math import sqrt");
    }

    /// return as last line — unchanged.
    #[test]
    fn test_no_wrap_return() {
        assert_eq!(maybe_wrap_last_expr("return x"), "return x");
    }

    /// pass as last line — unchanged.
    #[test]
    fn test_no_wrap_pass() {
        assert_eq!(maybe_wrap_last_expr("pass"), "pass");
    }

    /// break as last line — unchanged.
    #[test]
    fn test_no_wrap_break() {
        assert_eq!(maybe_wrap_last_expr("break"), "break");
    }

    /// continue as last line — unchanged.
    #[test]
    fn test_no_wrap_continue() {
        assert_eq!(maybe_wrap_last_expr("continue"), "continue");
    }

    /// raise as last line — unchanged.
    #[test]
    fn test_no_wrap_raise() {
        assert_eq!(
            maybe_wrap_last_expr("raise ValueError('x')"),
            "raise ValueError('x')"
        );
    }

    /// assert as last line — unchanged.
    #[test]
    fn test_no_wrap_assert() {
        assert_eq!(maybe_wrap_last_expr("assert x == 1"), "assert x == 1");
    }

    /// del as last line — unchanged.
    #[test]
    fn test_no_wrap_del() {
        assert_eq!(maybe_wrap_last_expr("del x"), "del x");
    }

    /// global as last line — unchanged.
    #[test]
    fn test_no_wrap_global() {
        assert_eq!(maybe_wrap_last_expr("global x"), "global x");
    }

    /// nonlocal as last line — unchanged.
    #[test]
    fn test_no_wrap_nonlocal() {
        assert_eq!(maybe_wrap_last_expr("nonlocal x"), "nonlocal x");
    }

    /// yield as last line — unchanged.
    #[test]
    fn test_no_wrap_yield() {
        assert_eq!(maybe_wrap_last_expr("yield x"), "yield x");
    }

    /// Augmented assignment (+= etc.) is unchanged.
    #[test]
    fn test_no_wrap_augmented_assignment() {
        assert_eq!(maybe_wrap_last_expr("x += 1"), "x += 1");
    }

    /// Comparison expression (with ==) is wrapped (it's a bare expression).
    #[test]
    fn test_wrap_comparison_expr() {
        assert_eq!(maybe_wrap_last_expr("x == 1"), "__result__ = x == 1");
    }

    /// String literal is wrapped.
    #[test]
    fn test_wrap_string_literal() {
        assert_eq!(maybe_wrap_last_expr("\"hello\""), "__result__ = \"hello\"");
    }

    /// Variable reference is wrapped.
    #[test]
    fn test_wrap_variable_ref() {
        let code = "x = 42\nx";
        assert_eq!(maybe_wrap_last_expr(code), "x = 42\n__result__ = x");
    }

    // ── execute() functional tests ────────────────────────────────────────────

    /// AC-11: execute('print("hello world")', Default::default()).stdout == 'hello world\n'
    #[test]
    #[ignore = "slow: VM init per test"]
    fn test_execute_ac11_hello_world() {
        let settings = ExecutionSettings::default();
        let result = execute("print(\"hello world\")", settings);
        assert_eq!(result.stdout, "hello world\n");
        assert!(result.error.is_none(), "unexpected error: {:?}", result.error);
    }

    /// AC-08: Timeout returns Timeout error with duration_ns <= 500_000_000.
    #[test]
    #[ignore = "slow: VM init per test"]
    fn test_execute_timeout() {
        let settings = ExecutionSettings {
            timeout_ns: 200_000_000, // 200ms
            ..ExecutionSettings::default()
        };
        let start = Instant::now();
        let result = execute("while True: pass", settings);
        let elapsed_ms = start.elapsed().as_millis();

        match result.error {
            Some(ExecutionError::Timeout { limit_ns }) => {
                assert_eq!(limit_ns, 200_000_000, "limit_ns should match timeout setting");
            }
            other => panic!("Expected Timeout error, got: {:?}", other),
        }
        assert!(
            elapsed_ms < 1000,
            "Expected return within 1000ms, took {}ms",
            elapsed_ms
        );
        assert!(
            result.duration_ns <= 500_000_000,
            "duration_ns {} exceeds 500ms",
            result.duration_ns
        );
    }

    /// AC-12: Output limit exceeded returns OutputLimitExceeded.
    #[test]
    #[ignore = "slow: VM init per test"]
    fn test_execute_output_limit_exceeded() {
        let settings = ExecutionSettings {
            max_output_bytes: 100,
            ..ExecutionSettings::default()
        };
        let result = execute("print(\"x\" * 10000)", settings);
        match result.error {
            Some(ExecutionError::OutputLimitExceeded { limit_bytes }) => {
                assert_eq!(limit_bytes, 100);
            }
            other => panic!("Expected OutputLimitExceeded, got: {:?}", other),
        }
    }

    /// AC-15: duration_ns > 0 for any input.
    #[test]
    #[ignore = "slow: VM init per test"]
    fn test_execute_duration_nonzero() {
        let result = execute("x = 1", ExecutionSettings::default());
        assert!(result.duration_ns > 0, "duration_ns should be > 0");
    }

    /// AC-14: 20 concurrent threads produce no panics or data races.
    #[test]
    #[ignore = "slow: VM init per test"]
    fn test_execute_concurrent_20_threads() {
        use std::sync::Arc;
        let barrier = Arc::new(std::sync::Barrier::new(20));
        let handles: Vec<_> = (0..20)
            .map(|_| {
                let barrier = Arc::clone(&barrier);
                std::thread::spawn(move || {
                    barrier.wait();
                    let result = execute("x = 1", ExecutionSettings::default());
                    assert!(result.error.is_none(), "unexpected error: {:?}", result.error);
                    result.duration_ns
                })
            })
            .collect();

        for handle in handles {
            let dur = handle.join().expect("Thread panicked");
            assert!(dur > 0, "duration_ns should be > 0");
        }
    }
}
