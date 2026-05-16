"""Download poster images from URLs, normalize to JPEG, return base64."""

from __future__ import annotations

import base64
import hashlib
import mimetypes
import re
from pathlib import Path

import httpx
from PIL import Image
import io

from config import POSTERS_DIR


SUPPORTED_MIME = {"image/jpeg", "image/png", "image/webp", "application/pdf"}
MAX_DIMENSION = 4096  # pixels — keep within Claude vision limits

_CONTENT_TYPE_EXT = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/avif": ".avif",
    "image/gif": ".gif",
    "application/pdf": ".pdf",
}


def _rewrite_cdn_url(url: str) -> str:
    """
    Rewrite CDN transformation URLs that default to AVIF to request JPEG instead.

    Wix image URLs embed format parameters like enc_avif in the path.
    Replacing with enc_jpg makes the CDN serve a format Pillow can open.
    """
    if "wixstatic.com" in url:
        url = re.sub(r"\benc_avif\b", "enc_jpg", url)
        url = re.sub(r"\bquality_auto\b", "q_85", url)
    return url


def download_poster(url: str) -> Path:
    """Download poster from URL, save to POSTERS_DIR, return local path."""
    url = _rewrite_cdn_url(url)

    resp = httpx.get(url, follow_redirects=True, timeout=30)
    resp.raise_for_status()

    content_type = resp.headers.get("content-type", "").split(";")[0].strip()

    # Use actual content-type for extension — URL extension is unreliable after CDN rewrites
    ext = _CONTENT_TYPE_EXT.get(content_type) or mimetypes.guess_extension(content_type) or ".jpg"
    if ext == ".jpe":
        ext = ".jpg"

    if ext == ".avif":
        raise ValueError(
            "This URL serves an AVIF image, which isn't supported by the image library. "
            "Download the poster manually and upload the file instead, "
            "or find a JPEG/PNG version of the image."
        )

    # Stable filename from the (possibly rewritten) URL
    url_hash = hashlib.sha256(url.encode()).hexdigest()[:16]
    dest = POSTERS_DIR / f"{url_hash}{ext}"
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

    # Downscale if needed before any conversion (saves memory)
    w, h = img.size
    if max(w, h) > MAX_DIMENSION:
        scale = MAX_DIMENSION / max(w, h)
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)

    buf = io.BytesIO()
    fmt = "JPEG" if suffix in (".jpg", ".jpeg") else "PNG"

    # JPEG doesn't support transparency — flatten RGBA/P onto white
    if fmt == "JPEG" and img.mode in ("RGBA", "P", "LA"):
        background = Image.new("RGB", img.size, (255, 255, 255))
        background.paste(img, mask=img.split()[-1] if img.mode in ("RGBA", "LA") else None)
        img = background
    elif img.mode not in ("RGB", "RGBA", "L"):
        img = img.convert("RGB")

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
