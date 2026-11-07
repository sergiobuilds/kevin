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
        self.assertEqual(spec, {**UPDATE, "explanation": "좋아, 눈 펑펑 내리게 할게!"})
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
        with patch.dict("os.environ", {"KEVIN_OLLAMA_URL": "http://localhost:7/api/chat", "KEVIN_OLLAMA_MODEL": "chosen", "KEVIN_OLLAMA_TIMEOUT": "12"}), patch("urllib.request.urlopen", return_value=response()) as call:
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

    def test_explicit_words_override_model_drift(self):
        # A small model often picks the wrong scene, colour or stop scope; the
        # sentence's explicit words must win and unmentioned fields stay put.
        cases = (
            ("크리스마스 기차 보여줘, 하늘은 보라색", {"mode": "start", "preset": "christmas_train", "sky": "#f39c12"},
             {"mode": "start", "preset": "christmas_train", "sky": "#5b2a9e", "music": "keep"}),
            ("산타 날려줘, 눈 펑펑", {"preset": "christmas_train", "snow": "normal", "music": "stop"},
             {"preset": "santa", "snow": "heavy", "music": "keep", "sky": "keep"}),
            ("전부 멈춰", {"snow": "normal", "music": "stop"},
             {"mode": "stop", "preset": "keep", "snow": "keep", "music": "stop"}),
            ("피자 시켜줘", {"preset": "christmas_train", "music": "stop", "volume": 45},
             {"mode": "unsupported", "preset": "keep", "music": "keep", "volume": None}),
        )
        for prompt, model_out, expected in cases:
            spec = {**UPDATE, **model_out}
            with self.subTest(prompt=prompt), patch("urllib.request.urlopen", return_value=response(spec)):
                result = show_intent.interpret(prompt, {"speed": 1})
                for key, value in expected.items():
                    self.assertEqual(result[key], value, key)

    def test_invalid_input_never_calls_model(self):
        for prompt, recipe in (("", {}), ("x" * 2001, {}), (None, {}), ("hi", []), ("hi", {"speed": float("nan")})):
            with patch("urllib.request.urlopen") as call:
                with self.assertRaises(show_intent.IntentError):
                    show_intent.interpret(prompt, recipe)
                call.assert_not_called()

    def test_refusal_survives_supported_keywords_in_mixed_requests(self):
        refusal = {**UPDATE, 'mode': 'unsupported', 'snow': 'keep',
                   'explanation': 'TV는 지원하지 않습니다.'}
        for prompt in ('TV를 켜고 캐럴도 틀어줘', 'TV랑 산타를 보여줘', '지원 안 되는 것도 전부 멈춰'):
            with self.subTest(prompt=prompt), patch('urllib.request.urlopen', return_value=response(refusal)):
                self.assertEqual(show_intent.interpret(prompt, {'music': 'stop'}), refusal)

    def test_volume_only_never_becomes_play_even_when_model_drifted(self):
        for music in ('keep', 'play', 'stop'):
            model = {**UPDATE, 'snow': 'keep', 'volume': 30, 'music': music}
            with self.subTest(music=music), patch('urllib.request.urlopen', return_value=response(model)):
                result = show_intent.interpret('음악 볼륨만 30으로', {'music': 'stop', 'volume': 45})
            self.assertEqual(result['music'], 'keep')
            self.assertEqual(result['volume'], 30)
            self.assertNotIn('캐럴 틀', result['explanation'])

    def test_explicit_play_with_volume_still_plays(self):
        model = {**UPDATE, 'snow': 'keep', 'music': 'keep', 'volume': 30}
        with patch('urllib.request.urlopen', return_value=response(model)):
            result = show_intent.interpret('캐럴 틀어줘 볼륨 30으로', {'music': 'stop'})
        self.assertEqual((result['music'], result['volume']), ('play', 30))

    def test_other_device_on_verbs_never_start_music(self):
        cases = (
            ('라디오 끄고 산타 켜줘', 'stop', None, 'stop'),
            ('음악은 그대로 두고 산타 켜줘', 'keep', None, 'keep'),
            ('음악 켜지 말고 볼륨만 30으로', 'keep', 30, 'keep'),
            ('Stop the radio and turn Santa on', 'stop', None, 'stop'),
            ('Leave music alone and turn Santa on', 'keep', None, 'keep'),
            ("Don't turn music on, volume 30", 'keep', 30, 'keep'),
            ('산타 켜고 라디오 꺼줘', 'stop', None, 'stop'),
        )
        for prompt, music, volume, expected in cases:
            model = {**UPDATE, 'snow': 'keep', 'music': music, 'volume': volume}
            with self.subTest(prompt=prompt), patch('urllib.request.urlopen', return_value=response(model)):
                result = show_intent.interpret(prompt, {'music': 'stop', 'volume': 45})
            self.assertEqual(result['music'], expected)
            self.assertEqual(result['volume'], volume)
            self.assertNotIn('캐럴 틀', result['explanation'])

    def test_explicit_keep_and_negation_correct_music_model_drift(self):
        for prompt in ('음악은 그대로 두고 산타 켜줘', '음악 켜지 말고 볼륨만 30으로',
                       '음악 끄지 말고 산타 켜줘', '음악 안 켜고 볼륨만 30으로',
                       "Don't start the music, volume 30"):
            model = {**UPDATE, 'snow': 'keep', 'music': 'play', 'volume': 30}
            with self.subTest(prompt=prompt), patch('urllib.request.urlopen', return_value=response(model)):
                result = show_intent.interpret(prompt, {'music': 'stop', 'volume': 45})
            self.assertEqual(result['music'], 'keep')

    def test_ambiguous_music_words_preserve_validated_model_choice(self):
        for music in ('keep', 'play', 'stop'):
            model = {**UPDATE, 'snow': 'keep', 'music': music}
            with self.subTest(music=music), patch('urllib.request.urlopen', return_value=response(model)):
                result = show_intent.interpret('라디오도 부탁해 산타 켜줘', {'music': 'stop'})
            self.assertEqual(result['music'], music)

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
