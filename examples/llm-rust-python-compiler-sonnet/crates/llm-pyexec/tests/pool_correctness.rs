// crates/llm-pyexec/tests/pool_correctness.rs
// Tests: AC-04, AC-15

use llm_pyexec::pool::InterpreterPool;
use llm_pyexec::{execute, ExecutionError, ExecutionSettings};

/// AC-15: pool.idle_count() == expected pool size after init.
/// AC-04 (partial): checkout/checkin restores idle count.
///
/// When run with PYEXEC_POOL_SIZE=2, expected == 2.
/// When run without env var, expected == 4 (default).
#[test]
fn test_pool_checkout_checkin_single() {
    let expected_size: usize = std::env::var("PYEXEC_POOL_SIZE")
        .ok()
        .and_then(|s| s.parse().ok())
        .unwrap_or(4);

    let pool = InterpreterPool::global();

    // Wait for all slots to be idle (other tests may be using the pool concurrently).
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(30);
    while pool.idle_count() < expected_size && std::time::Instant::now() < deadline {
        std::thread::sleep(std::time::Duration::from_millis(10));
    }

    // After init (and any concurrent test activity has settled), all slots are idle.
    assert_eq!(
        pool.idle_count(), expected_size,
        "After init, idle_count should equal pool size ({})", expected_size
    );

    // Execute one call — checks out a slot and returns it.
    let result = execute("1 + 1", ExecutionSettings::default());
    assert!(result.error.is_none(), "Unexpected error: {:?}", result.error);
    assert_eq!(result.return_value, Some("2".to_string()),
        "Expected return_value '2', got {:?}", result.return_value);

    // Allow brief propagation time for checkin.
    std::thread::sleep(std::time::Duration::from_millis(50));

    // After checkin, idle count restored.
    assert_eq!(
        pool.idle_count(), expected_size,
        "After checkin, idle_count should be restored to {}", expected_size
    );
}

/// AC-04: 16 threads × 10 executions = 160 total; zero errors.
#[test]
fn test_pool_concurrent_16_threads() {
    use std::sync::{Arc, Barrier};

    let barrier = Arc::new(Barrier::new(16));
    let handles: Vec<_> = (0..16).map(|_i| {
        let barrier = Arc::clone(&barrier);
        std::thread::spawn(move || {
            barrier.wait(); // Start all 16 threads simultaneously.
            let mut errors = 0usize;
            for _ in 0..10 {
                let result = execute(
                    "result = sum(i * i for i in range(100))",
                    ExecutionSettings::default(),
                );
                if result.error.is_some() {
                    errors += 1;
                }
            }
            errors
        })
    }).collect();

    let total_errors: usize = handles.into_iter()
        .map(|h| h.join().expect("Thread panicked"))
        .sum();
    assert_eq!(total_errors, 0, "{total_errors} out of 160 executions had errors");
}

/// AC-04: State isolation — variable from call 1 MUST raise NameError in call 2.
#[test]
fn test_pool_state_isolation() {
    let settings = ExecutionSettings::default();

    // Call 1: assign x = 42.
    let r1 = execute("x = 42", settings.clone());
    assert!(r1.error.is_none(), "Call 1 failed: {:?}", r1.error);

    // Call 2: reference x — must raise NameError (name 'x' is not defined).
    let r2 = execute("x", settings.clone());
    match r2.error {
        Some(ExecutionError::RuntimeError { ref message, .. }) => {
            assert!(
                message.contains("name") && message.contains("'x'"),
                "Expected NameError about 'x', got: {message}"
            );
        }
        other => panic!(
            "Expected RuntimeError(NameError) in call 2, got: {other:?}\n\
             This indicates state isolation failure — x from call 1 leaked into call 2."
        ),
    }
}
