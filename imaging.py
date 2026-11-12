"""
Rendering pipeline for the /fakemessage command.

BLOCKING I/O - WHAT WAS WRONG AND WHAT CHANGED
------------------------------------------------
The original opened a brand new `aiohttp.ClientSession()` on every
single call, and did all of its Pillow work (font loading, text
wrapping/measuring, drawing, compositing, PNG encoding) inline in an
`async def`. Pillow is fully synchronous - none of that actually
yields to the event loop, so a render was really "block the loop for
however long Pillow takes" with `await` sprinkled around it.

Split, the same way database.py is split:
  - `_fetch_avatar_bytes` does the real network I/O (aiohttp) and
    stays on the event loop, using one shared `aiohttp.ClientSession`
    passed in from the bot (see main.py) instead of opening a new one
    per call.
  - Everything CPU-bound - decoding avatars, wrapping/measuring text,
    drawing, compositing, PNG-encoding - runs inside a single
    `asyncio.to_thread` dispatch from `render_fake_message`.

FONT FALLBACK
------------------------------------------------
The original loaded exactly one font (fonts/app_font.ttf) and fell
back to Pillow's built-in bitmap font if that file was missing. Any
character that font doesn't have a glyph for - e.g. the Unicode
"Mathematical Alphanumeric Symbols" block used by 𝖆𝖇𝖈-style bold
fraktur, 𝕒𝕓𝕔-style double-struck, or ᴀʙᴄ-style small-caps text - has
never been renderable here, no matter which single font is picked;
none of the usual "looks like Discord's font" TTFs cover those blocks.

`discover_fonts()` now scans `fonts/` for *every* .ttf/.otf dropped in
there (via the .addfont command or by hand) and reads each one's cmap
table (fontTools) once, up front, into a set of the Unicode code
points it covers. When drawing, each character is resolved against
that list in order - first font that actually has a glyph for it wins
- so text can freely mix scripts/styles across whatever fonts are
loaded, without the caller ever picking a font by hand. `app_font.ttf`
is always tried first (for a consistent look on ordinary text) and
its gaps are filled by whatever else is loaded, in alphabetical order.

Note this only *renders* code points a loaded font actually covers -
it can't invent glyphs. For full coverage of the exotic Unicode blocks
above, drop in a font that has them (e.g. Noto Sans Math or Symbola,
both well known for exactly this kind of broad coverage) via
`.addfont`. With nothing covering a given character, the primary font
is used anyway so something still renders (typically a "tofu" box)
instead of the character silently vanishing.
"""
import asyncio
import colorsys
import io
import math
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

LONDON_TZ = ZoneInfo('Europe/London')
from typing import Optional

import aiohttp
from PIL import Image, ImageDraw, ImageFont

DISCORD_GREY = (49, 51, 56)  # default background if no bg_color is given
FONTS_DIR = "fonts"

# How much every background-related hex (bg_color, third_gradient's
# color - never name_color) gets darkened before painting: HSL
# lightness is reduced by this fraction, so 0.5 means the result keeps
# 50% of the original lightness. See darken_hex_color().
BG_DARKEN_FACTOR = 0.5

# Display scale for text/name/avatar. These were sized for a much
# smaller canvas and ended up looking lost against the CARD_WIDTH x
# MIN_HEIGHT floor - tripling them (300%) makes them actually fill the
# frame instead of floating in a sea of background color. CARD_WIDTH/
# MIN_HEIGHT/MAX_HEIGHT are deliberately left unscaled: the point is
# for content to occupy more of that same canvas, not for the canvas
# to grow just as much and cancel the gain out
SCALE = 3
NAME_SIZE = 18 * SCALE
MSG_SIZE = 17 * SCALE
REPLY_SIZE = 14 * SCALE

# ------------------------------------------------------- dynamic sizing
#
# CARD_WIDTH is now a soft cap on the *text area*, not the final
# canvas width: the canvas grows to fit whatever's actually in it
# (name / message / avatar), then gets WIDTH_PAD_FACTOR (1.25x)
# breathing room on top - so a short message gets a snugly-sized card
# with 25% background padding, not a fixed-width column with a lot of
# empty space to the right. A message wide enough to exceed CARD_WIDTH
# still wraps at that cap, same as before; only what happens beneath
# the cap changed. Height is unrelated: MIN_HEIGHT (360) is a floor
# only - content needing more than MAX_HEIGHT (a "1440p" soft target)
# is never truncated to fit; it's just allowed to make a taller image
# instead of losing text.

CARD_WIDTH = 960
WIDTH_PAD_FACTOR = 1.25
MIN_WIDTH = 400  # floor so a one-word message doesn't shrink to a sliver
MIN_HEIGHT, MAX_HEIGHT = 360, 1440

# The background is rendered at BG_SOURCE_SCALE x the size actually
# used (see build_background_crop). BG_SOURCE_SCALE is anchored to
# CARD_WIDTH (960) even though the real per-render width now varies -
# it's just a fixed ratio, not tied to any particular render's actual
# width. 1440p-scale (~2.667x, vs. a flat 4x/4K) keeps more of a
# gradient's range inside the visible crop instead of it reading as
# one flat color; see build_background_crop for the full explanation.
BG_SOURCE_SCALE = 2560 / CARD_WIDTH

NAME_ROW_H = 24 * SCALE
LINE_H = 22 * SCALE
REPLY_ROW_H = 26 * SCALE
REPLY_CHAR_LIMIT = 90  # character count, not a pixel size - stays unscaled
REPLY_AVATAR_SIZE = 18 * SCALE
REPLY_AVATAR_TEXT_GAP = 26 * SCALE

# Discord's decoration frames are drawn on a canvas larger than the
# avatar itself so the frame can extend past the circular edge -
# 1.28x centered over the avatar matches how the official client
# overlays it.
DECORATION_SCALE = 1.28

# --------------------------------------------------------- inline markdown
#
# None of this needs a dedicated bold/italic font file - both are
# faked from whatever regular-weight font the character-fallback
# chain already resolves to: bold via a thin stroke traced around the
# glyph outline (a standard Pillow trick), italic by rendering to a
# scratch image and shearing it. See _draw_run/_draw_sheared_text.
ITALIC_SHEAR = 0.22  # ~12 degrees - matches a typical italic slant
BOLD_STROKE_FACTOR = 0.045  # stroke width as a fraction of font size
CODE_CHIP_COLOR = (0, 0, 0, 70)  # translucent so it still reads on any bg_color
CODE_CHIP_PAD_X_FACTOR = 0.28
CODE_CHIP_PAD_Y_FACTOR = 0.24
CODE_CHIP_RADIUS_FACTOR = 0.28

# A looping gradient name_color ("rrggbb-rrggbb") cycles smoothly
# between its two colors every this many pixels, horizontally, from
# the start of the name - see render_gradient_name.
NAME_GRADIENT_PERIOD = 150

# Discord's own muted grey for the compact timestamp shown beside a
# username - see format_display_datetime / the date option.
TIMESTAMP_COLOR = (148, 155, 164)


# ------------------------------------------------------------ display date/time
#
# Parses the fakemessage `date` option into an actual datetime, then
# formats it back the way Discord itself shows a compact timestamp
# beside a username. Deliberately forgiving - a value that doesn't
# match any recognized shape just falls back to "now" instead of
# failing the whole render over a typo'd timestamp.
#
# Accepted input, case-insensitively and whitespace-tolerant:
#   (blank/omitted)      -> right now
#   "10/09/2026 22:29"   -> that date (DD/MM/YYYY - day first), 24h time
#   "22:29"              -> today, 24h time
#   "2229"               -> today, 24h time, no colon
#   "10:29pm"/"10:29 pm" -> today, 12h time (am/pm, optional space)
#
# Note on "10/09/2026": this reads it as day/month/year (10 September),
# not month/day - flag it if you actually wanted US-style MM/DD, it's
# a one-line swap in _DATE_TIME_RE's group order.
_DATE_TIME_RE = re.compile(r'^(\d{1,2})/(\d{1,2})/(\d{4})\s+(\d{1,2}):(\d{2})\s*([ap]\.?m\.?)?$', re.IGNORECASE)
_TIME_12H_RE = re.compile(r'^(\d{1,2}):(\d{2})\s*([ap]\.?m\.?)$', re.IGNORECASE)
_TIME_24H_COLON_RE = re.compile(r'^(\d{1,2}):(\d{2})$')
_TIME_24H_COMPACT_RE = re.compile(r'^(\d{3,4})$')


def _to_24h(hour: int, meridiem: str) -> int:
    m = meridiem.lower().replace(".", "")
    if m == "pm" and hour != 12:
        hour += 12
    elif m == "am" and hour == 12:
        hour = 0
    return hour


def parse_display_datetime(raw: Optional[str]) -> datetime:
    if raw is None or not raw.strip():
        return datetime.now(LONDON_TZ)
    s = raw.strip()

    m = _DATE_TIME_RE.match(s)
    if m:
        day, month, year, hour, minute, meridiem = m.groups()
        hour = _to_24h(int(hour), meridiem) if meridiem else int(hour)
        try:
            return datetime(int(year), int(month), int(day), hour, int(minute))
        except ValueError:
            return datetime.now(LONDON_TZ)

    m = _TIME_12H_RE.match(s)
    if m:
        hour_s, minute_s, meridiem = m.groups()
        hour = _to_24h(int(hour_s), meridiem)
        try:
            return datetime.now(LONDON_TZ).replace(hour=hour, minute=int(minute_s), second=0, microsecond=0)
        except ValueError:
            return datetime.now(LONDON_TZ)

    m = _TIME_24H_COLON_RE.match(s)
    if m:
        try:
            return datetime.now(LONDON_TZ).replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
        except ValueError:
            return datetime.now(LONDON_TZ)

    m = _TIME_24H_COMPACT_RE.match(s)
    if m:
        digits = m.group(1).zfill(4)
        try:
            return datetime.now(LONDON_TZ).replace(hour=int(digits[:2]), minute=int(digits[2:]), second=0, microsecond=0)
        except ValueError:
            return datetime.now(LONDON_TZ)

    return datetime.now(LONDON_TZ)


def format_display_datetime(dt: datetime) -> str:
    """Bare 'H:MM AM/PM' for a timestamp from today (matching how
    Discord's own compact view shows today's messages), or
    'DD/MM/YYYY H:MM AM/PM' for any other date."""
    time_str = dt.strftime("%I:%M %p")
    if time_str.startswith("0"):
        time_str = time_str[1:]
    if dt.date() == datetime.now(LONDON_TZ).date():
        return time_str
    return f"{dt.strftime('%d/%m/%Y')} {time_str}"


# ------------------------------------------------------------ font fallback

@dataclass
class FontFile:
    path: str
    name: str
    coverage: set = field(default_factory=set)
    bitmap_sizes: Optional[list] = None  # fixed strike sizes (ppem) for color/bitmap fonts; None = normal scalable font


_font_files: list[FontFile] = []
_ttfont_cache: dict = {}
_bitmap_sizes_by_path: dict = {}


def _load_bitmap_strike_sizes(path: str) -> Optional[list]:
    """Sorted list of fixed pixel sizes (ppem) this font can actually
    be loaded at, for color/bitmap emoji fonts (CBLC/CBDT, or sbix)
    that only embed a handful of pre-rendered strikes rather than a
    scalable outline - asking FreeType for any other size raises
    OSError('invalid pixel size'). None means this is a normal
    scalable font with no such restriction."""
    try:
        from fontTools.ttLib import TTFont
        tt = TTFont(path, lazy=True, fontNumber=0)
        try:
            if "CBLC" in tt:
                sizes = sorted({s.bitmapSizeTable.ppemX for s in tt["CBLC"].strikes})
                return sizes or None
            if "sbix" in tt:
                sizes = sorted({s.ppem for s in tt["sbix"].strikes})
                return sizes or None
            return None
        finally:
            tt.close()
    except Exception:
        return None


def _load_cmap_coverage(path: str) -> Optional[set]:
    """Set of Unicode code points this font has glyphs for, read from
    its cmap table. None (not an empty set) means "couldn't parse this
    file at all" so the caller can skip it instead of silently keeping
    a dead entry around."""
    try:
        from fontTools.ttLib import TTFont
        tt = TTFont(path, lazy=True, fontNumber=0)
        try:
            cmap = tt.getBestCmap()
        finally:
            tt.close()
        return set(cmap.keys()) if cmap else set()
    except Exception:
        return None


def discover_fonts():
    """(Re)scans fonts/ for .ttf/.otf files and rebuilds the fallback
    priority list. Synchronous (parses every font file's cmap) - call
    through asyncio.to_thread from cog code. Safe to call anytime,
    including with fonts already loaded (e.g. after .addfont); this is
    also called automatically when the Fun cog loads/reloads.

    app_font.ttf - the original single "looks like Discord" font - is
    always tried first if present, so ordinary text keeps the same
    look as before; anything else in the folder fills in gaps
    alphabetically after it.
    """
    global _font_files, _ttfont_cache, _bitmap_sizes_by_path
    _ttfont_cache = {}  # sizes are keyed by path; rebuild fresh rather than mutate in place
    _bitmap_sizes_by_path = {}
    _reset_mono_font_cache()

    if not os.path.isdir(FONTS_DIR):
        _font_files = []
        return

    candidates = [
        f for f in sorted(os.listdir(FONTS_DIR))
        if f.lower().endswith((".ttf", ".otf"))
    ]
    candidates.sort(key=lambda f: (f != "app_font.ttf", f.lower()))

    files = []
    for fname in candidates:
        path = os.path.join(FONTS_DIR, fname)
        coverage = _load_cmap_coverage(path)
        if coverage is None:
            print(f"[imaging] Skipping unreadable font file: {fname}")
            continue
        bitmap_sizes = _load_bitmap_strike_sizes(path)
        if bitmap_sizes:
            _bitmap_sizes_by_path[path] = bitmap_sizes
        files.append(FontFile(path=path, name=fname, coverage=coverage, bitmap_sizes=bitmap_sizes))
    _font_files = files


def get_loaded_fonts() -> list:
    return list(_font_files)


def _get_sized_font(path: str, size: int):
    size = max(1, int(size))  # Prevent 0 or negative pixel sizes
    strike_sizes = _bitmap_sizes_by_path.get(path)
    if strike_sizes:
        # color/bitmap fonts (e.g. Apple's emoji font) only embed a
        # handful of fixed strikes - snap to the closest one instead
        # of asking FreeType for an arbitrary size it'll reject
        size = min(strike_sizes, key=lambda s: abs(s - size))
    key = (path, size)
    cached = _ttfont_cache.get(key)
    if cached is None:
        try:
            cached = ImageFont.truetype(path, size)
        except OSError:
            return None
        _ttfont_cache[key] = cached
    return cached


def _fallback_font(size: int):
    """Last resort when no loaded file covers a character, and the
    overall default when no fonts are loaded at all - mirrors the
    original's load_font()."""
    if _font_files:
        font = _get_sized_font(_font_files[0].path, size)
        if font is not None:
            return font
    return ImageFont.load_default()


def _resolve_font_for_char(ch: str, size: int):
    cp = ord(ch)
    for f in _font_files:
        if cp in f.coverage:
            font = _get_sized_font(f.path, size)
            if font is not None:
                return font
            # this font claims the glyph but can't render at this
            # size (e.g. a fixed-strike emoji font) - keep looking in
            # case another loaded font also covers it
    return None


def _segment_runs(text: str, size: int):
    """Splits text into consecutive runs that resolve to the same
    font, e.g. 'hi 𝖆𝖇𝖈 there' -> [(fontA, 'hi '), (fontB, '𝖆𝖇𝖈'), (fontA, ' there')].
    Keeps drawing/measuring to one call per run instead of per
    character."""
    if not text:
        return []
    runs = []
    current_font = None
    current_text = ""
    for ch in text:
        font = _resolve_font_for_char(ch, size) or _fallback_font(size)
        if font is current_font:
            current_text += ch
        else:
            if current_text:
                runs.append((current_font, current_text))
            current_font, current_text = font, ch
    if current_text:
        runs.append((current_font, current_text))
    return runs


def measure_text(text: str, size: int) -> float:
    return sum(font.getlength(segment) for font, segment in _segment_runs(text, size))


def draw_fallback_text(draw: ImageDraw.ImageDraw, xy, text: str, size: int, fill):
    x, y = xy
    for font, segment in _segment_runs(text, size):
        draw.text((x, y), segment, font=font, fill=fill, embedded_color=True)
        x += font.getlength(segment)


# --------------------------------------------------------- monospace (code)
#
# `code` spans (`` `like this` ``) get their own font-resolution chain
# that prefers an actual monospace face instead of app_font.ttf, so
# they visually read as code the way Discord's client renders them.
# Search order: (1) anything in fonts/ whose filename suggests it's
# monospace - drop in a file named e.g. "consolas.ttf" via .addfont
# and it's picked up automatically, same as app_font.ttf is; (2) a
# handful of monospace fonts commonly preinstalled on Debian/Ubuntu
# servers (DejaVu Sans Mono ships with the fonts-dejavu-core package
# and is very often already present). Falls back to the normal
# per-character chain for anything the mono font itself doesn't cover
# (e.g. CJK), so an exotic character in a code span still renders
# something instead of silently vanishing - it just won't be
# monospaced.
_SYSTEM_MONO_FONTS = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansMono-Regular.ttf",
)

_mono_font_path: Optional[str] = ""  # "" = not searched yet, None = searched, nothing found
_mono_coverage_cache: dict = {}


def _reset_mono_font_cache():
    global _mono_font_path, _mono_coverage_cache
    _mono_font_path = ""
    _mono_coverage_cache = {}


def _find_mono_font() -> Optional[str]:
    global _mono_font_path
    if _mono_font_path != "":
        return _mono_font_path
    for f in _font_files:
        lname = f.name.lower()
        if "consolas" in lname or "mono" in lname:
            _mono_font_path = f.path
            return _mono_font_path
    for candidate in _SYSTEM_MONO_FONTS:
        if os.path.isfile(candidate):
            _mono_font_path = candidate
            return _mono_font_path
    _mono_font_path = None
    return None


def _mono_font_coverage() -> set:
    path = _find_mono_font()
    if not path:
        return set()
    coverage = _mono_coverage_cache.get(path)
    if coverage is None:
        coverage = _load_cmap_coverage(path) or set()
        _mono_coverage_cache[path] = coverage
    return coverage


def _resolve_mono_font(ch: str, size: int):
    path = _find_mono_font()
    if path and ord(ch) in _mono_font_coverage():
        font = _get_sized_font(path, size)
        if font is not None:
            return font
    return _resolve_font_for_char(ch, size) or _fallback_font(size)


def _segment_mono_runs(text: str, size: int):
    """Same idea as _segment_runs, but resolving each character
    through _resolve_mono_font instead of the ordinary chain."""
    if not text:
        return []
    runs = []
    current_font = None
    current_text = ""
    for ch in text:
        font = _resolve_mono_font(ch, size)
        if font is current_font:
            current_text += ch
        else:
            if current_text:
                runs.append((current_font, current_text))
            current_font, current_text = font, ch
    if current_text:
        runs.append((current_font, current_text))
    return runs


def _code_text_width(text: str, size: int) -> float:
    return sum(font.getlength(seg) for font, seg in _segment_mono_runs(text, size))


def _draw_code_text(draw: ImageDraw.ImageDraw, xy, text: str, size: int, fill):
    x, y = xy
    for font, seg in _segment_mono_runs(text, size):
        draw.text((x, y), seg, font=font, fill=fill, embedded_color=True)
        x += font.getlength(seg)


# ------------------------------------------------------------- background

def parse_hex_color(text):
    text = text.strip().lstrip('#')
    if len(text) != 6:
        return None
    try:
        return tuple(int(text[i:i+2], 16) for i in (0, 2, 4))
    except ValueError:
        return None


def darken_hex_color(hex_str: str, factor: float = None) -> str:
    """Darkens a hex color by reducing its HSL lightness by `factor`
    (0.4 -> the result keeps 60% of the original lightness). Hue and
    saturation are left alone, so a color gets darker without shifting
    hue or washing out toward grey. Returns a bare 6-digit hex string
    (no '#'), matching what parse_hex_color expects everywhere else in
    this module. Input that isn't a valid hex color is returned
    unchanged, same as parse_hex_color's "give up quietly" behavior."""
    if factor is None:
        factor = BG_DARKEN_FACTOR
    rgb = parse_hex_color(hex_str)
    if rgb is None:
        return hex_str
    r, g, b = (c / 255.0 for c in rgb)
    h, l, s = colorsys.rgb_to_hls(r, g, b)  # colorsys: Hue, Lightness, Saturation
    l = max(0.0, min(1.0, l * (1.0 - factor)))
    r2, g2, b2 = colorsys.hls_to_rgb(h, l, s)
    return "".join(f"{max(0, min(255, round(c * 255))):02x}" for c in (r2, g2, b2))


def _darken_bg_color_input(bg_color_input: Optional[str], factor: float = None):
    """Darkens bg_color's hex value(s) before it's parsed and painted -
    either a single solid color, or both stops of an 'rrggbb-rrggbb'
    gradient. Returns (new_bg_color_input, report_rows), where
    report_rows is a list of (label, orig_hex, dark_hex) - empty if
    there was nothing valid to darken."""
    if not bg_color_input:
        return bg_color_input, []
    if "-" in bg_color_input:
        left, right = bg_color_input.split("-", 1)
        rows = []
        new_left, new_right = left, right
        if parse_hex_color(left) is not None:
            new_left = darken_hex_color(left, factor)
            rows.append(("gradient start", left.strip().lstrip('#'), new_left))
        if parse_hex_color(right) is not None:
            new_right = darken_hex_color(right, factor)
            rows.append(("gradient end", right.strip().lstrip('#'), new_right))
        return f"{new_left}-{new_right}", rows
    if parse_hex_color(bg_color_input) is not None:
        dark = darken_hex_color(bg_color_input, factor)
        return dark, [("background", bg_color_input.strip().lstrip('#'), dark)]
    return bg_color_input, []


def _darken_third_gradient_input(third_gradient_input: Optional[str], factor: float = None):
    """Darkens just the hex segment of a third_gradient string (one of
    up to three dash-separated parts - see parse_third_gradient),
    leaving its position/intensity numbers untouched. Returns
    (new_third_gradient_input, report_rows)."""
    if not third_gradient_input:
        return third_gradient_input, []
    parts = third_gradient_input.split("-")
    rows = []
    new_parts = []
    for p in parts:
        stripped = p.strip()
        if parse_hex_color(stripped) is not None:
            dark = darken_hex_color(stripped, factor)
            rows.append(("third gradient", stripped.lstrip('#'), dark))
            new_parts.append(dark)
        else:
            new_parts.append(p)
    return "-".join(new_parts), rows


def _luminance(rgb) -> float:
    r, g, b = rgb
    return 0.299 * r + 0.587 * g + 0.114 * b


def ensure_readable(fg: tuple, bg: tuple) -> tuple:
    """Darkens `fg` if it's too close in brightness to `bg` to read
    clearly - e.g. white text (FFFFFF) on a white background (FFFFFF)
    comes out as a legible grey instead of vanishing into it.

    Only steps in when fg isn't already at least 50% darker than bg
    (measured as luminance distance vs. bg's own luminance); a color
    that's already comfortably distinct - including the normal case
    here of light text on the default dark grey, which is *lighter*
    than bg, not darker, but already reads perfectly fine - is left
    completely untouched.
    """
    fg_l, bg_l = _luminance(fg), _luminance(bg)
    if abs(fg_l - bg_l) >= bg_l * 0.5:
        return fg
    r, g, b = fg
    return (int(r * 0.8), int(g * 0.8), int(b * 0.8))


def _base_gradient_color_at(x: float, width: int, color_input: Optional[str]) -> tuple:
    """The base (pre third-gradient) background color at a given x
    column - solid color, or the horizontal-gradient blend at that x -
    matching what build_background()'s base layer actually paints
    there."""
    if not color_input:
        return DISCORD_GREY
    if "-" in color_input:
        left, right = color_input.split("-", 1)
        c1, c2 = parse_hex_color(left), parse_hex_color(right)
        if c1 and c2:
            if width <= 1:
                return c1
            t = max(0.0, min(1.0, x / (width - 1)))
            return tuple(int(round(c1[i] + (c2[i] - c1[i]) * t)) for i in range(3))
        return DISCORD_GREY
    return parse_hex_color(color_input) or DISCORD_GREY


def _third_gradient_strength_at(x: float, width: int, third_gradient_input: Optional[str],
                                 spread_ref_width: Optional[int] = None):
    """(color, strength 0..1) of the third-gradient overlay at a given
    x column, using the same boosted smoothstep curve
    `_third_gradient_mask` paints with - so this sees the same local
    strength the actual pixels there end up with. None if there's no
    third gradient (or it's unparseable).

    spread_ref_width: what "intensity %" is measured against for the
    *spread* (how far the stop's influence reaches), independent of
    `width`, which position % is always measured against. Defaults to
    `width` itself (the original, single-width behavior) when not
    given - see build_background for why build_background_crop passes
    something different here."""
    if not third_gradient_input:
        return None
    parsed = parse_third_gradient(third_gradient_input, width)
    if not parsed:
        return None
    px, color, intensity_pct = parsed
    spread_ref = width if spread_ref_width is None else spread_ref_width
    spread_px = _third_gradient_spread_px(intensity_pct, spread_ref)
    dist = abs(x - px)
    if spread_px <= 0:
        return (color, 1.0) if round(x) == px else (color, 0.0)
    if dist >= spread_px:
        return (color, 0.0)
    t = dist / spread_px
    eased = 1.0 - (t * t * (3.0 - 2.0 * t))
    eased = min(1.0, eased * 1.5)
    return (color, eased)


def local_bg_color(x: float, width: int, bg_color_input: Optional[str], third_gradient_input: Optional[str],
                    spread_ref_width: Optional[int] = None) -> tuple:
    """The actual effective background color at a given x column,
    accounting for both the base (possibly gradient) color and the
    third-gradient overlay - used so text contrast is checked against
    what's really drawn behind it, not just the base color at the far
    left edge. A dark third-gradient stripe sitting right where the
    name/message happens to fall is exactly the case a left-edge-only
    reference would get wrong.

    spread_ref_width is passed straight through to
    _third_gradient_strength_at - callers must pass the same value
    they rendered the actual background with, or this will describe a
    stripe wider or narrower than the one really behind the text."""
    base = _base_gradient_color_at(x, width, bg_color_input)
    third = _third_gradient_strength_at(x, width, third_gradient_input, spread_ref_width)
    if third is None:
        return base
    color, strength = third
    if strength <= 0:
        return base
    return tuple(int(round(base[i] + (color[i] - base[i]) * strength)) for i in range(3))


def _round_half_up(value: float) -> int:
    """Standard round-half-up (356.4 -> 356, 356.5 -> 357) - Python's
    builtin round() uses round-half-to-even instead, which would round
    356.5 down to 356."""
    return int(value + 0.5)


# third_gradient's intensity number (1-100) used to mean "spread as a
# fraction of half the reference width" (100 -> spread_ref/2). It now
# means two linked things instead: first, 1-100 maps straight onto a
# 0.01-1.0 fraction (intensity/100); second, that fraction multiplies
# the *reference width itself* (not halved), but with the reference
# clamped into [SPREAD_REF_MIN, SPREAD_REF_MAX] first. The clamp is
# what makes this "dynamic" without being erratic: card width is
# content-driven now (see CARD_WIDTH/WIDTH_PAD_FACTOR), so the same
# intensity value would otherwise produce a barely-there spread on a
# tiny one-word card and a wildly different one on a card stretched
# wide by a long name. Clamping the reference keeps intensity's actual
# on-screen effect consistent across that whole range instead.
SPREAD_REF_MIN = 720
SPREAD_REF_MAX = 1440

# Dropping the old /2 halving means intensity 100 now reaches the FULL
# clamped reference width (up to 1440px) - on a card anywhere near
# typical widths (~700-1200px) that's wide enough to blow past both
# edges at once, reading as an even glow/vignette rather than a
# gradient with a visible center. SPREAD_SCALE is a pure visual-tuning
# knob on top of the width-linked math above: it shrinks the whole
# curve down without changing how it scales with width or intensity,
# so turning it down further (or back up) just makes every spread
# uniformly smaller/larger, not differently-shaped.
SPREAD_SCALE = 0.6


def _third_gradient_spread_px(intensity_pct: float, spread_ref: float) -> float:
    """The single source of truth for turning a parsed intensity
    percentage into an actual pixel spread - used by both
    _third_gradient_strength_at (contrast checking) and
    build_background (actual pixel rendering), which must always
    agree on this number or text contrast gets checked against a
    stripe wider or narrower than what's really painted."""
    clamped_ref = max(SPREAD_REF_MIN, min(SPREAD_REF_MAX, spread_ref))
    return (intensity_pct / 100.0) * clamped_ref * SPREAD_SCALE


def _horizontal_gradient_mask(width, height):
    """An 'L' mode mask that goes 0 (black) at x=0 to 255 (white) at
    x=width-1, constant down every column - used to blend two colors
    left-to-right instead of Pillow's default top-to-bottom."""
    if width <= 1:
        return Image.new("L", (max(width, 1), max(height, 1)), 0)
    row = bytes(int(255 * x / (width - 1)) for x in range(width))
    line = Image.frombytes("L", (width, 1), row)
    return line.resize((width, height), Image.NEAREST)


def _looping_gradient_mask(width, height, period):
    """An 'L' mode mask that cycles smoothly between 0 and 255 every
    `period` pixels horizontally (a cosine wave, so the blend is
    continuous the whole way round the loop - there's never a hard
    seam to "feather", unlike a flat two-tone stripe would need).
    Constant down every column, same convention as
    _horizontal_gradient_mask - used the same way, just repeating."""
    if width <= 0 or period <= 0:
        return Image.new("L", (max(width, 1), max(height, 1)), 0)
    row = bytes(
        round(255 * (1 - math.cos(2 * math.pi * (x % period) / period)) / 2)
        for x in range(width)
    )
    line = Image.frombytes("L", (width, 1), row)
    return line.resize((width, height), Image.NEAREST)


def parse_name_color(name_color: Optional[str]):
    """Parses the fakemessage name_color option. Mirrors bg_color's
    own 'rrggbb-rrggbb' convention: two valid hexes separated by a
    dash means a gradient instead of a flat fill. Returns
    ('solid', (r,g,b)), ('gradient', ((r,g,b), (r,g,b))), or None if
    name_color is empty or not valid hex."""
    if not name_color:
        return None
    if "-" in name_color:
        left, right = name_color.split("-", 1)
        c1, c2 = parse_hex_color(left), parse_hex_color(right)
        return ("gradient", (c1, c2)) if (c1 and c2) else None
    c1 = parse_hex_color(name_color)
    return ("solid", c1) if c1 else None


def render_gradient_name(text: str, size: int, color1, color2, period: int = NAME_GRADIENT_PERIOD) -> Image.Image:
    """Renders `text` (through the normal multi-font Unicode-fallback
    chain, same as draw_fallback_text) with a smooth looping
    horizontal gradient between color1 and color2 - one full
    color1->color2->color1 cycle every `period` pixels, starting from
    the left edge of the name so color1 always leads, matching
    whichever order the two hexes were given in - instead of a flat
    fill. Contrast-adjustment against the background (see
    ensure_readable elsewhere in this module) is deliberately skipped
    here: nudging either of two deliberately-chosen gradient stops
    for contrast would fight the exact look being asked for. Returns
    an RGBA image sized to fit the text, alpha-masked to the glyph
    shapes - paste it with itself as the mask."""
    width = max(1, round(measure_text(text, size)) + 4)
    height = round(size * 1.6)
    glyphs = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw_fallback_text(ImageDraw.Draw(glyphs), (0, 0), text, size, (255, 255, 255))
    alpha = glyphs.split()[3]

    mask = _looping_gradient_mask(width, height, period)
    start = Image.new("RGB", (width, height), color1)
    end = Image.new("RGB", (width, height), color2)
    gradient = Image.composite(end, start, mask).convert("RGBA")
    gradient.putalpha(alpha)
    return gradient


def _third_gradient_mask(width, height, center_x, spread_px):
    """An 'L' mask peaking at 255 on the center_x column and smoothly
    (smoothstep) falling to 0 over spread_px pixels on either side.
    spread_px is how far the third color's influence reaches - a
    bigger spread means more of the canvas gets pulled toward it."""
    if width <= 0 or height <= 0:
        return Image.new("L", (max(width, 1), max(height, 1)), 0)

    row = bytearray(width)
    if spread_px <= 0:
        if 0 <= center_x < width:
            row[center_x] = 255
    else:
        for x in range(width):
            dist = abs(x - center_x)
            if dist >= spread_px:
                row[x] = 0
            else:
                t = dist / spread_px
                eased = 1.0 - (t * t * (3.0 - 2.0 * t))
                eased = min(1.0, eased * 1.5)  # +50% effectiveness: stays at full strength over more of the spread before falling off
                row[x] = max(0, min(255, int(round(eased * 255))))
    line = Image.frombytes("L", (width, 1), bytes(row))
    return line.resize((width, height), Image.NEAREST)


def parse_third_gradient(text: str, width: int):
    """Parses the `third_gradient` option into (pixel_x, (r,g,b), intensity_pct).

    Accepted forms (a leading '#' on the hex is fine):
      "HEX"                -> position 50%, intensity 50%
      "POS-HEX"            -> position POS%, intensity 50%          e.g. "33-000000"
      "HEX-INTENSITY"      -> position 50%, intensity INTENSITY%    e.g. "ffffff-20"
      "POS-HEX-INTENSITY"  -> position POS%, intensity INTENSITY%   e.g. "40-121212-70"

    POS/INTENSITY are 0-100 percentages, clamped into that range; the
    hex segment is identified by actually being a valid 6-digit hex
    color rather than by its position in the string, so "POS-HEX" and
    "HEX-INTENSITY" can share the same 2-part shape without ambiguity.
    Position is converted from a percentage of `width` into a pixel
    column with round-half-up. INTENSITY controls the stop's *spread*
    (how far its influence reaches) rather than a position - see
    _third_gradient_spread_px for how the 1-100 value actually becomes
    a pixel distance. Returns None if nothing in the string resolves
    to a valid color.
    """
    if not text:
        return None
    parts = [p.strip() for p in text.split("-")]

    def is_hex(p):
        return parse_hex_color(p) is not None

    def pct(p, default):
        if p is None:
            return default
        try:
            val = float(p)
        except ValueError:
            return default
        return max(0.0, min(100.0, val))

    hex_str = pos_str = intensity_str = None

    if len(parts) == 1:
        hex_str = parts[0]
    elif len(parts) == 2:
        a, b = parts
        if is_hex(a):
            hex_str, intensity_str = a, b
        elif is_hex(b):
            pos_str, hex_str = a, b
        else:
            return None
    else:
        hex_index = next((i for i, p in enumerate(parts) if is_hex(p)), None)
        if hex_index is None:
            return None
        hex_str = parts[hex_index]
        rest = [p for i, p in enumerate(parts) if i != hex_index]
        pos_str = rest[0] if len(rest) > 0 else None
        intensity_str = rest[1] if len(rest) > 1 else None

    color = parse_hex_color(hex_str)
    if color is None:
        return None

    pos_pct = pct(pos_str, 50.0)
    intensity_pct = pct(intensity_str, 50.0)

    px = _round_half_up((pos_pct / 100.0) * width)
    px = max(0, min(width - 1, px))
    return px, color, intensity_pct


def build_background(width, height, color_input, third_gradient_input=None, third_gradient_spread_ref_width=None):
    """Solid color by default. Supports 'rrggbb-rrggbb' for a
    left-to-right gradient, plus an optional third gradient stop
    layered on top (see parse_third_gradient) that smoothly blends in
    around a given x position.

    third_gradient_spread_ref_width: see _third_gradient_strength_at -
    lets a caller measure the stop's *spread* against a different
    width than its *position*. Defaults to `width` (old behavior)."""
    if not color_input:
        base = Image.new("RGB", (width, height), DISCORD_GREY)
    elif "-" in color_input:
        left, right = color_input.split("-", 1)
        c1, c2 = parse_hex_color(left), parse_hex_color(right)
        if c1 and c2:
            start = Image.new("RGB", (width, height), c1)
            end = Image.new("RGB", (width, height), c2)
            mask = _horizontal_gradient_mask(width, height)
            base = Image.composite(end, start, mask)
        else:
            base = Image.new("RGB", (width, height), DISCORD_GREY)
    else:
        base = Image.new("RGB", (width, height), parse_hex_color(color_input) or DISCORD_GREY)

    if third_gradient_input:
        parsed = parse_third_gradient(third_gradient_input, width)
        if parsed:
            px, color, intensity_pct = parsed
            spread_ref = width if third_gradient_spread_ref_width is None else third_gradient_spread_ref_width
            spread_px = _third_gradient_spread_px(intensity_pct, spread_ref)
            mask = _third_gradient_mask(width, height, px, spread_px)
            overlay = Image.new("RGB", (width, height), color)
            base = Image.composite(overlay, base, mask)

    return base


def _crop_box(src_w: int, src_h: int, crop_w: int, crop_h: int):
    """Pixel box (left, top, right, bottom) for a crop_w x crop_h slice
    taken from the bottom-left corner of a src_w x src_h image. Split
    out from build_background_crop so the coordinate math can be unit
    tested without touching Pillow at all. Clamps crop_h to src_h so a
    request for more rows than exist never produces a negative top
    (the caller is expected to size src_h from crop_h in the first
    place - see build_background_crop - so this is a safety net, not
    the normal path)."""
    crop_h = min(crop_h, src_h)
    left = 0
    right = min(crop_w, src_w)
    bottom = src_h
    top = src_h - crop_h
    return left, top, right, bottom


def build_background_crop(crop_width: int, crop_height: int, color_input, third_gradient_input=None):
    """Renders the existing background system (gradient + third_gradient
    above - unchanged) at BG_SOURCE_SCALE x the requested size, then
    crops a crop_width x crop_height slice from the bottom-left corner.
    The visible card ends up looking like a small window onto a much
    bigger rendered canvas, instead of a gradient painted to exactly
    fit the card.

    Because this is a crop, not a resize, nothing is ever stretched -
    but it does mean bg_color's gradient stops and third_gradient's
    *position* percentage are measured against the FULL source width
    (crop_width * BG_SOURCE_SCALE), not the visible crop_width. E.g.
    with the default BG_SOURCE_SCALE (~2.667, a 1440p-scale source),
    only the leftmost ~37.5% of a bg_color gradient's colour range -
    and only a third_gradient positioned in roughly that same first
    portion - ever falls inside the visible crop, unless its spread is
    wide enough to bleed back into frame. That's the intended effect
    (a peek into a bigger canvas) and not a bug.

    third_gradient's *intensity* (spread), by contrast, is deliberately
    measured against crop_width, not the source width - intensity is
    meant to control how much of the visible card the stop covers, and
    if it scaled with the hidden larger source instead, a fairly low
    intensity would already be wide enough to flood the entire visible
    crop with solid color, making the option nearly unusable. crop_width
    itself then gets clamped into [SPREAD_REF_MIN, SPREAD_REF_MAX]
    before intensity is applied to it - see _third_gradient_spread_px -
    so intensity's effect stays consistent even though crop_width is
    now content-driven and varies per render instead of always being
    the old fixed CARD_WIDTH.

    Returns (cropped_image, src_width). Callers need src_width to pass
    into local_bg_color() as its `width` argument for text-contrast
    checks (position is still measured against the full source there
    too), and should also pass crop_width as local_bg_color's
    spread_ref_width so contrast is checked against the same spread
    the pixels were actually painted with.
    """
    # BG_SOURCE_SCALE isn't a whole number (1440p-scale, not 4K), so
    # round to the nearest pixel rather than truncating - Image.new
    # and everything downstream needs real ints, not floats.
    src_w = round(crop_width * BG_SOURCE_SCALE)
    src_h = round(crop_height * BG_SOURCE_SCALE)
    src = build_background(
        src_w, src_h, color_input, third_gradient_input,
        third_gradient_spread_ref_width=crop_width,
    )
    box = _crop_box(src_w, src_h, crop_width, crop_height)
    return src.crop(box), src_w


# ----------------------------------------------------- inline markdown

# Backtick spans are pulled out first, via regex, and never re-parsed
# for other markers - matches Discord/CommonMark treating inline code
# as literal content, and sidesteps the ambiguity of "*" appearing
# inside a code span. No support for nesting bold/italic *around* a
# code span (e.g. "**`x`**") - the code span is always plain code,
# never bold-code - which is a simplification but keeps the parser a
# lot simpler than a real CommonMark implementation.
_CODE_SPAN_RE = re.compile(r'`([^`]+)`')
_EMPHASIS_MARKERS = ('~~', '**', '__', '*', '_')  # longest-first, so "**" wins over two "*"
_MARKER_STYLE = {'~~': 'strike', '**': 'bold', '__': 'underline', '*': 'italic', '_': 'italic'}


def _tokenize_emphasis(text: str):
    """Splits text into ('text', s) / ('marker', s) tokens on the
    non-code markers. Longest marker checked first at each position so
    "**" is never seen as two separate "*" tokens."""
    tokens = []
    i, n = 0, len(text)
    buf = ""
    while i < n:
        matched = None
        for m in _EMPHASIS_MARKERS:
            if text.startswith(m, i):
                matched = m
                break
        if matched:
            if buf:
                tokens.append(['text', buf])
                buf = ""
            tokens.append(['marker', matched])
            i += len(matched)
        else:
            buf += text[i]
            i += 1
    if buf:
        tokens.append(['text', buf])
    return tokens


def _pair_emphasis_markers(tokens):
    """Matches each marker token to the most recent still-open marker
    of the same kind. Anything left unmatched at the end (a stray
    "**" with no closing partner) is converted back into a literal
    text token instead of silently swallowing the rest of the message
    into one giant styled run."""
    stack = []
    for idx, tok in enumerate(tokens):
        if tok[0] != 'marker':
            continue
        m = tok[1]
        if stack and stack[-1][0] == m:
            stack.pop()
        else:
            stack.append((m, idx))
    for m, idx in stack:
        tokens[idx][0] = 'text'
        tokens[idx][1] = m
    return tokens


def _parse_emphasis(text: str, base_styles: frozenset):
    """Bold/italic/underline/strike only - no backticks (those are
    already extracted by parse_markdown before this runs). Returns a
    list of (text, styles) runs, styles always including base_styles."""
    if not text:
        return []
    tokens = _pair_emphasis_markers(_tokenize_emphasis(text))
    active = []
    runs = []
    buf = ""

    def flush():
        if buf:
            runs.append((buf, base_styles | frozenset(_MARKER_STYLE[m] for m in active)))

    for tok in tokens:
        if tok[0] == 'text':
            buf += tok[1]
        else:
            flush()
            buf = ""
            m = tok[1]
            if active and active[-1] == m:
                active.pop()
            else:
                active.append(m)
    flush()
    return runs


def parse_markdown(text: str):
    """Parses Discord-style inline markdown into a list of
    (text, frozenset(styles)) runs, styles drawn from {'bold',
    'italic', 'underline', 'strike', 'code'} (a run can carry several
    at once, e.g. "**~~both~~**"). Marker characters themselves are
    never included in the output text. An unclosed marker (no
    matching partner anywhere in the message) falls back to literal
    text - see _pair_emphasis_markers.
    """
    if not text:
        return []
    runs = []
    pos = 0
    for m in _CODE_SPAN_RE.finditer(text):
        if m.start() > pos:
            runs.extend(_parse_emphasis(text[pos:m.start()], frozenset()))
        runs.append((m.group(1), frozenset({'code'})))
        pos = m.end()
    if pos < len(text):
        runs.extend(_parse_emphasis(text[pos:], frozenset()))
    return runs


def measure_styled_run(text: str, size: int, styles: frozenset) -> float:
    if not text:
        return 0.0
    if 'code' in styles:
        # The chip's own left/right padding is part of this run's
        # footprint - otherwise whatever follows immediately (e.g. a
        # comma with no space before it) would land underneath the
        # box's right-hand padding instead of after it. Note: during
        # wrapping this gets measured per word, so a multi-word code
        # span (which later renders as one continuous chip - see
        # _merge_adjacent_styled) has its padding counted once per
        # word rather than once overall. Harmless - it just means
        # such a span may wrap very slightly earlier than the exact
        # theoretical fit.
        return _code_text_width(text, size) + round(size * CODE_CHIP_PAD_X_FACTOR) * 2
    return sum(font.getlength(seg) for font, seg in _segment_runs(text, size))


def measure_styled_line(parts, size: int) -> float:
    return sum(measure_styled_run(t, size, s) for t, s in parts)


def _draw_sheared_text(canvas: Image.Image, x: float, y: float, segments, size: int, fill, stroke_w: int = 0) -> float:
    """Renders already-font-resolved `segments` (from _segment_runs or
    _segment_mono_runs - a list of (font, text) pairs) to a scratch
    RGBA image, then shears it to fake italics (regular TrueType files
    here have no dedicated italic variant), and alpha-composites the
    result onto `canvas` at (x, y). Returns the advance width the
    next run should start at: unsheared text width plus a small extra
    gap - the shear leans the *top* of each glyph to the right while
    its bottom stays put, so without a little breathing room the
    slanted top edge of a trailing letter visually runs into whatever
    comes right after it (most noticeable at a word boundary, since
    that's exactly where the next run starts drawing)."""
    total_w = sum(font.getlength(seg) for font, seg in segments)
    if total_w <= 0:
        return 0.0

    scratch_h = round(size * 1.8)  # generous - covers ascenders/descenders/stroke bleed
    scratch_w = int(total_w) + stroke_w * 2 + 4
    scratch = Image.new("RGBA", (scratch_w, scratch_h), (0, 0, 0, 0))
    sdraw = ImageDraw.Draw(scratch)
    sx = float(stroke_w)
    for font, seg in segments:
        if stroke_w:
            sdraw.text((sx, 0), seg, font=font, fill=fill, stroke_width=stroke_w, stroke_fill=fill, embedded_color=True)
        else:
            sdraw.text((sx, 0), seg, font=font, fill=fill, embedded_color=True)
        sx += font.getlength(seg)

    shear_px = int(scratch_h * ITALIC_SHEAR) + 1
    sheared = scratch.transform(
        (scratch_w + shear_px, scratch_h),
        Image.AFFINE,
        (1, ITALIC_SHEAR, -ITALIC_SHEAR * scratch_h, 0, 1, 0),
        resample=Image.BICUBIC,
    )

    dest_x, dest_y = int(x) - stroke_w, int(y)
    # alpha_composite raises if the pasted region doesn't fit inside
    # canvas bounds - can happen for the last line of a message that
    # lands right at the card's bottom edge - so clip instead of
    # letting a render crash on an otherwise-harmless overflow of
    # mostly-transparent padding.
    max_w, max_h = canvas.width - dest_x, canvas.height - dest_y
    if max_w <= 0 or max_h <= 0:
        return total_w
    if sheared.width > max_w or sheared.height > max_h:
        sheared = sheared.crop((0, 0, min(sheared.width, max_w), min(sheared.height, max_h)))
    canvas.alpha_composite(sheared, (dest_x, dest_y))
    return total_w + round(size * ITALIC_SHEAR * 0.5)


def _draw_run(canvas: Image.Image, draw: ImageDraw.ImageDraw, x: float, y: float, text: str, size: int, styles: frozenset, fill) -> float:
    """Draws one (text, styles) part - already split so every
    character in it shares the same style combination - at (x, y) and
    returns its advance width (the caller's next run starts there).
    Handles any combination of the five supported styles."""
    if not text:
        return 0.0

    is_code = 'code' in styles
    is_bold = 'bold' in styles
    is_italic = 'italic' in styles
    stroke_w = max(1, round(size * BOLD_STROKE_FACTOR)) if is_bold else 0

    if is_code:
        text_w = _code_text_width(text, size)
        pad_x = round(size * CODE_CHIP_PAD_X_FACTOR)
        pad_y = round(size * CODE_CHIP_PAD_Y_FACTOR)
        radius = round(size * CODE_CHIP_RADIUS_FACTOR)
        draw.rounded_rectangle(
            (x, y - pad_y, x + text_w + pad_x * 2, y + size + pad_y),
            radius=radius, fill=CODE_CHIP_COLOR,
        )
        text_x = x + pad_x  # inset so the box has equal breathing room on both sides
        if is_italic:
            italic_w = _draw_sheared_text(canvas, text_x, y, _segment_mono_runs(text, size), size, fill, stroke_w)
            advance = italic_w + pad_x * 2
        else:
            _draw_code_text(draw, (text_x, y), text, size, fill)
            advance = text_w + pad_x * 2
        underline_x0 = text_x
    else:
        text_w = sum(font.getlength(seg) for font, seg in _segment_runs(text, size))
        if is_italic:
            advance = _draw_sheared_text(canvas, x, y, _segment_runs(text, size), size, fill, stroke_w)
        else:
            cx = x
            for font, seg in _segment_runs(text, size):
                if stroke_w:
                    draw.text((cx, y), seg, font=font, fill=fill, stroke_width=stroke_w, stroke_fill=fill, embedded_color=True)
                else:
                    draw.text((cx, y), seg, font=font, fill=fill, embedded_color=True)
                cx += font.getlength(seg)
            advance = text_w
        underline_x0 = x

    if 'underline' in styles or 'strike' in styles:
        ascent, _descent = _fallback_font(size).getmetrics()
        line_w = max(1, round(size * 0.06))
        if 'underline' in styles:
            uy = y + ascent + max(1, round(size * 0.08))
            draw.line((underline_x0, uy, underline_x0 + text_w, uy), fill=fill, width=line_w)
        if 'strike' in styles:
            sy = y + round(ascent * 0.58)
            draw.line((underline_x0, sy, underline_x0 + text_w, sy), fill=fill, width=line_w)

    return advance


def draw_styled_line(canvas: Image.Image, draw: ImageDraw.ImageDraw, xy, parts, size: int, fill) -> float:
    """Draws a full line (as produced by wrap_styled_text) part by
    part, left to right. Returns the total width drawn."""
    x, y = xy
    start_x = x
    for text, styles in parts:
        x += _draw_run(canvas, draw, x, y, text, size, styles, fill)
    return x - start_x


_WORD_RE = re.compile(r'\S+')


def _styled_words(runs):
    """Splits (text, styles) runs into a stream of ('word', parts) and
    ('space', styles) tokens. A word is a list of (text, styles)
    sub-parts (one visual word can be made of several differently-
    styled pieces, e.g. "un**believ**able" - three runs, no
    whitespace between them - still comes out as one word, wrapped as
    a single atomic unit). A space token carries whatever style the
    whitespace itself had, so *if* it ends up staying on one line
    (see wrap_styled_text) a multi-word span sharing one style - most
    importantly a multi-word code span - still reads as one
    continuous run instead of a separate chip per word."""
    current = []
    for text, styles in runs:
        chunks = re.split(r'(\s+)', text)
        for chunk in chunks:
            if chunk == "":
                continue
            if chunk.isspace():
                if current:
                    yield ('word', current)
                    current = []
                yield ('space', styles)
            else:
                current.append((chunk, styles))
    if current:
        yield ('word', current)


def _merge_adjacent_styled(parts):
    """Concatenates consecutive (text, styles) parts that share the
    exact same style set. Wrapping can hand back a multi-word span as
    several separate parts (one per word, glued together with a
    styled space in between) - merging them back into one part before
    drawing means _draw_run only ever sees (and boxes) it once, so a
    multi-word code span renders as a single continuous chip instead
    of one small chip per word."""
    merged = []
    for text, styles in parts:
        if merged and merged[-1][1] == styles:
            merged[-1] = (merged[-1][0] + text, styles)
        else:
            merged.append((text, styles))
    return merged


def wrap_styled_text(runs, size: int, max_width: float):
    """Style-aware counterpart to wrap_text(): `runs` is the output of
    parse_markdown() for the whole message. Wraps the same way
    wrap_text() does (whitespace normalized to a single separator, an
    unbreakable word wider than max_width gets hard-broken character
    by character) but each returned line is a list of (text, styles)
    parts instead of a plain string, so a style that spans a wrap
    point still renders correctly on each side of it."""
    if not runs:
        return [[]]
    if max_width <= 0:
        return [_merge_adjacent_styled(list(runs))]

    tokens = list(_styled_words(runs))
    if not tokens:
        return [[]]

    lines = []
    current, current_w = [], 0.0
    pending_space_styles = None  # style of a not-yet-placed space, if any

    def flush():
        nonlocal current, current_w
        lines.append(_merge_adjacent_styled(current))
        current, current_w = [], 0.0

    for kind, payload in tokens:
        if kind == 'space':
            pending_space_styles = payload
            continue

        word = payload
        w = sum(measure_styled_run(t, size, s) for t, s in word)
        sep_styles = pending_space_styles if current else None
        sep_w = measure_styled_run(" ", size, sep_styles) if sep_styles is not None else 0.0
        pending_space_styles = None

        if w > max_width:
            # Hard-break this one word char by char, same as wrap_text.
            if current:
                flush()
            piece, piece_w = [], 0.0
            for text, styles in word:
                for ch in text:
                    ch_w = measure_styled_run(ch, size, styles)
                    if piece and piece_w + ch_w > max_width:
                        lines.append(piece)
                        piece, piece_w = [], 0.0
                    if piece and piece[-1][1] == styles:
                        piece[-1] = (piece[-1][0] + ch, styles)
                    else:
                        piece.append((ch, styles))
                    piece_w += ch_w
            current, current_w = piece, piece_w
            continue

        if current and current_w + sep_w + w > max_width:
            flush()
            sep_styles, sep_w = None, 0.0

        if sep_styles is not None:
            current.append((" ", sep_styles))
        elif current:
            current.append((" ", frozenset()))  # defensive fallback - shouldn't normally trigger
        current.extend(word)
        current_w += sep_w + w

    if current or not lines:
        flush()
    return lines


def wrap_text(text, size, max_width):
    """Word-wraps text to fit max_width. A single "word" that's wider
    than max_width on its own (a long URL, a wall of text with no
    spaces) is hard-broken character by character instead of being
    left to overflow the canvas - the only way an unbroken token would
    normally get shorter is by shrinking the font, and we'd rather
    keep the font size consistent and readable."""
    if max_width <= 0:
        return [text] if text else [""]

    words = text.split()
    lines, current = [], ""
    for word in words:
        if measure_text(word, size) > max_width:
            if current:
                lines.append(current)
                current = ""
            piece = ""
            for ch in word:
                test = piece + ch
                if not piece or measure_text(test, size) <= max_width:
                    piece = test
                else:
                    lines.append(piece)
                    piece = ch
            current = piece
            continue

        test = f"{current} {word}".strip()
        if measure_text(test, size) <= max_width:
            current = test
        else:
            if current:
                lines.append(current)
            current = word
    return lines + [current] if current else lines or [""]


def circle_mask(size, supersample=4):
    """Anti-aliased circular mask: draws the circle at `supersample`x
    the target resolution, then downsamples with LANCZOS so the edge
    isn't jagged. A mask drawn straight at the final size shows its
    stair-stepping much more obviously now that avatars render 3x
    bigger than before."""
    big = size * supersample
    mask = Image.new("L", (big, big), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, big - 1, big - 1), fill=255)
    return mask.resize((size, size), Image.LANCZOS)


# ---------------------------------------------------------------- avatars

async def _fetch_avatar_bytes(session: aiohttp.ClientSession, url: str) -> Optional[bytes]:
    """Network-only half of the original fetch_avatar - safe to await
    directly on the event loop since there's no Pillow call in here."""
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            return await resp.read()
    except Exception:
        return None


def _decode_avatar(data: Optional[bytes], size: int) -> Image.Image:
    """CPU-bound half. `data is None` (fetch failed) and 'bytes that
    fail to decode' both fall into the same except branch, matching
    the original fetch_avatar()'s single broad try/except - either way
    you get the blurple placeholder, not a crash."""
    try:
        if data is None:
            raise ValueError("no data")
        img = Image.open(io.BytesIO(data)).convert("RGBA").resize((size, size), Image.LANCZOS)
    except Exception:
        img = Image.new("RGBA", (size, size), (88, 101, 242, 255))
    img.putalpha(circle_mask(size))
    return img


def _decode_decoration(data: Optional[bytes], size: int) -> Optional[Image.Image]:
    """Unlike _decode_avatar, no circle_mask and no placeholder on
    failure - a decoration is an arbitrary-shaped PNG with its own
    alpha channel (it's meant to sit *outside* the avatar's circle
    too), and a missing/broken fetch should just mean "skip drawing
    it", not "paste a blurple square over the avatar"."""
    if data is None:
        return None
    try:
        return Image.open(io.BytesIO(data)).convert("RGBA").resize((size, size), Image.LANCZOS)
    except Exception:
        return None


# ------------------------------------------------------------- orchestration

def _truncate_reply_snippet(snippet: Optional[str]) -> str:
    """Caps the reply preview at REPLY_CHAR_LIMIT characters, swapping
    the last one for an ellipsis when it runs over - e.g. a 200-char
    snippet keeps its first 89 characters plus "…"."""
    if not snippet:
        return ""
    if len(snippet) <= REPLY_CHAR_LIMIT:
        return snippet
    return snippet[:REPLY_CHAR_LIMIT - 1] + "…"


def _fit_single_line(text: str, size: int, max_width: float) -> str:
    """Pixel-width sibling of _truncate_reply_snippet, for text that's
    always drawn as one line and never wrapped: the display name, and
    the reply-preview name. Under the old content-driven sizing, the
    canvas grew (via natural_width) to guarantee these always fit;
    with CARD_WIDTH fixed, an unusually long name/reply-name can now
    be wider than the space available for it, so this hard-truncates
    with an ellipsis instead of letting it silently draw past the
    right edge of the card and get clipped mid-character."""
    if not text or max_width <= 0 or measure_text(text, size) <= max_width:
        return text
    ellipsis_w = measure_text("…", size)
    budget = max(0.0, max_width - ellipsis_w)
    piece = ""
    for ch in text:
        test = piece + ch
        if measure_text(test, size) <= budget:
            piece = test
        else:
            break
    return piece + "…"


def _draw_fake_message_sync(name, avatar_data, avatar_size, message, name_color,
                             reply, reply_name, reply_avatar_data, reply_has_avatar,
                             reply_snippet, bg_color, third_gradient, decoration_data=None,
                             date_input=None):
    """All the Pillow work. Fully synchronous - only ever call this
    through asyncio.to_thread, never directly from a coroutine."""
    padding = 16 * SCALE
    text_x = padding + avatar_size + 12 * SCALE
    snippet = _truncate_reply_snippet(reply_snippet) if reply else ""

    timestamp_text = format_display_datetime(parse_display_datetime(date_input))
    timestamp_gap = round(NAME_SIZE * 0.22)
    timestamp_w = measure_text(timestamp_text, REPLY_SIZE)

    # name_color is deliberately excluded - only background-related
    # hexes get darkened. Everything below this point only ever sees
    # the darkened strings, so the crop and every contrast check are
    # automatically checking against what's really painted.
    bg_color, bg_report = _darken_bg_color_input(bg_color)
    third_gradient, third_report = _darken_third_gradient_input(third_gradient)
    color_report = bg_report + third_report

    # --- width grows to fit content, then gets 25% padding on top ---
    #
    # max_text_w is how wide the text area needs to be to hold the
    # longer of (name [+ gap + timestamp], message-on-one-line)
    # without wrapping, capped at CARD_WIDTH so an unusually long
    # message still wraps instead of producing a huge canvas.
    # natural_content_w adds the avatar/padding back in to get the
    # width the whole card would need with zero slack; the real
    # canvas is that times WIDTH_PAD_FACTOR (1.25), floored at
    # MIN_WIDTH.
    message_runs = parse_markdown(message)
    max_text_w = max(
        measure_text(name, NAME_SIZE) + timestamp_gap + timestamp_w,
        min(measure_styled_line(message_runs, MSG_SIZE), CARD_WIDTH - text_x - padding),
    )
    natural_content_w = text_x + max_text_w + padding
    width = max(MIN_WIDTH, round(natural_content_w * WIDTH_PAD_FACTOR))

    msg_wrap_width = width - text_x - padding
    lines = wrap_styled_text(message_runs, MSG_SIZE, msg_wrap_width)
    # name isn't part of max_text_w's cap logic beyond being one of the
    # two candidates measured above, so still needs its own hard fit -
    # an unusually long name relative to the message can still exceed
    # msg_wrap_width and needs truncating rather than overflowing the
    # card. Reserve room for the timestamp riding alongside it too.
    name = _fit_single_line(name, NAME_SIZE, msg_wrap_width - timestamp_gap - timestamp_w)

    if reply:
        rx = avatar_size // 2 + padding
        reply_avail_w = max(0.0, width - rx - REPLY_AVATAR_TEXT_GAP - padding)
        # Name gets first claim on the row (up to ~45% of it) so a long
        # snippet can't crowd it out entirely; snippet gets whatever's
        # left, on top of its existing 90-char cap.
        reply_name = _fit_single_line(f"{reply_name}  ", REPLY_SIZE, reply_avail_w * 0.45)
        reply_name_w = measure_text(reply_name, REPLY_SIZE)
        snippet = _fit_single_line(snippet, REPLY_SIZE, max(0.0, reply_avail_w - reply_name_w))

    # --- height still grows with however many lines that wrapping produced ---
    content_height = padding * 2 + NAME_ROW_H + len(lines) * LINE_H
    if reply:
        content_height += REPLY_ROW_H
    # The avatar itself (now much bigger) needs to fit vertically too,
    # not just the name/message rows next to it.
    content_height = max(content_height, padding * 2 + avatar_size)
    # MIN_HEIGHT is a hard floor; MAX_HEIGHT is a soft target only -
    # an unusually long message is allowed to exceed it rather than
    # have its text cut off to fit.
    height = max(content_height, MIN_HEIGHT)

    canvas, bg_ref_width = build_background_crop(width, height, bg_color, third_gradient)
    canvas = canvas.convert("RGBA")
    draw = ImageDraw.Draw(canvas, "RGBA")
    # If we hit the MIN_HEIGHT floor, center the content in the extra
    # space instead of leaving it all pinned to the top.
    y = padding + max(0, height - content_height) / 2.0

    if reply:
        r_avatar = (
            _decode_avatar(reply_avatar_data, REPLY_AVATAR_SIZE) if reply_has_avatar
            else Image.new("RGBA", (REPLY_AVATAR_SIZE, REPLY_AVATAR_SIZE), (0, 0, 0, 0))
        )
        rx = avatar_size // 2 + padding
        canvas.paste(r_avatar, (rx, int(y)), r_avatar)
        reply_name_x = rx + REPLY_AVATAR_TEXT_GAP
        reply_snippet_x = reply_name_x + reply_name_w
        reply_name_color = ensure_readable(
            (255, 255, 255),
            local_bg_color(reply_name_x + reply_name_w / 2, bg_ref_width, bg_color, third_gradient, spread_ref_width=width),
        )
        reply_snippet_color = ensure_readable(
            (148, 155, 164),
            local_bg_color(
                reply_snippet_x + measure_text(snippet, REPLY_SIZE) / 2, bg_ref_width, bg_color, third_gradient,
                spread_ref_width=width,
            ),
        )
        draw_fallback_text(draw, (reply_name_x, y), reply_name, REPLY_SIZE, reply_name_color)
        draw_fallback_text(draw, (reply_snippet_x, y), snippet, REPLY_SIZE, reply_snippet_color)
        y += REPLY_ROW_H

    avatar = _decode_avatar(avatar_data, avatar_size)
    canvas.paste(avatar, (padding, int(y)), avatar)

    if decoration_data:
        decoration_size = round(avatar_size * DECORATION_SCALE)
        decoration = _decode_decoration(decoration_data, decoration_size)
        if decoration:
            offset = (avatar_size - decoration_size) // 2
            canvas.paste(decoration, (padding + offset, int(y) + offset), decoration)

    name_w = measure_text(name, NAME_SIZE)
    name_parsed = parse_name_color(name_color)
    if name_parsed and name_parsed[0] == "gradient":
        c1, c2 = name_parsed[1]
        gradient_img = render_gradient_name(name, NAME_SIZE, c1, c2)
        dest_x, dest_y = round(text_x), round(y)
        # Same overflow guard as the italic-text path - alpha_composite
        # raises if the pasted region doesn't fully fit on canvas.
        max_w, max_h = canvas.width - dest_x, canvas.height - dest_y
        if max_w > 0 and max_h > 0:
            if gradient_img.width > max_w or gradient_img.height > max_h:
                gradient_img = gradient_img.crop((0, 0, min(gradient_img.width, max_w), min(gradient_img.height, max_h)))
            canvas.alpha_composite(gradient_img, (dest_x, dest_y))
    else:
        name_rgb = name_parsed[1] if name_parsed else (255, 255, 255)
        name_bg = local_bg_color(
            text_x + name_w / 2, bg_ref_width, bg_color, third_gradient, spread_ref_width=width
        )
        name_rgb = ensure_readable(name_rgb, name_bg)
        draw_fallback_text(draw, (text_x, y), name, NAME_SIZE, name_rgb)

    ts_x = text_x + name_w + timestamp_gap
    ts_y = y + round((NAME_SIZE - REPLY_SIZE) * 0.62)  # roughly aligns baselines with the (larger) name font
    ts_bg = local_bg_color(
        ts_x + timestamp_w / 2, bg_ref_width, bg_color, third_gradient, spread_ref_width=width
    )
    ts_color = ensure_readable(TIMESTAMP_COLOR, ts_bg)
    draw_fallback_text(draw, (ts_x, ts_y), timestamp_text, REPLY_SIZE, ts_color)

    y += NAME_ROW_H
    for line in lines:
        line_w = measure_styled_line(line, MSG_SIZE)
        line_bg = local_bg_color(
            text_x + line_w / 2, bg_ref_width, bg_color, third_gradient, spread_ref_width=width
        )
        msg_color = ensure_readable((220, 221, 222), line_bg)
        draw_styled_line(canvas, draw, (text_x, y), line, MSG_SIZE, msg_color)
        y += LINE_H

    buf = io.BytesIO()
    canvas.convert("RGB").save(buf, format="PNG")
    buf.seek(0)
    return buf, color_report


async def render_fake_message(session: aiohttp.ClientSession, name, avatar_url, message, name_color=None,
                               reply=False, reply_name=None, reply_avatar_url=None, reply_snippet=None,
                               bg_color=None, third_gradient=None, decoration_url=None, date=None):
    """Orchestrator: real async network fetches on the event loop,
    then one asyncio.to_thread dispatch for all of the Pillow work
    (decoding both avatars + drawing the whole canvas together).

    Returns (buf, color_report): buf is the PNG BytesIO; color_report
    is a list of (label, orig_hex, dark_hex) rows, one per
    background-related hex that got HSL-darkened before painting (see
    darken_hex_color) - empty if bg_color/third_gradient weren't set,
    or didn't contain anything that parsed as a hex color."""
    avatar_size = 48 * SCALE  # 144px - 300% of the original 48px

    avatar_data = await _fetch_avatar_bytes(session, avatar_url)

    reply_has_avatar = bool(reply and reply_avatar_url)
    reply_avatar_data = await _fetch_avatar_bytes(session, reply_avatar_url) if reply_has_avatar else None

    decoration_data = await _fetch_avatar_bytes(session, decoration_url) if decoration_url else None

    return await asyncio.to_thread(
        _draw_fake_message_sync,
        name, avatar_data, avatar_size, message, name_color,
        reply, reply_name, reply_avatar_data, reply_has_avatar,
        reply_snippet, bg_color, third_gradient, decoration_data, date,
    )
