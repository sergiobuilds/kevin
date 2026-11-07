#!/usr/bin/env python3
"""Optional authenticated local ASR helper. No audio/transcript request logging."""
import argparse
import hmac
import io
import json
import os
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading

MAX_AUDIO_BYTES = 2 * 1024 * 1024
MAX_SECONDS = 60
MIMES = {"audio/webm", "audio/wav", "audio/x-wav", "audio/ogg", "audio/mp4", "audio/mpeg", "audio/flac", "audio/x-flac"}
DEFAULT_MODEL = Path(__file__).resolve().parents[1] / "work/voice-runtime/models/faster-whisper-base"


class AudioTooLong(ValueError):
    pass


def decode_bounded(audio):
    """PyAV decode with a 60-second decoded-sample bound, entirely in memory."""
    import av
    import numpy as np

    resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
    chunks = []
    count = 0

    def append(frames):
        nonlocal count
        for frame in frames:
            chunk = frame.to_ndarray().reshape(-1)
            count += chunk.size
            if count > 16000 * MAX_SECONDS:
                raise AudioTooLong()
            chunks.append(chunk)

    with av.open(io.BytesIO(audio)) as container:
        for frame in container.decode(audio=0):
            append(resampler.resample(frame))
        append(resampler.resample(None))
    if not count:
        raise ValueError("empty audio")
    return np.concatenate(chunks).astype(np.float32) / 32768.0


def recognize(model, audio):
    samples = decode_bounded(audio)
    segments, info = model.transcribe(
        samples, language="ko", beam_size=5, vad_filter=True,
        condition_on_previous_text=False, word_timestamps=False,
    )
    # Iteration is required: faster-whisper defers model inference to the generator.
    text = " ".join(segment.text.strip() for segment in segments).strip()
    return {"text": text, "language": info.language}


def make_server(host, port, token, model):
    if not isinstance(token, str) or not token.strip() or any(c in token for c in "\r\n"):
        raise ValueError("ASR_TOKEN must be set")
    gate = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def setup(self):
            super().setup()
            self.connection.settimeout(15)

        def reply(self, code, value):
            raw = json.dumps(value, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(raw)
            self.close_connection = True

        def do_POST(self):
            if self.path != "/transcribe":
                self.reply(404, {"error": "not found"})
                return
            actual = self.headers.get("Authorization", "").encode()
            if not hmac.compare_digest(actual, ("Bearer " + token).encode()):
                self.reply(401, {"error": "authentication required"})
                return
            if self.headers.get("Transfer-Encoding"):
                self.reply(400, {"error": "Content-Length required; chunked requests unsupported"})
                return
            lengths = self.headers.get_all("Content-Length", [])
            if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit():
                self.reply(411, {"error": "Content-Length required"})
                return
            if len(lengths[0]) > 10:
                self.reply(413, {"error": "audio exceeds 2 MiB"})
                return
            length = int(lengths[0])
            if not 0 < length <= MAX_AUDIO_BYTES:
                self.reply(413, {"error": "audio must be 1 byte through 2 MiB"})
                return
            mime = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if mime not in MIMES:
                self.reply(415, {"error": "unsupported audio format"})
                return
            if not gate.acquire(blocking=False):
                self.reply(503, {"error": "speech service busy"})
                return
            try:
                audio = self.rfile.read(length)
                if len(audio) != length:
                    self.reply(400, {"error": "incomplete audio"})
                    return
                result = recognize(model, audio)
                self.reply(200, result)
            except AudioTooLong:
                self.reply(413, {"error": "audio duration exceeds 60 seconds"})
            except (TimeoutError, OSError):
                self.reply(422, {"error": "audio read or decode failed"})
            except Exception:
                # Exception details may contain private input or filesystem paths.
                self.reply(422, {"error": "audio decode or transcription failed"})
            finally:
                gate.release()

    return ThreadingHTTPServer((host, port), Handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--model", type=Path, default=DEFAULT_MODEL)
    args = parser.parse_args()
    token = os.getenv("ASR_TOKEN", "")
    if not token.strip() or any(c in token for c in "\r\n"):
        parser.error("Set ASR_TOKEN in the environment; do not pass a token on the command line.")
    if not args.model.is_dir() or not (args.model / "model.bin").is_file():
        parser.error("Local base model missing; see docs/unified-show-voice.md.")
    from faster_whisper import WhisperModel
    model = WhisperModel(str(args.model.resolve()), device="cpu", compute_type="int8", cpu_threads=4, num_workers=1, local_files_only=True)
    server = make_server(args.host, args.port, token, model)
    print("Local ASR ready (CPU/int8, 4 threads; no request content logging).", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
