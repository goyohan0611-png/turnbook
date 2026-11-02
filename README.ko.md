<div align="center">

<img src="https://raw.githubusercontent.com/goyohan0611-png/turnbook/main/demo/logo.png" alt="Turnbook" width="112">

# Turnbook

### AI는 세션이 끝나면 다 잊어버립니다. 이건 기억합니다. 그것도 LLM을 한 번도 부르지 않고요.

**AI 요약을 거치지 않습니다: 모든 대화를 원문 그대로 저장해서 빠지거나 지어낸 내용이 없고, 저장에 드는 모델 토큰은 0입니다. LongMemEval 91.8%, 질문당 기억 3.1천 토큰으로 ([0.10.0](#정말-되나요)). 내 PC에서 돌아갑니다.**

[![][license-shield]][license-link]
[![][python-shield]][python-link]
[![][platform-shield]][platform-link]
[![][longmemeval-shield]][benchmarks-link]
[![][ci-shield]][ci-link]
[![][pypi-shield]][pypi-link]

*월요일에 한 에이전트와 정한 걸, 수요일에 다른 에이전트한테 물어보세요. 알고 있습니다.*

| | 일반적인 기억 도구 (Mem0, Mastra, Supermemory 등) | **Turnbook** |
|---|---|---|
| 대화를 저장하는 방식 | LLM이 사실이나 메모로 다시 정리 | **원문 그대로** |
| 저장 비용 | 대화마다 LLM API 호출, 쓰는 동안 계속 | **없음** |
| API 키나 클라우드 요금제 | 필요 | **필요 없음** |
| 대화가 가는 곳 | LLM 회사로, 클라우드형이면 그 회사 서버로 | **내 PC에만** |
| 무엇이 저장되나 | 모델이나 코드가 넣기로 한 것만 | **모든 대화가 자동으로** |
| 같이 쓰는 앱 | 그 도구를 붙여 만든 앱 하나 | **Claude Code, Codex, Cursor, Copilot CLI, Gemini CLI, OpenCode를 그대로** |
| LongMemEval (답변 모델 gpt-5-mini) | 최고 94.9% (Mastra) | 질문당 3.1천 토큰으로 91.8% |

<sub>LongMemEval_S 500문항 전체, 공식 gpt-4o 채점기. 모든 대화를 LLM으로 다시 정리하면 여러 세션을 합치거나 날짜를 따지는 문제에서 몇 점을 더 얻습니다. Turnbook은 대신 원문을 그대로 두고 저장에 비용을 쓰지 않습니다. <a href="#다른-도구와-비교">비교</a></sub>

[English](README.md) · **한국어**

<img src="https://raw.githubusercontent.com/goyohan0611-png/turnbook/main/demo/demo.gif" alt="실제 녹화: Claude Code가 Turnbook에 저장한 결정을 다른 앱인 Codex가 기억하는 장면" width="900">

<sub>한 PC에서 실제로 녹화: Claude Code(Opus 5.5, effort high)와 Codex(GPT-6.1-Sol, effort high)가 Turnbook 기억 하나를 같이 씁니다. 모델이 생각하는 시간은 빨리 감았습니다.</sub>

토큰이나 같은 설명을 반복하는 수고를 덜었다면, ⭐ 하나가 다른 사람이 Turnbook을 찾는 데 도움이 됩니다.

</div>

## 설치

Windows: [Turnbook-Setup.cmd](https://raw.githubusercontent.com/goyohan0611-png/turnbook/main/scripts/Turnbook-Setup.cmd)를
받아서 더블클릭하거나, PowerShell에서:

```powershell
irm https://raw.githubusercontent.com/goyohan0611-png/turnbook/main/scripts/install.ps1 | iex
```

macOS / Linux:

```bash
curl -LsSf https://raw.githubusercontent.com/goyohan0611-png/turnbook/main/scripts/install.sh | sh
```

Python이 없어도 되고, 따로 설정할 것도 없습니다. 설치 후 AI 앱을 한 번 껐다 켜세요. Python이 이미
있다면 `pip install turnbook && turnbook-install`. 스크립트를 먼저 읽어 보고 버전을 고정해서
설치하고 싶다면: [신중한 설치](docs/install.md#careful-install-read-first-pin-a-version)

<details>
<summary>MCP 클라이언트에 직접 연결하기</summary>

`pip install turnbook` 후:

```json
{
  "mcpServers": {
    "turnbook": {
      "command": "python",
      "args": ["-m", "turnbook.mcp_server"]
    }
  }
}
```

Claude Code: `claude mcp add turnbook -- python -m turnbook.mcp_server`. 이렇게 하면 도구만
연결됩니다. 대화 자동 저장까지 쓰려면 `turnbook-install`을 실행하세요.
</details>

## 써보기

평소처럼 일하다가, 새 세션이나 다른 앱, 다른 모델에서 물어보세요.

- *"어제 어디까지 했지?"*
- *"결제 서비스 DB 뭘로 하기로 했었지? 이유가 뭐였더라?"*
- *"지난주에 인증 미들웨어 어디 고쳤어?"*
- *"우리 강아지 이름이 뭐였지?"* (지나가듯 한 번 말한 것도)

따로 태그를 달거나 저장할 필요가 없습니다. 모든 대화가 이미 저장돼 있고, 에이전트는 질문에 답이
되는 몇 개만 꺼내 봅니다.

## 하는 일

- **모든 에이전트가 하나의 기억으로.** Claude Code, Codex, Cursor, Copilot CLI, Gemini CLI, OpenCode와
  모든 MCP 앱이 같은 기억을 읽고 씁니다. 한 에이전트와 정하고 다른 에이전트에게 물어보세요. 모델을
  바꿔도 기억은 그대로입니다.
- **메시지마다 필요한 기억이 알아서.** Claude Code, Codex, Copilot CLI, Gemini CLI, OpenCode에서는
  메시지를 보내면 Turnbook이 과거에서 필요한 걸 찾아 모델이 답하기 전에 넣어 줍니다. 따로 물어볼
  필요가 없습니다.
- **저장은 공짜, 내 PC에서.** 모든 대화를 SQLite 파일 하나에 원문 그대로 저장합니다. 모델 호출도,
  토큰도, 계정도, 클라우드도 없습니다.

<details>
<summary>그 밖의 기능</summary>

- **오케스트레이션에도 맞습니다.** 오케스트레이터가 에이전트 여러 개를 동시에 돌려도 모두 같은 기억에
  쓰고 읽어서, 한 에이전트가 정한 걸 다음 에이전트가 압니다. 기억마다 어느 앱·세션에서 나왔는지
  남습니다.
- **기억이 쌓여도 버팁니다.** 0.9.47의 검색 모델로, 기억이 20만 5천 턴이 돼도 질문의 79.7%는 답이 찾은 것 중 상위 30개 안에
  있고(6천 턴에서 82.8%), 검색 한 번은 약 0.4초입니다. [기억이 쌓일수록](#기억이-쌓일수록)
- **언어가 달라도.** 검색 모델이 한국어도 영어만큼 거의 잘 읽어서, 영어로 말한 걸 한국어로 물어도,
  그 반대도 찾습니다.
  [한국어에서는](#한국어에서는)
- **프로젝트 문서도.** `turnbook docs add 폴더`를 하면 그 폴더의 Markdown·텍스트 파일을 제목 단위로 나눠
  대화와 함께 검색되게 하고, 파일이 바뀌면 따라 갱신합니다.
- **여러 PC에서 같은 기억.** `turnbook folder share`를 실행하고, 다른 PC에서
  `turnbook folder <코드>`. (AI는 명령어를 알려줄 뿐 기억을 스스로 옮기지 않습니다. 그래서 웹페이지가
  AI를 속여도 못 옮깁니다.) 사무실 네트워크나 [Tailscale](https://tailscale.com), TLS, PC를 떠나기
  전에 한 줄씩 암호화.
- **기억을 파일로 다른 사람에게.** `turnbook export 파일`은 전부, `--project 폴더`를 붙이면 그 폴더에서
  나눈 대화만 담습니다. 받는 사람은 `turnbook import 파일`.
- **내 것.** 계정 없음, 자체 클라우드 없음, 사용 기록 수집 없음. 비밀번호·API 키 같은 건 저장 전에
  가려지고, *"아까 친 비밀번호 지워줘"* 하면 모든 곳에서 지워집니다. [docs/privacy.md](docs/privacy.md)
- **원하면 팀으로.** 프로젝트의 결정과 진행 상황을 팀원의 AI와 공유합니다. 개인 기억은 절대
  공유하지 않습니다. [docs/teams.md](docs/teams.md)

</details>

## 지원하는 앱

설치 프로그램이 찾아서 연결합니다. 새 앱을 깔았으면 한 번 더 실행하세요.

| 앱 | 대화 저장 | 방식 |
|---|---|---|
| Claude Code | 모든 대화 | 대화 기록 파일을 읽음. 훅이 메시지마다 관련 기억을 넣어줌 |
| Codex CLI | 모든 대화 | 대화 기록 파일을 읽음. 훅이 메시지마다 관련 기억을 넣어줌 (`/hooks`에서 한 번 허용) |
| Cursor | 모든 대화 | 대화 기록 파일을 읽음 |
| Copilot CLI | 모든 대화 | 대화 기록 파일을 읽음. 훅이 메시지마다 관련 기억을 넣어줌 |
| Claude Desktop (Cowork) | 모든 대화 | 세션 파일을 읽음 |
| Gemini CLI | 모든 대화 | 훅. 답하기 전에 관련 기억도 넣어줌 |
| OpenCode | 모든 대화 | 플러그인. 답하기 전에 관련 기억도 넣어줌 (어떤 모델이든) |
| Claude Desktop 채팅, Windsurf, VS Code Copilot, Cline, LM Studio | 모델이 저장한 것 | MCP 도구 |

다른 MCP 클라이언트도 도구로 쓸 수 있습니다. 위의 "직접 연결하기"를 보세요.

## 동작 방식

```mermaid
flowchart LR
    A["Claude Code · Codex · Cursor<br/>Gemini CLI · OpenCode · ..."] -- "대화 기록, 훅, 플러그인" --> C["저장 서비스<br/>(백그라운드)"]
    C -- "원문 그대로,<br/>비밀은 가려서" --> S[("내 PC의<br/>기억")]
    S --> I["로컬 검색<br/>단어 + 의미 + 시간"]
    A -- "과거가 필요한 질문" --> I
    I -- "답이 되는 몇 개의 대화<br/>(수천 토큰)" --> A
```

- **저장에 비용이 들지 않습니다.** 말한 그대로 저장합니다. 저장할 때 모델이 요약하거나 뽑아내지
  않으니, 들어가면서 빠지거나 지어낸 게 섞이지 않고 토큰도 쓰지 않습니다.
- **검색은 내 PC에서.** 단어 검색, 로컬 임베딩 모델(Google EmbeddingGemma 4비트, ONNX, GPU 불필요),
  언제 한 말인지를 함께 보고 작은 학습 점수로 줄 세웁니다. 에이전트는 기록 전체가 아니라 원문 대화가
  담긴 짧은 묶음을 받습니다.
- **생각은 이미 쓰는 모델이 합니다.** Turnbook는 근거를 건네고, 답은 모델이 합니다. 모델이 좋아지면
  기억은 그대로여도 답이 좋아집니다.

## 에이전트 메모리를 넘어서

Turnbook의 속은 대화 기록 위의 RAG이고, 답을 쓰기 직전까지의 모든 단계가 들어 있습니다. 보통 RAG는 부품을
엮어 직접 만들지만, Turnbook은 답 쓰기 전 단계를 혼자 다 하고 답만 이미 쓰는 AI에게 맡깁니다:

| 단계 | 보통 RAG | Turnbook |
|---|---|---|
| 모으기 | 직접 짜는 수집 스크립트 | 에이전트와의 대화가 자동으로 저장 |
| 자르기 | 손봐야 하는 분할 규칙 | 한 턴씩, 원문 그대로 |
| 임베딩 | 토큰마다 돈 내는 임베딩 API | 내 PC의 로컬 모델, 무료 |
| 저장소 | 따로 운영하는 벡터 DB | SQLite 파일 하나 |
| 찾기 | 직접 짜는 검색 코드 | 단어·의미·시간으로 찾고 작은 학습 점수로 재정렬 |
| 넘기기 | 프롬프트 조립 코드 | MCP로 크기가 정해진 근거 묶음 전달 |
| 답하기 | 쓰는 LLM | 쓰는 LLM (Claude, Codex 등) |

같은 엔진으로 사용자마다 기억하는 챗봇, 지난 문의를 떠올리는 상담봇, 팀의 결정 기록 검색도 만들 수 있습니다.
지금은 개인용 로컬 기억으로 배포되며, 파이썬 API와 여러 사용자용 서버 모드는 아직 없습니다. 관심 있으면 이슈를
남겨주세요.

## 정말 되나요?

[LongMemEval](https://github.com/xiaowu0162/LongMemEval)은 장기 대화 기억을 재는 공개 벤치마크입니다.
질문 500개, 질문마다 과거 대화 세션 약 50개. 여기 모든 Turnbook 점수는 같은 답변 모델(gpt-5-mini),
같은 프롬프트, 공식 gpt-4o 채점기로 쟀고, 답변 모델이 답을 못 낸 문항은 틀린 것으로 셉니다.

| | 정확도 | 기억 **저장**에 쓴 모델 토큰 | 답변 모델이 읽는 분량 |
|---|---|---|---|
| **Turnbook 0.10.0** | **91.8%** (500문항 전체) | **0** | **3.1천 토큰** |
| Turnbook 0.9.9 | 88.8% (500문항 전체) | 0 | 3.4천 토큰 |

### 다른 도구와 비교

다른 기억 도구들은 각자 다른 답변 모델·채점기·설정으로 LongMemEval 점수를 발표해서, 한 줄로 순위를 매길 수
없습니다. 가장 비슷한 조건은 [Mastra Observational Memory](https://mastra.ai/research/observational-memory)로,
같은 답변 모델(gpt-5-mini)로 94.9%를 발표했습니다. 대화를 저장할 때 gemini-2.5-flash가 정리해 둔 관찰 기록
약 3만 토큰을 답변 모델이 읽습니다. Mastra와 Mem0, Supermemory 같은 도구는 저장할 때 모든 대화를 LLM으로
정리해서 날짜가 붙은 사실을 쓰고 바뀐 정보를 고쳐 둡니다. Turnbook이 점수를 잃는 곳이 바로 여기입니다:
틀린 41문항 중 33문항이 여러 세션을 합치거나, 날짜를 따지거나, 바뀐 최신 값을 물어야 하는 문제입니다.
Turnbook은 원문을 그대로 두고, 저장에 모델 토큰을 쓰지 않으며, 내 PC에서 돌아갑니다.

같은 답변 모델·채점기로 직접 잰 비교 하나:
[rohitg00/agentmemory](https://github.com/rohitg00/agentmemory)가 공개한 검색 결과는 80.4%
(같은 499문항에서 Turnbook 0.9.9는 89.0%), [자세히](docs/benchmarks.md).

### 더 적은 토큰으로 같은 정확도

0.10.0부터 새로 설치하면 검색 모델이 mpnet이 아니라 EmbeddingGemma입니다. 같은 500문항, 같은 답변 모델과
채점기에서 검색 모델과 묶음 크기만 바꿨습니다:

| 검색 모델 | 답변 모델이 읽는 분량 (중앙값) | 정확도 |
|---|---|---|
| mpnet | 3.4천 토큰 | 86.6% |
| mpnet | 6.7천 토큰 | 89.6% |
| mpnet | 1만 3.2천 토큰 | 90.4% |
| mpnet | 2만 6.3천 토큰 | 91.8% |
| **EmbeddingGemma** | **3.1천 토큰** | **91.8%** |
| EmbeddingGemma | 2만 5천 토큰 | 93.0% |

같은 크기의 mpnet과 비교하면 EmbeddingGemma만 맞힌 문항이 33개, mpnet만 맞힌 문항이 7개입니다(p = 0.00004).
여러 과거 세션을 엮어야 하는 질문이 가장 많이 올랐습니다: 73.6% → 88.4%. 모델마다 재정렬 가중치는
LoCoMo와 LongMemEval을 함께 써서 학습했습니다. 이 측정의 묶음은 최대 1만 5천 자였고, 0.10.0부터
`brain_context`는 기본으로 최대 3만 자를 보냅니다(`max_chars`로 정할 수 있습니다). LoCoMo에서 가중치가
보지 못한 대화로 잰 검색(답이 든 대화가 상위 10개 안): 83.7%, mpnet 70.2%.
[research/runs/graph-v9.14-full500-mpnet8-gemma](research/runs/graph-v9.14-full500-mpnet8-gemma)

**학습한 재정렬 없이**(단순 순위 결합, 어떤 벤치마크에도 맞춘 가중치 없음): 500문항 전체 87.0%,
튜닝에 한 번도 쓰지 않은 380문항에서 86.8%. 같은 380문항에서 지금 검색은 91.1%입니다(이쪽만 맞힌 문항 19개,
재정렬 없이만 맞힌 문항 3개). 재정렬은 답이 여러 세션에 흩어진 문제에서 가장 크게 돕습니다.
[norerank](research/runs/graph-v9.14-full500-mpnet8-norerank)

**메시지마다 넣는 기억**(4개, 약 300토큰)은 힌트이지 전체 검색이 아닙니다: 그것만으로는 같은 500문항의
61.4%를 맞히고, 모델이 더 필요할 때 부르는 `brain_context`는 91.8%입니다. 관계없는 작업을 방해하지도
않습니다: GSM8K 수학 문제 200개에 다른 사람의 기억을 붙였을 때 gpt-5-mini는 191개, 붙이지 않았을 때 193개를
풀었습니다(p = 0.69). [hook](research/runs/graph-v9.14-full500-mpnet8-hook) ·
[interference](research/runs/graph-v9.23-hook-interference)

### 기억이 쌓일수록

아래 그래프는 0.9.47과 그 검색 모델(mpnet)로 잰 것입니다. 0.10의 모델로는 6천 턴에서 답이 든 대화가 상위 30개
안에 든 질문이 92.5%입니다(0.9.47: 82.8%). 더 큰 기억은 아직 재는 중입니다.

기억이 쌓이면 찾기가 어려워집니다. 찾는 대화와 비슷한 과거 대화가 많아지기 때문입니다. 0.9.46부터 Turnbook은
찾아낸 대화를 고정된 공식 대신 작은 학습 점수로 줄 세웁니다. LoCoMo(질문 1,536개)에서 한 사람의
기억 옆에 다른 대화를 넣어 기억을 10배로 키웠을 때, 답이 모델에 전달된 질문의 비율이 이전 방식은
5.6%p(82.5% → 76.9%), 지금은 3.1%p(83.5% → 80.4%) 떨어졌습니다. 각 대화는 나머지 9개로 학습한
가중치로 쟀습니다. 늘린 기억은 다른 사람들의 대화라서, 한 사람의 실제 기록보다 구분하기 쉬웠을 수
있습니다. 같은 변경으로 LongMemEval에서는 답이 모델에 전달된 비율이 97.5%에서 98.3%가 되었습니다.

0.9.47은 이 점수에 더 많은 단서(겹친 단어가 얼마나 드문지, 한 과거 대화에서 몇 개가 함께 찾아졌는지)를 넣고,
검색마다 후보를 50개가 아니라 100개 봅니다. LoCoMo를 20만 5천 턴의 기억 안에 넣었을 때,
답이 든 대화가 상위 30개 안에 든 질문은 79.7%로, 0.9.45의 고정 공식(73.0%)보다 많았습니다. 그 크기에서 검색 한
번은 약 0.4초입니다.

<p align="center"><img src="https://raw.githubusercontent.com/goyohan0611-png/turnbook/main/demo/scale-large.png" alt="6천 턴에서 20만 5천 턴까지 답이 상위 30개 안에 든 비율: 0.9.47은 82.8%에서 79.7%, 단순 순위 결합은 76.8%에서 73.0%" width="720"></p>

<p align="center"><img src="https://raw.githubusercontent.com/goyohan0611-png/turnbook/main/demo/scale.png" alt="기억 1배, 4배, 10배에서의 검색 정확도: 학습 재정렬 83.5%에서 80.4%, 이전 방식 82.5%에서 76.9%" width="720"></p>

### 한국어에서는

LoCoMo 대화 4개를 한국어로 옮겨서(질문 582개) 쟀습니다. 의미 검색만으로, 답이 든 대화가 찾은 것 중
상위 30개 안에 든 질문의 비율입니다.

| 검색 모델 | 영어 | 한국어 | 한국어로 묻고 영어 기억 찾기 |
|---|---|---|---|
| mpnet | 75.6% | 67.5% | 70.6% |
| **EmbeddingGemma** (0.10.0 기본) | **90.2%** | **86.9%** | **87.8%** |

mpnet으로 검색 전체(단어, 의미, 재정렬)를 쓴 0.9.47에서는 한국어 질문의 77.5%(영어 84.7%)가 상위 30개
안에 들었습니다. 단어 검색은 "콩이를"에서 "콩이"를 알아봅니다(조사 처리).
[research/runs/graph-v9.21-korean](research/runs/graph-v9.21-korean)

모든 질문, 답, 채점 결과는 [`research/runs/`](research/runs)에 있고, 방법·실패·재현 방법은
[docs/benchmarks.md](docs/benchmarks.md)에 있습니다.

## 자주 묻는 질문

**PC가 느려지지 않나요?** 백그라운드 서비스는 새 대화가 없을 때 CPU 코어 하나의 약 2%를 씁니다. 앱마다
뜨는 기억 서버는 EmbeddingGemma로 약 525MB(mpnet은 772MB)입니다. 기억 검색은 약 40ms(GitHub 서버에서
95번째 백분위 46ms, mpnet은 26ms), 앱을 켜고 첫 검색은 약 1.4초. 새 대화 한 턴을 임베딩하는 데 약 0.1초이고,
서비스는 보통보다 낮은 CPU 우선순위로 돌며 밀린 임베딩은 절반 속도로 합니다. 처음 설치하면 PC에 있던 대화를
백그라운드에서 읽고, 그동안은 단어 검색이 바로 답합니다.

**검색 모델은 뭔가요?** [EmbeddingGemma](https://huggingface.co/google/embeddinggemma-300m)입니다(약 200MB,
처음 쓸 때 Hugging Face에서 내려받음). 0.9.47 이전부터 쓰던 기억은 예전 모델(mpnet)로 계속 검색하는 동안,
백그라운드 서비스가 할 일이 없을 때 조금씩 EmbeddingGemma로 다시 읽고, 다 읽으면 알아서 넘어가며 예전
모델은 지웁니다. `turnbook model`로 지금 모델을 볼 수 있고, `turnbook model 저장소@파일`로 Hugging Face의
다른 ONNX 임베딩 모델로 바꿀 수 있습니다(기억은 백그라운드에서 새 모델로 다시 읽음). 직접 고른 모델은 지우지 않습니다.
EmbeddingGemma는 [Gemma 이용 약관](https://ai.google.dev/gemma/terms)을 따르며,
Turnbook 자체 코드나 라이선스에 포함되지 않습니다.

**돈이 드나요?** 아니요. API 키도 필요 없고 자체적으로 모델을 부르지 않습니다. 과거가 필요한 질문이
오면 쓰고 있는 AI 앱이 수천 토큰을 읽을 뿐입니다.

**내 데이터가 밖으로 나가나요?** 아니요. 내 다른 PC나 팀과 공유할 때만 나갑니다. 사용 기록 수집도
없습니다. API 키, 비밀번호, 카드 번호 같은 건 저장 전에 가려집니다.

**CLAUDE.md나 앱 자체 메모리 기능과 뭐가 다른가요?** 그쪽은 모델이 쓴 메모를 앱마다 따로 갖고
있습니다. Turnbook는 모든 대화를 원문 그대로 갖고, PC의 모든 앱과 모델이 함께 씁니다.

**한국어 되나요?** 됩니다. 한국어·영어 대화 모두, 질문도 어느 쪽 언어로든. 기본 모델에서 한국어 의미
검색은 영어보다 약 3%p 낮고, 한국어로 물어 영어 기억을 찾는 것도 비슷하게 됩니다:
[한국어에서는](#한국어에서는).

**지우려면?** `turnbook-uninstall`이 모든 앱 연결을 끊고 서비스를 멈춥니다. 기억은 `~/.turnbook`에
남고, `--purge`를 붙이면 기억까지 지웁니다.

> [!NOTE]
> **베타 버전.** Windows에서 매일 쓰고 있습니다. macOS와 Linux는 CI에서 모든 테스트를 통과하지만 아직
> 매일 쓰이지는 않았습니다.

## 더 보기

[MCP 도구](docs/install.md#mcp-tools) · [기억을 3D로 보기](docs/privacy.md#seeing-your-memory)
(`turnbook-view`) · [변경 기록](CHANGELOG.md) · [보안](SECURITY.md)

기여를 환영합니다. 라이선스: [Apache-2.0](LICENSE).

<!-- Links -->
[license-shield]: https://img.shields.io/badge/license-Apache--2.0-4dc9f6?style=flat-square
[license-link]: LICENSE
[python-shield]: https://img.shields.io/badge/python-3.10+-7dd8f8?style=flat-square&logo=python&logoColor=white
[python-link]: https://www.python.org/
[platform-shield]: https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-b0e8ff?style=flat-square
[platform-link]: docs/benchmarks.md#known-limits
[longmemeval-shield]: https://img.shields.io/badge/LongMemEval-91.8%25-2ea44f?style=flat-square
[benchmarks-link]: docs/benchmarks.md
[ci-shield]: https://img.shields.io/github/actions/workflow/status/goyohan0611-png/turnbook/tests.yml?style=flat-square&label=tests
[ci-link]: https://github.com/goyohan0611-png/turnbook/actions/workflows/tests.yml
[pypi-shield]: https://img.shields.io/pypi/v/turnbook?style=flat-square&label=pypi&cacheSeconds=3600
[pypi-link]: https://pypi.org/project/turnbook/
