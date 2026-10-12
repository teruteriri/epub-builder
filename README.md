# Novel EPUB Builder

A local web app for scraping web novel chapters and assembling them into an EPUB with your preferred formatting.

## Setup

1. Copy these files into the same folder as your existing `epub_builder.py` and `fonts/` directory:
   - `app.py`
   - `static/index.html`

2. Install dependencies:
   ```bash
   pip install fastapi uvicorn cloudscraper beautifulsoup4 ebooklib lxml python-multipart
   ```

3. Run the server:
   ```bash
   uvicorn app:app --reload --port 8000
   ```

4. Open [http://localhost:8000](http://localhost:8000)

## Usage

1. **Book details** — Fill in title, author, and optionally a summary (HTML is fine).
2. **Content selector** — The CSS selector for the chapter body on the target site.
   - `div.entry-content` works for nomad-translations.com and most WordPress themes
   - Inspect the page source if chapters come out empty
3. **Delay** — Seconds to wait between requests. 1.5s is polite; lower if you trust the site.
4. **Sections & chapters** — Add sections (Main Story, Extras, Bonus, etc.), then add chapters either one by one or via bulk paste.
5. **Bulk paste format:**
   ```
   Chapter 1 | https://example.com/ch1
   Chapter 2 | https://example.com/ch2
   https://example.com/ch3          ← title auto-assigned
   ```
6. Click **Build EPUB** and watch the progress log. Download when done.

## File layout

```
your-folder/
├── app.py              ← FastAPI backend (this file)
├── epub_builder.py     ← your existing builder
├── fonts/              ← your existing fonts folder
└── static/
    └── index.html      ← the UI
```

## Deploying

For Railway / Render / Fly.io, add a `requirements.txt`:
```
fastapi
uvicorn
cloudscraper
beautifulsoup4
ebooklib
lxml
python-multipart
```
And set the start command to `uvicorn app:app --host 0.0.0.0 --port 8000`.
