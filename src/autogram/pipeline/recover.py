"""Resolving a publish that was started but never confirmed.

A run can die between Instagram accepting a post and the ledger recording it —
most plausibly when a runner hits its timeout during the fifteen-minute video
poll. The post folder still holds a ``publishing.json`` naming the container,
and Instagram knows what became of it.

This runs **before** scheduling, because recovering a published post writes the
ledger, and the ledger is what tells the scheduler whether today's slot is
already used.
"""

from __future__ import annotations

import logging

from autogram.content.post import Post
from autogram.instagram.client import ContainerStatus
from autogram.instagram.errors import Disposition, InstagramError, MediaNotReady
from autogram.pipeline.context import Context
from autogram.pipeline.publish import Outcome, Result, _cleanup_staged, _clear_marker
from autogram.pipeline.publish import _fail, _succeed
from autogram.state import log as events

log = logging.getLogger(__name__)


def resolve_pending(ctx: Context, post: Post) -> Outcome | None:
    """Settle an in-flight post. Returns None if there was nothing pending."""
    pending = ctx.pending.read(post.folder)
    if pending is None:
        return None

    log.info(
        "Found an unfinished publish for '%s' (container %s); asking Instagram.",
        post.name,
        pending.container_id,
    )

    try:
        status = ctx.client.container_status(pending.container_id)
    except InstagramError as exc:
        if exc.disposition is Disposition.AUTH:
            raise
        # Without knowing the container's fate, publishing again risks a
        # duplicate. Leave everything and try next run.
        ctx.events.emit(
            events.RETRY_LATER, post=post.name, reason=f"status unknown: {exc}", now=ctx.now
        )
        return Outcome(Result.RETRY_LATER, post.name, f"container status unknown: {exc}")

    if status is ContainerStatus.PUBLISHED:
        # It went out; only the bookkeeping was lost. The media id is not
        # recoverable from a container, hence recorded as recovered.
        log.info("'%s' was already published; recording it.", post.name)
        return _succeed(ctx, post, None, pending.staged, recovered=True)

    if status is ContainerStatus.FINISHED:
        try:
            published = ctx.client.publish(pending.container_id)
        except MediaNotReady as exc:
            return Outcome(Result.RETRY_LATER, post.name, str(exc))
        except InstagramError as exc:
            if exc.disposition is Disposition.AUTH:
                raise
            if exc.disposition is Disposition.RETRY_LATER:
                ctx.events.emit(
                    events.RETRY_LATER, post=post.name, reason=str(exc), now=ctx.now
                )
                return Outcome(Result.RETRY_LATER, post.name, str(exc))
            _cleanup_staged(ctx, pending.staged)
            _clear_marker(ctx, post)
            return _fail(ctx, post, [str(exc)])

        return _succeed(ctx, post, published.id, pending.staged)

    if status is ContainerStatus.IN_PROGRESS:
        try:
            ctx.client.wait_until_ready(pending.container_id)
            published = ctx.client.publish(pending.container_id)
        except MediaNotReady as exc:
            ctx.events.emit(
                events.RETRY_LATER, post=post.name, reason=str(exc), now=ctx.now
            )
            return Outcome(Result.RETRY_LATER, post.name, str(exc))
        except InstagramError as exc:
            if exc.disposition is Disposition.AUTH:
                raise
            if exc.disposition is Disposition.RETRY_LATER:
                ctx.events.emit(
                    events.RETRY_LATER, post=post.name, reason=str(exc), now=ctx.now
                )
                return Outcome(Result.RETRY_LATER, post.name, str(exc))
            _cleanup_staged(ctx, pending.staged)
            _clear_marker(ctx, post)
            return _fail(ctx, post, [str(exc)])

        return _succeed(ctx, post, published.id, pending.staged)

    # ERROR or EXPIRED: the container is dead, but the media is unchanged and
    # the cause is often transient (a fetch that timed out). Clear up and let
    # this run try again from scratch.
    log.info(
        "Container for '%s' reported %s; discarding it and retrying.",
        post.name,
        status.value,
    )
    _cleanup_staged(ctx, pending.staged)
    _clear_marker(ctx, post)
    return None
