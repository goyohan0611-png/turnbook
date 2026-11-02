"""Candidate pools for the learning-curve study (learning_curve.py), from LoCoMo.

LoCoMo (snap-research/locomo): 10 long two-person conversations, ~200 questions each, every
question labelled with the turns that answer it. One conversation stands in for one user's memory,
so its questions can be replayed in order to see whether recall improves with use.

Each conversation is stored as the capture service would store it, embedded, and every question
goes through the shipped word and meaning searches (top 50 each). We keep both rankings, the
shipped fused order, and the turn texts, so learning_curve.py can run offline in seconds.
No model is called. Adversarial questions (category 5, no answer in the conversation) are skipped.

    python research/locomo_pools.py            # all 10 conversations
    python research/locomo_pools.py --limit 1  # a quick look
"""
from __future__ import annotations

from argparse import ArgumentParser
from datetime import datetime, timedelta
from pathlib import Path
import hashlib
import json
import sys
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from turnbook.conversation_memory import ConversationMemoryStore  # noqa: E402
from turnbook.local_embedder import PRESETS, LocalEmbedder  # noqa: E402
LOCAL_MODEL = PRESETS["multilingual"]   # the pools' default: files without a model suffix
from turnbook.mcp_server import _PIECES, CONTEXT_LIMIT, excerpt, fuse, semantic_turns  # noqa: E402
from turnbook.warmup import warm_turns  # noqa: E402

DATA = ROOT / "research/external/locomo/locomo10.json"
URL = "https://raw.githubusercontent.com/snap-research/locomo/main/data/locomo10.json"
SHA256 = "79fa87e90f04081343b8c8debecb80a9a6842b76a7aa537dc9fdf651ea698ff4"
CACHE = ROOT / "bench/.cache"
OUT = ROOT / "research/runs/graph-v9.20-learning/pools.json"
POOL = 50


def fetch() -> None:
    if not DATA.exists():
        DATA.parent.mkdir(parents=True, exist_ok=True)
        urllib.request.urlretrieve(URL, DATA)
    if hashlib.sha256(DATA.read_bytes()).hexdigest() != SHA256:
        sys.exit(f"{DATA} does not match the pinned LoCoMo file")


def turns_of(sample: dict):
    """(turn_id, text, happened_at) in order; a shared photo is kept as its caption."""
    talk = sample["conversation"]
    sessions = sorted((int(k.split("_")[1]) for k in talk
                       if k.startswith("session_") and isinstance(talk[k], list)))
    for number in sessions:
        start = datetime.strptime(talk[f"session_{number}_date_time"], "%I:%M %p on %d %B, %Y")
        for index, turn in enumerate(talk[f"session_{number}"]):
            text = f"{turn['speaker']}: {turn['text']}"
            if turn.get("blip_caption"):
                text += f" [shares a photo: {turn['blip_caption']}]"
            yield turn["dia_id"], text, (start + timedelta(seconds=30 * index)).isoformat()


def korean(samples: list[dict]) -> list[dict]:
    """The conversations locomo_ko.py translated, with their Korean text in place."""
    path = ROOT / "research/runs/graph-v9.21-korean/locomo_ko.json"
    done = json.loads(path.read_text(encoding="utf-8"))
    out = []
    for sample in samples:
        ko = done.get(sample["sample_id"])
        if not ko or not ko["qa"]:
            continue
        talk = {k: v for k, v in sample["conversation"].items()}
        for key, turns in talk.items():
            if key.startswith("session_") and isinstance(turns, list):
                talk[key] = [{"speaker": t["speaker"], "dia_id": t["dia_id"],
                              "text": ko["turns"].get(t["dia_id"], t["text"])} for t in turns]
        qa = []
        for n, item in enumerate(sample["qa"]):
            text = ko["qa"].get(str(n))
            if text is None:
                qa.append({**item, "category": 5})        # untranslated: skipped like category 5
                continue
            question, _, answer = text.partition("A:")
            qa.append({**item, "question": question.replace("Q:", "").strip(),
                       "answer": answer.strip()})
        out.append({**sample, "conversation": talk, "qa": qa})
    return out


def evidence_ids(qa: dict) -> list[str]:
    ids = []
    for item in qa.get("evidence", []):
        ids += [part.strip() for part in str(item).replace(",", ";").split(";") if part.strip()]
    return ids


def conversation(sample: dict, directory: Path, embedder, others: list[dict] = (),
                 pool: int = POOL) -> tuple[dict, list[dict]]:
    """`others`: more conversations in the same store, as a memory that has grown (their turn
    ids get their conversation's prefix; only the ones that reach a pool are kept in `texts`)."""
    store = directory / f"{sample['sample_id']}.sqlite"
    texts, extra = {}, {}
    with ConversationMemoryStore(store) as conversations:
        ordinal = 0
        for source in [sample, *others]:
            prefix = "" if source is sample else source["sample_id"] + "/"
            for tid, text, at in turns_of(source):
                tid = prefix + tid
                (extra if prefix else texts)[tid] = {"text": text, "at": at}
                conversations.record({"turn_id": tid, "client": "locomo",
                                      "client_session_id": tid.split(":")[0], "scope": "global",
                                      "role": "user", "content_redacted": text,
                                      "raw_sha256": tid, "source_path": source["sample_id"],
                                      "happened_at": at, "known_at": at,
                                      "source_ordinal": ordinal})
                ordinal += 1
    while warm_turns(store, limit=2048, embedder=embedder):
        pass
    questions = []
    with ConversationMemoryStore(store) as conversations:
        for number, qa in enumerate(sample["qa"]):
            if qa["category"] == 5:
                continue
            query = qa["question"]
            words = [excerpt(t) for t in conversations.search(query, limit=pool)["turns"]]
            meaning = semantic_turns(store, query, limit=pool, embedder=embedder)
            shipped = fuse(meaning, words, limit=CONTEXT_LIMIT)
            questions.append({
                "conv": sample["sample_id"], "n": number, "question": query,
                "answer": str(qa.get("answer", "")), "category": qa["category"],
                "evidence": evidence_ids(qa),
                "meaning": [[t["turn_id"], t["similarity"]] for t in meaning],
                "words": [t["turn_id"] for t in words],
                "shipped": [t["turn_id"] for t in shipped]})
            for t in meaning + words:
                if t["turn_id"] in extra:
                    texts[t["turn_id"]] = extra[t["turn_id"]]
    _PIECES.pop(str(store), None)
    store.unlink()
    return texts, questions


def hit(order: list[str], gold: list[str], k: int) -> bool:
    return bool(set(order[:k]) & set(gold))


def hits(questions: list[dict]) -> str:
    shown = [q for q in questions if q["evidence"]]
    return f"{len(shown)} questions " + " ".join(
        f"hit@{k} {sum(hit(q['shipped'], q['evidence'], k) for q in shown) / len(shown):.1%}"
        for k in (5, 10, 30))


def main() -> None:
    parser = ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--limit", type=int, help="first N conversations only")
    parser.add_argument("--mix", type=int, default=0,
                        help="also store the next N conversations in each one's memory")
    parser.add_argument("--pool", type=int, default=POOL, help="turns kept from each search")
    parser.add_argument("--ko", action="store_true", help="the Korean translation")
    parser.add_argument("--model", default=LOCAL_MODEL, help="another embedding model")
    parser.add_argument("--only", nargs="*", help="these conversations only")
    args = parser.parse_args()
    fetch()
    everything = json.loads(DATA.read_text(encoding="utf-8"))
    if args.ko:
        everything = korean(everything)
    samples = [s for s in everything[:args.limit] if not args.only or s["sample_id"] in args.only]
    CACHE.mkdir(parents=True, exist_ok=True)
    slug = args.model.replace("/", "_").replace("@", "_")
    embedder = LocalEmbedder(CACHE / f"embed-{slug}.json", model=args.model)
    result = {"turns": {}, "questions": []}
    with tempfile.TemporaryDirectory() as directory:
        for i, sample in enumerate(samples):
            others = [everything[(i + j) % len(everything)] for j in range(1, args.mix + 1)]
            texts, questions = conversation(sample, Path(directory), embedder, others,
                                            args.pool)
            result["turns"][sample["sample_id"]] = texts
            result["questions"] += questions
            print(sample["sample_id"], len(texts), "turns", hits(questions), flush=True)
            embedder.cache.flush()
    out = OUT.with_name(f"pools-mix{args.mix}.json") if args.mix else OUT
    if args.ko:
        out = out.with_name(out.name.replace("pools", "pools-ko"))
    if args.model != LOCAL_MODEL:
        out = out.with_name(out.stem + "-" + args.model.split("/")[1].split("@")[0] + ".json")
    if args.pool != POOL:
        out = out.with_name(f"{out.stem}-pool{args.pool}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    print("all", hits(result["questions"]))


if __name__ == "__main__":
    main()
