"""Contract shapes for ROADMAP's ten tracks, not claims of ten working drivers."""
import copy
import threading
import unittest
from unittest.mock import patch

from devices.base import Capability, Result
from devices.registry import Device, DeviceError, Registry
from devices.wled import WLED
from test_device_control import Fake, action, plan, finished
from device_control import DeviceController


class TrackShapes(unittest.TestCase):
    def test_ten_track_shapes_share_planner_and_executor_without_track_branches(self):
        integer = {"type": "integer", "minimum": 0, "maximum": 100}
        text = {"type": "string"}
        shapes = [
            ("led", "scene", text, "santa"),
            ("light", "zones", {"type": "array", "maxItems": 3, "items": {
                "type": "object", "properties": {"id": integer, "brightness": integer},
                "required": ["id", "brightness"]}}, [{"id": 1, "brightness": 20}]),
            ("speaker", "volume", integer, 30),
            ("wallpad", "channel", integer, 1),
            ("tv", "restore", text, "saved_local_state"),
            ("infrared", "command", {"type": "string", "enum": ["start", "stop"]}, "start"),
            ("vacuum", "zone", {"type": "array", "minItems": 4, "maxItems": 4, "items": integer}, [10, 10, 20, 20]),
            ("security", "illuminate", {"type": "boolean"}, True),
            ("revive", "ears", {"type": "object", "properties": {"left": integer, "right": integer},
                                "required": ["left", "right"]}, {"left": 10, "right": 80}),
            ("discovery", "learned_command", {"type": "string", "enum": ["red", "blue"]}, "red"),
        ]
        devices = []
        for slug, key, schema, value in shapes:
            adapter = Fake(apply=lambda changes, cancel: Result("acknowledged"))
            adapter.capabilities = (Capability(key, slug, schema),)
            devices.append(Device(slug, slug, [], adapter))
        registry = Registry(devices)
        # At most eight actions per request; run the same code for all ten shapes.
        for slug, key, schema, value in shapes:
            with self.subTest(slug=slug):
                c = DeviceController(registry=registry, config={"KEVIN_SHOW_LIVE": "1"},
                                     planner=lambda *_, s=slug, k=key, v=value: plan(action(s, **{k: v})))
                try:
                    c.command("등록된 기기", True)
                    self.assertEqual(finished(c)["status"], "complete")
                    self.assertEqual(registry.devices[slug].adapter.calls[-1], {key: value})
                finally:
                    c.close()
        self.assertEqual(len(registry.schema()["properties"]["actions"]["items"]["anyOf"]), 10)
        with self.assertRaises(DeviceError):
            registry.validate(plan(action("vacuum", zone=[True, 1, 2, 3])))
        with self.assertRaises(DeviceError):
            registry.validate(plan(action("revive", ears={"left": 10, "execute": "raw"})))

    def test_provider_nulls_normalize_without_deleting_required_nested_fields(self):
        adapter = WLED({"url_env": "WLED", "segment_ids": [0, 1, 2]}, {})
        registry = Registry([Device("shelf", "선반", [], adapter)])
        spec = plan(action("shelf", segments=[{"id": 1, "power": None, "brightness": 30, "color": None}]))
        self.assertEqual(registry.validate(spec)["actions"][0]["changes"], {"segments": [{"id": 1, "brightness": 30}]})
        with self.assertRaises(DeviceError):
            registry.validate(plan(action("shelf", segments=[{"id": None, "brightness": 30}])))
        with self.assertRaises(DeviceError):
            registry.validate(plan(action("shelf", segments=[{"id": 2, "brightness": 256}])))
        with self.assertRaises(DeviceError):
            registry.validate(plan(action("shelf", segments=[{"id": 3, "brightness": 30}])))


class WLEDTests(unittest.TestCase):
    def test_direct_api_preserves_unmentioned_zones_and_filters_report(self):
        adapter = WLED({"url_env": "WLED", "segment_ids": [0, 1, 2]}, {"WLED": "http://fixture"})
        state = {"on": True, "bri": 128, "seg": [{"id": 1, "bri": 40, "private": "hidden"}], "private": "hidden"}
        with patch.object(adapter, "_request", side_effect=[{"success": True}, state]) as request:
            result = adapter.apply({"segments": [{"id": 1, "brightness": 40, "color": [255, 0, 0]}]}, threading.Event())
        self.assertEqual(request.call_args_list[0].args[0], {"seg": [{"id": 1, "bri": 40, "col": [[255, 0, 0]]}]})
        self.assertEqual(result.status, "state_reported")
        self.assertFalse(result.physically_verified)
        self.assertNotIn("hidden", str(result.detail))

    def test_duplicate_segments_rejected_before_another_device_is_sent(self):
        lamp = Fake()
        wled = WLED({"url_env": "WLED", "segment_ids": [0, 1]}, {"WLED": "http://fixture"})
        registry = Registry([Device("lamp", "램프", [], lamp), Device("shelf", "선반", [], wled)])
        c = DeviceController(registry=registry, config={"KEVIN_SHOW_LIVE": "1"},
            planner=lambda *_: plan(action(), action("shelf", segments=[{"id": 1, "power": True}, {"id": 1, "power": False}])))
        try:
            c.command("두 기기", True)
            self.assertEqual(finished(c)["status"], "error")
            self.assertEqual(lamp.calls, [])
        finally:
            c.close()
