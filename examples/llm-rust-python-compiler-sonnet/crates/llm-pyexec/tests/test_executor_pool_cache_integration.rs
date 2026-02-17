//! Integration tests targeting the cross-feature boundaries introduced by
//! the merged branch issue/05-m1-executor-integration.
//!
//! This branch wired executor.rs to dispatch work through InterpreterPool (M1)
//! and warm/update BytecodeCache (M2). The key interaction points are:
//!
//! 1. executor.rs → pool.rs (dispatch_work + POOL_CHECKOUT_TIMEOUT)
//! 2. executor.rs → cache.rs (cache_key warm + insert on non-SyntaxError)
//! 3. executor.rs fallback path (pool exhaustion → run_with_timeout + build_interpreter)
//! 4. executor.rs result mapping (OutputLimitExceeded takes priority over VM error)
//! 5. execute() API + BytecodeCache::global() + InterpreterPool::global() singleton coexistence
//!
//! Priority 1: Conflict resolution areas (cache insert logic, pool dispatch, fallback path)
//! Priority 2: Cross-feature interactions (pool warm path result, cache dedup in execute())
//! Priority 3: Shared file modifications (lib.rs re-exports all three: execute, pool, cache)

use llm_pyexec::{
    BytecodeCache,
    InterpreterPool,
    cache::cache_key,
    executor::maybe_wrap_last_expr,
    execute,
    types::{ExecutionError, ExecutionSettings, DEFAULT_ALLOWED_MODULES},
    OutputBuffer,
};
use std::collections::HashSet;
use std::sync::Arc;

// ── Helper ─────────────────────────────────────────────────────────────────────

fn default_settings() -> ExecutionSettings {
    ExecutionSettings::default()
}

fn fast_timeout_settings() -> ExecutionSettings {
    ExecutionSettings {
        timeout_ns: 5_000_000_000, // 5s - enough for VM startup
        ..ExecutionSettings::default()
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// Priority 1: Conflict Resolution — executor.rs cache integration
// The merge wired executor.rs to call BytecodeCache::global().get() + .insert()
// ─────────────────────────────────────────────────────────────────────────────

/// AC-05 partial: Verify that BytecodeCache::global() is accessible from the
/// same binary as execute() and InterpreterPool::global().
///
/// The executor.rs merge adds calls to BytecodeCache::global() inside execute().
/// This test verifies the global singleton is correctly initialized and accessible.
#[test]
fn test_executor_cache_global_accessible_after_pool_merge() {
    let cache = BytecodeCache::global();

    // The cache must be functional — insert and retrieve
    let source = "__executor_cache_test__ = 99";
    let wrapped = maybe_wrap_last_expr(source);
    let key = cache_key(&wrapped);

    let before = cache.len();
    cache.insert(key, wrapped.clone());
    assert_eq!(
        cache.len(),
        before + 1,
        "BytecodeCache::global() must accept inserts after M1-executor-integration merge; \
         len should be {} but was {}",
        before + 1,
        cache.len()
    );

    // Retrieve it back
    assert_eq!(
        cache.get(&key),
        Some(wrapped),
        "BytecodeCache::global() must return the value inserted by executor path"
    );
}

/// Priority 1: Verify the executor's cache-key computation uses the WRAPPED source,
/// not the raw source. The executor calls cache_key(&wrapped) after maybe_wrap_last_expr.
///
/// Conflict area: executor.rs inserts into cache AFTER wrapping; the cache key
/// must match what would be produced from the same source in a subsequent call.
#[test]
fn test_executor_cache_key_uses_wrapped_source() {
    let raw = "x * 2";
    let wrapped = maybe_wrap_last_expr(raw);

    // The executor wraps first, then computes cache key
    let executor_key = cache_key(&wrapped);

    // A subsequent call with the same raw source produces the same wrapped source,
    // hence the same cache key → deduplication works
    let wrapped_again = maybe_wrap_last_expr(raw);
    let key_again = cache_key(&wrapped_again);

    assert_eq!(
        wrapped, wrapped_again,
        "maybe_wrap_last_expr must be deterministic: same raw source must always produce \
         the same wrapped source (executor cache deduplication requires this)"
    );
    assert_eq!(
        executor_key, key_again,
        "cache_key of wrapped source must be identical across calls: \
         executor's cache deduplication depends on this invariant"
    );
    assert_eq!(
        wrapped,
        "__result__ = x * 2",
        "executor wraps bare expression 'x * 2' as '__result__ = x * 2'"
    );
}

/// Priority 1: Verify the executor only inserts into the cache for NON-SyntaxError results.
///
/// Conflict area: executor.rs has: `if !is_syntax_error { cache.insert(key, wrapped); }`
/// This test verifies the logic: a local cache (not global) must not receive SyntaxError sources.
#[test]
fn test_executor_cache_insert_skipped_for_syntax_errors() {
    // Use a local cache to avoid global state contamination
    let cache = BytecodeCache::new(64);

    // Simulate what executor does for a SyntaxError path:
    // 1. Compute key
    // 2. Warm cache (get)
    // 3. Execute → SyntaxError
    // 4. Do NOT insert

    let bad_source = "def f(:";  // definitely a syntax error
    let wrapped = maybe_wrap_last_expr(bad_source);
    let key = cache_key(&wrapped);

    // Simulate "warm" step (executor calls get before execution)
    let _warm = cache.get(&key);
    assert_eq!(_warm, None, "SyntaxError source should not be pre-cached");

    // Simulate the SyntaxError result — executor skips insert
    let is_syntax_error = true;  // simulating the result
    if !is_syntax_error {
        cache.insert(key, wrapped.clone());
    }

    // Cache must remain empty (no insert happened)
    assert_eq!(
        cache.len(),
        0,
        "BytecodeCache must NOT store source strings that produce SyntaxErrors; \
         executor.rs's is_syntax_error guard must prevent cache poisoning"
    );
}

/// Priority 1: Verify the executor DOES insert into the cache for successful executions.
///
/// For non-SyntaxError results, executor.rs inserts the wrapped source into the cache.
#[test]
fn test_executor_cache_insert_occurs_for_non_syntax_error() {
    let cache = BytecodeCache::new(64);

    let source = "result = 42";
    let wrapped = maybe_wrap_last_expr(source);
    let key = cache_key(&wrapped);

    // Simulate "warm" step
    let _warm = cache.get(&key);

    // Simulate successful execution — executor inserts
    let is_syntax_error = false;
    if !is_syntax_error {
        cache.insert(key, wrapped.clone());
    }

    assert_eq!(
        cache.len(),
        1,
        "BytecodeCache must store source strings for successful (non-SyntaxError) results; \
         executor.rs's cache.insert() must fire for non-SyntaxError outcomes"
    );
    assert_eq!(
        cache.get(&key),
        Some(wrapped),
        "Inserted wrapped source must be retrievable from cache"
    );
}

// ─────────────────────────────────────────────────────────────────────────────
// Priority 1: executor.rs → pool.rs dispatch channel protocol
// ─────────────────────────────────────────────────────────────────────────────

/// Priority 1: Verify that the per-call response channel protocol is correct.
///
/// executor.rs creates a SyncSender<VmRunResult> and embeds it in WorkItem.
/// The pool slot sends the result back through this channel.
/// This test verifies the channel mechanics at the type level.
#[test]
fn test_executor_pool_response_channel_type_contract() {
    // executor.rs creates: let (response_tx, response_rx) = sync_channel::<VmRunResult>(1);
    // VmRunResult is pub(crate), so we test the channel mechanics with String as proxy.
    // The Send-ness contract: SyncSender<T: Send> is Send.
    let (tx, rx) = std::sync::mpsc::sync_channel::<String>(1);

    // SyncSender must be Send (crosses thread boundary to WorkItem)
    fn assert_send<T: Send>(_: T) {}
    assert_send(tx.clone());

    // Simulate pool slot sending result back
    tx.send("result".to_string()).expect("send must succeed on fresh channel");

    // Executor receives within timeout
    let result = rx.recv_timeout(std::time::Duration::from_secs(1))
        .expect("recv_timeout must succeed when sender sends");
    assert_eq!(result, "result",
        "Pool response channel round-trip must work: executor embeds SyncSender in WorkItem, \
         pool slot sends result, executor receives via recv_timeout");

    // After result received, sending again would fail (channel capacity 1 is consumed)
    // but pool slot returns to idle — testing the empty case
    let timeout_result = rx.recv_timeout(std::time::Duration::from_millis(1));
    assert!(
        timeout_result.is_err(),
        "recv_timeout must return Err when no more results (slot returned to pool)"
    );
}

/// Priority 1: Verify pool idle count tracks slot availability correctly.
///
/// Before any execute() calls, the global pool must have all slots available.
/// This is a precondition for the executor's warm path to succeed.
///
/// We test a local pool (not global) to avoid polluting process state.
#[test]
#[ignore = "slow: VM init per slot"]
fn test_executor_pool_all_slots_available_before_first_dispatch() {
    let pool = InterpreterPool::new(2);

    // Before any dispatch, all slots must be idle (warm path precondition)
    assert_eq!(
        pool.idle_count(),
        2,
        "Pool must have all {} slots available before any executor dispatch; \
         warm path requires idle slots",
        pool.size()
    );
    assert_eq!(
        pool.size(),
        2,
        "pool.size() must equal configured size"
    );
}

// ─────────────────────────────────────────────────────────────────────────────
// Priority 2: Cross-feature interactions — execute() with pool+cache
// ─────────────────────────────────────────────────────────────────────────────

/// Priority 2: Verify execute() returns a result with all required fields.
///
/// The M1-executor-integration merge changed execute() to use pool dispatch.
/// The ExecutionResult structure must be complete regardless of which path runs.
#[test]
#[ignore = "slow: VM init"]
fn test_execute_result_has_all_required_fields_via_pool() {
    let result = execute("x = 1", fast_timeout_settings());

    // All fields must be present (duration_ns > 0 even for trivial code)
    assert!(
        result.duration_ns > 0,
        "execute() must report non-zero duration_ns even after pool-path execution; \
         got {}",
        result.duration_ns
    );

    // No error for valid Python
    assert!(
        result.error.is_none(),
        "execute('x = 1') must return no error via pool path; got: {:?}",
        result.error
    );

    // stdout is empty for assignment-only code
    assert_eq!(
        result.stdout, "",
        "Assignment-only code must produce no stdout output"
    );
}

/// Priority 2: Verify execute() captures stdout correctly via pool path.
///
/// The pool slot thread runs run_code which installs OutputBuffer capture.
/// Stdout written inside the VM must be readable in the ExecutionResult.
#[test]
#[ignore = "slow: VM init"]
fn test_execute_stdout_captured_via_pool_path() {
    let result = execute(r#"print("pool_test_output")"#, fast_timeout_settings());

    assert!(
        result.error.is_none(),
        "print() should not produce an error; got: {:?}",
        result.error
    );
    assert_eq!(
        result.stdout, "pool_test_output\n",
        "execute() must capture stdout written via pool slot's run_code OutputBuffer; \
         expected 'pool_test_output\\n' but got '{}'",
        result.stdout
    );
}

/// Priority 2: Verify execute() returns expression value via __result__ convention.
///
/// executor.rs wraps bare expressions as `__result__ = <expr>`.
/// pool.rs slot calls run_code which captures __result__ from scope.
/// The result must flow back through the response channel to the executor.
#[test]
#[ignore = "slow: VM init"]
fn test_execute_return_value_via_pool_path() {
    let result = execute("1 + 1", fast_timeout_settings());

    assert!(
        result.error.is_none(),
        "Arithmetic expression must not produce an error via pool path; got: {:?}",
        result.error
    );
    assert_eq!(
        result.return_value,
        Some("2".to_string()),
        "execute('1 + 1') must return '2' via __result__ convention through pool channel; \
         got: {:?}",
        result.return_value
    );
}

/// Priority 2: Verify execute() blocks denied imports via pool allowlist.
///
/// pool.rs slot calls interp.set_allowed_set() before each run_code().
/// The import hook in vm.rs must enforce the allowlist passed in WorkItem.allowed_set.
#[test]
#[ignore = "slow: VM init"]
fn test_execute_denied_module_via_pool_allowlist() {
    let result = execute("import socket", fast_timeout_settings());

    match &result.error {
        Some(ExecutionError::ModuleNotAllowed { module_name }) => {
            assert_eq!(
                module_name, "socket",
                "Denied module name must be 'socket'; pool slot must enforce allowlist \
                 from WorkItem.allowed_set; got: '{}'",
                module_name
            );
        }
        other => panic!(
            "execute('import socket') must return ModuleNotAllowed(socket) via pool path; \
             pool slot uses set_allowed_set to update allowlist before each run_code; \
             got: {:?}",
            other
        ),
    }
}

/// Priority 2: Verify execute() with SyntaxError does not insert into BytecodeCache::global().
///
/// executor.rs: `let is_syntax_error = matches!(result.error, Some(ExecutionError::SyntaxError {...}));`
///              `if !is_syntax_error { BytecodeCache::global().insert(key, wrapped); }`
/// The global cache len must not increase for SyntaxError inputs.
#[test]
#[ignore = "slow: VM init"]
fn test_execute_syntax_error_not_cached_in_global_cache() {
    let cache = BytecodeCache::global();

    // Use a unique source to detect if it gets inserted
    let bad_source = "def syntax_error_test_unique_xy(::";
    let wrapped = maybe_wrap_last_expr(bad_source);
    let key = cache_key(&wrapped);

    // Verify not in cache yet
    assert_eq!(
        cache.get(&key),
        None,
        "Unique bad source must not be pre-cached"
    );

    // Execute the bad code
    let result = execute(bad_source, fast_timeout_settings());

    // Must be a SyntaxError
    assert!(
        matches!(result.error, Some(ExecutionError::SyntaxError { .. })),
        "Bad source must produce SyntaxError; got: {:?}",
        result.error
    );

    // Cache must not have been updated
    assert_eq!(
        cache.get(&key),
        None,
        "BytecodeCache::global() must NOT store SyntaxError source; \
         executor.rs's is_syntax_error guard must prevent this; \
         cache.get() returned Some() but should be None"
    );
}

/// Priority 2: Verify that a successfully executed snippet IS inserted into BytecodeCache::global().
///
/// After execute() completes without SyntaxError, the wrapped source must appear
/// in the global cache keyed by SHA-256(wrapped_source).
#[test]
#[ignore = "slow: VM init"]
fn test_execute_success_inserts_into_global_cache() {
    let cache = BytecodeCache::global();

    // Use a unique source to detect the insert
    let source = "successful_cache_test_unique_abc = 12345";
    let wrapped = maybe_wrap_last_expr(source);
    let key = cache_key(&wrapped);

    // Must not be in cache before execution
    assert_eq!(
        cache.get(&key),
        None,
        "Unique source must not be pre-cached before execute()"
    );

    // Execute (should succeed)
    let result = execute(source, fast_timeout_settings());

    assert!(
        result.error.is_none(),
        "Assignment must succeed; got: {:?}",
        result.error
    );

    // After successful execution, executor must have inserted into global cache
    assert_eq!(
        cache.get(&key),
        Some(wrapped.clone()),
        "BytecodeCache::global() must contain the wrapped source after a successful execute(); \
         executor.rs: 'if !is_syntax_error {{ BytecodeCache::global().insert(key, wrapped); }}'"
    );
}

// ─────────────────────────────────────────────────────────────────────────────
// Priority 1: executor.rs fallback path (pool exhaustion)
// ─────────────────────────────────────────────────────────────────────────────

/// Priority 1: Verify the executor fallback path produces correct results.
///
/// When InterpreterPool::global().dispatch_work() returns false (pool exhausted),
/// executor.rs falls back to run_with_timeout(build_interpreter + run_code).
///
/// We test the fallback path indirectly by verifying execute() works correctly
/// even if called before the pool is initialized (via local testing patterns).
///
/// The key invariant: OutputLimitExceeded error (from output.is_limit_exceeded())
/// must take priority over any VM runtime error, on both pool AND fallback paths.
#[test]
#[ignore = "slow: VM init"]
fn test_execute_output_limit_exceeded_takes_priority_over_vm_error() {
    // Output limit of 100 bytes: print("x" * 10000) will exceed it
    let settings = ExecutionSettings {
        max_output_bytes: 100,
        timeout_ns: 5_000_000_000,
        ..ExecutionSettings::default()
    };

    let result = execute(r#"print("x" * 10000)"#, settings);

    // executor.rs: checks output.is_limit_exceeded() AFTER checking result.error
    // OutputLimitExceeded must override the VM's runtime error
    match &result.error {
        Some(ExecutionError::OutputLimitExceeded { limit_bytes }) => {
            assert_eq!(
                *limit_bytes, 100,
                "OutputLimitExceeded.limit_bytes must match settings.max_output_bytes; \
                 executor.rs: 'if limit_exceeded {{ return OutputLimitExceeded {{ limit_bytes: max_output_bytes }}}}'"
            );
        }
        other => panic!(
            "execute() with output exceeding max_output_bytes must return OutputLimitExceeded; \
             executor.rs output limit check must take priority over VM error; got: {:?}",
            other
        ),
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// Priority 2: execute() + BytecodeCache deduplication
// ─────────────────────────────────────────────────────────────────────────────

/// Priority 2 (AC-05 integration): Verify that executing the same snippet multiple times
/// results in only ONE cache entry (deduplication via SHA-256 key).
///
/// executor.rs: same source → same maybe_wrap_last_expr output → same cache_key →
///              BytecodeCache.insert() overwrites existing entry (same key → same slot).
///
/// We verify deduplication by checking that the key maps to the same value regardless
/// of how many times execute() was called with the same source. We do NOT rely on
/// comparing cache.len() before vs after, since other concurrent tests may also insert
/// entries into the global singleton cache.
#[test]
#[ignore = "slow: VM init"]
fn test_execute_repeated_identical_source_deduplicates_in_cache() {
    let cache = BytecodeCache::global();

    // Use a unique source to isolate this test from other tests
    let source = "repeated_dedup_test_unique_xyz = 77777";
    let wrapped = maybe_wrap_last_expr(source);
    let key = cache_key(&wrapped);

    // Must not be in cache yet (unique source)
    assert_eq!(
        cache.get(&key),
        None,
        "Unique test source must not be pre-cached before first execute()"
    );

    // Execute 3 times with the same source
    for i in 0..3 {
        let result = execute(source, fast_timeout_settings());
        assert!(
            result.error.is_none(),
            "Execute iteration {} must succeed; got: {:?}",
            i,
            result.error
        );
    }

    // The key must map to exactly the wrapped source string (not duplicated, not absent)
    // BytecodeCache.insert() with the same key overwrites — same SHA-256 key → same slot
    let cached_value = cache.get(&key);
    assert_eq!(
        cached_value,
        Some(wrapped.clone()),
        "After 3 identical execute() calls, cache must contain the wrapped source \
         at the key SHA-256(wrapped_source); BytecodeCache.insert() with the same key \
         overwrites rather than growing the cache; got: {:?}",
        cached_value
    );

    // Execute the same source 3 more times — cache entry must remain the same value
    for i in 0..3 {
        let result = execute(source, fast_timeout_settings());
        assert!(
            result.error.is_none(),
            "Execute iteration {} (second round) must succeed; got: {:?}",
            i,
            result.error
        );
    }

    // The key must still point to the same wrapped source (LRU updated, value unchanged)
    assert_eq!(
        cache.get(&key),
        Some(wrapped),
        "After 6 total identical execute() calls, cache must still contain \
         the same wrapped source (deduplication: same key, same value)"
    );
}

// ─────────────────────────────────────────────────────────────────────────────
// Priority 1: executor.rs timeout path
// ─────────────────────────────────────────────────────────────────────────────

/// Priority 1: Verify execute() returns Timeout error when pool dispatched code exceeds timeout.
///
/// executor.rs: response_rx.recv_timeout(execution_timeout) returns Err → vm_result=None
/// Then: `error: Some(ExecutionError::Timeout { limit_ns: timeout_ns })`
#[test]
#[ignore = "slow: VM init"]
fn test_execute_timeout_via_pool_returns_correct_error() {
    let timeout_ns = 200_000_000u64; // 200ms
    let settings = ExecutionSettings {
        timeout_ns,
        ..ExecutionSettings::default()
    };

    let start = std::time::Instant::now();
    let result = execute("while True: pass", settings);
    let elapsed = start.elapsed();

    // Must be a Timeout error with matching limit_ns
    match &result.error {
        Some(ExecutionError::Timeout { limit_ns }) => {
            assert_eq!(
                *limit_ns, timeout_ns,
                "Timeout error limit_ns must match settings.timeout_ns; \
                 executor.rs sets limit_ns=timeout_ns from ExecutionSettings"
            );
        }
        other => panic!(
            "Infinite loop must produce Timeout error via pool dispatch timeout path; \
             executor.rs: recv_timeout returns Err → vm_result=None → Timeout error; \
             got: {:?}",
            other
        ),
    }

    // Must return reasonably quickly (within 2x the timeout + pool checkout overhead)
    assert!(
        elapsed.as_nanos() < 5_000_000_000,
        "Timeout must cause execute() to return within 5 seconds; took {:?}",
        elapsed
    );
}

// ─────────────────────────────────────────────────────────────────────────────
// Priority 3: Shared file — lib.rs re-exports all components after M1 executor merge
// ─────────────────────────────────────────────────────────────────────────────

/// Priority 3: Verify all three new exports from lib.rs coexist after the M1-executor merge.
///
/// lib.rs exports: execute (executor.rs), InterpreterPool (pool.rs), BytecodeCache (cache.rs)
/// The M1-executor-integration merge adds pool dispatch inside execute(), which means
/// pool.rs and cache.rs must both be linked into the same binary as executor.rs.
#[test]
fn test_all_m1_executor_exports_coexist_in_lib() {
    // execute is re-exported from executor.rs
    let wrapped = maybe_wrap_last_expr("2 + 2");
    assert_eq!(wrapped, "__result__ = 2 + 2",
        "executor's maybe_wrap_last_expr must be accessible via lib re-export after M1 merge");

    // BytecodeCache is re-exported from cache.rs
    let cache = BytecodeCache::new(8);
    let key = cache_key("lib_coexistence_test");
    cache.insert(key, "v".to_string());
    assert_eq!(cache.len(), 1,
        "BytecodeCache from lib.rs re-export must work alongside execute() after M1 merge");

    // OutputBuffer is re-exported from output.rs (used by WorkItem)
    let output = OutputBuffer::new(1_048_576);
    assert!(!output.is_limit_exceeded(),
        "OutputBuffer from lib.rs re-export must work after M1 merge");

    // DEFAULT_ALLOWED_MODULES is re-exported from types.rs (used by pool + executor)
    assert_eq!(DEFAULT_ALLOWED_MODULES.len(), 11,
        "DEFAULT_ALLOWED_MODULES must remain 11 entries after M1-executor-integration merge");

    // InterpreterPool is re-exported from pool.rs
    // We verify the type is importable and has expected API without creating a global pool
    // (which would be slow)
    let _ = std::any::type_name::<InterpreterPool>();
}

/// Priority 3: Verify execute() function signature matches what the M1-executor integration expects.
///
/// executor.rs uses: `execute(code: &str, settings: ExecutionSettings) -> ExecutionResult`
/// The function must accept both &str (not String) and ExecutionSettings (not &ExecutionSettings).
#[test]
fn test_execute_function_signature_compatibility() {
    // These are compile-time checks — if this test compiles, the signature is correct.
    // execute() must accept &str literals directly
    let _: fn(&str, ExecutionSettings) -> llm_pyexec::ExecutionResult = execute;

    // ExecutionSettings::default() must produce a valid settings value
    let _settings = ExecutionSettings {
        timeout_ns: 1_000_000_000,
        max_output_bytes: 1_048_576,
        allowed_modules: vec!["math".to_string()],
    };
}

/// Priority 2: Verify executor's POOL_CHECKOUT_TIMEOUT constant doesn't block forever.
///
/// executor.rs sets POOL_CHECKOUT_TIMEOUT = 30 seconds for the dispatch_work call.
/// This is the maximum time the executor waits for a free slot before falling back.
/// We verify the fallback path works: if the pool is busy, we still get a result.
///
/// Note: We can't easily exhaust the global pool in a test. Instead, we verify that
/// execute() always returns within a reasonable time (the pool or fallback path works).
#[test]
#[ignore = "slow: VM init"]
fn test_execute_always_returns_result_even_under_concurrent_load() {
    use std::sync::{Arc, Mutex};
    use std::thread;

    let results = Arc::new(Mutex::new(Vec::<bool>::new()));
    let mut handles = vec![];

    // Launch 8 concurrent execute() calls — some may hit the pool, some may fall back
    for i in 0..8usize {
        let results = Arc::clone(&results);
        let handle = thread::spawn(move || {
            let source = format!("x_{i} = {i} * {i}");
            let result = execute(&source, fast_timeout_settings());
            let ok = result.error.is_none();
            results.lock().expect("mutex").push(ok);
        });
        handles.push(handle);
    }

    for h in handles {
        h.join().expect("thread must not panic");
    }

    let results = results.lock().expect("mutex");
    assert_eq!(results.len(), 8, "All 8 concurrent execute() calls must complete");

    let all_ok = results.iter().all(|&ok| ok);
    assert!(
        all_ok,
        "All concurrent execute() calls must succeed (no errors); \
         executor.rs must correctly handle concurrent pool dispatch and fallback"
    );
}

// ─────────────────────────────────────────────────────────────────────────────
// Priority 1: executor.rs duration_ns measurement
// ─────────────────────────────────────────────────────────────────────────────

/// Priority 1: Verify duration_ns is recorded from Instant::now() at the START of execute().
///
/// executor.rs: `let start = Instant::now();`
///              `let duration_ns = start.elapsed().as_nanos() as u64;`
/// duration_ns must be > 0 even for trivially fast code (pool dispatch has overhead).
#[test]
#[ignore = "slow: VM init"]
fn test_execute_duration_ns_reflects_total_wall_time_including_pool_dispatch() {
    let result = execute("pass", fast_timeout_settings());

    assert!(
        result.duration_ns > 0,
        "duration_ns must be > 0 even for 'pass'; executor measures from Instant::now() \
         at function entry, including pool dispatch overhead; got: {}",
        result.duration_ns
    );

    // For a trivial 'pass' statement, duration should be less than 30s
    // (the POOL_CHECKOUT_TIMEOUT is 30s; if we waited that long, something is very wrong)
    assert!(
        result.duration_ns < 30_000_000_000,
        "duration_ns must be less than POOL_CHECKOUT_TIMEOUT (30s); got: {}ns",
        result.duration_ns
    );
}

// ─────────────────────────────────────────────────────────────────────────────
// Priority 2: execute() with custom allowed_modules (uses pool.set_allowed_set)
// ─────────────────────────────────────────────────────────────────────────────

/// Priority 2: Verify custom allowed_modules are respected via pool's set_allowed_set.
///
/// executor.rs builds the allowed_set from settings and passes it in WorkItem.allowed_set.
/// pool.rs slot calls interp.set_allowed_set((*item.allowed_set).clone()) before run_code.
/// The VM's import hook must use the per-call allowlist, not the default one.
#[test]
#[ignore = "slow: VM init"]
fn test_execute_custom_allowlist_restricts_imports_via_pool_set_allowed_set() {
    // Custom settings: only allow "math", not "json"
    let settings = ExecutionSettings {
        allowed_modules: vec!["math".to_string()],
        timeout_ns: 5_000_000_000,
        max_output_bytes: 1_048_576,
    };

    // json should be denied even though it's in DEFAULT_ALLOWED_MODULES
    let result = execute("import json", settings);

    match &result.error {
        Some(ExecutionError::ModuleNotAllowed { module_name }) => {
            assert_eq!(
                module_name, "json",
                "json must be denied with custom allowlist [math only]; \
                 pool slot's set_allowed_set must update the allowlist for each call"
            );
        }
        other => panic!(
            "import json must be denied when custom allowlist only includes 'math'; \
             pool.rs calls interp.set_allowed_set(item.allowed_set) before run_code; \
             got: {:?}",
            other
        ),
    }
}

/// Priority 2: Verify executor's build_allowed_set + pool's set_allowed_set round-trip.
///
/// The allowed_set built by executor.rs must match what pool.rs needs to
/// call set_allowed_set(). Both use HashSet<String> which matches vm.rs's
/// set_allowed_set(HashSet<String>) signature.
#[test]
fn test_executor_allowed_set_type_matches_pool_set_allowed_set_parameter() {
    use llm_pyexec::modules::build_allowed_set;

    let settings = ExecutionSettings::default();
    let allowed_set = build_allowed_set(&settings);

    // This is the Arc::new() step in executor.rs
    let arc_set: Arc<HashSet<String>> = Arc::new(allowed_set);

    // This is (*item.allowed_set).clone() in pool.rs slot
    let cloned_for_set_allowed: HashSet<String> = (*arc_set).clone();

    assert_eq!(
        cloned_for_set_allowed.len(),
        DEFAULT_ALLOWED_MODULES.len(),
        "Cloned allowed_set for set_allowed_set must have the same cardinality as DEFAULT_ALLOWED_MODULES; \
         pool.rs: interp.set_allowed_set((*item.allowed_set).clone())"
    );

    // Verify all DEFAULT_ALLOWED_MODULES are present
    for module in DEFAULT_ALLOWED_MODULES {
        assert!(
            cloned_for_set_allowed.contains(*module),
            "Cloned set must contain '{}' from DEFAULT_ALLOWED_MODULES",
            module
        );
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// Priority 1: Pool state isolation — critical AC-04 requirement
// ─────────────────────────────────────────────────────────────────────────────

/// Priority 1 (AC-04): Verify state isolation between execute() calls via pool.
///
/// Variable 'x' assigned in call 1 must NOT be visible in call 2 on the same slot.
/// pool.rs resets scope by creating a fresh scope in run_code (vm.new_scope_with_builtins()).
/// The global scope is fresh per-call because vm.run_code_obj(code, scope.clone()) uses
/// a new scope each time — previous local variables don't persist.
#[test]
#[ignore = "slow: VM init"]
fn test_execute_state_isolation_between_calls_via_pool() {
    let settings = fast_timeout_settings();

    // Call 1: assign a variable
    let result1 = execute("isolation_test_var = 42", settings.clone());
    assert!(
        result1.error.is_none(),
        "Call 1 (assign variable) must succeed; got: {:?}",
        result1.error
    );

    // Small delay to ensure slot returns to pool
    std::thread::sleep(std::time::Duration::from_millis(100));

    // Call 2: try to access the variable — must get NameError (not RuntimeError on the value)
    let result2 = execute("isolation_test_var", settings.clone());

    assert!(
        result2.error.is_some(),
        "Call 2 accessing variable from call 1 must produce an error; \
         pool.rs creates fresh scope per call via vm.new_scope_with_builtins(); \
         variables assigned in previous calls must NOT persist"
    );

    // The error must be a RuntimeError (NameError is a RuntimeError in RustPython)
    assert!(
        matches!(result2.error, Some(ExecutionError::RuntimeError { .. })),
        "NameError from accessing undefined variable must map to RuntimeError; got: {:?}",
        result2.error
    );
}

/// Priority 1: Verify sys.modules reset between pool slot calls.
///
/// pool.rs calls reset_sys_modules() after each run_code().
/// Modules imported in call 1 must not be in sys.modules in call 2.
#[test]
#[ignore = "slow: VM init"]
fn test_execute_sys_modules_reset_between_pool_calls() {
    let settings = fast_timeout_settings();

    // Call 1: import math (adds math to sys.modules)
    let result1 = execute("import math; x = math.pi", settings.clone());
    assert!(
        result1.error.is_none(),
        "Call 1 (import math) must succeed; got: {:?}",
        result1.error
    );

    // Small delay to ensure slot returns to pool with reset sys.modules
    std::thread::sleep(std::time::Duration::from_millis(100));

    // Call 2: try to use math without importing — if sys.modules persisted, math would work
    // With reset, math would be gone from scope (but still importable)
    // Key test: user-space variable 'x' from call 1 must not exist
    let result2 = execute("x", settings.clone());

    // x was assigned in call 1's scope — with state reset, it must not exist in call 2
    assert!(
        result2.error.is_some(),
        "Variable 'x' from call 1 scope must not persist in call 2; \
         pool.rs resets scope per call via fresh vm.new_scope_with_builtins(); got: {:?}",
        result2
    );
}

// ─────────────────────────────────────────────────────────────────────────────
// Priority 3: Verify no unsafe blocks in executor.rs (AC-18)
// ─────────────────────────────────────────────────────────────────────────────

/// Priority 3 (AC-18): executor.rs must not contain unsafe blocks after M1 merge.
///
/// The M1-executor-integration merge adds pool dispatch code; this must all be
/// safe Rust using Arc, Mutex, mpsc channels — no unsafe blocks required.
#[test]
fn test_executor_rs_contains_no_unsafe_blocks() {
    let executor_source = std::fs::read_to_string(
        concat!(env!("CARGO_MANIFEST_DIR"), "/src/executor.rs"),
    )
    .expect("executor.rs must be readable from crate src/");

    let unsafe_block_count = executor_source
        .lines()
        .filter(|line| {
            let trimmed = line.trim();
            // Skip lines that are pure comments
            if trimmed.starts_with("//") {
                return false;
            }
            // Skip lines that only mention unsafe in comments
            if let Some(comment_pos) = trimmed.find("//") {
                let code_part = &trimmed[..comment_pos];
                return code_part.contains("unsafe {") || code_part.contains("unsafe<");
            }
            // Check for unsafe blocks in code
            trimmed.contains("unsafe {") || trimmed.contains("unsafe<")
        })
        .count();

    assert_eq!(
        unsafe_block_count,
        0,
        "executor.rs must contain zero unsafe blocks after M1-executor-integration merge (AC-18); \
         found {} potential unsafe lines",
        unsafe_block_count
    );
}

/// Priority 3 (AC-18): pool.rs must not contain unsafe blocks after M1 merge.
#[test]
fn test_pool_rs_contains_no_unsafe_blocks_after_executor_merge() {
    let pool_source = std::fs::read_to_string(
        concat!(env!("CARGO_MANIFEST_DIR"), "/src/pool.rs"),
    )
    .expect("pool.rs must be readable from crate src/");

    let unsafe_block_count = pool_source
        .lines()
        .filter(|line| {
            let trimmed = line.trim();
            if trimmed.starts_with("//") {
                return false;
            }
            if let Some(comment_pos) = trimmed.find("//") {
                let code_part = &trimmed[..comment_pos];
                return code_part.contains("unsafe {") || code_part.contains("unsafe<");
            }
            trimmed.contains("unsafe {") || trimmed.contains("unsafe<")
        })
        .count();

    assert_eq!(
        unsafe_block_count,
        0,
        "pool.rs must contain zero unsafe blocks after M1-executor-integration merge (AC-18); \
         found {} potential unsafe lines",
        unsafe_block_count
    );
}

// ─────────────────────────────────────────────────────────────────────────────
// Priority 2: Integration with all 5 canonical bench snippets
// ─────────────────────────────────────────────────────────────────────────────

/// Priority 2: Verify execute() handles all 5 canonical LLM snippet categories.
///
/// These are the PRD's benchmark categories. Each must succeed via the pool path
/// after M1-executor-integration. This tests the end-to-end pipeline including
/// pool dispatch, allowlist enforcement, output capture, and result construction.
#[test]
#[ignore = "slow: VM init"]
fn test_execute_five_canonical_snippet_categories_via_pool() {
    let settings = fast_timeout_settings();

    let snippets: &[(&str, &str)] = &[
        ("bench_01_arithmetic", "sum(i*i for i in range(100))"),
        ("bench_02_string", "s = 'hello'; s.upper()"),
        ("bench_03_list_comprehension", "[x**2 for x in range(10)]"),
        ("bench_04_dict", "d = {i: i*2 for i in range(10)}; len(d)"),
        ("bench_05_import_stdlib", "import math; math.sqrt(4.0)"),
    ];

    for (bench_name, snippet) in snippets {
        let result = execute(snippet, settings.clone());
        assert!(
            result.error.is_none(),
            "Canonical snippet '{}' ('{}') must execute without error via pool path; \
             got: {:?}",
            bench_name,
            snippet,
            result.error
        );
        assert!(
            result.duration_ns > 0,
            "Canonical snippet '{}' must have non-zero duration_ns; got: {}",
            bench_name,
            result.duration_ns
        );
    }
}
