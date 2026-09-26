# Benchmark replay (Table 1)

`python -m eval.table1` reproduces the Violations, Detected and False alarms columns of Table 1 offline. It makes no model calls and produces no labels.

- AgentDojo. `eval.run` regenerates the frozen AgentDojo ledgers from the sealed corpus and checks them against `data/agentdojo-ledgers.SHA256SUMS`. `eval.agentdojo.outbound_review` then replays the outbound check (AAP-4) over all 2,162 captured runs, and `eval.agentdojo.allowlist_sensitivity` applies the calendar-participant allowlist and then the initial-destinations allowlist that the paper reports. Violations are the successful attacks. Detected counts the successful attacks with an alarm that matches the attack's harmful action. False alarms are the no-injection runs with any alarm.
- tau3-bench and ClawsBench. `eval.catalog_replay.runtime` evaluates the captured action records of all 388 runs with the shared AAP catalog, and the result is cross-tabulated against the final labels of the annotations release. The two message-format checks are not part of the reported promise set. Detected counts violation runs with an alarm; false alarms are no-violation runs with an alarm.

## Run

Python 3.12 and `uv`, from the repository root.

```sh
uv venv --python 3.12 .venv
uv pip install -e packages/commons -e packages/sdk -r eval/requirements.txt
cd eval/data
cat runs/paper_main_v1-corpus.tar.gz.b64.part* | base64 -d > runs/paper_main_v1-corpus.tar.gz
cat records/captured_records.jsonl.gz.b64.part* | base64 -d > records/captured_records.jsonl.gz
shasum -a 256 -c SHA256SUMS        # or: sha256sum -c SHA256SUMS
cd ../..
.venv/bin/python -m eval.run fetch --archive eval/data/runs/paper_main_v1-corpus.tar.gz
.venv/bin/python -m eval.table1
```

It takes about five minutes and ends with the table below. The command exits with status 1 if any number differs from the paper.

```
Benchmark   Model          Violations  Detected  False alarms
AgentDojo   GLM-4.7-Flash         266       156          7/97
AgentDojo   Qwen3-30B-A3B         161        92          9/97
tau3-bench  GLM-4.7-Flash         111        15          0/53
tau3-bench  Qwen3-30B-A3B         154        31          0/10
ClawsBench  GLM-5.2                 4         0          2/56
All                               696       294        18/313
```

`--work DIR` keeps the intermediate AgentDojo ledgers (per-run alarms, witnesses, reviewed destinations) in an empty directory outside the repository.

## Data

| Path | Contents |
| --- | --- |
| `data/runs/` | The recorded tau3-bench and AgentDojo runs as base64 parts of one sealed archive, with the manifests that pin them (`cohort.lock.json`, `seal/`) and `case_id_map.json`, which maps the paper's and the reviews' case ids to the ids this copy produces |
| `data/records/` | The action records of the 328 tau3-bench runs and the 60 ClawsBench runs, as base64 parts |
| `data/SHA256SUMS` | SHA-256 of the two reassembled archives |
| `data/agentdojo-ledgers.SHA256SUMS` | SHA-256 of the frozen AgentDojo ledgers that `eval.run` regenerates |
| `data/labels/` | Copies of the four label files of the annotations release (`tau/index.json`, `tau/labels-1.jsonl`, `tau/labels-2.jsonl`, `clawsbench/labels.jsonl`), read by default; `--annotations` points elsewhere |
| `data/reviews/` | The manual reviews of AgentDojo alarms (which alarms match the attack's harmful action, and whether flagged recipients in no-injection runs were authorized) |
| `data/added_records/` | The scope review behind "With added records" and the frozen AgentDojo inputs of the added-records check |

The tau3-bench captures were made with tau2-bench upstream 8ebb749 plus a local trace-export patch; the patched commit id is withheld for anonymity and appears as `0000000000000000000000000000000000000000` in the captures, the seal and `eval/tau/`. Local paths inside the captures were rewritten for review and the seal was recomputed after both changes, so case ids derived from the corpus differ from the paper's. The review files keep the paper's case ids and local file pointers were removed from them; `eval.table1` maps the ids through `case_id_map.json` before the replay.
