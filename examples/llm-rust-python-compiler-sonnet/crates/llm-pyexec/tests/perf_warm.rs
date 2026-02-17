// crates/llm-pyexec/tests/perf_warm.rs
// Tests: AC-06, AC-07, AC-08
// Run with --release: cargo test --test perf_warm --release

use llm_pyexec::pool::InterpreterPool;
use llm_pyexec::{execute, ExecutionSettings};
use std::time::Instant;

const WARMUP_CALLS: usize = 10;
const MEASURED_CALLS: usize = 190;

// Snippet definitions — EXACT match to PRD AC-06 specification (§8.1 canonical snippets).

/// bench_01: Arithmetic sum of squares
const SNIPPET_01: &str = "sum(i*i for i in range(1000))";

/// bench_02: String ops — FULL PANGRAM as specified in PRD AC-06
const SNIPPET_02: &str = concat!(
    "words = \"the quick brown fox jumps over the lazy dog\".split()\n",
    "\" \".join(w.capitalize() for w in words)"
);

/// bench_03: List comprehension
const SNIPPET_03: &str = concat!(
    "matrix = [[j*10+i for i in range(10)] for j in range(10)]\n",
    "[x for row in matrix for x in row if x % 3 == 0]"
);

/// bench_04: Dict ops
const SNIPPET_04: &str = concat!(
    "text = \"hello world\"\n",
    "freq = {}\n",
    "for c in text:\n",
    "    freq[c] = freq.get(c, 0) + 1\n",
    "sorted(freq.items(), key=lambda x: -x[1])"
);

/// bench_05: JSON roundtrip
const SNIPPET_05: &str = concat!(
    "import json\n",
    "data = {\"key\": \"value\", \"numbers\": [1, 2, 3], \"nested\": {\"a\": 1}}\n",
    "json.dumps(json.loads(json.dumps(data)))"
);

const SNIPPETS: &[(&str, &str)] = &[
    ("bench_01", SNIPPET_01),
    ("bench_02", SNIPPET_02),
    ("bench_03", SNIPPET_03),
    ("bench_04", SNIPPET_04),
    ("bench_05", SNIPPET_05),
];

fn measure_snippet(code: &str) -> Vec<u64> {
    let settings = ExecutionSettings::default();

    // Warmup: discarded.
    for _ in 0..WARMUP_CALLS {
        let _ = execute(code, settings.clone());
    }

    // Measure MEASURED_CALLS calls.
    let mut latencies = Vec::with_capacity(MEASURED_CALLS);
    for _ in 0..MEASURED_CALLS {
        let t0 = Instant::now();
        let _ = execute(code, settings.clone());
        latencies.push(t0.elapsed().as_nanos() as u64);
    }
    latencies
}

fn median(samples: &mut Vec<u64>) -> u64 {
    samples.sort_unstable();
    samples[samples.len() / 2]
}

fn p95(samples: &mut Vec<u64>) -> u64 {
    samples.sort_unstable();
    let idx = ((samples.len() as f64 * 0.95) as usize).min(samples.len() - 1);
    samples[idx]
}

/// AC-06 + AC-07: Warm-path median < 10ms and p95 < 50ms for all 5 snippets.
#[test]
fn test_warm_latency_all_snippets() {
    // Pre-warm the pool before measuring.
    let _ = InterpreterPool::global();

    for (name, code) in SNIPPETS {
        let mut latencies = measure_snippet(code);
        let med = median(&mut latencies.clone());
        let p95_val = p95(&mut latencies);

        assert!(
            med < 10_000_000,
            "{name}: median {med}ns ({:.1}ms) >= 10ms target",
            med as f64 / 1_000_000.0
        );
        assert!(
            p95_val < 50_000_000,
            "{name}: p95 {p95_val}ns ({:.1}ms) >= 50ms target",
            p95_val as f64 / 1_000_000.0
        );
    }
}

/// AC-08: 190 bench_01 calls complete in < 1.9s (≥ 100 ops/sec).
#[test]
fn test_warm_throughput_bench_01() {
    // Pre-warm the pool.
    let _ = InterpreterPool::global();

    let code = SNIPPET_01;
    let settings = ExecutionSettings::default();

    // Warmup.
    for _ in 0..WARMUP_CALLS {
        let _ = execute(code, settings.clone());
    }

    // Measure total wall-clock time for 190 calls.
    let wall_start = Instant::now();
    for _ in 0..MEASURED_CALLS {
        let _ = execute(code, settings.clone());
    }
    let wall_ns = wall_start.elapsed().as_nanos() as u64;

    // 190 calls in < 1.9s = ≥ 100 ops/sec (AC-08).
    assert!(
        wall_ns < 1_900_000_000,
        "190 bench_01 calls took {wall_ns}ns ({:.1}ms), threshold is 1900ms",
        wall_ns as f64 / 1_000_000.0
    );
}
