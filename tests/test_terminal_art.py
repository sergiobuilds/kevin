import re
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageChops

from terminal_art import LAVENDER, ROOM_BORDER, TerminalArt, image_to_ansi, sanitize_terminal_text


class TerminalArtTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        assets = Path(self.directory.name)
        room = Image.new("RGB", (120, 80), (20, 40, 60))
        room.paste((80, 100, 120), (0, 0, 60, 80))
        room.save(assets / "room.png")
        Image.new("RGBA", (20, 60), (255, 0, 0, 255)).save(assets / "walker.png")
        portrait = Image.new("RGBA", (20, 40), (0, 0, 0, 0))
        portrait.paste((0, 200, 0, 128), (5, 5, 15, 35))
        portrait.save(assets / "portrait.png")
        self.art = TerminalArt(assets)

    def test_all_modes_return_exact_rgb_canvas_even_when_tiny(self):
        for mode in ("room", "acting", "talk", "planning", "error", "stopped", "listening"):
            for size in ((1, 1), (7, 13), (160, 80), (30, 120)):
                with self.subTest(mode=mode, size=size):
                    frame = self.art.compose(mode, *size)
                    self.assertEqual(frame.size, size)
                    self.assertEqual(frame.mode, "RGB")

    def test_room_fits_without_stretching_and_has_distinct_background(self):
        frame = self.art.compose("room", 200, 80)
        self.assertEqual(frame.getpixel((39, 20)), ROOM_BORDER)
        self.assertEqual(frame.getpixel((40, 20)), (80, 100, 120))
        self.assertEqual(frame.getpixel((159, 20)), (20, 40, 60))
        self.assertEqual(frame.getpixel((160, 20)), ROOM_BORDER)
        self.assertEqual(self.art.compose("talk", 200, 80).getpixel((0, 0)), LAVENDER)

    def test_portrait_contains_entire_alpha_layer_on_lavender(self):
        # A 20x40 portrait fits 100x80 at 40x80, centered at x=30.
        frame = self.art.compose("talk", 100, 80)
        expected = Image.new("RGBA", (100, 80), LAVENDER + (255,))
        with Image.open(Path(self.directory.name) / "portrait.png") as portrait:
            expected.alpha_composite(portrait.resize((40, 80), Image.Resampling.LANCZOS), (30, 0))
        self.assertIsNone(ImageChops.difference(frame, expected.convert("RGB")).getbbox())
        self.assertEqual(frame.getpixel((30, 0)), LAVENDER)
        self.assertEqual(frame.getpixel((50, 40)), (116, 211, 122))

    def test_walker_moves_only_in_clear_floor_corridor(self):
        positions = []
        for target in ("wander", "led", "radio", "unknown"):
            for phase in (0, .25, .5, .75, 1, -0.25):
                frame = self.art.compose("acting", 120, 80, phase, target)
                red = [(x, y) for y in range(80) for x in range(120)
                       if frame.getpixel((x, y)) == (255, 0, 0)]
                self.assertTrue(red)
                xs, ys = zip(*red)
                self.assertEqual(max(ys), 75)
                self.assertEqual(max(ys) - min(ys) + 1, 24)
                self.assertGreaterEqual(min(xs), 46)
                self.assertLessEqual(max(xs), 98)
                if target == "wander":
                    positions.append(min(xs))
        self.assertGreater(len(set(positions)), 1)
        self.assertEqual(positions[0], positions[4])

    def test_frames_do_not_mutate_cached_layers(self):
        first = self.art.compose("room", 120, 80)
        first.paste((0, 0, 0), (0, 0, 120, 80))
        self.assertEqual(self.art.compose("room", 120, 80).getpixel((0, 0)), (80, 100, 120))

    def test_invalid_dimensions_fail(self):
        for size in ((0, 80), (80, 0), (-1, 10)):
            with self.assertRaises(ValueError):
                self.art.compose("talk", *size)

    def test_shipped_assets_load_without_custom_directory(self):
        art = TerminalArt()
        for mode in ("room", "talk"):
            frame = art.compose(mode, 120, 80)
            self.assertEqual(frame.size, (120, 80))
            self.assertGreater(len(frame.getcolors(10000)), 100)


class AnsiTests(unittest.TestCase):
    def test_top_is_foreground_bottom_is_background_and_runs_share_color(self):
        image = Image.new("RGB", (3, 2), (1, 2, 3))
        image.paste((4, 5, 6), (0, 1, 3, 2))
        self.assertEqual(image_to_ansi(image), "\x1b[38;2;1;2;3m\x1b[48;2;4;5;6m▀▀▀\x1b[0m")

    def test_color_changes_and_odd_row_padding(self):
        image = Image.new("RGB", (2, 3), (10, 20, 30))
        image.putpixel((1, 0), (40, 50, 60))
        output = image_to_ansi(image)
        rows = output.split("\r\n")
        self.assertEqual(len(rows), 2)
        self.assertIn("\x1b[38;2;40;50;60m▀", rows[0])
        self.assertEqual(rows[1], "\x1b[38;2;10;20;30m\x1b[48;2;10;20;30m▀▀\x1b[0m")
        self.assertFalse(output.endswith("\n"))
        for row in rows:
            self.assertTrue(row.endswith("\x1b[0m"))
            self.assertEqual(re.sub(r"\x1b\[[0-9;]*m", "", row), "▀▀")


class TerminalTextTests(unittest.TestCase):
    def test_ansi_injection_and_unicode_controls_are_removed(self):
        text = "\x1b[2J안\x1b[31m녕\x1b[0m\x1b]0;hijack\x07\r\n\t\x00\x7f\x85\u202e\u200b\u2028\u2029!"
        self.assertEqual(sanitize_terminal_text(text, 20), "안녕!")
        self.assertEqual(sanitize_terminal_text("a\x1b]title\x1b\\b", 10), "ab")

    def test_korean_and_fullwidth_characters_respect_cell_limit(self):
        self.assertEqual(sanitize_terminal_text("가나다", 5), "가나")
        self.assertEqual(sanitize_terminal_text("A한ＢZ", 5), "A한Ｂ")
        self.assertEqual(sanitize_terminal_text("한글", 1), "")
        self.assertEqual(sanitize_terminal_text("anything", 0), "")

    def test_combining_marks_do_not_take_extra_cells(self):
        self.assertEqual(sanitize_terminal_text("e\u0301한!", 3), "e\u0301한")
        self.assertEqual(sanitize_terminal_text("\u0301A", 1), "A")


if __name__ == "__main__":
    unittest.main()
