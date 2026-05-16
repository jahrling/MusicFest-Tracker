"""Download poster images from URLs, normalize to JPEG, return base64."""

from __future__ import annotations

import base64
import hashlib
import mimetypes
from pathlib import Path
from urllib.parse import urlparse

import httpx
from PIL import Image
import io

from config import POSTERS_DIR, SCRAPE_DELAY_SECONDS


SUPPORTED_MIME = {"image/jpeg", "image/png", "image/webp", "application/pdf"}
MAX_DIMENSION = 4096  # pixels — keep within Claude vision limits


def download_poster(url: str) -> Path:
    """Download poster from URL, save to POSTERS_DIR, return local path."""
    resp = httpx.get(url, follow_redirects=True, timeout=30)
    resp.raise_for_status()

    content_type = resp.headers.get("content-type", "").split(";")[0].strip()
    ext = mimetypes.guess_extension(content_type) or ".jpg"
    if ext == ".jpe":
        ext = ".jpg"

    # Stable filename from URL hash
    url_hash = hashlib.sha256(url.encode()).hexdigest()[:16]
    filename = f"{url_hash}{ext}"
    dest = POSTERS_DIR / filename
    dest.write_bytes(resp.content)
    return dest


def load_image_as_base64(path: Path) -> tuple[str, str]:
    """Return (base64_data, media_type) for a local image file."""
    suffix = path.suffix.lower()

    if suffix == ".pdf":
        # Return raw PDF bytes; Claude can handle PDFs natively
        data = path.read_bytes()
        return base64.standard_b64encode(data).decode(), "application/pdf"

    # Open and optionally resize with Pillow
    img = Image.open(path)
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGB")

    # Downscale if needed
    w, h = img.size
    if max(w, h) > MAX_DIMENSION:
        scale = MAX_DIMENSION / max(w, h)
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)

    buf = io.BytesIO()
    fmt = "JPEG" if suffix in (".jpg", ".jpeg") else "PNG"
    img.save(buf, format=fmt)
    media_type = "image/jpeg" if fmt == "JPEG" else "image/png"
    return base64.standard_b64encode(buf.getvalue()).decode(), media_type


def prepare_poster(source: str | Path) -> tuple[str, str, Path]:
    """
    Accept a URL string or local path.
    Returns (base64_data, media_type, local_path).
    """
    if isinstance(source, str) and source.startswith("http"):
        local_path = download_poster(source)
    else:
        local_path = Path(source)

    b64, media_type = load_image_as_base64(local_path)
    return b64, media_type, local_path
