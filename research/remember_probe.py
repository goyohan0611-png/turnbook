"""Does the model decide well, by itself, what goes into the facts layer? Old vs new instructions.

In apps whose conversations are captured anyway (Claude Code, Codex), the model is told when to
call brain_remember (CAPTURED_NOTE). Keyword rules cannot cover how people say things, so the
judgement is the model's. Codex reads each instruction text and 32 user messages (Korean and
English, half that should be saved, half that should not) and says whether it would call
brain_remember. One call at a time.

    python research/remember_probe.py
"""
from __future__ import annotations

from pathlib import Path
import json
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "research"))

from codex_replies import EMPTY, codex  # noqa: E402

from turnbook.mcp_server import CAPTURED_NOTE  # noqa: E402

NEW_NOTE = (
    "Every turn of this conversation is saved automatically, so do not call brain_remember for "
    "ordinary chat, questions or task requests. Call it, whatever the language, when the user: "
    "asks you to remember something; makes or confirms a decision ('let's go with X from now "
    "on', '앞으로 X로 하자'), with the reason, memory_type='decision' and the project in "
    "`entities`; states a preference or a rule for how you should work; or states a fact that "
    "can change later (where they live or work, a pet, a plan, a date, a number they track): "
    "then with `about` and `attribute`, so the newest value replaces the old. When an answer "
    "came from old conversation turns rather than such a fact, record the fact you learned the "
    "same way, dated by `effective_at`. Silently. ")

SAVE = [
    "앞으로 릴리스 노트는 영어로만 쓰자.", "We decided to drop Python 3.9 support.",
    "나 다음 달에 판교로 이사해.", "My manager is now Jiwoo, not Sam.",
    "코드 리뷰할 때 테스트부터 봐줘. 항상.", "From now on, use tabs, not spaces, in this repo.",
    "이거 기억해: 스테이징 DB는 매주 월요일에 초기화돼.", "Launch moved to November 14.",
    "나 커피는 디카페인만 마셔.", "Let's go with Postgres for the hub, SQLite is too limited.",
    "우리 팀 회의는 이제 화요일 10시야.", "I prefer short answers, no bullet lists.",
    "결정했어, 로고는 1번으로 간다.", "Our AWS bill this month was $1,240.",
    "콩이가 이번 주에 중성화 수술했어.", "Remember that the client wants weekly reports.",
]
SKIP = [
    "이 함수 왜 에러 나는지 봐줘.", "What does this regex do?",
    "ㅋㅋ 오늘 날씨 좋다", "Thanks, that worked!",
    "테스트 돌려봐", "Can you make this loop faster?",
    "이 문장 영어로 번역해줘: 안녕하세요", "Show me the git log.",
    "음 잠깐만", "Which is better, React or Vue?",
    "README 오타 고쳐줘", "Explain how OAuth works.",
    "이거 커밋해", "lol ok",
    "로그 좀 보여줘", "How many lines is this file?",
]


def ask(note: str, message: str) -> bool | None:
    prompt = ("You are an AI coding assistant connected to a memory tool, brain_remember, which "
              "saves a fact or decision for later conversations. Your instructions about it:\n\n"
              + note + "\n\nThe user now says:\n" + message
              + "\n\nWould you call brain_remember for this message? Do not run any commands. "
              'Reply with ONLY {"call": true} or {"call": false}.')
    reply = codex(prompt)
    if '"call": true' in reply.replace("'", '"').lower():
        return True
    if '"call": false' in reply.replace("'", '"').lower():
        return False
    return None


def main() -> None:
    EMPTY.mkdir(exist_ok=True)
    out = ROOT / "research/runs/graph-v9.20-learning/remember_probe.json"
    results = json.loads(out.read_text(encoding="utf-8")) if out.exists() else {}
    for label, note in (("old", CAPTURED_NOTE), ("new", NEW_NOTE)):
        mine = results.setdefault(label, {})
        for message in SAVE + SKIP:
            if message not in mine:
                mine[message] = ask(note, message)
                out.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
                print(label, mine[message], message, flush=True)
    for label in ("old", "new"):
        mine = results[label]
        saved = sum(mine[m] is True for m in SAVE)
        wrong = sum(mine[m] is True for m in SKIP)
        print(f"{label}: saved {saved}/{len(SAVE)} that should be, {wrong}/{len(SKIP)} that "
              f"should not; missed: {[m for m in SAVE if mine[m] is not True]}")


if __name__ == "__main__":
    main()
