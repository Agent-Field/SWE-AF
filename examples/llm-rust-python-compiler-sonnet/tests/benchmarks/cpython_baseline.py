#!/usr/bin/env python3
"""
CPython baseline benchmark for the 5 canonical LLM-output snippets.
Uses timeit for warm-path measurement. Prints per-snippet timing.
Run: python3 tests/benchmarks/cpython_baseline.py
"""
import timeit

SNIPPETS = {
    "bench_01_arithmetic": "result = sum(i * i for i in range(1000))",
    "bench_02_string_ops": (
        'words = "the quick brown fox".split()\n'
        'result = " ".join(w.capitalize() for w in words)'
    ),
    "bench_03_list_comprehension": (
        "matrix = [[i * j for j in range(10)] for i in range(10)]\n"
        "flat = [x for row in matrix for x in row if x % 3 == 0]"
    ),
    "bench_04_dict_ops": (
        'freq = {}\n'
        'for ch in "hello world": freq[ch] = freq.get(ch, 0) + 1\n'
        'result = sorted(freq.items(), key=lambda x: -x[1])'
    ),
    "bench_05_json_roundtrip": (
        "import json\n"
        'data = json.loads(\'{"key": [1, 2, 3], "flag": true}\')\n'
        'result = json.dumps(data, sort_keys=True)'
    ),
}

NUMBER = 1000

for name, stmt in SNIPPETS.items():
    elapsed = timeit.timeit(stmt, number=NUMBER)
    mean_us = (elapsed / NUMBER) * 1_000_000
    print(f"{name}: {mean_us:.2f}µs (mean over {NUMBER} iterations)")
