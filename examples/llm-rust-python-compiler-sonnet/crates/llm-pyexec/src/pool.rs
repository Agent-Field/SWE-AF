//! Interpreter pool for llm-pyexec — persistent-thread-per-slot actor model.
//!
//! ## Design
//!
//! Each pool slot is a dedicated OS thread that:
//! 1. Initializes one `PyInterp` at startup (pre-warming).
//! 2. Blocks indefinitely on a `Receiver<WorkItem>` channel.
//! 3. On receiving a work item: calls `run_code()`, resets interpreter state,
//!    sends `VmRunResult` back via the work item's response channel.
//! 4. The interpreter NEVER crosses thread boundaries — this is the key design
//!    invariant required because `PyInterp` is not `Send`.
//!
//! ## Thread safety
//!
//! The pool itself (slot dispatch) uses `Mutex<VecDeque<Sender<WorkItem>>>` +
//! `Condvar` to hand work channels to calling threads. Only the `Sender` end
//! of the work channel (which is `Send`) crosses thread boundaries. The
//! `PyInterp` stays on its dedicated slot thread.
//!
//! ## Pool size
//!
//! Configured via `PYEXEC_POOL_SIZE` env var at first call to `InterpreterPool::global()`.
//! Default: 4.
//!
//! ## Timeout handling
//!
//! If the caller's `recv_timeout` on the response channel times out, the work
//! item has already been sent to (and is being executed by) the slot thread.
//! The slot thread will complete execution eventually and send the result —
//! but no one is listening. The slot's result channel disconnects, the slot
//! thread discards the result, resets interpreter state, and returns its sender
//! to the available queue. Pool size remains stable. No replacement thread needed.
//!
//! This is possible because `std::sync::mpsc::SyncSender::send()` on a
//! disconnected channel returns `Err(SendError)`, which the slot thread
//! handles by simply continuing its loop.
//!
//! ## Zero unsafe blocks (AC-18)
//!
//! This file contains no `unsafe` code. All concurrency uses safe Rust APIs
//! (`Mutex`, `Condvar`, `mpsc::sync_channel`, `Arc`).

use std::collections::HashSet;
use std::collections::VecDeque;
use std::sync::{Arc, Condvar, Mutex, OnceLock};
use std::time::Duration;

use crate::output::OutputBuffer;
use crate::types::DEFAULT_ALLOWED_MODULES;
use crate::vm::{build_interpreter, run_code, VmRunResult};

// ── Work item types ──────────────────────────────────────────────────────────

/// A unit of work sent from `execute()` to a pool slot thread.
///
/// All fields are `Send` — this is what crosses the thread boundary.
///
/// - `String`: Send
/// - `OutputBuffer`: Send (it's Arc<Mutex<...>>)
/// - `Arc<HashSet<String>>`: Send
/// - `SyncSender<VmRunResult>`: Send
/// - `VmRunResult` is Send because it contains only String and Option<ExecutionError>
pub(crate) struct WorkItem {
    /// The (already-wrapped) Python source string to execute.
    pub wrapped_source: String,
    /// Output buffer for capturing stdout/stderr.
    pub output: OutputBuffer,
    /// The allowlist for this specific call (may differ from pool default).
    pub allowed_set: Arc<HashSet<String>>,
    /// One-shot channel to send the result back to the calling thread.
    pub response: std::sync::mpsc::SyncSender<VmRunResult>,
}

// ── Pool slot ────────────────────────────────────────────────────────────────

/// Starts one pool slot: a dedicated OS thread that initializes a `PyInterp`
/// and loops processing `WorkItem`s.
///
/// Returns the `SyncSender<WorkItem>` that the pool uses to dispatch work to this slot.
///
/// Called once per slot at pool initialization time.
fn start_slot_thread(
    slot_id: usize,
    pool_available: Arc<(Mutex<VecDeque<std::sync::mpsc::SyncSender<WorkItem>>>, Condvar)>,
) -> std::sync::mpsc::SyncSender<WorkItem> {
    // Bounded channel capacity 1: the slot processes one item at a time.
    // SyncSender<WorkItem> is Send; the channel is safe to share across threads.
    let (tx, rx) = std::sync::mpsc::sync_channel::<WorkItem>(1);
    let tx_for_pool = tx.clone();

    std::thread::Builder::new()
        .name(format!("pyexec-pool-slot-{slot_id}"))
        .spawn(move || {
            // Initialize interpreter on the slot thread (never leaves this thread).
            let default_set: HashSet<String> = DEFAULT_ALLOWED_MODULES
                .iter()
                .map(|s| s.to_string())
                .collect();
            let dummy_output = OutputBuffer::new(1_048_576);
            let mut interp = build_interpreter(default_set, dummy_output);

            // Capture the baseline sys.modules set for state reset between calls.
            // This is done once after initialization and before any user code runs.
            let baseline_modules = capture_baseline_modules(&interp);

            // Signal to pool that this slot is ready.
            {
                let (lock, cvar) = &*pool_available;
                let mut queue = lock.lock().expect("pool slot queue poisoned");
                queue.push_back(tx.clone());
                cvar.notify_one();
            }

            // Process work items indefinitely.
            loop {
                let item = match rx.recv() {
                    Ok(item) => item,
                    Err(_) => break, // Channel closed (pool dropped). Exit.
                };

                // Override the allowlist for this call.
                interp.set_allowed_set((*item.allowed_set).clone());

                // Execute the code.
                let result = run_code(&interp, &item.wrapped_source, item.output);

                // Reset sys.modules to baseline state (PRD M1 state reset contract).
                reset_sys_modules(&interp, &baseline_modules);

                // Send result back. If caller timed out (receiver dropped), this
                // returns Err(SendError) — we discard it and continue the loop.
                let _ = item.response.send(result);

                // Return this slot's sender to the available queue.
                {
                    let (lock, cvar) = &*pool_available;
                    let mut queue = lock.lock().expect("pool slot queue poisoned");
                    queue.push_back(tx.clone());
                    cvar.notify_one();
                }
            }
        })
        .expect("Failed to spawn pool slot thread");

    tx_for_pool
}

// ── sys.modules baseline capture and reset ──────────────────────────────────

/// Captures the set of module names currently in sys.modules.
///
/// Called once after `build_interpreter()` and before any user code runs.
/// The returned set is used by `reset_sys_modules()` after each execution.
fn capture_baseline_modules(interp: &crate::vm::PyInterp) -> HashSet<String> {
    interp.with_vm(|vm| {
        let sys_modules = match vm.sys_module.get_attr("modules", vm) {
            Ok(m) => m,
            Err(_) => return HashSet::new(),
        };
        let keys = match vm.call_method(&sys_modules, "keys", ()) {
            Ok(k) => k,
            Err(_) => return HashSet::new(),
        };
        let iter = match vm.call_method(&keys, "__iter__", ()) {
            Ok(i) => i,
            Err(_) => return HashSet::new(),
        };
        let mut result = HashSet::new();
        loop {
            match vm.call_method(&iter, "__next__", ()) {
                Ok(key) => {
                    if let Ok(s) = key.str(vm) {
                        result.insert(s.as_str().to_owned());
                    }
                }
                Err(_) => break, // StopIteration or error
            }
        }
        result
    })
}

/// Removes any sys.modules entries not present in the baseline set.
///
/// Called after each `run_code()` call to satisfy the PRD M1 state reset contract:
/// "No user-imported modules persisted in sys.modules beyond the allowed stdlib
/// modules that were pre-loaded at init time."
fn reset_sys_modules(interp: &crate::vm::PyInterp, baseline: &HashSet<String>) {
    interp.with_vm(|vm| {
        let sys_modules = match vm.sys_module.get_attr("modules", vm) {
            Ok(m) => m,
            Err(_) => return,
        };
        // Collect keys to remove (can't remove during iteration).
        let keys = match vm.call_method(&sys_modules, "keys", ()) {
            Ok(k) => k,
            Err(_) => return,
        };
        let keys_iter = match vm.call_method(&keys, "__iter__", ()) {
            Ok(i) => i,
            Err(_) => return,
        };
        let mut to_remove: Vec<String> = Vec::new();
        loop {
            match vm.call_method(&keys_iter, "__next__", ()) {
                Ok(key) => {
                    if let Ok(s) = key.str(vm) {
                        let name = s.as_str().to_owned();
                        if !baseline.contains(&name) {
                            to_remove.push(name);
                        }
                    }
                }
                Err(_) => break, // StopIteration or error
            }
        }
        // Remove non-baseline entries.
        for name in to_remove {
            let _ = vm.call_method(
                &sys_modules,
                "__delitem__",
                (vm.ctx.new_str(name),),
            );
        }
    });
}

// ── InterpreterPool ──────────────────────────────────────────────────────────

/// Fixed-size pool of pre-warmed RustPython interpreters.
///
/// Each slot is a dedicated OS thread. Work is dispatched via `SyncSender<WorkItem>`.
/// Results are returned via per-call `mpsc::sync_channel`.
///
/// # Pool size
///
/// Configured at construction time. Use [`InterpreterPool::global()`] for the
/// process-global singleton which reads `PYEXEC_POOL_SIZE` env var (default 4).
pub struct InterpreterPool {
    /// Queue of available slot senders.
    available: Arc<(Mutex<VecDeque<std::sync::mpsc::SyncSender<WorkItem>>>, Condvar)>,
    target_size: usize,
}

impl InterpreterPool {
    /// Creates and pre-warms a pool of `size` interpreter slot threads.
    ///
    /// Blocks until all `size` slot threads have initialized their interpreters
    /// and reported themselves as available. The minimum effective size is 1
    /// (a `size` of 0 is treated as 1).
    ///
    /// Each thread is named `pyexec-pool-slot-{id}` where `id` is 0-based.
    ///
    /// # Panics
    ///
    /// Panics if any slot thread fails to start.
    pub fn new(size: usize) -> Self {
        let target_size = size.max(1);
        let available = Arc::new((
            Mutex::new(VecDeque::with_capacity(target_size)),
            Condvar::new(),
        ));

        for slot_id in 0..target_size {
            start_slot_thread(slot_id, Arc::clone(&available));
        }

        // Wait until all slots have initialized and pushed themselves to available.
        {
            let (lock, cvar) = &*available;
            let mut queue = lock.lock().expect("pool queue poisoned");
            while queue.len() < target_size {
                queue = cvar.wait(queue).expect("pool condvar poisoned");
            }
        }

        InterpreterPool { available, target_size }
    }

    /// Returns a reference to the process-global pool singleton.
    ///
    /// Pool size is read from `PYEXEC_POOL_SIZE` env var at first call.
    /// Default: 4.
    ///
    /// # Note
    ///
    /// The `PYEXEC_POOL_SIZE` env var is read exactly once (at first call).
    /// Tests that set this env var MUST run in a separate test binary
    /// that has not yet called `global()`.
    pub fn global() -> &'static InterpreterPool {
        static INSTANCE: OnceLock<InterpreterPool> = OnceLock::new();
        INSTANCE.get_or_init(|| {
            let size: usize = std::env::var("PYEXEC_POOL_SIZE")
                .ok()
                .and_then(|s| s.parse().ok())
                .unwrap_or(4);
            InterpreterPool::new(size)
        })
    }

    /// Dispatch a work item to an available slot thread.
    ///
    /// Blocks until a slot is available or `checkout_timeout` elapses.
    /// Returns `true` if dispatched, `false` if no slot was available within
    /// the timeout (caller should fall back to a fresh interpreter).
    ///
    /// When `true` is returned, the caller must receive from `work.response`
    /// (which was embedded in the WorkItem) to get the result.
    ///
    /// When `false` is returned, the WorkItem was NOT sent to any slot thread
    /// (the caller should drop it or use its components for a fallback path).
    // executor.rs integration (sibling milestone) will use this method.
    #[allow(dead_code)]
    pub(crate) fn dispatch_work(&self, work: WorkItem, checkout_timeout: Duration) -> bool {
        let (lock, cvar) = &*self.available;
        let deadline = std::time::Instant::now() + checkout_timeout;

        let slot_tx = loop {
            let mut queue = lock.lock().expect("pool queue poisoned");
            if let Some(tx) = queue.pop_front() {
                break tx;
            }
            let remaining = deadline.saturating_duration_since(std::time::Instant::now());
            if remaining.is_zero() {
                return false; // Caller falls back to fresh interpreter.
            }
            let result = cvar.wait_timeout(queue, remaining).expect("pool condvar poisoned");
            drop(result.0); // Release lock; next iteration re-acquires.
        };

        // send() cannot fail: slot thread is alive and channel capacity is 1.
        // If the slot is somehow busy (shouldn't happen — it was in available queue),
        // this would block briefly. Channel capacity=1 handles this correctly.
        let _ = slot_tx.send(work);
        true
    }

    /// Returns the number of idle (available) slots.
    ///
    /// A slot is "idle" when its sender is in the available queue (not currently
    /// processing a work item).
    pub fn idle_count(&self) -> usize {
        let (lock, _) = &*self.available;
        let queue = lock.lock().expect("pool queue poisoned");
        queue.len()
    }

    /// Returns the configured pool size (total slots, idle + active).
    pub fn size(&self) -> usize {
        self.target_size
    }
}

// PyInterp is intentionally NOT Send. If this ever compiles with Send, audit
// the safety implications carefully (RustPython's Rc<> internals are not thread-safe).
// static_assertions::assert_not_impl_any!(crate::vm::PyInterp: Send);

// ── Tests ─────────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;
    use crate::modules::build_allowed_set;
    use crate::types::ExecutionSettings;
    use std::sync::Arc;
    use std::time::Duration;

    /// Helper to build a default allowed set for test WorkItems.
    fn make_allowed_set() -> Arc<HashSet<String>> {
        let settings = ExecutionSettings::default();
        Arc::new(build_allowed_set(&settings))
    }

    // (1) Unit: InterpreterPool::new(1) — after creation, idle_count()==1
    #[test]
    #[ignore = "slow: VM init"]
    fn test_pool_new_1_idle_count_is_1() {
        let pool = InterpreterPool::new(1);
        assert_eq!(pool.idle_count(), 1, "Expected idle_count==1 after new(1)");
        assert_eq!(pool.size(), 1, "Expected size()==1");
    }

    // (2) Unit: dispatch_work with checkout_timeout=Duration::ZERO returns false immediately
    // (no slots available scenario — occupy the slot first)
    #[test]
    #[ignore = "slow: VM init"]
    fn test_dispatch_work_zero_timeout_returns_false_when_no_slots() {
        let pool = InterpreterPool::new(1);
        assert_eq!(pool.idle_count(), 1);

        // Occupy the single slot with a real work item so the pool is busy.
        let (response_tx, _response_rx) = std::sync::mpsc::sync_channel::<VmRunResult>(1);
        let output = OutputBuffer::new(1_048_576);
        let work = WorkItem {
            wrapped_source: "x = 1\n".to_string(),
            output,
            allowed_set: make_allowed_set(),
            response: response_tx,
        };

        // Dispatch with a real (non-zero) timeout to grab the slot.
        let dispatched = pool.dispatch_work(work, Duration::from_secs(5));
        assert!(dispatched, "Expected first dispatch to succeed");

        // Now the pool has 0 idle slots. A dispatch with zero timeout must fail immediately.
        let (response_tx2, _response_rx2) = std::sync::mpsc::sync_channel::<VmRunResult>(1);
        let output2 = OutputBuffer::new(1_048_576);
        let work2 = WorkItem {
            wrapped_source: "y = 2\n".to_string(),
            output: output2,
            allowed_set: make_allowed_set(),
            response: response_tx2,
        };

        let not_dispatched = pool.dispatch_work(work2, Duration::ZERO);
        assert!(!not_dispatched, "Expected dispatch to fail with zero timeout and no slots");
    }

    // (3) Functional: dispatch one work item to a pool of 1, receive result via response channel,
    // assert result is non-error.
    #[test]
    #[ignore = "slow: VM init"]
    fn test_dispatch_and_receive_result() {
        let pool = InterpreterPool::new(1);

        let (response_tx, response_rx) = std::sync::mpsc::sync_channel::<VmRunResult>(1);
        let output = OutputBuffer::new(1_048_576);
        let work = WorkItem {
            wrapped_source: "__result__ = 1 + 1\n".to_string(),
            output,
            allowed_set: make_allowed_set(),
            response: response_tx,
        };

        let dispatched = pool.dispatch_work(work, Duration::from_secs(30));
        assert!(dispatched, "Expected dispatch to succeed");

        let result = response_rx
            .recv_timeout(Duration::from_secs(30))
            .expect("Expected result within timeout");

        assert!(
            result.error.is_none(),
            "Expected no error, got: {:?}",
            result.error
        );
    }

    // (4) Edge case: after dispatch and response received, idle_count returns to 1
    #[test]
    #[ignore = "slow: VM init"]
    fn test_idle_count_restored_after_dispatch() {
        let pool = InterpreterPool::new(1);
        assert_eq!(pool.idle_count(), 1);

        let (response_tx, response_rx) = std::sync::mpsc::sync_channel::<VmRunResult>(1);
        let output = OutputBuffer::new(1_048_576);
        let work = WorkItem {
            wrapped_source: "pass\n".to_string(),
            output,
            allowed_set: make_allowed_set(),
            response: response_tx,
        };

        let dispatched = pool.dispatch_work(work, Duration::from_secs(30));
        assert!(dispatched, "Expected dispatch to succeed");

        // Wait for result — slot returns to pool after sending result.
        let _result = response_rx
            .recv_timeout(Duration::from_secs(30))
            .expect("Expected result within timeout");

        // Give the slot thread a moment to push itself back to available queue.
        // This is needed because the slot sends the result and THEN pushes back.
        std::thread::sleep(Duration::from_millis(50));

        assert_eq!(
            pool.idle_count(),
            1,
            "Expected idle_count==1 after work completed"
        );
    }

    // (5) State isolation: variable assigned in call 1 must not be visible in call 2
    #[test]
    #[ignore = "slow: VM init"]
    fn test_state_isolation_between_calls() {
        let pool = InterpreterPool::new(1);

        // Call 1: assign a variable
        let (tx1, rx1) = std::sync::mpsc::sync_channel::<VmRunResult>(1);
        let work1 = WorkItem {
            wrapped_source: "secret_var = 42\n".to_string(),
            output: OutputBuffer::new(1_048_576),
            allowed_set: make_allowed_set(),
            response: tx1,
        };
        assert!(pool.dispatch_work(work1, Duration::from_secs(30)));
        let r1 = rx1.recv_timeout(Duration::from_secs(30)).expect("recv1 timeout");
        assert!(r1.error.is_none(), "Call 1 unexpected error: {:?}", r1.error);

        // Short wait to ensure slot returns to pool.
        std::thread::sleep(Duration::from_millis(50));

        // Call 2: try to access the variable — should fail with NameError
        let (tx2, rx2) = std::sync::mpsc::sync_channel::<VmRunResult>(1);
        let work2 = WorkItem {
            wrapped_source: "__result__ = secret_var\n".to_string(),
            output: OutputBuffer::new(1_048_576),
            allowed_set: make_allowed_set(),
            response: tx2,
        };
        assert!(pool.dispatch_work(work2, Duration::from_secs(30)));
        let r2 = rx2.recv_timeout(Duration::from_secs(30)).expect("recv2 timeout");

        assert!(
            r2.error.is_some(),
            "Expected NameError for secret_var in call 2, but got no error"
        );
    }
}
