"""
cover_normalise.py — normalise arbitrary cover bytes into well-formed JPEG.

Same job as Bot 0's cover_image_normalise.py (v11.2): Telegram rejects
.webp/.gif covers in InputMediaUploadedPhoto ("The extension of the photo
is invalid") and silently falls back to a non-photo send — the "unnamed
file" bug. Converting to RGB JPEG first guarantees a real photo post.
"""
from __future__ import annotations

import io
import logging
from typing import Optional, Tuple

log = logging.getLogger("bot2fetcher.cover_normalise")


def normalise_cover_bytes(data: bytes, source_url: str = "") -> Tuple[Optional[bytes], str]:
    """Return (jpeg_bytes, ".jpg"). On any failure return (data, guessed_ext)
    unchanged so the caller can still attempt a send."""
    if not data:
        return None, ""
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(data))
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=92)
        out = buf.getvalue()
        if len(out) < 200:
            raise ValueError("normalised cover suspiciously small")
        return out, ".jpg"
    except Exception as e:  # noqa: BLE001
        log.warning("cover normalise failed (%s) — sending raw bytes", e)
        ext = ".jpg"
        u = (source_url or "").lower()
        for cand in (".webp", ".png", ".gif", ".jpg", ".jpeg"):
            if cand in u:
                ext = ".jpg" if cand == ".jpeg" else cand
                break
        return data, ext
