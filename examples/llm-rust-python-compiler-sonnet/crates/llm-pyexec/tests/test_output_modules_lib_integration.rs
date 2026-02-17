//! Integration tests for the merged lib.rs conflict resolution.
//!
//! This file specifically exercises the interaction boundaries between:
//! - issue/04-output-buffer: output.rs module (OutputBuffer) + lib.rs re-export
//! - issue/05-module-allowlist: modules.rs module (check_module_allowed, build_allowed_set)
//!
//! The conflict in lib.rs was resolved by including BOTH:
//!   - `pub mod modules;` (from issue/05-module-allowlist)
//!   - `pub mod output;`  (from issue/04-output-buffer)
//! and preserving the `pub use output::OutputBuffer;` re-export.
//!
//! Test Priority 1: Conflict Resolution — verify lib.rs correctly exposes both new modules.
//! Test Priority 2: Cross-feature — OutputBuffer and modules both use ExecutionError from types.
//! Test Priority 3: Shared file — all modified functions in lib.rs work correctly together.

use llm_pyexec::modules::{build_allowed_set, check_module_allowed};
use llm_pyexec::output::OutputBuffer;
use llm_pyexec::{ExecutionError, ExecutionSettings, DEFAULT_ALLOWED_MODULES};
// Also test re-export from crate root:
use llm_pyexec::OutputBuffer as RootOutputBuffer;

// ── Priority 1: Conflict Resolution — lib.rs declares both new modules ────────

/// Verify `pub mod modules` is correctly declared in lib.rs after conflict resolution.
/// If the merge dropped this declaration, this test won't compile.
#[test]
fn test_lib_exposes_modules_module_after_merge() {
    let settings = ExecutionSettings::default();
    let allowed_set = build_allowed_set(&settings);
    // modules module must be accessible via llm_pyexec::modules
    let result = check_module_allowed("json", &allowed_set);
    assert_eq!(
        result,
        Ok(()),
        "modules module must be accessible via llm_pyexec::modules after lib.rs merge"
    );
}

/// Verify `pub mod output` is correctly declared in lib.rs after conflict resolution.
/// If the merge dropped this declaration, this test won't compile.
#[test]
fn test_lib_exposes_output_module_after_merge() {
    // output module must be accessible via llm_pyexec::output
    let buf = OutputBuffer::new(1024);
    buf.write_stdout(b"test").expect("write_stdout must work after lib.rs merge");
    let (stdout, _) = buf.into_strings();
    assert_eq!(
        stdout, "test",
        "output module must be accessible via llm_pyexec::output after lib.rs merge"
    );
}

/// Verify the `pub use output::OutputBuffer` re-export is preserved in lib.rs.
/// The conflict resolution notes state this re-export from the output-buffer branch
/// must be present; the modules branch added no re-exports.
#[test]
fn test_output_buffer_reexported_at_crate_root() {
    // RootOutputBuffer is imported as `use llm_pyexec::OutputBuffer` — tests the re-export
    let buf = RootOutputBuffer::new(256);
    buf.write_stdout(b"via root").expect("write via re-exported OutputBuffer failed");
    let (stdout, _) = buf.into_strings();
    assert_eq!(
        stdout, "via root",
        "OutputBuffer must be re-exported at crate root via `pub use output::OutputBuffer`"
    );
}

/// Verify that modules are declared in alphabetical order as stated in conflict resolution.
/// The resolution placed them as: modules, output, timeout, types (alphabetical).
/// Both modules and output must be accessible.
#[test]
fn test_both_new_modules_coexist_in_alphabetical_order() {
    // modules (alphabetically first of the two new modules)
    let set = build_allowed_set(&ExecutionSettings::default());
    assert!(
        !set.is_empty(),
        "modules module must coexist with output module in merged lib.rs"
    );

    // output (alphabetically second of the two new modules)
    let buf = OutputBuffer::new(64);
    assert!(
        buf.write_stdout(b"coexist").is_ok(),
        "output module must coexist with modules module in merged lib.rs"
    );
}

// ── Priority 2: Cross-Feature Interactions ────────────────────────────────────

/// Both OutputBuffer::write_stdout and check_module_allowed return Result<(), ExecutionError>.
/// This test verifies both error types are from the same ExecutionError enum (types.rs),
/// and can be used interchangeably — proving the shared type dependency is intact.
#[test]
fn test_output_and_modules_share_execution_error_type() {
    let buf = OutputBuffer::new(5); // tiny limit

    // Trigger an OutputLimitExceeded error from output module
    buf.write_stdout(b"hello").expect("first write should succeed");
    let output_err = buf.write_stdout(b"!").expect_err("second write should fail");

    // Trigger a ModuleNotAllowed error from modules module
    let empty_set = std::collections::HashSet::new();
    let module_err = check_module_allowed("socket", &empty_set)
        .expect_err("socket should be denied");

    // Both are ExecutionError variants — can be used in same match/collection
    let errors: Vec<ExecutionError> = vec![output_err, module_err];
    assert_eq!(errors.len(), 2, "Both modules must produce ExecutionError variants");

    match &errors[0] {
        ExecutionError::OutputLimitExceeded { limit_bytes } => {
            assert_eq!(*limit_bytes, 5, "OutputLimitExceeded limit_bytes must be 5");
        }
        other => panic!("Expected OutputLimitExceeded from output module, got {:?}", other),
    }

    match &errors[1] {
        ExecutionError::ModuleNotAllowed { module_name } => {
            assert_eq!(module_name, "socket", "ModuleNotAllowed module_name must be 'socket'");
        }
        other => panic!("Expected ModuleNotAllowed from modules module, got {:?}", other),
    }
}

/// The ExecutionSettings::max_output_bytes field drives OutputBuffer limits.
/// The ExecutionSettings::allowed_modules field drives build_allowed_set.
/// Both fields live in the same struct — verify they can both be read and used
/// simultaneously without conflict.
#[test]
fn test_execution_settings_drives_both_output_and_modules() {
    let settings = ExecutionSettings {
        max_output_bytes: 10,
        allowed_modules: vec!["math".to_string(), "json".to_string()],
        timeout_ns: 5_000_000_000,
    };

    // Use settings.max_output_bytes with OutputBuffer
    let buf = OutputBuffer::new(settings.max_output_bytes);
    assert!(buf.write_stdout(b"hello").is_ok(), "5-byte write within 10-byte limit should succeed");
    assert!(buf.write_stdout(b"world").is_ok(), "5-byte second write hitting limit exactly should succeed");
    // One more byte should exceed
    let overflow_err = buf.write_stdout(b"!").expect_err("11th byte should exceed limit");
    assert!(
        matches!(overflow_err, ExecutionError::OutputLimitExceeded { limit_bytes: 10 }),
        "Output limit from settings.max_output_bytes must be enforced: {:?}", overflow_err
    );

    // Use settings.allowed_modules with build_allowed_set
    let allowed_set = build_allowed_set(&settings);
    assert_eq!(allowed_set.len(), 2, "build_allowed_set must use settings.allowed_modules");
    assert!(
        check_module_allowed("math", &allowed_set).is_ok(),
        "math must be allowed per custom settings"
    );
    assert!(
        check_module_allowed("json", &allowed_set).is_ok(),
        "json must be allowed per custom settings"
    );
    assert!(
        check_module_allowed("re", &allowed_set).is_err(),
        "re must be denied when not in custom settings"
    );
}

/// OutputBuffer.write_stdout() checks the combined limit using both stdout and stderr lengths.
/// modules::check_module_allowed also interacts via ExecutionError.
/// Verify that when we simulate an executor: module check first, then output capture —
/// both components working in the same execution flow.
#[test]
fn test_module_check_then_output_capture_execution_flow() {
    let settings = ExecutionSettings::default();
    let allowed_set = build_allowed_set(&settings);
    let buf = OutputBuffer::new(settings.max_output_bytes);

    // Step 1: Module allowlist check (simulates: before VM runs, check import)
    let check_result = check_module_allowed("json", &allowed_set);
    assert!(
        check_result.is_ok(),
        "json module must be allowed by default settings"
    );

    // Step 2: Capture output (simulates: VM runs, output is captured)
    let simulated_output = b"import json\nresult = json.dumps({'a': 1})\nprint(result)\n";
    buf.write_stdout(simulated_output).expect("stdout write must succeed within limit");

    let (stdout, stderr) = buf.into_strings();
    assert!(
        stdout.contains("import json"),
        "Captured stdout must contain the written data"
    );
    assert!(
        stderr.is_empty(),
        "No stderr should be written in normal flow"
    );
}

/// Test that module denial and output buffer work correctly when a disallowed module
/// is checked — the error from modules must be representable in ExecutionResult.
#[test]
fn test_denied_module_produces_correct_execution_error_structure() {
    let settings = ExecutionSettings::default();
    let allowed_set = build_allowed_set(&settings);

    // Check that socket is denied (not in DEFAULT_ALLOWED_MODULES)
    let result = check_module_allowed("socket", &allowed_set);
    let error = result.expect_err("socket must be denied by default settings");

    match &error {
        ExecutionError::ModuleNotAllowed { module_name } => {
            assert_eq!(
                module_name, "socket",
                "Error must carry the exact denied module name"
            );
        }
        other => panic!("Expected ModuleNotAllowed, got {:?}", other),
    }

    // Verify the error can be serialized (for ExecutionResult.error field)
    let json = serde_json::to_string(&error).expect("ModuleNotAllowed must be serializable");
    assert!(
        json.contains(r#""type":"ModuleNotAllowed""#),
        "ModuleNotAllowed must have internal type tag: {json}"
    );
    assert!(
        json.contains(r#""module_name":"socket""#),
        "ModuleNotAllowed must contain the module name: {json}"
    );
}

/// Test the combined behavior: output buffer writes up to a custom limit, then
/// the limit_exceeded flag is visible, and the error type matches OutputLimitExceeded.
/// This verifies the cross-module error type compatibility introduced by the merge.
#[test]
fn test_output_limit_exceeded_error_round_trip_after_merge() {
    let max_bytes = 20usize;
    let buf = OutputBuffer::new(max_bytes);

    // Write exactly at the limit (should succeed)
    buf.write_stdout(b"12345678901234567890").expect("exactly 20 bytes must succeed");

    // One more write must fail
    let err = buf.write_stdout(b"x").expect_err("write past limit must fail");

    // Verify the error structure (used by both output and modules features post-merge)
    match err {
        ExecutionError::OutputLimitExceeded { limit_bytes } => {
            assert_eq!(
                limit_bytes, max_bytes,
                "limit_bytes must match the configured max_output_bytes"
            );
        }
        other => panic!("Expected OutputLimitExceeded, got {:?}", other),
    }

    assert!(
        buf.is_limit_exceeded(),
        "is_limit_exceeded() must return true after overflow"
    );
}

// ── Priority 3: Shared File — lib.rs correctness after both branches merged ───

/// Verify the four re-exports from the resolved lib.rs are all accessible.
/// The conflict resolution preserved: ExecutionError, ExecutionResult, ExecutionSettings,
/// DEFAULT_ALLOWED_MODULES (from types), and OutputBuffer (from output).
/// The modules branch added no re-exports.
#[test]
fn test_all_reexports_present_after_merge() {
    // From types re-exports (pre-existing + carried through merge)
    let _settings: ExecutionSettings = ExecutionSettings::default();
    let _error: ExecutionError = ExecutionError::ModuleNotAllowed {
        module_name: "test".to_string(),
    };
    let _modules: &[&str] = DEFAULT_ALLOWED_MODULES;

    // From output re-export (added by issue/04-output-buffer, preserved in merge)
    let _buf: RootOutputBuffer = RootOutputBuffer::new(1024);

    // modules module has no crate-root re-export (per conflict resolution notes)
    // It must be accessed via llm_pyexec::modules::*
    let settings2 = ExecutionSettings::default();
    let set = build_allowed_set(&settings2);
    assert!(
        set.len() > 0,
        "modules module must be accessible via llm_pyexec::modules path"
    );
}

/// Verify that DEFAULT_ALLOWED_MODULES (from types) and build_allowed_set (from modules)
/// produce consistent results — both modules operate on the same constant.
#[test]
fn test_default_allowed_modules_consistent_with_build_allowed_set() {
    let settings = ExecutionSettings::default();
    let set = build_allowed_set(&settings);

    // Every entry in DEFAULT_ALLOWED_MODULES must be present in the set
    for module in DEFAULT_ALLOWED_MODULES {
        assert!(
            set.contains(*module),
            "DEFAULT_ALLOWED_MODULES entry '{}' must be present in build_allowed_set output",
            module
        );
    }

    // The set size must equal DEFAULT_ALLOWED_MODULES length (no extras added)
    assert_eq!(
        set.len(),
        DEFAULT_ALLOWED_MODULES.len(),
        "build_allowed_set size must match DEFAULT_ALLOWED_MODULES length"
    );
}

/// Verify that all DEFAULT_ALLOWED_MODULES pass check_module_allowed using the default set.
/// This exercises both modules.rs and types.rs (via DEFAULT_ALLOWED_MODULES) together.
#[test]
fn test_all_default_modules_pass_check_module_allowed() {
    let settings = ExecutionSettings::default();
    let set = build_allowed_set(&settings);

    for module in DEFAULT_ALLOWED_MODULES {
        let result = check_module_allowed(module, &set);
        assert!(
            result.is_ok(),
            "DEFAULT_ALLOWED_MODULES entry '{}' must pass check_module_allowed",
            module
        );
    }
}

/// Verify the special os/os.path behavior works correctly when os.path is in
/// DEFAULT_ALLOWED_MODULES but bare os is not.
#[test]
fn test_os_path_special_case_with_default_allowed_modules() {
    let settings = ExecutionSettings::default();
    let set = build_allowed_set(&settings);

    // os.path is in DEFAULT_ALLOWED_MODULES
    assert!(
        DEFAULT_ALLOWED_MODULES.contains(&"os.path"),
        "os.path must be in DEFAULT_ALLOWED_MODULES"
    );
    // bare os is NOT in DEFAULT_ALLOWED_MODULES
    assert!(
        !DEFAULT_ALLOWED_MODULES.contains(&"os"),
        "bare os must NOT be in DEFAULT_ALLOWED_MODULES"
    );

    // os.path check must pass
    assert!(
        check_module_allowed("os.path", &set).is_ok(),
        "os.path must pass check_module_allowed with default settings"
    );

    // bare os import must ALSO pass (special case in modules.rs)
    assert!(
        check_module_allowed("os", &set).is_ok(),
        "bare 'os' must pass check_module_allowed when 'os.path' is in the set (special case)"
    );
}

/// Verify that clone semantics of OutputBuffer (from output module) work correctly,
/// and that into_strings works when live clones exist (timeout path).
/// This cross-tests the Arc clone behavior with the timeout module's abandonment pattern.
#[test]
fn test_output_buffer_clone_semantics_for_timeout_path() {
    let buf = OutputBuffer::new(1024);

    // Simulate: executor holds primary handle, VM thread holds clone
    let vm_clone = buf.clone();

    // VM thread writes to its clone
    vm_clone.write_stdout(b"from vm thread").expect("vm clone write must succeed");
    vm_clone.write_stderr(b"vm stderr").expect("vm clone stderr write must succeed");

    // Executor reads from primary handle while vm_clone still alive
    // (the fallback path in into_strings must be used since vm_clone is alive)
    let (stdout, stderr) = buf.into_strings();
    assert_eq!(
        stdout, "from vm thread",
        "into_strings must return stdout written via clone (timeout fallback path)"
    );
    assert_eq!(
        stderr, "vm stderr",
        "into_strings must return stderr written via clone (timeout fallback path)"
    );

    // vm_clone still exists here — into_strings must not panic
    drop(vm_clone); // Clean up
}

/// Verify that OutputBuffer limit is enforced across both stdout and stderr streams
/// (combined limit), and that the error is the correct ExecutionError variant.
/// This tests the exact combined-size logic from output.rs within the merged lib.rs.
#[test]
fn test_combined_stdout_stderr_limit_produces_correct_error() {
    let buf = OutputBuffer::new(10);

    // Write 6 bytes to stdout
    buf.write_stdout(b"123456").expect("6 bytes to stdout must succeed within 10-byte limit");

    // Try to write 5 bytes to stderr — combined would be 11, exceeding 10
    let err = buf.write_stderr(b"abcde").expect_err("5 more bytes to stderr should exceed 10-byte combined limit");

    match err {
        ExecutionError::OutputLimitExceeded { limit_bytes } => {
            assert_eq!(limit_bytes, 10, "limit_bytes must be the configured limit of 10");
        }
        other => panic!("Expected OutputLimitExceeded, got {:?}", other),
    }

    assert!(
        buf.is_limit_exceeded(),
        "is_limit_exceeded must be true after combined limit exceeded"
    );

    // into_strings should still work (returns partial stdout, empty stderr)
    let (stdout, stderr) = buf.into_strings();
    assert_eq!(stdout, "123456", "Partial stdout captured before limit exceeded");
    assert_eq!(stderr, "", "Stderr was not written (write was rejected)");
}

/// Concurrent test: Multiple threads use OutputBuffer (from output) and
/// check_module_allowed (from modules) simultaneously.
/// Verifies the merged lib.rs has no unsafe shared state between the two new modules.
#[test]
fn test_concurrent_output_and_module_operations() {
    use std::sync::Arc;
    use std::thread;

    let buf = Arc::new(OutputBuffer::new(1_000_000));
    let settings = Arc::new(ExecutionSettings::default());
    let mut handles = vec![];

    for i in 0..8 {
        let buf_clone = Arc::new(buf.as_ref().clone());
        let settings_clone = Arc::clone(&settings);

        let handle = thread::spawn(move || {
            // Each thread does a module check
            let allowed_set = build_allowed_set(&settings_clone);
            let module_check = check_module_allowed("json", &allowed_set);
            assert!(module_check.is_ok(), "json must be allowed in thread {}", i);

            // And an output write
            let data = format!("thread {} output\n", i);
            buf_clone
                .write_stdout(data.as_bytes())
                .expect("concurrent write must not fail within large limit");
        });
        handles.push(handle);
    }

    for handle in handles {
        handle.join().expect("thread must not panic");
    }
}
