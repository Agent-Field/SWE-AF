# Benchmark evaluator

Deterministic scorer for the benchmark rubric published in the root README
(Functional 30 / Structure 20 / Hygiene 20 / Git 15 / Quality 15). Run it
against any agent's output directory and get the same numbers anyone else
would:

```bash
python score.py <project_dir>              # Structure + Hygiene + Git (55 pts)
python score.py <project_dir> --run-tests  # + Functional (30 pts, executes npm test)
python score.py <project_dir> --json       # machine-readable report
```

Every check prints its evidence, so a disputed score is an argument about a
file listing or a git log, not about taste.

Honest boundaries, by design:

- **Quality (15 pts) is never auto-scored.** It is a judgment call; the
  script reports the inputs to that judgment (README, package metadata,
  custom error types) and assigns no points.
- **Unscorable ≠ zero.** The vendored artifacts in `../` had their `.git`
  stripped when they were copied in, so the Git dimension (and clean-status
  check) is reported as UNSCORABLE on them. Comparable scores must be
  produced from each agent's original output directory, including its
  `.git`.

Tested in `tests/test_benchmark_scorer.py`.
