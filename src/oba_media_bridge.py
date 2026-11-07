"""Local OBA media preparation; only explicit play-audio touches an audio device."""

import argparse
import base64
import io
import json
import math
from pathlib import Path
import struct
import sys
import wave

from PIL import Image, ImageDraw, ImageSequence

import pixels


PREVIEW_SCALE = 6
MAX_SOURCE_BYTES = 4_000_000


def _frame_pixels(image):
    rgba = image.convert('RGBA')
    background = Image.new('RGBA', rgba.size, (0, 0, 0, 255))
    background.alpha_composite(rgba)
    rgb = background.convert('RGB').resize(
        (pixels.WIDTH, pixels.HEIGHT), Image.Resampling.NEAREST)
    data = rgb.load()
    return [['#%02x%02x%02x' % data[x, y] for x in range(pixels.WIDTH)]
            for y in range(pixels.HEIGHT)]


def import_media(source):
    """Decode PNG or looping GIF without discarding frames or changing timing."""
    with Path(source).open('rb') as stream:
        raw = stream.read(MAX_SOURCE_BYTES + 1)
    if len(raw) > MAX_SOURCE_BYTES:
        raise pixels.PixelError('source exceeds the 4MB Terminator input limit')
    with Image.open(io.BytesIO(raw)) as image:
        if image.format not in ('PNG', 'GIF'):
            raise pixels.PixelError('only PNG and GIF are supported')
        if image.width * image.height > 4_000_000:
            raise pixels.PixelError('source exceeds the 4MP Terminator input limit')
        if not 1 <= image.n_frames <= 32:
            raise pixels.PixelError('1–32 frames required; no frames are discarded')
        if image.format == 'PNG' and image.n_frames != 1:
            raise pixels.PixelError('animated PNG is unsupported; provide a GIF')
        if image.format == 'GIF' and image.info.get('loop') != 0:
            raise pixels.PixelError('GIF must loop forever (loop=0) for terminator-scene v1')
        frames, durations = [], []
        for frame in ImageSequence.Iterator(image):
            if image.format == 'GIF':
                duration = frame.info.get('duration')
                if type(duration) is not int or not 40 <= duration <= 5000:
                    raise pixels.PixelError('each GIF delay must be explicit and 40–5000 ms')
                durations.append(duration)
            frames.append(_frame_pixels(frame))
        if durations and len(set(durations)) != 1:
            raise pixels.PixelError('variable frame timing cannot fit terminator-scene v1')
    return pixels.validate({'frames': frames, 'duration': durations[0] if durations else 200})


def santa_scene():
    """An original procedural Santa/sleigh/reindeer preset, not AI generation."""
    frames = []
    for index in range(32):
        image = Image.new('RGB', (pixels.WIDTH, pixels.HEIGHT), '#061329')
        draw = ImageDraw.Draw(image)
        for x, y in ((8, 2), (29, 5), (52, 1), (76, 4), (103, 2), (130, 5)):
            draw.point((x, y), fill='#6e94ba')
        draw.rectangle((0, 15, 143, 15), fill='#c4e5ff')
        # Whole sprite crosses from left clipping through right clipping in one cycle.
        x = -32 + round(index * 168 / 31)
        y = (0, 1, 1, 0)[index % 4]
        draw.rectangle((x + 1, y + 6, x + 5, y + 10), fill='#b48b32')  # gift sack
        draw.rectangle((x + 7, y + 2, x + 10, y + 3), fill='#f43b3b')  # hat
        draw.point((x + 6, y + 3), fill='#ffffff')
        draw.rectangle((x + 7, y + 4, x + 10, y + 5), fill='#ffd0a0')  # face
        draw.point((x + 10, y + 4), fill='#000000')
        draw.rectangle((x + 7, y + 6, x + 9, y + 6), fill='#ffffff')  # beard
        draw.rectangle((x + 6, y + 7, x + 10, y + 10), fill='#f43b3b')
        draw.line((x + 10, y + 8, x + 14, y + 7), fill='#ffd0a0')
        draw.rectangle((x, y + 11, x + 14, y + 12), fill='#a7223c')  # sleigh
        draw.line((x + 14, y + 12, x + 16, y + 9), fill='#a7223c', width=2)
        draw.line((x + 2, y + 14, x + 16, y + 14, x + 18, y + 12), fill='#ffd15c')
        draw.line((x + 14, y + 7, x + 23, y + 8), fill='#ffd15c')  # reins
        draw.rectangle((x + 23, y + 7, x + 29, y + 9), fill='#b97b45')
        draw.line((x + 29, y + 8, x + 31, y + 5), fill='#b97b45', width=2)
        draw.rectangle((x + 30, y + 4, x + 33, y + 5), fill='#b97b45')
        draw.point((x + 34, y + 5), fill='#ff633d')
        draw.line((x + 30, y + 3, x + 29, y + 1, x + 28, y + 2), fill='#ffd15c')
        stride = index % 2
        draw.line((x + 24, y + 10, x + 22 + stride * 3, y + 12), fill='#b97b45')
        draw.line((x + 28, y + 10, x + 30 - stride * 3, y + 12), fill='#b97b45')
        frames.append(_frame_pixels(image))
    return pixels.validate({'frames': frames, 'duration': 120})


def _checked_gif(scene):
    scene = pixels.validate(scene)
    if scene['duration'] % 10:
        raise pixels.PixelError('GIF requires exact 10 ms ticks; timing would be truncated')
    encoded = base64.b64decode(pixels.export_image(scene, 'gif'))
    with Image.open(io.BytesIO(encoded)) as image:
        if image.n_frames != len(scene['frames']):
            raise pixels.PixelError('GIF export merged identical frames; frame count would change')
        for index, frame in enumerate(ImageSequence.Iterator(image)):
            if frame.info.get('duration') != scene['duration']:
                raise pixels.PixelError('GIF export changed frame timing')
            if _frame_pixels(frame) != scene['frames'][index]:
                raise pixels.PixelError('GIF export changed colors; reduce the source palette')
    return encoded


def original_wav(duration_ms):
    """Synthesize a short original bell pattern as mono 16-bit 22050 Hz PCM."""
    rate = 22050
    notes = (523.25, 783.99, 659.25, 987.77, 587.33, 880.00, 698.46, 1046.50)
    count = round(rate * duration_ms / 1000)
    samples = bytearray()
    for index in range(count):
        time = index / rate
        beat = time / 0.48
        age = (beat % 1) * 0.48
        frequency = notes[int(beat) % len(notes)]
        attack = min(1, age / 0.008)
        release = min(1, (duration_ms / 1000 - time) / 0.04)
        tone = (math.sin(math.tau * frequency * age)
                + 0.30 * math.sin(math.tau * frequency * 2.76 * age))
        value = int(8500 * tone * attack * math.exp(-8 * age) * release)
        samples.extend(struct.pack('<h', value))
    output = io.BytesIO()
    with wave.open(output, 'wb') as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(rate)
        audio.writeframes(samples)
    return output.getvalue()


def export_bundle(scene, outdir, *, santa=False):
    """Validate all bytes before creating a fresh folder; never overwrite a bundle."""
    scene = pixels.validate(scene)
    gif = _checked_gif(scene)
    preview_frames = []
    with Image.open(io.BytesIO(gif)) as image:
        for frame in ImageSequence.Iterator(image):
            preview_frames.append(frame.convert('RGB').resize(
                (pixels.WIDTH * PREVIEW_SCALE, pixels.HEIGHT * PREVIEW_SCALE),
                Image.Resampling.NEAREST))
    preview = io.BytesIO()
    preview_frames[0].save(
        preview, format='GIF', save_all=True, append_images=preview_frames[1:],
        duration=scene['duration'], loop=0, disposal=2)
    project = {'format': 'terminator-scene', 'version': 1, 'scene': scene}
    assets = {
        'project.json': (json.dumps(project, separators=(',', ':')) + '\n').encode('utf-8'),
        'santa.gif' if santa else 'display.gif': gif,
        'preview.gif': preview.getvalue(),
    }
    if santa:
        assets['santa.wav'] = original_wav(len(scene['frames']) * scene['duration'])
    output = Path(outdir)
    output.mkdir(parents=True, exist_ok=False)
    for name, data in assets.items():
        (output / name).write_bytes(data)
    return {
        'output': str(output.resolve()), 'files': list(assets),
        'frames': len(scene['frames']), 'duration_ms': scene['duration'],
        'cycle_ms': len(scene['frames']) * scene['duration'],
        'physical_verified': False,
        'interpretation': 'procedural preset asset; future AI orchestration' if santa
                          else 'local media conversion; no AI inference',
    }


def play_audio(path):
    """Explicit blocking Windows playback through the OS default output."""
    if sys.platform != 'win32':
        raise RuntimeError('play-audio requires Windows winsound')
    path = Path(path).resolve()
    with wave.open(str(path), 'rb') as audio:
        if (audio.getcomptype() != 'NONE' or audio.getsampwidth() != 2
                or audio.getnchannels() not in (1, 2)):
            raise ValueError('play-audio requires a mono/stereo 16-bit PCM WAV')
    import winsound
    winsound.PlaySound(str(path), winsound.SND_FILENAME | winsound.SND_NODEFAULT)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    santa = sub.add_parser('santa', help='write a procedural animation and original WAV')
    santa.add_argument('outdir', type=Path)
    importer = sub.add_parser('import', help='convert PNG/GIF into an editable Terminator bundle')
    importer.add_argument('source', type=Path)
    importer.add_argument('outdir', type=Path)
    player = sub.add_parser('play-audio', help='explicitly play WAV on Windows default output')
    player.add_argument('wav', type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == 'santa':
            result = export_bundle(santa_scene(), args.outdir, santa=True)
        elif args.command == 'import':
            result = export_bundle(import_media(args.source), args.outdir)
        else:
            play_audio(args.wav)
            result = {'played': str(args.wav), 'output': 'Windows default audio output'}
    except (ValueError, OSError, RuntimeError, wave.Error) as error:
        parser.exit(1, f'{type(error).__name__}: {error}\n')
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


if __name__ == '__main__':
    main()
