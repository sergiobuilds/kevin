"""Validate a whole plan before dispatch; retain honest per-device outcomes."""
import copy
from dataclasses import asdict
import os
import threading
import uuid

import device_intent
from devices.registry import DeviceError, load_profile
from show_transport import Cancelled, check_cancel


class DeviceController:
    def __init__(self, *, profile=None, registry=None, planner=None, config=None):
        self.config = dict(os.environ if config is None else config)
        self.registry = registry or load_profile(profile, self.config)
        self.planner = planner
        self.lock = threading.RLock()
        self.cancel = threading.Event()
        self.closed = False
        self.owned = set()
        self.in_flight = False
        self.cleanup_pending = False
        self.live_settings = {d.id: getattr(d.adapter, "context", lambda: {})()
                              for d in self.registry.devices.values()}
        self.draft_settings = copy.deepcopy(self.live_settings)
        self.state = dict(status="idle", job_id=None, message="명령을 입력하세요.",
                          results=[], observed={}, preview=None)

    def get_state(self):
        with self.lock:
            state = copy.deepcopy(self.state)
        state.update(live_configured=self.config.get("KEVIN_SHOW_LIVE") == "1",
                     voice_configured=bool(self.config.get("KEVIN_ASR_URL") and self.config.get("KEVIN_ASR_TOKEN")))
        return state

    def command(self, prompt, execute=False):
        if not isinstance(prompt, str) or not 1 <= len(prompt.strip()) <= 2000 or type(execute) is not bool:
            raise DeviceError("요청 또는 실행 모드를 확인하세요.")
        if execute and self.config.get("KEVIN_SHOW_LIVE") != "1":
            raise DeviceError("--live로 실행한 요청만 기기에 전송할 수 있습니다.")
        with self.lock:
            if self.closed or self.in_flight or self.cleanup_pending or self.state["status"] in ("planning", "sending", "stopping"):
                raise DeviceError("기존 작업을 정지한 후 요청하세요.")
            self.cancel = threading.Event()
            cancel = self.cancel
            job = uuid.uuid4().hex
            observed = dict(reported=copy.deepcopy(self.state["observed"]),
                            settings=copy.deepcopy(self.live_settings if execute else self.draft_settings),
                            source="live_baseline" if execute else "preview_draft")
            self.state.update(status="planning", job_id=job, message="AI가 기기 요청을 해석하고 있습니다.",
                              results=[], preview=None)
            self.state.pop("plan", None)
            threading.Thread(target=self._run, args=(cancel, prompt.strip(), execute, observed), daemon=True).start()
        return {"job_id": job, "status": "queued"}

    def _set(self, cancel, **fields):
        with self.lock:
            if self.closed or self.cancel is not cancel:
                raise Cancelled("superseded job")
            check_cancel(cancel)
            self.state.update(fields)

    def _run(self, cancel, prompt, execute, observed):
        try:
            raw = (self.planner(prompt, self.registry, observed) if self.planner else
                   device_intent.interpret(prompt, self.registry, observed, self.config))
            check_cancel(cancel)
            plan = self.registry.validate(raw)
            self._set(cancel, plan=plan)
            if plan["mode"] in ("unsupported", "clarify"):
                self._set(cancel, status=plan["mode"], message=plan["explanation"])
                return
            if plan["mode"] == "stop":
                if execute:
                    self.stop()
                else:
                    self._set(cancel, status="preview", message="정지 미리보기: 기기에 전송하지 않았습니다.", preview=plan)
                return
            # Preflight the ENTIRE plan before the first device write.
            if execute:
                for action in plan["actions"]:
                    adapter = self.registry.devices[action["device"]].adapter
                    ready = getattr(adapter, "ready", lambda: True)
                    if not ready():
                        raise DeviceError("요청한 기기 연결 설정이 준비되지 않았습니다.")
            if not execute:
                rendered = []
                for action in plan["actions"]:
                    adapter = self.registry.devices[action["device"]].adapter
                    # Only the native renderer has a preview; HA does no I/O at all.
                    preview = getattr(adapter, "preview", None)
                    if preview is not None:
                        rendered.append(dict(device=action["device"], **asdict(preview(action["changes"], cancel))))
                self._set(cancel, status="preview", message="미리보기 완료: 기기를 조작하지 않았습니다.",
                          preview=plan, results=rendered)
                with self.lock:
                    self._set(cancel)
                    for item in rendered:
                        if "recipe" in item["detail"]:
                            self.draft_settings[item["device"]] = {"settings": item["detail"]["recipe"], "source": "preview_only"}
                return
            for index, action in enumerate(plan["actions"]):
                with self.lock:
                    self._set(cancel, status="sending", message=f'{action["device"]}: 기기 동작을 적용하고 있습니다.')
                    self.in_flight = True
                    self.owned.add(action["device"])
                try:
                    result = self.registry.devices[action["device"]].adapter.apply(action["changes"], cancel)
                    if result.status not in ("acknowledged", "sent_unverified", "state_reported"):
                        raise DeviceError("기기가 동작을 수락하지 않았습니다.")
                except Exception:
                    with self.lock:
                        if self.cancel is cancel:
                            self.state["results"].append(dict(device=action["device"], status="failed_unknown",
                                physically_verified=False, detail={"dispatch": "attempted"}))
                            self.state["results"].extend(dict(device=a["device"], status="not_sent",
                                physically_verified=False, detail={}) for a in plan["actions"][index + 1:])
                    raise
                finally:
                    with self.lock:
                        self.in_flight = False
                with self.lock:
                    # Preserve successful effects even if Esc arrived during the call.
                    if self.cancel is cancel:
                        public = dict(device=action["device"], **asdict(result))
                        self.state["results"].append(public)
                        self.state["observed"][action["device"]] = public
                        if "recipe" in result.detail:
                            self.live_settings[action["device"]] = {"settings": result.detail["recipe"],
                                                                    "source": "last_applied_configuration"}
                check_cancel(cancel)
            self._set(cancel, status="complete", message="전송 처리 완료. 기기별 상태 보고와 실제 관찰은 별도입니다.")
        except Cancelled:
            pass
        except Exception:
            try:
                self._set(cancel, status="error", message="기기 계획·연결·전송을 확인하세요. 실패한 동작은 자동 재시도하지 않았습니다.")
            except Cancelled:
                pass
        finally:
            with self.lock:
                self._finish_stop(cancel)

    def _finish_stop(self, cancel):
        if (self.cancel is cancel and cancel.is_set() and not self.in_flight and
                not self.cleanup_pending and self.state["status"] == "stopping"):
            self.state.update(status="stopped", message="작업 취소 완료. 이미 전송된 기기 동작은 되돌리지 않았습니다.")

    def stop(self):
        with self.lock:
            self.cancel.set()
            cancel = self.cancel
            # A planner has no hardware effects, so a new job need not await its HTTP response.
            if self.state["status"] == "stopping":
                return {"status": "stopping", "job_id": self.state["job_id"]}
            self.cleanup_pending = bool(self.owned)
            self.state.update(status="stopping" if self.in_flight or self.cleanup_pending else "stopped",
                              message="대기 동작 취소. 이미 전송된 동작의 실제 상태는 확인이 필요합니다.")
            owned = list(self.owned)
        def cleanup():
            failed = False
            for target in owned:
                try:
                    adapter = self.registry.devices[target].adapter
                    adapter.stop()
                    context = getattr(adapter, "context", None)
                    if context is not None:
                        with self.lock:
                            if self.cancel is cancel:
                                self.live_settings[target] = context()
                except Exception:
                    failed = True
            with self.lock:
                if self.cancel is cancel:
                    self.cleanup_pending = False
                    if failed:
                        self.state.update(status="error", message="작업은 취소했지만 기기 정리는 미확인입니다.")
                    else:
                        self._finish_stop(cancel)
        threading.Thread(target=cleanup, daemon=True).start()
        return {"status": self.get_state()["status"], "job_id": self.get_state()["job_id"]}

    def transcribe(self, audio, mime):
        # Reuse the original bounded ASR validation without constructing an active show.
        from show_control import ShowController
        helper = ShowController(config={k: v for k, v in self.config.items() if k != "KEVIN_SHOW_LIVE"})
        try:
            return helper.transcribe(audio, mime)
        finally:
            helper.close()

    def close(self):
        with self.lock:
            self.closed = True
            self.cancel.set()
        for device in self.registry.devices.values():
            close = getattr(device.adapter, "close", None)
            if close:
                close()
