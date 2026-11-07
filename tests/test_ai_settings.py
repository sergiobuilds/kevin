import copy
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock, patch

import ai_settings as ai
import kevin_bridge
import show_control


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "ai.json"
        self.env = {"KEVIN_AI_CONFIG": str(self.path)}
        self.probe = Mock()
        self.settings = ai.AISettings(self.env, probe=self.probe)

    def draft(self, **changes):
        return dict(provider="openai", model="gpt-6-luna", api_key="qa-placeholder",
                    ollama_url="http://127.0.0.1:11434", revision=self.settings.public()["revision"], **changes)

    def save(self, spec=None):
        spec = spec or self.draft()
        ticket = self.settings.test(spec)["test_id"]
        return self.settings.save(spec, ticket)

    def test_saved_config_is_shared_private_and_does_not_publish_key(self):
        public = self.save()
        self.assertTrue(public["verified"])
        self.assertNotIn("qa-placeholder", json.dumps(public))
        self.assertEqual(ai.runtime_config(self.env)["KEVIN_PLANNER"], "openai")
        self.assertEqual(ai.runtime_config(self.env)["OPENAI_API_KEY"], "qa-placeholder")
        self.assertEqual(ai.AISettings(self.env).public()["model"], "gpt-6-luna")
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_test_and_cancel_do_not_save_and_edited_fields_invalidate_ticket(self):
        draft = self.draft()
        result = self.settings.test(draft)
        self.assertFalse(self.path.exists())
        changed = dict(draft, model="another-model")
        with self.assertRaises(ai.SettingsError):
            self.settings.save(changed, result["test_id"])
        self.assertFalse(self.path.exists())

    def test_connection_failure_is_redacted_and_does_not_modify_existing_config(self):
        self.save()
        original = self.path.read_bytes()
        self.probe.side_effect = RuntimeError("private-response-with-key")
        with self.assertRaises(ai.SettingsError) as error:
            self.settings.test(self.draft())
        self.assertNotIn("private", str(error.exception))
        self.assertEqual(original, self.path.read_bytes())

    def test_stale_edit_and_reused_ticket_cannot_overwrite_saved_config(self):
        draft = self.draft()
        first = self.settings.test(draft)["test_id"]
        second = self.settings.test(draft)["test_id"]
        self.settings.save(draft, first)
        original = self.path.read_bytes()
        for ticket in (first, second):
            with self.assertRaises(ai.SettingsError):
                self.settings.save(draft, ticket)
        self.assertEqual(original, self.path.read_bytes())

    def test_environment_overrides_are_honored_and_not_copied_to_disk(self):
        env = dict(self.env, OPENAI_API_KEY="environment-credential", KEVIN_OPENAI_MODEL="env-model")
        settings = ai.AISettings(env, probe=self.probe)
        draft = dict(self.draft(), model="env-model", api_key="")
        result = settings.test(draft)
        settings.save(draft, result["test_id"])
        self.assertNotIn("environment-credential", self.path.read_text())
        self.assertEqual(ai.runtime_config(env)["OPENAI_API_KEY"], "environment-credential")
        self.assertTrue(settings.public()["verified"])
        with self.assertRaises(ai.SettingsError):
            settings.test(dict(draft, revision=settings.public()["revision"], model="ignored-model"))
        explicit = dict(env, KEVIN_PLANNER="ollama")
        self.assertEqual(ai.runtime_config(explicit)["KEVIN_PLANNER"], "ollama")

    def test_local_choice_never_copies_an_environment_key_or_switches_to_cloud(self):
        env = dict(self.env, OPENAI_API_KEY="environment-credential")
        settings = ai.AISettings(env, probe=self.probe)
        self.assertFalse(settings.public()["configured"])
        self.assertEqual(ai.runtime_config(env)["KEVIN_PLANNER"], "ollama")
        draft = dict(self.draft(), provider="ollama", model="qwen2.5:3b", api_key="")
        settings.save(draft, settings.test(draft)["test_id"])
        self.assertNotIn("environment-credential", self.path.read_text())
        self.assertTrue(settings.public()["verified"])

    def test_registered_key_is_preserved_on_blank_input_and_failure_marks_readiness(self):
        self.save()
        draft = dict(self.draft(), api_key="")
        self.settings.save(draft, self.settings.test(draft)["test_id"])
        self.assertEqual(ai.runtime_config(self.env)["OPENAI_API_KEY"], "qa-placeholder")
        self.settings.mark_failed()
        self.assertFalse(self.settings.public()["verified"])

    def test_invalid_config_and_arbitrary_local_destinations_never_probe(self):
        for updates in ({"ollama_url": "http://metadata.invalid"}, {"ollama_url": "http://localhost:bad"},
                        {"api_key": "bad\nheader"}, {"provider": "other"}, {"model": "../ bad"}):
            with self.subTest(updates=updates), self.assertRaises(ai.SettingsError):
                self.settings.test(dict(self.draft(), **updates))
        self.probe.assert_not_called()
        self.path.write_text('{"other_user_data":true}')
        with self.assertRaises(ai.SettingsError):
            self.settings.public()
        self.assertEqual(self.path.read_text(), '{"other_user_data":true}')

    def test_probe_uses_fixed_data_and_cannot_dispatch_a_device(self):
        with patch("show_intent.request_json", return_value={"ready": True}) as local, patch.object(show_control.ShowController, "command", side_effect=AssertionError()):
            ai.probe_connection(dict(provider="ollama", model="qwen2.5:3b", api_key="", ollama_url="http://localhost:11434"))
        self.assertEqual(local.call_args.args[1], "connection check")
        with patch("openai_planner.request_json", return_value={"ready": True}) as remote:
            ai.probe_connection(self.settings.candidate(self.draft())[0])
        self.assertEqual(remote.call_args.args[1], "connection check")
        self.assertFalse(self.path.exists())

    def test_local_probe_and_command_never_follow_redirects(self):
        received = []
        class Redirect(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                self.send_response(302)
                self.send_header("Location", "/capture")
                self.send_header("Content-Length", "0")
                self.end_headers()
            def do_GET(self):
                received.append(self.path)
                self.send_response(200)
                self.end_headers()
        server = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        spec = dict(provider="ollama", model="qwen2.5:3b", api_key="",
                    ollama_url=f"http://127.0.0.1:{server.server_port}")
        config = dict(KEVIN_PLANNER="ollama", KEVIN_OLLAMA_MODEL=spec["model"],
                      KEVIN_OLLAMA_URL=spec["ollama_url"])
        for request in (
            lambda: ai.probe_connection(spec),
            lambda: ai.request_json("system", "private request", {}, config),
            lambda: show_control.interpret("test", {}, dict(self.env, **config)),
        ):
            with self.assertRaises(ValueError):
                request()
        self.assertEqual(received, [])

    def test_saved_choice_reaches_controller_without_restart_and_explicit_flag_wins(self):
        self.save()
        with patch("openai_planner.interpret", return_value={"mode": "unsupported"}) as remote:
            show_control.interpret("test", {}, self.env)
        self.assertEqual(remote.call_args.args[2]["KEVIN_OPENAI_MODEL"], "gpt-6-luna")
        with patch("show_intent.interpret", return_value={}) as local:
            show_control.interpret("test", {}, dict(self.env, KEVIN_PLANNER="ollama"))
        local.assert_called_once()

    def test_cli_setup_masks_key_and_cancel_does_not_save(self):
        with patch("builtins.input", side_effect=["2", "", "n"]), patch("getpass.getpass", return_value="qa-placeholder"):
            self.assertFalse(ai.terminal_setup(self.settings))
        self.assertFalse(self.path.exists())


class SetupHTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = ai.AISettings({"KEVIN_AI_CONFIG": str(Path(self.tmp.name) / "ai.json")}, probe=Mock())
        class Fixture:
            pass
        bridge = Fixture()
        bridge.ai = self.settings
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), kevin_bridge.make_handler(bridge))
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.draft = dict(provider="openai", model="gpt-6-luna", api_key="qa-placeholder",
                          ollama_url="http://127.0.0.1:11434", revision="")

    def call(self, path, data=None, **headers):
        defaults = {"Origin": self.url, "Content-Type": "application/json"}
        defaults.update(headers)
        req = urllib.request.Request(self.url + path, headers=defaults,
            data=None if data is None else json.dumps(data).encode())
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                return response.status, dict(response.headers), json.load(response)
        except urllib.error.HTTPError as error:
            return error.code, dict(error.headers), json.load(error)

    def test_http_test_save_reload_never_returns_key(self):
        status, headers, config = self.call("/ai/config")
        self.assertEqual(status, 200)
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        self.assertEqual(headers["Cache-Control"], "no-store")
        token = {"X-KEVIN-Setup": config["csrf"]}
        _, _, checked = self.call("/ai/test", {"draft": self.draft}, **token)
        status, _, saved = self.call("/ai/save", {"draft": self.draft, "test_id": checked["test_id"]}, **token)
        self.assertEqual(status, 200)
        self.assertTrue(saved["settings"]["verified"])
        self.assertNotIn("qa-placeholder", json.dumps(saved))
        self.assertNotIn("qa-placeholder", json.dumps(self.call("/ai/config")[2]))

    def test_wrong_origin_host_csrf_and_simple_post_cannot_probe_or_write(self):
        for path, data, headers in (
            ("/ai/config", None, {"Origin": "https://attacker.invalid"}),
            ("/ai/config", None, {"Host": f"attacker.invalid:{self.server.server_port}"}),
            ("/ai/test", {"draft": self.draft}, {"X-KEVIN-Setup": "wrong"}),
            ("/ai/test", {"draft": self.draft}, {"Origin": "null", "X-KEVIN-Setup": self.settings.csrf}),
            ("/ai/test", {"draft": self.draft}, {"X-KEVIN-Setup": "é"}),
        ):
            with self.subTest(headers=headers):
                self.assertEqual(self.call(path, data, **headers)[0], 403)
        self.settings.probe.assert_not_called()
        self.assertFalse(self.settings.path.exists())

    def test_save_requires_matching_recent_test_and_rejects_unknown_fields(self):
        token = {"X-KEVIN-Setup": self.settings.csrf}
        self.assertEqual(self.call("/ai/save", {"draft": self.draft, "test_id": "made-up"}, **token)[0], 400)
        status, _, checked = self.call("/ai/test", {"draft": self.draft}, **token)
        self.assertEqual(status, 200)
        changed = dict(self.draft, api_key="edited-after-test")
        self.assertEqual(self.call("/ai/save", {"draft": changed, "test_id": checked["test_id"]}, **token)[0], 400)
        self.assertFalse(self.settings.path.exists())
        self.assertEqual(self.call("/ai/test", {"draft": dict(self.draft, endpoint="https://other.invalid")}, **token)[0], 400)


class WebProviderTests(unittest.TestCase):
    def test_invalid_local_agent_output_never_reaches_device_execution(self):
        turn = {"reply": "ok", "led": {"mode": "keep", "text": "", "color": "#FFFFFF",
            "bg": "#000000", "animation": "static", "preset": "keep", "snow": "keep", "sky": "keep",
            "score_left": 0, "score_right": 0}, "music": {"mode": "keep", "title": "", "tempo": 120,
            "notes": [], "volume": None}, "rps": {"human": "", "kevin": ""}}
        with tempfile.TemporaryDirectory() as tmp:
            env = {"KEVIN_AI_CONFIG": str(Path(tmp) / "ai.json")}
            settings = ai.AISettings(env, probe=Mock())
            draft = dict(provider="ollama", model="qwen2.5:3b", api_key="",
                         ollama_url="http://127.0.0.1:11434", revision="")
            settings.save(draft, settings.test(draft)["test_id"])
            controller = Mock()
            controller.live_configured = True
            controller.get_state.return_value = {"recipe": {}, "radio": {}}
            bridge = kevin_bridge.Bridge(controller=controller, ai=settings, eye=Mock())
            with patch("show_intent.request_json", return_value=turn):
                self.assertEqual(ai.request_json("system", "test", kevin_bridge.kevin_agent.SCHEMA,
                                               ai.runtime_config(env)), turn)
            variants = [{"led": {}}, dict(turn, reply=123), dict(turn, extra="unexpected")]
            for field, value in (("mode", "unknown"), ("score_left", True)):
                variants.append(dict(turn, led=dict(turn["led"], **{field: value})))
            variants.append(dict(turn, music=dict(turn["music"], notes=[{"n": "C4", "b": float("nan")}])))
            for invalid in variants:
                with self.subTest(invalid=invalid), patch("show_intent.request_json", return_value=invalid), patch.object(bridge, "_act") as act:
                    self.assertEqual(bridge.say("test")["error"], "ai_unavailable")
                    act.assert_not_called()
            controller.command.assert_not_called()

    def test_web_uses_selected_provider_model_and_never_silently_falls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {"KEVIN_AI_CONFIG": str(Path(tmp) / "ai.json")}
            settings = ai.AISettings(env, probe=Mock())
            draft = dict(provider="openai", model="gpt-6-luna", api_key="qa-placeholder",
                         ollama_url="http://127.0.0.1:11434", revision="")
            settings.save(draft, settings.test(draft)["test_id"])
            controller = Mock()
            controller.live_configured = False
            controller.get_state.return_value = {"recipe": {}, "radio": {}}
            bridge = kevin_bridge.Bridge(controller=controller, ai=settings, eye=Mock())
            turn = {"reply": "ok", "led": {"mode": "keep", "text": ""}, "music": {"mode": "keep", "notes": []}, "rps": {"human": "", "kevin": ""}}
            with patch("kevin_bridge.ai_json", return_value=turn) as request:
                self.assertEqual(bridge.say("테스트")["source"], "openai-agent")
            self.assertEqual(request.call_args.args[3]["KEVIN_OPENAI_MODEL"], "gpt-6-luna")
            with patch("kevin_bridge.ai_json", side_effect=RuntimeError("private token")), patch("kevin_bridge.plan", side_effect=AssertionError("fallback")):
                result = bridge.say("실패")
            self.assertEqual(result["error"], "ai_unavailable")
            self.assertNotIn("private", json.dumps(result))
            self.assertFalse(settings.public()["verified"])
