"""Roadmap track 02: direct local WLED JSON API with explicit segment IDs."""
import copy
import json
import re
import urllib.parse
import urllib.request

from devices.base import Capability, Result
from devices.home_assistant import NoRedirect
from devices.registry import DeviceError
from show_transport import check_cancel


class WLED:
    slug = "light"

    def __init__(self, settings, config):
        if (set(settings) != {"url_env", "segment_ids"} or
                not isinstance(settings["url_env"], str) or
                not re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", settings["url_env"]) or
                not isinstance(settings["segment_ids"], list) or
                not 1 <= len(settings["segment_ids"]) <= 16 or
                any(type(s) is not int or not 0 <= s <= 31 for s in settings["segment_ids"]) or
                len(set(settings["segment_ids"])) != len(settings["segment_ids"])):
            raise DeviceError("WLED 주소 환경변수와 허용 구역 ID를 확인하세요.")
        self.url = config.get(settings["url_env"], "")
        self.capabilities = (
            Capability("power", "Set the whole light power", {"type": "boolean"}),
            Capability("brightness", "Whole light brightness", {"type": "integer", "minimum": 0, "maximum": 255}),
            Capability("segments", "Edit only the registered zones. Other zones remain unchanged.", {
                "type": "array", "minItems": 1, "maxItems": len(settings["segment_ids"]),
                "items": {"type": "object", "properties": {
                    "id": {"type": "integer", "enum": settings["segment_ids"]},
                    "power": {"type": "boolean"},
                    "brightness": {"type": "integer", "minimum": 0, "maximum": 255},
                    "color": {"type": "array", "minItems": 3, "maxItems": 3,
                              "items": {"type": "integer", "minimum": 0, "maximum": 255}}},
                          "required": ["id"], "additionalProperties": False}}))
        self.snapshot = {"status": "not_sent", "physically_verified": False}

    def ready(self):
        try:
            url = urllib.parse.urlsplit(self.url)
            return bool(url.scheme in ("http", "https") and url.hostname and not url.username and
                        not url.password and not url.query and not url.fragment and url.path in ("", "/"))
        except (ValueError, AttributeError):
            return False

    def _request(self, data=None):
        if not self.ready():
            raise DeviceError("WLED 주소를 설정하세요.")
        request = urllib.request.Request(self.url.rstrip("/") + "/json/state",
            data=None if data is None else json.dumps(data).encode(),
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.build_opener(NoRedirect).open(request, timeout=5) as response:
                raw = response.read(65537)
            if len(raw) > 65536:
                raise ValueError()
            return json.loads(raw)
        except (OSError, ValueError):
            raise DeviceError("WLED 요청 실패. 실제 기기 상태는 미확인입니다.") from None

    def apply(self, plan, cancel):
        check_cancel(cancel)
        self.validate(plan)
        data = {}
        for key, wire in (("power", "on"), ("brightness", "bri")):
            if key in plan:
                data[wire] = plan[key]
        if "segments" in plan:
            seen, segments = set(), []
            for item in plan["segments"]:
                if item["id"] in seen or len(item) == 1:
                    raise DeviceError("중복되었거나 변경값 없는 WLED 구역입니다.")
                seen.add(item["id"])
                segment = {"id": item["id"]}
                for key, wire in (("power", "on"), ("brightness", "bri")):
                    if key in item:
                        segment[wire] = item[key]
                if "color" in item:
                    segment["col"] = [item["color"]]
                segments.append(segment)
            data["seg"] = segments
        self._request(data)
        check_cancel(cancel)
        result = Result("sent_unverified", detail={"evidence": "wled_request_completed"})
        try:
            state = self._request()
            if (isinstance(state, dict) and type(state.get("on")) is bool and
                    type(state.get("bri")) is int and isinstance(state.get("seg", []), list)):
                public = {k: state[k] for k in ("on", "bri", "seg") if k in state}
                # Do not expose arbitrary WLED metadata or connection identifiers.
                public["seg"] = [{k: s[k] for k in ("id", "on", "bri", "col") if k in s}
                                 for s in state.get("seg", []) if isinstance(s, dict)]
                result.detail.update(reported_state=public, source="wled")
                result.status = "state_reported"
        except DeviceError:
            result.detail["state_read"] = "unavailable"
        check_cancel(cancel)
        self.snapshot = dict(status=result.status, physically_verified=False, **result.detail)
        return result

    def validate(self, plan):
        seen = set()
        for segment in plan.get("segments", []):
            if segment["id"] in seen or len(segment) == 1:
                raise DeviceError("중복되었거나 변경값 없는 WLED 구역입니다.")
            seen.add(segment["id"])

    def stop(self):
        return Result("stopped", detail={"scope": "pending_actions_only"})

    def status(self):
        return copy.deepcopy(self.snapshot)
