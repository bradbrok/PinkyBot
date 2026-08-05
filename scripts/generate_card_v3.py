#!/usr/bin/env python3
"""
BitcoinMarket.net — Card Generator v3.1
Foto locale per topic + gradient + testo CoinTelegraph-style.
₿ usa DejaVuSans-Bold per rendering corretto.
"""
import sys, io, urllib.request, urllib.parse, re
from PIL import Image, ImageDraw, ImageFont, ImageEnhance
from datetime import datetime
from pathlib import Path

W, H   = 1200, 630
ORANGE = (247, 147, 26)
WHITE  = (255, 255, 255)
BLACK  = (0, 0, 0)

SCRIPTS_DIR = Path(__file__).parent
FONTS_DIR   = SCRIPTS_DIR / "fonts"
BG_DIR      = SCRIPTS_DIR / "bg_images"
CACHED_DIR  = BG_DIR / "cached"

DEJAVU_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

# Topic → local image filename (base dir first, then cached dir)
TOPIC_LOCAL = {
    "bitcoin":      "bitcoin.jpg",         # BTC/ETH coins on dark bg
    "chart":        "chart.jpg",           # Trading chart with magnifying glass
    "exchange":     "crypto2.jpg",         # Exchange app on phone
    "finance":      "finance.jpg",         # Stock market ticker
    "quantum":      "cached/quantum.jpg",  # Quantum computer lab
    "regulation":   "cached/regulation_eu.jpg",  # European Parliament
    "wallstreet":   "cached/wallstreet.jpg",     # NYSE exterior
    "fintech":      "cached/fintech.jpg",  # Mobile payment/money
    "investment":   "cached/investment.jpg",     # Investment/work
}

# Fallback Pexels URLs if local file missing
TOPIC_FALLBACK = {
    "bitcoin":    "https://images.pexels.com/photos/844124/pexels-photo-844124.jpeg?w=1200&h=630&fit=crop",
    "chart":      "https://images.pexels.com/photos/6781341/pexels-photo-6781341.jpeg?w=1200&h=630&fit=crop",
    "exchange":   "https://images.pexels.com/photos/8370752/pexels-photo-8370752.jpeg?w=1200&h=630&fit=crop",
    "finance":    "https://images.pexels.com/photos/210607/pexels-photo-210607.jpeg?w=1200&h=630&fit=crop",
    "quantum":    "https://images.pexels.com/photos/8386440/pexels-photo-8386440.jpeg?w=1200&h=630&fit=crop",
    "regulation": "https://images.pexels.com/photos/5668882/pexels-photo-5668882.jpeg?w=1200&h=630&fit=crop",
    "wallstreet": "https://images.pexels.com/photos/7567443/pexels-photo-7567443.jpeg?w=1200&h=630&fit=crop",
    "fintech":    "https://images.pexels.com/photos/8370752/pexels-photo-8370752.jpeg?w=1200&h=630&fit=crop",
    "investment": "https://images.pexels.com/photos/210607/pexels-photo-210607.jpeg?w=1200&h=630&fit=crop",
}

# Keyword → (display category, topic key)
CATEGORIES = {
    "bitcoin":   ("BITCOIN",          "bitcoin"),
    "btc":       ("BITCOIN",          "bitcoin"),
    "etf":       ("ETF",              "chart"),
    "etp":       ("ETF",              "chart"),
    "rally":     ("MERCATI",          "chart"),
    "breakout":  ("MERCATI",          "chart"),
    "80k":       ("MERCATI",          "bitcoin"),
    "quantum":   ("TECNOLOGIA",       "quantum"),
    "mica":      ("REGOLAMENTAZIONE", "regulation"),
    "mifid":     ("REGOLAMENTAZIONE", "regulation"),
    "regulation":("REGOLAMENTAZIONE", "regulation"),
    "regolament":("REGOLAMENTAZIONE", "regulation"),
    "casp":      ("REGOLAMENTAZIONE", "regulation"),
    "exchange":  ("EXCHANGE",         "exchange"),
    "kraken":    ("EXCHANGE",         "exchange"),
    "binance":   ("EXCHANGE",         "exchange"),
    "bybit":     ("EXCHANGE",         "exchange"),
    "coinbase":  ("EXCHANGE",         "exchange"),
    "bitvavo":   ("EXCHANGE",         "exchange"),
    "kucoin":    ("EXCHANGE",         "exchange"),
    "bitget":    ("EXCHANGE",         "exchange"),
    "bingx":     ("EXCHANGE",         "exchange"),
    "nexo":      ("FINANZA",          "finance"),
    "morgan":    ("FINANZA",          "wallstreet"),
    "stanley":   ("FINANZA",          "wallstreet"),
    "stablecoin":("STABLECOIN",       "wallstreet"),
    "revolut":   ("FINTECH",          "fintech"),
    "kyc":       ("PRIVACY",          "bitcoin"),
    "acquisi":   ("FINANZA",          "wallstreet"),
    "compra":    ("BITCOIN",          "bitcoin"),
    "las vegas": ("BITCOIN",          "bitcoin"),
    "inflow":    ("ETF",              "chart"),
    "conferenc": ("BITCOIN",          "bitcoin"),
    "fund":      ("FINANZA",          "investment"),
    "index":     ("MERCATI",          "investment"),
    "cop":       ("FINANZA",          "finance"),
    "coin50":    ("MERCATI",          "investment"),
    "sec":       ("FINANZA",          "finance"),
    "cftc":      ("FINANZA",          "finance"),
    "multa":     ("FINANZA",          "finance"),
    "strafe":    ("FINANZA",          "finance"),
    "fine":      ("FINANZA",          "finance"),
    "bulgar":    ("FINANZA",          "finance"),
    "italia":    ("ITALIA",           "chart"),
    "europe":    ("EUROPA",           "regulation"),
    "europa":    ("EUROPA",           "regulation"),
}

def get_cat_and_topic(headline: str):
    hl = headline.lower()
    # Check longer keywords first (e.g. "las vegas" before "bitcoin")
    for k, (cat, topic) in sorted(CATEGORIES.items(), key=lambda x: -len(x[0])):
        if k in hl:
            return cat, topic
    return "NEWS", "bitcoin"

def lf(name: str, size: int):
    path = str(FONTS_DIR / name)
    try:
        return ImageFont.truetype(path, size)
    except:
        return ImageFont.load_default()

def lf_system(path: str, size: int):
    try:
        return ImageFont.truetype(path, size)
    except:
        return ImageFont.load_default()

def wrap(text, font, max_w, draw, max_lines=2):
    words = text.split()
    lines, cur = [], ""
    for w in words:
        t = (cur + " " + w).strip()
        if draw.textlength(t, font=font) <= max_w:
            cur = t
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    lines = lines[:max_lines]
    if len(lines) == max_lines:
        lines[-1] = lines[-1].rstrip() + "…"
    return lines

def load_bg(topic: str) -> Image.Image:
    """Load background from local file, fallback to URL."""
    local_rel = TOPIC_LOCAL.get(topic, "bitcoin.jpg")
    local_path = BG_DIR / local_rel

    if local_path.exists():
        img = Image.open(local_path).convert("RGB")
        return img.resize((W, H), Image.LANCZOS)

    # Fallback: download from Pexels
    url = TOPIC_FALLBACK.get(topic, TOPIC_FALLBACK["bitcoin"])
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return Image.open(io.BytesIO(r.read())).convert("RGB").resize((W, H), Image.LANCZOS)
    except Exception as e:
        # Last resort: solid dark bg
        img = Image.new("RGB", (W, H), (10, 15, 25))
        return img

def generate_card(headline: str, category: str = None, output_path: str = "/tmp/btm_card.jpg",
                  topic_override: str = None) -> str:
    cat, topic = get_cat_and_topic(headline)
    if category:
        cat = category.upper()
    if topic_override:
        topic = topic_override

    bg = load_bg(topic)
    bg = ImageEnhance.Contrast(bg).enhance(1.15)
    bg = ImageEnhance.Color(bg).enhance(1.10)
    bg = ImageEnhance.Sharpness(bg).enhance(1.2)

    canvas = bg.convert("RGBA")
    ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d  = ImageDraw.Draw(ov)

    # Gradient bottom 50%
    bottom_start = int(H * 0.45)
    for y in range(bottom_start, H):
        t = (y - bottom_start) / (H - bottom_start)
        alpha = int(245 * (t ** 0.45))
        d.rectangle([0, y, W, y + 1], fill=(4, 5, 12, alpha))

    # Logo pill top-left
    d.rounded_rectangle([16, 16, 340, 62], radius=8, fill=(0, 0, 0, 160))
    # Orange bottom bar
    d.rectangle([0, H - 4, W, H], fill=ORANGE + (255,))

    canvas = Image.alpha_composite(canvas, ov).convert("RGB")
    draw = ImageDraw.Draw(canvas)

    # Fonts
    f_btc  = lf_system(DEJAVU_BOLD, 28)   # ₿ symbol - DejaVu only
    f_logo = lf("Montserrat-Bold.ttf", 28) # brand name
    f_cat  = lf("Montserrat-Bold.ttf", 18)
    f_h    = lf("Montserrat-Bold.ttf", 62)
    f_foot = lf("Montserrat-Regular.ttf", 18)

    # Logo: "B" in Montserrat (orange) + thin bars = ₿ effect, then "itcoinMarket.net"
    # (DejaVu renders ₿ as an empty box placeholder — use Montserrat B + bars instead)
    f_B_logo = lf("Montserrat-Bold.ttf", 28)
    b_char = "B"
    bb = draw.textbbox((0, 0), b_char, font=f_B_logo)
    bw = bb[2] - bb[0]
    bh = bb[3] - bb[1]
    bx, by = 28, 24
    draw.text((bx, by), b_char, font=f_B_logo, fill=ORANGE)
    # Two horizontal bars through B
    bar_x1 = bx - 3
    bar_x2 = bx + bw + 3
    bar_t = 2  # thickness
    bar_y1 = by + bb[1] + int(bh * 0.30)
    bar_y2 = by + bb[1] + int(bh * 0.65)
    draw.rectangle([bar_x1, bar_y1, bar_x2, bar_y1 + bar_t], fill=ORANGE)
    draw.rectangle([bar_x1, bar_y2, bar_x2, bar_y2 + bar_t], fill=ORANGE)
    draw.text((bx + bw + 4, by), "itcoinMarket.net", font=f_logo, fill=WHITE)

    # Category badge
    cw = int(draw.textlength(cat, font=f_cat))
    draw.rounded_rectangle([28, H - 220, 28 + cw + 24, H - 220 + 34], radius=16, fill=ORANGE)
    draw.text((28 + 12, H - 217), cat, font=f_cat, fill=BLACK)

    # Headline with shadow
    lines = wrap(headline, f_h, 1145, draw, max_lines=2)
    lh = 74
    y0 = H - 178 if len(lines) == 2 else H - 140
    for i, line in enumerate(lines):
        draw.text((29, y0 + i * lh + 2), line, font=f_h, fill=(0, 0, 0, 170))
        draw.text((28, y0 + i * lh),     line, font=f_h, fill=WHITE)

    # Footer
    date_str = datetime.now().strftime("%d %b %Y").upper()
    draw.text((28, H - 30), f"bitcoinmarket.net/magazine  ·  {date_str}", font=f_foot, fill=(170, 180, 195))

    canvas.save(output_path, "JPEG", quality=95)
    return output_path

if __name__ == "__main__":
    headline = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "Bitcoin si Avvicina a $80.000: Istituzioni Guidano il Rally"
    out = generate_card(headline)
    print(f"Saved: {out}")
    # Print detected topic for debugging
    _, topic = get_cat_and_topic(headline)
    print(f"Topic: {topic} → {TOPIC_LOCAL.get(topic, 'bitcoin.jpg')}")
