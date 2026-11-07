# 기여 안내 — 기기 하나 붙이기

10개 트랙을 여러 사람이 동시에 개발합니다. 서로의 작업을 막지 않도록 아래 규칙만 지켜 주세요.

## 1. 트랙 고르기

[ROADMAP.md](ROADMAP.md)에서 트랙을 고르고, 해당 GitHub 이슈에 본인을 할당합니다. 브랜치는 `device/<slug>` 입니다 (예: `device/tv`).

## 2. 어댑터 만들기

`src/devices/<slug>.py`에 [`DeviceAdapter`](src/devices/base.py) 규격을 구현합니다.

- `capabilities`: AI가 바꿀 수 있는 항목만 선언합니다. 선언하지 않은 항목은 기기에 닿지 않습니다.
- `apply(plan, cancel)`: 기기의 원래 통신법(BLE, IR, RS485, ADB, LAN …)으로 직접 보냅니다. 제조사 앱·클라우드를 거치지 않는 경로를 우선합니다. `cancel`이 켜지면 즉시 멈춥니다.
- `stop()`: 기기를 안전하고 조용한 상태로 둡니다. 여러 번 불러도 같은 결과여야 합니다.
- `status()`: 지금 상태를 사실대로 돌려줍니다. 화면·소리를 직접 확인하지 않았으면 `physically_verified=False`입니다.

## 3. 테스트

`tests/test_device_<slug>.py`는 **실물 없이** 돌아야 합니다. 가상 장치나 기록한 응답으로 확인합니다. 실물 시험은 `--live`일 때만 하고, 결과는 PR에 사진·영상·로그로 남깁니다.

```sh
PYTHONPATH=src python3 -m unittest discover -s tests
```

## 4. PR 체크리스트

- [ ] 다른 트랙의 파일을 고치지 않았다 (공통 파일 변경은 PR 설명에 이유를 적는다)
- [ ] 기기 주소·토큰·계정 정보를 커밋하지 않았다
- [ ] 실물 결과와 가상 결과를 구분해 적었다
- [ ] README 실물 검증 표에 한 줄을 추가했다 (실물 확인 시)
