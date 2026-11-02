# Benchmarks

Every number here comes from files in this repository: the pre-registrations, each question's
answer and the judge's verdict on it, under [`research/runs/`](../research/runs). The method is in
[REPORT.md](../research/REPORT.md), including the failures and the corrections.

## LongMemEval_S, answer accuracy, 500 questions

The answer model is gpt-5-mini; the judge is the official gpt-4o-2024-08-06.

| | accuracy | model tokens at write time | packet read per question |
|---|---|---|---|
| **0.10.0, all 500 questions** | **91.8%** (459/500) | **0** | 3.1k tokens |
| 0.10.0, never used for tuning (380) | **91.1%** (346/380) | 0 | 3.1k tokens |
| 0.9.10, all 500 | 88.8% (444/500) | 0 | 3.4k tokens |
| 0.9.10, never used for tuning (380) | 87.6% (333/380) | 0 | 3.4k tokens |

0.10.0 ran its default search model, EmbeddingGemma (4-bit ONNX), with 15,000-character packets
([`graph-v9.14-full500-mpnet8-gemma`](../research/runs/graph-v9.14-full500-mpnet8-gemma)). One
question gets no answer (the reader spends its 1,024 output tokens thinking) and counts as wrong. "Never used for tuning" means the
question ids held back since v7.0; the re-ranker weights were trained on LoCoMo and LongMemEval
together, so no LongMemEval score here is fully held out. 0.9.10 ran the int8 export of
multilingual mpnet ([`graph-v9.14-full500-mpnet8`](../research/runs/graph-v9.14-full500-mpnet8));
the model before it (multilingual MiniLM) scored the same 444/500. One question gets no parsable
answer there (the reader runs out of output) and counts as wrong.

Packet size against search model, same 500 questions, reader and judge (median tokens read):
mpnet 3.4k 86.6%, 6.7k 89.6%, 13.2k 90.4%, 26.3k 91.8%; EmbeddingGemma 3.1k 91.8%. Unanswered
questions count as wrong in every run. Against mpnet
at the same size: 33 questions only EmbeddingGemma answers, 7 only mpnet (exact McNemar p = 0.00004).

## agentmemory, all 499 questions

[rohitg00/agentmemory](https://github.com/rohitg00/agentmemory) (not the JordanMcCann project of the same name) publishes 95.2% on LongMemEval_S and states it is retrieval recall (recall_any@5),
not answer accuracy; it also publishes the sessions its hybrid search returned for each question.
Its first 5 sessions, every turn in full, went to the same answer model, prompt and official
judge as the 88.8% run. Only the retrieval differs. Its results file is pinned by commit and
hash in [`research/rival_agentmemory.py`](../research/rival_agentmemory.py); one question failed
to get an answer on retry and is left out of both columns.

| | accuracy | never used for tuning (379) | text the answer model reads |
|---|---|---|---|
| **Turnbook** | **89.0%** (444/499) | **87.1%** (330/379) | 3.4k tokens |
| agentmemory | 80.4% (401/499) | 78.6% (298/379) | ~16.5k tokens (66k characters) |

Turnbook alone was right on 64 questions, agentmemory alone on 21 (exact McNemar
p = 3.3e-6). By type, Turnbook / agentmemory: multi-session 80.8 / 65.8, temporal reasoning
88.9 / 77.0, single-session user 98.4 / 87.5, knowledge update 94.4 / 90.3, preference
75.0 / 71.4, abstention 93.3 / 90.0, single-session assistant 94.6 / 96.4.
Five whole sessions are agentmemory's unit of retrieval and its best case here: the answer model
reads about five times what Turnbook gives it. Per-question files:
[`research/runs/graph-v9.6-rival-agentmemory-k5`](../research/runs/graph-v9.6-rival-agentmemory-k5).

The older retrieval baseline below (commit `ceddf56`, 2026-10-19) uses agentmemory's definition
(is an answer session among the first K retrieved?), from `bench/evidence.py` in CI. It is not a
measurement of the current EmbeddingGemma checkout. Turnbook retrieves turns, not sessions; its
sessions are counted in the order their first turn ranks. Its 479 questions are those with an
answer-bearing turn; agentmemory's 500 include the abstention questions, and on its 470
non-abstention ones it scores 95.1% / 98.5%.

| | recall_any@5 | recall_any@10 |
|---|---|---|
| **Turnbook** (479 questions) | **98.5%** | **99.2%** |
| agentmemory hybrid (500) | 95.2% | 98.6% |

Separately, an **offline replay of the frozen EmbeddingGemma answer packets** above finds a gold
session among the first 5 distinct sessions in 466/470 non-abstention questions (99.15%), and
among the first 10 in 470/470 (100%). All 500 packets are present; 30 abstention questions are
explicitly excluded. Sessions are counted in their first-occurrence order **inside the packed
context**, so this is `packet_session_hit@K`, not a fresh retrieval run or answer accuracy. It
cannot measure answer-turn coverage or recover candidates the packet discarded. The reranker
training caveat above still applies; this is not a new blind result.

Reproduce without model calls:
`python bench/cached_sessions.py research/runs/graph-v9.14-full500-mpnet8-gemma --out report.json`.
The [recorded result](../bench/baseline/cached-gemma-session-recall.json) includes input hashes.

## Reading other published numbers

They measure different things, so they do not compare directly:

- **MemPalace's 96.6% and agentmemory's 95.2%** are retrieval recall (R@5): is the right session
  among the five returned? This page measures whether the final answer is correct.
- **Mem0's 94.4%** is its managed cloud platform, with proprietary components.
- **Mastra (94.87%), Emergence (86%), Supermemory (85.2%) and Zep (71.2%)** report their own
  setups and answer models; they were not reproduced here.

As far as we know, everything above 85% on that list runs a model over your conversations when it
saves them. Turnbook does not.

## Reproducing

1. Download `longmemeval_s_cleaned.json` from [LongMemEval](https://github.com/xiaowu0162/LongMemEval)
   into `research/external/longmemeval/`.
2. Set `OPENAI_API_KEY` for the answer model and the judge.
3. `pip install -e .`, then from `research/` run the commands in
   [REPORT.md §10](../research/REPORT.md#10-reproducing). A full 500-question run costs about US$3.

## Known limits

- **For the first few seconds after an AI app starts, answers use word search only** while the
  embedding model loads (6-30 s on a slow, synced disk). The first answer never waits for it.
- **Daily use on Windows only so far.** macOS and Linux pass the tests in CI but have not seen real
  use.
- **Capture follows each app's transcript format,** which is not a public interface; an app update
  can stop capture until Turnbook is updated.
- **Multi-session questions are the weakest type at 80%**: counting or combining facts across many
  conversations.

The full list is in [REPORT.md §9](../research/REPORT.md#9-known-limits).
