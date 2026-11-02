"""Which on-device embedding model finds an English memory from a Korean question?

The shipped model (multilingual MiniLM) misses "반려견 품종이 뭐였지?" for "we adopted a golden
retriever" (bench/latency.py). Here 20 English facts are hidden among 2,000 real user turns from
LongMemEval_S, and each is asked for four ways: in English, in plain Korean, in formal Korean, and
in Korean with the English word written in Hangul (골든 리트리버). Reported: the rank of the fact
among the 2,020 texts by cosine alone (1 = first), and how often it is in the top 10, plus the
model's load time and encoding speed. Also 5 Korean facts asked in English. No model is called but
the embedder; one model per process (memory).

    python model_probe.py distractors          # once: 2,000 user turns to research/runs/
    python model_probe.py run <model>          # one candidate, appends to the results file
"""
from __future__ import annotations

from pathlib import Path
import json
import os
import statistics
import sys
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "research"))

OUT = ROOT / "research/runs/graph-v9.8-model-probe"
DATA = ROOT / "research/external/longmemeval/longmemeval_s_cleaned.json"

# (English fact, English question, plain Korean, formal Korean, Korean with a loanword)
FACTS = [
    ("We finally adopted a golden retriever puppy last spring and named him Kongi.",
     "what kind of dog do I have", "우리 강아지 품종이 뭐야?", "반려견 품종이 뭐였지?",
     "내가 키우는 골든 리트리버 이름이 뭐였지?"),
    ("My sister just moved to Vancouver for her new nursing job.",
     "where does my sister live now", "내 여동생 지금 어디 살아?", "누나는 어느 도시로 이사했었지?",
     "밴쿠버로 이사 간 사람이 누구였지?"),
    ("I'm allergic to shellfish, so I always skip shrimp and crab.",
     "what food am I allergic to", "나 무슨 음식 알레르기 있지?", "내가 피해야 하는 음식이 뭐였더라?",
     "새우 먹어도 되나?"),
    ("I switched from coffee to green tea in the mornings to sleep better.",
     "what do I drink in the morning", "아침에 뭐 마신다고 했지?", "수면 때문에 바꾼 음료가 뭐였지?",
     "그린티 마시기 시작한 이유가 뭐였지?"),
    ("Our wedding anniversary is on June 14th.",
     "when is my anniversary", "결혼기념일이 언제야?", "우리 부부 기념일 날짜가 언제였지?",
     "웨딩 기념일 언제였지?"),
    ("I started learning the cello three months ago with a teacher on Saturdays.",
     "what instrument am I learning", "나 무슨 악기 배우고 있지?", "토요일마다 레슨 받는 게 뭐였지?",
     "첼로 레슨은 무슨 요일이야?"),
    ("My car is a 2019 Hyundai Ioniq hybrid, silver.",
     "what car do I drive", "내 차 뭐야?", "제가 타는 자동차 모델이 뭐였죠?", "아이오닉 몇 년식이었지?"),
    ("I ran my first half marathon in 2 hours and 4 minutes.",
     "what was my half marathon time", "하프 마라톤 기록이 몇이었지?", "첫 하프 완주 시간이 어떻게 됐지?",
     "하프 마라톤 타임 얼마였어?"),
    ("My landlord raised the rent to 1,850 dollars starting next month.",
     "how much is my rent going to be", "월세 얼마로 오른다고 했지?", "다음 달부터 집세가 얼마였지?",
     "렌트비 인상된 금액이 얼마야?"),
    ("I'm vegetarian since last year but I still eat eggs.",
     "do I eat meat", "나 고기 먹어?", "제 식단 제한이 뭐였죠?", "베지테리언 된 지 얼마나 됐지?"),
    ("My daughter's school is Maple Elementary, she's in second grade.",
     "what grade is my daughter in", "딸이 몇 학년이지?", "우리 딸 학교 이름이 뭐였지?",
     "메이플 초등학교 다니는 애가 몇 학년이야?"),
    ("I use a standing desk and a split ergonomic keyboard at work.",
     "what keyboard do I use", "회사에서 어떤 키보드 써?", "내 업무용 키보드 종류가 뭐였지?",
     "스탠딩 데스크 쓰고 있나?"),
    ("My favorite restaurant is a small ramen place called Menya Kaito near the station.",
     "what is my favorite restaurant", "내가 제일 좋아하는 식당이 어디야?", "역 근처 단골집 이름이 뭐였지?",
     "라멘집 이름이 뭐였더라?"),
    ("I have a dentist appointment on the 23rd for a root canal.",
     "when is my dentist appointment", "치과 예약 언제야?", "신경치료 받는 날이 언제였지?",
     "치과 언제 가야 해?"),
    ("We're planning a trip to Lisbon in October with my college friends.",
     "where am I traveling in October", "10월에 어디 여행 가?", "대학 친구들이랑 가는 여행지가 어디였지?",
     "리스본 여행 언제 간다고 했지?"),
    ("My blood pressure was 135 over 88 at the last checkup.",
     "what was my blood pressure", "혈압이 얼마였지?", "지난 건강검진 혈압 수치가 어땠지?",
     "검진 때 혈압 몇 나왔어?"),
    ("I prefer aisle seats on flights because of my knee.",
     "which seat do I prefer on planes", "비행기 좌석 어디가 좋다고 했지?", "제가 선호하는 기내 좌석이 뭐였죠?",
     "통로 쪽 좌석 원하는 이유가 뭐였지?"),
    ("My password manager is Bitwarden and I moved off LastPass.",
     "which password manager do I use", "비밀번호 관리 뭐로 해?", "어떤 비번 관리 앱 쓴다고 했지?",
     "라스트패스에서 뭐로 갈아탔지?"),
    ("I work as a backend engineer at a logistics startup, mostly in Go.",
     "what is my job", "나 무슨 일 해?", "제 직업이 뭐였죠?", "백엔드 개발할 때 주로 쓰는 언어가 뭐야?"),
    ("My mom's birthday is March 3rd and she likes orchids.",
     "what flowers does my mom like", "엄마가 좋아하는 꽃이 뭐야?", "어머니 생신 선물로 뭐가 좋을까?",
     "엄마 생일이 언제였지?"),
]
# Korean memory, English question (the other direction)
KO_FACTS = [
    ("우리 고양이 이름은 나비고 올해 다섯 살이야.", "what is my cat's name"),
    ("다음 주 금요일에 부산으로 출장 가.", "where is my business trip"),
    ("요즘 필라테스를 일주일에 두 번 하고 있어.", "how often do I do pilates"),
    ("내 노트북은 맥북 에어 M2 15인치야.", "what laptop do I have"),
    ("건강검진에서 콜레스테롤이 조금 높게 나왔어.", "what did my checkup say"),
]
KINDS = ("english", "korean", "formal", "loanword")
PREFIX = {"intfloat/": ("query: ", "passage: ")}     # e5 is trained with these prefixes


def distractors():
    from longmemeval_m_fetch import stream_instances
    seen, out = set(), []
    for row in stream_instances(DATA):
        for session in row["haystack_sessions"]:
            for turn in session:
                text = turn["content"].strip()
                if turn["role"] == "user" and 40 < len(text) < 1500 and text not in seen:
                    seen.add(text)
                    out.append(text)
        if len(out) >= 4000:
            break
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "distractors.json").write_text(json.dumps(out[::2][:2000], ensure_ascii=False),
                                          encoding="utf-8")


def encoder(model):
    from tokenizers import Tokenizer
    import numpy as np
    import onnxruntime
    from turnbook.local_embedder import model_paths
    tok_file, onnx_file = model_paths(model)
    tok = Tokenizer.from_file(tok_file)
    tok.enable_truncation(256)
    tok.enable_padding()
    options = onnxruntime.SessionOptions()
    options.log_severity_level = 3
    options.enable_cpu_mem_arena = False                  # as the product runs it
    options.intra_op_num_threads = max(1, (os.cpu_count() or 2) // 2)
    session = onnxruntime.InferenceSession(onnx_file, options, providers=["CPUExecutionProvider"])
    inputs = {i.name for i in session.get_inputs()}

    def encode(texts):
        rows = []
        for start in range(0, len(texts), 64):
            enc = tok.encode_batch(texts[start:start + 64])
            ids = np.array([e.ids for e in enc], dtype=np.int64)
            mask = np.array([e.attention_mask for e in enc], dtype=np.int64)
            feed = {"input_ids": ids, "attention_mask": mask}
            if "token_type_ids" in inputs:
                feed["token_type_ids"] = np.zeros_like(ids)
            hidden = session.run(None, feed)[0]
            w = mask[..., None].astype(np.float32)
            pooled = (hidden * w).sum(1) / np.clip(w.sum(1), 1e-9, None)
            rows.append(pooled / np.linalg.norm(pooled, axis=1, keepdims=True))
        return np.concatenate(rows)
    return encode


def run(model):
    import numpy as np
    from turnbook.local_embedder import model_paths
    query, passage = next((v for k, v in PREFIX.items() if model.startswith(k)), ("", ""))
    started = time.perf_counter()
    encode = encoder(model)
    load = time.perf_counter() - started
    texts = json.loads((OUT / "distractors.json").read_text(encoding="utf-8"))
    targets = [f[0] for f in FACTS] + [f[0] for f in KO_FACTS]
    corpus = texts + targets
    started = time.perf_counter()
    docs = encode([passage + t for t in corpus])
    per_100 = (time.perf_counter() - started) / len(corpus) * 100
    import psutil
    rss = psutil.Process().memory_info().rss / 2 ** 20      # model + a full batch, arena off
    one = []                                                # one question, as an app asks
    for fact in FACTS:
        started = time.perf_counter()
        encode([query + fact[2]])
        one.append(time.perf_counter() - started)
    ranks = {kind: [] for kind in KINDS + ("ko_memory",)}
    for n, fact in enumerate(FACTS):
        target = len(texts) + n
        for kind, question in zip(KINDS, fact[1:]):
            score = docs @ encode([query + question])[0]
            ranks[kind].append(int((score > score[target]).sum()) + 1)
    for n, (_, question) in enumerate(KO_FACTS):
        target = len(texts) + len(FACTS) + n
        score = docs @ encode([query + question])[0]
        ranks["ko_memory"].append(int((score > score[target]).sum()) + 1)
    result = {"model": model, "load_s": round(load, 1), "s_per_100_texts": round(per_100, 2),
              "rss_mb": round(rss), "query_ms": round(statistics.median(one) * 1000, 1),
              "onnx_mb": round(Path(model_paths(model)[1]).stat().st_size / 2 ** 20),
              **{f"{k}_top10": f"{sum(r <= 10 for r in v)}/{len(v)}" for k, v in ranks.items()},
              **{f"{k}_median_rank": float(np.median(v)) for k, v in ranks.items()},
              "ranks": ranks}
    with (OUT / "results.jsonl").open("a", encoding="utf-8") as log:
        log.write(json.dumps(result, ensure_ascii=False) + "\n")
    print(json.dumps({k: v for k, v in result.items() if k != "ranks"}, ensure_ascii=False))


if __name__ == "__main__":
    distractors() if sys.argv[1] == "distractors" else run(sys.argv[2])
