"""Sixel output: real pixels inside the terminal (Windows Terminal 1.22+, xterm, WezTerm).

KEVIN's scenes are drawn at DOS VGA resolution (320x200) and scaled up with
hard pixel edges, so the terminal shows crisp 90s-PC-game art instead of the
blurry half-block approximation.
"""
from __future__ import annotations

from PIL import Image

VGA = (320, 200)


def to_vga(image: Image.Image, scale: int = 3, colors: int = 64) -> Image.Image:
    """Downsample to 320x200 with a small palette, then scale up with hard edges."""
    small = image.convert('RGB').resize(VGA, Image.Resampling.LANCZOS)
    small = small.quantize(colors=colors, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
    return small.resize((VGA[0] * scale, VGA[1] * scale), Image.Resampling.NEAREST)


def encode(paletted: Image.Image) -> str:
    """Encode a 'P' mode image as a sixel string."""
    if paletted.mode != 'P':
        paletted = paletted.quantize(colors=64, dither=Image.Dither.NONE)
    width, height = paletted.size
    pal = paletted.getpalette()[: 3 * 256]
    used = sorted(set(paletted.getdata()))
    out = ['\x1bP0;1;0q', f'"1;1;{width};{height}']
    for i in used:
        r, g, b = pal[3 * i: 3 * i + 3]
        out.append(f'#{i};2;{r * 100 // 255};{g * 100 // 255};{b * 100 // 255}')
    try:
        import numpy as np
    except ImportError:
        np = None
    if np is not None:
        idx = np.asarray(paletted, dtype=np.uint8)
        pad = (-height) % 6
        if pad:
            idx = np.vstack([idx, np.full((pad, width), 255, np.uint8)])
        weights = (1 << np.arange(6, dtype=np.uint8))[:, None]
        for top in range(0, idx.shape[0], 6):
            band = idx[top: top + 6]
            first = True
            for color in np.unique(band):
                if color == 255 and pad:
                    continue
                bits = ((band == color) * weights).sum(0).astype(np.uint8) + 63
                nz = np.flatnonzero(bits != 63)
                if nz.size == 0:
                    continue
                bits = bits[: nz[-1] + 1]
                cuts = np.flatnonzero(np.diff(bits)) + 1
                starts = np.concatenate(([0], cuts))
                lengths = np.diff(np.concatenate((starts, [bits.size])))
                row = []
                for s, n in zip(starts.tolist(), lengths.tolist()):
                    ch = chr(int(bits[s]))
                    row.append(f'!{n}{ch}' if n > 3 else ch * n)
                out.append(('' if first else '$') + f'#{int(color)}' + ''.join(row))
                first = False
            out.append('-')
    else:  # slow but dependency free
        px = paletted.load()
        for top in range(0, height, 6):
            colors = {px[x, y] for y in range(top, min(top + 6, height)) for x in range(width)}
            first = True
            for color in sorted(colors):
                row = []
                for x in range(width):
                    bits = 0
                    for k in range(6):
                        y = top + k
                        if y < height and px[x, y] == color:
                            bits |= 1 << k
                    row.append(chr(63 + bits))
                out.append(('' if first else '$') + f'#{color}' + ''.join(row).rstrip('?'))
                first = False
            out.append('-')
    out.append('\x1b\\')
    return ''.join(out)
