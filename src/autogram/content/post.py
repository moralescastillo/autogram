"""The post model: a folder is a post (DESIGN.md §5.1).

The caption lives inside the folder it belongs to, so it cannot desync from its
media. This is the whole reason for the folder-per-post shape — the previous
system kept captions in a spreadsheet and matched them to media by name, which
silently paired the wrong caption with the wrong image whenever a file was
renamed or reordered.

Nothing is encoded in filenames except order. ``01.jpg`` sorts before
``02.jpg``; that is the only meaning a name carries.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class PostType(str, Enum):
    SINGLE = "single"
    CAROUSEL = "carousel"
    REEL = "reel"
    STORY = "story"


IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp"}
VIDEO_SUFFIXES = {".mp4", ".mov"}


@dataclass(frozen=True)
class UserTag:
    """An @-mention placed on the media. Coordinates are 0..1, images only."""

    username: str
    x: float | None = None
    y: float | None = None


@dataclass
class Post:
    """One post, discovered from one folder."""

    folder: str
    media: list[str]
    caption: str = ""
    type: PostType | None = None
    aspect: str | None = None
    user_tags: list[UserTag] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.folder.rstrip("/").rsplit("/", 1)[-1]

    def infer_type(self) -> PostType:
        """Derive the post type from the media when it was not declared.

        One image is a single post, several are a carousel, one video is a
        reel. Stories are never inferred — a story is a deliberate choice and
        must be declared in ``post.md``.
        """
        raise NotImplementedError


def parse_post_md(raw: str) -> dict:
    """Parse ``post.md`` into front matter plus caption body.

    Every field is optional; a folder with no ``post.md`` at all is a valid
    post with no caption. Returns a dict of front-matter fields with the
    caption under ``caption``.
    """
    raise NotImplementedError


def discover(storage, root: str) -> list[Post]:
    """Find posts under ``root``, in folder-name order."""
    raise NotImplementedError
