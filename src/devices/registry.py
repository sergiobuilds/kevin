"""Explicit local profiles and the planner's address-free capability contract."""
from __future__ import annotations

import copy
from dataclasses import dataclass
import json
from pathlib import Path
import re

from devices.base import DeviceAdapter


class DeviceError(ValueError):
    """Public errors never include connection configuration or provider payloads."""


@dataclass
class Device:
    id: str
    name: str
    aliases: list[str]
    adapter: DeviceAdapter


def check(value, schema):
    """Validate the small schema vocabulary used by shipped adapter capabilities."""
    kind = schema.get("type")
    valid = {"boolean": type(value) is bool, "number": type(value) in (int, float),
             "integer": type(value) is int, "string": isinstance(value, str),
             "object": isinstance(value, dict), "array": isinstance(value, list)}.get(kind, False)
    if not valid:
        raise DeviceError("기기 동작 값의 형식이 올바르지 않습니다.")
    if "enum" in schema and value not in schema["enum"]:
        raise DeviceError("등록하지 않은 기기 동작입니다.")
    if kind in ("number", "integer"):
        import math
        if not math.isfinite(value) or not schema.get("minimum", -float("inf")) <= value <= schema.get("maximum", float("inf")):
            raise DeviceError("기기 동작 값이 허용 범위를 벗어났습니다.")
    if kind == "string":
        if not schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", 100):
            raise DeviceError("기기 동작 문자열 길이를 확인하세요.")
        if "pattern" in schema and not re.fullmatch(schema["pattern"], value):
            raise DeviceError("기기 동작 문자열 형식을 확인하세요.")
    if kind == "object":
        properties = schema.get("properties", {})
        if set(value) - set(properties) or set(schema.get("required", [])) - set(value):
            raise DeviceError("기기 동작 객체 필드를 확인하세요.")
        for name, item in value.items():
            check(item, properties[name])
    if kind == "array":
        if not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", 32):
            raise DeviceError("기기 동작 목록 길이를 확인하세요.")
        for item in value:
            check(item, schema["items"])


def clean_nulls(value):
    if isinstance(value, dict):
        return {k: clean_nulls(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [clean_nulls(v) for v in value]
    return value


def strict_schema(schema, optional=False):
    """Portable strict provider schema; local validation remains authoritative."""
    field = copy.deepcopy(schema)
    if field["type"] == "object":
        originally_required = set(field.get("required", []))
        field["properties"] = {name: strict_schema(child, name not in originally_required)
                               for name, child in field.get("properties", {}).items()}
        field.update(required=list(field["properties"]), additionalProperties=False)
    if field["type"] == "array":
        field["items"] = strict_schema(field["items"])
    if optional:
        field["type"] = [field["type"], "null"]
        if "enum" in field:
            field["enum"].append(None)
    return field


class Registry:
    def __init__(self, devices):
        if not 1 <= len(devices) <= 32:
            raise DeviceError("프로필에는 1–32개 기기를 등록하세요.")
        self.devices = {}
        aliases = set()
        for device in devices:
            if not re.fullmatch(r"[a-z][a-z0-9_-]{0,47}", device.id):
                raise DeviceError("기기 ID는 짧은 영문 식별자여야 합니다.")
            if device.id in self.devices or not device.adapter.capabilities:
                raise DeviceError("중복 기기 또는 기능 없는 기기입니다.")
            for alias in [device.id, device.name, *device.aliases]:
                if not isinstance(alias, str) or not 1 <= len(alias.strip()) <= 80 or any(ord(c) < 32 for c in alias):
                    raise DeviceError("기기 이름이나 별칭을 확인하세요.")
            # A name may equal its own id; aliases must not overlap across devices.
            names = {a.strip().casefold() for a in [device.id, device.name, *device.aliases]}
            if names & aliases:
                raise DeviceError("기기 이름이나 별칭이 서로 겹칩니다.")
            aliases.update(names)
            self.devices[device.id] = device

    def catalog(self):
        return [dict(id=d.id, name=d.name, aliases=list(d.aliases),
                     capabilities=[dict(name=c.name, description=c.description, schema=copy.deepcopy(c.schema))
                                   for c in d.adapter.capabilities])
                for d in self.devices.values()]

    def schema(self):
        variants = []
        for device in self.devices.values():
            properties = {}
            for capability in device.adapter.capabilities:
                properties[capability.name] = strict_schema(capability.schema, optional=True)
            changes = dict(type="object", properties=properties, required=list(properties),
                           additionalProperties=False)
            variants.append(dict(type="object", properties={
                "device": dict(type="string", enum=[device.id]), "changes": changes},
                required=["device", "changes"], additionalProperties=False))
        return dict(type="object", properties={
            "mode": dict(type="string", enum=["execute", "unsupported", "clarify", "stop"]),
            "actions": dict(type="array", items={"anyOf": variants}, maxItems=8),
            "explanation": dict(type="string", minLength=1, maxLength=1000)},
            required=["mode", "actions", "explanation"], additionalProperties=False)

    def validate(self, plan):
        if not isinstance(plan, dict) or set(plan) != {"mode", "actions", "explanation"}:
            raise DeviceError("AI 응답이 기기 계획 형식과 다릅니다.")
        if plan["mode"] not in ("execute", "unsupported", "clarify", "stop"):
            raise DeviceError("AI 응답의 실행 모드가 올바르지 않습니다.")
        if not isinstance(plan["explanation"], str) or not 1 <= len(plan["explanation"].strip()) <= 1000:
            raise DeviceError("AI 계획 설명을 확인하세요.")
        actions = plan["actions"]
        if not isinstance(actions, list) or len(actions) > 8:
            raise DeviceError("한 요청에는 최대 8개 기기 동작을 지정하세요.")
        if (plan["mode"] == "execute") != bool(actions):
            raise DeviceError("실행 또는 거절 계획의 동작 목록이 올바르지 않습니다.")
        seen, normalized = set(), []
        for action in actions:
            if not isinstance(action, dict) or set(action) != {"device", "changes"}:
                raise DeviceError("기기 동작 형식이 올바르지 않습니다.")
            target, changes = action["device"], action["changes"]
            if not isinstance(target, str) or target not in self.devices or target in seen:
                raise DeviceError("등록하지 않았거나 중복된 기기입니다.")
            if not isinstance(changes, dict):
                raise DeviceError("기기 변경값은 객체여야 합니다.")
            supported = {c.name: c.schema for c in self.devices[target].adapter.capabilities}
            if set(changes) - set(supported):
                raise DeviceError("이 기기에서 지원하지 않는 동작입니다.")
            clean = {}
            for name, value in changes.items():
                if value is not None:
                    value = clean_nulls(value)
                    check(value, supported[name])
                    clean[name] = value
            if not clean:
                raise DeviceError("실행할 기기 변경값이 없습니다.")
            validate = getattr(self.devices[target].adapter, "validate", None)
            if validate is not None:
                validate(clean)  # Pure adapter constraints, before ANY device dispatch.
            normalized.append(dict(device=target, changes=clean))
            seen.add(target)
        return dict(mode=plan["mode"], actions=normalized, explanation=plan["explanation"].strip())


def load_profile(path, config):
    # Only built-in adapter names are accepted, never Python import paths.
    from devices.home_assistant import HomeAssistant
    from devices.show import ChristmasShow
    from devices.wled import WLED
    if path is None:
        return Registry([Device("christmas", "크리스마스 전광판과 라디오",
                                ["전광판", "라디오", "크리스마스"], ChristmasShow(config))])
    try:
        raw = Path(path).read_bytes()
        if len(raw) > 65536:
            raise ValueError()
        profile = json.loads(raw)
        if not isinstance(profile, dict) or set(profile) != {"version", "devices"} or type(profile["version"]) is not int or profile["version"] != 1:
            raise ValueError()
        if not isinstance(profile["devices"], list):
            raise ValueError()
        devices = []
        for spec in profile["devices"]:
            if not isinstance(spec, dict) or set(spec) != {"id", "name", "aliases", "adapter", "config"}:
                raise ValueError()
            if not isinstance(spec["aliases"], list) or len(spec["aliases"]) > 16 or not isinstance(spec["config"], dict):
                raise ValueError()
            if spec["adapter"] == "christmas_show" and not spec["config"]:
                adapter = ChristmasShow(config)
            elif spec["adapter"] in ("ha_switch", "ha_remote"):
                adapter = HomeAssistant(spec["adapter"], spec["config"], config)
            elif spec["adapter"] == "wled":
                adapter = WLED(spec["config"], config)
            else:
                raise ValueError()
            devices.append(Device(spec["id"], spec["name"], spec["aliases"], adapter))
        return Registry(devices)
    except (OSError, ValueError, TypeError, KeyError):
        raise DeviceError("기기 프로필 형식과 설정을 확인하세요.") from None
