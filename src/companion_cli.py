"""A color game inside the terminal. No browser or graphical window is opened."""
import argparse
import base64
import codecs
from collections import deque
from contextlib import contextmanager
import io
import json
import os
from pathlib import Path
import queue
import shutil
import sys
import threading
import time
import wave

from terminal_art import TerminalArt, image_to_ansi, sanitize_terminal_text

_keys = deque()
_decoder = codecs.getincrementaldecoder('utf-8')('replace')


@contextmanager
def terminal_session():
    old = None
    if os.name == 'nt':
        import ctypes
        kernel = ctypes.windll.kernel32
        handle = kernel.GetStdHandle(-11)
        mode = ctypes.c_ulong()
        if not kernel.GetConsoleMode(handle, ctypes.byref(mode)):
            raise RuntimeError('실제 Windows 터미널에서 실행해 주세요.')
        old = mode.value
        if not kernel.SetConsoleMode(handle, old | 4):
            raise RuntimeError('컬러 터미널 모드를 켜지 못했습니다.')
        sys.stdout.reconfigure(encoding='utf-8')
    else:
        import termios
        import tty
        old = termios.tcgetattr(sys.stdin)
        tty.setcbreak(sys.stdin.fileno())
        active = termios.tcgetattr(sys.stdin)
        active[3] &= ~(termios.ISIG | termios.IEXTEN)
        termios.tcsetattr(sys.stdin, termios.TCSANOW, active)
    try:
        sys.stdout.write('\x1b[?1049h\x1b[?25l\x1b[?7l\x1b[2J')
        sys.stdout.flush()
        yield
    finally:
        try:
            sys.stdout.write('\x1b[0m\x1b[?7h\x1b[?25h\x1b[?1049l')
            sys.stdout.flush()
        finally:
            if os.name == 'nt':
                kernel.SetConsoleMode(handle, old)
            else:
                termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old)


def read_key():
    if os.name == 'nt':
        import msvcrt
        if not msvcrt.kbhit():
            return None
        char = msvcrt.getwch()
        if char in ('\x00', '\xe0'):
            msvcrt.getwch()
            return None
        return char
    import select
    if _keys:
        return _keys.popleft()
    if not select.select([sys.stdin], [], [], 0)[0]:
        return None
    _keys.extend(_decoder.decode(os.read(sys.stdin.fileno(), 4096)))
    return _keys.popleft() if _keys else None


class Backend:
    """In-process show controller. Real devices are touched only with --live."""

    def __init__(self, live=False):
        from show_control import ShowController
        config = dict(os.environ)
        config['KEVIN_SHOW_LIVE'] = '1' if live else '0'
        self.live = live
        self.local = ShowController(config=config)

    def request(self, action, body=None):
        if action == 'state':
            return self.local.get_state()
        if action == 'command':
            return self.local.command(body['prompt'], body['execute'] and self.live)
        if action == 'stop':
            return self.local.stop()
        if action == 'transcribe':
            return self.local.transcribe(body['audio'], body['mime'])
        raise ValueError(action)

    def close(self):
        self.local.close()


class Game:
    def __init__(self, backend):
        self.backend = backend
        self.art = TerminalArt()
        self.mode = 'room'
        self.message = 'Space: 말 걸기'
        self.prompt = ''
        self.live = False
        self.state = {}
        self.target = 'wander'
        self.epoch = 0
        self.pending = False
        self.results = queue.Queue()
        self.capture_cancel = threading.Event()
        self.poll_at = 0
        self.job = None
        self.waiting = False
        self.request_pending = False
        self.executing_live = False
        self.mutations = queue.Queue()
        threading.Thread(target=self._mutate, daemon=True).start()

    def _mutate(self):
        while True:
            epoch, kind, action, body = self.mutations.get()
            if action == 'command' and epoch != self.epoch:
                continue
            self._request(epoch, kind, action, body)

    def _request(self, epoch, kind, action, body):
        try:
            self.results.put((epoch, kind, self.backend.request(action, body), None))
        except Exception:
            self.results.put((epoch, kind, None, '연결·모델·설정을 확인해 주세요. 완료 여부는 미확인입니다.'))

    def worker(self, kind, action, body=None):
        epoch = self.epoch
        if action in ('command', 'stop'):
            self.mutations.put((epoch, kind, action, body))
        else:
            threading.Thread(target=self._request, args=(epoch, kind, action, body), daemon=True).start()

    def submit(self, prompt):
        if self.waiting or not prompt.strip():
            return
        execute = self.live and self.state.get('live_configured') is True
        self.executing_live = execute
        self.epoch += 1
        self.pending = False
        self.job = None
        self.request_pending = True
        self.live = False
        self.waiting = True
        self.mode = 'planning'
        self.message = '실물 요청 처리 중. 실제 결과 확인은 별도입니다.' if execute else '미리보기 해석 중. 기기는 조작하지 않습니다.'
        self.worker('command', 'command', {'prompt': prompt.strip(), 'execute': execute})
        self.prompt = ''

    def stop(self):
        self.epoch += 1
        self.capture_cancel.set()
        self.pending = False
        self.waiting = True
        self.request_pending = True
        self.live = False
        self.job = None
        self.mode = 'stopped'
        self.message = '전체 정지 요청 중.'
        self.worker('stop', 'stop', {})

    def record(self):
        if self.waiting:
            return
        if not self.state.get('voice_configured'):
            self.message = '기존 허브의 음성 설정이 필요합니다. 텍스트로 요청할 수 있습니다.'
            return
        try:
            import sounddevice as sd
        except ImportError:
            self.message = '마이크 입력에는 sounddevice 설치가 필요합니다. 텍스트 입력은 가능합니다.'
            return
        epoch = self.epoch
        cancel = self.capture_cancel = threading.Event()
        self.mode = 'listening'
        self.waiting = True
        self.message = '6초간 녹음합니다. Esc로 취소·정지.'
        def capture():
            try:
                chunks = []
                with sd.RawInputStream(samplerate=16000, channels=1, dtype='int16') as stream:
                    for _ in range(60):
                        if cancel.is_set():
                            return
                        data, overflow = stream.read(1600)
                        if overflow:
                            raise ValueError('audio overflow')
                        chunks.append(bytes(data))
                if cancel.is_set():
                    return
                buffer = io.BytesIO()
                with wave.open(buffer, 'wb') as audio:
                    audio.setnchannels(1)
                    audio.setsampwidth(2)
                    audio.setframerate(16000)
                    audio.writeframes(b''.join(chunks))
                result = self.backend.request('transcribe', {
                    'audio': base64.b64encode(buffer.getvalue()).decode(), 'mime': 'audio/wav'})
                self.results.put((epoch, 'speech', result, None))
            except Exception:
                self.results.put((epoch, 'speech', None, '녹음·음성 변환 실패. 마이크와 허브 설정을 확인하세요.'))
        threading.Thread(target=capture, daemon=True).start()

    def update(self):
        while True:
            try:
                epoch, kind, result, error = self.results.get_nowait()
            except queue.Empty:
                break
            if epoch != self.epoch:
                continue
            if kind == 'state':
                self.pending = False
            if error:
                self.request_pending = False
                self.mode, self.message, self.waiting = 'error', error, False
                continue
            if kind in ('command', 'stop'):
                self.request_pending = False
                self.job = result.get('job_id')
                self.poll_at = 0
            elif kind == 'speech':
                self.waiting = False
                if not result.get('text', '').strip():
                    self.mode, self.message = 'talk', '인식된 말이 없습니다. 다시 말해 주세요.'
                else:
                    self.submit(result['text'])
            elif kind == 'state':
                self.state = result
                status = result.get('status')
                if self.job and result.get('job_id') != self.job:
                    self.job = None
                    self.mode, self.message, self.waiting = 'error', '허브의 작업이 바뀌었습니다. 상태를 확인하세요.', False
                elif self.waiting and self.mode != 'listening':
                    self.message = result.get('message', status or '상태 확인 중')
                    if status in ('sending', 'audio'):
                        self.mode = 'acting'
                        self.target = 'led' if status == 'sending' else 'radio'
                    elif status in ('queued', 'planning', 'rendering', 'stopping'):
                        pass
                    else:
                        self.waiting = False
                        self.job = None
                        self.mode = 'error' if status in ('error', 'unsupported') else 'stopped' if status == 'stopped' else 'room'
                        if status == 'stopped':
                            self.executing_live = False
        if not self.pending and not self.request_pending and time.monotonic() >= self.poll_at:
            self.pending = True
            self.poll_at = time.monotonic() + .7
            self.worker('state', 'state')

    def key(self, char):
        if char in ('\x1b', '\x18'):
            self.stop()
        elif char == '\x16':
            self.record()
        elif char == '\x0c' and not self.waiting:
            self.live = not self.live and self.state.get('live_configured') is True
        elif self.mode == 'room' and char == ' ':
            self.mode, self.message = 'talk', '듣고 있어요. 요청을 입력하거나 Ctrl+V로 녹음해 주세요.'
        elif not self.waiting and self.mode != 'room':
            if char in ('\r', '\n'):
                self.submit(self.prompt)
            elif char in ('\x08', '\x7f'):
                self.prompt = self.prompt[:-1]
            elif char.isprintable() and len(self.prompt) < 2000:
                self.prompt += char

    def frame(self, cols, rows, phase):
        width = max(20, min(cols - 1, 180))
        graphic_rows = max(4, rows - 7)
        art = self.art.compose(self.mode, width, graphic_rows * 2, phase=phase, target=self.target)
        safe = lambda text: sanitize_terminal_text(text, width - 2)
        lines = [safe('KEVIN  /  HOME ALONE, BUT SMART     ' + ('LIVE · 실물 확인 별도' if self.live or self.executing_live else 'PREVIEW · 이번 요청')),
                 '+' + '-' * (width - 2) + '+', safe(self.message),
                 safe('> ' + self.prompt), safe('Space 대화 | Enter 요청 | Esc 정지 | Ctrl+V 음성 | Ctrl+L 실물 선택 | Ctrl+C 종료')]
        return '\x1b[H' + image_to_ansi(art) + '\x1b[0m' + ''.join(
            f'\x1b[{graphic_rows + i + 1};1H\x1b[2K\x1b[97;44m{line}\x1b[0m'
            for i, line in enumerate(lines))


def main(argv=None):
    parser = argparse.ArgumentParser(description='터미널 안에서 실행되는 컬러 AI 게임')
    parser.add_argument('--live', action='store_true',
                        help='실제 LED 전광판과 블루투스 라디오로 내보냅니다. 없으면 미리보기만 합니다.')
    args = parser.parse_args(argv)
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        parser.error('실제 터미널에서 실행해 주세요. 브라우저는 사용하지 않습니다.')
    backend = Backend(args.live)
    game = Game(backend)
    try:
        with terminal_session():
            started = time.monotonic()
            while True:
                char = read_key()
                if char == '\x03':
                    break
                if char:
                    game.key(char)
                game.update()
                size = shutil.get_terminal_size((120, 40))
                sys.stdout.write(game.frame(size.columns, size.lines, time.monotonic() - started))
                sys.stdout.flush()
                time.sleep(.16)
    except KeyboardInterrupt:
        pass
    finally:
        game.epoch += 1
        game.capture_cancel.set()
        backend.close()


if __name__ == '__main__':
    main()
