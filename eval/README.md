# Evaluation

## Official workflow

The supported evaluation interface is the repository package CLI. The sealed corpus ships with this
artifact as `corpus/paper_main_v1-corpus.tar.gz` (Tau and AgentDojo captures) and
`corpus/clawsbench-standard60-corpus.tar.gz` (the ClawsBench replay inputs). Restore them, then run
everything else offline:

```bash
python -m eval.run fetch --archive corpus/paper_main_v1-corpus.tar.gz
python -m eval.run verify
python -m eval.run evaluate --benchmark all
mkdir -p eval/clawsbench && tar xzf corpus/clawsbench-standard60-corpus.tar.gz -C eval/clawsbench
```

`fetch` installs and verifies the archive; it is the only command with any network code path, and
a direct archive URL will be configured only if the repository becomes public. `verify` and
`evaluate` operate on the sealed cohort described by `eval/paper_main_v1/cohort.lock.json`.
Canonical benchmark packages are `eval/agentdojo`, `eval/tau`, and `eval/clawsbench`; new paper
evidence belongs under `eval/paper_main_v1`.

## Local ClawsBench viewer

ClawsBench capture, repair, analysis, policies, tests, and viewer export live under
`eval/clawsbench`. Its sealed raw corpus defaults to the ignored `eval/clawsbench/corpus`, and
generated viewer manifests default to the ignored `eval/clawsbench/data`. Both locations can be
overridden with `CLAWSBENCH_CORPUS_ROOT` and `CLAWSBENCH_VIEWER_DATA` or the export CLI flags.

```bash
python -m eval.clawsbench.export
cd eval/viewer/app
npm run dev
```

The shipped viewer exposes ClawsBench only. It uses the bounded local API when the sealed corpus
exists and otherwise falls back to the tracked published ClawsBench manifests.

Historical evaluation writeups remain under `eval/writeups` with an explicit status notice. Paper
and writing repository updates await user approval.
