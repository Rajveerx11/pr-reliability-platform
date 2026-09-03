# Version-one evaluation report

> Deterministic harness replay. This is not a real model run and not single-agent quality evidence.
> Status checked 2026-09-03. A real current baseline still requires issues #36 and #14.
> Do not use this replay as a model-quality or production-readiness claim.

## Recorded run

- Mode: `deterministic_replay`
- Runner version: `1`
- Label: Full-cohort harness replay with no model findings
- Recorded: `2026-08-16T07:56:00Z`
- Evaluated commit: `8a74102156a6d95b40f5d6bd6dff5990cb3b6f5d`
- Repository status: the harness and its dependencies are now merged to `main`; this recorded run
  still applies only to the historical evaluated commit above
- Corpus fingerprint: `6023eb95d144931eeb8cd5fb7499bc11ed26ce7dde50be7f02fc11815ec9a6aa`
- Provider: unknown; no provider credentials were configured
- Model: unknown; no model was configured
- Environment: Windows 11 `10.0.26200`; Python `3.14.3`; Pydantic `2.13.4`

## Results

- Full cohort: 10/10
- Defect recall: unknown because no model attempt ran
- Reported-finding false-positive rate: unknown because no findings were reported and the
  denominator is zero
- Model run success rate: unknown because no model attempt ran
- p50/p95 latency: unknown
- Agent duration: unknown
- Usage coverage: 0.0%
- Input, output, and total tokens: unknown
- Exact reported cost: unknown
- Retries: 0; timeouts: 0

Every protected verifier rejected its broken fixture and accepted its reference fix.

| Task | Category | Agent status | TP | FP | FN |
|---|---|---:|---:|---:|---:|
| `python-authorization-002` | authorization | not run | unknown | unknown | unknown |
| `python-cache-invalidation-007` | state management | not run | unknown | unknown | unknown |
| `python-idempotency-001` | idempotency | not run | unknown | unknown | unknown |
| `python-money-rounding-008` | correctness | not run | unknown | unknown | unknown |
| `python-pagination-003` | edge case | not run | unknown | unknown | unknown |
| `python-path-traversal-006` | security | not run | unknown | unknown | unknown |
| `python-retry-limit-004` | reliability | not run | unknown | unknown | unknown |
| `python-timezone-009` | data integrity | not run | unknown | unknown | unknown |
| `python-webhook-dedup-010` | idempotency | not run | unknown | unknown | unknown |
| `python-webhook-signature-005` | security | not run | unknown | unknown | unknown |

## Limits and blockers

- The recorded replay predates the final merged integration. It cannot be treated as a current
  `main` model-quality baseline because no model attempt ran.
- Context token budget is unknown because context selection did not run.
- Protected verifier timeout was 10 seconds per invocation.
- Disposable sandbox was not enabled.
- The recorded replay did not execute the Proof of Work adapter. The adapter is now merged, but a
  new real-provider run must exercise the current pipeline.
- No provider or model credentials were available. The committed replay contains intentionally
  empty finding lists, not invented model output.
- No latency, duration, usage, or cost was measured. These values remain unknown, not zero.
- Frozen corpus has defect-seeded tasks but no clean negative tasks. Report defines the
  reported-finding false-positive rate as false findings divided by all reported findings. This
  replay has no reported findings, so the metric cannot be calculated.

This report proves harness reproducibility and full-cohort verifier execution only. A real
single-agent baseline remains blocked until one provider/model is configured and the frozen cohort
runs through the current merged pipeline.
