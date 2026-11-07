# API·오픈소스 활용 근거

이 표는 저장소에서 확인되는 사용 경로입니다. **통합 코드 존재, 모의 응답 시험, 로컬 네트워크 시뮬레이터, 실물 관찰은 서로 다른 근거**입니다. 이번 QA에서 유료 API나 실물 기기를 호출하지 않았습니다. 공식 행사 스폰서 목록·인정 조건은 확인하지 않았으므로 아래 서비스 사용을 공식 스폰서 적합성 인증으로 표현하지 않습니다.

## 대화·계획·음성

| 구성 | 사용 위치·이유 | 현재 검증과 남은 확인 |
|---|---|---|
| OpenAI Responses | [kevin_bridge.py](../src/kevin_bridge.py)의 `openai_json`, `plan`, `see_person`; [kevin_agent.py](../src/kevin_agent.py)의 `decide`. 대화·현재 상태를 구조화된 LED/음악 계획으로 변환하고 카메라 이미지의 사람 여부를 묻는 경로 | [test_openai_planner.py](../tests/test_openai_planner.py)는 응답·오류 mock, [test_bridge_safety.py](../tests/test_bridge_safety.py)는 지연된 계획/관측의 취소를 mock으로 검증. 이번 QA의 실제 서비스 응답·비용·시각 인식 정확도 근거는 없음 |
| ElevenLabs TTS | `kevin_bridge.tts`가 대사를 MP3로 받고 메모리 캐시 사용. [app.js](app/app.js)의 `speakOut`이 재생하며 실패 시 브라우저 음성 합성으로 전환 | 코드 경로 확인. 이번 QA의 실제 합성 품질·음성 지연·요금·Windows 스피커 출력 검증 없음. 캐시 존재가 비용 절감률 실측을 뜻하지 않음 |
| 로컬 Ollama 계획 | [show_intent.py](../src/show_intent.py)의 `interpret`, `ground`. 외부 계획 실패 시 로컬 해석 경로와 명시 단어 보정 | [test_show_intent.py](../tests/test_show_intent.py)의 규칙·입출력 검사. 로컬 서버/모델이 준비돼야 동작하며 모든 웹 대화·TTS·vision 기능의 완전한 오프라인 동등성을 주장하지 않음 |

모델 이름은 구성 기본값일 뿐 실제 계정에서의 사용 가능성·우수성·스폰서 인정 증거가 아닙니다. 키는 브리지에서 읽으며 클라우드 경로를 사용하면 요청 텍스트·이미지 등 해당 입력이 서비스에 전송됩니다.

## 장치 라이브러리와 프로토콜

| 재사용 | 구현·목적 | 검증 범위 |
|---|---|---|
| pypixelcolor 0.5.0 | [show_transport.py](../src/show_transport.py), BLE 프레임 인코딩·ACK·native 표시 명령 | [test_pypixelcolor_contract.py](../tests/test_pypixelcolor_contract.py), [test_show_control.py](../tests/test_show_control.py): 라이브러리 + fake BLE session. 행사장 LED 기록은 [README 실물 표](../README.md)와 구분 |
| python-wled | [devices/light.py](../src/devices/light.py), 구역 밝기 변경·재조회 | [test_device_light.py](../tests/test_device_light.py), [test_wled_sim.py](../tests/test_wled_sim.py): 로컬 HTTP 가상 장치. 실제 WLED 하드웨어 확인 아님 |
| adb-shell / python-broadlink | [devices/tv.py](../src/devices/tv.py), [devices/ir.py](../src/devices/ir.py): ADB 상태·복원, IR 학습·전송 | [test_device_tv.py](../tests/test_device_tv.py), [test_device_ir.py](../tests/test_device_ir.py): 로컬 TCP/UDP 시뮬레이터. 실제 TV·IR 수신 대상의 결과와 다름 |
| HA REST / Valetudo API | [devices/wallpad.py](../src/devices/wallpad.py), [devices/vacuum.py](../src/devices/vacuum.py): 설정한 조명 채널·청소 구역만 제어 | 로컬 HTTP 가상 서버와 재조회 검사. HA·Valetudo 소스/펌웨어를 실행한 검증이나 월패드 RS485 직접 구현이 아님 |
| Pillow / cryptography | [receipt.py](../src/receipt.py): 영상 변화 계산·사진 해시·Ed25519 묶음 | [test_receipt.py](../tests/test_receipt.py): 생성 이미지·무결성 회귀. 실물 목표 달성·신뢰할 서명자 신원 인증은 별도 |

정확한 버전·라이선스·출처와 코드 복사 여부는 [데모 문서의 재사용 표](HACKATHON_DEMO.md#출처와-재사용-범위)를 기준으로 합니다. 외부 프로젝트를 자체 개발 성과나 후원 관계로 표현하지 않습니다.

## 아직 필요한 API 비교 실험

같은 한국어 요청 집합과 장치 초기 상태에서 클라우드/로컬 경로를 비교해야 합니다. 기록 항목은 요청→검증된 계획의 지연(p50/p95), 허용 계획 정확도·수정률, 실패/취소율, 요청당 사용량·실제 청구액, 로컬 자원 사용량입니다. TTS는 캐시 적중/미적중과 청취 평가를 분리하고 vision은 사람·빈방·조명 변화별 오탐/미탐을 구분합니다. **현재 이 비교 결과는 없으며** 모델 성능이나 비용 우위를 수치로 주장하지 않습니다. 공식 스폰서 목록과 API별 인정 조건은 발표 전에 별도로 확인할 항목입니다.
