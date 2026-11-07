import copy
import json
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from device_control import DeviceController
from devices.base import Capability, Result
from devices.home_assistant import HomeAssistant
from devices.registry import Device, DeviceError, Registry, load_profile
from show_control import ShowController
from show_intent import _validate, ground
from show_transport import Cancelled


def action(target="lamp", **changes):
    return dict(device=target, changes=changes or {"power": True})


def plan(*actions, mode="execute"):
    return dict(mode=mode, actions=list(actions), explanation="계획")


def finished(controller):
    end = time.monotonic() + 4
    while time.monotonic() < end:
        state = controller.get_state()
        if state["status"] not in ("planning", "sending", "stopping"):
            return state
        time.sleep(.005)
    raise AssertionError(controller.get_state())


class Fake:
    slug = "fixture"
    capabilities = (Capability("power", "Switch power", {"type": "boolean"}),)
    def __init__(self, apply=None):
        self.calls = []
        self.run = apply
    def apply(self, changes, cancel):
        self.calls.append(changes)
        if self.run:
            return self.run(changes, cancel)
        return Result("state_reported", detail={"reported_state": changes["power"]})
    def stop(self):
        return Result("stopped")
    def status(self):
        return {}


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.lamp, self.fan = Fake(), Fake()
        self.registry = Registry([Device("lamp", "램프", [], self.lamp), Device("fan", "선풍기", [], self.fan)])

    def controller(self, value, **options):
        c = DeviceController(registry=self.registry, planner=lambda *_: copy.deepcopy(value),
                             config={"KEVIN_SHOW_LIVE": "1"}, **options)
        self.addCleanup(c.close)
        return c

    def test_entire_plan_validated_before_first_effect(self):
        for value in (plan(action(), action("fan", power="on")),
                      plan(action(), action("unknown")),
                      plan(action(), action()),
                      plan(action(), mode="unsupported")):
            c = self.controller(value)
            c.command("테스트", True)
            self.assertEqual(finished(c)["status"], "error")
        self.assertEqual(self.lamp.calls, [])

    def test_all_targets_preflight_before_first_effect(self):
        self.fan.ready = lambda: False
        c = self.controller(plan(action(), action("fan")))
        c.command("두 기기", True)
        self.assertEqual(finished(c)["status"], "error")
        self.assertEqual(self.lamp.calls, [])

    def test_preview_does_not_call_any_adapter_or_commit_observation(self):
        c = self.controller(plan(action()))
        with patch.object(self.lamp, "apply", side_effect=AssertionError()), patch("socket.socket", side_effect=AssertionError()):
            c.command("램프 켜", False)
            state = finished(c)
        self.assertEqual(state["status"], "preview")
        self.assertEqual(state["observed"], {})

    def test_failure_keeps_completed_effect_and_marks_remainder_not_sent(self):
        self.fan.run = Mock(side_effect=DeviceError("private endpoint"))
        third = Fake()
        self.registry = Registry([*self.registry.devices.values(), Device("third", "세번째", [], third)])
        c = self.controller(plan(action(), action("fan"), action("third")))
        c.command("세 기기", True)
        state = finished(c)
        self.assertEqual([r["status"] for r in state["results"]], ["state_reported", "failed_unknown", "not_sent"])
        self.assertEqual(third.calls, [])
        self.assertNotIn("private endpoint", str(state))

    def test_stop_discards_late_plan_without_waiting_or_effects(self):
        entered, release = threading.Event(), threading.Event()
        c = self.controller(plan(action()))
        def slow(*_):
            entered.set()
            release.wait(2)
            return plan(action())
        c.planner = slow
        c.command("늦는 요청", True)
        self.assertTrue(entered.wait(1))
        c.stop()
        self.assertEqual(c.get_state()["status"], "stopped")
        c.planner = lambda *_: plan(mode="unsupported")
        c.command("새 요청", False)
        release.set()
        self.assertEqual(finished(c)["status"], "unsupported")
        self.assertEqual(self.lamp.calls, [])

    def test_stop_during_dispatch_never_sends_next_action(self):
        entered = threading.Event()
        def slow(changes, cancel):
            entered.set()
            cancel.wait(2)
            raise Cancelled()
        self.lamp.run = slow
        c = self.controller(plan(action(), action("fan")))
        c.command("두 기기", True)
        self.assertTrue(entered.wait(1))
        c.stop()
        state = finished(c)
        self.assertEqual(state["status"], "stopped")
        self.assertEqual(self.fan.calls, [])
        self.assertEqual([r["status"] for r in state["results"]], ["failed_unknown", "not_sent"])

    def test_live_requires_process_opt_in(self):
        c = DeviceController(registry=self.registry, planner=lambda *_: plan(action()), config={})
        self.addCleanup(c.close)
        with self.assertRaises(DeviceError):
            c.command("켜줘", True)

    def test_profile_catalog_has_no_connection_or_secret_data(self):
        adapter = HomeAssistant("ha_switch", dict(url_env="HA_URL", token_env="HA_TOKEN", entity_id="switch.private"),
                                {"HA_URL": "http://secret-host", "HA_TOKEN": "private-token"})
        registry = Registry([Device("lamp", "램프", [], adapter)])
        public = json.dumps([registry.catalog(), registry.schema()])
        for secret in ("private", "secret-host", "HA_TOKEN", "entity_id"):
            self.assertNotIn(secret, public)
        with self.assertRaises(DeviceError):
            Registry([Device("a", "램프", [], Fake()), Device("b", "램프", [], Fake())])

    def test_profile_loader_rejects_imports_unknown_fields_and_raw_ir(self):
        with tempfile.NamedTemporaryFile(mode="w+", suffix=".json") as f:
            f.write(json.dumps(dict(version=1, devices=[dict(id="x", name="X", aliases=[], adapter="os.system", config={})])))
            f.flush()
            with self.assertRaises(DeviceError):
                load_profile(f.name, {})
        with self.assertRaises(DeviceError):
            HomeAssistant("ha_remote", dict(url_env="URL", token_env="TOKEN", entity_id="remote.tv",
                device="tv", commands={"on": "b64:untrusted"}), {})


class CompatibilityTests(unittest.TestCase):
    def test_model_refusal_is_not_overturned_and_volume_does_not_start_music(self):
        base = dict(mode="unsupported", preset="keep", speed=None, snow="keep",
                    sky="keep", music="keep", volume=None, explanation="미지원")
        self.assertEqual(ground("TV 켜고 캐럴 틀어줘", base, {})["mode"], "unsupported")
        update = dict(base, mode="update", volume=30)
        self.assertEqual(_validate(ground("음악 볼륨만 30", update, {}))["music"], "keep")

    def test_show_preview_cannot_leak_music_or_scene_into_live_request(self):
        from test_show_control import FakeRadio, FakeTransport, finished as show_finished, plan as show_plan, small_scene
        plans = iter([show_plan(music="play", snow="heavy"), show_plan(speed=1.5)])
        radio = FakeRadio()
        c = ShowController(planner=lambda *_: next(plans), radio=radio, transport=FakeTransport(),
                           renderer=small_scene, config=dict(KEVIN_SHOW_LIVE="1", KEVIN_LED_ADDRESS="fake", KEVIN_RADIO_DEVICE="CRF-35B"))
        self.addCleanup(c.close)
        c.command("캐럴 눈", False)
        self.assertEqual(show_finished(c)["recipe"]["music"], "play")
        c.command("속도만", True)
        live = show_finished(c)
        self.assertEqual(live["recipe"]["snow"], "normal")
        self.assertEqual(radio.calls, [])


class HAHTTPTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        calls = self.calls
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                calls.append((self.path, self.headers["Authorization"],
                              json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
                self.reply([])
            def do_GET(self):
                calls.append((self.path, self.headers["Authorization"], None))
                self.reply({"state": "on", "last_updated": "2026-11-07T00:00:00Z", "attributes": {}})
            def reply(self, value):
                body = json.dumps(value).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, *args): pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.config = dict(HA_URL=f"http://127.0.0.1:{server.server_port}", HA_TOKEN="qa-token")
        self.settings = dict(url_env="HA_URL", token_env="HA_TOKEN", entity_id="switch.lamp")

    def test_real_http_switch_uses_service_then_reports_state_without_physical_claim(self):
        adapter = HomeAssistant("ha_switch", self.settings, self.config)
        result = adapter.apply({"power": True}, threading.Event())
        self.assertEqual([c[0] for c in self.calls], ["/api/services/switch/turn_on", "/api/states/switch.lamp"])
        self.assertEqual(self.calls[0][1], "Bearer qa-token")
        self.assertEqual(result.status, "state_reported")
        self.assertFalse(result.physically_verified)
        self.assertNotIn("qa-token", str(adapter.status()))

    def test_ir_sends_only_learned_mapping_once_and_stop_never_powers_off(self):
        settings = dict(self.settings, entity_id="remote.room", device="television", commands={"hdmi1": "learned_hdmi_1"})
        adapter = HomeAssistant("ha_remote", settings, self.config)
        result = adapter.apply({"command": "hdmi1"}, threading.Event())
        adapter.stop()
        self.assertEqual(result.status, "sent_unverified")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][2]["command"], "learned_hdmi_1")
        self.assertEqual(self.calls[0][2]["num_repeats"], 1)

    def test_write_success_read_failure_is_not_lost_or_retried(self):
        adapter = HomeAssistant("ha_switch", self.settings, self.config)
        with patch.object(adapter, "_request", side_effect=[[], DeviceError("read")]) as request:
            result = adapter.apply({"power": True}, threading.Event())
        self.assertEqual(result.status, "sent_unverified")
        self.assertEqual(result.detail["state_read"], "unavailable")
        self.assertEqual(request.call_count, 2)

    def test_invalid_auth_url_or_redirect_never_leaks_credentials(self):
        adapter = HomeAssistant("ha_switch", self.settings, dict(self.config, HA_URL="http://user:password@host"))
        with self.assertRaises(DeviceError):
            adapter.apply({"power": True}, threading.Event())
        from devices.home_assistant import NoRedirect
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, "", {}, "http://other"))
