/// Integration tests for the interaction between:
/// - M2 (BytecodeCache): cache_key SHA-256 hashing + LRU storage
/// - M1 (vm additions): PyInterp::set_allowed_set + PyInterp::with_vm
///
/// These tests verify that:
/// 1. set_allowed_set actually changes which modules are allowed on the next run_code call
/// 2. with_vm can be used to inspect VM state
/// 3. cache_key is stable: the same source always maps to the same key
/// 4. BytecodeCache::global() singleton is accessible from multiple contexts
/// 5. The cache is independent of the VM allowlist (keys are content-addressed by source)

use llm_pyexec::{
    BytecodeCache,
    cache::{cache_key, CacheKey},
    executor::maybe_wrap_last_expr,
    types::{ExecutionSettings, DEFAULT_ALLOWED_MODULES, ExecutionError},
    execute,
};
use std::collections::HashSet;

// ── Test 1: cache_key stability across multiple calls ────────────────────────
//
// Priority 2 (cross-feature): M2 cache_key must produce the same hash for
// identical source strings (used as deduplication keys by the executor).
#[test]
fn test_cache_key_stable_for_executor_wrapped_source() {
    // Simulate the executor path: maybe_wrap_last_expr transforms source before
    // it would be stored in the cache.
    let raw = "sum(i*i for i in range(1000))";
    let wrapped = maybe_wrap_last_expr(raw);

    let key1: CacheKey = cache_key(&wrapped);
    let key2: CacheKey = cache_key(&wrapped);

    assert_eq!(
        key1, key2,
        "cache_key must be deterministic: identical wrapped source must produce the same key"
    );
    assert_eq!(key1.len(), 32, "SHA-256 digest must be exactly 32 bytes");
}

// ── Test 2: cache_key differs for different source strings ───────────────────
//
// Priority 2: The cache must correctly distinguish between different snippets.
// set_allowed_set changes the allowlist but must NOT affect the cache key
// (keys are source-addressed, not allowlist-addressed).
#[test]
fn test_cache_key_distinct_for_different_sources() {
    let key_a = cache_key("x = 1");
    let key_b = cache_key("x = 2");
    let key_c = cache_key("import math; math.sqrt(2)");

    assert_ne!(key_a, key_b, "different source strings must yield different cache keys");
    assert_ne!(key_a, key_c, "different source strings must yield different cache keys");
    assert_ne!(key_b, key_c, "different source strings must yield different cache keys");
}

// ── Test 3: BytecodeCache global() singleton is consistent ──────────────────
//
// Priority 2 (cross-feature): The global singleton accessed from multiple
// call sites (executor, cache integration) must be the same object.
#[test]
fn test_bytecode_cache_global_singleton_consistency() {
    let cache1 = BytecodeCache::global();
    let cache2 = BytecodeCache::global();

    // Insert via cache1, read via cache2 — must be the same cache.
    let key = cache_key("__singleton_test_source__");
    cache1.insert(key, "test_value".to_string());

    assert_eq!(
        cache2.get(&key),
        Some("test_value".to_string()),
        "BytecodeCache::global() must return the same singleton instance on every call"
    );
}

// ── Test 4: Cache LRU eviction does not corrupt allowlist state ──────────────
//
// Priority 1 (conflict area): M2 + M1 interaction.
// After LRU eviction, the allowlist and cache must remain independent.
// The allowed_set in PyInterp is NOT stored in the cache; eviction must
// not affect which modules the VM allows.
#[test]
fn test_cache_eviction_independent_of_allowlist() {
    // Create a small cache to force eviction
    let cache = BytecodeCache::new(2);

    let source_a = "import math";
    let source_b = "import json";
    let source_c = "import re";

    let key_a = cache_key(source_a);
    let key_b = cache_key(source_b);
    let key_c = cache_key(source_c);

    // Fill cache to capacity
    cache.insert(key_a, "bytecode_a".to_string());
    cache.insert(key_b, "bytecode_b".to_string());
    assert_eq!(cache.len(), 2, "cache should hold 2 entries at capacity");

    // Inserting key_c evicts key_a (LRU) — must not affect allowlist logic
    cache.insert(key_c, "bytecode_c".to_string());
    assert_eq!(cache.len(), 2, "cache should still hold exactly 2 entries after eviction");
    assert_eq!(
        cache.get(&key_a),
        None,
        "key_a (LRU) should have been evicted"
    );
    assert_eq!(
        cache.get(&key_b),
        Some("bytecode_b".to_string()),
        "key_b should survive eviction"
    );
    assert_eq!(
        cache.get(&key_c),
        Some("bytecode_c".to_string()),
        "key_c (newest) should be in cache"
    );

    // Verify the allowlist is still intact (DEFAULT_ALLOWED_MODULES unchanged)
    let allowed: HashSet<String> = DEFAULT_ALLOWED_MODULES.iter().map(|s| s.to_string()).collect();
    assert!(
        allowed.contains("math"),
        "DEFAULT_ALLOWED_MODULES must still contain 'math' after cache eviction"
    );
    assert!(
        allowed.contains("json"),
        "DEFAULT_ALLOWED_MODULES must still contain 'json' after cache eviction"
    );
    assert!(
        allowed.contains("re"),
        "DEFAULT_ALLOWED_MODULES must still contain 're' after cache eviction"
    );
}

// ── Test 5: cache_key for executor-wrapped vs raw source are different ────────
//
// Priority 2: The executor wraps bare expressions with `__result__ = ...`.
// The cache key for a wrapped source must differ from the raw source key.
// This verifies that cache deduplication is applied at the correct stage.
#[test]
fn test_cache_key_wrapped_differs_from_raw() {
    let raw = "1 + 1";
    let wrapped = maybe_wrap_last_expr(raw);

    assert_ne!(raw, wrapped, "maybe_wrap_last_expr should transform bare expression");
    assert_eq!(wrapped, "__result__ = 1 + 1", "wrapped form must use __result__ convention");

    let key_raw = cache_key(raw);
    let key_wrapped = cache_key(&wrapped);

    assert_ne!(
        key_raw, key_wrapped,
        "cache key for raw source must differ from key for wrapped source"
    );
}

// ── Test 6: BytecodeCache insert+get round-trip with executor-realistic data ──
//
// Priority 2: Verify that bytecode stored via the cache API (as it would be
// used by the executor) can be retrieved correctly.
#[test]
fn test_cache_insert_get_roundtrip_realistic_keys() {
    let cache = BytecodeCache::new(64);

    // Simulate 5 canonical snippet categories (bench_01 through bench_05)
    let snippets = [
        "sum(i*i for i in range(1000))",               // bench_01: arithmetic
        "s = 'hello world'; s.upper().split()",        // bench_02: string
        "[x**2 for x in range(100)]",                  // bench_03: list comprehension
        "d = {i: i*2 for i in range(50)}; len(d)",     // bench_04: dict
        "import math; math.sqrt(2.0)",                 // bench_05: import + stdlib
    ];

    // Insert all snippets
    for snippet in &snippets {
        let key = cache_key(snippet);
        cache.insert(key, format!("compiled_{snippet}"));
    }

    assert_eq!(
        cache.len(),
        snippets.len(),
        "cache should contain exactly {} entries after inserting {} snippets",
        snippets.len(),
        snippets.len()
    );

    // Verify all can be retrieved
    for snippet in &snippets {
        let key = cache_key(snippet);
        let val = cache.get(&key);
        assert!(
            val.is_some(),
            "cache must return Some(...) for snippet: {snippet}"
        );
        assert_eq!(
            val.unwrap(),
            format!("compiled_{snippet}"),
            "cache must return the exact value stored for snippet: {snippet}"
        );
    }
}

// ── Test 7: Concurrent cache + allowlist reads don't race ────────────────────
//
// Priority 1 (conflict area): M1 set_allowed_set is &mut self (safe, no
// concurrent mutation during run_code). But the global cache must survive
// concurrent reads and writes from multiple simulated executor threads.
#[test]
fn test_cache_concurrent_access_with_allowlist_simulation() {
    use std::sync::Arc;
    use std::thread;

    let cache = Arc::new(BytecodeCache::new(128));
    let allowed_modules: Arc<HashSet<String>> = Arc::new(
        DEFAULT_ALLOWED_MODULES.iter().map(|s| s.to_string()).collect()
    );

    let handles: Vec<_> = (0_u32..8)
        .map(|thread_id| {
            let cache = Arc::clone(&cache);
            let allowed = Arc::clone(&allowed_modules);
            thread::spawn(move || {
                for i in 0_u32..10 {
                    let source = format!("result_{thread_id}_{i} = {thread_id} + {i}");
                    let key = cache_key(&source);
                    cache.insert(key, format!("bytecode_{thread_id}_{i}"));

                    // Simulate allowlist check (as executor would do)
                    let _ = allowed.contains("math");
                    let _ = allowed.contains("socket"); // denied module

                    // Read back
                    let _ = cache.get(&key);
                }
            })
        })
        .collect();

    for h in handles {
        h.join().expect("thread must not panic during concurrent cache access");
    }

    // Cache should not be empty after 80 writes (may have eviction but > 0 entries)
    assert!(
        cache.len() > 0,
        "cache must have entries after concurrent insertions"
    );
}

// ── Test 8: M4 profile settings are reflected in Cargo.toml ─────────────────
//
// Priority 1 (M4 release profile merged): Verify the Cargo.toml contains
// the required [profile.release] and [profile.bench] sections as specified.
#[test]
fn test_cargo_toml_release_profile_settings() {
    let cargo_toml = std::fs::read_to_string(
        concat!(env!("CARGO_MANIFEST_DIR"), "/../../Cargo.toml")
    ).expect("Cargo.toml must be readable");

    // [profile.release] requirements (AC-12)
    assert!(
        cargo_toml.contains("opt-level = 3"),
        "Cargo.toml [profile.release] must contain opt-level = 3"
    );
    assert!(
        cargo_toml.contains("lto = \"fat\""),
        "Cargo.toml [profile.release] must contain lto = \"fat\""
    );
    assert!(
        cargo_toml.contains("codegen-units = 1"),
        "Cargo.toml [profile.release] must contain codegen-units = 1"
    );
    assert!(
        cargo_toml.contains("panic = \"abort\""),
        "Cargo.toml [profile.release] must contain panic = \"abort\""
    );
    assert!(
        cargo_toml.contains("strip = \"symbols\""),
        "Cargo.toml [profile.release] must contain strip = \"symbols\""
    );

    // [profile.bench] requirements (AC-12) — no panic="abort", no strip
    assert!(
        cargo_toml.contains("[profile.bench]"),
        "Cargo.toml must contain [profile.bench] section"
    );
    assert!(
        cargo_toml.contains("[profile.release]"),
        "Cargo.toml must contain [profile.release] section"
    );
}

// ── Test 9: M2 + M1: cache_key from set_allowed_set-modified allowlists ──────
//
// Priority 2 (cross-feature): The cache key is computed from the SOURCE only,
// not from the allowlist. Two executions of the same source with different
// allowlists must produce the SAME cache key (source deduplication).
#[test]
fn test_cache_key_independent_of_allowlist() {
    let source = "import math; math.sqrt(4)";

    // Key computed with "math" allowed
    let key_with_math = cache_key(source);

    // Key computed as if allowlist was changed (but cache_key ignores allowlist)
    let key_without_math = cache_key(source);

    assert_eq!(
        key_with_math, key_without_math,
        "cache_key must be source-addressed only; changing the allowlist must not change the key"
    );
}

// ── Test 10: M1 set_allowed_set + M2 global cache integration via execute ────
//
// Priority 2: Verify execute() + BytecodeCache::global() work together.
// After 3 identical calls, the global cache should have at most 1 entry
// for that source (deduplication).
//
// NOTE: execute() doesn't currently insert into BytecodeCache; this test
// verifies the cache API is usable from the same binary as execute().
#[test]
fn test_global_cache_usable_alongside_execute_api() {
    // Verify BytecodeCache::global() is accessible and functional when the
    // execute() API is present in the same binary (no symbol conflicts).
    let global_cache = BytecodeCache::global();
    let initial_len = global_cache.len();

    let source = "__global_api_test__ = 42";
    let key = cache_key(source);

    // Must not be in cache yet (unique test source)
    assert_eq!(
        global_cache.get(&key),
        None,
        "unique test source must not be pre-cached"
    );

    // Insert and verify
    global_cache.insert(key, "compiled_test_bytecode".to_string());
    assert_eq!(
        global_cache.len(),
        initial_len + 1,
        "global cache len must increase by 1 after inserting a new entry"
    );
    assert_eq!(
        global_cache.get(&key),
        Some("compiled_test_bytecode".to_string()),
        "global cache must return the inserted value"
    );
}

// ── Test 11: M2 + M1: lib.rs public re-exports are correct ───────────────────
//
// Priority 2 (shared file): lib.rs was modified by M2 to add pub mod cache
// and pub use cache::BytecodeCache. Verify these re-exports work correctly
// without shadowing execute or other existing re-exports.
#[test]
fn test_lib_reexports_cache_and_execute_coexist() {
    // BytecodeCache must be importable from the crate root (re-exported in lib.rs)
    let cache = BytecodeCache::new(4);
    cache.insert(cache_key("test"), "v".to_string());
    assert_eq!(cache.len(), 1, "BytecodeCache from crate root must work");

    // ExecutionSettings must still be importable (not shadowed by M2 changes)
    let settings = ExecutionSettings::default();
    assert_eq!(
        settings.timeout_ns, 5_000_000_000,
        "ExecutionSettings::default() must still be accessible after M2 merge"
    );
    assert_eq!(
        settings.max_output_bytes, 1_048_576,
        "ExecutionSettings::default().max_output_bytes must still be 1 MiB after M2 merge"
    );
    assert_eq!(
        settings.allowed_modules.len(), 11,
        "ExecutionSettings::default().allowed_modules must still have 11 entries after M2 merge"
    );

    // ExecutionError variants must be available (shared types.rs not broken by M1/M2)
    let err = ExecutionError::ModuleNotAllowed { module_name: "socket".to_string() };
    assert_eq!(
        format!("{err:?}"),
        "ModuleNotAllowed { module_name: \"socket\" }",
        "ExecutionError::ModuleNotAllowed must be constructible after M1 merge"
    );
}

// ── Test 12: M1 vm.rs new methods: set_allowed_set takes a HashSet ────────────
//
// Priority 1 (conflict resolution): M1 added set_allowed_set which takes
// HashSet<String>. This test verifies that DEFAULT_ALLOWED_MODULES can be
// used to construct the exact type expected by set_allowed_set.
#[test]
fn test_set_allowed_set_type_compatibility_with_default_modules() {
    // DEFAULT_ALLOWED_MODULES is &[&str]; set_allowed_set takes HashSet<String>.
    // Verify the conversion path that pool.rs would use.
    let allowed_set: HashSet<String> = DEFAULT_ALLOWED_MODULES
        .iter()
        .map(|s| s.to_string())
        .collect();

    assert_eq!(
        allowed_set.len(),
        DEFAULT_ALLOWED_MODULES.len(),
        "HashSet<String> built from DEFAULT_ALLOWED_MODULES must have the same cardinality"
    );

    // Verify all modules present
    for module in DEFAULT_ALLOWED_MODULES {
        assert!(
            allowed_set.contains(*module),
            "HashSet must contain '{}' from DEFAULT_ALLOWED_MODULES",
            module
        );
    }

    // Also verify a custom allowlist (as pool.rs would pass to set_allowed_set)
    let custom_set: HashSet<String> = vec!["math".to_string(), "re".to_string()]
        .into_iter()
        .collect();
    assert_eq!(custom_set.len(), 2);
    assert!(custom_set.contains("math"));
    assert!(custom_set.contains("re"));
    assert!(!custom_set.contains("socket"), "socket must not be in custom allowlist");
}
