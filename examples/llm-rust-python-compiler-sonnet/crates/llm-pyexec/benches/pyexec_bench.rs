// crates/llm-pyexec/benches/pyexec_bench.rs
//
// Three Criterion benchmark groups:
//   cold_start         — RustPython CLI subprocess spawn-to-result (AC-09)
//   cpython_cold_start — CPython `python3 -c` subprocess (AC-10)
//   warm_throughput    — All 5 snippets with pool pre-warmed (AC-11, AC-20)

use criterion::{black_box, criterion_group, criterion_main, Criterion};
use llm_pyexec::{execute, ExecutionSettings, InterpreterPool};
use std::time::Duration;

// ---------------------------------------------------------------------------
// §8.1 Canonical snippet constants — exact strings as specified in architecture
// ---------------------------------------------------------------------------

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

// ---------------------------------------------------------------------------
// Group 1: cold_start — RustPython CLI subprocess spawn-to-result (AC-09)
// ---------------------------------------------------------------------------

fn cold_start(c: &mut Criterion) {
    // LLMPYEXEC_CLI_PATH env var overrides the default. The default resolves to
    // <workspace-root>/target/release/llm-pyexec-cli using CARGO_MANIFEST_DIR
    // (set at compile time) to navigate from the crate root to workspace root.
    let cli_path = std::env::var("LLMPYEXEC_CLI_PATH").unwrap_or_else(|_| {
        let manifest_dir = env!("CARGO_MANIFEST_DIR");
        let workspace_root = std::path::Path::new(manifest_dir)
            .parent()  // crates/
            .and_then(|p| p.parent())  // workspace root
            .map(|p| p.to_path_buf())
            .unwrap_or_else(|| std::path::PathBuf::from("."));
        workspace_root
            .join("target")
            .join("release")
            .join("llm-pyexec-cli")
            .to_string_lossy()
            .into_owned()
    });

    let mut group = c.benchmark_group("cold_start");
    group.sample_size(10);
    group.warm_up_time(Duration::from_millis(200));
    group.measurement_time(Duration::from_secs(5));

    for (name, snippet) in [("bench_01", SNIPPET_01), ("bench_05", SNIPPET_05)] {
        group.bench_function(name, |b| {
            b.iter(|| {
                use std::io::Write;
                let mut child = std::process::Command::new(&cli_path)
                    .stdin(std::process::Stdio::piped())
                    .stdout(std::process::Stdio::piped())
                    .stderr(std::process::Stdio::null())
                    .spawn()
                    .expect("Failed to spawn llm-pyexec-cli");
                if let Some(mut stdin) = child.stdin.take() {
                    let _ = stdin.write_all(snippet.as_bytes());
                }
                black_box(child.wait_with_output().ok())
            })
        });
    }
    group.finish();
}

// ---------------------------------------------------------------------------
// Group 2: cpython_cold_start — CPython subprocess (AC-10)
// ---------------------------------------------------------------------------

fn cpython_cold_start(c: &mut Criterion) {
    // Skip gracefully if python3 not found in PATH.
    if std::process::Command::new("python3")
        .arg("--version")
        .output()
        .is_err()
    {
        eprintln!("python3 not found; skipping cpython_cold_start benchmark");
        return;
    }

    let mut group = c.benchmark_group("cpython_cold_start");
    group.sample_size(10);
    group.warm_up_time(Duration::from_millis(200));
    group.measurement_time(Duration::from_secs(5));

    for (name, snippet) in [("bench_01", SNIPPET_01), ("bench_05", SNIPPET_05)] {
        group.bench_function(name, |b| {
            b.iter(|| {
                black_box(
                    std::process::Command::new("python3")
                        .arg("-c")
                        .arg(snippet)
                        .output()
                        .ok(),
                )
            })
        });
    }
    group.finish();
}

// ---------------------------------------------------------------------------
// Group 3: warm_throughput — All 5 snippets with pool pre-warmed (AC-11, AC-20)
// ---------------------------------------------------------------------------

fn warm_throughput(c: &mut Criterion) {
    use criterion::Throughput;

    // Pre-warm the pool singleton before benchmarking starts.
    // This ensures pool initialization overhead is not measured.
    let _ = InterpreterPool::global();

    let mut group = c.benchmark_group("warm_throughput");
    group.sample_size(50);
    group.warm_up_time(Duration::from_secs(2));
    group.measurement_time(Duration::from_secs(10));
    group.throughput(Throughput::Elements(1));

    let settings = ExecutionSettings::default();

    for (name, snippet) in [
        ("bench_01_arithmetic", SNIPPET_01),
        ("bench_02_string_ops", SNIPPET_02),
        ("bench_03_list_comprehension", SNIPPET_03),
        ("bench_04_dict_ops", SNIPPET_04),
        ("bench_05_json_roundtrip", SNIPPET_05),
    ] {
        group.bench_function(name, |b| {
            b.iter(|| execute(black_box(snippet), settings.clone()))
        });
    }
    group.finish();
}

// ---------------------------------------------------------------------------
// Criterion configuration and registration
// ---------------------------------------------------------------------------

criterion_group!(benches_cold_start, cold_start);
criterion_group!(benches_cpython_cold_start, cpython_cold_start);
criterion_group!(benches_warm_throughput, warm_throughput);
criterion_main!(
    benches_cold_start,
    benches_cpython_cold_start,
    benches_warm_throughput
);
