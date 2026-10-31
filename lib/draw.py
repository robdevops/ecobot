"""Drawing for the charts and the dashboard, on Pillow alone: a Canvas in figure pixels (y down), drawn at SCALE times the size and
reduced, so edges and text come out smooth; the fonts found on the system; tick steps; and the PNG of at most 256 colours.

Sizes given in points (pt) are at DPI: 1 pt = DPI / 72 px, as the charts were laid out."""

import functools
import glob
import io
import math
from dataclasses import dataclass

from PIL import Image, ImageChops, ImageColor, ImageDraw, ImageFont

DPI = 200
PT = DPI / 72                    # pixels in a point
SCALE = 3                        # drawn this many times larger, then reduced: the anti-aliasing
FONT_DIRS = ("/usr/share/fonts", "/usr/local/share/fonts")
FAMILIES = ("Inter", "Roboto", "DejaVu Sans", "Liberation Sans")   # the first installed is used; else Pillow's own font
WEIGHTS = {"normal": 400, "semibold": 600, "bold": 700}
LINE_SPACING = 1.2               # a line of a multi-line label is this many times its font size


# ---------- colours ----------
@functools.cache
def rgb(colour: str) -> tuple[int, int, int]:
    """(red, green, blue) of "#RRGGBB" or a colour name."""
    return ImageColor.getrgb(colour)[:3]


def blend(colour: str, share: float, towards: str = "#FFFFFF") -> str:
    """The colour with this share of `towards` (white, unless given) blended in."""
    return "#" + "".join(f"{round(t * share + c * (1 - share)):02X}" for c, t in zip(rgb(colour), rgb(towards)))


# ---------- fonts ----------
def _installed() -> dict[str, dict[int, str]]:
    """{family: {weight: file}} of the upright fonts installed."""
    found: dict[str, dict[int, str]] = {}
    for path in sorted(p for d in FONT_DIRS for p in glob.glob(f"{d}/**/*.[ot]tf", recursive=True)):
        try:
            family, style = ImageFont.truetype(path, 10).getname()
        except Exception:
            continue
        style = (style or "").lower().replace(" ", "").replace("-", "")
        if "italic" in style or "oblique" in style:
            continue
        weight = {"regular": 400, "book": 400, "normal": 400, "semibold": 600, "demibold": 600, "bold": 700}.get(style)
        if weight is not None:
            found.setdefault(family, {}).setdefault(weight, path)
    return found


class Fonts:
    """The font family the pictures are drawn in, and its files by weight. `family=None` (or none installed): Pillow's own font,
    one weight, made bold by an outline (the reference pictures use it, so they come out the same on any machine)."""

    def __init__(self, family: str | None = "auto"):
        files = _installed() if family else {}
        if family == "auto":
            family = next((f for f in FAMILIES if 400 in files.get(f, {})), None)
        self.files = files.get(family, {}) if family else {}
        self.family = family if self.files else "Pillow"
        self.title_weight = "semibold" if 600 in self.files else "bold"

    @functools.lru_cache(maxsize=256)
    def get(self, size_px: float, weight: str = "normal") -> tuple[ImageFont.FreeTypeFont, int]:
        """(font, outline width for a bold made by outline) at this size in pixels."""
        want = WEIGHTS[weight]
        if self.files:
            have = min(self.files, key=lambda w: (abs(w - want), -w))
            return ImageFont.truetype(self.files[have], round(size_px)), 0
        return ImageFont.load_default(round(size_px)), round(size_px / 22) if want >= 600 else 0


FONTS = Fonts()


def use_fonts(family: str | None = "auto"):
    """Draw in this family from now on ("auto": as at start; None: Pillow's own font, for pictures that must match anywhere)."""
    global FONTS
    FONTS = Fonts(family)


# ---------- geometry ----------
def clip_below(points: list[tuple[float, float]], level: float) -> list[tuple[float, float]]:
    """The part of a closed shape at or below `level` (y down: y >= level), as a closed shape."""
    out = []
    for (x0, y0), (x1, y1) in zip(points, points[1:] + points[:1]):
        inside0, inside1 = y0 >= level, y1 >= level
        if inside0:
            out.append((x0, y0))
        if inside0 != inside1:
            out.append((x0 + (x1 - x0) * (level - y0) / (y1 - y0), level))
    return out


def arc(cx: float, cy: float, r: float, start: float, end: float, steps: int) -> list[tuple[float, float]]:
    """Points on a circle from `start` to `end` degrees (0 = east, counter-clockwise, as on paper; y down); `steps` 0: its first point."""
    if steps <= 0:
        return [(cx + r * math.cos(math.radians(start)), cy - r * math.sin(math.radians(start)))]
    return [(cx + r * math.cos(math.radians(a)), cy - r * math.sin(math.radians(a)))
            for a in (start + (end - start) * i / steps for i in range(steps + 1))]


def rounded_rect(x0: float, y0: float, x1: float, y1: float, r: float, steps: int = 8) -> list[tuple[float, float]]:
    """A rectangle with rounded corners as a closed shape."""
    r = max(0.0, min(r, (x1 - x0) / 2, (y1 - y0) / 2))
    return (arc(x1 - r, y0 + r, r, 0, 90, steps) + arc(x0 + r, y0 + r, r, 90, 180, steps) + arc(x0 + r, y1 - r, r, 180, 270, steps)
            + arc(x1 - r, y1 - r, r, 270, 360, steps))


def nice_ticks(lo: float, hi: float, nbins: int, steps=(1, 2, 2.5, 5, 10), integer: bool = False) -> list[float]:
    """Round tick values within [lo, hi]: the smallest step of `steps` (times a power of ten) that gives at most `nbins` intervals
    and at least two ticks."""
    if hi <= lo:
        return [lo]
    raw = (hi - lo) / nbins
    scale = 10 ** math.floor(math.log10(raw))
    for power in (scale, scale * 10, scale * 100):
        for s in steps:
            step = s * power
            if (integer and step < 1) or step < raw * (1 - 1e-9):
                continue
            first = math.ceil(lo / step - 1e-9) * step
            ticks = [round(first + i * step, 10) for i in range(int((hi - first) / step + 1e-9) + 1)]
            if len(ticks) >= 2 or step > hi - lo:
                return [t + 0.0 for t in ticks]
    return [lo, hi]


# ---------- the canvas ----------
@dataclass
class Text:
    """A label as drawn (kept on the canvas, for tests to read)."""
    text: str
    x: float
    y: float
    ha: str
    va: str
    box: tuple[float, float, float, float]   # its extent in figure pixels: left, top, right, bottom
    order: tuple = ()                        # where it comes in the drawing as it was laid out (see Canvas.current)


ANCHOR_H = {"left": "l", "center": "m", "right": "r"}
ANCHOR_V = {"top": "a", "center": "m", "baseline": "s", "bottom": "d"}


class Canvas:
    """A picture `width` x `height` pixels on `bg`. Every coordinate is in pixels of the final picture (y down); line widths and
    font sizes are in points."""

    def __init__(self, width: int, height: int, bg: str = "#FFFFFF", scale: int = SCALE):
        self.width, self.height, self.s = width, height, scale
        self.image = Image.new("RGB", (width * scale, height * scale), bg)
        self.pen = ImageDraw.Draw(self.image)
        self.texts: list[Text] = []
        self.current: tuple = ()   # (panel, place) of what is being drawn: labels are listed in the order they were laid out
        self.panels = 0

    def _xy(self, points) -> list[tuple[float, float]]:
        s = self.s
        return [(x * s, y * s) for x, y in points]

    def _translucent(self, points: list[tuple[float, float]], colour: str, alpha: float):
        """A shape filled with `colour` at `alpha` over what is already drawn (only its own corner of the picture is redone)."""
        xs, ys = [p[0] for p in points], [p[1] for p in points]
        box = (max(0, math.floor(min(xs))), max(0, math.floor(min(ys))),
               min(self.image.width, math.ceil(max(xs)) + 1), min(self.image.height, math.ceil(max(ys)) + 1))
        if box[2] <= box[0] or box[3] <= box[1]:
            return
        layer = Image.new("RGBA", (box[2] - box[0], box[3] - box[1]), (0, 0, 0, 0))
        ImageDraw.Draw(layer).polygon([(x - box[0], y - box[1]) for x, y in points], fill=(*rgb(colour), round(255 * alpha)))
        region = self.image.crop(box).convert("RGBA")
        self.image.paste(Image.alpha_composite(region, layer).convert("RGB"), box[:2])

    def polygon(self, points, fill: str, alpha: float = 1.0, outline: str | None = None, width: float = 0.0):
        """A filled shape (closed), with an outline `width` pt wide when given."""
        if len(points) < 3:
            return
        xy = self._xy(points)
        if alpha < 1:
            self._translucent(xy, fill, alpha)
        elif fill:
            self.pen.polygon(xy, fill=fill)
        if outline and width:
            self.line(list(points) + [points[0]], outline, width)

    def rect(self, x0: float, y0: float, x1: float, y1: float, fill: str, alpha: float = 1.0):
        x0, x1 = min(x0, x1), max(x0, x1)
        y0, y1 = min(y0, y1), max(y0, y1)
        if alpha < 1:
            self.polygon([(x0, y0), (x1, y0), (x1, y1), (x0, y1)], fill, alpha)
        else:
            s = self.s
            self.pen.rectangle((x0 * s, y0 * s, x1 * s - 1, y1 * s - 1), fill=fill)

    def circle(self, x: float, y: float, r: float, fill: str | None = None, outline: str | None = None, width: float = 0.0):
        """A disc of radius `r` px (and a ring `width` pt wide, centred on its edge)."""
        s = self.s
        if fill:
            self.pen.ellipse(((x - r) * s, (y - r) * s, (x + r) * s, (y + r) * s), fill=fill)
        if outline and width:
            w = width * PT * s
            self.pen.ellipse(((x - r) * s - w / 2, (y - r) * s - w / 2, (x + r) * s + w / 2, (y + r) * s + w / 2), outline=outline,
                             width=max(1, round(w)))

    def dot(self, x: float, y: float, diameter: float, colour: str, rim: float = 0.0, rim_colour: str = "#FFFFFF"):
        """A dot `diameter` pt across with a rim `rim` pt wide centred on its edge, as a marker is drawn."""
        r = diameter * PT / 2
        if rim:
            self.circle(x, y, r + rim * PT / 2, rim_colour)
            self.circle(x, y, max(r - rim * PT / 2, 0.5), colour)
        else:
            self.circle(x, y, r, colour)

    def line(self, points, colour: str, width: float, dots: tuple[float, float] | None = None, round_ends: bool = True,
             alpha: float = 1.0):
        """A line through the points, `width` pt wide with round joins and ends. dots=(on, off), in widths: a dotted line instead."""
        if len(points) < 2:
            return
        if alpha < 1:
            colour = blend(colour, 1 - alpha, "#FFFFFF")
        if dots:
            for a, b in _dashes(points, dots[0] * width * PT, dots[1] * width * PT):
                self.line([a, b], colour, width, round_ends=False)
            return
        w = width * PT * self.s
        xy = self._xy(points)
        self.pen.line(xy, fill=colour, width=max(1, round(w)), joint="curve" if len(xy) > 2 else None)
        if round_ends and w >= 3:
            for x, y in (xy[0], xy[-1]):
                self.pen.ellipse((x - w / 2, y - w / 2, x + w / 2, y + w / 2), fill=colour)

    # ----- text -----
    def font(self, size: float, weight: str = "normal"):
        return FONTS.get(size * PT * self.s, weight)

    def measure(self, text: str, size: float, weight: str = "normal") -> tuple[float, float]:
        """(width, height) of the text in pixels, as `text` draws it."""
        box = self.text_box(text, 0, 0, size, weight)
        return box[2] - box[0], box[3] - box[1]

    def text_box(self, text: str, x: float, y: float, size: float, weight: str = "normal", ha: str = "left",
                 va: str = "baseline") -> tuple[float, float, float, float]:
        font, stroke = self.font(size, weight)
        anchor = ANCHOR_H[ha] + ANCHOR_V[va if "\n" not in text or va != "baseline" else "top"]
        s = self.s
        if "\n" in text:
            box = self.pen.multiline_textbbox((x * s, y * s), text, font=font, anchor=anchor, spacing=self._spacing(font),
                                              align=ha if ha != "center" else "center", stroke_width=stroke)
        else:
            box = self.pen.textbbox((x * s, y * s), text, font=font, anchor=anchor, stroke_width=stroke)
        return tuple(v / s for v in box)

    @staticmethod
    def _spacing(font) -> float:
        """Pillow's gap between lines, so that they are LINE_SPACING font sizes apart (it adds the height of an "A")."""
        return font.size * LINE_SPACING - font.getbbox("A", anchor="la")[3]

    def text(self, text: str, x: float, y: float, size: float, colour: str, weight: str = "normal", ha: str = "left",
             va: str = "baseline", record: bool = True) -> tuple[float, float, float, float]:
        """Write `text` at (x, y): `ha` left / center / right of it, `va` top / center / baseline / bottom (a line break starts
        another line). Returns its extent in pixels."""
        font, stroke = self.font(size, weight)
        anchor = ANCHOR_H[ha] + ANCHOR_V[va if "\n" not in text or va != "baseline" else "top"]
        s = self.s
        if "\n" in text:
            self.pen.multiline_text((x * s, y * s), text, fill=colour, font=font, anchor=anchor, spacing=self._spacing(font),
                                    align=ha if ha != "center" else "center", stroke_width=stroke, stroke_fill=colour)
        else:
            self.pen.text((x * s, y * s), text, fill=colour, font=font, anchor=anchor, stroke_width=stroke, stroke_fill=colour)
        box = self.text_box(text, x, y, size, weight, ha, va)
        if record:
            self.texts.append(Text(text, x, y, ha, va, box, self.current))
        return box

    # ----- out -----
    def png(self) -> bytes:
        """The picture at its own size, as a PNG of at most 256 colours."""
        return png(self.image.reduce(self.s) if self.s > 1 else self.image)


def _dashes(points, on: float, off: float):
    """The pieces (start, end) of a dotted line along the points: `on` px drawn, `off` px left, repeating."""
    pieces, drawing, left = [], True, on
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        length = math.hypot(x1 - x0, y1 - y0)
        done = 0.0
        while done < length:
            step = min(left, length - done)
            a = done / length
            b = (done + step) / length
            if drawing:
                pieces.append(((x0 + (x1 - x0) * a, y0 + (y1 - y0) * a), (x0 + (x1 - x0) * b, y0 + (y1 - y0) * b)))
            done += step
            left -= step
            if left <= 1e-9:
                drawing = not drawing
                left = on if drawing else off
    return pieces


# ---------- PNG ----------
GROUPS = 250       # colours Pillow's octree picks; the rest of the 256 are kept for colours of an area it put in with another
AREA = 0.002       # a colour on this share of the pixels covers an area (a background, a line, a bar): it must come out exactly


def png(image: Image.Image) -> bytes:
    """An RGB picture as a PNG of at most 256 colours: well under half the size of a full-colour one. Pillow's octree groups the
    colours; each group is drawn in the commonest colour among its own pixels, and a colour that covers an area but shares a group
    with a commoner one gets an entry of its own. So the background, the cards, the lines and the bars come out exactly, and the
    in-between shades of anti-aliased edges within a few levels."""
    indexed = image.quantize(GROUPS, method=Image.Quantize.FASTOCTREE, dither=Image.Dither.NONE)
    index = Image.frombytes("L", indexed.size, indexed.tobytes())
    members: dict[int, list[tuple[int, tuple[int, int, int]]]] = {}
    for count, (r, g, b, i) in Image.merge("RGBA", (*image.split(), index)).getcolors(image.width * image.height):
        members.setdefault(i, []).append((count, (r, g, b)))
    flat = (indexed.getpalette() or []) + [0] * 768
    flat = flat[:768]
    spare = iter(range(GROUPS, 256))
    channels = image.split()
    for i, group in members.items():
        group.sort(reverse=True)
        flat[3 * i:3 * i + 3] = group[0][1]
        for count, colour in group[1:]:
            if count < AREA * image.width * image.height:
                break
            j = next(spare, None)
            if j is None:
                break
            mask = None
            for channel, value in zip(channels, colour):
                hit = channel.point([255 if v == value else 0 for v in range(256)])
                mask = hit if mask is None else ImageChops.multiply(mask, hit)
            index.paste(j, mask=mask)
            flat[3 * j:3 * j + 3] = colour
    out = Image.frombytes("P", index.size, index.tobytes())
    out.putpalette(flat)
    buf = io.BytesIO()
    out.save(buf, format="png", dpi=(DPI, DPI))
    return buf.getvalue()
