# Project profile — commonsense-atlas (for the planning agent)

Read this before estimating a task. It describes what the repository *is*, where
work actually happens, and what "done" costs in practice.

## Goal

PhD thesis: build a commonsense knowledge graph from ~1.8M Gemini-2.5-Flash
sentences generated from WordNet synsets (10–35 sentences per synset), then use
it to study LLM inference over the graph. Everything is local-first; heavy LLM
work runs on the CINECA **Leonardo** HPC via SLURM, data syncs to a Mac.

## Pipeline (four phases — a task almost always touches one)

1. **SoTA SRL + WSD (phase 1)** — `external/multi-srl` (VerbAtlas frames),
   `src/wsd/consec_enrichment.py` + `external/consec` (ConSeC). Produces the
   parsed/disambiguated KB. SLURM: `consec_enrichment.sh`, `kb_multi-srl_test.sh`.
   Model: checkpoint `ontosemcorgemini_2_nn/epoch=15-f1=0.8864`.
2. **LLM improvement chain (phase 2)** — `src/improvement/{bridge_frame_gap,
   srl_reinforcement, head_word_wsd, generate_frame_definitions}.py` and
   `src/wsd/consec_enrichment_for_llm.py`. Battery wrappers `battery_*.sh`
   driven by `llm_battery_chain.sh`; run folders `data/battery/<run>/`.
   Adds `llm_predictions` next to `predictions`.
3. **Coreference (phase 3)** — `src/coreference/{filter,resolve,itemize,consec,
   flatten}.py`, launcher `run_coref_pipeline.sh`; per-arm run folders
   `data/interim/coref_stage*/<run>/` with `latest` symlinks. Flatten emits
   one line per item (option B: core line + child lines).
4. **Atlas QA (phase 4)** — `src/atlas/*` (taxonomy sampling → question
   generation → **local** Memgraph Cypher execution → answer generation →
   report) and `src/utils/atlas/*` (audits: `audit_qa_alignment.py`,
   `audit_qa_manual.py`, `atlas_query_forensics.py`). SLURM:
   `taxonomy_sampling.sh`, `atlas_questions_generator.sh`,
   `atlas_answer_generator.sh`, `atlas_qa_judge.sh`.

## Where work happens

- **Python stages**: `src/<phase>/*.py`; shared helpers in `src/utils/`.
- **SLURM**: `slurm_scripts/*.sh` (27 scripts). Every run writes its own
  timestamped folder (`data/battery/<ddmmyyyy_hhmmss>/`, `data/interim/coref_stage/<…>/`,
  `data/interim/atlas_generation/<…>/`) plus `latest` symlinks and `_run_info.json`.
- **Logs**: `slurm_scripts/slurm_logs/<stage>/` (job-id filenames; ~1.9 GB mirror).
- **Audits / reports**: `audits/*.md` (dead-code inventory, original-KB stats,
  QA alignment, coref audit, SRL forensics). Read them before proposing work.
- **Tests**: `planner/tests/` for the planner; the thesis pipeline has no test
  suite — verification is usually a smoke run on one chunk + an audit.

## Cost model (what tasks really take)

- **SLURM rerun of one stage** — editing a script: 15–60 min of code/review;
  queue + run: 20 min–2 h wall clock, mostly unattended. Estimate *hands-on*
  time, not wall-clock: usually 60–120 min including a smoke chunk.
- **Writing/updating an audit** — read 1–3 JSONL chunks + summary JSONs,
  sanity-check counts, write the .md: 90–180 min.
- **Design work** (schema, prompt, validator, scheduler): 120–240 min; split it.
- **Data investigation** (why 623 duplicates, why 0 WSD children): 60–180 min,
  1–3 hypothesis/fix cycles; always ends with a rerun and a verification.
- **Thesis writing**: 3–4 h per section; never estimate under 120 min.
- Changing `planner/` itself: small (30–90 min), covered by `planner/tests/`.

## Conventions

- Determinism: `seed=42` everywhere (vLLM engine + per-request), temperature
  0.0–0.3 for judges/estimators.
- Never delete `data/`; run folders are append-only evidence.
- Model arms: Qwen3-8B (legacy env, vllm 0.8.5) and Qwen3.8-27B (modern env,
  TP=2) — A/B comparisons must stay arm-separated.
- A task that changes a pipeline stage is only "done" after a rerun + an audit
  artifact, not after the code edit.
