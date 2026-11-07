"""Two explicit HA mappings. Neither discovery nor arbitrary service calls."""
import json
import re
import threading
import urllib.error
import urllib.parse
import urllib.request

from devices.base import Capability, Result
from devices.registry import DeviceError
from show_transport import check_cancel


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class HomeAssistant:
    slug = "home_assistant"

    def __init__(self, kind, settings, config):
        required = {"url_env", "token_env", "entity_id"}
        if kind == "ha_remote":
            required |= {"device", "commands"}
        if set(settings) != required:
            raise DeviceError("HA 기기 설정 필드를 확인하세요.")
        for name in ("url_env", "token_env"):
            if not isinstance(settings[name], str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", settings[name]):
                raise DeviceError("HA 환경변수 이름을 확인하세요.")
        domain = "switch" if kind == "ha_switch" else "remote"
        if not isinstance(settings["entity_id"], str) or not re.fullmatch(domain + r"\.[a-z0-9_]+", settings["entity_id"]):
            raise DeviceError("HA 기기 종류와 entity_id가 다릅니다.")
        if kind == "ha_remote":
            commands = settings["commands"]
            if (not isinstance(commands, dict) or not 1 <= len(commands) <= 32 or
                    not isinstance(settings["device"], str) or not 1 <= len(settings["device"]) <= 80):
                raise DeviceError("IR 학습 기기와 명령 목록을 확인하세요.")
            if any(not isinstance(k, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,47}", k) or
                   not isinstance(v, str) or not 1 <= len(v) <= 100 or v.startswith("b64:")
                   for k, v in commands.items()):
                raise DeviceError("IR은 미리 학습한 이름 있는 명령만 등록하세요.")
            self.capabilities = (Capability("command", "Press one previously learned remote button; no inferred power state.",
                                            {"type": "string", "enum": list(commands)}),)
        else:
            self.capabilities = (Capability("power", "Set switch power explicitly (not toggle).", {"type": "boolean"}),)
        self.kind, self.settings = kind, dict(settings)
        self.url = config.get(settings["url_env"], "")
        self.token = config.get(settings["token_env"], "")
        self.snapshot = {"status": "not_sent", "physically_verified": False}
        self.lock = threading.Lock()

    def ready(self):
        try:
            url = urllib.parse.urlsplit(self.url)
            return bool(self.token.strip() and "\n" not in self.token and "\r" not in self.token and
                        url.scheme in ("http", "https") and url.hostname and not url.username and
                        not url.password and not url.query and not url.fragment and url.path in ("", "/"))
        except (ValueError, AttributeError):
            return False

    def _request(self, path, data=None):
        if not self.ready():
            raise DeviceError("HA 연결 환경변수를 설정하세요.")
        request = urllib.request.Request(self.url.rstrip("/") + path,
            data=None if data is None else json.dumps(data).encode(),
            headers={"Authorization": "Bearer " + self.token, "Content-Type": "application/json"})
        try:
            with urllib.request.build_opener(NoRedirect).open(request, timeout=5) as response:
                raw = response.read(65537)
            if len(raw) > 65536:
                raise ValueError()
            return json.loads(raw)
        except (OSError, ValueError, urllib.error.URLError):
            raise DeviceError("HA 요청 실패. 실제 기기 상태는 미확인입니다.") from None

    def apply(self, plan, cancel):
        check_cancel(cancel)
        if self.kind == "ha_switch":
            # The registry has already validated every action before dispatch.
            path = "/api/services/switch/" + ("turn_on" if plan["power"] else "turn_off")
            data = {"entity_id": self.settings["entity_id"]}
        else:
            path = "/api/services/remote/send_command"
            data = {"entity_id": self.settings["entity_id"], "device": self.settings["device"],
                    "command": self.settings["commands"][plan["command"]], "num_repeats": 1}
        self._request(path, data)
        check_cancel(cancel)
        result = Result("sent_unverified", detail={"evidence": "ha_service_completed"})
        if self.kind == "ha_switch":
            # A failed read must not hide that the write already happened.
            try:
                state = self._request("/api/states/" + self.settings["entity_id"])
                if (isinstance(state, dict) and state.get("state") in ("on", "off") and
                        state.get("attributes", {}).get("assumed_state") is not True):
                    result.detail.update(reported_state=state["state"], source="home_assistant",
                                         last_updated=state.get("last_updated"))
                    if state["state"] == ("on" if plan["power"] else "off"):
                        result.status = "state_reported"
                    else:
                        result.detail["state_matches"] = False
            except (DeviceError, AttributeError):
                result.detail["state_read"] = "unavailable"
        check_cancel(cancel)
        with self.lock:
            self.snapshot = dict(status=result.status, physically_verified=False, **result.detail)
        return result

    def stop(self):
        # Cancelling cannot undo a transmitted IR code or restore another user's switch.
        return Result("stopped", detail={"scope": "pending_actions_only"})

    def status(self):
        with self.lock:
            return dict(self.snapshot)
