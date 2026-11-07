<div align="center">

# TERMINATOR

**말 한마디로 집 안의 옛날 기기를 부리는 터미널 속 AI**

*스마트하지 않은 물건에게, 스마트한 손을.*

<img src="docs/led-preview.gif" alt="144×16 LED 전광판에 기차·트리·선물이 지나가는 크리스마스 장면" width="100%">

<sub>위 화면은 실제 전송 코드가 만든 144×16 프레임을 LED 점 모양으로 확대한 것입니다.</sub>

</div>

---

## 한 문장 소개

> **“크리스마스 분위기 내줘.”** 라고 말하면, 터미널 속 AI가 대답하고 **133cm LED 전광판**에 산타와 기차가 달리며 **오래된 블루투스 라디오**에서 캐럴이 흘러나옵니다.

집에는 앱도, 클라우드도, API도 없는 물건이 가득합니다. 블루투스로만 말하는 전광판, 라디오, 리모컨 하나로 움직이는 장난감 기차가 그렇습니다. 스마트홈은 이런 물건을 버리고 새로 사라고 합니다. **Terminator는 반대로 갑니다.** AI가 기존 물건의 통신법을 쥐고, 사람의 말을 그 기기의 언어로 바꿔 직접 부립니다.

## 시연 장면

<table><tr><td><img src="docs/terminal-room.png" alt="도트 그래픽 방을 걷는 AI 캐릭터"></td><td><img src="docs/terminal-talk.png" alt="Space를 누르면 캐릭터가 크게 다가와 요청을 받는 화면"></td></tr><tr><td align="center"><sub>방을 서성이는 AI</sub></td><td align="center"><sub>말을 걸면 다가와 듣는다</sub></td></tr></table>

```
┌──────────────────────────────────────────────────────────┐
│  (도트 그래픽의 방, AI 캐릭터가 서성인다)                    │
│                                                          │
│  [Space]  →  배경이 사라지고 캐릭터가 크게 다가온다           │
│                                                          │
│  > 산타가 날아다니게 해줘. 눈은 펑펑, 캐럴도.                 │
│                                                          │
│  「눈을 많이 내리고, 산타 장면으로 바꿀게. 캐럴도 틀어줄게.」   │
│        │                         │                       │
│        ▼                         ▼                       │
│   LED 전광판 (BLE)          블루투스 라디오 (A2DP)          │
└──────────────────────────────────────────────────────────┘
```

1. 터미널에서 `./terminator --live` 를 실행합니다. 90년대 PC 게임처럼 색이 있는 도트 화면에서 AI 캐릭터가 방을 걷습니다.
2. **Space**를 누르면 배경이 사라지고 캐릭터가 화면 가득 다가와 말을 기다립니다.
3. 한국어로 말하거나(**Ctrl+V** 녹음) 입력합니다. 예: “기차 더 빨리”, “하늘을 보라색으로”, “음악은 꺼줘”.
4. AI가 요청을 **허용된 동작의 계획**으로 바꾸고, 전광판과 라디오가 동시에 바뀝니다.
5. **Esc** 한 번이면 모든 기기가 멈춥니다.

## 무엇이 다른가

| | 기존 스마트홈 | Terminator |
|---|---|---|
| 대상 기기 | 회사 앱·클라우드에 등록된 신제품 | 이미 집에 있는 블루투스·적외선 기기 |
| 연결 방식 | 제조사 클라우드 경유 | PC가 기기 통신법으로 **직접** 전송 |
| 명령 방식 | 앱 버튼, 정해진 음성 명령 | 자유로운 한국어 문장 → 검증된 계획 |
| 여러 기기 | 기기마다 따로 | 한 문장이 화면·소리를 **한 장면**으로 묶음 |
| 인터페이스 | 대시보드 | 대화하는 캐릭터가 있는 터미널 |

## 구조

```mermaid
flowchart LR
    U["말 / 입력"] --> ASR["음성 인식<br/>faster-whisper"]
    ASR --> CLI["터미널 캐릭터<br/>companion_cli"]
    U --> CLI
    CLI --> PLAN["의도 해석<br/>로컬 LLM (Ollama)<br/>JSON Schema 고정"]
    PLAN --> CTRL["쇼 컨트롤러<br/>검증 · 취소 · 상태"]
    CTRL --> REN["장면 렌더러<br/>144×16 애니메이션"]
    REN --> LED["LED 전광판<br/>iPixel BLE"]
    CTRL --> RADIO["블루투스 라디오<br/>Windows 오디오 장치"]
```

AI가 기기를 마음대로 조작하지 않도록 경계를 두었습니다.

- **언어 모델은 계획만 만듭니다.** 출력은 JSON Schema로 고정되어 장면, 속도, 눈, 하늘색, 음악, 볼륨만 바꿀 수 있습니다. 지원하지 않는 요청은 `unsupported`로 돌려보내고 일부만 몰래 실행하지 않습니다.
- **기기 조작은 컨트롤러만 합니다.** 계획을 다시 검증한 뒤 장면을 그려 보내며, 실행 중 새 요청이나 Esc가 오면 이전 작업을 취소합니다.
- **기본은 미리보기입니다.** `--live`와 기기 주소를 함께 지정했을 때만 실물로 내보냅니다.

## 사용한 오픈소스와 기술

| 영역 | 사용 기술 |
|---|---|
| LED 전광판 전송 | [pypixelcolor](https://github.com/lucagoc/pypixelcolor) 0.5.0 (iPixel Color BLE 프로토콜), [Bleak](https://github.com/hbldh/bleak) |
| 통신 규약 참고 | [go-ipxl](https://github.com/yyewolf/go-ipxl), [ha-ipixel-color](https://github.com/cagcoach/ha-ipixel-color) |
| 의도 해석 | [Ollama](https://ollama.com) 로컬 모델 + JSON Schema 구조화 출력 |
| 음성 인식 | [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (선택) |
| 장면·도트 렌더링 | [Pillow](https://python-pillow.org) |
| 라디오 재생 | Windows Runtime `MediaPlayer` + 블루투스 오디오 장치 지정 |

## 실행

Python 3.10 이상이 필요합니다.

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
./terminator            # 미리보기: 기기를 건드리지 않습니다
```

실물 기기로 내보내려면 Windows에서 다음을 설정합니다.

```powershell
py -m venv .venv
.venv\Scripts\pip install -r requirements-live.txt
$env:TERMINATOR_LED_ADDRESS = "<전광판 BLE 주소>"
$env:TERMINATOR_RADIO_DEVICE = "<블루투스 라디오 장치 이름>"
$env:TERMINATOR_OLLAMA_MODEL = "qwen2.5:7b"
.\terminator.cmd --live
```

| 키 | 동작 |
|---|---|
| Space | 캐릭터에게 말 걸기 |
| Enter | 요청 보내기 |
| Ctrl+V | 음성 녹음 시작/끝 (`TERMINATOR_ASR_URL` 설정 시) |
| Ctrl+L | 미리보기 ↔ 실물 전환 |
| Esc | 모든 기기 정지 |
| Ctrl+C | 종료 |

음성 인식 서버는 `src/local_asr_server.py`를 `requirements-voice.txt` 환경에서 실행합니다.

## 검증

```sh
PYTHONPATH=src python3 -m unittest discover -s tests
```

99개 테스트가 의도 해석 스키마, 장면 렌더링, 전광판 전송 계획, 취소·정지, 음성 업로드 경계, 터미널 화면을 확인합니다. 실물 BLE·오디오 장치가 필요한 10개는 장치가 없으면 건너뜁니다.

## 실물 검증 상태

정직하게 구분해 둡니다.

| 기기 | 상태 |
|---|---|
| 디라이브 133×19.2cm LED 전광판 (iPixel Color, 144×16) | 기종 조회, 문구·GIF 전송과 표시 확인 |
| 코비 CRF-35B 블루투스 라디오 | 캐럴 재생 청취 확인 |
| MYKA FORT 90995 리모컨 기차 | 리모컨이 적외선 방식임을 확인. 적외선 송신 경로 연결 진행 중 |

## 다음 단계

- **적외선 기기 연결:** 리모컨 신호를 배워 다시 쏘는 송신기를 붙여 기차·선풍기·TV 같은 적외선 가전을 같은 문장으로 부립니다.
- **카메라 확인:** 웹캠으로 실제 결과를 보고, 의도와 다르면 AI가 다시 조정합니다.
- **통신법 학습:** 알려진 오픈소스가 없는 기기도 관찰로 명령의 의미를 찾아 연결합니다.

## 팀

OBA.RUN 2nd 해커톤 출품작입니다.
