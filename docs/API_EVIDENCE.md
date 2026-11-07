# API·오픈소스 활용 근거

이 표는 저장소에서 확인되는 사용 경로입니다. **통합 코드 존재, 모의 응답 시험, 로컬 네트워크 시뮬레이터, 실물 관찰은 서로 다른 근거**입니다. 이번 QA에서 유료 API나 실물 기기를 호출하지 않았습니다. 공식 행사 스폰서 목록·인정 조건은 확인하지 않았으므로 아래 서비스 사용을 공식 스폰서 적합성 인증으로 표현하지 않습니다.

## 대화·계획·음성

| 구성 | 사용 위치·이유 | 현재 검증과 남은 확인 |
|---|---|---|
| OpenAI Responses | [kevin_bridge.py](../src/kevin_bridge.py)의 `openai_json`, `plan`, `see_person`; [kevin_agent.py](../src/kevin_agent.py)의 `decide`. 대화·현재 상태를 구조화된 LED/음악 계획으로 변환하고 카메라 이미지의 사람 여부를 묻는 경로 | [test_openai_planner.py](../tests/test_openai_planner.py)는 응답·오류 mock, [test_bridge_safety.py](../tests/test_bridge_safety.py)는 지연된 계획/관측의 취소를 mock으로 검증. 이번 QA의 실제 서비스 응답·비용·시각 인식 정확도 근거는 없음 |
| ElevenLabs TTS | `kevin_bridge.tts`가 대사를 MP3로 받고 메모리 캐시 사용. [app.js](app/app.js)의 `speakOut`이 재생하며 실패 시 브라우저 음성 합성으로 전환 | 코드 경로 확인. 이번 QA의 실제 합성 품질·음성 지연·요금·Windows 스피커 출력 검증 없음. 캐시 존재가 비용 절감률 실측을 뜻하지 않음 |
| 로컬 Ollama 계획 | [show_intent.py](../src/show_intent.py)의 `interpret`, `ground`. 외부 계획 실패 시 로컬 해석 경로와 명시 단어 보정 | [test_show_intent.py](../tests/test_show_intent.py)의 규칙·입출력 검사. 로컬 서버/모델이 준비돼야 동작하며 모든 웹 대화·TTS·vision 기능의 완전한 오프라인 동등성을 주장하지 않음 |

모델 이름은 구성 기본값일 뿐 실제 계정에서의 사용 가능성·우수성·스폰서 인정 증거가 아닙니다. 키는 브리지에서 읽으며 클라우드 경로를 사용하면 요청 텍스트·이미지 등 해당 입력이 서비스에 전송됩니다.
