# Benchmarks

Run: `python3 -m nextclaw.cli bench` (also exercised by `tests/`).

The suite runs the real code in temp directories and reports:

| Benchmark | What it measures |
|---|---|
| `file_report_success` | End-to-end success + correctness (claimed totals == recomputed; fixture has known line counts) |
| `crash_recovery` | A `RuntimeError` is injected after step 0; resume must complete with verified output and without redoing done steps |
| `permission_denials` | Path traversal, absolute-path escape, destructive command, and unapproved high-risk exec are all denied |
| `memory_roundtrip` | remember -> recall -> forget with provenance |

Results are written to `data/bench_results.json`. The suite reports
`success_rate`, per-benchmark `duration_s`, and evidence (SHA-256, counts).

## Honesty rules

- Benchmarks measure **this MVP only**. There is no head-to-head comparison
  with OpenClaw yet; see [COMPARISON.md](COMPARISON.md).
- The injected crash is the only simulated part; everything else executes for real.
- Token/compute accounting is reported as wall time + step counts until LLM
  providers are enabled.
