# KEVIN 실행·관찰 데모

기존 LED·라디오 제어를 보완하고, 실제 오픈소스 클라이언트가 가상 WLED 장치와 통신하는 02/08 확장을 제공합니다. 04–07의 실물 연결이나 09/10은 이 변경에 포함되지 않습니다. 모델 API·BLE·카메라 권한 없이 확장 데모를 실행할 수 있습니다.

## 확장 데모 실행

Python 3.11 이상:

```sh
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements-demo.txt
.venv/bin/python src/demo_server.py
```

Windows에서는 `py -3.11 -m venv .venv` 후 `.venv\Scripts\python.exe`로 같은 명령을 실행합니다. 브라우저에서 `http://127.0.0.1:8790`을 엽니다. 다른 장비에서 리뷰할 때는 `--host <호스트의 사설 IP> --port <포트>`를 명시합니다. 정적 파일은 `docs/demo/`의 정해진 파일만, 영수증은 `.runtime/demo/receipts/`의 허용된 파일만 제공합니다. `KEVIN_LED_ADDRESS` 등 실물 설정이 있어도 이 서버는 자체 loopback 가상 장치만 사용합니다.

데모의 흐름:

```text
브라우저 → demo_server → WLEDLightAdapter → python-wled 1.1.1
                                            ↓ HTTP
                               가상 WLED /json/state
                                            ↓ 상태 재조회
                         지정 구역 일치 + 나머지 구역 보존 확인
                                            ↓
                         합성 전후 장면 + 서명된 관찰 기록
```

시뮬레이터는 WLED 펌웨어를 실행하지 않습니다. 공식 JSON API의 필요한 부분을 구현한 테스트 상대입니다. payload 구성·기기 모델 파싱은 실제 upstream 라이브러리가 담당합니다. 상태가 맞는다는 결과와 물리 검증을 분리해 항상 `physically_verified=false`를 반환합니다.

## 90초 시연 순서

1. **구역 하나만 제어:** 진열대를 선택하고 밝기를 바꿉니다. 현관·작업대가 유지되고 “상태 일치 / 다른 구역 보존”을 확인합니다.
2. **관찰 후 실행:** “현관이 어두워”를 누릅니다. 실제 WLED 클라이언트 호출 뒤 상태와 합성 화면의 밝기를 확인합니다. 서명된 기록에서 `source=simulation`을 확인합니다.
3. **응답과 결과의 차이:** “켜졌는데 안 밝아”를 누릅니다. API 상태는 맞지만 영상 조건은 실패한 결과를 보여줍니다.
4. **장치가 명령을 무시:** 체크 후 다시 어두운 현관을 실행합니다. HTTP 200이어도 상태가 바뀌지 않으면 실패합니다.
5. **관찰이 오래됨:** 새 명령과 영수증을 만들지 않습니다. “이미 충분히 밝아”도 조명 요청을 보내지 않습니다.

관찰 시나리오는 현관 구역의 합성 초기 상태를 준비합니다. 다른 구역과 실패 설정은 유지합니다. 영상 밝기는 lux가 아니며, 이 데모가 실제 현관의 광학·카메라 조건을 검증한 것은 아닙니다.

## 기존 실물 경로에서 바뀐 동작

- LED 또는 라디오 하나만 설정해도 해당 기기를 실행할 수 있습니다. 복합 요청은 필요한 모든 설정을 먼저 검사합니다.
- 미리보기의 장면·음악 상태가 다음 실물 요청으로 넘어가지 않습니다.
- LED만 요청하면 라디오를 재생하지 않고, 라디오만 요청하면 LED를 전송하지 않습니다.
- 음량만 요청하면 기존 플레이어의 음량만 바꿉니다. 재생·재개·프로세스 시작을 하지 않습니다. 플레이어가 없으면 다음 재생 설정값만 보관하고 `volume_applied=false`를 반환합니다.
- 플래너가 거절한 혼합 요청은 거절을 유지합니다. 다른 기기의 “켜줘”를 라디오 재생으로 보정하지 않습니다.
- MCP는 실행 중에도 `stop_all`을 읽습니다. 이전 작업의 취소 확인과 새 명령 등록 사이를 보호하며, 취소 후 늦은 카메라 결과를 성공 영수증으로 반환하지 않습니다.
- 카메라가 없거나 고장 나도 기기 실행 결과는 남기고 관찰 불가를 표시합니다. 촬영된 변화는 오디오 청취나 요청 의미의 성공으로 확대하지 않습니다.

## 현장 실물 확인

실제 장치가 연결된 Windows 사용자 세션에서 기존 `requirements-live.txt`를 설치합니다. 이 변경에서 Mini가 현장 장비에 접속하거나 성공 영상을 촬영한 것은 아닙니다.

```powershell
.venv\Scripts\python.exe -m pip install -r requirements-live.txt
$env:KEVIN_SHOW_LIVE = '1'
$env:KEVIN_LED_ADDRESS = '<현장의 기존 전광판 주소>'
$env:KEVIN_RADIO_DEVICE = 'CRF-35B'
$env:KEVIN_CAMERA = '0'
# ROI는 실제 캡처 해상도 안의 x,y,width,height를 지정
$env:KEVIN_CAMERA_ROI = '<x,y,width,height>'
.\kevin.cmd --live
```

MCP 클라이언트는 같은 환경에서 `.venv\Scripts\python.exe src/kevin_mcp.py`를 stdio 서버로 실행합니다. 현장에서 먼저 `led_scene`의 명시적 preset과 `radio`의 play/keep+volume/stop을 확인하고, 이후 `say`를 사용합니다. 비어 있는 LED 명령과 아무 변경 없는 radio keep은 거절합니다.

LED 장면 변경 → 음악 재생 → 음량만 변경 → 음악만 정지 → 전광판이 유지되는지 → 전체 정지를 3회 반복하는 것을 권합니다. LED가 화면에서 실제로 반복 재생되는지와 라디오 소리가 들리는지는 현장에서 별도로 확인해야 합니다.

WLED 실물이 준비되면 `requirements-demo.txt`도 설치하고, 신뢰한 설정에 `KEVIN_WLED_HOST`와 `KEVIN_SHOW_LIVE=1`을 지정해 MCP의 `light_segment` 도구를 노출할 수 있습니다. 이 경우에도 API readback은 `device_reported`이고 물리 검증은 false입니다. 실물 WLED 호환성은 이번에 확인하지 않았습니다. `stop_all`은 WLED의 진행 중 요청을 취소하며 조명을 강제로 전부 끄지 않습니다.

## 영수증 검증

```sh
.venv/bin/python src/receipt.py .runtime/demo/receipts/<영수증 ID>
```

`source`는 `camera / simulation / recording / unspecified`, `scope`는 `visual_change_only`입니다. `changed/moving`은 영상 차이이며 목표 의미나 소리를 증명하지 않습니다. Ed25519는 첨부 이미지 해시와 서명된 기록의 무결성을 확인합니다. 공개키를 영수증 안에서 읽는 기본 검증은 별도로 신뢰한 서명자 신원을 보증하지 않습니다.

## 출처와 재사용 범위

| 구성 | 출처·버전 | 실제 사용 범위 |
|---|---|---|
| WLED 클라이언트 | [frenck/python-wled](https://github.com/frenck/python-wled), PyPI `wled==1.1.1`, MIT | 의존성으로 설치. `update → segment → update`, payload·모델 파싱 재사용. 코드 vendoring 없음 |
| 프로토콜 | [WLED JSON API](https://kno.wled.ge/interfaces/json-api/) | `/json`, `/json/state`, `/json/si`와 catalog 응답의 필요한 부분을 자체 시뮬레이터로 구현. 전체 펌웨어 호환 주장 없음 |
| 영수증 | 기존 KEVIN `receipt.py`; [Pillow](https://github.com/python-pillow/Pillow), [cryptography](https://github.com/pyca/cryptography) | 기존 영상 비교·Ed25519를 사용하고 출처·ROI·누락 파일 검증 보완 |
| 실물 제어 | 기존 KEVIN `show_transport.py`; [pypixelcolor](https://github.com/lucagoc/pypixelcolor) `0.5.0` | 기존 BLE 경로 유지. Windows MediaPlayer에 재생 없는 음량 변경 추가 |

python-wled의 포트 80 목적지를 시뮬레이터의 지정 포트로 옮기는 작은 session wrapper를 추가했습니다. 목적지 변경과 HTTP redirect는 허용하지 않습니다. 라이브러리 소스를 복사하지 않았으며 의존성의 원본 라이선스를 따릅니다.

## 검증 명령

```sh
# 전체 기존 테스트까지 실행할 환경
.venv/bin/python -m pip install -r requirements-live.txt -r requirements-demo.txt
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -q
node --check docs/demo/demo.js
git diff --check
```

WLED와 데모 테스트는 실제 loopback HTTP를 사용합니다. 장치 응답 무시·다른 구역 변경·시간 초과·취소와 재사용, 오래된 관찰, 영상 판정 실패, 영수증 누락·변조를 확인합니다. 실제 모델 API·현장 BLE·Windows 오디오·웹캠의 검증을 대체하지 않습니다.
