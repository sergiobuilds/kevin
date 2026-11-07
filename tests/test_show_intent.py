import copy
import io
import json
import unittest
import urllib.error
from unittest.mock import patch

import show_intent


UPDATE = {"mode": "update", "preset": "keep", "speed": None, "snow": "heavy", "sky": "keep", "music": "keep", "volume": None, "explanation": "눈을 더 많이 내리도록 변경합니다."}


def response(spec=UPDATE, **extra):
    result = {"done": True, "message": {"content": json.dumps(spec)}, **extra}
    return io.BytesIO(json.dumps(result).encode())


class IntentTests(unittest.TestCase):
    def test_real_protocol_and_partial_update_preserve_current(self):
        recipe = {"preset": "santa", "speed": 1.2, "sky": "#123456", "snow": "normal", "music": "play", "volume": 30}
        original = copy.deepcopy(recipe)
        with patch("urllib.request.urlopen", return_value=response()) as call:
            spec = show_intent.interpret("눈만 더 많이 내려 줘", recipe, "http://localhost:11434", "test-model")
        self.assertEqual(spec, UPDATE)
        self.assertEqual(recipe, original)
        request = call.call_args.args[0]
        self.assertEqual(request.full_url, "http://localhost:11434/api/chat")
        body = json.loads(request.data)
        self.assertEqual(body["format"], show_intent.SCHEMA)
        self.assertFalse(body["think"])
        self.assertFalse(body["stream"])
        self.assertEqual(body["options"]["temperature"], 0)
        self.assertEqual(json.loads(body["messages"][1]["content"])["current_recipe"], recipe)

    def test_environment_and_full_endpoint(self):
        with patch.dict("os.environ", {"TERMINATOR_OLLAMA_URL": "http://localhost:7/api/chat", "TERMINATOR_OLLAMA_MODEL": "chosen", "TERMINATOR_OLLAMA_TIMEOUT": "12"}), patch("urllib.request.urlopen", return_value=response()) as call:
            show_intent.interpret("눈", {})
        self.assertEqual(call.call_args.args[0].full_url, "http://localhost:7/api/chat")
        self.assertEqual(json.loads(call.call_args.args[0].data)["model"], "chosen")
        self.assertEqual(call.call_args.kwargs["timeout"], 12)

    def test_all_external_schema_fields_are_validated(self):
        invalid = [
            {"mode": "execute"}, {"mode": []}, {"preset": "unknown"},
            {"snow": "blizzard"}, {"music": True}, {"sky": "red"},
            {"sky": "#123456\n"}, {"speed": True}, {"speed": 0.49}, {"speed": 2.01},
            {"speed": "1"}, {"speed": float("nan")}, {"speed": float("inf")},
            {"volume": -1}, {"volume": 101}, {"volume": False},
            {"explanation": ""}, {"explanation": " "}, {"explanation": "x" * 1001},
            {"explanation": []}, {"extra": True}, {"mode": "unsupported"},
            {"mode": "stop", "music": "stop"},
        ]
        for changes in invalid:
            with self.subTest(changes=changes), patch("urllib.request.urlopen", return_value=response({**UPDATE, **changes})):
                with self.assertRaises(show_intent.IntentError):
                    show_intent.interpret("요청", {})
        for invalid_spec in ([], None, {k: v for k, v in UPDATE.items() if k != "speed"}):
            with self.subTest(spec=invalid_spec), patch("urllib.request.urlopen", return_value=response(invalid_spec)):
                with self.assertRaises(show_intent.IntentError):
                    show_intent.interpret("요청", {})

    def test_start_stop_unsupported_and_color(self):
        for mode, changes in (
            ("start", {"preset": "christmas_train", "sky": "#Aa0033"}),
            ("stop", {"snow": "keep", "music": "stop"}),
            ("unsupported", {"snow": "keep"}),
        ):
            spec = {**UPDATE, "mode": mode, **changes}
            with patch("urllib.request.urlopen", return_value=response(spec)):
                result = show_intent.interpret("요청", {})
                self.assertEqual(result["mode"], mode)
                self.assertEqual(result["sky"], spec["sky"].lower())

    def test_invalid_input_never_calls_model(self):
        for prompt, recipe in (("", {}), ("x" * 2001, {}), (None, {}), ("hi", []), ("hi", {"speed": float("nan")})):
            with patch("urllib.request.urlopen") as call:
                with self.assertRaises(show_intent.IntentError):
                    show_intent.interpret(prompt, recipe)
                call.assert_not_called()

    def test_errors_never_become_a_keyword_fallback(self):
        errors = [TimeoutError("private"), urllib.error.URLError("private"), urllib.error.HTTPError("private", 500, "private", {}, io.BytesIO(b"private"))]
        for error in errors:
            with patch("urllib.request.urlopen", side_effect=error):
                with self.assertRaises(show_intent.IntentError) as caught:
                    show_intent.interpret("산타 시작", {})
                self.assertNotIn("private", str(caught.exception))
        for raw in (b"not json", b"[]", b"x" * 128001, json.dumps({"done": True, "message": {"content": "not json"}}).encode()):
            with patch("urllib.request.urlopen", return_value=io.BytesIO(raw)):
                with self.assertRaises(show_intent.IntentError):
                    show_intent.interpret("산타 시작", {})
        for extra in ({"done": False}, {"done_reason": "length"}, {"message": {}}):
            with patch("urllib.request.urlopen", return_value=response(**extra)):
                with self.assertRaises(show_intent.IntentError):
                    show_intent.interpret("산타 시작", {})

    def test_retry_only_unsupported_think_option(self):
        error = urllib.error.HTTPError("private", 400, "bad", {}, io.BytesIO(b'{"error":"model does not support thinking"}'))
        with patch("urllib.request.urlopen", side_effect=[error, response()]) as call:
            self.assertEqual(show_intent.interpret("눈", {})["snow"], "heavy")
        self.assertNotIn("think", json.loads(call.call_args.args[0].data))
        self.assertEqual(call.call_count, 2)


if __name__ == "__main__":
    unittest.main()
