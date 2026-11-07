"""Screenshot-adapted Christmas train scene and existing synthesized carol."""

import argparse
import io
import json
import math
from pathlib import Path
import struct
import wave

from PIL import Image, ImageDraw

import oba_media_bridge as bridge
import pixels


FRAME_COUNT = 32
FRAME_MS = 120
SAMPLE_RATE = 22050
BEAT_SECONDS = 0.375
# Traditional Jingle Bells chorus; this synthesis/voicing is an original arrangement.
CHORUS = (
    ('E', 1), ('E', 1), ('E', 2), ('E', 1), ('E', 1), ('E', 2),
    ('E', 1), ('G', 1), ('C', 1.5), ('D', .5), ('E', 4),
    ('F', 1), ('F', 1), ('F', 1.5), ('F', .5), ('F', 1), ('E', 1),
    ('E', 1), ('E', .5), ('E', .5), ('E', 1), ('D', 1), ('D', 1),
    ('E', 1), ('D', 2), ('G', 2),
    ('E', 1), ('E', 1), ('E', 2), ('E', 1), ('E', 1), ('E', 2),
    ('E', 1), ('G', 1), ('C', 1.5), ('D', .5), ('E', 4),
    ('F', 1), ('F', 1), ('F', 1.5), ('F', .5), ('F', 1), ('E', 1),
    ('E', 1), ('E', .5), ('E', .5), ('G', 1), ('G', 1), ('F', 1),
    ('D', 1), ('C', 4),
)
REFERENCE_URL = 'https://www.instagram.com/reel/Dd8vXumP7DU/'
REFERENCE_SCREENSHOT = 'user-supplied screenshot (not included)'
WHITE = '#fff6e6'
GOLD = '#ffd75c'
RED = '#ef3045'
GREEN = '#19b85b'
SNOW = '#dceaff'


def _tree(draw, center):
    """A 13-pixel decorated tree beneath its star, kept large for this strip."""
    draw.rectangle((center - 1, 12, center + 1, 14), fill='#a05c37')
    for top, bottom, halfwidth in ((3, 6, 3), (5, 9, 5), (7, 12, 8)):
        draw.polygon(((center, top), (center - halfwidth, bottom),
                      (center + halfwidth, bottom)), fill='#087b45')
        draw.line((center, top + 1, center - halfwidth + 1, bottom), fill=GREEN)
    for x, y in ((0, 4), (-2, 6), (2, 7), (-4, 9), (0, 9), (5, 11), (-6, 11)):
        draw.point((center + x, y), fill=RED)
    for x, y in ((0, 5), (1, 6), (-2, 8), (-1, 8), (2, 9), (3, 10),
                 (-5, 11), (-2, 11), (0, 12), (4, 12)):
        draw.point((center + x, y), fill=GOLD)
    for x, y in ((0, 3), (-3, 7), (3, 8), (-5, 10), (6, 12)):
        draw.point((center + x, y), fill=WHITE)
    draw.line((center - 1, 1, center + 1, 1), fill=GOLD)
    draw.line((center, 0, center, 2), fill=GOLD)
    draw.point((center, 1), fill=WHITE)


def _gift(draw, x, top, width, color, ribbon):
    draw.rectangle((x, top, x + width - 1, 14), fill=color)
    draw.line((x, top + 2, x + width - 1, top + 2), fill=ribbon)
    mid = x + width // 2
    draw.line((mid, top, mid, 14), fill=ribbon)
    draw.line((mid, top - 1, mid - 2, top - 2, mid - 2, top - 1,
               mid + 2, top - 1, mid + 2, top - 2, mid, top - 1), fill=ribbon)
    draw.line((x, top, x + width - 1, top), fill=WHITE)


def _train(draw, x, index):
    """Red locomotive, green passenger carriage, snowy roofs and turning wheels."""
    # Carriage and coupling; the warm window sequence remains readable at 1:1.
    draw.rectangle((x, 10, x + 19, 13), fill=GREEN)
    draw.line((x, 10, x + 19, 10), fill=RED)
    draw.line((x - 1, 9, x + 20, 9), fill=SNOW)
    for window in (2, 6, 10, 14):
        draw.rectangle((x + window, 11, x + window + 1, 12), fill=GOLD)
        draw.point((x + window, 11), fill=WHITE)
    draw.line((x + 19, 13, x + 24, 13), fill=GOLD)
    # Tall cab at the back of the red boiler and a visible chimney at the front.
    draw.rectangle((x + 24, 8, x + 32, 12), fill=RED)
    draw.line((x + 23, 7, x + 33, 7), fill=SNOW)
    draw.rectangle((x + 26, 9, x + 29, 10), fill=GOLD)
    draw.point((x + 26, 9), fill=WHITE)
    draw.rectangle((x + 33, 10, x + 43, 12), fill=RED)
    draw.line((x + 33, 10, x + 42, 10), fill=GOLD)
    draw.rectangle((x + 39, 8, x + 40, 9), fill=RED)
    draw.line((x + 38, 8, x + 41, 8), fill=GOLD)
    draw.point((x + 43, 11), fill=WHITE)
    draw.line((x + 24, 13, x + 44, 13), fill=RED)
    draw.line((x + 44, 12, x + 46, 14), fill=GOLD)
    for wheel in (3, 15, 27, 38):
        draw.rectangle((x + wheel, 13, x + wheel + 2, 14), fill='#294274')
        draw.point((x + wheel + index % 2, 14), fill=WHITE)
        draw.point((x + wheel + 1, 13), fill=GOLD)
    for puff in range(3):
        draw.point((x + 37 - puff * 3 - index % 2, 5 - puff % 2), fill=SNOW)


def christmas_scene(snow='normal', sky=None):
    """32 exact 144×16 frames adapting the supplied tall curtain screenshot."""
    if snow not in ('none', 'normal', 'heavy'):
        raise ValueError('snow must be none, normal or heavy')
    if sky is not None:
        import re
        if not isinstance(sky, str) or not re.fullmatch(r'#[0-9a-fA-F]{6}', sky):
            raise ValueError('sky must be #RRGGBB')
    base = Image.new('RGB', (pixels.WIDTH, pixels.HEIGHT))
    draw = ImageDraw.Draw(base)
    for y in range(16):
        for region in range(4):
            color = sky or (8 + region * 2, 20 + (y // 2) * 2, 70 + region * 6 + (y // 2) * 3)
            draw.line((region * 36, y, region * 36 + 35, y), fill=color)
    # Blue curtain strands and white/gold fairy lights span the entire panel.
    for x in range(1, 144, 4):
        for y in range(x % 3, 15, 3):
            draw.point((x, y), fill=('#2044a4', '#2855bc')[x % 3 == 0])
    draw.line((0, 15, 143, 15), fill=SNOW)
    frames = []
    for index in range(FRAME_COUNT):
        image = base.copy()
        draw = ImageDraw.Draw(image)
        for flake in range({'none': 0, 'normal': 105, 'heavy': 210}[snow]):
            x = (flake * 37 + (index // 8) * (flake % 2)) % 144
            y = (flake * 7 + index) % 16
            draw.point((x, y), fill=(SNOW, '#9fbbef', WHITE)[flake % 3])
        for star in range(32):
            x, y = (star * 23 + 3) % 144, (star * 5 + 1) % 9
            draw.point((x, y), fill=GOLD if (star + index // 2) % 4 else WHITE)
            if (star + index // 4) % 8 == 0 and 1 <= y <= 6:
                draw.line((x - 1, y, x + 1, y), fill=GOLD)
                draw.line((x, y - 1, x, y + 1), fill=WHITE)
        # Gifts remain stationary; high bows survive a passing foreground train.
        for args in ((52, 9, 6, RED, '#51e5e4'), (60, 7, 6, '#2878df', GOLD),
                     (88, 8, 7, RED, GOLD), (98, 10, 6, '#b84be0', WHITE),
                     (111, 7, 8, GREEN, GOLD), (122, 9, 6, RED, WHITE),
                     (133, 6, 8, RED, GOLD)):
            _gift(draw, *args)
        _tree(draw, 78)
        position = index * 144 // FRAME_COUNT
        for wrapped in (position - 144, position):
            _train(draw, wrapped, index)
        frames.append(bridge._frame_pixels(image))
    return pixels.validate({'frames': frames, 'duration': FRAME_MS})


def jingle_bells_wav():
    """Finite mono PCM: bell melody, soft bass and arpeggiated triads, no playback."""
    semitones = {'C': 0, 'D': 2, 'E': 4, 'F': 5, 'G': 7}
    total_beats = sum(beats for _, beats in CHORUS)
    duration = total_beats * BEAT_SECONDS
    samples = [0.0] * round(duration * SAMPLE_RATE)
    beat_start = 0
    for note, beats in CHORUS:
        frequency = 523.251 * 2 ** (semitones[note] / 12)
        start = round(beat_start * BEAT_SECONDS * SAMPLE_RATE)
        count = round(beats * BEAT_SECONDS * SAMPLE_RATE)
        for offset in range(min(count, len(samples) - start)):
            age = offset / SAMPLE_RATE
            envelope = min(1, age / .006) * math.exp(-3.8 * age)
            envelope *= min(1, (count - offset - 1) / (.035 * SAMPLE_RATE))
            tone = math.sin(math.tau * frequency * age)
            tone += .20 * math.sin(math.tau * frequency * 2 * age)
            tone += .10 * math.sin(math.tau * frequency * 3 * age)
            samples[start + offset] += .17 * tone * envelope
        beat_start += beats
    # C, F, G, C harmony repeats beneath the two chorus halves.
    chords = ((130.813, 164.814, 195.998), (174.614, 220, 261.626),
              (195.998, 246.942, 293.665), (130.813, 164.814, 195.998))
    pcm = bytearray()
    for index, melody in enumerate(samples):
        time = index / SAMPLE_RATE
        beat = time / BEAT_SECONDS
        chord = chords[(int(beat) // 8) % 4]
        age = (beat % 1) * BEAT_SECONDS
        backing = .035 * math.sin(math.tau * chord[0] * time) * math.exp(-7 * age)
        backing += .025 * math.sin(math.tau * chord[int(beat) % 3] * 2 * age) * math.exp(-9 * age)
        fade = min(1, time / .015, (duration - time) / .1)
        pcm.extend(struct.pack('<h', round(32767 * (melody + backing) * fade)))
    output = io.BytesIO()
    with wave.open(output, 'wb') as audio:
        audio.setparams((1, 2, SAMPLE_RATE, 0, 'NONE', 'not compressed'))
        audio.writeframes(pcm)
    return output.getvalue()


def generate(outdir):
    output = Path(outdir)
    if output.exists() or output.is_symlink():
        raise FileExistsError(f'refusing existing output: {output}')
    audio = jingle_bells_wav()
    result = bridge.export_bundle(christmas_scene(), output)
    with (output / 'jingle_bells.wav').open('xb') as stream:
        stream.write(audio)
    result['files'].append('jingle_bells.wav')
    result['interpretation'] = 'procedural screenshot adaptation; no arbitrary AI generation'
    result['reference_adaptation'] = {
        'source_url': REFERENCE_URL,
        'screenshot': REFERENCE_SCREENSHOT,
        'basis': 'supplied screenshot only; source video not reviewed',
        'composition': 'blue night, white/gold snow and stars, red/green train, tree and gifts',
        'limitations': 'recomposed for 144x16; no exact source-video match claimed',
    }
    project_path = output / 'project.json'
    project = json.loads(project_path.read_text())
    project['reference_adaptation'] = result['reference_adaptation']
    project_path.write_text(json.dumps(project, separators=(',', ':')) + '\n')
    result['reference_match'] = 'screenshot adaptation only; exact source-video match unverified'
    result['audio'] = 'original synthesized instrumental arrangement of traditional Jingle Bells'
    result['audio_seconds'] = sum(beats for _, beats in CHORUS) * BEAT_SECONDS
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    command = commands.add_parser('generate', help='create a fresh offline asset bundle')
    command.add_argument('outdir', type=Path)
    args = parser.parse_args(argv)
    try:
        result = generate(args.outdir)
    except (ValueError, OSError, wave.Error) as error:
        parser.exit(1, f'{type(error).__name__}: {error}\n')
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


if __name__ == '__main__':
    main()
