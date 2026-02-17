use std::sync::mpsc;
use std::thread;
use std::time::Duration;

/// Run `f` in a new thread. Wait at most `timeout_ns` nanoseconds for it to finish.
///
/// # Returns
/// - `Some(T)` if `f` completed within the timeout.
/// - `None` if the timeout was exceeded. The spawned thread is abandoned (not joined).
///   The thread will terminate on its own when it finishes its work or the process exits.
/// - `None` if the spawned thread panics (channel becomes Disconnected).
///
/// # Thread safety
/// `f` must be `Send + 'static`. The return type `T` must be `Send + 'static`.
///
/// # Abandonment guarantee
/// When `None` is returned, the spawned thread holds no shared references to data
/// that the caller owns exclusively. The `OutputBuffer` inside is reference-counted;
/// the thread's clone of the Arc will be dropped when the thread eventually terminates.
///
/// # Why no SIGALRM / process::exit
/// SIGALRM is not thread-safe on Linux with multi-threading. process::exit kills
/// all threads including the caller. Thread abandonment is the only portable,
/// safe mechanism for interrupting a tight Python loop that never yields.
pub fn run_with_timeout<F, T>(f: F, timeout_ns: u64) -> Option<T>
where
    F: FnOnce() -> T + Send + 'static,
    T: Send + 'static,
{
    let (tx, rx) = mpsc::channel::<T>();

    let _handle = thread::Builder::new()
        .name("pyexec-vm".to_string())
        .spawn(move || {
            let result = f();
            // If send fails, the receiver was dropped (timed out). Ignore.
            let _ = tx.send(result);
        })
        .expect("Failed to spawn execution thread");

    let timeout = Duration::from_nanos(timeout_ns);
    match rx.recv_timeout(timeout) {
        Ok(result) => Some(result),
        Err(mpsc::RecvTimeoutError::Timeout) => None,
        Err(mpsc::RecvTimeoutError::Disconnected) => {
            // Thread panicked without sending. Treat as timeout/error.
            None
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::Instant;

    /// Fast closure (sleep 1ms) with 1s timeout returns Some with correct value.
    #[test]
    fn test_fast_closure_returns_some() {
        let result = run_with_timeout(
            || {
                std::thread::sleep(Duration::from_millis(1));
                42u32
            },
            1_000_000_000, // 1 second
        );
        assert_eq!(result, Some(42u32), "Expected Some(42), got {:?}", result);
    }

    /// Slow closure (sleep 500ms) with 50ms timeout returns None.
    #[test]
    fn test_slow_closure_returns_none() {
        let result = run_with_timeout(
            || {
                std::thread::sleep(Duration::from_millis(500));
                99u32
            },
            50_000_000, // 50 milliseconds
        );
        assert!(result.is_none(), "Expected None for timed-out closure, got {:?}", result);
    }

    /// Verify the None case returns within ~200ms wall clock (generous slop for CI).
    #[test]
    fn test_timeout_returns_promptly() {
        let timeout_ns = 50_000_000u64; // 50 milliseconds
        let start = Instant::now();
        let result = run_with_timeout(
            || {
                std::thread::sleep(Duration::from_millis(500));
                0u32
            },
            timeout_ns,
        );
        let elapsed_ns = start.elapsed().as_nanos() as u64;

        assert!(result.is_none(), "Expected None, got {:?}", result);
        // Assert that we returned within timeout_ns * 5 (generous slop for CI).
        // 50ms * 5 = 250ms
        let max_allowed_ns = timeout_ns * 5;
        assert!(
            elapsed_ns < max_allowed_ns,
            "Expected return within {}ns, but took {}ns",
            max_allowed_ns,
            elapsed_ns
        );
    }

    /// Panicking closure returns None instead of propagating panic.
    #[test]
    fn test_panicking_closure_returns_none() {
        // The panic in the spawned thread will cause the channel sender to be dropped
        // without sending a value, resulting in Disconnected error -> None.
        let result = run_with_timeout(
            || -> u32 {
                panic!("intentional panic in spawned thread");
            },
            1_000_000_000, // 1 second — plenty of time for the thread to panic
        );
        assert!(
            result.is_none(),
            "Expected None for panicking closure, got {:?}",
            result
        );
    }
}
