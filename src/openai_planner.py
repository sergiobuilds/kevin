"""Optional OpenAI planner. It produces the existing plan, never device effects."""
import json
import math
import urllib.error
import urllib.request

from show_intent import IntentError, SCHEMA, SYSTEM, _validate


ENDPOINT = "https://api.openai.com/v1/responses"


class OpenAIPlanError(IntentError):
    """A redacted, user-actionable OpenAI configuration or response error."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        # An API credential must never follow a redirect to another endpoint.
        return None


def interpret(prompt, current_recipe, config):
    if not isinstance(prompt, str) or not 1 <= len(prompt.strip()) <= 2000:
        raise OpenAIPlanError("연출 요청은 1–2000자여야 합니다.")
    if current_recipe is not None and not isinstance(current_recipe, dict):
        raise OpenAIPlanError("현재 연출은 객체여야 합니다.")
    key = config.get("OPENAI_API_KEY", "")
    model = config.get("KEVIN_OPENAI_MODEL", "gpt-6-luna")
    if not isinstance(key, str) or not key.strip() or any(ord(c) < 33 or ord(c) > 126 for c in key):
        raise OpenAIPlanError("OpenAI 선택에는 OPENAI_API_KEY 환경변수가 필요합니다.")
    if not isinstance(model, str) or not model.strip() or len(model) > 128:
        raise OpenAIPlanError("OpenAI 모델 설정을 확인하세요.")
    try:
        timeout = float(config.get("KEVIN_OPENAI_TIMEOUT", "30"))
        if not math.isfinite(timeout) or not 0 < timeout <= 120:
            raise ValueError()
        content = json.dumps({"prompt": prompt.strip(), "current_recipe": current_recipe},
                             ensure_ascii=False, allow_nan=False)
        if len(content) > 16000:
            raise ValueError()
    except (ValueError, TypeError):
        raise OpenAIPlanError("OpenAI 제한 시간 또는 현재 연출 설정을 확인하세요.") from None
    payload = {
        "model": model, "stream": False, "store": False,
        "reasoning": {"effort": "none"}, "max_output_tokens": 1200,
        "input": [{"role": "system", "content": SYSTEM},
                  {"role": "user", "content": content}],
        "text": {"format": {"type": "json_schema", "name": "kevin_show_plan",
                            "strict": True, "schema": SCHEMA}},
    }
    request = urllib.request.Request(ENDPOINT, json.dumps(payload).encode("utf-8"),
        {"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    try:
        with urllib.request.build_opener(_NoRedirect).open(request, timeout=timeout) as response:
            raw = response.read(128001)
        if len(raw) > 128000:
            raise OpenAIPlanError("OpenAI 응답이 허용 크기를 초과했습니다.")
        result = json.loads(raw)
        if (not isinstance(result, dict) or result.get("status") != "completed" or
                result.get("error") is not None or result.get("incomplete_details") is not None):
            raise OpenAIPlanError("OpenAI 응답이 완료되지 않았습니다. 실행하지 않았습니다.")
        output = result.get("output")
        if not isinstance(output, list):
            raise OpenAIPlanError("OpenAI 응답에 계획이 없습니다.")
        messages = []
        for item in output:
            if not isinstance(item, dict) or item.get("type") not in ("message", "reasoning"):
                raise OpenAIPlanError("OpenAI 응답에 예상하지 않은 출력이 있습니다.")
            if item["type"] == "message":
                messages.append(item)
        if len(messages) != 1:
            raise OpenAIPlanError("OpenAI 응답에 하나의 계획이 필요합니다.")
        message = messages[0]
        parts = message.get("content")
        if message.get("role") != "assistant" or message.get("status") != "completed" or not isinstance(parts, list):
            raise OpenAIPlanError("OpenAI 계획 메시지가 완료되지 않았습니다.")
        if any(isinstance(part, dict) and part.get("type") == "refusal" for part in parts):
            raise OpenAIPlanError("OpenAI가 요청을 거절했습니다. 실행하지 않았습니다.")
        if (len(parts) != 1 or not isinstance(parts[0], dict) or
                parts[0].get("type") != "output_text" or not isinstance(parts[0].get("text"), str)):
            raise OpenAIPlanError("OpenAI 응답에 구조화된 계획이 없습니다.")
        # Reuse local contract validation, but do not run Ollama-specific keyword
        # grounding: it could otherwise overturn an unsupported request or negation.
        try:
            return _validate(json.loads(parts[0]["text"]))
        except IntentError:
            raise OpenAIPlanError("OpenAI 계획이 허용된 연출 계약과 다릅니다. 실행하지 않았습니다.") from None
    except urllib.error.HTTPError as error:
        code = error.code
        error.close()
        raise OpenAIPlanError(f"OpenAI 요청 실패 (HTTP {code}). API 키·권한·할당량을 확인하세요.") from None
    except (TimeoutError, urllib.error.URLError, OSError):
        raise OpenAIPlanError("OpenAI 연결 실패 또는 시간 초과입니다. 실행하지 않았습니다.") from None
    except (ValueError, TypeError, UnicodeError) as error:
        if isinstance(error, IntentError):
            raise
        raise OpenAIPlanError("OpenAI 응답이 올바른 연출 계획이 아닙니다.") from None
