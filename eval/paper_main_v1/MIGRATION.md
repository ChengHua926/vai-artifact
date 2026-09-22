# `paper_main_v1` evaluation migration record

This directory records the immutable comparison baseline before evaluator relocation and the newly generated canonical evidence. The old comparison bundle was the untracked pre-consolidation `eval/results/paper_main_v1/`; after the merge it was relocated to the external research archive, and its two files not byte-identical to the committed bundle (the old-layout `index.json` and the pre-correction `tau/monitor_manifest.jsonl`) are preserved with checksums under `agent_accountability_archive/2026-08-18-eval-consolidation/legacy-results-unique/`.

## Preservation points

| Purpose | Tag | Resolved commit |
| --- | --- | --- |
| Current main layout | `pre-consolidation-main-2026-08-18` | `6211686c68e4f6cc144640678dc1e70f0ac4799c` |
| Causal-audit layout | `pre-consolidation-causal-audit-2026-08-18` | `4bf69fb4856c671eec817167b5e334ac03c89ed7` |
| Tau dependency | `paper-main-v1-tau-2026-08-18` | `7ac89f5128bc9ff86e37cf49a54687b35083e598` |

The local tags were verified and left unchanged. No network operation was performed. The prior SSH fetch failed with `Permission denied (publickey)`; remote preservation remains pending authentication.

## Imported evaluator history

The reviewed `paper-main-eval` commits were cherry-picked in this order, without modifying that source branch: `a8c7b4d`, `86c5476`, `39e3294`, `5236153`, `5079499`, `e10abc8`, `fd22442`.

`baseline.json` retains the baseline index artifact hashes and ledger counts, plus complete AgentDojo and Tau summary objects. Its root checks are index `7f2f0015d67c0096c6ad909c6944d73d6e56b07a452e106295c5abbd39686f75`, root sums `e6989fa247c64adc62093fcd6dfadac10a101bcea6df7f2b4bc39c321e0d177d`, AgentDojo summary `89139e567603a74cd3d126c7555f0fa2d0c539490f0703e29fc80c748c5f9aec`, and Tau summary `cdb4018ba56d8ac9e0f27ed05357ded56856468d7feb684867f66e82712378e9`.

## Headline baseline

- AgentDojo: 2,162 cases, 2,157 graded, 5 protocol errors; GLM exact egress catches 125/125 and Qwen 70/70.
- Tau: 328 cases, 74 fires, zero replay/parity mismatches, 8 exact failed runs, and 9 exact fire rows.

## Dataset archive and restore

The ignored corpus was copied byte-for-byte into `eval/paper_main_v1/corpus/` and authenticated by the production loader: 14 accepted shards, 19 accepted/rejected roots, 11,359 listed evidence files, and 373,750,290 evidence bytes. Two archive builds produced identical bytes.

- Local archive: `paper_main_v1-corpus.tar.gz`
- External location: `agent_accountability_archive/2026-08-18-eval-consolidation/` (a sibling of the repository)
- SHA-256: `2a4a8988b7f34c446861b9eec22394289c8ffb89c12dc4ae1b9a2182f9205b25`
- Byte size: `48,037,974`
- Release URL: not configured (`null`)
- Standalone production fetch and verification: PASS for all 19 roots

The external archive also contains a manifest-verified byte copy and restore copy of the legacy `eval/corpus` tree, plus the two untracked causal-worktree documents with verified hashes. Source corpora and the causal worktree remain untouched. No source was moved to Trash; that reversible step is deferred until this integration branch is approved and merged.

## Pre-merge removal safety

The anchored ignores for `eval/corpus/`, `eval/cohorts/`, `eval/data/`, `eval/results/`,
`eval/coverage/`, `eval/archive/`, and `eval/smoke/` are transitional safeguards for untracked local
bytes only; none is an active source location. Before approval and merge, leave any surviving local
copies in place and do not move them to Trash. After approval and merge, first reverify the external
`legacy-eval-corpus/SHA256SUMS`, its empty-directory restore copy, the paper archive hash/size above,
and the canonical 19-root loader. Only then may those exact legacy roots be moved to Trash. Emptying
Trash remains a separate, unapproved action.

## Generated evidence comparison

The canonical managed output and a second generation into an empty directory were byte-identical. Every AgentDojo ledger and summary is byte-identical to the old bundle. Every substantive Tau ledger and summary is also byte-identical; only `tau/monitor_manifest.jsonl` reflects the approved module relocation, with the resulting child and root checksums. `index.json` additionally records the relocated canonical source paths and source hashes.

The committed headline gates are 2,162 AgentDojo cases, 2,157 graded, 5 protocol errors, GLM 125/125 and Qwen 70/70 exact catches; and 328 Tau cases, 2,333 calls, 900 effects, 74 fires, zero replay/parity errors, 8 exact failed runs, and 9 exact fire rows.
