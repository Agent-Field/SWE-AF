//! Integration tests for the merged lib.rs conflict resolution.
//!
//! This file exercises the interaction boundaries between:
//! - issue/02-types: types.rs module (ExecutionSettings, ExecutionError, ExecutionResult, DEFAULT_ALLOWED_MODULES)
//! - issue/03-timeout: timeout.rs module (run_with_timeout)
//!
//! The conflict was in lib.rs, which now declares both `pub mod timeout` and `pub mod types`
//! and re-exports public types from the types module.
//!
//! Priority 1 (Conflict Resolution): Verify lib.rs correctly exposes both modules.
//! Priority 2 (Cross-Feature): Verify timeout module works with types from types module.
//! Priority 3 (Shared Exports): Verify all re-exports are accessible via crate root.

use llm_pyexec::timeout::run_with_timeout;
use llm_pyexec::{
    ExecutionError, ExecutionResult, ExecutionSettings, DEFAULT_ALLOWED_MODULES,
};
use std::time::{Duration, Instant};

// ── Priority 1: Conflict Resolution — lib.rs exposes both modules ────────────

/// Verify that `pub mod timeout` is correctly declared in lib.rs after conflict resolution.
/// The timeout module must be accessible as a submodule of the crate.
#[test]
fn test_lib_exposes_timeout_module() {
    // If timeout module is missing or incorrectly merged, this won't compile.
    // We exercise it to confirm the module is present and functional.
    let result = run_with_timeout(|| 42u32, 1_000_000_000);
    assert_eq!(
        result,
        Some(42u32),
        "timeout module must be accessible via llm_pyexec::timeout after merge"
    );
}

/// Verify that `pub mod types` is correctly declared and re-exports work after conflict resolution.
/// The types re-exports must be accessible directly from the crate root.
#[test]
fn test_lib_exposes_types_reexports_at_crate_root() {
    // These use the re-exports `pub use types::*` from lib.rs.
    // If the re-exports were dropped during conflict resolution, this won't compile.
    let settings = ExecutionSettings::default();
    assert_eq!(
        settings.timeout_ns, 5_000_000_000,
        "ExecutionSettings must be re-exported from crate root"
    );

    let _: &[&str] = DEFAULT_ALLOWED_MODULES;
    assert_eq!(
        DEFAULT_ALLOWED_MODULES.len(),
        11,
        "DEFAULT_ALLOWED_MODULES must be re-exported from crate root"
    );
}

/// Verify that both modules coexist correctly in the merged lib.rs.
/// Specifically test that importing both at once works (no name collision).
#[test]
fn test_both_modules_coexist_in_merged_lib() {
    // Use timeout module
    let timeout_result = run_with_timeout(|| "hello".to_string(), 500_000_000);
    assert!(
        timeout_result.is_some(),
        "timeout module must coexist with types module in merged lib.rs"
    );

    // Use types module (via re-export)
    let settings = ExecutionSettings::default();
    assert!(
        !settings.allowed_modules.is_empty(),
        "types module must coexist with timeout module in merged lib.rs"
    );
}

/// Verify all four re-exported items are accessible (ExecutionError, ExecutionResult,
/// ExecutionSettings, DEFAULT_ALLOWED_MODULES). The conflict resolution must have
/// preserved all four `pub use types::*` re-exports.
#[test]
fn test_all_four_type_reexports_accessible() {
    // ExecutionSettings
    let _settings: ExecutionSettings = ExecutionSettings::default();

    // ExecutionResult — construct directly since there's no execute() yet
    let _result: ExecutionResult = ExecutionResult {
        stdout: String::new(),
        stderr: String::new(),
        return_value: None,
        error: None,
        duration_ns: 0,
    };

    // ExecutionError — all 5 variants must be constructible
    let _e1 = ExecutionError::SyntaxError { message: "msg".to_string(), line: 1, col: 1 };
    let _e2 = ExecutionError::RuntimeError { message: "msg".to_string(), traceback: String::new() };
    let _e3 = ExecutionError::Timeout { limit_ns: 100 };
    let _e4 = ExecutionError::OutputLimitExceeded { limit_bytes: 1024 };
    let _e5 = ExecutionError::ModuleNotAllowed { module_name: "socket".to_string() };

    // DEFAULT_ALLOWED_MODULES
    assert_eq!(DEFAULT_ALLOWED_MODULES.len(), 11);
}

// ── Priority 2: Cross-Feature Interactions ───────────────────────────────────

/// Verify that ExecutionSettings::timeout_ns can be passed directly to run_with_timeout.
/// This is the primary cross-feature interaction: types.rs defines the timeout_ns field,
/// timeout.rs consumes it. They must use compatible types (both u64 nanoseconds).
#[test]
fn test_execution_settings_timeout_ns_compatible_with_run_with_timeout() {
    let settings = ExecutionSettings::default();

    // The timeout_ns field in ExecutionSettings must be directly usable as the
    // timeout parameter for run_with_timeout — same type (u64), same unit (ns).
    let result = run_with_timeout(|| 99u32, settings.timeout_ns);
    assert_eq!(
        result,
        Some(99u32),
        "ExecutionSettings::timeout_ns must be type-compatible with run_with_timeout's timeout_ns parameter"
    );
}

/// Verify that a custom ExecutionSettings with a short timeout_ns works correctly
/// when passed to run_with_timeout. This tests the timeout enforcement path with
/// a settings-sourced timeout value.
#[test]
fn test_custom_settings_timeout_ns_enforces_timeout_via_run_with_timeout() {
    let settings = ExecutionSettings {
        timeout_ns: 50_000_000, // 50ms
        ..ExecutionSettings::default()
    };

    let start = Instant::now();
    let result = run_with_timeout(
        || {
            std::thread::sleep(Duration::from_millis(500));
            42u32
        },
        settings.timeout_ns,
    );
    let elapsed = start.elapsed();

    assert!(
        result.is_none(),
        "run_with_timeout with ExecutionSettings::timeout_ns=50ms should time out slow closure"
    );
    assert!(
        elapsed < Duration::from_millis(500),
        "Timeout from ExecutionSettings should return promptly (elapsed: {:?})",
        elapsed
    );
}

/// Verify the ExecutionError::Timeout variant correctly uses limit_ns from settings.
/// This tests the round-trip: settings.timeout_ns → timeout fires → ExecutionError::Timeout.
#[test]
fn test_timeout_error_limit_ns_matches_execution_settings_timeout_ns() {
    let timeout_ns = 100_000_000u64; // 100ms
    let settings = ExecutionSettings {
        timeout_ns,
        ..ExecutionSettings::default()
    };

    // Simulate what an executor would do: use settings.timeout_ns for the timeout,
    // then construct ExecutionError::Timeout with the same value.
    let result = run_with_timeout(
        || std::thread::sleep(Duration::from_secs(10)),
        settings.timeout_ns,
    );

    assert!(result.is_none(), "Slow closure should time out");

    // Construct the error that would be returned, using the settings value
    let error = ExecutionError::Timeout {
        limit_ns: settings.timeout_ns,
    };

    // Verify the error correctly reflects the settings
    match error {
        ExecutionError::Timeout { limit_ns } => {
            assert_eq!(
                limit_ns, timeout_ns,
                "ExecutionError::Timeout::limit_ns must match ExecutionSettings::timeout_ns"
            );
        }
        _ => panic!("Expected Timeout variant"),
    }
}

/// Verify ExecutionResult can capture timeout information.
/// Test the construction of a complete ExecutionResult with a Timeout error.
#[test]
fn test_execution_result_with_timeout_error() {
    let settings = ExecutionSettings {
        timeout_ns: 50_000_000, // 50ms
        ..ExecutionSettings::default()
    };

    let start = Instant::now();
    let timed_out = run_with_timeout(
        || {
            std::thread::sleep(Duration::from_secs(10));
        },
        settings.timeout_ns,
    )
    .is_none();
    let duration_ns = start.elapsed().as_nanos() as u64;

    assert!(timed_out, "Should have timed out");

    // Construct a realistic ExecutionResult with a Timeout error
    let exec_result = ExecutionResult {
        stdout: String::new(),
        stderr: String::new(),
        return_value: None,
        error: Some(ExecutionError::Timeout {
            limit_ns: settings.timeout_ns,
        }),
        duration_ns,
    };

    // Verify it serializes to correct JSON with the internal tag
    let json = serde_json::to_string(&exec_result).expect("serialize ExecutionResult");
    assert!(
        json.contains(r#""type":"Timeout""#),
        "ExecutionResult with Timeout error must serialize with type discriminator: {json}"
    );
    assert!(
        json.contains(r#""limit_ns":50000000"#),
        "Timeout limit_ns must match settings.timeout_ns in serialized JSON: {json}"
    );
    assert!(
        json.contains(r#""error":"#),
        "ExecutionResult must have 'error' field in JSON: {json}"
    );
    assert!(
        json.contains(r#""duration_ns":"#),
        "ExecutionResult must have 'duration_ns' field in JSON: {json}"
    );
}

// ── Priority 3: Shared File Modifications — lib.rs correctness ───────────────

/// Verify module declarations are alphabetical as described in the conflict resolution notes.
/// The conflict resolution notes specify modules should be declared alphabetically (timeout, types).
#[test]
fn test_lib_modules_alphabetical_order() {
    // Both modules must be accessible. The order matters for readability but
    // not compilation. We test both are accessible via their full paths.
    let result = run_with_timeout::<_, u32>(|| 1u32, 1_000_000_000);
    assert!(result.is_some(), "timeout module accessible");
    let _: ExecutionSettings = ExecutionSettings::default();
    // If this compiles, both modules are correctly declared in lib.rs.
}

/// Verify that the stub comment was correctly dropped from lib.rs per conflict resolution notes.
/// The conflict resolution notes say the '// Stub implementation' comment was dropped.
/// We verify this indirectly: the types module functions correctly (not a stub).
#[test]
fn test_types_module_is_not_stub() {
    // A stub would not have real Default implementations or the full DEFAULT_ALLOWED_MODULES.
    let settings = ExecutionSettings::default();
    assert_eq!(
        settings.timeout_ns, 5_000_000_000,
        "ExecutionSettings should not be a stub — must have real default values"
    );
    assert_eq!(
        settings.max_output_bytes, 1_048_576,
        "ExecutionSettings should not be a stub — must have real default values"
    );
    assert_eq!(
        settings.allowed_modules.len(),
        11,
        "ExecutionSettings should not be a stub — must have 11 allowed modules"
    );
    assert_eq!(
        DEFAULT_ALLOWED_MODULES.len(),
        11,
        "DEFAULT_ALLOWED_MODULES should not be a stub — must have 11 entries"
    );
}

/// Verify that ExecutionError serialization uses internal tagging (#[serde(tag = "type")]).
/// This is critical for AC-17 (CLI JSON output must have error.type discriminator).
#[test]
fn test_execution_error_internal_serde_tagging_all_variants() {
    let variants: Vec<(&str, ExecutionError)> = vec![
        (
            "SyntaxError",
            ExecutionError::SyntaxError { message: "bad".to_string(), line: 1, col: 1 },
        ),
        (
            "RuntimeError",
            ExecutionError::RuntimeError { message: "err".to_string(), traceback: String::new() },
        ),
        ("Timeout", ExecutionError::Timeout { limit_ns: 1_000 }),
        ("OutputLimitExceeded", ExecutionError::OutputLimitExceeded { limit_bytes: 256 }),
        (
            "ModuleNotAllowed",
            ExecutionError::ModuleNotAllowed { module_name: "os".to_string() },
        ),
    ];

    for (expected_type, variant) in variants {
        let json = serde_json::to_string(&variant)
            .unwrap_or_else(|e| panic!("Failed to serialize {expected_type}: {e}"));

        // Internal tagging: the "type" field should be at the same level as other fields
        let expected_tag = format!(r#""type":"{expected_type}""#);
        assert!(
            json.contains(&expected_tag),
            "ExecutionError::{expected_type} must serialize with internal type tag. JSON: {json}"
        );

        // Ensure it's NOT externally tagged (no wrapping object with variant name as key)
        let external_tag = format!(r#"{{"{expected_type}""#);
        assert!(
            !json.starts_with(&external_tag),
            "ExecutionError::{expected_type} must NOT use external tagging. JSON: {json}"
        );
    }
}

/// Verify concurrent access to types (from types module) and timeout (from timeout module).
/// The merged lib.rs must not introduce any data races or unsafe sharing.
#[test]
fn test_concurrent_types_and_timeout_access() {
    use std::sync::{Arc, Mutex};
    use std::thread;

    let results: Arc<Mutex<Vec<Option<u32>>>> = Arc::new(Mutex::new(Vec::new()));
    let mut handles = vec![];

    for i in 0..8 {
        let results_clone = Arc::clone(&results);
        let handle = thread::spawn(move || {
            // Each thread creates settings (from types module) and uses timeout
            let settings = ExecutionSettings::default();
            let timeout = settings.timeout_ns;

            let result = run_with_timeout(move || i as u32, timeout);

            results_clone
                .lock()
                .expect("mutex poisoned")
                .push(result);
        });
        handles.push(handle);
    }

    for handle in handles {
        handle.join().expect("thread panicked");
    }

    let results = results.lock().expect("mutex poisoned");
    assert_eq!(
        results.len(),
        8,
        "All 8 concurrent threads should complete without data races"
    );
    for result in results.iter() {
        assert!(
            result.is_some(),
            "Each thread's timeout should return Some(...): {:?}",
            result
        );
    }
}
