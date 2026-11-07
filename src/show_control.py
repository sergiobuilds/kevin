"""Background show orchestration with explicit live gates and cancellable effects."""

import base64
import binascii
import copy
import importlib
import math
import os
from pathlib import Path
import re
import threading
import uuid

import pixels
from show_transport import Cancelled, LedTransport, RadioPlayer, check_cancel


class ShowError(ValueError):
    pass


ACTIVE = {'queued', 'planning', 'rendering', 'sending', 'audio', 'stopping'}
DEFAULT_RECIPE = dict(preset='christmas_train', speed=1, snow='normal', sky=None,
                      music='stop', volume=45)


def interpret(prompt, recipe, config):
    provider = config.get('KEVIN_PLANNER', 'ollama')
    if provider == 'openai':
        module = importlib.import_module('openai_planner')
        return module.interpret(prompt, recipe, config)
    if provider != 'ollama':
        raise ShowError('KEVIN_PLANNER must be ollama or openai')
    module = importlib.import_module('show_intent')
    return module.interpret(prompt, recipe, endpoint=config.get('KEVIN_OLLAMA_URL'),
                            model=config.get('KEVIN_OLLAMA_MODEL'))


def normalized_plan(plan):
    if not isinstance(plan, dict):
        raise ShowError('model returned an invalid plan')
    result = dict(mode=plan.get('mode'), preset=plan.get('preset', 'keep'),
                  speed=plan.get('speed'), snow=plan.get('snow', 'keep'),
                  sky=plan.get('sky', 'keep'), music=plan.get('music', 'keep'),
                  volume=plan.get('volume'), explanation=plan.get('explanation', ''))
    for name, choices in (
        ('mode', ('start', 'update', 'stop', 'unsupported')),
        ('preset', ('christmas_train', 'santa', 'keep')),
        ('snow', ('keep', 'none', 'normal', 'heavy')),
        ('music', ('play', 'stop', 'keep')),
    ):
        if result[name] not in choices:
            raise ShowError('model returned an invalid ' + name)
    if not isinstance(result['sky'], str) or (result['sky'] != 'keep' and
                                               not re.fullmatch(r'#[0-9a-fA-F]{6}', result['sky'])):
        raise ShowError('model returned an invalid sky color')
    for name, lower, upper in (('speed', .5, 2), ('volume', 0, 100)):
        value = result[name]
        if value is not None and (type(value) not in (int, float) or
                                  not math.isfinite(value) or not lower <= value <= upper):
            raise ShowError('model returned an invalid ' + name)
    if not isinstance(result['explanation'], str) or len(result['explanation']) > 4000:
        raise ShowError('model returned an invalid explanation')
    return result


def render_recipe(recipe):
    import oba_christmas_show
    if recipe['preset'] == 'christmas_train':
        scene = oba_christmas_show.christmas_scene(snow=recipe['snow'], sky=recipe['sky'])
    else:
        import oba_media_bridge
        scene = oba_media_bridge.santa_scene()
        for index, frame in enumerate(scene['frames']):
            if recipe['sky'] is not None:
                for row in frame:
                    row[:] = [recipe['sky'] if color == '#061329' else color for color in row]
            for flake in range({'none': 0, 'normal': 35, 'heavy': 105}[recipe['snow']]):
                frame[(flake * 7 + index) % 16][(flake * 37 + index // 8) % 144] = '#dceaff'
    scene['duration'] = max(40, min(5000, round(120 / recipe['speed'] / 10) * 10))
    return pixels.validate(scene)


class ShowController:
    def __init__(self, *, planner=None, transport=None, radio=None, renderer=None,
                 transcriber=None, config=None):
        self.config = dict(os.environ if config is None else config)
        self.lock = threading.RLock()
        self.planner = planner
        self.renderer = renderer or render_recipe
        self.transcriber = transcriber
        self.transport = transport or LedTransport(self.config.get('KEVIN_LED_ADDRESS', ''))
        self.data_dir = Path(self.config.get('KEVIN_SHOW_DATA_DIR') or
                             Path(__file__).resolve().parent.parent / '.runtime')
        self.radio = radio or RadioPlayer(self.config.get('KEVIN_RADIO_DEVICE', ''),
                                          self.data_dir / 'jingle_bells.wav')
        self.cancel = threading.Event()
        self.cleanup_cancel = threading.Event()
        self.closed = False
        self.audio_prepared = False
        self.applied_scene = None
        self.state = dict(status='idle', message='말 걸어줘.', job_id=None, revision=0,
                          recipe=copy.deepcopy(DEFAULT_RECIPE),
                          transport={'status': 'not_sent', 'physical_verified': False},
                          radio={'status': 'not_started', 'audible_verified': False})

    @property
    def live_configured(self):
        return (self.config.get('KEVIN_SHOW_LIVE') == '1' and
                bool(self.config.get('KEVIN_LED_ADDRESS', '').strip()) and
                self.config.get('KEVIN_RADIO_DEVICE') == 'CRF-35B')

    def get_state(self):
        with self.lock:
            result = copy.deepcopy(self.state)
        result.update(live_configured=self.live_configured,
                      voice_configured=bool(self.config.get('KEVIN_ASR_URL') and
                                            self.config.get('KEVIN_ASR_TOKEN')),
                      radio_device='CRF-35B' if self.config.get('KEVIN_RADIO_DEVICE') == 'CRF-35B' else None)
        # Read actual player state only after live playback was attempted.
        if result['radio']['status'] not in ('not_started', 'stopped', 'stopped_child_terminated'):
            result['radio'] = self.radio.status()
        return result

    def command(self, prompt, execute=False, scene=None):
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 2000:
            raise ShowError('prompt must contain 1..2000 characters')
        if type(execute) is not bool:
            raise ShowError('execute must be a boolean')
        if execute and not self.live_configured:
            raise ShowError('live execution requires server live, panel and CRF-35B configuration')
        if scene is not None:
            scene = copy.deepcopy(pixels.validate(scene))
            if scene['duration'] % 10:
                raise ShowError('exact 10ms frame delays required')
        with self.lock:
            if self.closed:
                raise ShowError('show controller is closed')
            if self.state['status'] in ACTIVE:
                raise ShowError('show job active; stop it before another command')
            cancel = self.cancel = threading.Event()
            job_id = uuid.uuid4().hex
            recipe = copy.deepcopy(self.state['recipe'])
            current = copy.deepcopy(scene if scene is not None else self.state.get('scene'))
            self.state.update(status='queued', job_id=job_id, message='응, 들었어.',
                              transcript=prompt.strip())
            self.state.pop('plan', None)
            threading.Thread(target=self._run, args=(job_id, cancel, prompt.strip(),
                             execute, recipe, current), daemon=True).start()
        return {'job_id': job_id, 'status': 'queued'}

    def supersede(self, wait=6.0):
        """Drop the job in flight so a newer request runs now (people talk over KEVIN)."""
        import time
        end = time.monotonic() + wait
        while True:
            with self.lock:
                status = self.state['status']
                if status not in ACTIVE:
                    return False
                if status != 'stopping':
                    self.cancel.set()
                    self.applied_scene = None
                    self.state.update(status='idle', message='새 요청으로 바꿀게.')
                    return True
            if time.monotonic() > end:
                return False
            time.sleep(.1)

    def _update(self, cancel, **fields):
        with self.lock:
            if self.cancel is not cancel or cancel.is_set() or self.closed:
                raise Cancelled('show cancelled')
            self.state.update(fields)

    def _commit_scene(self, cancel, recipe, scene):
        with self.lock:
            self._update(cancel, recipe=recipe)
            if self.state.get('scene') != scene:
                self.state['scene'] = copy.deepcopy(scene)
                self.state['revision'] += 1

    def _prepare_audio(self, cancel):
        import oba_christmas_show
        check_cancel(cancel)
        with self.lock:
            if self.audio_prepared:
                return
        audio = oba_christmas_show.jingle_bells_wav()
        check_cancel(cancel)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        path = self.data_dir / 'jingle_bells.wav'
        # One controller/job writes this owned artifact; no user media path accepted.
        temporary = self.data_dir / ('carol-' + uuid.uuid4().hex + '.tmp')
        try:
            temporary.write_bytes(audio)
            with self.lock:
                check_cancel(cancel)
                temporary.replace(path)
                self.audio_prepared = True
        finally:
            temporary.unlink(missing_ok=True)

    def _run(self, job_id, cancel, prompt, execute, recipe, scene):
        stage = 'planning'
        try:
            self._update(cancel, status=stage, message='음… 어떻게 할지 생각 중.')
            raw = (self.planner(prompt, copy.deepcopy(recipe)) if self.planner else
                   interpret(prompt, copy.deepcopy(recipe), self.config))
            check_cancel(cancel)
            plan = normalized_plan(raw)
            self._update(cancel, plan=plan)
            if plan['mode'] == 'unsupported':
                self._update(cancel, status='unsupported', message=plan['explanation'] or '그건 지금 내가 움직일 수 있는 기기로는 못 해.')
                return
            if plan['mode'] == 'stop':
                if execute:
                    self.stop(expected_cancel=cancel)
                else:
                    self._update(cancel, status='preview', message='다 멈추면 돼. (미리보기라 기기는 그대로야)')
                return
            previous = recipe.copy()
            for key in ('preset', 'snow', 'sky', 'music', 'speed', 'volume'):
                if plan[key] not in (None, 'keep'):
                    recipe[key] = plan[key]
            visual_change = any(previous[k] != recipe[k] for k in ('preset', 'snow', 'sky'))
            stage = 'rendering'
            self._update(cancel, status=stage, message='장면 그리는 중이야.')
            if scene is None or visual_change:
                scene = self.renderer(recipe)
            elif recipe['speed'] != previous['speed']:
                # Keep manually edited pixels when only changing playback speed.
                scene['duration'] = max(40, min(5000, round(
                    scene['duration'] * previous['speed'] / recipe['speed'] / 10) * 10))
            scene = pixels.validate(scene)
            self._commit_scene(cancel, recipe, scene)
            if not execute:
                self._update(cancel, status='preview', message=(plan['explanation'] or '이렇게 하면 돼.') + ' (미리보기)')
                return
            if self.applied_scene != scene:
                stage = 'sending'
                with self.lock:
                    self.applied_scene = None
                self._update(cancel, status=stage, message='전광판에 보내는 중!',
                             transport={'status': 'sending', 'physical_verified': False})
                result = self.transport.send(scene, cancel)
                check_cancel(cancel)
                if not isinstance(result, dict) or result.get('status') != 'acknowledged':
                    raise ShowError('LED did not acknowledge transfer')
                with self.lock:
                    self._update(cancel, transport=dict(result, physical_verified=False))
                    self.applied_scene = copy.deepcopy(scene)
            stage = 'audio'
            self._update(cancel, status=stage, message='라디오 켤게.')
            if recipe['music'] == 'play':
                self._prepare_audio(cancel)
                check_cancel(cancel)
                result = self.radio.play(recipe['volume'], cancel)
                check_cancel(cancel)
                self._update(cancel, radio=dict(result, audible_verified=False))
            elif plan['music'] == 'stop':
                check_cancel(cancel)
                self._update(cancel, radio=self.radio.stop())
            self._update(cancel, status='complete', message=plan['explanation'] or '됐어!')
        except Cancelled:
            return
        except Exception as error:
            # Never leak model URLs/tokens, BLE identifiers or platform exceptions.
            if stage == 'audio' and not cancel.is_set():
                try:
                    self.radio.stop()
                except Exception:
                    pass
                with self.lock:  # don't make every later request fail on the same missing radio
                    self.state['recipe']['music'] = 'stop'
            messages = {
                'planning': '어, 지금 머리가 잘 안 돌아가. AI 연결을 한번 봐줄래?',
                'rendering': '그 장면은 못 그리겠어. 다르게 말해줄래?',
                'sending': '전광판이 대답을 안 해. 전원이랑 블루투스 좀 봐줄래?',
                'audio': '전광판은 바꿨는데 라디오가 안 잡혀. 소리 출력에 라디오 연결됐는지 봐줘.',
            }
            if stage == 'planning' and self.config.get('KEVIN_PLANNER') == 'openai':
                from openai_planner import OpenAIPlanError
                if isinstance(error, OpenAIPlanError):
                    # Only this planner's explicitly redacted public messages.
                    messages['planning'] = str(error)
            try:
                fields = dict(status='error', message=messages[stage])
                if stage == 'sending':
                    fields['transport'] = {'status': 'failed_unknown', 'physical_verified': False}
                if stage == 'audio':
                    fields['radio'] = {'status': 'failed_unknown', 'audible_verified': False}
                self._update(cancel, **fields)
            except Cancelled:
                pass

    def send_manual_scene(self, scene, address, confirmed):
        if not self.live_configured or confirmed is not True or address != self.config.get('KEVIN_LED_ADDRESS'):
            raise ShowError('manual live transfer requires the configured panel and explicit confirmation')
        scene = copy.deepcopy(pixels.validate(scene))
        with self.lock:
            if self.closed or self.state['status'] in ACTIVE:
                raise ShowError('show job active; stop it before manual transfer')
            cancel = self.cancel = threading.Event()
            self.applied_scene = None
            self.state.update(status='sending', job_id=uuid.uuid4().hex,
                              transport={'status': 'sending', 'physical_verified': False})
        try:
            result = self.transport.send(scene, cancel)
            if not isinstance(result, dict) or result.get('status') != 'acknowledged':
                raise ShowError('LED did not acknowledge transfer')
            with self.lock:
                self._update(cancel, status='complete',
                             transport=dict(result, physical_verified=False))
                # Manual edits are outside the recipe; the next show must resend.
            return result
        except Cancelled:
            raise ShowError('manual transfer cancelled') from None
        except Exception:
            try:
                self._update(cancel, status='error',
                             transport={'status': 'failed_unknown', 'physical_verified': False})
            except Cancelled:
                pass
            raise ShowError('manual LED transfer failed; physical state unknown') from None

    def stop(self, *, expected_cancel=None):
        with self.lock:
            if expected_cancel is not None and (self.cancel is not expected_cancel or expected_cancel.is_set()):
                raise Cancelled('show cancelled')
            self.cancel.set()
            if self.state['status'] == 'stopping':
                return {'status': 'stopping', 'job_id': self.state['job_id']}
            self.state.update(status='stopping', message='멈추는 중…')
            self.state['recipe']['music'] = 'stop'
            self.applied_scene = None
        try:
            radio = self.radio.stop()
        except Exception:
            radio = {'status': 'stop_failed_unknown', 'audible_verified': False}
        with self.lock:
            self.state['radio'] = radio
        if (self.config.get('KEVIN_SHOW_LIVE') == '1' and
                self.config.get('KEVIN_LED_ADDRESS', '').strip() and not self.closed):
            threading.Thread(target=self._clear, daemon=True).start()
        else:
            with self.lock:
                failed = radio['status'] == 'stop_failed_unknown'
                self.state.update(status='error' if failed else 'stopped', message=(
                    '라디오가 안 멈춰. 직접 꺼줄래?' if failed else
                    '다 멈췄어.'))
        return {'status': self.get_state()['status'], 'job_id': self.state['job_id']}

    def _clear(self):
        try:
            result = self.transport.clear(self.cleanup_cancel)
            if not isinstance(result, dict) or result.get('status') != 'clear_acknowledged':
                raise ShowError('LED clear was not acknowledged')
            with self.lock:
                failed = self.state['radio']['status'] == 'stop_failed_unknown'
                self.state.update(status='error' if failed else 'stopped', message=(
                    '전광판은 껐는데 라디오가 안 멈춰. 직접 꺼줄래?' if failed else
                    '다 멈췄어. 전광판도 껐어.'),
                                  transport=dict(result, physical_verified=False))
        except Exception:
            with self.lock:
                self.state.update(status='error', message='음악은 멈췄는데 전광판이 안 꺼져. 봐줄래?',
                                  transport={'status': 'clear_failed_unknown', 'physical_verified': False})

    def transcribe(self, audio, mime):
        if not isinstance(audio, str) or len(audio) > 4 * ((2 * 1024 * 1024 + 2) // 3):
            raise ShowError('audio exceeds 2MB decoded limit')
        if not isinstance(mime, str) or not re.fullmatch(r'audio/[a-zA-Z0-9.+_-]+(?:;[a-zA-Z0-9=.+_ -]+)?', mime):
            raise ShowError('valid audio MIME required')
        try:
            decoded = base64.b64decode(audio, validate=True)
        except (ValueError, binascii.Error):
            raise ShowError('invalid base64 audio') from None
        if not decoded or len(decoded) > 2 * 1024 * 1024:
            raise ShowError('audio must contain 1 byte..2MB')
        try:
            if self.transcriber:
                result = self.transcriber(audio, mime)
            else:
                module = importlib.import_module('show_transcription')
                result = module.transcribe(audio, mime, endpoint=self.config.get('KEVIN_ASR_URL'),
                                           token=self.config.get('KEVIN_ASR_TOKEN'))
            if not isinstance(result, dict) or not isinstance(result.get('text'), str) or not isinstance(result.get('language'), str):
                raise ShowError('invalid transcription response')
            with self.lock:
                self.state.pop('speech_error', None)
            return {'text': result['text'], 'language': result['language']}
        except Exception:
            message = '음성 변환 실패: 로컬 ASR 설정과 연결을 확인하고 텍스트로 입력하세요.'
            with self.lock:
                self.state['speech_error'] = message
            raise ShowError(message) from None

    def close(self):
        with self.lock:
            self.closed = True
            self.cancel.set()
            self.cleanup_cancel.set()
        self.radio.close()
