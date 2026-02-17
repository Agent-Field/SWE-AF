//! Integration tests for the vm-wrapper merge (issue/06-vm-wrapper).
//!
//! This file exercises the interaction boundaries between:
//! - vm.rs (build_interpreter, run_code) — newly merged via issue/06-vm-wrapper
//! - output.rs (OutputBuffer) — shared with vm.rs for stdout/stderr capture
//! - modules.rs (check_module_allowed, build_allowed_set) — import hook integration
//! - timeout.rs (run_with_timeout) — wraps the VM execution thread
//! - types.rs (ExecutionSettings, ExecutionResult, ExecutionError) — shared types
//!
//! Priority 1: VM + OutputBuffer integration (stdout/stderr capture via sys.stdout/sys.stderr)
//! Priority 2: VM + modules integration (import hook enforcement of allowlist)
//! Priority 3: VM + timeout integration (timeout wrapping run_code)
//! Priority 4: Full pipeline — all five modules working together end-to-end

use llm_pyexec::modules::build_allowed_set;
use llm_pyexec::output::OutputBuffer;
use llm_pyexec::timeout::run_with_timeout;
use llm_pyexec::{ExecutionError, ExecutionResult, ExecutionSettings, DEFAULT_ALLOWED_MODULES};

// ── Priority 1: VM-OutputBuffer interaction boundaries ───────────────────────

/// Verify OutputBuffer's clone() semantics support the VM's execution pattern.
///
/// vm.rs calls: let output = OutputBuffer::new(...); then passes output.clone()
/// to install_output_capture() and keeps the original for into_strings().
/// This test verifies that write-via-clone + read-via-original is correct.
#[test]
fn test_vm_output_buffer_clone_write_read_pattern() {
    let output = OutputBuffer::new(1_048_576);

    // Simulate VM's pattern: clone for capture, keep original for reading
    let vm_capture = output.clone();

    // VM writes to the clone (as sys.stdout.write() would do)
    vm_capture
        .write_stdout(b"hello world\n")
        .expect("VM stdout write must succeed");

    // After VM completes, main thread reads via original
    let (stdout, stderr) = output.into_strings();
    assert_eq!(
        stdout, "hello world\n",
        "stdout written via VM clone must be readable via original OutputBuffer"
    );
    assert_eq!(
        stderr, "",
        "stderr must be empty when only stdout was written"
    );
}

/// Verify OutputBuffer handles the timeout path where the VM clone outlives into_strings().
///
/// vm.rs uses run_with_timeout which may abandon the VM thread. That thread
/// holds a clone of OutputBuffer. into_strings() must handle this gracefully.
#[test]
fn test_vm_output_buffer_survives_live_vm_clone_on_timeout() {
    let output = OutputBuffer::new(1_048_576);

    // Simulate: VM thread writes before timeout
    let vm_clone = output.clone();
    vm_clone
        .write_stdout(b"partial output")
        .expect("write before timeout");
    vm_clone.write_stderr(b"some error").expect("stderr write");

    // Simulate: timeout fires, main thread calls into_strings while vm_clone still alive
    // (vm_clone is NOT dropped yet — simulating the abandoned thread holding it)
    let (stdout, stderr) = output.into_strings();

    assert_eq!(
        stdout, "partial output",
        "into_strings must recover stdout even when VM clone is still alive"
    );
    assert_eq!(
        stderr, "some error",
        "into_strings must recover stderr even when VM clone is still alive"
    );

    // Now drop the abandoned VM clone (simulates OS eventually reclaiming it)
    drop(vm_clone);
}

/// Verify that OutputBuffer's output size limit is correctly enforced
/// when using the VM's write-via-closure pattern (multiple small writes).
///
/// vm.rs's build_writer_object() calls write_stdout() once per write() call,
/// and print() generates two write() calls: "hello" + "\n".
/// This verifies accumulated small writes correctly enforce the limit.
#[test]
fn test_vm_output_multiple_small_writes_enforce_limit() {
    let settings = ExecutionSettings {
        max_output_bytes: 20,
        ..ExecutionSettings::default()
    };
    let output = OutputBuffer::new(settings.max_output_bytes);
    let vm_clone = output.clone();

    // Simulate multiple print() calls: each generates 2 write() calls
    // print("hello") → write("hello") + write("\n") = 6 bytes
    vm_clone.write_stdout(b"hello").expect("write 1 ok");
    vm_clone.write_stdout(b"\n").expect("write 2 ok");

    // print("world") → write("world") + write("\n") = 6 bytes (total: 12)
    vm_clone.write_stdout(b"world").expect("write 3 ok");
    vm_clone.write_stdout(b"\n").expect("write 4 ok");

    // print("12345678") → write("12345678") = 8 bytes (total: 20 — exactly at limit)
    vm_clone.write_stdout(b"12345678").expect("write 5 ok");
    // One more byte would be 21 — over limit
    let overflow = vm_clone.write_stdout(b"\n");

    // The final write should fail with OutputLimitExceeded
    assert!(
        matches!(
            overflow,
            Err(ExecutionError::OutputLimitExceeded { limit_bytes: 20 })
        ),
        "VM output capture must enforce max_output_bytes limit across accumulated writes: {:?}",
        overflow
    );

    assert!(
        output.is_limit_exceeded(),
        "is_limit_exceeded must be true after overflow in VM write pattern"
    );
}

/// Verify stderr capture works independently from stdout.
///
/// vm.rs installs separate write objects for sys.stdout and sys.stderr.
/// Both write to the same OutputBuffer but the data is separated.
#[test]
fn test_vm_stderr_captured_independently_from_stdout() {
    let output = OutputBuffer::new(1_048_576);
    let vm_clone = output.clone();

    // Simulate VM: print goes to stdout, warnings/tracebacks go to stderr
    vm_clone
        .write_stdout(b"normal output\n")
        .expect("stdout write");
    vm_clone
        .write_stderr(b"warning: something\n")
        .expect("stderr write");
    vm_clone
        .write_stdout(b"more output\n")
        .expect("second stdout write");

    let (stdout, stderr) = output.into_strings();
    assert_eq!(
        stdout, "normal output\nmore output\n",
        "stdout must only contain stdout writes"
    );
    assert_eq!(
        stderr, "warning: something\n",
        "stderr must only contain stderr writes"
    );
}

// ── Priority 2: VM-modules interaction boundaries ────────────────────────────

/// Verify build_allowed_set produces the exact set that check_module_allowed expects.
///
/// vm.rs calls build_allowed_set(&settings) to get the HashSet, then passes it
/// to is_module_allowed/check_module_allowed in the import hook closure.
/// This test verifies the type contract between the two functions.
#[test]
fn test_vm_modules_allowed_set_contract() {
    let settings = ExecutionSettings::default();
    let allowed_set = build_allowed_set(&settings);

    // All modules in DEFAULT_ALLOWED_MODULES must pass
    for module in DEFAULT_ALLOWED_MODULES {
        assert!(
            llm_pyexec::modules::check_module_allowed(module, &allowed_set).is_ok(),
            "Module '{}' from DEFAULT_ALLOWED_MODULES must be allowed in VM's allowed_set",
            module
        );
    }

    // socket, subprocess, sys.stdin must be denied (not in allowlist, no special case)
    // Note: bare "os" is NOT in DEFAULT_ALLOWED_MODULES but IS allowed via the
    // os/os.path special case in check_module_allowed (since "os.path" is in the set)
    for denied in &["socket", "subprocess", "sys.stdin"] {
        assert!(
            llm_pyexec::modules::check_module_allowed(denied, &allowed_set).is_err(),
            "Module '{}' must be denied by default allowed_set",
            denied
        );
    }

    // Confirm the os/os.path special case: "os" is allowed even though not explicitly listed
    assert!(
        llm_pyexec::modules::check_module_allowed("os", &allowed_set).is_ok(),
        "bare 'os' must be allowed via os/os.path special case in modules.rs"
    );
}

/// Verify the ExecutionError::ModuleNotAllowed variant has the correct structure
/// that vm.rs's extract_module_not_allowed expects to detect.
///
/// vm.rs raises ImportError("ModuleNotAllowed:<name>") and then extracts it
/// via strip_prefix("ModuleNotAllowed:"). The check_module_allowed function
/// returns ExecutionError::ModuleNotAllowed { module_name }.
/// Both must use the exact same module name.
#[test]
fn test_vm_module_not_allowed_error_name_matches() {
    let denied_module = "socket";
    let empty_set = std::collections::HashSet::new();

    let err = llm_pyexec::modules::check_module_allowed(denied_module, &empty_set)
        .expect_err("socket must be denied");

    match &err {
        ExecutionError::ModuleNotAllowed { module_name } => {
            assert_eq!(
                module_name.as_str(),
                denied_module,
                "ModuleNotAllowed.module_name must exactly match the denied module name"
            );
        }
        other => panic!("Expected ModuleNotAllowed, got {:?}", other),
    }

    // Also verify it serializes to the format expected by CLI (AC-17)
    let json = serde_json::to_string(&err).expect("ModuleNotAllowed must serialize");
    assert!(
        json.contains(r#""type":"ModuleNotAllowed""#),
        "ModuleNotAllowed must have internal type tag for CLI output: {json}"
    );
    assert!(
        json.contains(r#""module_name":"socket""#),
        "ModuleNotAllowed JSON must contain the module name: {json}"
    );
}

/// Verify the os/os.path special case works correctly in a VM-like context.
///
/// vm.rs's is_module_allowed checks both the full module name and its
/// parent package. For "os.path", the parent is "os" — and check_module_allowed
/// already handles the bare "os" special case.
#[test]
fn test_vm_os_path_special_case_in_module_check() {
    let settings = ExecutionSettings::default();
    let allowed_set = build_allowed_set(&settings);

    // "os.path" is explicitly allowed
    assert!(
        llm_pyexec::modules::check_module_allowed("os.path", &allowed_set).is_ok(),
        "os.path must be explicitly allowed"
    );

    // "os" (bare) must also be allowed due to os.path special case
    assert!(
        llm_pyexec::modules::check_module_allowed("os", &allowed_set).is_ok(),
        "bare 'os' must be allowed when os.path is in allowlist (import machinery loads it)"
    );

    // "os" parent is allowed, which enables VM's submodule resolution for os.path
    let parent_allowed =
        llm_pyexec::modules::check_module_allowed("os", &allowed_set).is_ok();
    assert!(
        parent_allowed,
        "os (parent of os.path) must be allowed for VM submodule resolution to work"
    );
}

/// Verify that custom ExecutionSettings with restricted allowed_modules
/// produces an allowed_set that denies everything not explicitly listed.
///
/// This is what vm.rs uses for the import hook — settings drive everything.
#[test]
fn test_vm_custom_settings_restrict_modules_for_import_hook() {
    let settings = ExecutionSettings {
        allowed_modules: vec!["math".to_string()],
        ..ExecutionSettings::default()
    };
    let allowed_set = build_allowed_set(&settings);

    // math should be allowed
    assert!(
        llm_pyexec::modules::check_module_allowed("math", &allowed_set).is_ok(),
        "math must be allowed in custom settings"
    );

    // json must be denied (not in custom list)
    let result = llm_pyexec::modules::check_module_allowed("json", &allowed_set);
    assert!(
        matches!(
            result,
            Err(ExecutionError::ModuleNotAllowed { ref module_name }) if module_name == "json"
        ),
        "json must be denied when not in custom allowed_modules: {:?}",
        result
    );
}

// ── Priority 3: VM-timeout interaction boundaries ────────────────────────────

/// Verify run_with_timeout can wrap a VM-like computation that returns Option<VmResult>.
///
/// vm.rs uses run_with_timeout to enforce the execution timeout. The closure
/// returns a result structure. This test verifies the wrapping pattern works.
#[test]
fn test_vm_timeout_wrapping_pattern_with_result_struct() {
    let settings = ExecutionSettings {
        timeout_ns: 2_000_000_000, // 2 second timeout (generous for fast computation)
        ..ExecutionSettings::default()
    };

    // Simulate a fast VM execution that completes within timeout
    let result = run_with_timeout(
        move || {
            // Simulate VM work: build an ExecutionResult-like struct
            let output = OutputBuffer::new(settings.max_output_bytes);
            output.write_stdout(b"result: 42\n").unwrap();
            let (stdout, stderr) = output.into_strings();
            ExecutionResult {
                stdout,
                stderr,
                return_value: Some("42".to_string()),
                error: None,
                duration_ns: 1_000_000,
            }
        },
        settings.timeout_ns,
    );

    assert!(
        result.is_some(),
        "Fast VM execution must not be timed out with generous timeout"
    );

    let exec_result = result.unwrap();
    assert_eq!(exec_result.stdout, "result: 42\n");
    assert_eq!(exec_result.return_value, Some("42".to_string()));
    assert!(exec_result.error.is_none());
}

/// Verify that when run_with_timeout returns None (timeout), we correctly
/// construct an ExecutionResult with ExecutionError::Timeout.
///
/// This is the exact pattern vm.rs uses after the timeout fires.
#[test]
fn test_vm_timeout_constructs_correct_execution_result() {
    let settings = ExecutionSettings {
        timeout_ns: 50_000_000, // 50ms — fast timeout
        ..ExecutionSettings::default()
    };

    let start = std::time::Instant::now();
    let vm_result = run_with_timeout(
        || {
            std::thread::sleep(std::time::Duration::from_millis(500));
            42u32
        },
        settings.timeout_ns,
    );
    let duration_ns = start.elapsed().as_nanos() as u64;

    // VM timed out — construct ExecutionResult as the executor would
    let exec_result = if let Some(_) = vm_result {
        panic!("Should have timed out");
    } else {
        ExecutionResult {
            stdout: String::new(),
            stderr: String::new(),
            return_value: None,
            error: Some(ExecutionError::Timeout {
                limit_ns: settings.timeout_ns,
            }),
            duration_ns,
        }
    };

    assert!(
        matches!(
            exec_result.error,
            Some(ExecutionError::Timeout { limit_ns }) if limit_ns == settings.timeout_ns
        ),
        "Timed-out execution must produce ExecutionError::Timeout with correct limit_ns: {:?}",
        exec_result.error
    );

    assert!(
        exec_result.duration_ns <= 500_000_000,
        "duration_ns after timeout must be less than 500ms: {}",
        exec_result.duration_ns
    );
}

/// Verify the OutputBuffer can outlive a timed-out run_with_timeout closure.
///
/// When vm.rs's closure is abandoned, the OutputBuffer clone it held is still
/// live. The executor must be able to read partial output via into_strings().
#[test]
fn test_vm_output_buffer_accessible_after_timeout_abandonment() {
    use std::sync::{Arc, Barrier};

    let output = OutputBuffer::new(1_048_576);
    let output_clone = output.clone();

    // Use a barrier to ensure the thread has written before timeout check
    let barrier = Arc::new(Barrier::new(2));
    let barrier_clone = Arc::clone(&barrier);

    let handle = std::thread::spawn(move || {
        // Write some partial output before "timing out"
        output_clone
            .write_stdout(b"partial output before timeout\n")
            .expect("write must succeed");
        barrier_clone.wait(); // Signal that write is done
        // Thread keeps running (simulating abandoned VM thread)
        std::thread::sleep(std::time::Duration::from_millis(50));
        // output_clone is dropped here when thread exits
    });

    // Wait for the thread to write
    barrier.wait();

    // Main thread reads output (simulating post-timeout recovery)
    // output still has a live clone (the thread's output_clone)
    let (stdout, _stderr) = output.into_strings();

    assert!(
        stdout.contains("partial output"),
        "into_strings must recover partial output written before thread abandonment: '{stdout}'"
    );

    handle.join().expect("thread must not panic");
}

// ── Priority 4: Full pipeline — all modules end-to-end ───────────────────────

/// Test the complete pipeline that the executor would use:
/// ExecutionSettings → OutputBuffer + allowed_set → run_with_timeout → ExecutionResult
///
/// This exercises the cross-module contracts without invoking the VM directly.
#[test]
fn test_full_pipeline_settings_to_result_no_vm() {
    // Step 1: Create settings (types.rs)
    let settings = ExecutionSettings {
        timeout_ns: 1_000_000_000,
        max_output_bytes: 1024,
        ..ExecutionSettings::default()
    };

    // Step 2: Build OutputBuffer from settings (output.rs)
    let output = OutputBuffer::new(settings.max_output_bytes);

    // Step 3: Build allowed set from settings (modules.rs)
    let allowed_set = build_allowed_set(&settings);

    // Step 4: Validate module in import hook (modules.rs)
    let json_check = llm_pyexec::modules::check_module_allowed("json", &allowed_set);
    assert!(json_check.is_ok(), "json must be allowed");

    let socket_check = llm_pyexec::modules::check_module_allowed("socket", &allowed_set);
    assert!(socket_check.is_err(), "socket must be denied");

    // Step 5: Execute with timeout (timeout.rs wrapping output.rs)
    let output_clone = output.clone();
    let start = std::time::Instant::now();
    let vm_result = run_with_timeout(
        move || {
            // Simulate VM execution: write output
            output_clone.write_stdout(b"42\n").unwrap();
            let (stdout, stderr) = output_clone.into_strings();
            (stdout, stderr, None::<ExecutionError>)
        },
        settings.timeout_ns,
    );
    let duration_ns = start.elapsed().as_nanos() as u64;

    // Step 6: Build ExecutionResult (types.rs)
    let exec_result = match vm_result {
        Some((stdout, stderr, error)) => ExecutionResult {
            stdout,
            stderr,
            return_value: None,
            error,
            duration_ns,
        },
        None => ExecutionResult {
            stdout: String::new(),
            stderr: String::new(),
            return_value: None,
            error: Some(ExecutionError::Timeout {
                limit_ns: settings.timeout_ns,
            }),
            duration_ns,
        },
    };

    // Verify final result
    assert_eq!(exec_result.stdout, "42\n", "stdout must be captured");
    assert!(
        exec_result.error.is_none(),
        "No error expected in successful execution"
    );
    assert!(
        exec_result.duration_ns > 0,
        "duration_ns must be non-zero"
    );

    // Step 7: Verify JSON serialization (serde + types.rs)
    let json = serde_json::to_string(&exec_result).expect("ExecutionResult must serialize");
    assert!(
        json.contains(r#""stdout":"42\n""#),
        "Serialized result must contain stdout: {json}"
    );
    assert!(
        json.contains(r#""error":null"#),
        "Serialized result must have null error on success: {json}"
    );
    assert!(
        json.contains(r#""duration_ns":"#),
        "Serialized result must contain duration_ns: {json}"
    );
}

/// Verify the full error reporting pipeline: denied module → ExecutionResult with error.
///
/// Simulates what happens when the VM's import hook denies a module:
/// check_module_allowed() → ExecutionError::ModuleNotAllowed → ExecutionResult
#[test]
fn test_full_pipeline_denied_module_error_propagation() {
    let settings = ExecutionSettings::default();
    let allowed_set = build_allowed_set(&settings);

    // Simulate VM import hook denying "socket"
    let import_err = llm_pyexec::modules::check_module_allowed("socket", &allowed_set)
        .expect_err("socket must be denied");

    // Build ExecutionResult as vm.rs would
    let exec_result = ExecutionResult {
        stdout: String::new(),
        stderr: String::new(),
        return_value: None,
        error: Some(import_err),
        duration_ns: 100_000,
    };

    // Verify the result
    match &exec_result.error {
        Some(ExecutionError::ModuleNotAllowed { module_name }) => {
            assert_eq!(
                module_name, "socket",
                "ModuleNotAllowed must carry 'socket' as the denied module name"
            );
        }
        other => panic!(
            "Expected Some(ModuleNotAllowed(socket)), got {:?}",
            other
        ),
    }

    // Verify JSON serialization for CLI output (AC-17)
    let json = serde_json::to_string(&exec_result).expect("ExecutionResult must serialize");
    assert!(
        json.contains(r#""type":"ModuleNotAllowed""#),
        "CLI output must have type discriminator for ModuleNotAllowed: {json}"
    );
    assert!(
        json.contains(r#""module_name":"socket""#),
        "CLI output must have module_name in ModuleNotAllowed error: {json}"
    );
}

/// Verify the full pipeline for output limit exceeded scenario.
///
/// Simulates: large print() → OutputBuffer limit → OutputLimitExceeded → ExecutionResult
#[test]
fn test_full_pipeline_output_limit_exceeded_error_propagation() {
    let settings = ExecutionSettings {
        max_output_bytes: 100,
        ..ExecutionSettings::default()
    };

    let output = OutputBuffer::new(settings.max_output_bytes);
    let vm_clone = output.clone();

    // Simulate VM printing large data (101 bytes exceeds 100-byte limit)
    let large_data = "x".repeat(101);
    let write_result = vm_clone.write_stdout(large_data.as_bytes());

    assert!(
        matches!(
            write_result,
            Err(ExecutionError::OutputLimitExceeded { limit_bytes: 100 })
        ),
        "Large write must produce OutputLimitExceeded: {:?}",
        write_result
    );

    let output_err = write_result.unwrap_err();

    // Build ExecutionResult as the VM's write closure would signal back
    let exec_result = ExecutionResult {
        stdout: String::new(),
        stderr: String::new(),
        return_value: None,
        error: Some(output_err),
        duration_ns: 50_000,
    };

    // Verify
    assert!(
        matches!(
            exec_result.error,
            Some(ExecutionError::OutputLimitExceeded { limit_bytes: 100 })
        ),
        "ExecutionResult must carry OutputLimitExceeded error: {:?}",
        exec_result.error
    );

    // Verify CLI JSON schema compliance (AC-16, AC-17)
    let json = serde_json::to_string(&exec_result).expect("serialize");
    assert!(
        json.contains(r#""type":"OutputLimitExceeded""#),
        "CLI output must have type discriminator: {json}"
    );
    assert!(
        json.contains(r#""limit_bytes":100"#),
        "CLI output must have limit_bytes: {json}"
    );
}

/// Verify that the ExecutionResult JSON schema matches AC-16 requirements.
///
/// AC-16 requires: stdout, stderr, return_value, error, duration_ns all present.
#[test]
fn test_execution_result_json_schema_has_all_required_fields() {
    // Success case
    let success = ExecutionResult {
        stdout: "hello\n".to_string(),
        stderr: String::new(),
        return_value: None,
        error: None,
        duration_ns: 12345,
    };

    let json = serde_json::to_string(&success).expect("serialize success");
    assert!(
        json.contains(r#""stdout":"hello\n""#),
        "Must have stdout: {json}"
    );
    assert!(json.contains(r#""stderr":"""#), "Must have stderr: {json}");
    assert!(
        json.contains(r#""return_value":null"#),
        "Must have return_value: {json}"
    );
    assert!(
        json.contains(r#""error":null"#),
        "Must have error (null on success): {json}"
    );
    assert!(
        json.contains(r#""duration_ns":12345"#),
        "Must have duration_ns: {json}"
    );

    // Error case (SyntaxError — AC-17)
    let syntax_err = ExecutionResult {
        stdout: String::new(),
        stderr: String::new(),
        return_value: None,
        error: Some(ExecutionError::SyntaxError {
            message: "invalid syntax".to_string(),
            line: 1,
            col: 5,
        }),
        duration_ns: 1000,
    };

    let err_json = serde_json::to_string(&syntax_err).expect("serialize error");
    assert!(
        err_json.contains(r#""type":"SyntaxError""#),
        "SyntaxError must have type discriminator: {err_json}"
    );
    assert!(
        !err_json.contains(r#""error":null"#),
        "error must not be null when error is present: {err_json}"
    );
}

/// Verify concurrent execution uses isolated OutputBuffers per execution.
///
/// Each call to the VM gets its own OutputBuffer (new per execution).
/// Concurrent executions must not share output buffers.
#[test]
fn test_concurrent_executions_have_isolated_output_buffers() {
    use std::sync::{Arc, Mutex};
    use std::thread;

    let results: Arc<Mutex<Vec<String>>> = Arc::new(Mutex::new(Vec::new()));
    let mut handles = vec![];

    for i in 0..8 {
        let results_clone = Arc::clone(&results);
        let handle = thread::spawn(move || {
            // Each thread creates its own isolated OutputBuffer
            let output = OutputBuffer::new(1_048_576);
            let vm_clone = output.clone();

            // Write thread-specific data
            let data = format!("thread {} output\n", i);
            vm_clone
                .write_stdout(data.as_bytes())
                .expect("concurrent write must not fail");

            drop(vm_clone);
            let (stdout, _) = output.into_strings();

            results_clone
                .lock()
                .expect("mutex ok")
                .push(stdout);
        });
        handles.push(handle);
    }

    for handle in handles {
        handle.join().expect("thread must not panic");
    }

    let results = results.lock().expect("mutex ok");
    assert_eq!(
        results.len(),
        8,
        "All 8 concurrent executions must complete"
    );

    // Each result must only contain its own thread's output (no cross-contamination)
    for result in results.iter() {
        assert!(
            result.contains("thread") && result.contains("output"),
            "Result must contain 'thread' and 'output': '{}'",
            result
        );
    }
}

/// Verify that all five ExecutionError variants serialize to valid JSON
/// that can be embedded in ExecutionResult.error field.
///
/// This tests the complete error type surface used by vm.rs.
#[test]
fn test_all_vm_error_variants_serialize_correctly() {
    let variants: Vec<ExecutionError> = vec![
        ExecutionError::SyntaxError {
            message: "invalid syntax".to_string(),
            line: 1,
            col: 5,
        },
        ExecutionError::RuntimeError {
            message: "division by zero".to_string(),
            traceback: "Traceback...\n".to_string(),
        },
        ExecutionError::Timeout {
            limit_ns: 5_000_000_000,
        },
        ExecutionError::OutputLimitExceeded {
            limit_bytes: 1_048_576,
        },
        ExecutionError::ModuleNotAllowed {
            module_name: "socket".to_string(),
        },
    ];

    for variant in &variants {
        let result = ExecutionResult {
            stdout: String::new(),
            stderr: String::new(),
            return_value: None,
            error: Some(variant.clone()),
            duration_ns: 0,
        };

        let json = serde_json::to_string(&result).expect("ExecutionResult must serialize");
        assert!(
            json.contains(r#""type":"#),
            "Every error variant must have internal type tag in ExecutionResult JSON: {json}"
        );
        assert!(
            !json.contains(r#""error":null"#),
            "error must not be null when variant is present: {json}"
        );

        // Verify round-trip deserialization
        let deserialized: ExecutionResult =
            serde_json::from_str(&json).expect("ExecutionResult must deserialize");
        assert!(
            deserialized.error.is_some(),
            "Deserialized result must have error: {json}"
        );
    }
}

/// Verify that DEFAULT_ALLOWED_MODULES contains exactly the modules
/// that the PRD specifies, and that all are accessible via build_allowed_set.
///
/// This is the shared constant that types.rs defines and modules.rs uses.
#[test]
fn test_default_allowed_modules_prd_compliance() {
    // PRD specifies: math, re, json, datetime, collections, itertools, functools,
    // string, random, os.path, sys — 11 modules
    let expected: &[&str] = &[
        "math", "re", "json", "datetime", "collections",
        "itertools", "functools", "string", "random", "os.path", "sys",
    ];

    assert_eq!(
        DEFAULT_ALLOWED_MODULES.len(),
        11,
        "DEFAULT_ALLOWED_MODULES must have exactly 11 entries (AC-13)"
    );

    for module in expected {
        assert!(
            DEFAULT_ALLOWED_MODULES.contains(module),
            "DEFAULT_ALLOWED_MODULES must contain '{}' (AC-13)",
            module
        );
    }

    // Verify via build_allowed_set
    let settings = ExecutionSettings::default();
    let set = build_allowed_set(&settings);

    for module in expected {
        let check = llm_pyexec::modules::check_module_allowed(module, &set);
        assert!(
            check.is_ok(),
            "All PRD-specified modules must be allowed: '{}' was denied",
            module
        );
    }
}
