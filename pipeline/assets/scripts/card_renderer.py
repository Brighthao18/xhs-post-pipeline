"""Standalone renderer adapted from the user's existing eight card styles."""
from functools import lru_cache
import math
import os
from pathlib import Path
import random
import unicodedata

from PIL import Image, ImageDraw, ImageFont

CARD_WIDTH = 1080
CARD_HEIGHT = 1440
DEFAULT_AUTHOR = "示例作者"

STYLES = {

    # 1. Magazine style - warm cream gradient + gold accents
    "magazine": {
        "name": "magazine",
        "bg_type": "vertical",
        "bg_top": (255, 248, 240),
        "bg_bot": (252, 232, 205),
        "text_color": (50, 38, 25),
        "accent_color": (185, 138, 55),
        "secondary_color": (130, 100, 65),
        "font_size": 60,
        "line_spacing_ratio": 1.50,
        "quote_mark_char": "\u201c",
        "quote_mark_size": 220,
        "quote_mark_color": (210, 185, 130),
        "layout": "centered",
        "border_color": (195, 170, 120),
        "border_width": 3,
        "double_border": True,
        "top_ornament": "line",
        "bottom_ornament": "double_line",
        "left_bar": False,
        "stars": False,
        "paper_texture": False,
        "glow": False,
    },

    # 2. Vintage newspaper style - aged cream + left bar
    "vintage": {
        "name": "vintage",
        "bg_type": "vertical",
        "bg_top": (248, 238, 220),
        "bg_bot": (238, 225, 200),
        "text_color": (60, 42, 22),
        "accent_color": (145, 85, 25),
        "secondary_color": (105, 72, 38),
        "font_size": 56,
        "line_spacing_ratio": 1.50,
        "quote_mark_char": "\u201c",
        "quote_mark_size": 190,
        "quote_mark_color": (170, 130, 75),
        "layout": "left",
        "border_color": (165, 135, 90),
        "border_width": 5,
        "double_border": False,
        "top_ornament": "star",
        "bottom_ornament": "star",
        "left_bar": True,
        "stars": False,
        "paper_texture": True,
        "glow": False,
    },

    # 3. Minimalist literary style - pure white + clean typography
    "minimalist": {
        "name": "minimalist",
        "bg_type": "solid",
        "bg_top": (255, 255, 255),
        "bg_bot": (255, 255, 255),
        "text_color": (25, 25, 25),
        "accent_color": (195, 45, 45),
        "secondary_color": (160, 160, 160),
        "font_size": 62,
        "line_spacing_ratio": 1.55,
        "quote_mark_char": "",
        "quote_mark_size": 90,
        "quote_mark_color": (195, 45, 45),
        "layout": "centered",
        "border_color": (210, 210, 210),
        "border_width": 1,
        "double_border": False,
        "top_ornament": "none",
        "bottom_ornament": "thin_line",
        "left_bar": False,
        "stars": False,
        "paper_texture": False,
        "glow": False,
    },

    # 4. Ink brush style - deep navy + gold/ivory text
    "ink": {
        "name": "ink",
        "bg_type": "vertical",
        "bg_top": (12, 12, 28),
        "bg_bot": (28, 22, 52),
        "text_color": (255, 250, 238),
        "accent_color": (255, 210, 75),
        "secondary_color": (185, 165, 110),
        "font_size": 60,
        "line_spacing_ratio": 1.55,
        "quote_mark_char": "\u201c",
        "quote_mark_size": 230,
        "quote_mark_color": (85, 75, 45),
        "layout": "centered",
        "border_color": (85, 75, 55),
        "border_width": 4,
        "double_border": True,
        "top_ornament": "circle",
        "bottom_ornament": "circle",
        "left_bar": False,
        "stars": False,
        "paper_texture": False,
        "glow": True,
        "glow_color": (255, 210, 75),
        "glow_strength": 28,
    },

    # 5. Morandi palette - soft muted pastels
    "morandi": {
        "name": "morandi",
        "bg_type": "vertical",
        "bg_top": (242, 238, 235),
        "bg_bot": (230, 220, 218),
        "text_color": (82, 78, 76),
        "accent_color": (162, 142, 138),
        "secondary_color": (128, 112, 108),
        "font_size": 58,
        "line_spacing_ratio": 1.52,
        "quote_mark_char": "x",
        "quote_mark_size": 110,
        "quote_mark_color": (162, 142, 138),
        "layout": "centered",
        "border_color": (198, 188, 184),
        "border_width": 2,
        "double_border": False,
        "top_ornament": "star",
        "bottom_ornament": "line",
        "left_bar": False,
        "stars": False,
        "paper_texture": False,
        "glow": False,
    },

    # 6. Bold fashion style - coral-orange gradient + big white text
    "bold": {
        "name": "bold",
        "bg_type": "diagonal",
        "bg_top": (255, 95, 115),
        "bg_bot": (255, 175, 75),
        "text_color": (255, 255, 255),
        "accent_color": (255, 242, 200),
        "secondary_color": (255, 218, 175),
        "font_size": 66,
        "line_spacing_ratio": 1.50,
        "quote_mark_char": "",
        "quote_mark_size": 140,
        "quote_mark_color": (255, 242, 200),
        "layout": "centered",
        "border_color": (255, 255, 255),
        "border_width": 0,
        "double_border": False,
        "top_ornament": "none",
        "bottom_ornament": "line",
        "left_bar": False,
        "stars": False,
        "paper_texture": False,
        "glow": True,
        "glow_color": (255, 155, 90),
        "glow_strength": 45,
    },

    # 7. Watercolor style - soft pink-lavender radial
    "watercolor": {
        "name": "watercolor",
        "bg_type": "radial",
        "bg_top": (255, 242, 250),
        "bg_bot": (242, 215, 255),
        "bg_c3": (255, 225, 245),
        "text_color": (75, 45, 78),
        "accent_color": (175, 75, 125),
        "secondary_color": (138, 88, 128),
        "font_size": 58,
        "line_spacing_ratio": 1.52,
        "quote_mark_char": "",
        "quote_mark_size": 125,
        "quote_mark_color": (195, 115, 155),
        "layout": "centered",
        "border_color": (218, 188, 215),
        "border_width": 3,
        "double_border": False,
        "top_ornament": "flower",
        "bottom_ornament": "line",
        "left_bar": False,
        "stars": False,
        "paper_texture": False,
        "glow": False,
    },

    # 8. Starry night style - deep blue + stars + golden glow
    "starry": {
        "name": "starry",
        "bg_type": "vertical",
        "bg_top": (4, 4, 28),
        "bg_bot": (18, 12, 62),
        "text_color": (238, 238, 255),
        "accent_color": (255, 228, 115),
        "secondary_color": (178, 178, 255),
        "font_size": 60,
        "line_spacing_ratio": 1.55,
        "quote_mark_char": "",
        "quote_mark_size": 150,
        "quote_mark_color": (255, 228, 115),
        "layout": "centered",
        "border_color": (55, 55, 115),
        "border_width": 4,
        "double_border": True,
        "top_ornament": "sparkle",
        "bottom_ornament": "sparkle",
        "left_bar": False,
        "stars": True,
        "paper_texture": False,
        "glow": True,
        "glow_color": (95, 95, 255),
        "glow_strength": 32,
    },
}

def _build_bg(w, h, s):
    """Build background image based on style."""
    t = s["bg_type"]
    if t == "solid":
        return Image.new("RGB", (w, h), s["bg_top"])

    elif t == "vertical":
        c1, c2 = s["bg_top"], s["bg_bot"]
        img = Image.new("RGB", (w, h))
        draw = ImageDraw.Draw(img)
        for y in range(h):
            ratio = y / max(h - 1, 1)
            r = int(c1[0] * (1 - ratio) + c2[0] * ratio)
            g = int(c1[1] * (1 - ratio) + c2[1] * ratio)
            b = int(c1[2] * (1 - ratio) + c2[2] * ratio)
            draw.line([(0, y), (w, y)], fill=(r, g, b))
        return img

    elif t == "diagonal":
        c1, c2 = s["bg_top"], s["bg_bot"]
        img = Image.new("RGB", (w, h))
        px = img.load()
        for y in range(h):
            for x in range(w):
                ratio = (x / max(w - 1, 1) + y / max(h - 1, 1)) / 2
                px[x, y] = (
                    int(c1[0] * (1 - ratio) + c2[0] * ratio),
                    int(c1[1] * (1 - ratio) + c2[1] * ratio),
                    int(c1[2] * (1 - ratio) + c2[2] * ratio),
                )
        return img

    elif t == "radial":
        c1, c2 = s["bg_top"], s["bg_bot"]
        c3 = s.get("bg_c3", s["bg_bot"])
        cx1, cy1 = w // 2, h // 3
        cx2, cy2 = w * 2 // 3, h * 2 // 3
        img = Image.new("RGB", (w, h))
        px = img.load()
        max_d = math.sqrt(w**2 + h**2) * 0.65
        for y in range(h):
            for x in range(w):
                d1 = math.sqrt((x - cx1)**2 + (y - cy1)**2)
                ratio = min(1.0, d1 / max_d)
                px[x, y] = (
                    max(0, int(c1[0] * (1 - ratio) + c2[0] * ratio * 0.5 + c3[0] * ratio * 0.5)),
                    max(0, int(c1[1] * (1 - ratio) + c2[1] * ratio * 0.5 + c3[1] * ratio * 0.5)),
                    max(0, int(c1[2] * (1 - ratio) + c2[2] * ratio * 0.5 + c3[2] * ratio * 0.5)),
                )
        return img

    return Image.new("RGB", (w, h), (200, 200, 200))

def _add_stars(img, count=100):
    """Add random stars."""
    w, h = img.size
    draw = ImageDraw.Draw(img)
    rng = random.Random(42)
    for _ in range(count):
        x, y = rng.randint(0, w - 1), rng.randint(0, h - 1)
        size = rng.choices([1, 2], weights=[0.7, 0.3])[0]
        v = rng.randint(180, 255)
        draw.ellipse([x, y, x + size, y + size], fill=(v, v, min(v + 30, 255)))

def _add_paper_texture(img):
    """Add aged paper noise texture."""
    w, h = img.size
    rng = random.Random(777)
    for _ in range(6000):
        x, y = rng.randint(0, w - 1), rng.randint(0, h - 1)
        v = rng.randint(-15, 15)
        r, g, b = img.getpixel((x, y))
        img.putpixel((x, y), (
            max(0, min(255, r + v)),
            max(0, min(255, g + v)),
            max(0, min(255, b + v)),
        ))

def _add_corner_glow(img, glow_color, strength=25):
    """Add radial corner glow for dark themes."""
    w, h = img.size
    r, g, b = glow_color
    glow_img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow_img)
    corners = [(0, 0, 350, 350), (w - 350, 0, w, 350), (0, h - 350, 350, h), (w - 350, h - 350, w, h)]
    for x1, y1, x2, y2 in corners:
        cx_c, cy_c = (x1 + x2) // 2, (y1 + y2) // 2
        rad = max(x2 - x1, y2 - y1)
        for step in range(rad, 0, -3):
            alpha = int(strength * (1 - step / rad))
            if alpha > 0:
                gd.ellipse([cx_c - step, cy_c - step, cx_c + step, cy_c + step],
                           fill=(r, g, b, alpha))
    img = img.convert("RGBA")
    return Image.alpha_composite(img, glow_img).convert("RGB")

def _draw_ornament(draw, ornament, color, cx, y):
    """Draw a centered ornament at (cx, y)."""
    if ornament == "line":
        draw.line([(cx - 100, y), (cx + 100, y)], fill=color, width=2)
    elif ornament == "double_line":
        draw.line([(cx - 120, y), (cx + 120, y)], fill=color, width=1)
        draw.line([(cx - 70, y + 10), (cx + 70, y + 10)], fill=color, width=1)
    elif ornament == "thin_line":
        draw.line([(cx - 150, y), (cx + 150, y)], fill=color, width=1)
    elif ornament == "star":
        cx_s, cy_s = cx, y + 8
        pts = []
        for i in range(10):
            angle = math.pi / 2 + i * math.pi / 5
            rad = 14 if i % 2 == 0 else 14 * 0.38
            pts.append((cx_s + rad * math.cos(angle), cy_s - rad * math.sin(angle)))
        draw.polygon(pts, fill=color)
    elif ornament == "circle":
        draw.ellipse([cx - 18, y - 10, cx + 18, y + 26], outline=color, width=2)
        draw.ellipse([cx - 7, y - 1, cx + 7, y + 13], fill=color)
    elif ornament == "sparkle":
        draw.line([(cx - 25, y + 8), (cx + 25, y + 8)], fill=color, width=2)
        draw.line([(cx, y - 17), (cx, y + 33)], fill=color, width=2)
        for dx, dy in [(-14, -14), (14, -14), (-14, 14), (14, 14)]:
            draw.ellipse([cx + dx - 4, y + dy + 4, cx + dx + 4, y + dy + 12], fill=color)
    elif ornament == "flower":
        for ang in range(0, 360, 60):
            rad = math.radians(ang)
            px_f = cx + 14 * math.cos(rad)
            py_f = y + 8 + 14 * math.sin(rad)
            draw.ellipse([px_f - 7, py_f - 7, px_f + 7, py_f + 7], fill=color)
        draw.ellipse([cx - 6, y + 1, cx + 6, y + 13], fill=(200, 150, 90))


def resolve_fonts(cjk=None, latin=None):
    directory = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    candidates = {
        "cjk": [directory / "simsun.ttc", Path("/System/Library/Fonts/Supplemental/Songti.ttc"),
                Path("/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc")],
        "latin": [directory / "times.ttf", Path("/System/Library/Fonts/Supplemental/Times New Roman.ttf"),
                  Path("/usr/share/fonts/truetype/msttcorefonts/Times_New_Roman.ttf"),
                  Path("/usr/share/fonts/truetype/liberation2/LiberationSerif-Regular.ttf"),
                  Path("/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf")],
    }
    requested = {"cjk": cjk or os.environ.get("XHS_CJK_FONT"),
                 "latin": latin or os.environ.get("XHS_LATIN_FONT")}
    paths = {role: Path(value).expanduser() if value else next((p for p in candidates[role] if p.is_file()), candidates[role][0])
             for role, value in requested.items()}
    for role, path in paths.items():
        if not path.is_file():
            raise ValueError(f"Missing {role} font: {path}. Supply fonts.{role} explicitly.")
        paths[role] = str(path.resolve())
    return paths


def is_cjk(character):
    value = ord(character)
    return (0x2E80 <= value <= 0xA4CF or 0xAC00 <= value <= 0xD7AF
            or 0xF900 <= value <= 0xFAFF or 0xFF00 <= value <= 0xFFEF
            or 0x20000 <= value <= 0x323AF)


class FontPair:
    def __init__(self, size, fonts):
        self.size = int(size)
        self.paths = fonts
        self.cjk = ImageFont.truetype(fonts["cjk"], self.size)
        self.latin = ImageFont.truetype(fonts["latin"], self.size)
        self.ascent = max(self.cjk.getmetrics()[0], self.latin.getmetrics()[0])
        self.descent = max(self.cjk.getmetrics()[1], self.latin.getmetrics()[1])

    def runs(self, text):
        buffer, current = "", None
        for character in text:
            font = self.cjk if is_cjk(character) else self.latin
            if current is not None and font is not current:
                yield buffer, current
                buffer = ""
            buffer += character
            current = font
        if buffer:
            yield buffer, current

    @lru_cache(maxsize=4096)
    def width(self, text):
        return sum(font.getlength(run) for run, font in self.runs(text))

    def validate(self, text):
        for character in set(text):
            if character == "\n" or character.isspace():
                continue
            if unicodedata.category(character) in ("Cc", "Cs", "Cf"):
                raise ValueError("Card text contains unsupported control characters.")
            font = self.cjk if is_cjk(character) else self.latin
            mask = font.getmask(character)
            missing = font.getmask(chr(0x10FFFF))
            if mask.size == missing.size and bytes(mask) == bytes(missing):
                raise ValueError(f"Missing glyph {character!r} (U+{ord(character):04X}) in {font.getname()[0]}.")

    def draw(self, drawing, xy, text, color):
        x, baseline = xy
        boxes = []
        for run, font in self.runs(text):
            box = drawing.textbbox((x, baseline), run, font=font, anchor="ls")
            drawing.text((x, baseline), run, font=font, fill=color, anchor="ls")
            if run.strip():
                boxes.append(list(box))
            x += font.getlength(run)
        return boxes


def wrap_text(text, pair, max_width):
    lines = []
    for paragraph in text.split("\n"):
        if not paragraph:
            lines.append("")
            continue
        current = ""
        for character in paragraph:
            if pair.width(character) > max_width:
                raise ValueError("A glyph exceeds the available card width.")
            if current and pair.width(current + character) > max_width:
                lines.append(current)
                current = ""
            current += character
        lines.append(current)
    if "".join(lines) != text.replace("\n", ""):
        raise ValueError("Text wrapping changed the input content.")
    return lines


def create_quote_card(text, output_path, style_name="morandi", author=DEFAULT_AUTHOR, fonts=None):
    if style_name not in STYLES:
        raise ValueError(f"Unknown style: {style_name}")
    fonts = fonts or resolve_fonts()
    style = STYLES[style_name]
    width, height = CARD_WIDTH, CARD_HEIGHT
    left = 160 if style["layout"] == "left" else 140
    right, top, bottom = width - 140, 280, height - 340
    max_width, available = right - left, bottom - top

    for size in range(int(style["font_size"]), 31, -2):
        main = FontPair(size, fonts)
        main.validate(text)
        lines = wrap_text(text, main, max_width)
        line_height = max(math.ceil(size * style["line_spacing_ratio"]), main.ascent + main.descent + 8)
        block_height = len(lines) * line_height
        if block_height <= available:
            break
    else:
        raise ValueError("Card text does not fit without truncation; shorten it or split cards.")

    author_text = "— " + author if author else ""
    for author_size in range(38, 17, -2):
        author_pair = FontPair(author_size, fonts)
        author_pair.validate(author_text)
        if author_pair.width(author_text) <= max_width:
            break
    else:
        raise ValueError("Author signature is too wide.")

    image = _build_bg(width, height, style)
    if style.get("stars"):
        _add_stars(image)
    if style.get("paper_texture"):
        _add_paper_texture(image)
    if style.get("glow") and style.get("glow_color"):
        image = _add_corner_glow(image, style["glow_color"], style.get("glow_strength", 25))
    drawing = ImageDraw.Draw(image)
    center = width // 2
    border = 55
    if style["border_width"]:
        drawing.rectangle((border, border, width-border, height-border),
                          outline=style["border_color"], width=style["border_width"])
        if style.get("double_border"):
            drawing.rectangle((border+12, border+12, width-border-12, height-border-12),
                              outline=style["border_color"], width=1)
    if style.get("left_bar"):
        drawing.rectangle((border+19, top, border+27, bottom), fill=style["accent_color"])
    _draw_ornament(drawing, style["top_ornament"], style["accent_color"], center, 175)
    quote_mark = style.get("quote_mark_char")
    if quote_mark:
        decorative = FontPair(int(size * 2.8), fonts)
        decorative.draw(drawing, (left - 5, top + 70), "“", style["quote_mark_color"])
        decorative.draw(drawing, (right - 60, bottom + 85), "”", style["quote_mark_color"])

    baseline = top + (available - block_height) / 2 + main.ascent
    text_boxes = []
    for index, line in enumerate(lines):
        x = left if style["layout"] == "left" else center - main.width(line) / 2
        text_boxes.extend(main.draw(drawing, (x, baseline + index * line_height), line, style["text_color"]))
    if any(box[0] < left - 1 or box[2] > right + 1 or box[1] < top or box[3] > bottom
           for box in text_boxes):
        raise ValueError("Rendered text extends outside the content area.")

    _draw_ornament(drawing, style["bottom_ornament"], style["accent_color"], center, height-230)
    drawing.line((center-130, height-182, center+130, height-182), fill=style["accent_color"], width=2)
    signature_boxes = author_pair.draw(drawing,
        (center - author_pair.width(author_text)/2, height-118), author_text, style["secondary_color"])
    if any(box[0] < left-1 or box[2] > right+1 or box[3] >= height-border for box in signature_boxes):
        raise ValueError("Author signature extends outside the card.")

    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    image.convert("RGB").save(path, format="JPEG", quality=95, subsampling=0)
    return {"text": text, "lines": lines, "font_size": size, "style": style_name,
            "content_bounds": [left, top, right, bottom], "text_boxes": text_boxes,
            "author_boxes": signature_boxes, "text_not_truncated": True,
            "fonts": {"cjk": main.cjk.getname()[0], "latin": main.latin.getname()[0]},
            "font_paths": fonts}


def create_contact_sheet(paths, output_path, labels=None, fonts=None):
    columns = 2 if len(paths) <= 4 else 3
    rows = math.ceil(len(paths) / columns)
    thumbnail_width, thumbnail_height, label_height, gap = 324, 432, 38, 20
    sheet = Image.new("RGB", (columns*(thumbnail_width+gap)+gap,
                    rows*(thumbnail_height+label_height+gap)+gap), (247,247,247))
    drawing = ImageDraw.Draw(sheet)
    label_font = FontPair(23, fonts or resolve_fonts())
    for index, path in enumerate(paths):
        x = gap + (index % columns)*(thumbnail_width+gap)
        y = gap + (index // columns)*(thumbnail_height+label_height+gap)
        with Image.open(path) as image:
            image.load()
            thumbnail = image.convert("RGB").resize((thumbnail_width, thumbnail_height), Image.Resampling.LANCZOS)
        sheet.paste(thumbnail, (x, y+label_height))
        label_font.draw(drawing, (x, y+27), labels[index] if labels else str(index+1), (40,40,40))
    sheet.save(output_path, format="JPEG", quality=92)
    return list(sheet.size)
