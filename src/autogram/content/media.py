"""Reading media, and the one transform this tool performs.

Instagram accepts **JPEG only** for images — no PNG, no HEIC, no WebP. Phones
shoot HEIC and screenshots are PNG, so refusing them would reject most of the
content people actually have. Converting them is therefore the single exception
to "the tool does not process your images" (DESIGN.md §5.3).

The conversion is as light as it can be. A JPEG that is already correctly
oriented passes through untouched — no recompression, no quality loss. Anything
else is transposed to its displayed orientation, flattened to RGB, and encoded
once.

**EXIF orientation matters more than it looks.** Phone cameras commonly store
landscape pixels plus a "rotate 90°" tag. Reading raw dimensions would see a
4:3 landscape image where the user sees a 3:4 portrait, and validate the wrong
ratio. Orientation is applied before anything is measured, and baked into the
output rather than left for Instagram to honour.
"""

from __future__ import annotations

import io
import json
import logging
import shutil
import subprocess
from dataclasses import dataclass
from enum import Enum

log = logging.getLogger(__name__)

try:  # HEIC support must be registered before any Image.open call
    import pillow_heif

    pillow_heif.register_heif_opener()
    HEIC_SUPPORTED = True
except ImportError:  # pragma: no cover - dependency is declared
    HEIC_SUPPORTED = False

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp"}
VIDEO_SUFFIXES = {".mp4", ".mov"}

#: Files that live in a post folder but are not content.
RESERVED_NAMES = {"post.md", "error.txt", "publishing.json", ".ds_store"}

#: A reel's cover image, kept out of the media list so one video plus one cover
#: is still a reel rather than being mistaken for a carousel.
COVER_STEM = "cover"

JPEG_QUALITY = 95
EXIF_ORIENTATION_TAG = 274


class MediaKind(str, Enum):
    IMAGE = "image"
    VIDEO = "video"


@dataclass
class MediaItem:
    """One piece of media, measured and ready to upload."""

    path: str
    kind: MediaKind
    upload_bytes: bytes
    content_type: str
    width: int | None = None
    height: int | None = None
    duration: float | None = None

    @property
    def name(self) -> str:
        return self.path.rsplit("/", 1)[-1]

    @property
    def size(self) -> int:
        return len(self.upload_bytes)

    @property
    def upload_name(self) -> str:
        """Filename to stage under — converted images become .jpg."""
        stem = self.name.rsplit(".", 1)[0]
        return f"{stem}.jpg" if self.kind is MediaKind.IMAGE else self.name


def suffix_of(path: str) -> str:
    name = path.rsplit("/", 1)[-1]
    return f".{name.rsplit('.', 1)[-1].lower()}" if "." in name else ""


def is_media(path: str) -> bool:
    suffix = suffix_of(path)
    return suffix in IMAGE_SUFFIXES or suffix in VIDEO_SUFFIXES


def is_reserved(path: str) -> bool:
    return path.rsplit("/", 1)[-1].lower() in RESERVED_NAMES


def is_cover(path: str) -> bool:
    name = path.rsplit("/", 1)[-1].lower()
    return name.rsplit(".", 1)[0] == COVER_STEM and suffix_of(name) in IMAGE_SUFFIXES


def probe_image(data: bytes, path: str) -> MediaItem:
    """Measure an image and produce upload-ready JPEG bytes."""
    from PIL import Image, ImageOps

    try:
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            rotated = _has_rotation(image)
            oriented = ImageOps.exif_transpose(image)
            width, height = oriented.size
            upload_bytes, content_type = _to_jpeg(
                oriented, data, image.format, rotated=rotated
            )
    except OSError as exc:
        hint = ""
        if suffix_of(path) in (".heic", ".heif") and not HEIC_SUPPORTED:
            hint = " HEIC support is unavailable — install pillow-heif."
        raise ValueError(f"could not be read as an image ({exc}).{hint}") from exc

    return MediaItem(
        path=path,
        kind=MediaKind.IMAGE,
        upload_bytes=upload_bytes,
        content_type=content_type,
        width=width,
        height=height,
    )


def _to_jpeg(
    oriented, original: bytes, source_format: str | None, *, rotated: bool
) -> tuple[bytes, str]:
    """Return JPEG bytes, reusing the original when it is already fine.

    ``rotated`` must describe the *original* image: ``exif_transpose`` clears
    the orientation tag, so asking the transposed copy would always say no and
    a sideways photo would pass straight through.
    """
    from PIL import Image

    already_jpeg = source_format == "JPEG"

    if already_jpeg and not rotated and oriented.mode == "RGB":
        # Nothing to fix; recompressing would only lose quality.
        return original, "image/jpeg"

    image = oriented
    if image.mode in ("RGBA", "LA", "P"):
        # JPEG has no alpha. Compositing on white matches how these images are
        # displayed everywhere else.
        image = image.convert("RGBA")
        background = Image.new("RGB", image.size, (255, 255, 255))
        background.paste(image, mask=image.split()[-1])
        image = background
    elif image.mode != "RGB":
        image = image.convert("RGB")

    buffer = io.BytesIO()
    # No exif= argument: orientation is baked into the pixels above, and a
    # stale tag would rotate the image a second time.
    image.save(buffer, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    return buffer.getvalue(), "image/jpeg"


def _has_rotation(image) -> bool:
    try:
        exif = image.getexif()
    except Exception:  # pragma: no cover - defensive
        return False
    return exif.get(EXIF_ORIENTATION_TAG, 1) not in (1, None)


def probe_video(data: bytes, path: str) -> MediaItem:
    """Measure a video, if ffprobe is available.

    Without ffprobe the video is still uploaded — dimensions and duration are
    simply unknown, and the checks that need them are skipped with a warning.
    Instagram will reject genuinely invalid media anyway; the local checks exist
    to fail faster and more clearly, not to be the only gate.
    """
    width = height = None
    duration = None

    if shutil.which("ffprobe"):
        try:
            width, height, duration = _ffprobe(data)
        except Exception as exc:
            log.warning("ffprobe could not read %s: %s", path, exc)
    else:
        log.warning(
            "ffprobe is not installed; skipping dimension and duration checks "
            "for %s. Instagram will still validate it.",
            path,
        )

    return MediaItem(
        path=path,
        kind=MediaKind.VIDEO,
        upload_bytes=data,
        content_type="video/mp4" if suffix_of(path) == ".mp4" else "video/quicktime",
        width=width,
        height=height,
        duration=duration,
    )


def _ffprobe(data: bytes) -> tuple[int | None, int | None, float | None]:
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height:format=duration",
            "-of", "json",
            "pipe:0",
        ],
        input=data,
        capture_output=True,
        timeout=60,
    )
    return parse_ffprobe(result.stdout)


def parse_ffprobe(raw: bytes | str) -> tuple[int | None, int | None, float | None]:
    """Pull dimensions and duration out of ffprobe's JSON."""
    payload = json.loads(raw or "{}")
    streams = payload.get("streams") or [{}]
    stream = streams[0]

    width = stream.get("width")
    height = stream.get("height")
    duration = payload.get("format", {}).get("duration")

    return (
        int(width) if width else None,
        int(height) if height else None,
        float(duration) if duration else None,
    )


def probe(data: bytes, path: str) -> MediaItem:
    """Measure any supported media file."""
    suffix = suffix_of(path)
    if suffix in IMAGE_SUFFIXES:
        return probe_image(data, path)
    if suffix in VIDEO_SUFFIXES:
        return probe_video(data, path)
    raise ValueError(f"unsupported file type {suffix or '(none)'}.")
