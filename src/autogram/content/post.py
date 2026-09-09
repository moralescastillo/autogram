"""The post model: a folder is a post (DESIGN.md §5.1).

The caption lives inside the folder it belongs to, so it cannot desync from its
media. This is the whole reason for the folder-per-post shape — the previous
system kept captions in a spreadsheet and matched them to media by name, which
silently paired the wrong caption with the wrong image whenever a file was
renamed or reordered.

Nothing is encoded in filenames except order, and that ordering is *natural*:
``2.jpg`` before ``10.jpg``, the way a person reading the folder would expect.
Plain lexicographic sorting puts ``10`` second, which would silently reorder a
carousel shot on a phone.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

import yaml

from autogram.content import media as media_mod
from autogram.content.aspect import Aspect, AspectError
from autogram.content.aspect import parse as parse_aspect

QUEUE_ROOT = "queue"
NOW_ROOT = "now"
PUBLISHED_ROOT = "published"

POST_FILE = "post.md"

#: Front-matter keys a post may set. Anything else is a typo, and typos here
#: fail silently — an unrecognised "aspcet:" would simply never be applied.
KNOWN_KEYS = {"type", "aspect", "user_tags", "location_id", "alt_text",
              "cover", "share_to_feed", "thumb_offset"}

CAPTION_MAX_LENGTH = 2200
HASHTAG_MAX_COUNT = 30
HASHTAG_PATTERN = re.compile(r"#\w+")

_FRONT_MATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)
_DIGITS = re.compile(r"(\d+)")


class PostType(str, Enum):
    SINGLE = "single"
    CAROUSEL = "carousel"
    REEL = "reel"
    STORY = "story"


class PostError(ValueError):
    """A post could not be read. The message is written for the user."""


@dataclass(frozen=True)
class UserTag:
    """An @-mention placed on the media. Coordinates are 0..1, images only."""

    username: str
    x: float | None = None
    y: float | None = None

    def as_dict(self) -> dict:
        tag: dict = {"username": self.username}
        if self.x is not None and self.y is not None:
            tag["x"] = self.x
            tag["y"] = self.y
        return tag


@dataclass
class Post:
    """One post, discovered from one folder."""

    folder: str
    media: list[str]
    caption: str = ""
    type: PostType | None = None
    aspect: Aspect | None = None
    user_tags: list[UserTag] = field(default_factory=list)
    location_id: str | None = None
    alt_text: str | None = None
    cover: str | None = None
    share_to_feed: bool = True
    thumb_offset: int | None = None
    immediate: bool = False

    @property
    def name(self) -> str:
        return self.folder.rstrip("/").rsplit("/", 1)[-1]

    @property
    def hashtags(self) -> list[str]:
        return HASHTAG_PATTERN.findall(self.caption)

    def infer_type(self) -> PostType:
        """Derive the post type from the media when it was not declared.

        Stories are never inferred: a story is a deliberate choice, and
        guessing wrong would publish to the wrong surface entirely.
        """
        images = [p for p in self.media if media_mod.suffix_of(p) in media_mod.IMAGE_SUFFIXES]
        videos = [p for p in self.media if media_mod.suffix_of(p) in media_mod.VIDEO_SUFFIXES]

        if not self.media:
            raise PostError("has no media files.")

        if len(self.media) == 1:
            return PostType.REEL if videos else PostType.SINGLE

        if images and not videos and len(images) > 1:
            return PostType.CAROUSEL

        return PostType.CAROUSEL

    @property
    def resolved_type(self) -> PostType:
        return self.type or self.infer_type()


def natural_key(path: str) -> list:
    """Sort key that reads embedded numbers as numbers.

    ``2.jpg`` sorts before ``10.jpg``. Lexicographic order would not, and a
    carousel would silently come out in the wrong order.
    """
    name = path.rsplit("/", 1)[-1].lower()
    return [int(part) if part.isdigit() else part for part in _DIGITS.split(name)]


def parse_post_md(raw: str) -> dict:
    """Parse ``post.md`` into front-matter fields plus ``caption``.

    Every field is optional. A file with no front matter is entirely caption,
    which is the simplest thing a user can write.
    """
    text = raw.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")

    match = _FRONT_MATTER.match(text)
    if not match:
        return {"caption": text.strip()}

    front_matter, body = match.groups()

    try:
        fields = yaml.safe_load(front_matter) or {}
    except yaml.YAMLError as exc:
        raise PostError(f"has front matter that is not valid YAML: {exc}") from exc

    if not isinstance(fields, dict):
        raise PostError("has front matter that is not a set of key: value pairs.")

    fields = {str(k).lower(): v for k, v in fields.items()}
    fields["caption"] = body.strip()
    return fields


def _parse_user_tags(raw) -> list[UserTag]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise PostError("has a 'user_tags' field that is not a list.")

    tags = []
    for entry in raw:
        if isinstance(entry, str):
            tags.append(UserTag(username=entry.lstrip("@")))
            continue
        if not isinstance(entry, dict) or "username" not in entry:
            raise PostError(f"has a user tag without a username: {entry!r}.")
        tags.append(
            UserTag(
                username=str(entry["username"]).lstrip("@"),
                x=float(entry["x"]) if entry.get("x") is not None else None,
                y=float(entry["y"]) if entry.get("y") is not None else None,
            )
        )
    return tags


def build(folder: str, media_paths: list[str], post_md: str | None, *, immediate: bool = False) -> Post:
    """Assemble a Post from a folder's contents."""
    fields = parse_post_md(post_md) if post_md else {"caption": ""}

    unknown = set(fields) - KNOWN_KEYS - {"caption"}
    if unknown:
        raise PostError(
            f"has unrecognised setting(s) in post.md: {', '.join(sorted(unknown))}. "
            f"Valid settings are: {', '.join(sorted(KNOWN_KEYS))}."
        )

    declared_type = fields.get("type")
    if declared_type is not None:
        try:
            declared_type = PostType(str(declared_type).strip().lower())
        except ValueError:
            raise PostError(
                f"has an unknown type {fields['type']!r}. "
                f"Use one of: {', '.join(t.value for t in PostType)}."
            ) from None

    aspect = None
    if fields.get("aspect") is not None:
        try:
            aspect = parse_aspect(fields["aspect"])
        except AspectError as exc:
            raise PostError(str(exc)) from exc

    return Post(
        folder=folder,
        media=sorted(media_paths, key=natural_key),
        caption=fields.get("caption", ""),
        type=declared_type,
        aspect=aspect,
        user_tags=_parse_user_tags(fields.get("user_tags")),
        location_id=str(fields["location_id"]) if fields.get("location_id") else None,
        alt_text=fields.get("alt_text"),
        cover=fields.get("cover"),
        share_to_feed=bool(fields.get("share_to_feed", True)),
        thumb_offset=int(fields["thumb_offset"]) if fields.get("thumb_offset") is not None else None,
        immediate=immediate,
    )


def discover(
    storage, root: str, *, immediate: bool = False, on_error=None
) -> list[Post]:
    """Find posts under ``root``, in folder-name order.

    Cheap by design: this lists folders and reads only ``post.md``. Media bytes
    are downloaded later, and only for the post about to be published.

    A folder whose ``post.md`` cannot be read is reported through ``on_error``
    and skipped. One typo must not hide every post behind it — that would be a
    silent, confusing outage.
    """
    posts = []

    for entry in sorted(storage.list(root), key=lambda e: e.path):
        if not entry.is_dir:
            continue

        children = storage.list(entry.path)
        media_paths = [
            child.path
            for child in children
            if not child.is_dir
            and media_mod.is_media(child.path)
            and not media_mod.is_reserved(child.path)
            and not media_mod.is_cover(child.path)
        ]

        post_md = None
        if any(c.path.rsplit("/", 1)[-1].lower() == POST_FILE for c in children):
            post_md = storage.read(f"{entry.path}/{POST_FILE}").decode("utf-8")

        cover = next(
            (c.path for c in children if not c.is_dir and media_mod.is_cover(c.path)), None
        )

        try:
            post = build(entry.path, media_paths, post_md, immediate=immediate)
        except PostError as exc:
            if on_error is None:
                raise
            on_error(entry.path, exc)
            continue

        if cover and not post.cover:
            post.cover = cover

        posts.append(post)

    return posts
