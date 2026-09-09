"""Publishing one post.

The order here is deliberate and load-bearing:

1. Read and validate the media. Nothing is uploaded until it passes.
2. Stage the media to the serving backend and take fetchable URLs.
3. Create the containers, waiting for each carousel child before building the
   parent — a child that never finishes should not leave an orphan parent.
4. **Write the pending marker as soon as the final container exists**, before
   waiting on it. The wait can run fifteen minutes, which is exactly where a
   runner timeout bites; without the marker already on disk, the next run
   would build a second container and publish twice.
5. Publish.
6. Record in the ledger, clear the marker, retire the folder, delete staged
   media — in that order, because the ledger is the one fact recovery cannot
   reconstruct on its own.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum

from autogram.content.post import Post, PostType
from autogram.content.validate import ValidationResult, load_media, validate
from autogram.instagram.errors import Disposition, InstagramError, MediaNotReady
from autogram.pipeline.context import PUBLISHED_ROOT, STAGING_ROOT, Context
from autogram.state import log as events

log = logging.getLogger(__name__)


class Result(str, Enum):
    PUBLISHED = "published"
    FAILED = "failed"
    RETRY_LATER = "retry_later"
    SKIPPED = "skipped"


@dataclass
class Outcome:
    result: Result
    post: str
    detail: str = ""
    media_id: str | None = None
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.result in (Result.PUBLISHED, Result.SKIPPED, Result.RETRY_LATER)


def publish_post(ctx: Context, post: Post) -> Outcome:
    """Publish one post, routing every failure to the right place."""
    name = post.name

    if ctx.ledger.is_published(name):
        # A folder reusing the name of something already published would
        # otherwise post twice under one identity.
        return _fail(
            ctx,
            post,
            [
                f"A post named '{name}' was already published on "
                f"{ctx.ledger.find(name).published_at:%Y-%m-%d}. "
                f"Rename this folder to publish it as a new post."
            ],
        )

    items, read_problems = load_media(ctx.authoring, post)
    problems = list(read_problems.problems)
    if items:
        problems.extend(validate(post, items).problems)

    if problems:
        return _fail(ctx, post, problems)

    staged: list[str] = []
    try:
        urls = _stage(ctx, post, items)
        staged = list(urls.values())

        container_id = _create_container(ctx, post, items, urls)

        # The marker goes down before the wait, not before the publish.
        ctx.pending.write(
            post.folder,
            container_id,
            post_type=post.resolved_type.value,
            staged=staged,
            now=ctx.now,
        )

        ctx.client.wait_until_ready(container_id)
        published = ctx.client.publish(container_id)

    except MediaNotReady as exc:
        # A slow video, not a broken one. Leave everything in place.
        ctx.events.emit(events.RETRY_LATER, post=name, reason=str(exc), now=ctx.now)
        return Outcome(Result.RETRY_LATER, name, str(exc))

    except InstagramError as exc:
        if exc.disposition is Disposition.AUTH:
            raise
        if exc.disposition is Disposition.RETRY_LATER:
            ctx.events.emit(events.RETRY_LATER, post=name, reason=str(exc), now=ctx.now)
            return Outcome(Result.RETRY_LATER, name, str(exc))

        _cleanup_staged(ctx, staged)
        _clear_marker(ctx, post)
        return _fail(ctx, post, [str(exc)])

    return _succeed(ctx, post, published.id, staged)


def _stage(ctx: Context, post: Post, items) -> dict[str, str]:
    """Copy media to the serving backend and return fetchable URLs by path."""
    urls = {}
    for item in items:
        path = ctx.staging_path(post.name, item.upload_name)
        ctx.serving.write(path, item.upload_bytes)
        urls[item.path] = path
    return {item.path: urls[item.path] for item in items}


def _url_for(ctx: Context, staged_path: str) -> str:
    return ctx.serving.fetchable_url(staged_path)


def _create_container(ctx: Context, post: Post, items, staged: dict[str, str]) -> str:
    """Build the container to publish, whatever kind of post this is."""
    post_type = post.resolved_type
    tags = [tag.as_dict() for tag in post.user_tags] or None

    if post_type is PostType.SINGLE:
        return ctx.client.create_image_container(
            _url_for(ctx, staged[items[0].path]),
            caption=post.caption or None,
            alt_text=post.alt_text,
            user_tags=tags,
            location_id=post.location_id,
        )

    if post_type is PostType.STORY:
        item = items[0]
        url = _url_for(ctx, staged[item.path])
        if item.kind.value == "video":
            return ctx.client.create_story_container(video_url=url)
        return ctx.client.create_story_container(image_url=url)

    if post_type is PostType.REEL:
        video = next(i for i in items if i.kind.value == "video")
        cover_url = None
        if post.cover:
            # The cover is staged separately: it is not part of the post's
            # media, so validation and ordering never saw it.
            cover_bytes = ctx.authoring.read(post.cover)
            from autogram.content.media import probe

            cover_item = probe(cover_bytes, post.cover)
            cover_path = ctx.staging_path(post.name, cover_item.upload_name)
            ctx.serving.write(cover_path, cover_item.upload_bytes)
            cover_url = _url_for(ctx, cover_path)

        return ctx.client.create_reel_container(
            _url_for(ctx, staged[video.path]),
            caption=post.caption or None,
            cover_url=cover_url,
            thumb_offset=post.thumb_offset,
            share_to_feed=post.share_to_feed,
            user_tags=tags,
            location_id=post.location_id,
        )

    # Carousel: children first, each waited on before the parent is built.
    children = []
    for item in items:
        url = _url_for(ctx, staged[item.path])
        if item.kind.value == "video":
            child = ctx.client.create_video_container(url, is_carousel_item=True)
        else:
            child = ctx.client.create_image_container(url, is_carousel_item=True)
        ctx.client.wait_until_ready(child)
        children.append(child)

    return ctx.client.create_carousel_container(
        children, caption=post.caption or None, location_id=post.location_id
    )


def _succeed(ctx: Context, post: Post, media_id: str | None, staged: list[str],
             *, recovered: bool = False) -> Outcome:
    """Record, retire and tidy up, in the order recovery depends on."""
    name = post.name

    ctx.ledger.record(
        name,
        media_id=media_id,
        post_type=post.resolved_type.value,
        immediate=post.immediate,
        recovered=recovered,
        now=ctx.now,
    )
    ctx.events.emit(
        events.RECOVERED if recovered else events.PUBLISHED,
        post=name,
        media_id=media_id,
        post_type=post.resolved_type.value,
        now=ctx.now,
    )

    _clear_marker(ctx, post)
    _clear_error(ctx, post)

    try:
        ctx.authoring.move(post.folder, f"{PUBLISHED_ROOT}/{name}")
    except Exception as exc:
        # Already published and recorded, so this is untidy rather than
        # harmful — the ledger stops it going out again.
        log.warning("Published '%s' but could not move the folder: %s", name, exc)

    _cleanup_staged(ctx, staged)

    return Outcome(Result.PUBLISHED, name, media_id=media_id)


def _fail(ctx: Context, post: Post, problems: list[str]) -> Outcome:
    """Explain the failure where the user will see it: in the post's folder."""
    result = ValidationResult(problems=problems)

    # Written last, after any cleanup, so a user who spots it and immediately
    # edits the folder is not racing a delete.
    ctx.authoring.write(
        ctx.error_path(post.folder), result.as_error_text(post.name).encode("utf-8")
    )
    ctx.events.emit(events.FAILED, post=post.name, problems=problems, now=ctx.now)

    return Outcome(Result.FAILED, post.name, problems=problems)


def _clear_marker(ctx: Context, post: Post) -> None:
    try:
        ctx.pending.clear(post.folder)
    except Exception:  # pragma: no cover - defensive
        pass


def _clear_error(ctx: Context, post: Post) -> None:
    """Remove a previous failure note once the post succeeds."""
    try:
        ctx.authoring.delete(ctx.error_path(post.folder))
    except Exception:  # pragma: no cover - defensive
        pass


def _cleanup_staged(ctx: Context, staged: list[str]) -> None:
    for path in staged:
        try:
            ctx.serving.delete(path)
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("Could not remove staged file %s: %s", path, exc)


def sweep_staging(ctx: Context, live_names: set[str]) -> None:
    """Delete staged media belonging to posts that no longer exist.

    Covers the case where a marker was lost or unreadable and its staged files
    would otherwise sit in the bucket forever.
    """
    try:
        entries = ctx.serving.list(STAGING_ROOT)
    except Exception:  # pragma: no cover - defensive
        return

    for entry in entries:
        if entry.is_dir and entry.path.rsplit("/", 1)[-1] not in live_names:
            try:
                ctx.serving.delete(entry.path)
                log.info("Removed orphaned staging folder %s", entry.path)
            except Exception:  # pragma: no cover - defensive
                pass
