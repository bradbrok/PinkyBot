#!/usr/bin/env python3
"""
aiena_template_builder.py — Template-based article builder for aiena.it

Instead of patching preview HTML with regex, this module:
1. Extracts content from preview HTML (title, description, body, etc.)
2. Injects content into a canonical template
3. Produces structurally identical production articles

Usage:
    from aiena_template_builder import build_from_template, extract_content_from_preview

    content = extract_content_from_preview(preview_path)
    html = build_from_template(slug, content)

Author: Engineer (PinkyBot)
"""

import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Optional

# ── Paths ────────────────────────────────────────────────────────────────────
TEMPLATE_PATH = Path("/home/pinky/.pinkybot/templates/aiena_article_template.html")

# ── Italian months ───────────────────────────────────────────────────────────
MONTHS_IT_LONG = [
    'gennaio', 'febbraio', 'marzo', 'aprile', 'maggio', 'giugno',
    'luglio', 'agosto', 'settembre', 'ottobre', 'novembre', 'dicembre'
]


@dataclass
class ArticleContent:
    """Structured content extracted from a preview HTML."""
    slug: str
    title: str  # page <title> without "| AIena"
    headline: str  # <h1> article title
    subtitle: str  # <p class="article-subtitle">
    description: str  # meta description
    og_title: str  # og:title (often different from page title)
    og_description: str  # og:description
    og_image: str  # og:image URL
    category: str  # first badge category
    tags: list[str] = field(default_factory=list)  # additional category badges
    read_time: int = 7  # minutes
    date_iso: str = ""  # YYYY-MM-DD
    date_it: str = ""  # "8 maggio 2026"
    article_body: str = ""  # HTML content inside <article>, excluding ai-notice, bc-box, etc.


def date_it_long(d: date) -> str:
    """Format date as Italian: '8 maggio 2026'."""
    return f"{d.day} {MONTHS_IT_LONG[d.month - 1]} {d.year}"


def estimate_read_time(html: str) -> int:
    """Estimate read time from HTML content (words / 200 = minutes)."""
    text = re.sub(r'<[^>]+>', '', html)
    text = ' '.join(text.split())
    word_count = len(text.split())
    return max(1, word_count // 200)


def extract_content_from_preview(preview_path: Path) -> ArticleContent:
    """
    Extract structured content from a preview HTML file.

    This extracts only the TEXT content, not the structural elements like
    CSS, scripts, footer, bc-box, etc. Those come from the template.
    """
    html = preview_path.read_text(encoding="utf-8")
    slug = preview_path.stem

    # Extract <title> (without "| AIena")
    title_match = re.search(r'<title>([^<]+)</title>', html, re.IGNORECASE)
    title = title_match.group(1).replace(' | AIena', '').strip() if title_match else ""

    # Extract <h1> (headline)
    h1_match = re.search(r'<h1[^>]*class=["\'][^"\']*article-title["\'][^>]*>([^<]+)</h1>', html)
    if not h1_match:
        h1_match = re.search(r'<h1[^>]*>([^<]+)</h1>', html)
    headline = h1_match.group(1).strip() if h1_match else title

    # Extract subtitle
    subtitle_match = re.search(
        r'<p[^>]*class=["\'][^"\']*article-subtitle["\'][^>]*>(.*?)</p>',
        html, re.DOTALL | re.IGNORECASE
    )
    subtitle = subtitle_match.group(1).strip() if subtitle_match else ""
    # Clean up internal whitespace
    subtitle = re.sub(r'\s+', ' ', subtitle)

    # Extract meta description
    # NOTE: Use backreference \1 so closing quote matches only the same character
    # as the opening quote — prevents truncation when content contains apostrophes.
    desc_match = re.search(
        r'''<meta\s+name=["']description["']\s+content=(["'])(.*?)\1''',
        html, re.IGNORECASE
    )
    description = desc_match.group(2) if desc_match else subtitle[:160]

    # Extract og:title
    og_title_match = re.search(
        r'''<meta\s+property=["']og:title["']\s+content=(["'])(.*?)\1''',
        html, re.IGNORECASE
    )
    og_title = og_title_match.group(2) if og_title_match else headline

    # Extract og:description
    og_desc_match = re.search(
        r'''<meta\s+property=["']og:description["']\s+content=(["'])(.*?)\1''',
        html, re.IGNORECASE
    )
    og_description = og_desc_match.group(2) if og_desc_match else description

    # Extract og:image
    og_img_match = re.search(
        r'''<meta\s+property=["']og:image["']\s+content=(["'])(.*?)\1''',
        html, re.IGNORECASE
    )
    og_image = og_img_match.group(2) if og_img_match else "https://www.aiena.it/assets/aiena-og.jpg"

    # Extract category from badges
    # Pattern: <span class="badge badge-cat">Category</span>
    badge_matches = re.findall(
        r'<span\s+class=["\']badge\s+badge-cat["\'][^>]*>([^<]+)</span>',
        html, re.IGNORECASE
    )
    category = badge_matches[0] if badge_matches else "Indagine"
    tags = badge_matches[1:] if len(badge_matches) > 1 else []

    # Extract read time from byline
    read_match = re.search(r'(\d+)\s*min\s*lettura', html, re.IGNORECASE)
    read_time = int(read_match.group(1)) if read_match else 7

    # Extract date from byline or meta
    date_iso = date.today().isoformat()
    date_it = date_it_long(date.today())

    # Try to get date from article:published_time
    pub_time_match = re.search(
        r'<meta\s+property=["\']article:published_time["\']\s+content=["\'](\d{4}-\d{2}-\d{2})',
        html, re.IGNORECASE
    )
    if pub_time_match:
        date_iso = pub_time_match.group(1)
        try:
            d = date.fromisoformat(date_iso)
            date_it = date_it_long(d)
        except ValueError:
            pass

    # Extract article body - the actual content
    article_body = _extract_article_body(html)

    # Re-estimate read time from actual body
    if article_body:
        read_time = estimate_read_time(article_body)

    return ArticleContent(
        slug=slug,
        title=title,
        headline=headline,
        subtitle=subtitle,
        description=description,
        og_title=og_title,
        og_description=og_description,
        og_image=og_image,
        category=category,
        tags=tags,
        read_time=read_time,
        date_iso=date_iso,
        date_it=date_it,
        article_body=article_body,
    )


def _extract_article_body(html: str) -> str:
    """
    Extract the article body content from preview HTML.

    This extracts everything inside <article class="article-body"> EXCLUDING:
    - ai-notice (disclaimer)
    - diary-preview-banner
    - errore-box
    - bc-box (blockchain integrity)
    - segnala-box (we keep this - it's part of content)
    - sources (we keep this - it's part of content)
    - open-question (we keep this - it's part of content)
    """
    # Find the article body
    article_match = re.search(
        r'<article[^>]*class=["\'][^"\']*article-body["\'][^>]*>(.*?)</article>',
        html, re.DOTALL | re.IGNORECASE
    )
    if not article_match:
        # Fallback: try just <article>
        article_match = re.search(r'<article[^>]*>(.*?)</article>', html, re.DOTALL)

    if not article_match:
        return ""

    body = article_match.group(1)

    # Find the container div - get content between opening and closing div
    # Use a more robust approach: find first <div class="container"> and extract to matching </div>
    container_start = re.search(r'<div[^>]*class=["\'][^"\']*container["\'][^>]*>', body)
    if container_start:
        # Find content after opening tag
        start_pos = container_start.end()
        # Count div nesting to find matching close
        depth = 1
        pos = start_pos
        while depth > 0 and pos < len(body):
            next_open = body.find('<div', pos)
            next_close = body.find('</div>', pos)
            if next_close == -1:
                break
            if next_open != -1 and next_open < next_close:
                depth += 1
                pos = next_open + 4
            else:
                depth -= 1
                if depth == 0:
                    body = body[start_pos:next_close]
                else:
                    pos = next_close + 6

    # Remove AI notice (we have it in template)
    body = re.sub(
        r'<!--\s*AI\s*DISCLAIMER\s*-->\s*<div[^>]*class=["\'][^"\']*ai-notice["\'][^>]*>.*?</div>',
        '', body, flags=re.DOTALL | re.IGNORECASE
    )
    # Also try without comment marker
    body = re.sub(
        r'<div[^>]*class=["\'][^"\']*ai-notice["\'][^>]*>.*?</div>',
        '', body, flags=re.DOTALL | re.IGNORECASE
    )

    # Remove diary preview banner - more aggressive pattern
    # The banner has multiple nested divs, so we need to match the whole structure
    body = re.sub(
        r'<!--\s*DIARY\s*PREVIEW\s*BANNER\s*-->.*?(?=<p>|<h2>|$)',
        '', body, flags=re.DOTALL | re.IGNORECASE
    )
    # Match the div with all its content (multiple nested divs)
    body = re.sub(
        r'<div[^>]*class=["\'][^"\']*diary-preview-banner["\'][^>]*>.*?</div>\s*</div>\s*</div>',
        '', body, flags=re.DOTALL | re.IGNORECASE
    )
    # Simpler pattern - just the outer div to closing
    body = re.sub(
        r'<div[^>]*class=["\']diary-preview-banner["\'][^>]*>[\s\S]*?</div>\s*</div>\s*</div>\s*',
        '', body, flags=re.IGNORECASE
    )
    # Even simpler - remove anything with dpb- classes
    body = re.sub(
        r'<div[^>]*class=["\']dpb-[^"\']*["\'][^>]*>.*?</div>',
        '', body, flags=re.DOTALL | re.IGNORECASE
    )

    # Remove errore-box (we have it in template)
    # Pattern: the errore-box div followed by the trigger button div
    body = re.sub(
        r'<!--\s*ERRORE\s*BOX\s*-->.*?Segnala un errore in questo articolo</button>\s*</div>',
        '', body, flags=re.DOTALL | re.IGNORECASE
    )
    body = re.sub(
        r'<div[^>]*id=["\']errore-box["\'][^>]*>.*?</div>\s*</div>\s*</div>\s*<div[^>]*style=[^>]*>.*?</button>\s*</div>',
        '', body, flags=re.DOTALL | re.IGNORECASE
    )

    # Remove BC-BOX (we have it in template, OTS stamp fills it later)
    # Pattern with markers
    body = re.sub(
        r'<!--\s*BLOCKCHAIN\s*INTEGRITY.*?<!--\s*/BC-BOX\s*-->',
        '', body, flags=re.DOTALL | re.IGNORECASE
    )
    # Pattern without end marker - match the whole bc-box div
    body = re.sub(
        r'<div[^>]*class=["\'][^"\']*bc-box["\'][^>]*>[\s\S]*?</div>\s*</div>\s*</div>\s*</div>\s*</div>',
        '', body, flags=re.IGNORECASE
    )
    # Even more aggressive - remove everything from bc-box to end of content
    body = re.sub(
        r'<div[^>]*class=["\']bc-box["\'][^>]*id=["\']bc-box["\'].*',
        '', body, flags=re.DOTALL | re.IGNORECASE
    )
    body = re.sub(
        r'<div[^>]*id=["\']bc-box["\'].*',
        '', body, flags=re.DOTALL | re.IGNORECASE
    )

    # Clean up excessive whitespace while preserving structure
    body = re.sub(r'\n{3,}', '\n\n', body)
    body = body.strip()

    # Ensure proper indentation (6 spaces for article body content)
    lines = body.split('\n')
    # Don't re-indent if already indented
    if lines and not lines[0].startswith('      '):
        body = '\n'.join('      ' + line if line.strip() else '' for line in lines)

    return body


def build_from_template(slug: str, content: ArticleContent) -> str:
    """
    Build a production-ready article HTML from the template.

    Args:
        slug: Article slug (used for URLs)
        content: ArticleContent dataclass with extracted content

    Returns:
        Complete HTML string ready to write to disk
    """
    if not TEMPLATE_PATH.exists():
        raise FileNotFoundError(f"Template not found: {TEMPLATE_PATH}")

    template = TEMPLATE_PATH.read_text(encoding="utf-8")

    def tag_slug(tag: str) -> str:
        """Convert tag text to URL-safe slug (lowercase, accents removed, spaces as +)."""
        import unicodedata
        nfkd = unicodedata.normalize('NFD', tag.lower())
        ascii_tag = ''.join(c for c in nfkd if not unicodedata.combining(c))
        return ascii_tag.replace(' ', '+')

    # Build tags HTML — extra badge-cat tags as clickable links
    tags_html = ""
    for tag in content.tags:
        slug_q = tag_slug(tag)
        tags_html += f'        <a href="/archivio.html?q={slug_q}" class="badge badge-cat" style="text-decoration:none;cursor:pointer;">{tag}</a>\n'

    # Category slug for the main category link
    category_slug = tag_slug(content.category)

    # Replace all placeholders
    replacements = {
        "{{SLUG}}": slug,
        "{{TITLE}}": content.title,
        "{{HEADLINE}}": content.headline,
        "{{SUBTITLE}}": content.subtitle,
        "{{DESCRIPTION}}": content.description,
        "{{OG_TITLE}}": content.og_title,
        "{{OG_DESCRIPTION}}": content.og_description,
        "{{OG_IMAGE}}": content.og_image,
        "{{CATEGORY}}": content.category,
        "{{CATEGORY_SLUG}}": category_slug,
        "{{READ_TIME}}": str(content.read_time),
        "{{DATE_ISO}}": content.date_iso,
        "{{DATE_IT}}": content.date_it,
        "{{ARTICLE_BODY}}": content.article_body,
        "{{TAGS_HTML}}": tags_html.rstrip(),
    }

    html = template
    for placeholder, value in replacements.items():
        html = html.replace(placeholder, value)

    return html


def transform_preview_to_production_v2(
    preview_path: Path,
    articles_path: Path,
    slug: str,
    pub_date: Optional[date] = None
) -> bool:
    """
    Transform a preview HTML to production using the template-based approach.

    This is the new version of transform_preview_to_production that:
    1. Extracts content from preview
    2. Builds from canonical template
    3. Writes to articles directory

    Args:
        preview_path: Path to preview HTML file
        articles_path: Path where production HTML should be written
        slug: Article slug
        pub_date: Publication date (defaults to today)

    Returns:
        True on success, False on failure
    """
    try:
        # Extract content from preview
        content = extract_content_from_preview(preview_path)

        # Override date if provided
        if pub_date:
            content.date_iso = pub_date.isoformat()
            content.date_it = date_it_long(pub_date)
        elif not content.date_iso:
            # Default to today
            today = date.today()
            content.date_iso = today.isoformat()
            content.date_it = date_it_long(today)

        # Build from template
        html = build_from_template(slug, content)

        # Write to disk
        articles_path.write_text(html, encoding="utf-8")

        return True
    except Exception as e:
        print(f"[ERROR] transform_preview_to_production_v2 failed: {e}")
        import traceback
        traceback.print_exc()
        return False


# ── Test utility ─────────────────────────────────────────────────────────────

def test_extraction(preview_path: Path) -> None:
    """
    Test content extraction from a preview file.
    Prints extracted content for verification.
    """
    print(f"Testing extraction from: {preview_path}")
    print("=" * 60)

    content = extract_content_from_preview(preview_path)

    print(f"Slug: {content.slug}")
    print(f"Title: {content.title}")
    print(f"Headline: {content.headline}")
    print(f"Subtitle: {content.subtitle[:100]}..." if len(content.subtitle) > 100 else f"Subtitle: {content.subtitle}")
    print(f"Description: {content.description[:100]}..." if len(content.description) > 100 else f"Description: {content.description}")
    print(f"OG Title: {content.og_title}")
    print(f"OG Description: {content.og_description[:100]}..." if len(content.og_description) > 100 else f"OG Description: {content.og_description}")
    print(f"OG Image: {content.og_image}")
    print(f"Category: {content.category}")
    print(f"Tags: {content.tags}")
    print(f"Read time: {content.read_time} min")
    print(f"Date ISO: {content.date_iso}")
    print(f"Date IT: {content.date_it}")
    print(f"Article body length: {len(content.article_body)} chars")
    print()
    print("First 500 chars of article body:")
    print("-" * 40)
    print(content.article_body[:500])
    print("-" * 40)


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        path = Path(sys.argv[1])
        if path.exists():
            test_extraction(path)
        else:
            print(f"File not found: {path}")
            sys.exit(1)
    else:
        # Default test with decreto article
        test_path = Path("/var/www/aiena.it/articles/decreto-commissari-ponte-stretto-corte-conti.html")
        if test_path.exists():
            test_extraction(test_path)
        else:
            print("Usage: python aiena_template_builder.py <preview_path>")
