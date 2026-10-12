"""
epub_builder.py — shared EPUB assembly library

Provides:
    BookConfig  — dataclass describing a book
    Section     — dataclass describing a named group of chapters
    build_epub  — assembles the EPUB from a cache + config
    load_cache  — loads a chapter cache JSON file
    save_cache  — saves a chapter cache JSON file

Known ebooklib quirks fixed here:
    - Font media-type must be 'application/x-font-ttf' for TTF files or Apple
      Books refuses to apply them.
    - ebooklib strips class attributes from <body> tags when serialising XHTML;
      we set the class on <body> via a wrapping template instead of relying on
      ebooklib to preserve it.
    - ebooklib strips inline style attributes; chapter CSS is delivered via a
      linked stylesheet, never inline styles.
    - Cover images are embedded natively (as an OPF cover item + guide entry)
      so no repack through Calibre or an online converter is needed.

Requirements:
    pip install ebooklib lxml
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from ebooklib import epub


# ── Dataclasses ───────────────────────────────────────────────────────────────

@dataclass
class Section:
    """A named group of chapters that appears as a top-level TOC entry."""
    heading: str
    toc_label: str
    chapter_uids: list[str]
    subtitle: str = ""


@dataclass
class BookConfig:
    """Everything needed to assemble one EPUB."""
    title: str
    author: str
    cache_file: Path
    output_file: Path
    sections: list[Section]
    font_dir: Path | None         = None
    cover_image: Path | None      = None
    summary_html: str             = ""
    identifier: str               = ""
    language: str                 = "en"


# ── Cache helpers ─────────────────────────────────────────────────────────────

def load_cache(path: Path) -> dict:
    """Return the chapter cache dict, or {} if the file doesn't exist yet."""
    p = Path(path)
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return {}


def save_cache(path: Path, cache: dict) -> None:
    """Write the chapter cache dict to disk."""
    Path(path).write_text(
        json.dumps(cache, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# ── Stylesheet ────────────────────────────────────────────────────────────────

# Delivered as a linked CSS file so ebooklib never strips it.
_CSS = """\
@font-face {
    font-family: 'BookFont';
    src: url('../fonts/regular.ttf');
    font-weight: normal;
    font-style: normal;
}
@font-face {
    font-family: 'BookFont';
    src: url('../fonts/bold.ttf');
    font-weight: bold;
    font-style: normal;
}
@font-face {
    font-family: 'BookFont';
    src: url('../fonts/italic.ttf');
    font-weight: normal;
    font-style: italic;
}

body {
    font-family: 'BookFont', Georgia, serif;
    font-size: 1em;
    line-height: 1.75;
    margin: 1.5em 1.5em;
    color: #1a1a1a;
}

h1 {
    font-size: 1.6em;
    font-weight: bold;
    margin-top: 2em;
    margin-bottom: 0.4em;
    line-height: 1.2;
}

h2 {
    font-size: 1.15em;
    font-weight: normal;
    font-style: italic;
    margin-top: 0.3em;
    margin-bottom: 2em;
    color: #555;
}

h3 {
    font-size: 1.1em;
    font-weight: bold;
    margin-top: 1.8em;
    margin-bottom: 0.5em;
}

p {
    margin: 0;
    text-indent: 1.5em;
}

p:first-of-type,
h1 + p,
h2 + p,
h3 + p {
    text-indent: 0;
}

.chapter-title {
    text-indent: 0;
    font-size: 1.2em;
    font-weight: bold;
    margin-top: 2em;
    margin-bottom: 1.5em;
    text-align: center;
}

/* Summary / blurb page */
.summary-page p {
    text-indent: 0;
    margin-bottom: 0.8em;
}

/* Section divider page */
.section-page {
    text-align: center;
    margin-top: 30%;
}
.section-page h1 {
    margin-bottom: 0.3em;
}
.section-page h2 {
    margin-top: 0;
}
"""

_CSS_NO_FONTS = """\
body {
    font-family: Georgia, serif;
    font-size: 1em;
    line-height: 1.75;
    margin: 1.5em 1.5em;
    color: #1a1a1a;
}

h1 { font-size: 1.6em; font-weight: bold; margin-top: 2em; margin-bottom: 0.4em; line-height: 1.2; }
h2 { font-size: 1.15em; font-weight: normal; font-style: italic; margin-top: 0.3em; margin-bottom: 2em; color: #555; }
h3 { font-size: 1.1em; font-weight: bold; margin-top: 1.8em; margin-bottom: 0.5em; }

p { margin: 0; text-indent: 1.5em; }
p:first-of-type, h1 + p, h2 + p, h3 + p { text-indent: 0; }

.chapter-title { text-indent: 0; font-size: 1.2em; font-weight: bold; margin-top: 2em; margin-bottom: 1.5em; text-align: center; }
.summary-page p { text-indent: 0; margin-bottom: 0.8em; }
.section-page { text-align: center; margin-top: 30%; }
.section-page h1 { margin-bottom: 0.3em; }
.section-page h2 { margin-top: 0; }
"""


# ── XHTML helpers ─────────────────────────────────────────────────────────────

def _xhtml(uid: str, title: str, body_content: str, css_href: str,
           body_class: str = "") -> epub.EpubHtml:
    """
    Build an EpubHtml item.

    We write the full XHTML ourselves instead of letting ebooklib template it,
    which prevents the body-class-stripping bug.
    """
    cls_attr = f' class="{body_class}"' if body_class else ""
    content = f"""\
<?xml version='1.0' encoding='utf-8'?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
<head>
  <meta charset="utf-8"/>
  <title>{_esc(title)}</title>
  <link rel="stylesheet" type="text/css" href="{css_href}"/>
</head>
<body{cls_attr}>
{body_content}
</body>
</html>"""
    item = epub.EpubHtml(uid=uid, file_name=f"{uid}.xhtml", title=title)
    item.content = content.encode("utf-8")
    # Tell ebooklib not to re-template the content we already wrote
    item.is_linear = True
    return item


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


# ── Font loading ───────────────────────────────────────────────────────────────

_FONT_NAMES = {
    "regular": ["regular.ttf", "Regular.ttf", "normal.ttf"],
    "bold":    ["bold.ttf", "Bold.ttf"],
    "italic":  ["italic.ttf", "Italic.ttf"],
}


def _load_fonts(book: epub.EpubBook, font_dir: Path) -> bool:
    """
    Add TTF fonts from font_dir to the EPUB.

    Returns True if at least the regular font was found.
    Media-type must be 'application/x-font-ttf'; anything else and Apple Books
    silently ignores the font.
    """
    if not font_dir or not Path(font_dir).is_dir():
        return False

    found_any = False
    for role, candidates in _FONT_NAMES.items():
        for name in candidates:
            fp = Path(font_dir) / name
            if fp.exists():
                font_item = epub.EpubItem(
                    uid=f"font-{role}",
                    file_name=f"fonts/{name}",
                    media_type="application/x-font-ttf",
                    content=fp.read_bytes(),
                )
                book.add_item(font_item)
                found_any = True
                break  # found this role, move to next

    return found_any


# ── Cover ─────────────────────────────────────────────────────────────────────

def _add_cover(book: epub.EpubBook, cover_path: Path) -> epub.EpubHtml | None:
    """
    Embed a cover image natively.

    Adds the image as the OPF cover item and creates a cover XHTML page.
    Returns the cover page item so it can be inserted at the spine head.
    """
    p = Path(cover_path)
    if not p.exists():
        return None

    suffix = p.suffix.lower()
    media_types = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                   ".gif": "image/gif", ".webp": "image/webp"}
    mt = media_types.get(suffix, "image/jpeg")

    book.set_cover(f"cover{suffix}", p.read_bytes(), create_page=False)

    # Minimal cover page — just the image, no caption
    cover_page = _xhtml(
        uid="cover-page",
        title="Cover",
        body_content=f'<div style="text-align:center;"><img src="cover{suffix}" alt="Cover" style="max-width:100%;"/></div>',
        css_href="../styles/book.css",
    )
    book.add_item(cover_page)
    return cover_page


# ── Main builder ──────────────────────────────────────────────────────────────

def build_epub(config: BookConfig) -> None:
    """
    Assemble and write the EPUB described by *config*.

    Reads chapters from the cache file at config.cache_file.
    Writes the finished EPUB to config.output_file.
    """
    cache = load_cache(config.cache_file)

    book = epub.EpubBook()
    book.set_identifier(config.identifier or str(uuid.uuid4()))
    book.set_title(config.title)
    book.set_language(config.language)
    book.add_author(config.author)

    # ── Stylesheet ────────────────────────────────────────────────────────────
    has_fonts = _load_fonts(book, config.font_dir)
    css_content = _CSS if has_fonts else _CSS_NO_FONTS
    css_item = epub.EpubItem(
        uid="stylesheet",
        file_name="styles/book.css",
        media_type="text/css",
        content=css_content.encode("utf-8"),
    )
    book.add_item(css_item)
    css_href = "../styles/book.css"  # relative from OEBPS/

    # ── Cover ─────────────────────────────────────────────────────────────────
    cover_page = None
    if config.cover_image:
        cover_page = _add_cover(book, config.cover_image)

    # ── Summary page ──────────────────────────────────────────────────────────
    summary_page = None
    if config.summary_html:
        summary_page = _xhtml(
            uid="summary",
            title="Summary",
            body_content=f'<div class="summary-page">\n{config.summary_html}\n</div>',
            css_href=css_href,
        )
        book.add_item(summary_page)

    # ── Chapters + sections ───────────────────────────────────────────────────
    all_items: list[epub.EpubHtml] = []   # every XHTML page, in spine order
    toc_entries: list = []                # top-level TOC

    for section in config.sections:
        # Section divider page
        subtitle_tag = f"\n  <h2>{_esc(section.subtitle)}</h2>" if section.subtitle else ""
        sec_page = _xhtml(
            uid=f"sec-{_slugify(section.heading)}",
            title=section.heading,
            body_content=f'<div class="section-page">\n  <h1>{_esc(section.heading)}</h1>{subtitle_tag}\n</div>',
            css_href=css_href,
            body_class="section-page",
        )
        book.add_item(sec_page)
        all_items.append(sec_page)

        # Chapter pages for this section
        chapter_items: list[epub.EpubHtml] = []
        for uid in section.chapter_uids:
            entry = cache.get(uid)
            if not entry:
                print(f"  [warn] chapter '{uid}' not in cache — skipping")
                continue

            ch_title = entry.get("title", uid)
            ch_body  = entry.get("body", "<p><em>[No content]</em></p>")

            body_html = f'<p class="chapter-title">{_esc(ch_title)}</p>\n{ch_body}'
            ch_item = _xhtml(
                uid=f"ch-{uid}",
                title=ch_title,
                body_content=body_html,
                css_href=css_href,
            )
            book.add_item(ch_item)
            all_items.append(ch_item)
            chapter_items.append(ch_item)

        # TOC: section heading → list of chapter links
        if chapter_items:
            toc_entries.append(
                (
                    epub.Section(section.toc_label),
                    chapter_items,
                )
            )

    # ── TOC + NCX ─────────────────────────────────────────────────────────────
    book.toc = toc_entries
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())

    # ── Spine ─────────────────────────────────────────────────────────────────
    spine: list = ["nav"]
    if cover_page:
        spine.insert(0, cover_page)
    if summary_page:
        spine.insert(1 if cover_page else 0, summary_page)
    spine.extend(all_items)
    book.spine = spine

    # ── Write ─────────────────────────────────────────────────────────────────
    out = Path(config.output_file)
    out.parent.mkdir(parents=True, exist_ok=True)
    epub.write_epub(str(out), book)
    print(f"\n✓ EPUB written → {out}")


# ── Internal helpers ───────────────────────────────────────────────────────────

def _slugify(text: str) -> str:
    """Convert a heading to a safe XML id fragment."""
    import re
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "section"
