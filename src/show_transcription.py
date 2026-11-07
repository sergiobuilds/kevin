"""Bounded in-memory proxy to an explicitly configured local speech service."""
import base64
import binascii
import json
import os
import urllib.error
import urllib.parse
import urllib.request

MAX_AUDIO_BYTES = 2 * 1024 * 1024
MAX_BASE64_CHARS = 4 * ((MAX_AUDIO_BYTES + 2) // 3)
ALLOWED_MIMES = {"audio/webm", "audio/wav", "audio/x-wav", "audio/ogg", "audio/mp4", "audio/mpeg", "audio/flac", "audio/x-flac"}


class TranscriptionError(ValueError):
    """Safe user-visible speech configuration, input, or service failure."""


def configured():
    return bool(os.getenv("TERMINATOR_ASR_URL", "").strip() and os.getenv("TERMINATOR_ASR_TOKEN", "").strip())


def transcribe(audio_b64, mime, endpoint=None, token=None):
    """Decode at most 2 MiB, send raw audio once, return a real transcript."""
    if not isinstance(audio_b64, str) or not audio_b64 or len(audio_b64) > MAX_BASE64_CHARS:
        raise TranscriptionError("음성 데이터가 비어 있거나 2MB 제한을 초과했습니다.")
    try:
        audio = base64.b64decode(audio_b64, validate=True)
    except (ValueError, binascii.Error):
        raise TranscriptionError("음성 데이터가 올바른 base64가 아닙니다.") from None
    if not audio or len(audio) > MAX_AUDIO_BYTES:
        raise TranscriptionError("음성 데이터가 비어 있거나 2MB 제한을 초과했습니다.")
    if not isinstance(mime, str) or len(mime) > 200 or "\r" in mime or "\n" in mime or mime.split(";", 1)[0].strip().lower() not in ALLOWED_MIMES:
        raise TranscriptionError("지원하지 않는 음성 형식입니다.")
    url = endpoint if endpoint is not None else os.getenv("TERMINATOR_ASR_URL", "").strip()
    secret = token if token is not None else os.getenv("TERMINATOR_ASR_TOKEN", "").strip()
    if not isinstance(url, str) or not isinstance(secret, str) or not url or not secret.strip() or any(c in secret for c in "\r\n"):
        raise TranscriptionError("음성 서버 주소와 인증 토큰을 설정하세요.")
    try:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError()
        request = urllib.request.Request(url, audio, {"Content-Type": mime, "Authorization": "Bearer " + secret})
        with urllib.request.urlopen(request, timeout=90) as response:
            raw = response.read(64001)
        if len(raw) > 64000:
            raise TranscriptionError("음성 서버 응답이 너무 큽니다.")
        result = json.loads(raw)
        if not isinstance(result, dict) or not isinstance(result.get("text"), str) or not isinstance(result.get("language"), str):
            raise TranscriptionError("음성 서버 응답 형식이 올바르지 않습니다.")
        if len(result["text"]) > 8000 or len(result["language"]) > 32:
            raise TranscriptionError("음성 서버 응답이 너무 깁니다.")
        return {"text": result["text"].strip(), "language": result["language"]}
    except urllib.error.HTTPError as exc:
        messages = {401: "음성 서버 인증에 실패했습니다.", 413: "음성 크기 또는 녹음 길이 제한을 초과했습니다.", 415: "지원하지 않는 음성 형식입니다.", 422: "음성을 해독하지 못했습니다.", 503: "음성 서버가 사용 중이거나 준비되지 않았습니다."}
        exc.close()
        raise TranscriptionError(messages.get(exc.code, f"음성 서버 요청 실패 (HTTP {exc.code}).")) from None
    except (TimeoutError, urllib.error.URLError, OSError):
        raise TranscriptionError("음성 서버 연결 실패 또는 시간 초과입니다. 텍스트 입력을 사용할 수 있습니다.") from None
    except (ValueError, TypeError, UnicodeError) as exc:
        if isinstance(exc, TranscriptionError):
            raise
        raise TranscriptionError("음성 서버 주소 또는 응답 형식이 올바르지 않습니다.") from None
