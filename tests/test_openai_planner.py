import copy
import io
import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import Mock, patch

import openai_planner as luna
import show_control
import show_intent
from companion_cli import Backend


PLAN = dict(mode="update", preset="keep", speed=None, snow="heavy", sky="keep",
            music="keep", volume=None, explanation="눈을 많이 내리게 할게요.")
CONFIG = dict(OPENAI_API_KEY="qa-placeholder")


def response(plan=PLAN, **updates):
    result = dict(status="completed", output=[dict(type="message", role="assistant", status="completed",
        content=[dict(type="output_text", text=json.dumps(plan))])])
    result.update(updates)
    return io.BytesIO(json.dumps(result).encode())


class LunaTests(unittest.TestCase):
    def request(self, output=None, **config):
        opener = Mock()
        opener.open.return_value = output or response()
        with patch("openai_planner.urllib.request.build_opener", return_value=opener):
            result = luna.interpret("눈 많이", {"snow": "normal"}, dict(CONFIG, **config))
        return result, opener.open

    def test_responses_contract_exact_model_no_tools_no_ollama_parameters_or_config_leaks(self):
        result, call = self.request()
        self.assertEqual(result, PLAN)
        request = call.call_args.args[0]
        body = json.loads(request.data)
        self.assertEqual(request.full_url, "https://api.openai.com/v1/responses")
        self.assertEqual(request.get_header("Authorization"), "Bearer qa-placeholder")
        self.assertEqual(body["model"], "gpt-6-luna")
        self.assertFalse(body["store"])
        self.assertFalse(body["stream"])
        self.assertEqual(body["reasoning"], {"effort": "none"})
        self.assertTrue(body["text"]["format"]["strict"])
        self.assertEqual(body["text"]["format"]["schema"], show_intent.SCHEMA)
        self.assertEqual(json.loads(body["input"][1]["content"]), {"prompt": "눈 많이", "current_recipe": {"snow": "normal"}})
        for name in ("temperature", "think", "tools", "format", "options"):
            self.assertNotIn(name, body)
        self.assertNotIn("qa-placeholder", str(body))
        self.assertEqual(call.call_args.kwargs["timeout"], 30)

    def test_explicit_model_and_timeout_and_no_silent_model_substitution(self):
        _, call = self.request(KEVIN_OPENAI_MODEL="explicit-model", KEVIN_OPENAI_TIMEOUT="12")
        self.assertEqual(json.loads(call.call_args.args[0].data)["model"], "explicit-model")
        self.assertEqual(call.call_args.kwargs["timeout"], 12)
        self.assertEqual(call.call_count, 1)

    def test_key_prompt_recipe_and_timeouts_rejected_without_network(self):
        invalid = [
            ("x", {}, {"OPENAI_API_KEY": ""}), ("x", {}, {"OPENAI_API_KEY": "bad\r\nkey"}),
            ("", {}, {}), ("x" * 2001, {}, {}), ("x", [], {}),
            ("x", {"value": float("nan")}, {}),
            ("x", {"value": "x" * 16000}, {}),
            ("x", {}, {"KEVIN_OPENAI_TIMEOUT": "nan"}),
            ("x", {}, {"KEVIN_OPENAI_TIMEOUT": "0"}),
            ("x", {}, {"KEVIN_OPENAI_TIMEOUT": "121"}),
            ("x", {}, {"KEVIN_OPENAI_MODEL": ""}),
        ]
        for prompt, recipe, config in invalid:
            with self.subTest(config=config), patch("urllib.request.build_opener") as network:
                with self.assertRaises(show_intent.IntentError):
                    luna.interpret(prompt, recipe, dict(CONFIG, **config))
                network.assert_not_called()

    def test_incomplete_refusal_tools_multiple_messages_and_bad_schema_are_rejected(self):
        invalid = [
            response(status="incomplete", incomplete_details={"reason": "max_output_tokens"}),
            response(error={"message": "private-details"}),
            response(output=[dict(type="message", role="assistant", status="completed",
                                  content=[dict(type="refusal", refusal="private-details")])]),
            response(output=[dict(type="function_call", name="execute")]),
            response(output=[]),
            response(output=[{}, {}]),
            response(plan=dict(PLAN, speed=3)),
            response(plan=dict(PLAN, volume=True)),
            response(plan=dict(PLAN, device="unknown")),
            response(plan=dict(PLAN, mode="unsupported")),
            io.BytesIO(b"not JSON"),
            io.BytesIO(b" " * 128001),
        ]
        for output in invalid:
            with self.subTest(output=output):
                with self.assertRaises(show_intent.IntentError) as caught:
                    self.request(output)
                self.assertNotIn("private-details", str(caught.exception))

    def test_unsupported_and_negation_do_not_run_small_model_keyword_grounding(self):
        unsupported = dict(PLAN, mode="unsupported", snow="keep", explanation="TV는 지원하지 않아요.")
        with patch("show_intent.ground", side_effect=AssertionError("do not ground Luna")):
            result, _ = self.request(response(unsupported))
        self.assertEqual(result["mode"], "unsupported")

    def test_http_and_network_errors_are_redacted_never_retried_or_fallback(self):
        for error in [
            urllib.error.HTTPError(luna.ENDPOINT, 401, "private key", {}, io.BytesIO(b"private-details")),
            urllib.error.HTTPError(luna.ENDPOINT, 403, "", {}, io.BytesIO()),
            urllib.error.HTTPError(luna.ENDPOINT, 429, "", {}, io.BytesIO()),
            urllib.error.HTTPError(luna.ENDPOINT, 500, "", {}, io.BytesIO()),
            TimeoutError("private-details"), urllib.error.URLError("private-details"),
        ]:
            opener = Mock()
            opener.open.side_effect = error
            with self.subTest(error=type(error)), patch("urllib.request.build_opener", return_value=opener):
                with self.assertRaises(show_intent.IntentError) as caught:
                    luna.interpret("눈 많이", {}, CONFIG)
                self.assertEqual(opener.open.call_count, 1)
                self.assertNotIn("private", str(caught.exception))

    def test_controller_default_stays_ollama_even_if_a_key_exists(self):
        with patch("show_intent.interpret", return_value=PLAN) as ollama, patch("openai_planner.interpret") as openai:
            self.assertEqual(show_control.interpret("눈", {}, CONFIG), PLAN)
        ollama.assert_called_once()
        openai.assert_not_called()
        with patch("openai_planner.interpret", return_value=PLAN) as openai:
            self.assertEqual(show_control.interpret("눈", {}, dict(CONFIG, KEVIN_PLANNER="openai")), PLAN)
        openai.assert_called_once()
        with self.assertRaises(show_control.ShowError):
            show_control.interpret("눈", {}, {"KEVIN_PLANNER": "typo"})

    def test_cli_selection_overrides_env_without_enabling_hardware(self):
        with patch.dict("os.environ", {"KEVIN_PLANNER": "ollama"}, clear=False):
            backend = Backend(planner="openai")
        try:
            self.assertEqual(backend.local.config["KEVIN_PLANNER"], "openai")
            self.assertFalse(backend.local.live_configured)
        finally:
            backend.close()

    def test_controller_displays_actionable_redacted_cloud_errors(self):
        from test_show_control import FakeRadio, FakeTransport, finished, small_scene
        for error, expected in [
            (None, "OPENAI_API_KEY"),
            (luna.OpenAIPlanError("OpenAI 요청 실패 (HTTP 429). API 키·권한·할당량을 확인하세요."), "429"),
            (RuntimeError("private-token-in-platform-error"), "AI 연결"),
        ]:
            c = show_control.ShowController(config={"KEVIN_PLANNER": "openai"},
                radio=FakeRadio(), transport=FakeTransport(), renderer=small_scene)
            try:
                if error is None:
                    c.command("눈 많이", False)
                    state = finished(c)
                else:
                    with patch("openai_planner.interpret", side_effect=error):
                        c.command("눈 많이", False)
                        state = finished(c)
                self.assertEqual(state["status"], "error")
                self.assertIn(expected, state["message"])
                self.assertNotIn("private-token", str(state))
            finally:
                c.close()

    def test_stop_ignores_late_cloud_plan_without_device_calls(self):
        from test_show_control import FakeRadio, FakeTransport, finished, small_scene
        entered, release = threading.Event(), threading.Event()
        radio, transport = FakeRadio(), FakeTransport()
        def delayed(*args):
            entered.set()
            release.wait(2)
            return PLAN
        c = show_control.ShowController(config=dict(CONFIG, KEVIN_PLANNER="openai"),
            radio=radio, transport=transport, renderer=small_scene)
        try:
            with patch("openai_planner.interpret", side_effect=delayed):
                c.command("눈 많이", False)
                self.assertTrue(entered.wait(1))
                c.stop()
                release.set()
                self.assertEqual(finished(c)["status"], "stopped")
                self.assertEqual(transport.calls, [])
                self.assertFalse(any(call[0] == "play" for call in radio.calls))
        finally:
            release.set()
            c.close()

    def test_mcp_say_uses_selected_planner_and_direct_tools_do_not_call_a_model(self):
        import kevin_mcp
        from test_show_control import FakeRadio, FakeTransport, small_scene
        c = show_control.ShowController(config=dict(CONFIG, KEVIN_PLANNER="openai"),
            radio=FakeRadio(), transport=FakeTransport(), renderer=small_scene)
        try:
            with patch.dict("os.environ", {"KEVIN_CAMERA": "off"}):
                mcp = kevin_mcp.Kevin(controller=c, signer=Mock())
            with patch("openai_planner.interpret", return_value=PLAN) as openai:
                result = mcp.call("say", {"text": "눈 많이"})
                self.assertEqual(result["status"], "preview")
                self.assertEqual(result["plan"]["snow"], "heavy")
                openai.assert_called_once()
            with patch("openai_planner.interpret", side_effect=AssertionError("direct tool called model")):
                self.assertEqual(mcp.call("radio", {"music": "stop"})["status"], "preview")
        finally:
            c.close()

    def test_real_http_parser_with_qa_transport_and_redirect_denial(self):
        calls = []
        content = response().read()
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                calls.append((self.headers.get("Authorization"), json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
                self.send_response(200)
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
            def log_message(self, *args): pass
        server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        real = urllib.request.build_opener(luna._NoRedirect)
        def local_qa(request, **kwargs):
            self.assertEqual(request.full_url, luna.ENDPOINT)
            local = urllib.request.Request(f"http://127.0.0.1:{server.server_port}/v1/responses",
                request.data, dict(request.headers))
            return real.open(local, **kwargs)
        opener = Mock()
        opener.open.side_effect = local_qa
        try:
            with patch("urllib.request.build_opener", return_value=opener):
                self.assertEqual(luna.interpret("눈", {}, CONFIG), PLAN)
            self.assertEqual(calls[0][0], "Bearer qa-placeholder")
            self.assertIsNone(luna._NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.invalid"))
        finally:
            server.shutdown()
            server.server_close()

    def test_actual_http_redirect_is_not_followed_with_authorization(self):
        paths = []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                paths.append(self.path)
                self.send_response(302)
                self.send_header("Location", "/credential-sink")
                self.send_header("Content-Length", "0")
                self.end_headers()
            def do_GET(self):
                paths.append(self.path)
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()
            def log_message(self, *args): pass
        server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        real = urllib.request.build_opener(luna._NoRedirect)
        opener = Mock()
        opener.open.side_effect = lambda request, **kwargs: real.open(urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}/v1/responses", request.data, dict(request.headers)), **kwargs)
        try:
            with patch("urllib.request.build_opener", return_value=opener):
                with self.assertRaises(luna.OpenAIPlanError) as caught:
                    luna.interpret("눈", {}, CONFIG)
            self.assertIn("302", str(caught.exception))
            self.assertEqual(paths, ["/v1/responses"])
        finally:
            server.shutdown()
            server.server_close()
