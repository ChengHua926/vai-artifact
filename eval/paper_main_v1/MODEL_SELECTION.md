# Model selection for `paper_main_v1`

The selection rationale below was reconstructed on 2026-08-16 from the saved screening artifacts.
The artifacts record endpoints and benchmark outcomes; they do not record the original
deliberation.

The AgentDojo screen used v1.2.2 with AgentDojo 0.1.35: 70
`important_instructions` attack cases and 8 benign cases across banking, workspace, Slack, and
travel. The Tau screen used 12 fixed tasks: 6 airline and 6 retail. Every value below is a
benchmark-native verdict. No promise result or coverage result was used.

| Model | Response alias | Provider | Endpoint revision | AgentDojo attack success | AgentDojo attack utility | AgentDojo benign utility | Tau pass |
| --- | --- | --- | --- | ---: | ---: | ---: | ---: |
| **GLM 4.7 Flash** | `z-ai/glm-4.7-flash` | `deepinfra/bf16` | `z-ai/glm-4.7-flash-20260119` | **21/70** | **51/70** | **6/8** | **10/12** |
| **Qwen3 30B A3B** | `qwen/qwen3-30b-a3b` | `alibaba` | `qwen/qwen3-30b-a3b-04-28` | **20/70** | **51/70** | **7/8** | **5/12** |
| DeepSeek V4 Flash 0731 | `deepseek/deepseek-v4-flash-0731` | `gmicloud/fp8` | `deepseek/deepseek-v4-flash-20260731` | 0/70 | 61/70 | 8/8 | 12/12 |
| GPT-5.6 Luna | `openai/gpt-5.6-luna` | `openai` | `openai/gpt-5.6-luna-20260709` | 0/70 | 54/70 | 6/8 | 10/12 |
| DeepSeek V4 Flash 0423 | `deepseek/deepseek-v4-flash` | `gmicloud/fp8` | `deepseek/deepseek-v4-flash-20260423` | 4/70 | 60/70 | 8/8 | not screened |
| Muse Glimmer 30B | `meta/muse-glimmer-30b` | `deepinfra/bf16` | `meta/muse-glimmer-30b-20260810` | 1/70 | 61/70 | 7/8 | not screened |

GLM and Qwen were selected because both produced enough successful AgentDojo attacks to study while
still completing 51/70 attacked user tasks. Qwen also supplied substantial Tau failure variation
(7/12 failures), while GLM supplied a contrasting Tau profile (2/12 failures). The alternatives
produced zero to four AgentDojo attack successes in the saved screen, so they offered much less
failure evidence for this study.

This is a failure-rich stress cohort. It is not a representative model sample, a leaderboard
comparison, or evidence about promise coverage.

## Saved evidence

The screening directories lived under the legacy `eval/corpus/` tree, which was removed from the
working layout during consolidation. They remain available from two places: the manifest-verified
archive copy in `agent_accountability_archive/2026-08-18-eval-consolidation/legacy-eval-corpus/files/`
(checked against its `SHA256SUMS`), and in Git via the preservation tag, e.g.
`git show pre-consolidation-main-2026-08-18:eval/corpus/screen_20260812_glm47_v2/manifest.json`.

AgentDojo screen manifests (paths relative to either location above):

- `screen_20260812_glm47_v2/manifest.json`
- `screen_20260812_deepseek_v4/manifest.json`
- `screen_20260813_luna56_smoke_v2/manifest.json`
- `screen_20260815_deepseek0423/manifest.json`
- `screen_20260815_muse_glimmer/manifest.json`
- `screen_20260815_qwen3_30b/manifest.json`

Tau screen results and endpoint manifests. These are untracked local evidence in the sibling
`tau2-explore` checkout under `data/simulations/`, and a checksummed copy is preserved in
`agent_accountability_archive/2026-08-18-eval-consolidation/tau-screen-simulations/`
(verified against its `SHA256SUMS`):

- `screen_20260812_glm47/`
- `screen_20260812_deepseek_v4/`
- `screen_20260812_luna56/`
- `screen_20260815_qwen3_30b/`

These screening directories are local evidence; the full selected runs are the roots bound by
[`cohort.lock.json`](cohort.lock.json).
