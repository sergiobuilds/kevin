"""Web/guardian job ownership: one cancellation token from planning to evidence."""
from __future__ import annotations

import base64
import copy
from pathlib import Path
import threading
import time
import uuid

import guardian
import kevin_agent
from receipt import Signer, check, make_receipt
from show_transport import Cancelled, check_cancel
import song

FINAL = {'complete', 'preview', 'failed', 'stopped', 'unsupported'}


class BridgeRuntime:
    def __init__(self, controller, eye, *, plan_fn, agent_fn, vision_fn, out_dir=None,
                 clock=time.monotonic, idle_limit=30):
        self.c, self.eye = controller, eye
        self.plan_fn, self.agent_fn, self.vision_fn = plan_fn, agent_fn, vision_fn
        self.clock, self.idle_limit = clock, idle_limit
        self.lock = threading.RLock()
        self.execution = threading.Lock()
        self.vision_lock = threading.Lock()
        self.jobs, self.active, self.history = {}, None, []
        self.score = {'human': 0, 'kevin': 0}
        self.led_now, self.mode, self.guard_epoch = 'unknown', 'talk', 0
        self.alarm_until = 0
        self._reset_absence()
        self.out_dir = Path(out_dir or Path(__file__).resolve().parent.parent / '.runtime' / 'bridge')
        self.signer = Signer(self.out_dir / 'signing-key.pem')

    def _reset_absence(self):
        self.absent_since = self.last_absence = self.last_frame = None
        self.absence_count = 0

    def _public(self, job):
        return copy.deepcopy({k: v for k, v in job.items() if k != 'cancel'})

    def _new(self, kind):
        # Called with lock held; stop/new planning/action registration is atomic.
        if self.active:
            old = self.jobs[self.active]
            old['cancel'].set()
            if old['status'] not in FINAL:
                old.update(status='stopped', message='새 요청으로 바뀌었어.', receipt=None)
        self.c.supersede(wait=0)
        self.guard_epoch += 1
        job = dict(job_id=uuid.uuid4().hex, kind=kind, status='planning',
                   message='요청을 접수했어.', cancel=threading.Event(),
                   device_results={}, physically_verified=False,
                   observation={'status': 'unavailable', 'reason': 'not_observed', 'scope': 'visual_change_only'},
                   receipt=None)
        self.jobs[job['job_id']] = job
        self.active = job['job_id']
        while len(self.jobs) > 30:
            del self.jobs[next(iter(self.jobs))]
        return job

    def _update(self, job, **values):
        with self.lock:
            check_cancel(job['cancel'])
            if self.active != job['job_id']:
                raise Cancelled()
            job.update(values)

    def _finish_error(self, job, message):
        try:
            if job.get('pending_device'):
                with self.lock:
                    check_cancel(job['cancel'])
                    job['device_results'][job['pending_device']] = {'status': 'failed_unknown', 'physically_verified': False}
            self._update(job, status='failed', message=message)
        except Cancelled:
            pass

    def state(self, job_id=None):
        with self.lock:
            key = job_id or self.active
            if key and key not in self.jobs:
                raise KeyError(key)
            job = self._public(self.jobs[key]) if key else None
        state = self.c.get_state()
        return dict(job=job, status=job['status'] if job else state['status'],
                    message=job['message'] if job else state.get('message'),
                    recipe=state.get('applied_recipe', state.get('recipe')),
                    transport=state.get('transport'), radio=state.get('radio'),
                    led_now=self.led_now,
                    last_error=job['message'] if job and job['status'] == 'failed' else None,
                    physically_verified=False)

    def say(self, text):
        if not isinstance(text, str) or not text.strip() or len(text) > 500:
            raise ValueError('text must contain 1..500 characters')
        with self.lock:
            job = self._new('say')
            threading.Thread(target=self._say_worker, args=(job, text.strip()), daemon=True).start()
            return dict(job_id=job['job_id'], status='planning', reply='응, 요청을 확인하고 있어.')

    def _say_worker(self, job, text):
        cancel = job['cancel']
        try:
            state = self.c.get_state()
            with self.lock:
                history = copy.deepcopy(self.history)
                summary = {'recipe': state.get('applied_recipe', state.get('recipe')),
                           'led': self.led_now, 'radio': (state.get('radio') or {}).get('status')}
            try:
                turn = kevin_agent.validate_turn(self.agent_fn(text, history, summary))
                source = 'openai-agent'
            except Exception:
                check_cancel(cancel)
                p, source = self.plan_fn(text, summary['recipe'] or {})
                self._update(job, plan=p, source=source)
                if p['mode'] == 'unsupported':
                    self._update(job, status='unsupported', message=p['explanation'])
                    return
                if p['mode'] == 'stop':
                    with self.lock:
                        check_cancel(cancel)
                        job.update(status='stopping', message='중단 응답을 기다리고 있어.')
                        self.c.stop()
                    self._stop_worker(job)
                    return
                with self.execution:
                    check_cancel(cancel)
                    before = self.eye.snapshot()
                    self._controller(job, p, text)
                    self._evidence(job, before, p)
                return
            with self.execution:
                check_cancel(cancel)
                with self.lock:
                    check_cancel(cancel)
                    turn = kevin_agent.judge_rps(turn, self.score)
                self._update(job, turn=turn, source=source, status='running', message='기기 응답을 확인하고 있어.')
                before = self.eye.snapshot()
                self._act(job, turn)
                check_cancel(cancel)
                self._evidence(job, before, turn)
                with self.lock:
                    check_cancel(cancel)
                    self.history += [{'who': '사람', 'text': text},
                                     {'who': 'KEVIN', 'text': turn['reply']}]
                    self.history = self.history[-20:]
        except Cancelled:
            pass
        except Exception:
            self._finish_error(job, '요청을 끝내지 못했어. 장치별 결과를 확인해 줘.')

    def _controller(self, job, plan, text='bridge'):
        cancel = job['cancel']
        self._ready(cancel)
        with self.lock:
            check_cancel(cancel)
            previous = self.c.planner
            self.c.planner = lambda _t, _r: dict(plan)
            try:
                request = self.c.command(text, self.c.live_configured)
            finally:
                self.c.planner = previous
            self._update(job, status='running', controller_job_id=request['job_id'], message='장치 응답을 기다리고 있어.')
        deadline = time.monotonic() + 120
        while True:
            check_cancel(cancel)
            state = self.c.get_state()
            if state.get('job_id') != request['job_id']:
                raise Cancelled()
            if state['status'] in ('complete', 'preview', 'error', 'unsupported', 'stopped'):
                break
            if time.monotonic() > deadline:
                with self.lock:
                    check_cancel(cancel)
                    self.c.stop()
                raise RuntimeError('controller timeout')
            cancel.wait(.05)
        results = dict(job['device_results'])
        if any(plan.get(k) not in (None, 'keep') for k in ('preset', 'snow', 'sky', 'speed')):
            results['led'] = state.get('transport')
        if plan.get('music') not in (None, 'keep') or plan.get('volume') is not None:
            results['radio'] = state.get('radio')
        self._update(job, device_results=results)
        if state['status'] == 'error':
            raise RuntimeError('controller failed')
        self._update(job, status='observing' if state['status'] == 'complete' else state['status'],
                     action_status=state['status'], message=('미리보기야. 기기는 움직이지 않았어.'
            if state['status'] == 'preview' else '기기 응답을 확인했어. 관찰 결과도 확인할게.'))

    def _ready(self, cancel):
        deadline = time.monotonic() + 8
        while self.c.get_state()['status'] == 'stopping':
            check_cancel(cancel)
            if time.monotonic() > deadline:
                raise RuntimeError('previous stop is unconfirmed')
            cancel.wait(.05)

    def _record(self, job, device, reply):
        check_cancel(job['cancel'])
        valid = ('acknowledged', 'clear_acknowledged') if device == 'led' else (
            'playing', 'stopped', 'stopped_child_terminated', 'volume_changed', 'paused')
        with self.lock:
            check_cancel(job['cancel'])
            job['device_results'][device] = copy.deepcopy(reply)
        if not isinstance(reply, dict) or reply.get('status') not in valid:
            raise RuntimeError('device did not acknowledge')
        with self.lock:
            check_cancel(job['cancel'])
            with self.c.lock:
                self.c.state['transport' if device == 'led' else 'radio'] = copy.deepcopy(reply)
                if device == 'led':
                    self.c.applied_scene = None
                elif reply['status'] in ('playing', 'stopped', 'stopped_child_terminated'):
                    self.c.applied_recipe['music'] = 'play' if reply['status'] == 'playing' else 'stop'
                    self.c.state['recipe'] = copy.deepcopy(self.c.applied_recipe)
        self._update(job, pending_device=None)

    def _act(self, job, turn):
        led, music, cancel = turn['led'], turn['music'], job['cancel']
        targets = []
        if led['mode'] != 'keep': targets.append('led')
        if music['mode'] != 'keep' or music['volume'] is not None: targets.append('radio')
        self._update(job, targets=targets)
        if not targets:
            self._update(job, status='complete', message=turn['reply'])
            return
        if not self.c.live_configured:
            self._update(job, status='preview', message=turn['reply'] + ' (미리보기 · 기기 실행 없음)')
            return
        config = self.c.config
        if ('led' in targets and not config.get('KEVIN_LED_ADDRESS', '').strip() or
                'radio' in targets and config.get('KEVIN_RADIO_DEVICE') != 'CRF-35B'):
            raise RuntimeError('requested device not configured')
        check_cancel(cancel)
        self._ready(cancel)
        if led['mode'] == 'scene':
            p = dict(mode='update', preset=led['preset'], speed=None, snow=led['snow'], sky=led['sky'],
                     music='keep', volume=None, explanation=turn['reply'])
            self._controller(job, p)
            self._update(job, status='running')
        elif led['mode'] in ('text', 'score', 'clear'):
            self._update(job, pending_device='led')
            if led['mode'] == 'text':
                animation = 'scroll_left' if led['animation'] == 'static' and len(led['text']) > 9 else led['animation']
                reply = self.c.transport.command('send_text', cancel, led['text'], animation=animation,
                    speed=70, color=led['color'].lstrip('#'), bg_color=led['bg'].lstrip('#') or None)
            elif led['mode'] == 'score':
                reply = self.c.transport.command('set_scores', cancel, led['score_left'], led['score_right'])
            else:
                reply = self.c.transport.clear(cancel)
            self._record(job, 'led', reply)
            with self.lock:
                check_cancel(cancel)
                with self.c.lock:
                    self.c.applied_scene = None
                    self.c.state['transport'] = copy.deepcopy(reply)
        if led['mode'] != 'keep':
            with self.lock:
                check_cancel(cancel)
                self.led_now = led['mode'] + (': ' + led['text'] if led['mode'] == 'text' else '')
        check_cancel(cancel)
        if 'radio' in targets:
            self._update(job, pending_device='radio')
        if music['mode'] in ('song', 'carol'):
            if music['mode'] == 'song':
                wav = song.melody_wav(music['notes'], music['tempo'])
                check_cancel(cancel)
                self.c.radio.close()
                check_cancel(cancel)
                path = self.c.radio.audio_path
                path.parent.mkdir(parents=True, exist_ok=True)
                temp = path.with_name('song-' + job['job_id'] + '.tmp')
                try:
                    temp.write_bytes(wav)
                    with self.lock:
                        check_cancel(cancel)
                        temp.replace(path)
                        self.c.audio_prepared = True
                finally:
                    temp.unlink(missing_ok=True)
            else:
                self.c.radio.close()
                self.c.audio_prepared = False
                self.c._prepare_audio(cancel)
            check_cancel(cancel)
            # Zero is mute, not an absent volume.
            volume = music['volume'] if music['volume'] is not None else 60
            reply = self.c.radio.play(volume, cancel)
            self._record(job, 'radio', reply)
        elif music['mode'] == 'stop':
            check_cancel(cancel)
            self._record(job, 'radio', self.c.radio.stop())
        elif music['volume'] is not None:
            self._record(job, 'radio', self.c.radio.set_volume(music['volume'], cancel))
        if 'radio' in targets:
            with self.lock:
                check_cancel(cancel)
                with self.c.lock:
                    self.c.state['radio'] = copy.deepcopy(job['device_results']['radio'])
                    if music['mode'] != 'keep':
                        self.c.applied_recipe['music'] = 'stop' if music['mode'] == 'stop' else 'play'
                    if music['volume'] is not None:
                        self.c.applied_recipe['volume'] = music['volume']
                    elif music['mode'] in ('song', 'carol'):
                        self.c.applied_recipe['volume'] = 60
                    self.c.state['recipe'] = copy.deepcopy(self.c.applied_recipe)
        self._update(job, status='observing', action_status='complete', message=turn['reply'])

    def _evidence(self, job, before, plan):
        if job['status'] != 'observing':
            return
        if before is None or not job['device_results']:
            self._update(job, status='complete', observation={'status': 'unavailable', 'reason': 'camera_unavailable', 'scope': 'visual_change_only'})
            return
        try:
            frames, stamp = [], before['captured_at']
            for _ in range(2):
                deadline = time.monotonic() + 1.5
                while True:
                    check_cancel(job['cancel'])
                    frame = self.eye.snapshot()
                    if frame and frame['captured_at'] > stamp:
                        frames.append(frame['image']); stamp = frame['captured_at']; break
                    if time.monotonic() >= deadline:
                        raise RuntimeError('fresh observation unavailable')
                    job['cancel'].wait(.05)
                if job['cancel'].wait(.25): raise Cancelled()
            check_cancel(job['cancel'])
            rec = make_receipt(device='kevin-web', action={'kind': job['kind'], 'job_id': job['job_id']},
                plan=plan, device_reply=job['device_results'], before=before['image'], after=frames,
                started=time.time(), out_dir=self.out_dir / 'receipts', signer=self.signer,
                source=before.get('source', 'camera'))
            folder = Path(rec['path'])
            import json
            stored = json.loads((folder / 'receipt.json').read_text())
            images = {name: (folder / name).read_bytes() for name in stored['evidence']}
            self._update(job, observation=dict(rec['observation'], status='available'),
                         receipt={'id': rec['id'], 'integrity_verified': check(stored, images),
                                  'url': '/evidence/' + rec['id'] + '/receipt.json'})
        except Cancelled:
            raise
        except Exception:
            self._update(job, observation={'status': 'unavailable', 'reason': 'fresh_evidence_unavailable', 'scope': 'visual_change_only'})
        self._update(job, status='complete')

    def stop(self):
        with self.lock:
            job = self._new('stop')
            job.update(status='stopping', message='중단을 요청했어. 장치 응답을 확인할게.')
            self._reset_absence()
            # The new stop operation replaces every planner/guardian token.
            self.c.stop()
            threading.Thread(target=self._stop_worker, args=(job,), daemon=True).start()
            return dict(job_id=job['job_id'], status='stopping', reply=job['message'])

    def _stop_worker(self, job):
        try:
            deadline = time.monotonic() + 100
            while True:
                check_cancel(job['cancel'])
                state = self.c.get_state()
                if state['status'] not in ('stopping',):
                    break
                if time.monotonic() > deadline:
                    raise RuntimeError('stop timeout')
                job['cancel'].wait(.05)
            failed = state['status'] != 'stopped'
            self._update(job, status='failed' if failed else 'stopped',
                device_results={'led': state.get('transport'), 'radio': state.get('radio')},
                message=state.get('message') or ('정지 응답을 확인하지 못했어.' if failed else '정지 응답을 확인했어.'))
        except Cancelled:
            pass
        except Exception:
            self._finish_error(job, '정지 응답을 확인하지 못했어. 장치를 확인해 줘.')

    def set_mode(self, mode):
        if mode not in ('talk', 'home', 'away'):
            raise ValueError('invalid mode')
        with self.lock:
            self.mode, self.guard_epoch = mode, self.guard_epoch + 1
            self._reset_absence()
            if self.active and self.jobs[self.active]['kind'] == 'guardian' and self.jobs[self.active]['status'] not in FINAL:
                self.stop()
        return {'mode': mode}

    def guard(self, body):
        mode = body.get('mode')
        with self.lock:
            if mode != self.mode or mode not in ('home', 'away'):
                return {'action': 'none', 'reply': ''}
            if self.active and self.jobs[self.active]['status'] not in FINAL:
                return {'action': 'none', 'reply': ''}
            epoch = self.guard_epoch
            if mode == 'home':
                state = self.c.get_state()
                radio_off = (state.get('radio') or {}).get('status') in ('not_started', 'stopped', 'stopped_child_terminated')
                led_off = (state.get('transport') or {}).get('status') in ('not_sent', 'clear_acknowledged')
                if radio_off and led_off:
                    self._reset_absence()
                    return {'action': 'none', 'reply': ''}
        frame = self.eye.snapshot()
        if frame is None:
            with self.lock: self._reset_absence()
            return {'action': 'unavailable', 'reply': '새 카메라 영상이 없어 자동 동작을 보류했어.'}
        if not self.vision_lock.acquire(blocking=False):
            return {'action': 'none', 'reply': ''}
        try:
            try:
                person, what = self.vision_fn(base64.b64encode(frame['image']).decode())
            except Exception:
                person, what = None, ''
        finally:
            self.vision_lock.release()
        with self.lock:
            if epoch != self.guard_epoch or mode != self.mode:
                return {'action': 'none', 'reply': ''}
            latest = self.eye.snapshot()
            if latest is None or self.clock() - frame['captured_at'] > 5:
                self._reset_absence()
                return {'action': 'unavailable', 'reply': '관찰이 오래돼 자동 동작을 보류했어.'}
            if type(person) is not bool:
                self._reset_absence()
                return {'action': 'unavailable', 'reply': '사람이 있는지 확인하지 못해 자동 동작을 보류했어.'}
            now = self.clock()
            if mode == 'home':
                if person:
                    self._reset_absence()
                    return {'action': 'none', 'seen': what, 'presence': 'present'}
                if frame['captured_at'] == self.last_frame:
                    return {'action': 'none', 'presence': 'unknown'}
                if self.last_absence is None or now - self.last_absence > 12:
                    self.absent_since, self.absence_count = now, 0
                self.last_frame, self.last_absence = frame['captured_at'], now
                self.absence_count += 1
                duration = now - self.absent_since
                if duration < self.idle_limit or self.absence_count < 2:
                    return {'action': 'none', 'presence': 'absent', 'absence_seconds': round(duration), 'seen': what}
                # A fresh series of confirmed absence is required after every stop.
                result = self.stop()
                return dict(result, action='power_save', reply='사람이 없는 상태를 확인해서 중단을 요청했어.')
            if not person or now < self.alarm_until:
                return {'action': 'none', 'seen': what}
            self.alarm_until = now + 60
            job = self._new('guardian')
            threading.Thread(target=self._guard_worker, args=(job, frame), daemon=True).start()
            return dict(action='intruder_alert', job_id=job['job_id'], status='planning',
                        reply='사람이 보여서 경보를 요청했어.', seen=what)

    def _guard_worker(self, job, before):
        try:
            with self.execution:
                check_cancel(job['cancel'])
                if not self.c.live_configured:
                    self._update(job, status='preview', message='경보 미리보기야. 기기는 움직이지 않았어.')
                    return
                self._update(job, status='running', message='경보 장치 응답을 기다리고 있어.')
                self._ready(job['cancel'])
                if (not self.c.config.get('KEVIN_LED_ADDRESS', '').strip() or
                        self.c.config.get('KEVIN_RADIO_DEVICE') != 'CRF-35B'):
                    raise RuntimeError('alarm devices not configured')
                # Both effects participate in the same bridge cancellation token.
                self._update(job, pending_device='led')
                self._record(job, 'led', self.c.transport.send(guardian.intruder_scene(), job['cancel']))
                check_cancel(job['cancel'])
                self._update(job, pending_device='radio')
                self.c._prepare_audio(job['cancel'])
                check_cancel(job['cancel'])
                self._record(job, 'radio', self.c.radio.play(100, job['cancel']))
                self._update(job, status='observing', action_status='complete', message='경보 장치 응답을 확인했어. 실제 신고 기능은 없어.')
                self._evidence(job, before, {'mode': 'away'})
        except Cancelled:
            pass
        except Exception:
            self._finish_error(job, '경보 장치 실행을 끝내지 못했어. 장치별 결과를 확인해 줘.')
