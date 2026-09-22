# `paper_main_v1`

This is the source corpus for new AgentDojo and Tau paper evaluation. Start from
[`cohort.lock.json`](cohort.lock.json), not from a directory glob, an old normalized file, or a
viewer export.

The raw corpus lives under `corpus/` and is intentionally gitignored. The tracked lock and seal
files establish its exact membership and verify every accepted leaf:

```bash
# from the repository root
python -m eval.run verify
```

Archive metadata lives in [`dataset.json`](dataset.json). The verified archive is
`paper_main_v1-corpus.tar.gz`, SHA-256
`2a4a8988b7f34c446861b9eec22394289c8ffb89c12dc4ae1b9a2182f9205b25`, with
`48,037,974` bytes. It is published as a GitHub Release asset (tag
`paper-main-v1-corpus-2026-08-18`) and also kept outside Git in the research archive directory
(`agent_accountability_archive/2026-08-18-eval-consolidation/`, a sibling of this repository).
While the repository is private the asset requires an authenticated download, so `dataset.json`
carries no direct HTTPS URL (`url: null`) and bare `fetch` fails closed:

```bash
gh release download paper-main-v1-corpus-2026-08-18 \
  -R anon/agent_accountability_prototype -p 'paper_main_v1-corpus.tar.gz'
python -m eval.run fetch --archive paper_main_v1-corpus.tar.gz
```

A production-path restore into an empty standalone
dataset root passed verification of all 19 accepted and rejected roots.

The sealed capture contains 14 accepted roots and 5 rejected roots. Accepted membership is 2,162
AgentDojo cells and 328 Tau simulations, with USD 8.08406139 in captured accepted-run cost. The
original split capture is preserved under `seal/original-split-capture/`; `seal/current/` defines
the paths used now.

## Membership rules

- `accepted_shards` in the current seal are the only evaluation inputs.
- Rejected roots preserve superseded or invalid attempts for provenance. The loader never returns
  them.
- Five Qwen AgentDojo cells ended in `model_protocol_error`. They remain accepted captured cells
  with null benchmark verdicts; later analysis must report them separately, not rerun or coerce
  them.
- Legacy normalized JSON, coverage JSON, figures, and viewer JSON are derived artifacts. They do
  not define corpus membership.

The committed `agentdojo/` and `tau/` ledgers, summaries, child checksum manifests, root
`index.json`, and root `SHA256SUMS` are the deterministic paper evidence generated from this
cohort. Two complete generations were byte-identical. Their substantive rows and summaries are
identical to the sealed comparison bundle; only relocated source provenance, the Tau monitor
manifest relocation, and enclosing checksums changed. The supported evaluator is
`python -m eval.run evaluate --benchmark ...`; older raw corpora, normalized files, and evaluator
outputs are historical, not defaults for new paper results.

Headline verification remains: 2,162 AgentDojo cases, 2,157 graded, 5 protocol errors, GLM
125/125 and Qwen 70/70 exact catches; 328 Tau cases, 2,333 calls, 900 effects, 74 fires, zero
replay/parity errors, 8 exact failed runs, and 9 exact fire rows.

See [`MODEL_SELECTION.md`](MODEL_SELECTION.md) for the model and endpoint record.
