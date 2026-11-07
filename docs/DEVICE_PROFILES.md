# 기기 프로필

`./kevin`은 기존 전광판·라디오를 기본 프로필로 실행한다. 미리보기는 모델 해석과 로컬 렌더링만 하며 장비에 연결하지 않는다.

```sh
./kevin --list-devices
./kevin --profile profiles/local-light.example.json --list-devices
./kevin --profile profiles/local-light.example.json
```

프로필은 version 1 JSON이며 devices마다 id, name, aliases, adapter, config가 필요하다.
ID와 별칭은 기기끼리 겹칠 수 없다. `--list-devices`는 연결 설정을 출력하지 않는다.

## 직접 WLED

local-light 예제는 기존 쇼와 진열대 WLED를 함께 등록한다. WLED는 이미 로컬 네트워크에 설치되어 있어야 한다.
주소는 `KEVIN_WLED_URL` 환경변수(예: 스킴을 포함한 장치의 로컬 주소)로 설정한다.
segment_ids에는 실제 구성된 구역 ID만 등록한다. 자동 구역 생성이나 위치 추정은 하지 않는다.
기존 쇼의 실물 실행에는 기존 LED 주소와 CRF-35B 설정이 필요하다.
WLED만 요청하면 쇼 기기 설정은 필요하지 않다.

## 선택형 Home Assistant

home-assistant 예제의 entity_id와 학습된 IR device/command 이름을 자신의 값으로 교체한다.
`KEVIN_HA_URL`과 `KEVIN_HA_TOKEN`을 실행 환경에 설정한다.
토큰은 HA 사용자 권한을 가지므로 이 앱의 허용 목록과 같은 권한 범위라고 가정하지 않는다.
로컬 HTTP에서는 토큰이 암호화되지 않으므로 신뢰하는 로컬 연결이나 HTTPS를 사용한다.
앱은 요청한 switch의 turn_on/off 또는 등록한 remote 명령만 호출하고 토글·반복·임의 서비스는 받지 않는다.

## 실물 실행과 결과

```sh
./kevin --live --profile profiles/local-light.example.json
```

Ctrl+L은 다음 한 요청의 실물 실행을 선택한다. 그 요청의 모든 대상이 준비되어야 전송한다.
전송 성공은 물리 관찰과 다르다. WLED/HA 상태는 기기가 보고한 값이며 IR은 송신 결과만 알려 준다.
상태 읽기 실패 때문에 이미 보낸 명령을 다시 보내지 않는다. Esc는 대기 작업을 취소한다.
이미 전송된 IR은 되돌릴 수 없으며 스위치를 복구 목적으로 자동 토글하지 않는다.

장비 주소·토큰이 들어간 개인 프로필은 커밋하지 않는다. 이 예제에는 실제 장비 주소나 인증정보가 없다.
10개 트랙의 발산과 구현 경계는 [USE_CASE_ALIGNMENT](USE_CASE_ALIGNMENT.md)를 참고한다.
