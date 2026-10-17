"""
Novel EPUB Builder — FastAPI backend
Two-stage workflow:
  1. POST /api/scrape  → scrapes chapters, streams SSE progress, result is a
                         downloadable cache JSON
  2. POST /api/build   → accepts a cache JSON upload + book config, assembles
                         the EPUB and makes it available for download

Setup:
    pip install fastapi uvicorn cloudscraper beautifulsoup4 ebooklib lxml python-multipart

Run:
    uvicorn app:app --reload --port 8000
"""

import asyncio
import html as html_module
import json
import logging
import re
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncGenerator

import cloudscraper
from bs4 import BeautifulSoup
from curl_cffi import requests as cffi_requests
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from epub_builder import BookConfig, Section, build_epub

log = logging.getLogger(__name__)

# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(title="Novel EPUB Builder")
app.mount("/static", StaticFiles(directory="static"), name="static")

SCRAPER = cloudscraper.create_scraper(
    browser={"browser": "chrome", "platform": "windows", "mobile": False}
)

# job_id → {status, progress, errors, cache, json_path, epub_path, epub_name}
JOBS: dict[str, dict] = {}

# ── Models ────────────────────────────────────────────────────────────────────

class ChapterEntry(BaseModel):
    title: str
    url: str

class BuildSectionEntry(BaseModel):
    heading: str = "Main Story"
    toc_label: str = "Main Story"
    subtitle: str = ""
    chapter_count: int = 0
    chapters: list[ChapterEntry] = []

class SectionEntry(BaseModel):
    heading: str = "Main Story"
    toc_label: str = "Main Story"
    subtitle: str = ""
    chapters: list[ChapterEntry] = []
    chapter_count: int = 0

class ScrapeRequest(BaseModel):
    title: str
    content_selector: str = "div.entry-content"
    delay_seconds: float = 1.5
    mode: str = "chain"   # "chain" or "manual"
    # Chain crawl
    start_url: str = ""
    next_url_selector: str = ""
    count: int = 0
    chapter_prefix: str = "Chapter"
    # Manual list
    chapters: list[ChapterEntry] = []
    # Backward compatibility with old multi-section scrape payloads
    sections: list[SectionEntry] = []

class BuildRequest(BaseModel):
    title: str
    author: str
    identifier: str = ""
    summary_html: str = ""
    sections: list[BuildSectionEntry] = []

# ── Helpers ───────────────────────────────────────────────────────────────────

# Cloudflare challenge markers in response bodies/headers.
_CF_CHALLENGE_MARKERS = (
    "challenges.cloudflare.com",
    "cf-challenge",
    "Just a moment",
    "Checking your browser",
    "Verifying you are human",
)


@dataclass
class _FetchResult:
    """Structured result from fetch_html_detailed."""
    html: str | None = None
    ok: bool = False
    status_code: int = 0
    is_cloudflare_challenge: bool = False
    error_type: str = ""    # "cloudflare", "http", "connection", ""
    error_detail: str = ""


def _is_cloudflare_challenge(response) -> bool:
    """Detect whether a response is a Cloudflare managed/JS challenge page."""
    if response.headers.get("Cf-Mitigated") == "challenge":
        return True
    if response.headers.get("Server", "").lower() == "cloudflare" and response.status_code == 403:
        body_sample = response.text[:3000] if hasattr(response, "text") else ""
        return any(marker in body_sample for marker in _CF_CHALLENGE_MARKERS)
    return False


def _try_cloudscraper(url: str, timeout: int = 20):
    """Attempt fetch with cloudscraper. Returns (response, error_str)."""
    try:
        r = SCRAPER.get(url, timeout=timeout)
        return r, None
    except Exception as e:
        return None, str(e)


def _try_curl_cffi(url: str, timeout: int = 20):
    """Attempt fetch with curl_cffi (better TLS fingerprint). Returns (response, error_str)."""
    try:
        r = cffi_requests.get(url, timeout=timeout, impersonate="chrome")
        return r, None
    except Exception as e:
        return None, str(e)


def fetch_html_detailed(
    url: str,
    *,
    max_retries: int = 3,
    backoff_base: float = 2.0,
) -> _FetchResult:
    """
    Robust HTML fetcher with retry, backoff, and curl_cffi fallback.

    Strategy per attempt:
      1. Try cloudscraper (handles old-style CF JS challenges).
      2. If that returns a Cloudflare challenge or fails, try curl_cffi
         (better TLS fingerprint, may bypass some bot-detection).
      3. If still a CF challenge, classify and report clearly.
    """
    last_result = _FetchResult()

    for attempt in range(max_retries):
        if attempt > 0:
            delay = backoff_base ** attempt
            log.debug("fetch_html retry %d for %s (backoff %.1fs)", attempt, url, delay)
            time.sleep(delay)

        # ── Try cloudscraper first ────────────────────────────────────
        resp, err = _try_cloudscraper(url)
        if resp is not None:
            if resp.ok and not _is_cloudflare_challenge(resp):
                resp.encoding = resp.apparent_encoding
                return _FetchResult(html=resp.text, ok=True, status_code=resp.status_code)

            if _is_cloudflare_challenge(resp):
                # Don't give up yet — try curl_cffi
                pass
            elif not resp.ok:
                last_result = _FetchResult(
                    status_code=resp.status_code,
                    error_type="http",
                    error_detail=f"HTTP {resp.status_code}",
                )
                continue  # retry

        # ── Try curl_cffi as fallback ─────────────────────────────────
        resp2, err2 = _try_curl_cffi(url)
        if resp2 is not None:
            if resp2.ok and not _is_cloudflare_challenge(resp2):
                return _FetchResult(html=resp2.text, ok=True, status_code=resp2.status_code)

            if _is_cloudflare_challenge(resp2):
                last_result = _FetchResult(
                    status_code=resp2.status_code,
                    is_cloudflare_challenge=True,
                    error_type="cloudflare",
                    error_detail=(
                        "Cloudflare Managed Challenge / Turnstile detected. "
                        "This site requires a real browser to solve the challenge "
                        "and cannot be fetched by automated tools."
                    ),
                )
                # Cloudflare challenges are deterministic — retrying won't help
                return last_result
            elif not resp2.ok:
                last_result = _FetchResult(
                    status_code=resp2.status_code,
                    error_type="http",
                    error_detail=f"HTTP {resp2.status_code}",
                )
                continue  # retry

        # Both clients failed to even connect
        if resp is None and resp2 is None:
            last_result = _FetchResult(
                error_type="connection",
                error_detail=err or err2 or "Connection failed",
            )

    return last_result


def fetch_html(url: str) -> str | None:
    """
    Backward-compatible wrapper: returns HTML string or None.

    For richer error info, use fetch_html_detailed() directly.
    """
    result = fetch_html_detailed(url)
    return result.html if result.ok else None


def _parse_selector(soup: BeautifulSoup, selector: str):
    """Supports: tag, tag.class, .class, tag#id, #id"""
    id_match  = re.match(r"^([a-z]*)#([\w-]+)$", selector)
    cls_match = re.match(r"^([a-z]*)\.([^\s.#]+)$", selector)
    if id_match:
        tag, id_ = id_match.groups()
        return soup.find(tag or True, id=id_)
    elif cls_match:
        tag, cls = cls_match.groups()
        return soup.find(tag or True, class_=cls)
    else:
        return soup.find(selector or "div")


# ── Next.js RSC (React Server Components) extraction ─────────────────────

def _is_nextjs_page(html: str) -> bool:
    """Detect whether the HTML is a Next.js App Router page with RSC data."""
    return "self.__next_f" in html and "/_next/static/chunks/" in html


def _extract_rsc_payloads(html: str) -> list[str]:
    """Extract all decoded RSC payload strings from __next_f.push calls."""
    raw_payloads = re.findall(r'self\.__next_f\.push\(\[1,(.+?)\]\)<', html)
    decoded = []
    for raw in raw_payloads:
        try:
            decoded.append(json.loads(raw))
        except (json.JSONDecodeError, ValueError):
            pass
    return decoded


def _extract_rsc_html_content(html: str) -> str | None:
    """
    Extract chapter body HTML from a Next.js RSC data stream.

    Next.js App Router embeds server-rendered data in inline scripts as
    `self.__next_f.push([1, "..."])` calls. The chapter content is typically
    a raw HTML blob (with <p> tags) referenced by a `"content":"$ref"` field
    in the RSC component tree.

    Strategy:
      1. Find the payload with the most <p> tags (likely chapter body).
      2. Verify it has substantial text content (not just metadata).
    """
    payloads = _extract_rsc_payloads(html)
    if not payloads:
        return None

    best_html = None
    best_p_count = 0

    for payload in payloads:
        if not isinstance(payload, str):
            continue
        # Count <p> tags as a proxy for "this is chapter content"
        p_count = payload.count("<p>") + payload.count("<p ")
        if p_count >= 3 and p_count > best_p_count:
            # Verify it has actual text, not just empty <p><br /></p>
            text_sample = re.sub(r"<[^>]+>", "", payload)
            text_sample = text_sample.strip()
            if len(text_sample) > 200:
                best_html = payload
                best_p_count = p_count

    return best_html


def _extract_rsc_navigation(html: str) -> dict:
    """
    Extract navigation info (next/prev chapter slugs, novel slug) from
    Next.js RSC payloads.

    Returns dict with keys: next_chapter_slug, prev_chapter_slug, novel_slug
    """
    payloads = _extract_rsc_payloads(html)
    result = {}

    for payload in payloads:
        if not isinstance(payload, str):
            continue
        # Look for nextChapterSlug / prevChapterSlug in the RSC tree
        next_match = re.search(r'"nextChapterSlug"\s*:\s*"([^"]+)"', payload)
        prev_match = re.search(r'"prevChapterSlug"\s*:\s*"([^"]+)"', payload)
        novel_match = re.search(r'"novelSlug"\s*:\s*"([^"]+)"', payload)

        if next_match:
            result["next_chapter_slug"] = next_match.group(1)
        if prev_match:
            result["prev_chapter_slug"] = prev_match.group(1)
        if novel_match:
            result["novel_slug"] = novel_match.group(1)

    return result


def extract_body(html: str, selector: str) -> str:
    """
    Extract chapter body from a fetched page.

    Handles three common formats:
      - <p> tags (most WordPress / English TL sites)
      - <br><br> paragraph breaks (most Chinese novel sites)
      - Next.js RSC data streams (modern JS-rendered sites)

    Also strips injected <script> and <ins> ad tags from the container.
    """
    soup = BeautifulSoup(html, "html.parser")

    # ── Strategy 0: Next.js RSC extraction ────────────────────────────
    # Try this first if the page is a Next.js app, since the DOM body
    # will be empty (content lives in inline script payloads).
    if _is_nextjs_page(html):
        rsc_html = _extract_rsc_html_content(html)
        if rsc_html:
            # The RSC content is raw HTML — parse and clean it
            rsc_soup = BeautifulSoup(rsc_html, "html.parser")
            for junk in rsc_soup.find_all(["script", "ins", "iframe", "style"]):
                junk.decompose()
            p_tags = rsc_soup.find_all("p")
            if p_tags:
                paragraphs = []
                for p in p_tags:
                    text = p.get_text(separator=" ").strip()
                    text = re.sub(r"\s{2,}", " ", text)
                    if text and text != "\u00a0":
                        paragraphs.append(f"<p>{text}</p>")
                if paragraphs:
                    return "\n".join(paragraphs)

    container = _parse_selector(soup, selector)

    if not container:
        return "<p><em>[Could not extract chapter — check your CSS selector]</em></p>"

    for junk in container.find_all(["script", "ins", "iframe", "style"]):
        junk.decompose()

    # Strategy 1: <p> tags
    p_tags = container.find_all("p")
    if p_tags:
        paragraphs = []
        for p in p_tags:
            text = p.get_text(separator=" ").strip()
            text = re.sub(r"\s{2,}", " ", text)
            if text and text != "\u00a0":
                paragraphs.append(f"<p>{text}</p>")
        if paragraphs:
            return "\n".join(paragraphs)

    # Strategy 2: <br><br> paragraph breaks (Chinese novel sites)
    raw = container.decode_contents()
    raw = re.sub(r"<br\s*/?>", "\n", raw, flags=re.IGNORECASE)
    raw = re.sub(r"<[^>]+>", "", raw)
    raw = html_module.unescape(raw)

    paragraphs = []
    for block in re.split(r"\n{2,}", raw):
        text = " ".join(block.split()).strip()
        if text and text != "\u00a0":
            paragraphs.append(f"<p>{text}</p>")

    return "\n".join(paragraphs) if paragraphs else "<p><em>[Empty chapter]</em></p>"


# ── Page analysis / auto-detect ───────────────────────────────────────────

# Noise class names — elements with these in their class/id are unlikely to
# be the main content container.
_NOISE_NAMES = re.compile(
    r"nav|footer|sidebar|side-bar|header|menu|widget|comment|"
    r"recommend|related|social|share|advert|ad-|ads-|toolbar|"
    r"breadcrumb|pagination|copyright|disclaimer|popup|modal|"
    r"cookie|banner|signup|newsletter|author-bio|tag-list",
    re.IGNORECASE,
)

# Text patterns that indicate a "next chapter" anchor.
_NEXT_TEXT_PATTERNS = re.compile(
    r"下一[章页]|下页|next\s*chapter|next\s*page|next\b|下一[頁節]|"
    r"翻页.*下|后一章|後一章",
    re.IGNORECASE,
)

# Attribute (class/id) patterns that indicate a "next" control.
_NEXT_ATTR_PATTERNS = re.compile(
    r"next|下一", re.IGNORECASE,
)


def _best_selector_for(el) -> str:
    """Build the most specific short CSS selector for an element."""
    if el.get("id"):
        return f"{el.name}#{el['id']}"
    classes = [c for c in (el.get("class") or []) if not _NOISE_NAMES.search(c)]
    if classes:
        return f"{el.name}.{classes[0]}"
    return el.name


def _is_noise(el) -> bool:
    """Check if an element smells like navigation/noise."""
    name_str = " ".join([
        el.get("id") or "",
        " ".join(el.get("class") or []),
    ])
    return bool(_NOISE_NAMES.search(name_str))


def _extract_numeric_id(url: str) -> int | None:
    """Try to pull a trailing numeric segment from a URL path."""
    m = re.search(r"/(\d+)(?:\.html?)?/?(?:\?.*)?$", url)
    return int(m.group(1)) if m else None


def _url_looks_forward(current_url: str, candidate_url: str) -> bool:
    """Heuristic: the candidate URL is a 'forward step' from the current one."""
    from urllib.parse import urlparse
    cur = urlparse(current_url)
    cand = urlparse(candidate_url)
    # Must be same domain
    if cand.netloc and cand.netloc != cur.netloc:
        return False
    cur_num = _extract_numeric_id(current_url)
    cand_num = _extract_numeric_id(candidate_url)
    if cur_num is not None and cand_num is not None:
        return cand_num > cur_num
    # If no numeric IDs, just check same domain + different path
    return cand.path != cur.path


def _is_index_like(url: str) -> bool:
    """
    Heuristic: URL looks like a book index/catalogue page rather than a chapter.

    Strategy:
      1. Inspect the last path segment (minus any file extension).
      2. If it is purely numeric → definitely a chapter/page ID, not an index.
      3. If it contains 'chap' → clearly a chapter page.
      4. Otherwise check whether a known index keyword is the *terminal* path
         segment (anchored to the end of the path), not just a parent directory.

    This prevents false positives for URLs like:
      /books/332696/60995617.html   (last segment numeric → not index)
      /novel/story-name/chapter-2   (last segment contains 'chap' → not index)
    while still catching genuine index URLs like:
      /novel/                        (ends with /novel/)
      /catalog                       (ends with catalog)
    """
    from urllib.parse import urlparse
    path = urlparse(url).path.rstrip("/")
    segments = [s for s in path.split("/") if s]

    if not segments:
        return False

    last_seg = segments[-1]
    # Strip file extension (e.g. .html, .htm, .php)
    last_seg_bare = re.sub(r"\.\w{2,5}$", "", last_seg)

    # A purely numeric final segment is almost certainly a chapter/page ID, not an index
    if re.fullmatch(r"\d+", last_seg_bare):
        return False

    # A segment explicitly containing 'chap' is clearly a chapter page
    if re.search(r"\bchap", last_seg_bare, re.IGNORECASE):
        return False

    # Index keyword must be the *terminal* path segment (anchored to end of path)
    # Optional extension (.html etc.) before trailing slash/query-string
    return bool(re.search(
        r"/(catalogue|catalog|index|目录|书页|bookinfo|detail)(?:\.\w{2,5})?/?(?:\?.*)?$",
        url, re.IGNORECASE,
    )) or bool(re.search(
        r"/(book|books|novel|novels|info)(?:\.\w{2,5})?/?(?:\?.*)?$",
        url, re.IGNORECASE,
    ))


def analyze_page(url: str) -> dict:
    """
    Fetch a chapter page and run heuristic detection for:
      - content_selector: CSS selector for the main chapter text container
      - paragraph_style:  "p" or "br" (how paragraphs are separated)
      - next_url_selector: CSS selector for the next-chapter link
      - confidence: "high" / "medium" / "low"
    """
    from urllib.parse import urljoin

    result = fetch_html_detailed(url)
    if not result.ok:
        error_msg = {
            "cloudflare": (
                "Cloudflare Managed Challenge / Turnstile detected. "
                "This site requires solving an interactive browser challenge "
                "that automated tools cannot bypass."
            ),
            "http": f"The server returned HTTP {result.status_code}.",
            "connection": f"Could not connect to the site: {result.error_detail}",
        }.get(
            result.error_type,
            "Failed to fetch the page. The site may be blocking requests or require JavaScript.",
        )
        return {
            "content_selector": "",
            "next_url_selector": "",
            "paragraph_style": "unknown",
            "confidence": "low",
            "error": error_msg,
            "error_type": result.error_type,
        }

    html = result.html

    # ── Fast-path: Next.js RSC page ──────────────────────────────────────
    # Next.js App Router pages have an empty DOM body — all content lives
    # in inline `self.__next_f.push` scripts.  The normal container-scoring
    # heuristics will fail, so we use the RSC extraction directly.
    if _is_nextjs_page(html):
        rsc_html = _extract_rsc_html_content(html)
        nav = _extract_rsc_navigation(html)

        rsc_result = {
            "content_selector": "__nextjs_rsc",
            "paragraph_style": "p",
            "confidence": "high" if rsc_html else "low",
            "site_type": "nextjs",
        }

        if not rsc_html:
            rsc_result["error"] = (
                "Next.js page detected but could not extract chapter content "
                "from the RSC data stream."
            )
            rsc_result["confidence"] = "low"

        # Build next URL from RSC navigation data
        if nav.get("next_chapter_slug") and nav.get("novel_slug"):
            from urllib.parse import urlparse
            parsed = urlparse(url)
            # Build the next chapter URL using the same base path pattern
            next_url = f"{parsed.scheme}://{parsed.netloc}/novels/{nav['novel_slug']}/{nav['next_chapter_slug']}"
            rsc_result["next_url_selector"] = "__nextjs_rsc"
            rsc_result["next_url"] = next_url
        else:
            rsc_result["next_url_selector"] = ""

        return rsc_result

    soup = BeautifulSoup(html, "html.parser")

    # Remove script/style/nav/footer before analysis
    for tag in soup.find_all(["script", "style", "noscript"]):
        tag.decompose()

    # ── 1. Detect content container ──────────────────────────────────────
    candidates: list[tuple[float, any]] = []
    for el in soup.find_all(["div", "article", "section", "main"]):
        if _is_noise(el):
            continue

        text = el.get_text(separator=" ", strip=True)
        text_len = len(text)
        if text_len < 200:
            continue

        score = text_len
        p_count = len(el.find_all("p", recursive=False))
        score += p_count * 50
        br_count = len(el.find_all("br", recursive=False))
        score += br_count * 10
        child_divs = len(el.find_all("div", recursive=False))
        score -= child_divs * 100
        noise_children = sum(1 for ch in el.find_all(True, recursive=False) if _is_noise(ch))
        score -= noise_children * 200

        name_str = " ".join([(el.get("id") or ""), " ".join(el.get("class") or [])])
        if re.search(r"content|chapter|article|text|body|book|read", name_str, re.IGNORECASE):
            score += 500

        candidates.append((score, el))

    candidates.sort(key=lambda x: x[0], reverse=True)

    content_selector = ""
    paragraph_style = "unknown"
    content_confidence = 0

    if candidates:
        best_score, best_el = candidates[0]
        content_selector = _best_selector_for(best_el)
        content_confidence = best_score

        p_tags = best_el.find_all("p", recursive=False)
        if not p_tags:
            p_tags = best_el.find_all("p")
        br_tags = best_el.find_all("br")

        if len(p_tags) >= 3:
            paragraph_style = "p"
        elif len(br_tags) >= 3:
            paragraph_style = "br"
        else:
            raw = best_el.decode_contents()
            if re.search(r"<br\s*/?>\s*<br\s*/?>", raw, re.IGNORECASE):
                paragraph_style = "br"
            elif p_tags:
                paragraph_style = "p"

    # ── 2. Detect next-chapter link ──────────────────────────────────────
    next_url_selector = ""
    next_confidence = 0

    all_anchors = soup.find_all("a", href=True)
    scored_nexts: list[tuple[int, str, any]] = []

    for a in all_anchors:
        anchor_score = 0
        a_text = a.get_text(strip=True)
        a_id = a.get("id") or ""
        a_classes = " ".join(a.get("class") or [])

        if _NEXT_TEXT_PATTERNS.search(a_text):
            anchor_score += 100
        if _NEXT_ATTR_PATTERNS.search(a_id):
            anchor_score += 80
        if _NEXT_ATTR_PATTERNS.search(a_classes):
            anchor_score += 60

        parent = a.parent
        if parent:
            parent_id = parent.get("id") or ""
            parent_classes = " ".join(parent.get("class") or [])
            if _NEXT_ATTR_PATTERNS.search(parent_id):
                anchor_score += 50
            if _NEXT_ATTR_PATTERNS.search(parent_classes):
                anchor_score += 40

        if anchor_score == 0:
            continue

        href = a.get("href", "")
        abs_url = urljoin(url, href) if href else ""

        if abs_url and _url_looks_forward(url, abs_url):
            anchor_score += 30
        if abs_url and _is_index_like(abs_url):
            anchor_score -= 100

        # Build the selector for this anchor
        if a_id:
            sel = f"a#{a_id}"
        elif a.get("class"):
            sel = f"a.{a.get('class')[0]}"
        elif parent and parent.name != "body":
            p_id = parent.get("id") or ""
            p_classes = parent.get("class") or []
            if p_id:
                sel = f"{parent.name}#{p_id} a"
            elif p_classes:
                sel = f"{parent.name}.{p_classes[0]} a"
            else:
                sel = f"{parent.name} a"
        else:
            sel = "a"

        scored_nexts.append((anchor_score, sel, a))

    scored_nexts.sort(key=lambda x: x[0], reverse=True)

    if scored_nexts:
        best_next_score, best_next_sel, _ = scored_nexts[0]
        if best_next_score > 0:
            next_url_selector = best_next_sel
            next_confidence = best_next_score

    # ── 3. URL pattern fallback ──────────────────────────────────────────
    url_pattern = ""
    if not next_url_selector:
        num = _extract_numeric_id(url)
        if num is not None:
            next_guess = re.sub(
                r"/(\d+)(\.html?)?(/)?(\?.*)?$",
                lambda m: f"/{num + 1}{m.group(2) or ''}{m.group(3) or ''}{m.group(4) or ''}",
                url,
            )
            if next_guess != url:
                url_pattern = f"numeric_increment (current: {num})"

    # ── 4. Compute overall confidence ────────────────────────────────────
    signals = 0
    if content_confidence > 500:
        signals += 2
    elif content_confidence > 200:
        signals += 1
    if next_confidence > 100:
        signals += 2
    elif next_confidence > 50:
        signals += 1
    if paragraph_style != "unknown":
        signals += 1

    if signals >= 4:
        confidence = "high"
    elif signals >= 2:
        confidence = "medium"
    else:
        confidence = "low"

    result = {
        "content_selector": content_selector,
        "next_url_selector": next_url_selector,
        "paragraph_style": paragraph_style,
        "confidence": confidence,
    }
    if url_pattern:
        result["url_pattern"] = url_pattern
    return result


# ── Stage 1: Scrape job ────────────────────────────────────────────────────────

# Text patterns that positively identify a "next" anchor.
_NEXT_LINK_TEXT = re.compile(
    r"下一[章页節]|下页|后一章|後一章|next\s*ch|next\s*page|next\b",
    re.IGNORECASE,
)
# Text patterns that identify a "previous" anchor (should be avoided).
_PREV_LINK_TEXT = re.compile(
    r"上一[章页節]|上页|前一章|前一页|prev\s*ch|prev\s*page|prev\b|previous\b",
    re.IGNORECASE,
)


def _best_anchor_from_container(container, base_url: str) -> str | None:
    """
    Given a container element (e.g. a nav section), find the most-likely
    "next chapter" anchor.

    Priority:
      1. Anchor whose text explicitly matches _NEXT_LINK_TEXT.
      2. Anchor whose class/id contains "next".
      3. Anchor that is NOT labelled as "previous" and whose URL looks forward
         relative to base_url (numerically larger trailing ID).
      4. Last resort: the last anchor in the container that isn't "previous".
    """
    from urllib.parse import urljoin

    anchors = container.find_all("a", href=True)
    if not anchors:
        return None

    def abs_href(a):
        h = a.get("href", "")
        if not h or h.startswith("#"):
            return None
        return h if h.startswith("http") else urljoin(base_url, h)

    # Pass 1 — explicit next-text match
    for a in anchors:
        text = a.get_text(strip=True)
        if _NEXT_LINK_TEXT.search(text):
            return abs_href(a)

    # Pass 2 — class/id of anchor or its parent contains "next"
    for a in anchors:
        attrs = " ".join([
            a.get("id") or "",
            " ".join(a.get("class") or []),
            (a.parent.get("id") or "") if a.parent else "",
            " ".join((a.parent.get("class") or [])) if a.parent else "",
        ])
        if re.search(r"next", attrs, re.IGNORECASE):
            return abs_href(a)

    # Pass 3 — forward-looking URL that isn't labelled "previous"
    candidates = []
    for a in anchors:
        text = a.get_text(strip=True)
        if _PREV_LINK_TEXT.search(text):
            continue
        url = abs_href(a)
        if url and _url_looks_forward(base_url, url):
            candidates.append(url)
    if candidates:
        return candidates[-1]  # furthest-forward URL wins

    # Pass 4 — last anchor that doesn't look like "previous"
    for a in reversed(anchors):
        text = a.get_text(strip=True)
        if not _PREV_LINK_TEXT.search(text):
            url = abs_href(a)
            if url:
                return url

    return None


def extract_next_url(html: str, selector: str, base_url: str) -> str | None:
    """
    Find the next-chapter URL from a page using a CSS selector.

    When the selector resolves to an <a> tag directly, returns its href.
    When it resolves to a container element, delegates to
    _best_anchor_from_container() which picks the most-likely "next" link
    rather than blindly taking the first <a> (which could be "Previous").

    Special case: selector "__nextjs_rsc" extracts navigation from the
    Next.js RSC data stream embedded in inline scripts.

    Returns an absolute URL, or None if not found.
    """
    from urllib.parse import urljoin, urlparse

    # ── Next.js RSC navigation ────────────────────────────────────────
    if selector == "__nextjs_rsc" and _is_nextjs_page(html):
        nav = _extract_rsc_navigation(html)
        if nav.get("next_chapter_slug") and nav.get("novel_slug"):
            parsed = urlparse(base_url)
            return f"{parsed.scheme}://{parsed.netloc}/novels/{nav['novel_slug']}/{nav['next_chapter_slug']}"
        return None

    soup = BeautifulSoup(html, "html.parser")

    # Parse simple selectors: "tag.class", "tag#id", "#id", "tag"
    id_match   = re.match(r"^([a-z]*)#([\w-]+)$", selector)
    cls_match  = re.match(r"^([a-z]*)\.([^\s.#]+)\s+([a-z]+)$", selector)  # e.g. "li.next a"
    cls_match2 = re.match(r"^([a-z]*)\.([^\s.#]+)$", selector)             # e.g. "li.next"

    el = None
    if cls_match:
        ptag, pcls, ctag = cls_match.groups()
        parent = soup.find(ptag or True, class_=pcls)
        if parent:
            if ctag == "a":
                # Don't blindly take first <a>; use the smart picker
                return _best_anchor_from_container(parent, base_url)
            el = parent.find(ctag)
    elif id_match:
        tag, id_ = id_match.groups()
        el = soup.find(tag or True, id=id_)
    elif cls_match2:
        tag, cls = cls_match2.groups()
        el = soup.find(tag or True, class_=cls)
    else:
        el = soup.select_one(selector)  # fallback: try bs4's full CSS selector

    if not el:
        return None

    # If the resolved element is a container, use the smart anchor picker
    if el.name != "a":
        return _best_anchor_from_container(el, base_url)

    href = el.get("href", "")
    if not href or href.startswith("#"):
        return None
    return href if href.startswith("http") else urljoin(base_url, href)


async def run_scrape(job_id: str, req: ScrapeRequest) -> None:
    from urllib.parse import urlparse

    job = JOBS[job_id]
    job["status"] = "scraping"
    loop = asyncio.get_event_loop()
    cache: dict[str, dict] = {}

    is_chain = (req.mode == "chain") or (bool(req.start_url) and req.count > 0)

    if is_chain and req.start_url:
        total = max(1, req.count)
        prefix = (req.chapter_prefix or "Chapter").strip()
        current_url = req.start_url
        visited: set[str] = set()
        start_domain = urlparse(req.start_url).netloc

        job["progress"] = {"current": 0, "total": total, "title": "Starting chain scrape…"}

        for i in range(total):
            title = f"{prefix} {i + 1}"
            job["progress"] = {
                "current": i + 1,
                "total": total,
                "title": title,
            }

            html = await loop.run_in_executor(None, fetch_html, current_url)
            if not html:
                job["errors"].append(f"[Failed to fetch] {title} ({current_url})")
                cache[f"c{i}"] = {"title": title, "body": "<p><em>[Failed to fetch]</em></p>"}
                break

            visited.add(current_url)
            body = extract_body(html, req.content_selector)
            cache[f"c{i}"] = {"title": title, "body": body}

            # Follow next link if not at the end
            if i < total - 1:
                if not req.next_url_selector:
                    job["errors"].append(f"[No next-chapter selector specified after] {title}")
                    break

                next_url = extract_next_url(html, req.next_url_selector, current_url)
                if not next_url:
                    job["errors"].append(f"[No next link found after] {title}")
                    break
                if _is_index_like(next_url):
                    job["errors"].append(f"[Chain stopped — next link points to index page after] {title}")
                    break
                if next_url in visited:
                    job["errors"].append(f"[Chain stopped — duplicate URL detected after] {title}")
                    break
                next_domain = urlparse(next_url).netloc
                if next_domain and next_domain != start_domain:
                    job["errors"].append(f"[Chain stopped — next link goes to different domain ({next_domain}) after] {title}")
                    break
                if not _url_looks_forward(current_url, next_url):
                    job["errors"].append(f"[Warning: next URL may not be a forward step after] {title}")

                current_url = next_url
                await asyncio.sleep(req.delay_seconds)

    else:
        # Manual list of chapters
        chapters = list(req.chapters)
        if not chapters and req.sections:
            for s in req.sections:
                chapters.extend(s.chapters)

        total = len(chapters)
        job["progress"] = {"current": 0, "total": total, "title": ""}

        for i, ch in enumerate(chapters):
            title = ch.title or f"Chapter {i + 1}"
            job["progress"] = {"current": i + 1, "total": total, "title": title}

            html = await loop.run_in_executor(None, fetch_html, ch.url)
            if html:
                body = extract_body(html, req.content_selector)
                cache[f"c{i}"] = {"title": title, "body": body}
            else:
                cache[f"c{i}"] = {"title": title, "body": "<p><em>[Failed to fetch]</em></p>"}
                job["errors"].append(title)

            await asyncio.sleep(req.delay_seconds)

    # Save JSON to a temp file for download
    tmp_dir = Path(tempfile.mkdtemp())
    safe_title = re.sub(r"[^\w\s-]", "", req.title).strip() or "novel"
    json_path = tmp_dir / f"{safe_title}.json"
    json_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")

    job["status"] = "done"
    job["json_path"] = str(json_path)
    job["json_name"] = json_path.name


# ── Stage 2: Build EPUB ────────────────────────────────────────────────────────

async def run_build(job_id: str, req: BuildRequest, cache: dict, cover_image_path: Path | None = None) -> None:
    job = JOBS[job_id]
    job["status"] = "building"

    tmp_dir = Path(tempfile.mkdtemp())
    safe_title = re.sub(r"[^\w\s-]", "", req.title).strip() or "novel"
    epub_path  = tmp_dir / f"{safe_title}.epub"
    cache_path = tmp_dir / "cache.json"
    cache_path.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")

    script_dir = Path(__file__).resolve().parent

    all_uids = list(cache.keys())
    sections: list[Section] = []
    offset = 0

    req_sections = req.sections or [BuildSectionEntry(heading="Main Story", toc_label="Main Story", chapter_count=len(all_uids))]

    for i, s in enumerate(req_sections):
        count = s.chapter_count or len(s.chapters)
        if count <= 0:
            if i == len(req_sections) - 1:
                count = max(0, len(all_uids) - offset)
            else:
                count = 0

        section_uids = all_uids[offset : offset + count]
        offset += count

        sections.append(
            Section(
                heading=s.heading or "Main Story",
                subtitle=s.subtitle or "",
                toc_label=s.toc_label or s.heading or "Main Story",
                chapter_uids=section_uids,
            )
        )

    config = BookConfig(
        title=req.title,
        author=req.author,
        cache_file=cache_path,
        output_file=epub_path,
        font_dir=script_dir / "fonts",
        cover_image=cover_image_path,
        summary_html=req.summary_html,
        identifier=req.identifier or re.sub(r"\W+", "-", req.title.lower()),
        sections=sections,
    )

    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, build_epub, config)

    job["status"] = "done"
    job["epub_path"] = str(epub_path)
    job["epub_name"] = f"{safe_title}.epub"

# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/")
async def index():
    return FileResponse("static/index.html")


@app.get("/favicon.ico")
async def favicon():
    return FileResponse("static/favicon.ico")


class AnalyzeRequest(BaseModel):
    url: str


# Auto-detect — analyze a chapter page and return selectors
@app.post("/api/analyze")
async def analyze_endpoint(req: AnalyzeRequest):
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, analyze_page, req.url)
    return result


# Stage 1 — start scrape
@app.post("/api/scrape")
async def start_scrape(req: ScrapeRequest):
    job_id = str(uuid.uuid4())
    JOBS[job_id] = {
        "status": "queued",
        "progress": {"current": 0, "total": 0, "title": ""},
        "errors": [],
        "json_path": None,
        "json_name": None,
    }
    asyncio.create_task(run_scrape(job_id, req))
    return {"job_id": job_id}


# Stage 1 — SSE progress stream (used by both scrape and build jobs)
@app.get("/api/progress/{job_id}")
async def progress_stream(job_id: str):
    if job_id not in JOBS:
        raise HTTPException(status_code=404, detail="Job not found")

    async def event_stream() -> AsyncGenerator[str, None]:
        while True:
            job = JOBS.get(job_id, {})
            data = {
                "status":    job.get("status"),
                "progress":  job.get("progress", {}),
                "errors":    job.get("errors", []),
                "json_name": job.get("json_name"),
                "epub_name": job.get("epub_name"),
            }
            yield f"data: {json.dumps(data)}\n\n"
            if job.get("status") in ("done", "error"):
                break
            await asyncio.sleep(0.5)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


# Stage 1 — download the cache JSON
@app.get("/api/download-json/{job_id}")
async def download_json(job_id: str):
    job = JOBS.get(job_id)
    if not job or not job.get("json_path"):
        raise HTTPException(status_code=404, detail="JSON not ready")
    return FileResponse(
        path=job["json_path"],
        filename=job["json_name"],
        media_type="application/json",
    )


# Stage 2 — build EPUB from uploaded JSON + config
@app.post("/api/build")
async def start_build(
    config: str = Form(...),          # BuildRequest JSON as a form string
    cache_file: UploadFile = File(...),
    cover_image: UploadFile | None = File(default=None),
):
    try:
        req = BuildRequest.model_validate_json(config)
    except Exception as e:
        raise HTTPException(status_code=422, detail=str(e))

    raw = await cache_file.read()
    try:
        cache = json.loads(raw.decode("utf-8"))
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON cache file")

    # Handle optional cover image
    cover_path: Path | None = None
    if cover_image and cover_image.filename:
        cover_tmp = Path(tempfile.mkdtemp())
        suffix = Path(cover_image.filename).suffix.lower() or ".jpg"
        cover_path = cover_tmp / f"cover{suffix}"
        cover_path.write_bytes(await cover_image.read())

    job_id = str(uuid.uuid4())
    JOBS[job_id] = {
        "status": "queued",
        "progress": {},
        "errors": [],
        "epub_path": None,
        "epub_name": None,
    }
    asyncio.create_task(run_build(job_id, req, cache, cover_path))
    return {"job_id": job_id}


# Stage 2 — download the finished EPUB
@app.get("/api/download-epub/{job_id}")
async def download_epub(job_id: str):
    job = JOBS.get(job_id)
    if not job or job["status"] != "done" or not job.get("epub_path"):
        raise HTTPException(status_code=404, detail="EPUB not ready")
    return FileResponse(
        path=job["epub_path"],
        filename=job["epub_name"],
        media_type="application/epub+zip",
    )
