"""KEVIN talks back out loud with the Windows built-in voice (offline).

One persistent PowerShell child reads one line per utterance. A new line
cuts off the previous one so KEVIN never lags behind the conversation.
Elsewhere (or with KEVIN_VOICE=off) it stays silent.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import threading

_SCRIPT = r"""
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$ko = $s.GetInstalledVoices() | Where-Object { $_.VoiceInfo.Culture.Name -eq 'ko-KR' } | Select-Object -First 1
if ($ko) { $s.SelectVoice($ko.VoiceInfo.Name) }
$s.Rate = 1
[Console]::InputEncoding = New-Object System.Text.UTF8Encoding($false)
while ($null -ne ($line = [Console]::ReadLine())) {
    $s.SpeakAsyncCancelAll()
    if ($line.Trim()) { [void]$s.SpeakAsync($line) }
}
"""


def _clean(text: str) -> str:
    text = re.sub(r'\([^)]*\)', '', text)       # drop stage notes like (미리보기)
    return re.sub(r'[\r\n]+', ' ', text).strip()


class Voice:
    def __init__(self, enabled: bool | None = None):
        if enabled is None:
            enabled = sys.platform == 'win32' and os.getenv('KEVIN_VOICE', 'on') != 'off'
        self.enabled = enabled
        self.process = None
        self.lock = threading.Lock()
        self.last = None

    def say(self, text: str) -> None:
        text = _clean(text or '')
        if not self.enabled or not text or text == self.last:
            return
        self.last = text
        with self.lock:
            try:
                if self.process is None or self.process.poll() is not None:
                    self.process = subprocess.Popen(
                        ['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', _SCRIPT],
                        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        text=True, encoding='utf-8', bufsize=1)
                self.process.stdin.write(text + '\n')
                self.process.stdin.flush()
            except OSError:
                self.enabled = False

    def close(self) -> None:
        with self.lock:
            if self.process and self.process.poll() is None:
                try:
                    self.process.stdin.close()
                    self.process.wait(timeout=2)
                except Exception:
                    self.process.kill()
