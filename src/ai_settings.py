"""One private command-AI configuration for web, terminal and MCP."""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import secrets
import tempfile
import threading
import time
import urllib.parse


class SettingsError(ValueError):
    """Only safe, actionable messages may cross the local setup boundary."""


def config_path(env):
    if env.get("KEVIN_AI_CONFIG"):
        return Path(env["KEVIN_AI_CONFIG"]).expanduser()
    if os.name == "nt":
        return Path(env.get("APPDATA", str(Path.home()))) / "KEVIN" / "ai.json"
    if __import__("sys").platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "KEVIN" / "ai.json"
    return Path(env.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "kevin" / "ai.json"


def _fingerprint(value):
    if value.get("provider") == "ollama":
        value = dict(value, api_key="")
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def validate(spec, require_key=True):
    if not isinstance(spec, dict) or set(spec) != {"provider", "model", "ollama_url", "api_key"}:
        raise SettingsError("AI 설정 형식을 확인하세요.")
    if spec["provider"] not in ("ollama", "openai"):
        raise SettingsError("로컬 또는 OpenAI를 선택하세요.")
    if not isinstance(spec["model"], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./:+-]{0,119}", spec["model"]):
        raise SettingsError("사용할 모델 이름을 확인하세요.")
    key = spec["api_key"]
    if not isinstance(key, str) or len(key) > 1024 or any(ord(c) < 33 or ord(c) > 126 for c in key):
        raise SettingsError("API 키 형식을 확인하세요.")
    if require_key and spec["provider"] == "openai" and not key:
        raise SettingsError("OpenAI API 키를 입력하세요.")
    try:
        url = urllib.parse.urlsplit(spec["ollama_url"])
        _ = url.port
        if (url.scheme not in ("http", "https") or url.hostname not in ("127.0.0.1", "localhost", "::1") or
                url.username or url.password or url.query or url.fragment or url.path not in ("", "/")):
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise SettingsError("로컬 Ollama 주소는 localhost 주소여야 합니다.") from None
    return dict(spec)


class AISettings:
    def __init__(self, env=None, probe=None):
        self.env = dict(os.environ if env is None else env)
        self.path = config_path(self.env)
        self.lock = threading.RLock()
        self.csrf = secrets.token_urlsafe(32)
        self.probe = probe or probe_connection
        self.tickets = {}
        self.failed = set()

    def _read(self):
        if not self.path.exists():
            return {}
        try:
            if self.path.is_symlink():
                raise ValueError()
            with self.path.open("rb") as f:
                raw = f.read(16385)
            if len(raw) > 16384:
                raise ValueError()
            data = json.loads(raw)
            if (set(data) != {"version", "revision", "settings", "verified", "checked_at"} or
                    data["version"] != 1 or not isinstance(data["revision"], str)):
                raise ValueError()
            validate(data["settings"], require_key=False)
            return data
        except (OSError, ValueError, TypeError, KeyError):
            raise SettingsError("저장된 AI 설정을 읽지 못했습니다. 로컬 설정 파일을 확인하세요.") from None

    def effective(self):
        data = self._read()
        saved = data.get("settings", {})
        provider = self.env.get("KEVIN_PLANNER", saved.get("provider", "ollama"))
        model_var = "KEVIN_OPENAI_MODEL" if provider == "openai" else "KEVIN_OLLAMA_MODEL"
        default_model = "gpt-6-luna" if provider == "openai" else "qwen2.5:3b"
        model = self.env.get(model_var, saved.get("model") if saved.get("provider") == provider else default_model)
        spec = dict(provider=provider, model=model,
                    ollama_url=self.env.get("KEVIN_OLLAMA_URL", saved.get("ollama_url", "http://127.0.0.1:11434")),
                    api_key=self.env.get("OPENAI_API_KEY", saved.get("api_key", "")))
        return spec, data

    def public(self):
        spec, data = self.effective()
        fingerprint = _fingerprint(spec)
        configured = bool(data or self.env.get("KEVIN_PLANNER"))
        verified = bool(data.get("verified") == fingerprint and fingerprint not in self.failed)
        managed = [key for key, variable in (
            ("provider", "KEVIN_PLANNER"), ("api_key", "OPENAI_API_KEY"),
            ("model", "KEVIN_OPENAI_MODEL" if spec["provider"] == "openai" else "KEVIN_OLLAMA_MODEL"),
            ("ollama_url", "KEVIN_OLLAMA_URL")) if self.env.get(variable)]
        return dict(provider=spec["provider"], model=spec["model"], ollama_url=spec["ollama_url"],
                    has_key=bool(spec["api_key"]), configured=configured, verified=verified,
                    checked_at=data.get("checked_at") if verified else None,
                    revision=data.get("revision", ""), managed=managed,
                    defaults={"openai": self.env.get("KEVIN_OPENAI_MODEL", "gpt-6-luna"),
                              "ollama": self.env.get("KEVIN_OLLAMA_MODEL", "qwen2.5:3b")})

    def candidate(self, draft):
        allowed = {"provider", "model", "ollama_url", "api_key", "revision"}
        if not isinstance(draft, dict) or set(draft) - allowed:
            raise SettingsError("AI 설정 필드를 확인하세요.")
        current, data = self.effective()
        if draft.get("revision", "") != data.get("revision", ""):
            raise SettingsError("다른 곳에서 설정이 바뀌었습니다. 다시 열어 주세요.")
        provider = draft.get("provider", current["provider"])
        spec = dict(provider=provider,
                    model=draft.get("model", "gpt-6-luna" if provider == "openai" else "qwen2.5:3b"),
                    ollama_url=draft.get("ollama_url", current["ollama_url"]),
                    api_key=draft.get("api_key") or current["api_key"])
        for field, variable in (("provider", "KEVIN_PLANNER"), ("api_key", "OPENAI_API_KEY"),
                ("ollama_url", "KEVIN_OLLAMA_URL"),
                ("model", "KEVIN_OPENAI_MODEL" if provider == "openai" else "KEVIN_OLLAMA_MODEL")):
            if self.env.get(variable) and spec[field] != self.env[variable]:
                raise SettingsError("환경변수로 지정된 항목입니다. 실행 환경을 변경한 뒤 다시 설정하세요.")
        return validate(spec), data.get("revision", "")

    def test(self, draft):
        spec, revision = self.candidate(draft)
        try:
            self.probe(spec)
        except Exception:
            raise SettingsError("연결을 확인하지 못했어요. 모델 준비 상태·API 키·권한을 확인해 주세요.") from None
        token = secrets.token_urlsafe(32)
        with self.lock:
            now = time.time()
            self.tickets = {k: v for k, v in self.tickets.items() if v["expires"] > now}
            if len(self.tickets) >= 32:
                self.tickets.clear()
            self.tickets[token] = dict(fingerprint=_fingerprint(spec), revision=revision, expires=now + 300)
        return dict(test_id=token, model=spec["model"], provider=spec["provider"],
                    message="연결을 확인했어요. 저장하면 이 설정을 사용합니다.")

    def save(self, draft, test_id):
        spec, revision = self.candidate(draft)
        with self.lock:
            ticket = self.tickets.pop(test_id, None) if isinstance(test_id, str) else None
            if (not ticket or ticket["expires"] < time.time() or ticket["revision"] != revision or
                    not hmac.compare_digest(ticket["fingerprint"], _fingerprint(spec))):
                raise SettingsError("변경한 설정으로 연결을 먼저 확인해 주세요.")
            parent = self.path.parent
            parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            lock_path = parent / (self.path.name + ".lock")
            try:
                lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except OSError:
                raise SettingsError("다른 설정 저장이 진행 중입니다. 잠시 후 다시 확인해 주세요.") from None
            temporary = None
            try:
                os.close(lock_fd)
                if self._read().get("revision", "") != revision:
                    raise SettingsError("다른 곳에서 설정이 바뀌었습니다. 다시 열어 주세요.")
                stored = dict(spec)
                if self.env.get("OPENAI_API_KEY"):
                    stored["api_key"] = ""  # Never copy an environment credential into a file.
                elif spec["provider"] == "ollama":
                    stored["api_key"] = self._read().get("settings", {}).get("api_key", "")
                data = dict(version=1, revision=secrets.token_hex(16), settings=stored,
                            verified=_fingerprint(spec), checked_at=int(time.time()))
                fd, temporary = tempfile.mkstemp(prefix=".kevin-ai-", dir=parent)
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False)
                    f.flush()
                    os.fsync(f.fileno())
                os.chmod(temporary, 0o600)
                os.replace(temporary, self.path)
                self.failed.discard(_fingerprint(spec))
            except OSError:
                raise SettingsError("AI 설정을 저장하지 못했습니다. 로컬 폴더 권한을 확인하세요.") from None
            finally:
                if temporary and Path(temporary).exists():
                    Path(temporary).unlink()
                lock_path.unlink()
        return self.public()

    def mark_failed(self):
        self.failed.add(_fingerprint(self.effective()[0]))


def runtime_config(env=None):
    settings = AISettings(env)
    spec, _ = settings.effective()
    config = dict(settings.env)
    config.update(KEVIN_PLANNER=spec["provider"], KEVIN_OPENAI_MODEL=spec["model"],
                  KEVIN_OLLAMA_MODEL=spec["model"], KEVIN_OLLAMA_URL=spec["ollama_url"],
                  OPENAI_API_KEY=spec["api_key"])
    return config


def request_json(system, content, schema, config):
    if config.get("KEVIN_PLANNER", "ollama") == "openai":
        from openai_planner import request_json as remote
        result = remote(system, content, schema, config, max_tokens=4000)
    else:
        from show_intent import request_json as local
        result = local(system, content, schema, endpoint=config.get("KEVIN_OLLAMA_URL"),
                       model=config.get("KEVIN_OLLAMA_MODEL"), max_tokens=4000, no_redirect=True)
    _validate_result(result, schema)
    return result


def _validate_result(value, schema):
    """Validate the JSON Schema subset used by the command agent before dispatch."""
    types = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
    matches = {"object": isinstance(value, dict), "array": isinstance(value, list),
               "string": isinstance(value, str), "boolean": type(value) is bool,
               "integer": type(value) is int, "null": value is None,
               "number": type(value) in (int, float) and math.isfinite(value)}
    if not any(matches.get(kind, False) for kind in types) or ("enum" in schema and value not in schema["enum"]):
        raise SettingsError("AI 응답이 허용된 명령 형식과 다릅니다. 실행하지 않았습니다.")
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        if set(schema.get("required", [])) - set(value) or (schema.get("additionalProperties") is False and set(value) - set(properties)):
            raise SettingsError("AI 응답의 명령 필드가 올바르지 않습니다. 실행하지 않았습니다.")
        for key in value:
            if key in properties:
                _validate_result(value[key], properties[key])
    elif isinstance(value, list):
        for item in value:
            _validate_result(item, schema["items"])


def probe_connection(spec):
    # Fixed synthetic data only, never camera frames, user history or device commands.
    config = dict(KEVIN_PLANNER=spec["provider"], OPENAI_API_KEY=spec["api_key"],
                  KEVIN_OPENAI_MODEL=spec["model"], KEVIN_OPENAI_TIMEOUT="15",
                  KEVIN_OLLAMA_MODEL=spec["model"], KEVIN_OLLAMA_URL=spec["ollama_url"])
    schema = dict(type="object", properties={"ready": {"type": "boolean"}},
                  required=["ready"], additionalProperties=False)
    if spec["provider"] == "openai":
        from openai_planner import request_json as remote
        result = remote('Return {"ready":true} for a connection check.', "connection check", schema, config, max_tokens=64)
    else:
        from show_intent import request_json as local
        result = local('Return {"ready":true} for a connection check.', "connection check", schema,
                       endpoint=spec["ollama_url"], model=spec["model"], timeout=15, max_tokens=64, no_redirect=True)
    if result != {"ready": True}:
        raise SettingsError("연결 확인 응답이 올바르지 않습니다.")


def terminal_setup(settings=None):
    import getpass
    settings = settings or AISettings()
    public = settings.public()
    print("\n명령 해석 AI\n1. 이 컴퓨터에서 실행 (Ollama)\n2. 내 API 키로 연결 (OpenAI, 사용량별 과금)\n0. 나중에 설정")
    choice = input("선택: ").strip()
    if choice not in ("1", "2"):
        return False
    provider = "ollama" if choice == "1" else "openai"
    default = "qwen2.5:3b" if provider == "ollama" else "gpt-6-luna"
    model = input(f"모델 [{default}]: ").strip() or default
    key = getpass.getpass("OpenAI API 키 (등록된 키 유지: Enter): ") if provider == "openai" else ""
    draft = dict(provider=provider, model=model, api_key=key, ollama_url=public["ollama_url"], revision=public["revision"])
    try:
        result = settings.test(draft)
        if input(f"연결 확인: {model}. 저장하고 시작할까요? [y/N]: ").strip().lower() != "y":
            return False
        settings.save(draft, result["test_id"])
        print("AI 설정을 저장했습니다.")
        return True
    except SettingsError as error:
        print(str(error))
        return False
