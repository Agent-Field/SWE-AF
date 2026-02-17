//! Thread-safe output capture buffer for the llm-pyexec library.
//!
//! [`OutputBuffer`] accumulates bytes written to stdout and stderr during Python
//! execution, enforcing a combined byte limit.  The buffer is designed to be
//! shared between the main thread (which reads results after execution) and the
//! VM thread (which writes during execution) via `Arc<Mutex<_>>` interior
//! mutability — no `unsafe` code required.
//!
//! # Timeout path
//!
//! When the VM thread is abandoned on timeout, it may still hold a clone of the
//! `OutputBuffer`.  [`into_strings`](OutputBuffer::into_strings) handles this
//! gracefully: it tries `Arc::try_unwrap` first (fast path when no other clone
//! exists) and falls back to locking the `Mutex` and cloning the inner data.

use std::sync::{Arc, Mutex};

use crate::types::ExecutionError;

// ── Inner state ───────────────────────────────────────────────────────────────

struct OutputBufferInner {
    stdout: Vec<u8>,
    stderr: Vec<u8>,
    max_bytes: usize,
    limit_exceeded: bool,
}

impl OutputBufferInner {
    fn new(max_bytes: usize) -> Self {
        Self {
            stdout: Vec::new(),
            stderr: Vec::new(),
            max_bytes,
            limit_exceeded: false,
        }
    }

    /// Returns the combined number of bytes written so far.
    fn total_len(&self) -> usize {
        self.stdout.len() + self.stderr.len()
    }
}

// ── Public API ────────────────────────────────────────────────────────────────

/// A thread-safe buffer that captures VM stdout and stderr output.
///
/// Cheap to clone — all clones share the same underlying data via
/// `Arc<Mutex<OutputBufferInner>>`.
#[derive(Clone)]
pub struct OutputBuffer {
    inner: Arc<Mutex<OutputBufferInner>>,
}

impl OutputBuffer {
    /// Creates a new `OutputBuffer` that will accept up to `max_bytes` combined
    /// across stdout and stderr.
    pub fn new(max_bytes: usize) -> Self {
        Self {
            inner: Arc::new(Mutex::new(OutputBufferInner::new(max_bytes))),
        }
    }

    /// Appends `data` to the stdout stream.
    ///
    /// Returns `Err(ExecutionError::OutputLimitExceeded { limit_bytes })` if
    /// accepting `data` would push the combined stdout+stderr total over
    /// `max_bytes`.  On error the buffer state is *not* modified and
    /// `is_limit_exceeded()` is set to `true`.
    pub fn write_stdout(&self, data: &[u8]) -> Result<(), ExecutionError> {
        let mut inner = self.inner.lock().expect("OutputBuffer mutex poisoned");
        if inner.total_len() + data.len() > inner.max_bytes {
            inner.limit_exceeded = true;
            return Err(ExecutionError::OutputLimitExceeded {
                limit_bytes: inner.max_bytes,
            });
        }
        inner.stdout.extend_from_slice(data);
        Ok(())
    }

    /// Appends `data` to the stderr stream.
    ///
    /// Same limit semantics as [`write_stdout`](Self::write_stdout).
    pub fn write_stderr(&self, data: &[u8]) -> Result<(), ExecutionError> {
        let mut inner = self.inner.lock().expect("OutputBuffer mutex poisoned");
        if inner.total_len() + data.len() > inner.max_bytes {
            inner.limit_exceeded = true;
            return Err(ExecutionError::OutputLimitExceeded {
                limit_bytes: inner.max_bytes,
            });
        }
        inner.stderr.extend_from_slice(data);
        Ok(())
    }

    /// Returns `true` if any write has been rejected due to the byte limit.
    pub fn is_limit_exceeded(&self) -> bool {
        let inner = self.inner.lock().expect("OutputBuffer mutex poisoned");
        inner.limit_exceeded
    }

    /// Consumes this handle and returns `(stdout, stderr)` as UTF-8 strings.
    ///
    /// Invalid UTF-8 sequences are replaced with the Unicode replacement
    /// character (`\u{FFFD}`) via [`String::from_utf8_lossy`].
    ///
    /// If another `Arc` clone exists (e.g. the VM thread is still running after
    /// a timeout), this method falls back to locking the `Mutex` and cloning
    /// the byte vectors rather than panicking.
    pub fn into_strings(self) -> (String, String) {
        match Arc::try_unwrap(self.inner) {
            Ok(mutex) => {
                // We are the sole owner — unwrap without locking.
                let inner = mutex.into_inner().expect("OutputBuffer mutex poisoned");
                (
                    String::from_utf8_lossy(&inner.stdout).into_owned(),
                    String::from_utf8_lossy(&inner.stderr).into_owned(),
                )
            }
            Err(arc) => {
                // Another clone exists (timeout path) — lock and clone the data.
                let inner = arc.lock().expect("OutputBuffer mutex poisoned");
                (
                    String::from_utf8_lossy(&inner.stdout).into_owned(),
                    String::from_utf8_lossy(&inner.stderr).into_owned(),
                )
            }
        }
    }
}

// ── Unit tests ────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;
    use crate::types::ExecutionError;

    // (1) write_stdout under limit — data retrievable via into_strings
    #[test]
    fn test_write_stdout_under_limit() {
        let buf = OutputBuffer::new(64);
        assert!(buf.write_stdout(b"hello").is_ok());
        let (stdout, stderr) = buf.into_strings();
        assert_eq!(stdout, "hello");
        assert_eq!(stderr, "");
    }

    // (2) write_stderr under limit — appears in stderr from into_strings
    #[test]
    fn test_write_stderr_under_limit() {
        let buf = OutputBuffer::new(64);
        assert!(buf.write_stderr(b"error output").is_ok());
        let (stdout, stderr) = buf.into_strings();
        assert_eq!(stdout, "");
        assert_eq!(stderr, "error output");
    }

    // (3) write that exactly hits the byte limit (boundary — should succeed)
    #[test]
    fn test_write_stdout_exactly_at_limit() {
        let buf = OutputBuffer::new(5);
        // Writing exactly 5 bytes to a 5-byte buffer should succeed.
        assert!(buf.write_stdout(b"hello").is_ok());
        let (stdout, _) = buf.into_strings();
        assert_eq!(stdout, "hello");
    }

    // (4) write that exceeds limit returns OutputLimitExceeded
    #[test]
    fn test_write_stdout_exceeds_limit() {
        let buf = OutputBuffer::new(5);
        // First write fills the buffer exactly.
        assert!(buf.write_stdout(b"hello").is_ok());
        // One more byte must exceed the limit.
        let result = buf.write_stdout(b"!");
        match result {
            Err(ExecutionError::OutputLimitExceeded { limit_bytes }) => {
                assert_eq!(limit_bytes, 5);
            }
            other => panic!("expected OutputLimitExceeded, got {:?}", other),
        }
    }

    // (5) is_limit_exceeded() returns true after a limit-exceeded write
    #[test]
    fn test_is_limit_exceeded_after_overflow() {
        let buf = OutputBuffer::new(3);
        // Overflow immediately with a write larger than the limit.
        let _ = buf.write_stdout(b"toolong");
        assert!(buf.is_limit_exceeded());
    }

    // (6) clone() shares state — write via clone is visible through original
    #[test]
    fn test_clone_shares_state() {
        let buf = OutputBuffer::new(64);
        let clone = buf.clone();
        clone.write_stdout(b"from clone").expect("write via clone failed");
        // Check via the original handle.
        assert!(!buf.is_limit_exceeded());
        let (stdout, _) = buf.into_strings();
        assert_eq!(stdout, "from clone");
    }

    // (7) into_strings() with 2 Arc references (timeout path) — no panic
    #[test]
    fn test_into_strings_with_live_clone() {
        let buf = OutputBuffer::new(64);
        buf.write_stdout(b"data").expect("write failed");

        // Keep a second Arc alive to trigger the fallback path in into_strings.
        let _live_clone = buf.clone();
        let (stdout, stderr) = buf.into_strings();
        assert_eq!(stdout, "data");
        assert_eq!(stderr, "");
        // _live_clone is still alive here — no panic should have occurred.
    }

    // (8) Invalid UTF-8 bytes are replaced via from_utf8_lossy (not panic)
    #[test]
    fn test_invalid_utf8_replaced_not_panic() {
        let buf = OutputBuffer::new(64);
        // 0xFF is not valid UTF-8.
        buf.write_stdout(&[0xFF]).expect("write failed");
        buf.write_stderr(&[0xFE, 0x80]).expect("write failed");
        let (stdout, stderr) = buf.into_strings();
        // Should not panic; replacement character(s) present instead.
        assert!(stdout.contains('\u{FFFD}'));
        assert!(stderr.contains('\u{FFFD}'));
    }

    // (9) Combined stdout+stderr limit is enforced across both streams
    #[test]
    fn test_combined_limit_across_streams() {
        let buf = OutputBuffer::new(10);
        // Write 6 bytes to stdout — OK.
        assert!(buf.write_stdout(b"123456").is_ok());
        // Write 5 more bytes to stderr — combined total would be 11 > 10.
        let result = buf.write_stderr(b"abcde");
        match result {
            Err(ExecutionError::OutputLimitExceeded { limit_bytes }) => {
                assert_eq!(limit_bytes, 10);
            }
            other => panic!("expected OutputLimitExceeded, got {:?}", other),
        }
        assert!(buf.is_limit_exceeded());
    }
}
