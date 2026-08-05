#!/usr/bin/env python3
"""
BitcoinMarket.net — Style 2 Card Generator
Foto piena + sfumatura scura in basso + testo + branding.
"""
import sys, os, re, urllib.request, urllib.parse, json
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageEnhance
from datetime import datetime
from pathlib import Path

FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT_REG  = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
ORANGE = (247, 147, 26)
WHITE  = (255, 255, 255)
GRAY   = (160, 170, 185)
W, H   = 1200, 630

UNSPLASH_QUERIES = {
    "bitcoin":      "bitcoin cryptocurrency gold",
    "btc":          "bitcoin cryptocurrency gold",
    "ethereum":     "ethereum blockchain digital",
    "exchange":     "cryptocurrency trading screen",
    "mica":         "european union regulation finance",
    "etf":          "stock market investment fund",
    "kraken":       "crypto exchange trading",
    "binance":      "crypto exchange trading",
    "bybit":        "crypto exchange trading",
    "coinbase":     "crypto exchange trading",
    "strategy":     "finance investment corporate",
    "quantum":      "quantum computing technology",
    "sec":          "regulation finance law",
    "cftc":         "regulation finance law",
    "stablecoin":   "digital currency finance",
    "morgan":       "wall street finance bank",
    "nexo":         "cryptocurrency finance",
    "revolut":      "fintech digital banking",
    "default":      "bitcoin blockchain technology dark",
}

def get_query_for(headline: str) -> str:
    hl = headline.lower()
    for key, q in UNSPLASH_QUERIES.items():
        if key in hl:
            return q
    return UNSPLASH_QUERIES["default"]

def fetch_bg_image(query: str, seed: int = 1) -> Image.Image:
    """Fetch a relevant background image from Unsplash Source."""
    encoded = urllib.parse.quote(query)
    url = f"https://source.unsplash.com/1200x630/?{encoded}&sig={seed}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            import io
            return Image.open(io.BytesIO(r.read())).resize((W, H))
    except:
        # Fallback: dark gradient
        img = Image.new("RGB", (W, H), (10, 12, 22))
        return img

def load_font(path, size):
    try: return ImageFont.truetype(path, size)
    except: return ImageFont.load_default()

def wrap_text(text, font, max_width, draw, max_lines=3):
    words = text.split()
    lines, current = [], ""
    for word in words:
        test = (current + " " + word).strip()
        if draw.textlength(test, font=font) <= max_width:
            current = test
        else:
            if current: lines.append(current)
            current = word
    if current: lines.append(current)
    lines = lines[:max_lines]
    if len(lines) == max_lines and len(text.split()) > sum(len(l.split()) for l in lines):
        lines[-1] = lines[-1].rstrip() + "…"
    return lines

def generate_card(headline: str, category: str = "NEWS",
                  output_path: str = "/tmp/btm_card.jpg", seed: int = 1) -> str:
    query = get_query_for(headline)
    bg = fetch_bg_image(query, seed)

    # Darken full image
    bg = ImageEnhance.Brightness(bg).enhance(0.45)
    canvas = bg.convert("RGBA")

    # Dark gradient from bottom (covers ~65% of height)
    grad = Image.new("RGBA", (W, H), (0,0,0,0))
    gd = ImageDraw.Draw(grad)
    start_y = int(H * 0.25)
    for y in range(start_y, H):
        t = (y - start_y) / (H - start_y)
        alpha = int(215 * (t ** 0.55))
        gd.rectangle([0, y, W, y+1], fill=(5, 6, 16, alpha))
    # Subtle top vignette
    for y in range(0, 80):
        alpha = int(120 * (1 - y/80))
        gd.rectangle([0, y, W, y+1], fill=(0, 0, 0, alpha))

    canvas = Image.alpha_composite(canvas, grad).convert("RGB")
    draw = ImageDraw.Draw(canvas)

    f_tag   = load_font(FONT_BOLD, 21)
    f_head  = load_font(FONT_BOLD, 58)
    f_brand = load_font(FONT_BOLD, 26)
    f_small = load_font(FONT_REG,  19)

    # Top: category tag + date
    tag_pad = 14
    tag_text = f" {category} "
    tag_w = int(draw.textlength(tag_text, font=f_tag)) + tag_pad
    draw.rounded_rectangle([50, 44, 50+tag_w, 44+34], radius=5, fill=ORANGE)
    draw.text((50 + tag_pad//2, 47), category, font=f_tag, fill=(0,0,0))
    date_str = datetime.now().strftime("%d %b %Y").upper()
    draw.text((W - 60 - int(draw.textlength(date_str, font=f_small)), 52),
              date_str, font=f_small, fill=(200, 210, 220))

    # Headline (bottom area)
    lines = wrap_text(headline, f_head, 1100, draw, max_lines=3)
    total_h = len(lines) * 74
    y0 = H - total_h - 90
    for i, line in enumerate(lines):
        # Soft shadow
        draw.text((51, y0 + i*74 + 2), line, font=f_head, fill=(0,0,0,200))
        draw.text((50, y0 + i*74),     line, font=f_head, fill=WHITE)

    # Orange accent line above branding
    draw.rectangle([50, H-68, 400, H-65], fill=ORANGE)

    # Bottom branding
    draw.text((50, H-58), "₿  BitcoinMarket.net", font=f_brand, fill=WHITE)
    site_label = "bitcoinmarket.net/magazine"
    draw.text((W - 60 - int(draw.textlength(site_label, font=f_small)), H-52),
              site_label, font=f_small, fill=GRAY)

    canvas.save(output_path, "JPEG", quality=93)
    return output_path


if __name__ == "__main__":
    if len(sys.argv) > 1:
        headline = " ".join(sys.argv[1:])
    else:
        headline = "Bitcoin si Avvicina a $80.000: Cosa Sta Guidando il Recupero di Aprile"
    out = generate_card(headline)
    print(f"Saved: {out}")
