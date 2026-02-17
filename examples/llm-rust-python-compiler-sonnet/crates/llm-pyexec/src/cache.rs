//! Bytecode cache: an LRU cache keyed by SHA-256 hashes of Python source strings.
//!
//! The cache stores compiled bytecode (as `String`) indexed by a 32-byte SHA-256
//! digest of the corresponding source code. This avoids recompiling identical source
//! strings across successive `execute()` calls.
//!
//! # Environment variable
//!
//! `PYEXEC_BYTECODE_CACHE_SIZE` — maximum number of entries; defaults to `256`.
//! Setting it to `0` is treated as `1` (no panic, always keep at least one entry).
//!
//! # Thread safety
//!
//! [`BytecodeCache`] wraps its inner LRU cache in a `Mutex` so it can be shared
//! across threads via the `global()` singleton.

use std::num::NonZeroUsize;
use std::sync::{Mutex, OnceLock};

use lru::LruCache;
use sha2::{Digest, Sha256};

/// A 32-byte SHA-256 digest used as a cache key.
pub type CacheKey = [u8; 32];

/// Compute the SHA-256 hash of `source` bytes and return it as a [`CacheKey`].
///
/// The same input always produces the same 32-byte output; different inputs
/// produce distinct outputs with overwhelming probability.
pub fn cache_key(source: &str) -> CacheKey {
    let mut hasher = Sha256::new();
    hasher.update(source.as_bytes());
    hasher.finalize().into()
}

/// LRU cache mapping [`CacheKey`] → compiled bytecode `String`.
///
/// Create a local instance with [`BytecodeCache::new`] or obtain the
/// process-wide singleton with [`BytecodeCache::global`].
pub struct BytecodeCache {
    inner: Mutex<LruCache<CacheKey, String>>,
    capacity: usize,
}

impl BytecodeCache {
    /// Create a new [`BytecodeCache`] with the given maximum number of entries.
    ///
    /// `capacity` is clamped to a minimum of `1`; passing `0` is safe and will
    /// behave as though `capacity == 1`.
    pub fn new(capacity: usize) -> Self {
        let cap = NonZeroUsize::new(capacity.max(1)).expect("capacity >= 1");
        Self {
            inner: Mutex::new(LruCache::new(cap)),
            capacity: capacity.max(1),
        }
    }

    /// Return the process-wide singleton [`BytecodeCache`].
    ///
    /// The capacity is read once from the `PYEXEC_BYTECODE_CACHE_SIZE`
    /// environment variable. If the variable is absent or unparseable the
    /// default capacity of `256` is used. A value of `0` is treated as `1`.
    pub fn global() -> &'static BytecodeCache {
        static INSTANCE: OnceLock<BytecodeCache> = OnceLock::new();
        INSTANCE.get_or_init(|| {
            let capacity = std::env::var("PYEXEC_BYTECODE_CACHE_SIZE")
                .ok()
                .and_then(|v| v.parse::<usize>().ok())
                .unwrap_or(256);
            BytecodeCache::new(capacity)
        })
    }

    /// Look up `key` in the cache.
    ///
    /// Returns `Some(bytecode)` on a hit and advances the entry to the most-recently-used
    /// position; returns `None` on a miss.
    pub fn get(&self, key: &CacheKey) -> Option<String> {
        self.inner
            .lock()
            .expect("BytecodeCache mutex poisoned")
            .get(key)
            .cloned()
    }

    /// Insert `key` → `value` into the cache.
    ///
    /// If the cache is already at capacity the least-recently-used entry is
    /// evicted to make room.
    pub fn insert(&self, key: CacheKey, value: String) {
        self.inner
            .lock()
            .expect("BytecodeCache mutex poisoned")
            .put(key, value);
    }

    /// Return the number of entries currently in the cache.
    pub fn len(&self) -> usize {
        self.inner
            .lock()
            .expect("BytecodeCache mutex poisoned")
            .len()
    }

    /// Return `true` if the cache contains no entries.
    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }

    /// Return the maximum number of entries the cache can hold before eviction.
    pub fn capacity(&self) -> usize {
        self.capacity
    }

    /// Remove all entries from the cache, leaving it empty.
    ///
    /// The capacity is unchanged. This is primarily useful for test isolation
    /// when multiple tests share the same process-wide singleton.
    pub fn clear(&self) {
        self.inner
            .lock()
            .expect("BytecodeCache mutex poisoned")
            .clear();
    }
}

// ─── Unit tests ────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    // ── cache_key ────────────────────────────────────────────────────────────

    #[test]
    fn test_cache_key_consistent_output() {
        let key1 = cache_key("print('hello')");
        let key2 = cache_key("print('hello')");
        assert_eq!(key1, key2, "same input must always produce the same key");
        assert_eq!(key1.len(), 32, "key must be exactly 32 bytes");
    }

    #[test]
    fn test_cache_key_different_inputs_differ() {
        let key1 = cache_key("x = 1");
        let key2 = cache_key("x = 2");
        assert_ne!(key1, key2, "different inputs must produce different keys");
    }

    #[test]
    fn test_cache_key_empty_string() {
        let key = cache_key("");
        assert_eq!(key.len(), 32);
    }

    // ── get / insert / len round-trip ────────────────────────────────────────

    #[test]
    fn test_get_returns_none_on_miss() {
        let cache = BytecodeCache::new(8);
        let key = cache_key("some source");
        assert_eq!(cache.get(&key), None);
    }

    #[test]
    fn test_insert_then_get_returns_value() {
        let cache = BytecodeCache::new(8);
        let key = cache_key("x = 42");
        cache.insert(key, "compiled_bytecode".to_string());
        assert_eq!(cache.get(&key), Some("compiled_bytecode".to_string()));
    }

    #[test]
    fn test_len_tracks_insertions() {
        let cache = BytecodeCache::new(8);
        assert_eq!(cache.len(), 0);
        cache.insert(cache_key("a"), "A".to_string());
        assert_eq!(cache.len(), 1);
        cache.insert(cache_key("b"), "B".to_string());
        assert_eq!(cache.len(), 2);
    }

    #[test]
    fn test_is_empty_on_fresh_cache() {
        let cache = BytecodeCache::new(4);
        assert!(cache.is_empty());
        cache.insert(cache_key("x"), "v".to_string());
        assert!(!cache.is_empty());
    }

    #[test]
    fn test_capacity_returns_configured_value() {
        let cache = BytecodeCache::new(16);
        assert_eq!(cache.capacity(), 16);
    }

    // ── LRU eviction ─────────────────────────────────────────────────────────

    #[test]
    fn test_lru_eviction_with_capacity_one() {
        let cache = BytecodeCache::new(1);

        let key_a = cache_key("source_a");
        let key_b = cache_key("source_b");

        cache.insert(key_a, "bytecode_a".to_string());
        // Inserting key_b must evict key_a (only room for 1 entry)
        cache.insert(key_b, "bytecode_b".to_string());

        assert_eq!(cache.len(), 1, "capacity=1 must keep exactly one entry");
        assert_eq!(
            cache.get(&key_a),
            None,
            "key_a should have been evicted (LRU)"
        );
        assert_eq!(
            cache.get(&key_b),
            Some("bytecode_b".to_string()),
            "key_b should be the surviving entry"
        );
    }

    #[test]
    fn test_lru_eviction_order_with_capacity_two() {
        let cache = BytecodeCache::new(2);

        let key_a = cache_key("a");
        let key_b = cache_key("b");
        let key_c = cache_key("c");

        cache.insert(key_a, "A".to_string());
        cache.insert(key_b, "B".to_string());
        // Access key_a to make it recently used (key_b becomes LRU)
        let _ = cache.get(&key_a);
        // Inserting key_c must evict key_b (the new LRU)
        cache.insert(key_c, "C".to_string());

        assert_eq!(cache.len(), 2);
        assert_eq!(cache.get(&key_b), None, "key_b should be evicted");
        assert!(cache.get(&key_a).is_some(), "key_a should survive");
        assert!(cache.get(&key_c).is_some(), "key_c should survive");
    }

    // ── Edge cases ───────────────────────────────────────────────────────────

    #[test]
    fn test_capacity_zero_treated_as_one() {
        // Must not panic; clamps to 1
        let cache = BytecodeCache::new(0);
        assert_eq!(cache.capacity(), 1);
        let key = cache_key("x");
        cache.insert(key, "v".to_string());
        assert_eq!(cache.len(), 1);
    }

    #[test]
    fn test_overwrite_same_key_does_not_grow_len() {
        let cache = BytecodeCache::new(4);
        let key = cache_key("same source");
        cache.insert(key, "v1".to_string());
        cache.insert(key, "v2".to_string());
        assert_eq!(cache.len(), 1);
        assert_eq!(cache.get(&key), Some("v2".to_string()));
    }

    // ── clear ────────────────────────────────────────────────────────────────

    #[test]
    fn test_clear_empties_cache() {
        let cache = BytecodeCache::new(8);
        cache.insert(cache_key("a"), "A".to_string());
        cache.insert(cache_key("b"), "B".to_string());
        assert_eq!(cache.len(), 2);
        cache.clear();
        assert_eq!(cache.len(), 0, "cache must be empty after clear()");
        assert!(cache.is_empty());
    }

    #[test]
    fn test_clear_on_empty_is_safe() {
        let cache = BytecodeCache::new(4);
        // Must not panic when called on an already-empty cache.
        cache.clear();
        assert_eq!(cache.len(), 0);
    }

    #[test]
    fn test_clear_then_insert_works() {
        let cache = BytecodeCache::new(4);
        cache.insert(cache_key("x"), "v1".to_string());
        cache.clear();
        // Cache must be fully usable after clearing.
        let key = cache_key("y");
        cache.insert(key, "v2".to_string());
        assert_eq!(cache.len(), 1);
        assert_eq!(cache.get(&key), Some("v2".to_string()));
    }

    // ── Thread safety ────────────────────────────────────────────────────────

    #[test]
    fn test_concurrent_insert_and_get_no_panic() {
        use std::sync::Arc;
        use std::thread;

        let cache = Arc::new(BytecodeCache::new(64));

        let handles: Vec<_> = (0_u32..4)
            .map(|i| {
                let c = Arc::clone(&cache);
                thread::spawn(move || {
                    for j in 0_u32..16 {
                        let src = format!("thread_{i}_item_{j}");
                        let key = cache_key(&src);
                        c.insert(key, src.clone());
                        let _ = c.get(&key);
                    }
                })
            })
            .collect();

        for h in handles {
            h.join().expect("thread should not panic");
        }
    }
}
