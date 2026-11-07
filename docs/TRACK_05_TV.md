# 05 Android TV — 현재 앱 기억·전환·복귀

## 지금 실행되는 범위

실제 **adb-shell 0.4.4 클라이언트**가 loopback TCP의 작은 ADB peer에 연결한다. CNXN → OPEN(shell) → OKAY/WRTE/CLSE 왕복을 거쳐 현재 foreground 앱을 읽고, 등록된 다른 앱을 연 다음 원래 앱으로 복귀한다. TCP와 ADB 직렬화는 실제 upstream 코드가 수행한다. 반대편의 앱 상태와 `dumpsys` 응답은 명시적인 시뮬레이터다.

`source=simulation`, `simulated=true`, `physically_verified=false`를 모든 결과에 기록한다. Android OS·실제 TV·앱 설치·화면·영상·채널·재생 위치를 검증했다는 뜻이 아니다. `verification_scope=foreground_app_only`, `playback_position_restored=false`가 이 구분을 나타낸다.

```sh
PYTHONPATH=src:. .venv/bin/python -m demos.tv_sim.server --demo
PYTHONPATH=src:. .venv/bin/python -m unittest discover -s tests -p 'test_device_tv.py'
```

데모 순서: **home snapshot → youtube launch → home restore**. 데모의 `.MainActivity` component는 시뮬레이터 입력이며 해당 실제 앱 버전의 launchable activity라고 확인한 값이 아니다.

## 통합 인터페이스

```python
from demos.tv_sim.server import start_fixture
import threading

server, adapter = start_fixture()  # loopback, ephemeral port, already serving
adapter.apply({'action': 'snapshot'}, threading.Event())
adapter.apply({'action': 'launch', 'app': 'youtube'}, threading.Event())
adapter.apply({'action': 'restore'}, threading.Event())
adapter.status()               # current_app, saved_app, source, apps
server.device.snapshot()       # simulator state and ADB/shell traces
server.device.set_fault('ignore')  # none / offline / ignore / timeout / empty
adapter.stop()                 # cancels pending control, leaves app unchanged
server.device.reset()
adapter.reset()                # forgets saved snapshot, no network/device effect
server.shutdown()
server.server_close()
```

`start_fixture`가 사용하는 앱 별칭은 `home`, `youtube`다. `make_server(host='127.0.0.1', port=0, apps=None)`는 아직 실행하지 않은 server를 반환한다. 시뮬레이터는 loopback 주소에만 바인딩한다. 전체 데모 reset은 실행 중 작업이 끝난 후 수행한다.

## 실제 기기용 연결 경로

```python
from devices.tv import AndroidTVAdapter

adapter = AndroidTVAdapter(
    '192.0.2.10', 5555,  # example; supply the owner's configured numeric TV IP
    apps={'home': '<verified-package>/<verified-activity>',
          'youtube': '<verified-package>/<verified-activity>'},
    rsa_keys=[approved_signer], simulated=False, timeout=2,
)
```

실물에서는 이미 허용된 ADB TCP 접근, TV에서 승인한 키, 해당 기종·앱 버전의 실제 component를 준비해야 한다. `rsa_keys`는 upstream signer 객체다. 어댑터는 키를 생성·복사·저장하거나 ADB 권한을 자동 승인하지 않는다. Android의 새 TLS wireless pairing을 구현한 것이 아니며, adb-shell이 지원하는 기존 ADB TCP 연결 경로가 필요하다. 실제 TV 연결은 이번 검증에 포함하지 않았다.

기기 주소와 앱 매핑은 신뢰된 생성자 설정이며 계획에서 받을 수 없다. 계획에는 등록된 별칭만 들어간다. 생성자는 숫자 IP와 검증한 component 형태를 요구하며 shell 인수는 인용 처리한다. 기기에 보내는 명령은 고정 `dumpsys window windows`와 등록된 `am start -n <component>`뿐이다. 임의 shell·패키지·네트워크 주소 입력은 거절한다.

현재 파서는 `mCurrentFocus=Window{… package/activity}` 형식을 처리한다. 다른 Android 버전에서 해당 출력이 없거나 현재 패키지가 허용 목록에 없으면 성공을 꾸미지 않고 실패한다. 한 패키지에 한 별칭을 등록한다. 비교하는 것은 activity나 콘텐츠 전체가 아닌 package에 대응하는 foreground 앱이다. 느린 앱 전환에서 첫 readback이 목표와 다르면 실패로 보고하므로 실물 연결 후 전환 지연을 측정해야 한다.

## 상태 보존·실패 처리

- `snapshot`이 있어야 `restore`할 수 있다. 비어 있거나 인식할 수 없는 새 snapshot은 이전 저장값을 지운다.
- `launch`는 현재 foreground를 읽고 등록된 앱을 실행한 후 다시 읽는다. ACK만으로 성공하지 않는다.
- 복귀 전 현재 앱이 저장된 앱 또는 이 어댑터가 마지막으로 연 앱이어야 한다. 사용자가 다른 앱으로 바꿨다면 복귀를 거절한다.
- 명령이 이미 송신된 뒤 취소하면 앱이 열린 상태로 남을 수 있다. `stop`은 TV 전원이나 앱을 종료하지 않고 대기 중 제어만 취소한다.
- 작업은 전체 deadline과 취소 이벤트를 사용한다. upstream TCP 읽기/쓰기에 짧은 polling을 더해 응답 없는 peer에서도 중단한다. 동시 작업은 `busy`로 거절한다.
- 실패·timeout·취소도 물리 검증을 주장하지 않는다. 원시 주소·키·예외 문자열은 결과에 노출하지 않는다.

## 출처와 재사용

- [adb-shell 0.4.4](https://pypi.org/project/adb-shell/0.4.4/), [upstream 저장소](https://github.com/JeffLIrion/adb_shell), **Apache-2.0**: 의존성으로 사용. `AdbDevice`, `TcpTransport`, ADB 패킷 직렬화·응답 처리를 실제 호출한다. upstream 소스를 복사하지 않았다.
- [AdbDevice API](https://adb-shell.readthedocs.io/en/stable/adb_shell.adb_device.html): public transport injection·connect·shell API 근거.
- [Android ADB 공식 안내](https://developer.android.com/tools/adb): ADB 연결과 activity manager `am start -n` 명령 근거.
- [AOSP ADB protocol](https://android.googlesource.com/platform/packages/modules/adb/+/refs/heads/main/protocol.txt): 독립적으로 작성한 최소 시뮬레이터의 wire format 근거.
- [python-androidtv](https://github.com/JeffLIrion/python-androidtv), **MIT**: 비교·검토만 했다. 이번 어댑터는 더 작은 adb-shell 의존성을 사용하며 python-androidtv를 설치·복사하지 않는다.

검증은 실제 OSS 클라이언트와 loopback 프로토콜 peer 사이의 왕복, 상태 보존·입력 거절·연결 실패·timeout·취소다. 실물 성공 근거를 추가하기 전까지 장비 호환 표에는 가상/프로토콜 검증으로 기록한다.
