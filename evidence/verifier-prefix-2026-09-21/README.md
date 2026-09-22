This evidence records a paired rerun of the native verifier scaling experiment on September 21, 2026. At 10,000 records with a cumulative checkpoint every 10 records, mean total verification time changed from **34.9665 s to 0.4581 s (76.33×)**; mean prefix validation changed from **34.5745 s to 0.07368 s (469.27×)**. All 60 workers reproduced the expected final digest and clean verdict, and all 30 pairs agreed on the complete verdict.

| Records | Cadence | Legacy total, mean s | Streaming total, mean s |
| ---: | --- | ---: | ---: |
| 100 | Final only | 0.011338 | 0.011547 |
| 100 | Every 10 records | 0.015532 | 0.011485 |
| 1,000 | Final only | 0.045415 | 0.041615 |
| 1,000 | Every 10 records | 0.356043 | 0.051922 |
| 10,000 | Final only | 0.393844 | 0.395603 |
| 10,000 | Every 10 records | 34.966512 | 0.458117 |

Each cell has five fresh processes per implementation. Pairs and implementation order within each pair were shuffled with seed 20260921; workers ran sequentially. Both arms used Python 3.12.13, the same six archived fixtures/native seed records, and the same current verifier and HTTP store. Only the verifier's `check_prefixes` binding changed. Ratios are ratios of arithmetic means. The original September 16 **36.5371 s** result remains a separate historical measurement; the reported 76.33× compares the two September 21 arms.

The runner executes the original archived `verify_worker` function against actual `process_challenge` and local FastAPI/SQLite HTTP retrieval. Chain reads are fixture values; settlement is a stub. Total time includes the stub call, but no real RPC, chain transaction, or settlement latency. The recorded phase timers cover retrieval, final hashing, prefix validation, and predicate evaluation; other verifier work remains inside total time. An identical observation hook captures the returned final digest after the original phase timer, without hashing again.

At 10,000 records with checkpoints, mean peak worker RSS changed from **1,043.05 MiB to 170.24 MiB**. RSS is the fresh worker's lifetime high-water mark, including imports and fixture loading, and excludes the separate store process. Five repetitions characterize these fixtures; the data do not establish a performance distribution for other workloads.

The streaming helper preserves the existing cumulative canonical-JSON-list commitments. It encodes each record once through the last checkpoint, copies the fixed-size Keccak state, and appends the closing list bracket to the copy before checking each root. Its verifier-only dependency is **`safe-pysha3==1.0.5`**. Legacy hashing uses `Crypto.Hash.keccak` directly through `aa_commons.ids`; read `metadata-addendum.json` with `metadata.json`, whose original backend field named the auxiliary `eth_hash` environment backend. The addendum also records the post-run metadata-label correction in the runner. Original measurement records were preserved unchanged.

The measurement checkout was based on commit `6e94a24d41e32f2dec343346e127d060ac436d81` with the optimization still uncommitted. That base commit alone does not identify the measured optimization: `metadata.json` records the exact SHA-256 of each source file. Relevant pins are:

- Measured `packages/verifier/aa_verifier/prefixes.py`: `2941d450ff2cc0a93bbd8a8b4fad732109374f4133d5e8ec31c619a965642a6a`.
- Original archived `scripts/practicality/measure_local.py`: `4445b92268c814c9f494eff10e302b7069fb556287ee0187dcfdc3ab3de00bb6`.
- Original source tar `local-measurement-source-278bd53.tar.gz`: `3b6a45604910bb19095b6208ce02b9deb710f3b57c12fe0d9c2221a5c7cc8694`.
- Measured runner: `0fa224585bfe92eeed22b2af534b15293f7d5f5f7dd47c4706c288d2da7e1d4f`; runner after metadata correction: `26d4f2a896e8d9f8ab741d1391b4a8794aa08beb7704be0591f8571ddb52a0fc`.

The runner checks the original measurement source hash and AST equality of legacy `trace_hash` and `check_prefixes`. Source files were unchanged during the 60-worker run. All fixture hashes and package versions are in `metadata.json`. `validation-python312.txt` records **221 tests plus 57 subtests passed**; `validation-legacy-binding.txt` records **20 existing binding tests passed** with the legacy helper substituted as a direct control.

This folder contains seven exact text copies from the local archive: `paired-raw.jsonl`, `paired-summary.json`, `metadata.json`, `metadata-addendum.json`, `schedule.json`, and the two validation logs. It does not contain corpus records, databases, binaries, package/source tarballs, or the original fixture archive. The full local archive is:

```text
/workspace/archive/2026-09-21-verifier-prefix-optimization/
```

The original inputs remain local under:

```text
/workspace/archive/2026-09-16-practicality-and-agentdojo/practicality/
```

No public download of that original fixture archive is supplied by this evidence folder. Reproducing the exact run requires access to those original inputs; the evidence files alone are insufficient to reconstruct them. The current runner expects `native-seed-records.json`, the six `scaling-fixture-*.json` files, and the historical `metadata.json`, `scaling-raw.jsonl`, and `scaling-summary.json` under `--fixtures`, plus the archived measurement source tar under `--source-archive`.

From the repository root, with Python/dependencies matching `metadata.json`, use a new output directory:

```sh
python scripts/practicality/compare_verifier_prefixes.py \
  --fixtures /path/to/practicality/local-performance \
  --source-archive /path/to/practicality/source-snapshots/local-measurement-source-278bd53.tar.gz \
  --output /path/to/new-paired-output \
  --sizes 100 1000 10000 --repetitions 5 --seed 20260921
```

If `safe-pysha3` is installed in a separate `pip --target` directory, add `--dependency-path /path/to/that-target`. The runner requires a local loopback listener and refuses an existing output directory. It reads no wallet files and invokes no blockchain client.

Raw SHA-256: `8f8a5b0c4656ced5d2109053eda83e98dd42813ae63b3aca791af990db655dc2`.
Summary SHA-256: `5c3c1910b1202beabd1d45b97e8dada847a33dcdc9af0447741c1913922a8c5d`.
