# 웹 브리지 접근 범위

브리지는 기본적으로 실제 서버 포트의 `127.0.0.1`, `localhost`, `[::1]` Host만 받습니다. 브라우저 Origin은 같은 브리지 주소와 `https://sergiobuilds.github.io`를 허용합니다. 다른 사이트의 명령·카메라 접근과 DNS rebinding Host는 403으로 거절합니다. POST는 JSON 객체여야 합니다.

별도 로컬 UI나 신뢰하는 터널이 필요하면 브리지 시작 전에 명시적으로 설정합니다. 값은 쉼표로 구분하며 Host는 포트가 있으면 포함하고 Origin은 scheme·host·port까지 정확히 적습니다.

```sh
export KEVIN_BRIDGE_ALLOWED_HOSTS='your-trusted-tunnel.example'
export KEVIN_BRIDGE_ALLOWED_ORIGINS='https://your-ui.example,http://127.0.0.1:8080'
```

허용한 터널 Host의 HTTP/HTTPS 같은 출처도 허용됩니다. 이 설정은 사용자 인증이 아닙니다. 공개 터널을 허용하면 그 URL을 아는 비브라우저 클라이언트도 접근할 수 있으므로 터널 측 접근 제어를 함께 구성해야 합니다. 브라우저 보안 정책에 따라 HTTPS 페이지에서 로컬 HTTP 연결은 별도 허용이 필요할 수 있습니다.

`KEVIN_CAMERA=off`는 브리지 호스트의 카메라 자동 탐색을 끕니다. 웹 페이지의 브라우저 카메라는 별도의 브라우저 권한입니다. 웹 정지는 요청 접수와 기기 확인을 구분하며, 연결 오류나 장치 정지 실패를 성공으로 표시하지 않습니다.
