import base64
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import threading
import unittest
import urllib.error
import urllib.request
import wave
from unittest.mock import patch

import show_transcription as speech

spec = importlib.util.spec_from_file_location("local_asr_server", Path(__file__).resolve().parents[1] / "src/local_asr_server.py")
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


@contextlib.contextmanager
def running_server():
    server = helper.make_server("127.0.0.1", 0, "test-only", object())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:%s/transcribe" % server.server_port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


class TranscriptionTests(unittest.TestCase):
    def test_configured_requires_both_environment_values(self):
        for url, token, expected in (("", "", False), ("http://localhost", "", False), ("", "test-only", False), ("http://localhost", "test-only", True), (" ", " ", False)):
            with patch.dict("os.environ", {"KEVIN_ASR_URL": url, "KEVIN_ASR_TOKEN": token}):
                self.assertEqual(speech.configured(), expected)

    def test_proxy_sends_raw_bytes_and_bearer_to_real_http_helper(self):
        with running_server() as url, patch.object(helper, "recognize", return_value={"text": "  기차를 시작해 줘  ", "language": "ko"}) as recognize:
            # This test stubs inference only; the proxy and HTTP handling are real.
            with patch.dict("os.environ", {"KEVIN_ASR_URL": url, "KEVIN_ASR_TOKEN": "test-only"}):
                result = speech.transcribe(base64.b64encode(b"test webm").decode(), "audio/webm;codecs=opus")
            self.assertEqual(result, {"text": "기차를 시작해 줘", "language": "ko"})
            self.assertEqual(recognize.call_args.args[1], b"test webm")

    def test_bad_input_and_missing_configuration_do_not_open_network(self):
        valid = base64.b64encode(b"audio").decode()
        cases = [("", "audio/webm"), (None, "audio/wav"), ("not-base64", "audio/webm"),
                 ("a" * (speech.MAX_BASE64_CHARS + 1), "audio/webm"),
                 (base64.b64encode(b"x" * (speech.MAX_AUDIO_BYTES + 1)).decode(), "audio/wav"),
                 (valid, "text/html"), (valid, "audio/webm\r\nX: bad"), (valid, None)]
        for audio, mime in cases:
            with self.subTest(mime=mime), patch("urllib.request.urlopen") as call:
                with self.assertRaises(speech.TranscriptionError):
                    speech.transcribe(audio, mime, "http://localhost", "test-only")
                call.assert_not_called()
        with patch.dict("os.environ", {"KEVIN_ASR_URL": "", "KEVIN_ASR_TOKEN": ""}), patch("urllib.request.urlopen") as call:
            with self.assertRaises(speech.TranscriptionError):
                speech.transcribe(valid, "audio/wav")
            call.assert_not_called()

    def test_exact_2_mib_boundary_and_empty_transcript(self):
        audio = b"x" * speech.MAX_AUDIO_BYTES
        with patch("urllib.request.urlopen", return_value=io.BytesIO(b'{"text":"","language":"ko"}')) as call:
            self.assertEqual(speech.transcribe(base64.b64encode(audio).decode(), "audio/wav", "http://localhost/transcribe", "test-only"), {"text": "", "language": "ko"})
        self.assertEqual(call.call_args.args[0].data, audio)
        self.assertEqual(call.call_args.args[0].get_header("Authorization"), "Bearer test-only")

    def test_response_and_network_failures_are_visible_without_private_details(self):
        for raw in (b"not json", b"[]", b'{}', b'{"text":3,"language":"ko"}', b'{"text":"x","language":3}', b"x" * 64001):
            with patch("urllib.request.urlopen", return_value=io.BytesIO(raw)):
                with self.assertRaises(speech.TranscriptionError):
                    speech.transcribe("eA==", "audio/wav", "http://localhost/transcribe", "test-only")
        for error in (TimeoutError("private"), urllib.error.URLError("private"), urllib.error.HTTPError("private", 401, "private", {}, None)):
            with patch("urllib.request.urlopen", side_effect=error):
                with self.assertRaises(speech.TranscriptionError) as caught:
                    speech.transcribe("eA==", "audio/wav", "http://localhost/transcribe", "test-only")
                self.assertNotIn("private", str(caught.exception))

    def test_helper_auth_limits_mime_and_error_mapping(self):
        with running_server() as url, patch.object(helper, "recognize") as recognize:
            cases = [({}, b"x", 401),
                     ({"Authorization": "Bearer test-only", "Content-Type": "text/plain"}, b"x", 415),
                     ({"Authorization": "Bearer test-only", "Content-Type": "audio/wav"}, b"", 413),
                     ({"Authorization": "Bearer test-only", "Content-Type": "audio/wav", "Content-Length": str(helper.MAX_AUDIO_BYTES + 1)}, b"x", 413)]
            for headers, body, status in cases:
                with self.subTest(status=status), self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(urllib.request.Request(url, body, headers), timeout=3)
                self.assertEqual(caught.exception.code, status)
                caught.exception.close()
            recognize.assert_not_called()
            for error, status in ((helper.AudioTooLong(), 413), (ValueError("private transcript"), 422)):
                recognize.side_effect = error
                with self.assertRaises(speech.TranscriptionError) as caught:
                    speech.transcribe("eA==", "audio/wav", url, "test-only")
                self.assertNotIn("private", str(caught.exception))

    def test_helper_rejects_concurrent_inference(self):
        entered, release = threading.Event(), threading.Event()
        def slow_recognize(model, audio):
            entered.set()
            release.wait(timeout=5)
            return {"text": "", "language": "ko"}
        with running_server() as url, patch.object(helper, "recognize", side_effect=slow_recognize):
            result = []
            worker = threading.Thread(target=lambda: result.append(speech.transcribe("eA==", "audio/wav", url, "test-only")))
            worker.start()
            try:
                self.assertTrue(entered.wait(timeout=3))
                with self.assertRaisesRegex(speech.TranscriptionError, "사용 중"):
                    speech.transcribe("eA==", "audio/wav", url, "test-only")
            finally:
                release.set()
                worker.join(timeout=5)
            self.assertEqual(result, [{"text": "", "language": "ko"}])


@unittest.skipUnless(importlib.util.find_spec("av") and importlib.util.find_spec("numpy"), "optional ASR runtime required")
class DecoderTests(unittest.TestCase):
    @staticmethod
    def silence(seconds):
        output = io.BytesIO()
        with wave.open(output, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(b"\0\0" * int(seconds * 16000))
        return output.getvalue()

    def test_exact_duration_limit_decodes_and_longer_audio_rejected(self):
        self.assertEqual(helper.decode_bounded(self.silence(60)).size, 60 * 16000)
        with self.assertRaises(helper.AudioTooLong):
            helper.decode_bounded(self.silence(60.01))

    def test_empty_or_invalid_audio_fails(self):
        for audio in (self.silence(0), b"not audio"):
            with self.assertRaises(Exception):
                helper.decode_bounded(audio)


if __name__ == "__main__":
    unittest.main()
