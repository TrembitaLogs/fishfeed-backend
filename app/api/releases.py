"""Mobile releases download page for testers."""

import html as html_mod
import json
import mimetypes
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from app.config import get_settings

router = APIRouter(prefix="/mobile/releases", tags=["releases"])

COMMON_STYLES = """
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
    text-decoration: none;
    color: inherit;
    display: block;
    transition: box-shadow 0.15s;
  }
  .card:hover { box-shadow: 0 2px 8px rgba(0,0,0,0.14); }
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
"""

LIST_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>FishFeed — Mobile Releases</title>
<style>
  %(styles)s
  .notes-preview {
    font-size: 0.85rem;
    color: #444;
    margin-bottom: 4px;
    white-space: pre-line;
    line-height: 1.4;
    display: -webkit-box;
    -webkit-line-clamp: 3;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }
  .card-arrow { color: #bbb; font-size: 1.2rem; }
</style>
</head>
<body>
<h1>FishFeed Releases</h1>
<p class="subtitle">Download the latest APK for testing</p>
<div id="releases"><div class="loading">Loading releases…</div></div>
<script>
function nl(text) {
  const el = document.createElement('span');
  el.textContent = text;
  return el.innerHTML;
}
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
      const ver = (r.version.startsWith('v') ? '' : 'v') + r.version;
      const size = r.size_mb ? r.size_mb.toFixed(1) + ' MB' : '';
      const date = r.date || '';
      const meta = [date, size].filter(Boolean).join(' · ');
      return `<a class="card" href="${ver}/">
        <div class="card-header">
          <span class="version">${ver}</span>
          <span>
            ${i === 0 ? '<span class="badge">latest</span> ' : ''}
            <span class="card-arrow">›</span>
          </span>
        </div>
        ${meta ? `<div class="meta">${meta}</div>` : ''}
      </a>`;
    }).join('');
  } catch (e) {
    container.innerHTML = '<div class="error">Failed to load releases.</div>';
  }
})();
</script>
</body>
</html>"""

DETAIL_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>FishFeed — %(version)s</title>
<style>
  %(styles)s
  .back { display: inline-block; margin-bottom: 16px; color: #2980b9; text-decoration: none; font-size: 0.9rem; }
  .back:hover { text-decoration: underline; }
  .detail-section { margin-bottom: 16px; }
  .detail-label { font-size: 0.75rem; color: #888; text-transform: uppercase;
    letter-spacing: 0.5px; margin-bottom: 4px; }
  .detail-value { font-size: 0.95rem; color: #333; }
</style>
</head>
<body>
<a class="back" href="../">← All releases</a>
<div class="card" style="cursor:default;">
  <div class="card-header">
    <span class="version">%(version)s</span>
    %(badge)s
  </div>
  %(meta_sections)s
  %(notes_html)s
  <a class="download-btn" href="%(apk_url)s">Download APK</a>
</div>
</body>
</html>"""


@router.get("/", response_class=HTMLResponse)
async def releases_page() -> HTMLResponse:
    """Serve the mobile releases list page."""
    return HTMLResponse(content=LIST_PAGE % {"styles": COMMON_STYLES})


@router.get("/index.json")
async def releases_index() -> JSONResponse:
    """Serve index.json enriched with file size and upload date."""
    settings = get_settings()
    base = Path(settings.RELEASES_DIR).resolve()
    index_file = base / "index.json"

    if not index_file.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    data = json.loads(index_file.read_text())
    releases = data.get("releases", data) if isinstance(data, dict) else data

    for release in releases:
        apk_path = base / (release.get("apk") or release.get("file", ""))
        if apk_path.is_file():
            stat = apk_path.stat()
            release["size_mb"] = round(stat.st_size / (1024 * 1024), 1)
            release["date"] = datetime.fromtimestamp(stat.st_mtime, tz=UTC).strftime("%Y-%m-%d %H:%M")

    return JSONResponse(content=releases)


@router.get("/{version}/")
async def release_detail(version: str) -> HTMLResponse:
    """Serve a detail page for a specific release version."""
    settings = get_settings()
    base = Path(settings.RELEASES_DIR).resolve()
    index_file = base / "index.json"

    if not index_file.is_file():
        raise HTTPException(status_code=404, detail="No releases found")

    data = json.loads(index_file.read_text())
    all_releases = data.get("releases", data) if isinstance(data, dict) else data

    # Normalize version for matching (strip leading 'v')
    norm = version.lstrip("v")
    release = None
    release_idx = -1
    for idx, r in enumerate(all_releases):
        if r["version"].lstrip("v") == norm:
            release = r
            release_idx = idx
            break

    if release is None:
        raise HTTPException(status_code=404, detail="Release not found")

    # Enrich with file info
    apk_rel = release.get("apk") or release.get("file", "")
    apk_path = base / apk_rel
    size_str = ""
    date_str = ""
    if apk_path.is_file():
        stat = apk_path.stat()
        size_str = f"{stat.st_size / (1024 * 1024):.1f} MB"
        date_str = datetime.fromtimestamp(stat.st_mtime, tz=UTC).strftime("%Y-%m-%d %H:%M")

    ver_display = version if version.startswith("v") else f"v{version}"

    meta_parts = []
    if date_str:
        meta_parts.append(
            f'<div class="detail-section"><div class="detail-label">Date</div>'
            f'<div class="detail-value">{date_str}</div></div>'
        )
    if size_str:
        meta_parts.append(
            f'<div class="detail-section"><div class="detail-label">Size</div>'
            f'<div class="detail-value">{size_str}</div></div>'
        )

    notes_raw = release.get("notes", "")
    # Split dash-prefixed items onto separate lines
    notes_formatted = notes_raw.replace("- ", "\n- ").strip()
    notes_html = ""
    if notes_formatted:
        escaped = html_mod.escape(notes_formatted)
        notes_html = (
            f'<div class="detail-section"><div class="detail-label">Release notes</div>'
            f'<div class="notes">{escaped}</div></div>'
        )

    badge = '<span class="badge">latest</span>' if release_idx == 0 else ""

    # Build APK download URL relative to the detail page
    apk_url = f"../{apk_rel}"

    page = DETAIL_PAGE % {
        "styles": COMMON_STYLES,
        "version": ver_display,
        "badge": badge,
        "meta_sections": "\n".join(meta_parts),
        "notes_html": notes_html,
        "apk_url": apk_url,
    }
    return HTMLResponse(content=page)


@router.get("/{file_path:path}")
async def releases_file(file_path: str) -> FileResponse:
    """Serve static files (APK, release-notes.txt, etc.) from the releases directory."""
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
