#!/usr/bin/env python3
"""
BitcoinMarket.net — Branded Telegram Card Generator
Produces 1200x630 branded image cards for Telegram posts.
"""
import sys
import math
import textwrap
from PIL import Image, ImageDraw, ImageFont, ImageFilter

FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT_REG  = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

BG_TOP    = (10, 10, 26)     # #0a0a1a
BG_MID    = (26, 26, 46)     # #1a1a2e
BG_BOT    = (13, 27, 42)     # #0d1b2a
ORANGE    = (247, 147, 26)   # #f7931a
WHITE     = (255, 255, 255)
GRAY      = (136, 146, 164)  # #8892a4
DARK_GRAY = (74, 85, 104)    # #4a5568


def make_gradient(w, h):
    img = Image.new("RGB", (w, h))
    for y in range(h):
        t = y / h
        if t < 0.5:
            r = int(BG_TOP[0] + (BG_MID[0]-BG_TOP[0]) * (t*2))
            g = int(BG_TOP[1] + (BG_MID[1]-BG_TOP[1]) * (t*2))
            b = int(BG_TOP[2] + (BG_MID[2]-BG_TOP[2]) * (t*2))
        else:
            r = int(BG_MID[0] + (BG_BOT[0]-BG_MID[0]) * ((t-0.5)*2))
            g = int(BG_MID[1] + (BG_BOT[1]-BG_MID[1]) * ((t-0.5)*2))
            b = int(BG_MID[2] + (BG_BOT[2]-BG_MID[2]) * ((t-0.5)*2))
        for x in range(w):
            img.putpixel((x, y), (r, g, b))
    return img


def draw_glow_circle(draw, cx, cy, r, color, alpha):
    """Draw a soft glowing circle."""
    for dr in range(r, 0, -2):
        a = int(alpha * (dr/r)**2)
        col = color + (a,)
        draw.ellipse([cx-dr, cy-dr, cx+dr, cy+dr], fill=col)


def wrap_text(text, font, max_width, draw):
    """Wrap text to fit max_width pixels."""
    words = text.split()
    lines = []
    current = ""
    for word in words:
        test = (current + " " + word).strip()
        w = draw.textlength(test, font=font)
        if w <= max_width:
            current = test
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def generate_card(headline: str, category: str = "NEWS", output_path: str = "/tmp/btm_card.jpg") -> str:
    W, H = 1200, 630

    # Base gradient
    base = make_gradient(W, H)
    overlay = Image.new("RGBA", (W, H), (0,0,0,0))
    draw = ImageDraw.Draw(overlay)

    # Glow circles (decorative, like the SVG)
    draw_glow_circle(draw, 100, 100, 220, ORANGE, 18)
    draw_glow_circle(draw, 1100, 530, 270, ORANGE, 12)
    draw_glow_circle(draw, 600, 315, 420, (26, 58, 92), 25)

    # Merge
    img = base.convert("RGBA")
    img = Image.alpha_composite(img, overlay)
    img = img.convert("RGB")
    draw = ImageDraw.Draw(img)

    # Left accent bar
    for i in range(8):
        alpha = 255 - i * 25
        draw.rectangle([60-i, 80, 61-i, H-80], fill=ORANGE + (alpha,) if False else ORANGE)
    draw.rectangle([60, 80, 67, H-80], fill=ORANGE)

    # Category tag
    try:
        font_tag = ImageFont.truetype(FONT_BOLD, 22)
        font_headline = ImageFont.truetype(FONT_BOLD, 62)
        font_sub = ImageFont.truetype(FONT_BOLD, 30)
        font_brand = ImageFont.truetype(FONT_BOLD, 28)
        font_brand_small = ImageFont.truetype(FONT_REG, 20)
    except:
        font_tag = font_headline = font_sub = font_brand = font_brand_small = ImageFont.load_default()

    # Category pill
    tag_text = f"  {category}  "
    tag_w = int(draw.textlength(tag_text, font=font_tag)) + 4
    draw.rounded_rectangle([100, 90, 100+tag_w, 90+36], radius=6, fill=ORANGE)
    draw.text((102, 93), tag_text.strip(), font=font_tag, fill=(0,0,0))

    # Headline (wrapped)
    max_w = 980
    lines = wrap_text(headline, font_headline, max_w, draw)
    # Limit to 3 lines
    if len(lines) > 3:
        lines = lines[:3]
        lines[-1] = lines[-1].rstrip() + "…"

    y_start = 160
    line_height = 78
    for i, line in enumerate(lines):
        # Shadow
        draw.text((101, y_start + i*line_height + 2), line, font=font_headline, fill=(0,0,0,180))
        draw.text((100, y_start + i*line_height), line, font=font_headline, fill=WHITE)

    # Divider line
    y_div = y_start + len(lines) * line_height + 30
    draw.rectangle([100, y_div, 700, y_div+3], fill=ORANGE)

    # Bottom branding
    btc_y = H - 90
    # ₿ symbol
    draw.text((100, btc_y - 8), "₿", font=font_sub, fill=ORANGE)
    # Site name
    draw.text((142, btc_y), "BitcoinMarket.net", font=font_brand, fill=WHITE)
    # Tagline
    draw.text((142, btc_y + 36), "Compare · Guide · News", font=font_brand_small, fill=GRAY)

    # Bottom right: date placeholder
    from datetime import datetime
    date_str = datetime.now().strftime("%d %b %Y").upper()
    date_font = font_brand_small
    date_w = int(draw.textlength(date_str, font=date_font))
    draw.text((W - date_w - 60, btc_y + 18), date_str, font=date_font, fill=DARK_GRAY)

    img.save(output_path, "JPEG", quality=92)
    return output_path


if __name__ == "__main__":
    headline = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "Test Headline: Bitcoin raggiunge nuovi massimi storici nel 2026"
    out = generate_card(headline)
    print(f"Saved: {out}")
