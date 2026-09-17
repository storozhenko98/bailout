#!/usr/bin/env python3
"""Build the static share artwork. Requires Pillow; no production dependency.

Uses the site's licensed fonts and the PNG export of site/favicon.svg.
Keep filenames versioned when changing artwork: social platforms cache images.
"""
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / 'site'
SCALE = 2
INK = '#151715'
GREEN = '#b7f567'
MUTED = '#596057'
PALE = '#edf3e8'


def font(size, bold=False, mono=False):
    result = ImageFont.truetype(str(SITE / 'fonts' / ('ibm-plex-mono.ttf' if mono else 'space-grotesk.ttf')), size * SCALE)
    if not mono:
        result.set_variation_by_axes([700 if bold else 500])
    return result


class Canvas:
    def __init__(self, height):
        self.height = height
        self.image = Image.new('RGB', (1200 * SCALE, height * SCALE), 'white')
        self.draw = ImageDraw.Draw(self.image)

    def box(self, coords, fill, radius=0, outline=None, width=3):
        self.draw.rounded_rectangle(tuple(round(x * SCALE) for x in coords), radius * SCALE,
                                    fill=fill, outline=outline, width=width * SCALE)

    def line(self, coords, fill=INK, width=3):
        self.draw.line([(int(x * SCALE), int(y * SCALE)) for x, y in coords], fill=fill, width=width * SCALE, joint='curve')

    def text(self, xy, text, size, bold=False, mono=False, fill=INK):
        face = font(size, bold, mono)
        assert self.draw.textlength(text, font=face) / SCALE + xy[0] < 1160, text
        self.draw.text((xy[0] * SCALE, xy[1] * SCALE), text, font=face, fill=fill, anchor='lt')

    def icon(self, x, y, size):
        icon = Image.open(SITE / 'icon-512.png').convert('RGBA').resize((size * SCALE, size * SCALE), Image.Resampling.LANCZOS)
        self.image.paste(icon, (x * SCALE, y * SCALE), icon)

    def save(self, name):
        target = SITE / 'social' / name
        self.image.resize((1200, self.height), Image.Resampling.LANCZOS).save(target, optimize=True)
        print(f'{target.relative_to(ROOT)}: 1200 × {self.height}, {target.stat().st_size:,} bytes')


def terminal(c, x, y, w, h, guide=False):
    c.box((x + 10, y + 10, x + w + 10, y + h + 10), INK, 14)
    c.box((x, y, x + w, y + h), INK, 14, INK)
    c.box((x + 3, y + 3, x + w - 3, y + 52), PALE, 11)
    c.box((x + 3, y + 30, x + w - 3, y + 52), PALE)
    for i in range(3):
        c.box((x + 22 + 23*i, y + 21, x + 34 + 23*i, y + 33), 'white', 6, INK, 2)
    c.line([(x + 40, y + 90), (x + 57, y + 104), (x + 40, y + 118)], GREEN, 5)
    c.line([(x + 77, y + 119), (x + 112, y + 119)], GREEN, 5)
    for i, length in enumerate([0.56, 0.43, 0.65]):
        yy = y + 162 + i * 46
        if guide:
            c.box((x + 40, yy, x + 57, yy + 17), None, 3, GREEN, 2)
        else:
            c.line([(x + 40, yy + 8), (x + 47, yy + 15), (x + 60, yy + 1)], GREEN, 4)
        c.box((x + 82, yy + 3, x + w * length, yy + 11), '#75846b', 4)


def graph_card(guide=False):
    # Graphical Open Graph artwork stays legible at small Messages preview sizes.
    c = Canvas(630)
    c.box((0, 0, 1200, 630), PALE)
    for x in range(0, 1201, 80): c.line([(x, 0), (x, 630)], '#dce5d5', 1)
    for y in range(0, 631, 80): c.line([(0, y), (1200, y)], '#dce5d5', 1)
    terminal(c, 238, 136, 690, 358, guide)
    # The large exit arrow echoes Bailout's mark: temporary help, then move on.
    c.box((827, 62, 1065, 300), INK, 15)
    c.box((817, 52, 1055, 290), GREEN, 15, INK, 4)
    c.line([(865, 241), (1005, 103)], INK, 18)
    c.line([(923, 103), (1005, 103), (1005, 184)], INK, 18)
    if guide:
        c.box((127, 367, 339, 562), INK, 10)
        c.box((117, 357, 329, 552), 'white', 10, INK)
        c.line([(223, 360), (223, 548)], INK, 3)
        for yy in [392, 416, 440, 464]:
            c.line([(141, yy), (201, yy)], '#75846b', 4)
            c.line([(246, yy), (306, yy)], '#75846b', 4)
    else:
        c.box((100, 360, 270, 530), INK, 16)
        c.icon(90, 350, 170)
    c.save('docs-og-v1.png' if guide else 'home-og-v1.png')


def x_card(guide=False):
    # Explicit 2:1 art for X, where the image may be the main visible headline.
    c = Canvas(600)
    c.icon(60, 48, 52)
    c.text((125, 57), 'bailout.', 40, bold=True)
    c.text((951, 66), 'bailout.dev', 18, mono=True, fill=MUTED)
    if guide:
        lines = ['Get in.', 'Get unstuck.', 'Get out.']
        c.box((57, 350, 441, 424), GREEN)
    else:
        lines = ['The harness', 'meant to be', 'deleted.']
        c.box((57, 350, 410, 424), GREEN)
    for i, line in enumerate(lines): c.text((60, 165 + i * 89), line, 80, bold=True)
    terminal(c, 762, 182, 365, 323, guide)
    c.text((64, 474), 'Setup & recovery guide.' if guide else 'Fresh machine? Broken agent?', 27, fill=MUTED)
    c.text((64, 537), 'GET IN. GET UNSTUCK. GET OUT.' if guide else 'ONE COMMAND. NO ACCOUNT. NO API KEY.', 16, mono=True)
    c.save('docs-x-v1.png' if guide else 'home-x-v1.png')


if __name__ == '__main__':
    (SITE / 'social').mkdir(exist_ok=True)
    for guide in [False, True]:
        graph_card(guide)
        x_card(guide)
