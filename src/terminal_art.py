"""Pillow scene composition and truecolor terminal output; no display side effects."""

from functools import lru_cache
from pathlib import Path
import re
import unicodedata

from PIL import Image


LAVENDER = (233, 224, 245)
ROOM_BORDER = (112, 82, 63)
_ANSI = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]")


class TerminalArt:
    """Compose RGB pixel canvases (two pixel rows per terminal graphic row).

    Assets are loaded once. Resized layers are cached with a bounded cache;
    returned frames are independent images and may be modified by the caller.
    Phase is a continuous animation cycle, with integer phases repeating.
    """

    def __init__(self, asset_dir=None):
        directory = Path(asset_dir) if asset_dir is not None else (
            Path(__file__).resolve().parent.parent / "assets" / "character"
        )
        self._assets = {}
        for name in ("room", "walker", "portrait"):
            with Image.open(directory / f"{name}.png") as source:
                self._assets[name] = source.convert("RGBA")
        # Instance-owned cache avoids retaining old renderer instances globally.
        self._scaled = lru_cache(maxsize=24)(self._resize)

    def _resize(self, name, width, height):
        source = self._assets[name]
        scale = min(width / source.width, height / source.height)
        size = (max(1, int(source.width * scale)),
                max(1, int(source.height * scale)))
        return source.resize(size, Image.Resampling.LANCZOS)

    def compose(self, mode, width, height, phase=0, target="wander"):
        """Return exactly width × height RGB pixels; dimensions must be positive."""
        if width <= 0 or height <= 0:
            raise ValueError("canvas dimensions must be positive")
        if mode in ("room", "acting"):
            canvas = Image.new("RGBA", (width, height), ROOM_BORDER + (255,))
            room = self._scaled("room", width, height)
            left, top = (width - room.width) // 2, (height - room.height) // 2
            canvas.alpha_composite(room, (left, top))
            walker = self._scaled("walker", room.width, max(1, round(room.height * .30)))
            progress = 1 - abs(2 * (phase % 1) - 1)
            # Feet stay on the clear floor between the rug and right-hand plant.
            if target == "wander":
                center = .42 + .36 * progress
            else:
                center = {"led": .65, "radio": .73}.get(target, .60)
                center += .025 * (2 * progress - 1)
            x = left + round(room.width * center - walker.width / 2)
            y = top + max(0, round(room.height * .95) - walker.height)
            x = max(left, min(x, left + room.width - walker.width))
            canvas.alpha_composite(walker, (x, y))
        else:
            canvas = Image.new("RGBA", (width, height), LAVENDER + (255,))
            portrait = self._scaled("portrait", width, height)
            canvas.alpha_composite(
                portrait, ((width - portrait.width) // 2, (height - portrait.height) // 2)
            )
        return canvas.convert("RGB")


def image_to_ansi(image):
    """Encode upper-half blocks with top foreground and bottom background RGB.

    Odd final pixel rows are padded by repeating their last row. Each terminal
    row resets SGR; rows use CRLF with no trailing newline. Identical color runs
    share escape sequences. Cursor placement belongs to the CLI.
    """
    rgb = image.convert("RGB")
    pixels = rgb.load()
    rows = []
    for y in range(0, rgb.height, 2):
        parts = []
        foreground = background = None
        for x in range(rgb.width):
            upper = pixels[x, y]
            lower = pixels[x, min(y + 1, rgb.height - 1)]
            if upper != foreground:
                parts.append("\x1b[38;2;%d;%d;%dm" % upper)
                foreground = upper
            if lower != background:
                parts.append("\x1b[48;2;%d;%d;%dm" % lower)
                background = lower
            parts.append("▀")
        parts.append("\x1b[0m")
        rows.append("".join(parts))
    return "\r\n".join(rows)


def sanitize_terminal_text(text, max_cells):
    """Remove terminal controls and truncate by display cells, retaining Korean.

    East Asian wide/fullwidth characters occupy two cells, combining marks zero,
    and other printable characters one. Unicode controls, format characters,
    surrogates and line/paragraph separators cannot reach the terminal.
    """
    if max_cells <= 0:
        return ""
    result = []
    cells = 0
    for char in _ANSI.sub("", text):
        category = unicodedata.category(char)
        if category.startswith("C") or category in ("Zl", "Zp"):
            continue
        width = 0 if category.startswith("M") else (
            2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
        )
        if cells + width > max_cells:
            break
        if width == 0 and not result:
            continue
        result.append(char)
        cells += width
    return "".join(result)
