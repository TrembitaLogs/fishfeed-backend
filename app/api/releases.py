"""Mobile releases download page for testers."""

import mimetypes
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, HTMLResponse

from app.config import get_settings

router = APIRouter(prefix="/mobile/releases", tags=["releases"])

HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>FishFeed — Mobile Releases</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: #f5f7fa;
    color: #1a1a2e;
    padding: 16px;
    max-width: 600px;
    margin: 0 auto;
  }
  h1 { font-size: 1.5rem; margin-bottom: 4px; }
  .subtitle { color: #666; font-size: 0.9rem; margin-bottom: 24px; }
  .loading { text-align: center; padding: 40px; color: #888; }
  .error { text-align: center; padding: 40px; color: #c0392b; }
  .card {
    background: #fff;
    border-radius: 12px;
    padding: 16px;
    margin-bottom: 12px;
    box-shadow: 0 1px 3px rgba(0,0,0,0.08);
  }
  .card-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 8px;
  }
  .version { font-size: 1.1rem; font-weight: 600; }
  .badge {
    background: #27ae60;
    color: #fff;
    font-size: 0.7rem;
    padding: 2px 8px;
    border-radius: 10px;
    font-weight: 600;
    text-transform: uppercase;
  }
  .meta { font-size: 0.8rem; color: #888; margin-bottom: 8px; }
  .notes {
    font-size: 0.85rem;
    color: #444;
    margin-bottom: 12px;
    white-space: pre-line;
    line-height: 1.4;
  }
  .download-btn {
    display: block;
    width: 100%;
    padding: 12px;
    background: #2980b9;
    color: #fff;
    border: none;
    border-radius: 8px;
    font-size: 1rem;
    font-weight: 600;
    text-align: center;
    text-decoration: none;
    cursor: pointer;
  }
  .download-btn:active { background: #1c6ea4; }
  .empty { text-align: center; padding: 40px; color: #888; }
</style>
</head>
<body>
<h1>FishFeed Releases</h1>
<p class="subtitle">Download the latest APK for testing</p>
<div id="releases"><div class="loading">Loading releases…</div></div>
<script>
(async () => {
  const container = document.getElementById('releases');
  try {
    const res = await fetch('index.json');
    if (!res.ok) throw new Error('Failed to load releases');
    const data = await res.json();
    const releases = data.releases || data;
    if (!releases.length) {
      container.innerHTML = '<div class="empty">No releases available yet.</div>';
      return;
    }
    container.innerHTML = releases.map((r, i) => {
      const size = r.size_mb ? r.size_mb.toFixed(1) + ' MB' : '';
      const date = r.date || '';
      const meta = [date, size].filter(Boolean).join(' · ');
      return `<div class="card">
        <div class="card-header">
          <span class="version">v${r.version}</span>
          ${i === 0 ? '<span class="badge">latest</span>' : ''}
        </div>
        ${meta ? `<div class="meta">${meta}</div>` : ''}
        ${r.notes ? `<div class="notes">${r.notes}</div>` : ''}
        <a class="download-btn" href="${r.apk || r.file}">Download APK</a>
      </div>`;
    }).join('');
  } catch (e) {
    container.innerHTML = '<div class="error">Failed to load releases.</div>';
  }
})();
</script>
</body>
</html>"""


@router.get("/", response_class=HTMLResponse)
async def releases_page() -> HTMLResponse:
    """Serve the mobile releases HTML page."""
    return HTMLResponse(content=HTML_PAGE)


@router.get("/{file_path:path}")
async def releases_file(file_path: str) -> FileResponse:
    """Serve static files (APK, index.json, etc.) from the releases directory."""
    settings = get_settings()
    base = Path(settings.RELEASES_DIR).resolve()
    target = (base / file_path).resolve()

    # Prevent path traversal
    if not target.is_relative_to(base):
        raise HTTPException(status_code=403, detail="Forbidden")

    if not target.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    media_type, _ = mimetypes.guess_type(str(target))
    return FileResponse(path=target, media_type=media_type or "application/octet-stream")
