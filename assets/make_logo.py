#!/usr/bin/env python3
"""
Generate the GARW Genie pixel wordmark (logo + icon) as PNG and SVG.
Letters are drawn from a 5x7 bitmap font, then given layered pixel outlines
and an "electric" spark border by dilating the pixel mask.  Re-run after
tweaking PALETTE / SCALE:   python3 assets/make_logo.py
"""
import random
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent

# 5x7 pixel font (1 = pixel on). Chunky, slightly rounded corners for the retro look.
FONT = {
    "G": ["01111", "10000", "10000", "10111", "10001", "10001", "01111"],
    "A": ["01110", "10001", "10001", "11111", "10001", "10001", "10001"],
    "R": ["11110", "10001", "10001", "11110", "10100", "10010", "10001"],
    "W": ["10001", "10001", "10001", "10101", "10101", "11011", "10001"],
    "E": ["11111", "10000", "10000", "11110", "10000", "10000", "11111"],
    "N": ["10001", "11001", "10101", "10011", "10001", "10001", "10001"],
    "I": ["11111", "00100", "00100", "00100", "00100", "00100", "11111"],
    "H": ["10001", "10001", "10001", "11111", "10001", "10001", "10001"],
    "C": ["01111", "10000", "10000", "10000", "10000", "10000", "01111"],
    " ": ["00000"] * 7,
}

PALETTE = {
    "bg":     (15, 18, 22),       # app window colour, so the header blends
    "face":   (255, 122, 26),     # GARW orange
    "shade":  (196, 82, 8),       # bottom/right bevel on the orange
    "hilite": (255, 170, 96),     # top/left bevel
    "ink":    (24, 16, 12),       # ring 1: dark ink
    "cream":  (244, 239, 224),    # ring 2: pale outline
    "teal":   (47, 212, 255),     # sparks
    "green":  (61, 220, 132),     # GENIE face (second word)
    "gshade": (24, 150, 84),
    "ghilite": (150, 245, 190),
}


STROKE = 3   # Press Start 2P is an 8 px/em pixel font; size 8*STROKE gives STROKE-pixel strokes
TTF = HERE / "PressStart2P.ttf"


def render_word(word, gap=1):
    """Return (set of (x, y) pixels, width) for a word, using the real Press Start 2P glyphs
    when the TTF is present (falls back to the built-in 5x7 font)."""
    if TTF.is_file():
        # Press Start 2P is monospaced, so a narrow glyph like "I" sits in a wide cell.
        # Render each glyph on its own, trim to its ink, and pack with a fixed gap between
        # ink edges (optical spacing) so GENIE doesn't gap around the I.
        from PIL import ImageFont
        font = ImageFont.truetype(str(TTF), 8 * STROKE)
        letter_gap = gap * STROKE
        px, x0 = set(), 0
        for ch in word:
            img = Image.new("L", (8 * STROKE + 8, 12 * STROKE), 0)
            ImageDraw.Draw(img).text((0, 0), ch, font=font, fill=255)
            w, h = img.size
            data = img.load()
            g = {(x, y) for y in range(h) for x in range(w) if data[x, y] > 128}
            if not g:
                x0 += 4 * STROKE
                continue
            gminx, gmaxx = min(x for x, _ in g), max(x for x, _ in g)
            px |= {(x - gminx + x0, y) for (x, y) in g}
            x0 += (gmaxx - gminx + 1) + letter_gap
        if not px:
            return px, 0
        miny = min(y for _, y in px)
        return {(x, y - miny) for (x, y) in px}, x0 - letter_gap
    px, x0 = set(), 0
    for ch in word:
        rows = FONT[ch]
        for y, row in enumerate(rows):
            for x, bit in enumerate(row):
                if bit == "1":
                    for sx in range(STROKE):
                        for sy in range(STROKE):
                            px.add((x0 + x * STROKE + sx, y * STROKE + sy))
        x0 += (len(rows[0]) + gap) * STROKE
    return px, x0 - gap * STROKE


def dilate(mask, r=1, diamond=False):
    out = set()
    for (x, y) in mask:
        for dx in range(-r, r + 1):
            for dy in range(-r, r + 1):
                if diamond and abs(dx) + abs(dy) > r:
                    continue
                out.add((x + dx, y + dy))
    return out


def sparks(mask, ring, rng, density=0.42):
    """Jagged electric pixels just outside `ring`, attached to it."""
    outer = dilate(ring, 1) - ring - mask
    out = set()
    for p in sorted(outer):
        if rng.random() < density:
            out.add(p)
            if rng.random() < 0.5:  # occasional 2-pixel tendril
                dx, dy = rng.choice([(1, 0), (-1, 0), (0, 1), (0, -1)])
                q = (p[0] + dx, p[1] + dy)
                if q not in ring and q not in mask:
                    out.add(q)
    return out


def compose(lines, scale, pad=6, line_gap=3, seed=7):
    """lines: [(word, face, shade, hilite)]. Returns (PIL image, svg string)."""
    rng = random.Random(seed)
    layers = []      # (pixels, colour) drawn back to front
    masks = []
    y0 = 0
    width = 0
    for word, *_ in lines:
        px, w = render_word(word)
        px = {(x, y + y0) for (x, y) in px}
        masks.append(px)
        width = max(width, w)
        y0 += 7 * STROKE + line_gap
    # centre shorter words
    for i, (word, *_) in enumerate(lines):
        _, w = render_word(word)
        off = (width - w) // 2
        masks[i] = {(x + off, y) for (x, y) in masks[i]}
    all_mask = set().union(*masks)
    ring1 = dilate(all_mask, 1) - all_mask               # ink (1 px)
    ring2 = dilate(all_mask, 3) - all_mask - ring1       # cream (2 px)
    # Fill enclosed background holes (e.g. the gap between two letters where the two
    # outlines almost meet) so the badge reads as one solid plate.
    covered = all_mask | ring1 | ring2
    xs_ = [x for x, _ in covered]; ys_ = [y for _, y in covered]
    x0, x1, y0, y1 = min(xs_) - 1, max(xs_) + 1, min(ys_) - 1, max(ys_) + 1
    outside, stack = set(), [(x0, y0)]
    while stack:
        x, y = stack.pop()
        if (x, y) in outside or (x, y) in covered or not (x0 <= x <= x1 and y0 <= y <= y1):
            continue
        outside.add((x, y))
        stack += [(x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)]
    holes = {(x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)} - covered - outside
    ring2 |= holes
    spk = sparks(all_mask, dilate(all_mask, 3), rng)     # teal sparks
    layers.append((spk, PALETTE["teal"]))
    layers.append((ring2, PALETTE["cream"]))
    layers.append((ring1, PALETTE["ink"]))
    for (word, face, shade, hilite), m in zip(lines, masks):
        layers.append((m, PALETTE[face]))
        # bevel: pixels whose bottom/right neighbour is outside the word get shade, top/left get hilite
        layers.append(({(x, y) for (x, y) in m if (x + 1, y) not in m or (x, y + 1) not in m}, PALETTE[shade]))
        layers.append(({(x, y) for (x, y) in m if ((x - 1, y) not in m or (x, y - 1) not in m)
                        and (x + 1, y) in m and (x, y + 1) in m}, PALETTE[hilite]))

    xs = [x for L, _ in layers for (x, _) in L]
    ys = [y for L, _ in layers for (_, y) in L]
    minx, maxx, miny, maxy = min(xs) - pad, max(xs) + pad, min(ys) - pad, max(ys) + pad
    W, H = (maxx - minx + 1) * scale, (maxy - miny + 1) * scale
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    svg = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" shape-rendering="crispEdges">']
    for L, col in layers:
        hexcol = "#%02x%02x%02x" % col
        for (x, y) in L:
            X, Y = (x - minx) * scale, (y - miny) * scale
            d.rectangle([X, Y, X + scale - 1, Y + scale - 1], fill=col + (255,))
            svg.append(f'<rect x="{X}" y="{Y}" width="{scale}" height="{scale}" fill="{hexcol}"/>')
    svg.append("</svg>")
    return img, "\n".join(svg)


def main():
    # Wordmark: GARW (orange) over GENIE (green), transparent background.
    logo, svg = compose([("GARW", "face", "shade", "hilite"), ("GENIE", "green", "gshade", "ghilite")], scale=6, pad=5, line_gap=4)
    logo.save(HERE / "garw_genie_logo.png")
    (HERE / "garw_genie_logo.svg").write_text(svg)
    # Same on the app's dark background, for README / previews.
    bg = Image.new("RGBA", logo.size, PALETTE["bg"] + (255,))
    bg.alpha_composite(logo)
    bg.save(HERE / "garw_genie_logo_dark.png")
    # Header-sized wordmark (single line "GARW GENIE" is too wide; keep the two-line mark, ~40 px tall).
    small, _ = compose([("GARW", "face", "shade", "hilite"), ("GENIE", "green", "gshade", "ghilite")], scale=1, pad=3, line_gap=4)
    small.save(HERE / "garw_genie_header.png")
    # Icon: a single pixel "G" with the same outlines, on the dark tile.
    g, gsvg = compose([("G", "face", "shade", "hilite")], scale=1, pad=3)
    tile = Image.new("RGBA", (512, 512), PALETTE["bg"] + (255,))
    k = 440 // max(g.width, g.height)
    gs = g.resize((g.width * k, g.height * k), Image.NEAREST)
    tile.alpha_composite(gs, ((512 - gs.width) // 2, (512 - gs.height) // 2))
    # rounded corners
    mask = Image.new("L", tile.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, 511, 511], radius=96, fill=255)
    tile.putalpha(mask)
    tile.save(HERE / "garw_genie_icon.png")
    (HERE / "garw_genie_icon.svg").write_text(gsvg)
    tile.save(HERE / "garw_genie.ico", sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)])
    print("wrote logo", logo.size, "header", small.size, "icon", tile.size)


if __name__ == "__main__":
    main()
