# Extension labeling round

Committee labels for the runs the calibration round did not cover: the 298
Tau runs of the sealed cohort that are not among the 30 in `selection_v1.json`,
and the 30 ClawsBench tasks not in the calibration selection. Same three
models, same frozen prompts (`prompts/tau_v1.md`, `prompts/clawsbench_v1.md`),
same request parameters as the calibration round. No human labels here: the
calibration round's per-labeler error rates are what a later aggregation
applies to these votes.

Files:

- `selection.json` (tracked). `python -m eval.labeling.select_tasks --extension
  [--check]`. Tau: the complement of `selection_v1.json` by `case_id`, sorted
  by `case_id`, shuffled once with seed 20260909, ids `ext-001..ext-298`, so a
  `--limit` prefix is an unbiased sample. Each row keeps the mapping to model,
  domain and task exactly as `selection_v1.json` does. ClawsBench: the sorted
  complement of the calibration 30. `derived_from` records the sha256 of the
  `selection_v1.json` bytes the complement was taken against.
- `tau/` (gitignored). The blind bundle, `index.json` + `tasks/ext-NNN.json`,
  10.1 MB, deterministic from the sealed corpus (`python -m eval.run fetch
  --archive`) via `python -m eval.labeling.build_tau_bundle --selection
  eval/labeling/extension/selection.json --out eval/labeling/extension/tau
  --label "Tau extension set" --selection-file
  eval/labeling/extension/selection.json`. Its digest is tracked in
  `tau_bundle.sha256` and repeated in every Tau run manifest
  (`committee.bundle_digest`: sha256 over `index.json`, then each
  `tasks/*.json` by name, each as name, NUL, bytes).
- `ledgers/<bench>/`: `<model>.jsonl` (append-only vote ledgers),
  `consensus.json` (the plain 2-of-3 record), `run_manifest.json`,
  `SHA256SUMS`.

Instrument note. The calibration round's Tau bundle cut tool results at 1000
characters (19 of the 30 runs had at least one cut result; the committee and
both humans labeled that text). This round uses full tool results: the run
manifest records `truncated_steps: 0` under `tau_bundle`, and the bundle was
built with `result_limit=None`. ClawsBench prompts were never cut. Decision
of 2026-09-09: label the extension on full results and do not re-run the
calibration 30.

Ledger rules. Rows are appended the moment they land, in completion order,
and a ledger is never rewritten. A (model, task) pair can therefore carry an
error row followed by an ok row; readers take the last ok row per pair
(`committee.resolve_rows`). Every row records the sha256 and length of the
prompt actually sent, start and finish timestamps, the serving provider, the
response model, attempts, transport retries, the cost billed across all
attempts (`billed_cost_usd`), and the full raw reply plus reasoning text where
the provider exposes it.

Run: `python -m eval.labeling.committee --bench <tau|clawsbench> --stage
extension --workers 4 --max-cost <usd>`. Re-running the same command resumes:
ok rows are skipped, error rows are retried. `--preflight` prints the
OpenRouter balance; `--dry-run` prints prompt digests without any network use.
