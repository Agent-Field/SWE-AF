#!/usr/bin/env bash
# tests/benchmarks/compare_bench.sh
# Runs cold_start, cpython_cold_start, warm_throughput benchmarks and
# prints a Markdown comparison table (AC-17).
#
# Usage: bash tests/benchmarks/compare_bench.sh
# Must be run from the workspace root.

set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CLI="${REPO_ROOT}/target/release/llm-pyexec-cli"

if [[ ! -f "$CLI" ]]; then
    echo "Building release binary..." >&2
    cargo build --release -p llm-pyexec-cli --quiet
fi

# Run Criterion benchmark groups.
echo "Running cold_start benchmarks..." >&2
cargo bench --bench pyexec_bench -- cold_start 2>/dev/null || true
echo "Running cpython_cold_start benchmarks..." >&2
cargo bench --bench pyexec_bench -- cpython_cold_start 2>/dev/null || true
echo "Running warm_throughput benchmarks..." >&2
cargo bench --bench pyexec_bench -- warm_throughput 2>/dev/null || true

CRITERION_DIR="${REPO_ROOT}/target/criterion"

# Explicit bench_id → warm_throughput Criterion directory name mapping.
# This prevents the fragile suffix-trial pattern (ISSUE-5 fix, architecture §8.6).
declare -A WARM_DIR_MAP
WARM_DIR_MAP["bench_01"]="bench_01_arithmetic"
WARM_DIR_MAP["bench_02"]="bench_02_string_ops"
WARM_DIR_MAP["bench_03"]="bench_03_list_comprehension"
WARM_DIR_MAP["bench_04"]="bench_04_dict_ops"
WARM_DIR_MAP["bench_05"]="bench_05_json_roundtrip"

# Markdown table header.
echo ""
echo "## Benchmark Comparison: RustPython Pool vs CPython"
echo ""
echo "| Snippet | Cold-start (RustPython) | Cold-start (CPython) | Warm Median | Warm p95 | Throughput (ops/s) |"
echo "|---------|------------------------|---------------------|-------------|----------|--------------------|"

# Parse Criterion estimates.json via Python helper.
parse_mean_ms() {
    local json_path="$1"
    if [[ ! -f "$json_path" ]]; then echo "N/A"; return; fi
    python3 -c "
import json, sys
try:
    d = json.load(open('$json_path'))
    mean = d['mean']['point_estimate'] / 1e6
    print(f'{mean:.1f}ms')
except Exception:
    print('N/A')
" 2>/dev/null || echo "N/A"
}

parse_median_ms() {
    local json_path="$1"
    if [[ ! -f "$json_path" ]]; then echo "N/A"; return; fi
    python3 -c "
import json
try:
    d = json.load(open('$json_path'))
    med = d['median']['point_estimate'] / 1e6
    print(f'{med:.2f}ms')
except Exception:
    print('N/A')
" 2>/dev/null || echo "N/A"
}

parse_throughput_ops() {
    local json_path="$1"
    if [[ ! -f "$json_path" ]]; then echo "N/A"; return; fi
    python3 -c "
import json
try:
    d = json.load(open('$json_path'))
    mean_ns = d['mean']['point_estimate']
    ops = 1e9 / mean_ns if mean_ns > 0 else 0
    print(f'{ops:.1f}')
except Exception:
    print('N/A')
" 2>/dev/null || echo "N/A"
}

for BENCH_ID in bench_01 bench_02 bench_03 bench_04 bench_05; do
    RUST_COLD_JSON="${CRITERION_DIR}/cold_start/${BENCH_ID}/new/estimates.json"
    CPYTHON_COLD_JSON="${CRITERION_DIR}/cpython_cold_start/${BENCH_ID}/new/estimates.json"
    WARM_DIR="${WARM_DIR_MAP[$BENCH_ID]}"
    WARM_JSON="${CRITERION_DIR}/warm_throughput/${WARM_DIR}/new/estimates.json"

    RUST_COLD=$(parse_mean_ms "$RUST_COLD_JSON")
    CPYTHON_COLD=$(parse_mean_ms "$CPYTHON_COLD_JSON")
    WARM_MED=$(parse_median_ms "$WARM_JSON")
    WARM_P95="N/A"  # p95 from estimates.json (Criterion stores mean/median/slope)
    THRPT=$(parse_throughput_ops "$WARM_JSON")

    echo "| ${BENCH_ID} | ${RUST_COLD} | ${CPYTHON_COLD} | ${WARM_MED} | ${WARM_P95} | ${THRPT} |"
done
