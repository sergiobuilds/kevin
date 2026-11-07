"""Compatibility adapter: the original Christmas show remains hub-free."""
import copy
import threading
import time

from devices.base import Capability, Result
from devices.registry import DeviceError
from show_control import ACTIVE, ShowController
from show_transport import Cancelled, check_cancel


class ChristmasShow:
    slug = "christmas_show"
    capabilities = (
        Capability("preset", "LED scene", {"type": "string", "enum": ["christmas_train", "santa"]}),
        Capability("speed", "Animation speed multiplier", {"type": "number", "minimum": .5, "maximum": 2}),
        Capability("snow", "Snow amount", {"type": "string", "enum": ["none", "normal", "heavy"]}),
        Capability("sky", "Sky colour name", {"type": "string", "enum": ["red", "orange", "yellow", "green", "blue", "navy", "purple", "pink", "black", "night"]}),
        Capability("music", "Carol playback; keep unchanged for volume-only requests", {"type": "string", "enum": ["play", "stop"]}),
        Capability("volume", "Radio volume", {"type": "number", "minimum": 0, "maximum": 100}),
    )
    colors = dict(red="#d0202a", orange="#f07818", yellow="#e8c020", green="#1f8a3a", blue="#1d3f9e",
                  navy="#14215c", purple="#5b2a9e", pink="#d8508a", black="#05060c", night="#0b1a3a")

    def __init__(self, config):
        self.pending = {}
        self.controller = ShowController(planner=lambda *_: copy.deepcopy(self.pending), config=config)
        self.lock = threading.Lock()

    def ready(self):
        return self.controller.live_configured

    def context(self):
        return {"settings": copy.deepcopy(self.controller.live_recipe),
                "source": "app_configuration_not_physical_observation"}

    def _run(self, changes, cancel, execute):
        with self.lock:
            check_cancel(cancel)
            self.pending = dict(mode="update", **changes)
            if "sky" in changes:
                self.pending["sky"] = self.colors[changes["sky"]]
            self.controller.command("검증된 기기 계획", execute=execute)
            try:
                while True:
                    check_cancel(cancel)
                    state = self.controller.get_state()
                    if state["status"] not in ACTIVE:
                        if state["status"] not in ("complete", "preview"):
                            raise DeviceError(state["message"])
                        return Result("acknowledged" if execute else "preview",
                                      detail={"recipe": state["recipe"], "frames": len(state["scene"]["frames"]),
                                              "evidence": "transport_ack_only" if execute else "rendered_only"})
                    time.sleep(.02)
            except Cancelled:
                if not execute:
                    with self.controller.lock:
                        self.controller.cancel.set()
                        self.controller.state.update(status="stopped")
                raise

    def preview(self, changes, cancel):
        return self._run(changes, cancel, False)

    def apply(self, plan, cancel):
        return self._run(plan, cancel, True)

    def stop(self):
        self.controller.stop()
        deadline = time.monotonic() + 100
        while self.controller.get_state()["status"] == "stopping":
            if time.monotonic() >= deadline:
                raise DeviceError("쇼 정리 제한 시간을 초과했습니다.")
            time.sleep(.02)
        if self.controller.get_state()["status"] == "error":
            raise DeviceError("쇼 기기 정리는 미확인입니다.")
        return Result("stopped", detail={"scope": "owned_show", "physical_state": "unverified"})

    def status(self):
        # Public planner context contains neither frames nor addresses.
        state = self.controller.get_state()
        return {"recipe": copy.deepcopy(self.controller.live_recipe), "status": state["status"]}

    def close(self):
        self.controller.close()
