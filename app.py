"""
Novel EPUB Builder — FastAPI backend
Scrapes chapters from any site using cloudscraper, assembles an EPUB via
epub_builder.py, and streams live progress to the UI via Server-Sent Events.

Setup:
    pip install fastapi uvicorn cloudscraper beautifulsoup4 ebooklib lxml python-multipart

    Place epub_builder.py and your fonts/ folder in the same directory as app.py.

Run:
    uvicorn app:app --reload --port 8000
"""

import asyncio
import json
import re
import tempfile
import time
import uuid
from pathlib import Path
from typing import AsyncGenerator

import cloudscraper
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from epub_builder import BookConfig, Section, build_epub

# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(title="Novel EPUB Builder")
app.mount("/static", StaticFiles(directory="static"), name="static")

SCRAPER = cloudscraper.create_scraper(
    browser={"browser": "chrome", "platform": "windows", "mobile": False}
)

# In-memory job store: job_id → {"status", "chapters", "errors", "epub_path"}
JOBS: dict[str, dict] = {}

# ── Models ────────────────────────────────────────────────────────────────────

class ChapterEntry(BaseModel):
    title: str
    url: str

class SectionEntry(BaseModel):
    heading: str
    toc_label: str
    subtitle: str = ""
    chapters: list[ChapterEntry]

class BuildRequest(BaseModel):
    title: str
    author: str
    identifier: str = ""
    summary_html: str = ""
    # CSS selector to find chapter body on target site (e.g. "div.entry-content")
    content_selector: str = "div.entry-content"
    delay_seconds: float = 1.5
    sections: list[SectionEntry]

# ── Helpers ───────────────────────────────────────────────────────────────────

def fetch_html(url: str) -> str | None:
    try:
        r = SCRAPER.get(url, timeout=20)
        r.raise_for_status()
        return r.text
    except Exception as e:
        return None

def extract_body(html: str, selector: str) -> str:
    soup = BeautifulSoup(html, "html.parser")

    # Parse selector: supports "tag.class", "tag#id", "#id", ".class", "tag"
    tag, _, rest = selector.partition(".")
    if rest:
        container = soup.find(tag or True, class_=rest.split(".")[0])
    else:
        tag2, _, id_ = selector.partition("#")
        if id_:
            container = soup.find(tag2 or True, id=id_)
        else:
            container = soup.find(tag or "div")

    if not container:
        return "<p><em>[Could not extract chapter — check your CSS selector]</em></p>"

    paragraphs = []
    for p in container.find_all("p"):
        text = p.get_text(separator=" ").strip()
        text = re.sub(r"\s{2,}", " ", text)
        if text and text != "\u00a0":
            paragraphs.append(f"<p>{text}</p>")

    return "\n".join(paragraphs) if paragraphs else "<p><em>[Empty chapter]</em></p>"

# ── SSE job runner ─────────────────────────────────────────────────────────────

async def run_job(job_id: str, req: BuildRequest) -> None:
    job = JOBS[job_id]
    job["status"] = "scraping"

    # Flatten all chapters with their section index
    all_chapters: list[tuple[int, int, ChapterEntry]] = []  # (sec_idx, ch_idx, entry)
    for si, section in enumerate(req.sections):
        for ci, ch in enumerate(section.chapters):
            all_chapters.append((si, ci, ch))

    total = len(all_chapters)
    cache: dict[str, dict] = {}  # uid → {title, body}

    for i, (si, ci, ch) in enumerate(all_chapters):
        uid = f"s{si}-c{ci}"
        job["progress"] = {"current": i + 1, "total": total, "title": ch.title}

        loop = asyncio.get_event_loop()
        html = await loop.run_in_executor(None, fetch_html, ch.url)

        if html:
            body = extract_body(html, req.content_selector)
            cache[uid] = {"title": ch.title, "body": body}
        else:
            cache[uid] = {"title": ch.title, "body": "<p><em>[Failed to fetch]</em></p>"}
            job["errors"].append(ch.title)

        await asyncio.sleep(req.delay_seconds)

    # Build EPUB into a temp file
    job["status"] = "building"
    tmp_dir = Path(tempfile.mkdtemp())
    epub_path = tmp_dir / f"{req.title}.epub"
    cache_path = tmp_dir / "cache.json"
    cache_path.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")

    script_dir = Path(__file__).resolve().parent

    config = BookConfig(
        title=req.title,
        author=req.author,
        cache_file=cache_path,
        output_file=epub_path,
        font_dir=script_dir / "fonts",
        cover_image=None,            # cover upload is a future feature
        summary_html=req.summary_html,
        identifier=req.identifier or re.sub(r"\W+", "-", req.title.lower()),
        sections=[
            Section(
                heading=s.heading,
                subtitle=s.subtitle,
                toc_label=s.toc_label,
                chapter_uids=[f"s{si}-c{ci}" for ci in range(len(s.chapters))],
            )
            for si, s in enumerate(req.sections)
        ],
    )

    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, build_epub, config)

    job["status"] = "done"
    job["epub_path"] = str(epub_path)
    job["epub_name"] = f"{req.title}.epub"

# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/")
async def index():
    return FileResponse("static/index.html")

@app.post("/api/build")
async def start_build(req: BuildRequest):
    job_id = str(uuid.uuid4())
    JOBS[job_id] = {
        "status": "queued",
        "progress": {"current": 0, "total": 0, "title": ""},
        "errors": [],
        "epub_path": None,
        "epub_name": None,
    }
    asyncio.create_task(run_job(job_id, req))
    return {"job_id": job_id}

@app.get("/api/progress/{job_id}")
async def progress_stream(job_id: str):
    if job_id not in JOBS:
        raise HTTPException(status_code=404, detail="Job not found")

    async def event_stream() -> AsyncGenerator[str, None]:
        while True:
            job = JOBS.get(job_id, {})
            data = {
                "status": job.get("status"),
                "progress": job.get("progress", {}),
                "errors": job.get("errors", []),
                "epub_name": job.get("epub_name"),
            }
            yield f"data: {json.dumps(data)}\n\n"
            if job.get("status") in ("done", "error"):
                break
            await asyncio.sleep(0.5)

    return StreamingResponse(event_stream(), media_type="text/event-stream")

@app.get("/api/download/{job_id}")
async def download_epub(job_id: str):
    job = JOBS.get(job_id)
    if not job or job["status"] != "done":
        raise HTTPException(status_code=404, detail="EPUB not ready")
    return FileResponse(
        path=job["epub_path"],
        filename=job["epub_name"],
        media_type="application/epub+zip",
    )
