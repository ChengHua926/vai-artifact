The verifier-only cumulative-prefix optimization reduced mean verification time for the original 10,000-record, every-10-record fixture from **34.9665 s to 0.4581 s (76.33×)**. Prefix validation itself fell from **34.5745 s to 0.07368 s (469.27×)**. All 60 workers reproduced the archived final digest and clean verdict; all 30 legacy/streaming pairs agreed on the complete verdict. Confidence: high for these measured fixtures and compatibility checks.

The experiment reused all six September 16 scaling fixtures and the original native seed records. Each cell and implementation received five fresh worker processes. Pairs and order within each pair were shuffled with seed 20260921; workers ran sequentially after other agent tests finished. Both arms used Python 3.12.13 and the same current packages, store, fixtures, and worker instrumentation. The only arm difference was the `aa_verifier.check_prefixes` binding.

| Records | Checkpoint cadence | Legacy total, mean s | Streaming total, mean s | Total ratio, legacy/streaming | Legacy prefix, mean s | Streaming prefix, mean s |
| ---: | --- | ---: | ---: | ---: | ---: | ---: |
| 100 | Final only | 0.011338 | 0.011547 | 0.982× | 0.000004 | 0.000004 |
| 100 | Every 10 records | 0.015532 | 0.011485 | 1.352× | 0.003442 | 0.000714 |
| 1,000 | Final only | 0.045415 | 0.041615 | 1.091× | 0.000022 | 0.000021 |
| 1,000 | Every 10 records | 0.356043 | 0.051922 | 6.857× | 0.314115 | 0.007198 |
| 10,000 | Final only | 0.393844 | 0.395603 | 0.996× | 0.000210 | 0.000206 |
| 10,000 | Every 10 records | 34.966512 | 0.458117 | 76.327× | 34.574451 | 0.073678 |

Ratios are ratios of arithmetic means, not means of per-pair ratios. At 10,000 records with checkpoints, total-time medians were 34.9200 s and 0.4602 s; observed ranges were 34.4141–35.6574 s and 0.4518–0.4657 s. Final-only means remained approximately 0.394–0.396 s at 10,000 records. The shorter cases contain visible HTTP/scheduling variability; five repetitions do not establish a general performance distribution.

At 10,000 records with 1,000 cumulative checkpoints, the phase breakdown confirms that the change removes repeated prefix work:

| Measurement | Legacy mean | Streaming mean |
| --- | ---: | ---: |
| HTTP retrieval | 0.292569 s | 0.285068 s |
| Final trace hash | 0.067241 s | 0.067758 s |
| Prefix validation | 34.574451 s | 0.073678 s |
| Predicate evaluation | 0.019919 s | 0.019645 s |
| Total verification | 34.966512 s | 0.458117 s |
| Worker CPU time | 34.653404 s | 0.215557 s |
| Peak worker RSS | 1,043.05 MiB | 170.24 MiB |

Total verification includes other `process_challenge` work outside the four phase timers. HTTP retrieval includes both records and the promise. The worker calls the actual current verifier against the actual local FastAPI/SQLite store. Chain reads use the original fixture escrow; there is no blockchain RPC or transaction. Real settlement latency is absent, while the stub call remains inside total time. Peak RSS is the fresh worker's lifetime high-water mark, including imports and fixture loading, and excludes the separate store process. It is not a measurement of only the prefix helper's allocations.

The original published September 16 result remains **36.5370519332 s**, conventionally reported as 36.54 s, for the same 10,000-record checkpointed fixture. It has not been replaced or relabeled. The 34.9665 s value is the new legacy rerun on September 21. The claimed 76.33× comparison uses the two arms of this new paired experiment. Historical raw results, summary, metadata, seed records, fixtures, and source archive were copied byte-for-byte into [paired-run/inputs](/workspace/archive/2026-09-21-verifier-prefix-optimization/paired-run/inputs); the original archive files were also checked unchanged after the run.

The new helper in [prefixes.py](/workspace/vai/packages/verifier/aa_verifier/prefixes.py) encodes each record once through the last checkpoint. It maintains the existing canonical JSON list without its closing bracket, copies the Keccak state at each checkpoint, appends `]` to the copy, and compares the existing root. `safe-pysha3==1.0.5` provides an actual fixed-size state copy: its C implementation copies `sizeof(SHA3_state)`, rather than replaying accumulated input. The package source is preserved in [safe_pysha3-1.0.5.tar.gz](/workspace/archive/2026-09-21-verifier-prefix-optimization/backend-source/safe_pysha3-1.0.5.tar.gz), SHA-256 `88ceaad6af4b6bdecd2f54b31ad0e5e5e210d4f5ecabb1bd1fd3539ad61b7bf1`.

The legacy validator and `trace_hash` were checked against the archived source by AST equality. The SDK, commitment bytes, checkpoint roots, and paper were untouched. The backend dependency belongs to the verifier package. The measured legacy hashing path imports **`Crypto.Hash.keccak` directly through `aa_commons.ids`**. The original run metadata's `hash_backends.legacy_backend` field instead named the auxiliary `eth_hash` environment backend; [metadata-addendum.json](/workspace/archive/2026-09-21-verifier-prefix-optimization/paired-run/metadata-addendum.json) records this correction and the actual legacy source/binary hashes. Original metadata, raw rows, and measured source snapshots remain intact. The runner's metadata helper was corrected after measurements; its worker instrumentation and scheduling were unchanged.

Validation passed in both environments: **221 tests plus 57 subtests** on Python 3.13 and the same **221 tests plus 57 subtests** on pinned Python 3.12. The Python 3.12 output is in [validation-python312.txt](/workspace/archive/2026-09-21-verifier-prefix-optimization/validation-python312.txt). As a direct control, all **20 existing end-to-end binding tests** also passed with the verifier binding replaced by the legacy helper; see [validation-legacy-binding.txt](/workspace/archive/2026-09-21-verifier-prefix-optimization/validation-legacy-binding.txt). The four-worker, 100-record smoke also passed, and `git diff --check` was clean. The full experiment verified source immutability during measurement and digest/verdict agreement after all workers completed.

The reproducible runner is [compare_verifier_prefixes.py](/workspace/vai/scripts/practicality/compare_verifier_prefixes.py). It SHA-checks the archived measurement source (`4445b92268c814c9f494eff10e302b7069fb556287ee0187dcfdc3ab3de00bb6`) and executes only its unchanged `verify_worker`, `expanded_native_records`, `elapsed`, and `rss_bytes` function definitions. It does not execute archived configuration, lifecycle setup, or `main`. A hook records the returned final digest after the original phase timer without hashing it again; its small observation overhead lies inside total verification for both arms.

Use a new output directory to reproduce the full run with the existing isolated backend installation:

```sh
cd /workspace/vai
/workspace/hermes-agent/.venv/bin/python \
  scripts/practicality/compare_verifier_prefixes.py \
  --output /workspace/archive/2026-09-21-verifier-prefix-optimization/paired-rerun \
  --dependency-path /workspace/archive/2026-09-21-verifier-prefix-optimization/python312-dependencies \
  --sizes 100 1000 10000 --repetitions 5 --seed 20260921
```

The pinned dependency can be installed into a fresh isolated target using that interpreter's `pip install --target` and the archived `safe_pysha3-1.0.5.tar.gz`; pass that target with `--dependency-path`. A local loopback listener must be permitted. The runner never reads wallet files or invokes chain clients.

Evidence: [60 raw worker rows](/workspace/archive/2026-09-21-verifier-prefix-optimization/paired-run/paired-raw.jsonl), [complete statistics and paired differences](/workspace/archive/2026-09-21-verifier-prefix-optimization/paired-run/paired-summary.json), [environment and source/input hashes](/workspace/archive/2026-09-21-verifier-prefix-optimization/paired-run/metadata.json), [randomized schedule](/workspace/archive/2026-09-21-verifier-prefix-optimization/paired-run/schedule.json), and [measured source snapshot](/workspace/archive/2026-09-21-verifier-prefix-optimization/paired-run/source-snapshot).

Raw SHA-256: `8f8a5b0c4656ced5d2109053eda83e98dd42813ae63b3aca791af990db655dc2`.
Summary SHA-256: `5c3c1910b1202beabd1d45b97e8dada847a33dcdc9af0447741c1913922a8c5d`.
