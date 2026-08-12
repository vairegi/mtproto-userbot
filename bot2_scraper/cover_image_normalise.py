"""
cover_image_normalise.py — v11.2

Normalise arbitrary cover bytes into a Telegram-friendly JPEG so
``InputMediaUploadedPhoto`` never fails with "The extension of the
photo is invalid".

Why this exists
---------------
v11 flipped the cover source from ``t.nhentai.net/.../cover.jpg.webp``
(downscaled thumbnail) to ``i.nhentai.net/galleries/<media_id>/1.<ext>``
(full-quality page 1). That's higher quality — good — but some
galleries have page 1 in ``.webp``, ``.gif``, or ``.png`` extensions.
Telegram's photo pipeline accepts JPEG cleanly; the other formats
frequently trigger ``PHOTO_INVALID_DIMENSIONS`` or
``"The extension of the photo is invalid"`` on ``InputMediaUploadedPhoto``,
which forces the caller to fall back to a non-photo ``send_file(buf, ...)``
that Telegram then renders as a tiny sticker-style preview (the exact
bug in the user's screenshot).

Fix: decode the bytes with Pillow, re-encode as a well-formed JPEG
(quality 88, RGB, max side ≤ 2560 to stay inside Telegram's 10 MB
photo limit for any realistic cover). Fall back to the original bytes
if Pillow isn't installed or the decode fails — the caller still has
its own non-photo fallback path.

This module is intentionally dependency-optional: Pillow is listed in
requirements.txt, but if it's missing at runtime we log-and-fallback
so we never break the pipeline over a missing wheel.
"""
from __future__ import annotations

import io
import logging
from typing import Optional, Tuple

log = logging.getLogger("cover_image_normalise")

# Telegram's photo pipeline is happiest with JPEG ≤ 10 MB and max side
# ≤ 2560 px. Cover images are typically ~1280 × 1800 so this cap only
# ever bites on very large gallery pages.
_MAX_SIDE = 2560
_JPEG_QUALITY = 88


def normalise_cover_bytes(
    raw: Optional[bytes],
    *,
    source_url: str = "",
) -> Tuple[Optional[bytes], str]:
    """
    Turn raw cover bytes into ``(jpeg_bytes, extension)`` suitable for
    Telethon's ``InputMediaUploadedPhoto``.

    Returns
    -------
    (bytes, ".jpg")   on success (either a real JPEG re-encode, or a
                      pass-through if the source was already JPEG and
                      small enough).
    (raw, guess)      on any failure — caller keeps its existing
                      non-photo fallback path.
    (None, ".jpg")    if ``raw`` is falsy.
    """
    if not raw:
        return None, ".jpg"

    # Quick pass-through: if the URL clearly says .jpg / .jpeg AND the
    # bytes start with the JPEG magic, hand them back untouched.
    u = (source_url or "").lower()
    is_jpeg_url = ".jpg" in u or ".jpeg" in u
    starts_jpeg = raw[:3] == b"\xff\xd8\xff"
    if is_jpeg_url and starts_jpeg and len(raw) <= 8 * 1024 * 1024:
        return raw, ".jpg"

    # Everything else: try to decode + re-encode as JPEG via Pillow.
    try:
        from PIL import Image  # local import so a missing dep can't
                                # crash the caller at import time.
    except Exception as e:      # noqa: BLE001
        log.warning(
            "Pillow unavailable, leaving cover as-is (this may cause a "
            "small preview on Telegram): %s", e,
        )
        return raw, _extension_from_url(source_url)

    try:
        with Image.open(io.BytesIO(raw)) as im:
            # Convert palette / RGBA / P / LA / CMYK etc. down to RGB
            # so the JPEG encoder doesn't choke.
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")

            # Cap the max side. Pillow's thumbnail() keeps aspect ratio.
            if max(im.size) > _MAX_SIDE:
                im.thumbnail((_MAX_SIDE, _MAX_SIDE), Image.LANCZOS)

            out = io.BytesIO()
            im.save(
                out,
                format="JPEG",
                quality=_JPEG_QUALITY,
                optimize=True,
                progressive=True,
            )
            data = out.getvalue()
            if len(data) < 200:
                log.warning(
                    "cover re-encode produced suspiciously small JPEG "
                    "(%d bytes) for %s", len(data), source_url,
                )
                return raw, _extension_from_url(source_url)
            return data, ".jpg"
    except Exception as e:  # noqa: BLE001
        log.warning(
            "cover re-encode failed for %s (%s) — keeping raw bytes",
            source_url, e,
        )
        return raw, _extension_from_url(source_url)


def _extension_from_url(url: str) -> str:
    u = (url or "").lower()
    for ext in (".webp", ".png", ".gif", ".jpeg", ".jpg"):
        if ext in u:
            return ".jpg" if ext == ".jpeg" else ext
    return ".jpg"
