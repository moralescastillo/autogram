"""Validation, run before anything is uploaded.

Two principles shape this.

**Collect everything, stop at nothing.** A post with three problems reports
three problems. Failing on the first would have the user fix, wait an hour, and
discover the next one — which is how people abandon a tool.

**Check locally what Instagram would reject remotely.** Not to duplicate its
rules, but to fail in seconds with a readable message rather than after an
upload with an error code. Instagram remains the final authority; anything not
checkable here still gets caught there.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from autogram.content import media as media_mod
from autogram.content.aspect import (
    MAX_FEED_RATIO,
    MIN_FEED_RATIO,
    RATIO_TOLERANCE,
    Aspect,
    of as aspect_of,
)
from autogram.content.media import MediaItem, MediaKind
from autogram.content.post import (
    CAPTION_MAX_LENGTH,
    HASHTAG_MAX_COUNT,
    Post,
    PostType,
)

CAROUSEL_MIN_ITEMS = 2
CAROUSEL_MAX_ITEMS = 10

#: Meta's documented ceiling for images published through the API.
IMAGE_MAX_BYTES = 8 * 1024 * 1024
VIDEO_MAX_BYTES = 100 * 1024 * 1024

REEL_MIN_SECONDS = 3
REEL_MAX_SECONDS = 15 * 60
STORY_VIDEO_MAX_SECONDS = 60


@dataclass
class ValidationResult:
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems

    def add(self, problem: str) -> None:
        self.problems.append(problem)

    def as_error_text(self, post_name: str) -> str:
        """Render the problems as the ``error.txt`` a user will read."""
        lines = [
            f"Autogram could not publish '{post_name}'.",
            "",
            "Fix the following and the next scheduled run will try again:",
            "",
        ]
        lines.extend(f"  - {problem}" for problem in self.problems)
        lines.extend(["", "This file is replaced each time the post is retried."])
        return "\n".join(lines)


def validate(post: Post, items: list[MediaItem]) -> ValidationResult:
    """Check a post and its loaded media. Never raises; returns every problem."""
    result = ValidationResult()

    if not items:
        result.add("The folder contains no images or videos.")
        return result

    try:
        post_type = post.resolved_type
    except Exception as exc:
        result.add(str(exc))
        return result

    _check_caption(post, result)
    _check_type_matches_media(post_type, items, result)
    _check_sizes(items, result)

    if post_type is PostType.CAROUSEL:
        _check_carousel(post, items, result)
    elif post_type is PostType.SINGLE:
        _check_feed_ratio(items[0], result)
    elif post_type is PostType.REEL:
        _check_reel(items, result)
    elif post_type is PostType.STORY:
        _check_story(items, result)

    if post.aspect is not None:
        _check_declared_aspect(post.aspect, items, result)

    return result


def _check_caption(post: Post, result: ValidationResult) -> None:
    if len(post.caption) > CAPTION_MAX_LENGTH:
        result.add(
            f"The caption is {len(post.caption)} characters; Instagram allows "
            f"{CAPTION_MAX_LENGTH}."
        )

    hashtags = post.hashtags
    if len(hashtags) > HASHTAG_MAX_COUNT:
        result.add(
            f"The caption has {len(hashtags)} hashtags; Instagram allows "
            f"{HASHTAG_MAX_COUNT}."
        )


def _check_type_matches_media(
    post_type: PostType, items: list[MediaItem], result: ValidationResult
) -> None:
    videos = [i for i in items if i.kind is MediaKind.VIDEO]
    images = [i for i in items if i.kind is MediaKind.IMAGE]

    if post_type is PostType.SINGLE:
        if len(items) != 1:
            result.add(
                f"type is 'single' but the folder has {len(items)} media files. "
                f"Use 'carousel', or remove the extras."
            )
        elif videos:
            result.add("type is 'single' but the media is a video. Use 'reel'.")

    elif post_type is PostType.REEL:
        if not videos:
            result.add("type is 'reel' but the folder has no video.")
        elif len(videos) > 1:
            result.add(f"type is 'reel' but the folder has {len(videos)} videos.")
        elif images:
            result.add(
                "type is 'reel' but the folder also has images. Name a cover "
                "image 'cover.jpg' if that is what you meant."
            )

    elif post_type is PostType.STORY:
        if len(items) != 1:
            result.add(
                f"type is 'story' but the folder has {len(items)} media files. "
                f"A story is one image or one video."
            )

    elif post_type is PostType.CAROUSEL:
        if not CAROUSEL_MIN_ITEMS <= len(items) <= CAROUSEL_MAX_ITEMS:
            result.add(
                f"A carousel needs between {CAROUSEL_MIN_ITEMS} and "
                f"{CAROUSEL_MAX_ITEMS} items; this folder has {len(items)}."
            )


def _check_sizes(items: list[MediaItem], result: ValidationResult) -> None:
    for item in items:
        if item.kind is MediaKind.IMAGE and item.size > IMAGE_MAX_BYTES:
            result.add(
                f"{item.name} is {item.size / 1_048_576:.1f}MB after conversion; "
                f"Instagram's limit is {IMAGE_MAX_BYTES // 1_048_576}MB."
            )
        elif item.kind is MediaKind.VIDEO and item.size > VIDEO_MAX_BYTES:
            result.add(
                f"{item.name} is {item.size / 1_048_576:.1f}MB; Instagram's "
                f"limit is {VIDEO_MAX_BYTES // 1_048_576}MB."
            )


def _ratio(item: MediaItem) -> Aspect | None:
    if not item.width or not item.height:
        return None
    return aspect_of(item.width, item.height)


def _check_feed_ratio(item: MediaItem, result: ValidationResult) -> None:
    ratio = _ratio(item)
    if ratio is None:
        return
    if not MIN_FEED_RATIO * (1 - RATIO_TOLERANCE) <= ratio.value <= MAX_FEED_RATIO * (
        1 + RATIO_TOLERANCE
    ):
        result.add(
            f"{item.name} is {ratio} ({item.width}x{item.height}). Feed posts "
            f"must be between 4:5 (tall) and 1.91:1 (wide)."
        )


def _check_carousel(post: Post, items: list[MediaItem], result: ValidationResult) -> None:
    for item in items:
        _check_feed_ratio(item, result)

    ratios = [(item, _ratio(item)) for item in items]
    measured = [(item, ratio) for item, ratio in ratios if ratio is not None]
    if len(measured) < 2:
        return

    first_item, first_ratio = measured[0]
    mismatched = [
        item.name for item, ratio in measured[1:] if not ratio.matches(first_ratio)
    ]
    if mismatched:
        # Instagram crops every child to the first item's ratio, so this would
        # silently mangle the rest rather than fail.
        result.add(
            f"Carousel items must all share one aspect ratio. "
            f"{first_item.name} is {first_ratio}, but "
            f"{', '.join(mismatched)} differ. Instagram would crop them to match."
        )


def _check_reel(items: list[MediaItem], result: ValidationResult) -> None:
    for item in items:
        if item.kind is not MediaKind.VIDEO:
            continue
        if item.duration is not None:
            if item.duration < REEL_MIN_SECONDS:
                result.add(
                    f"{item.name} is {item.duration:.1f}s; reels must be at "
                    f"least {REEL_MIN_SECONDS}s."
                )
            elif item.duration > REEL_MAX_SECONDS:
                result.add(
                    f"{item.name} is {item.duration / 60:.1f} minutes; reels "
                    f"can be at most {REEL_MAX_SECONDS // 60} minutes."
                )


def _check_story(items: list[MediaItem], result: ValidationResult) -> None:
    # Stories are exempt from the feed ratio bounds: 9:16 is correct here and
    # would fail the feed check.
    for item in items:
        if item.kind is MediaKind.VIDEO and item.duration is not None:
            if item.duration > STORY_VIDEO_MAX_SECONDS:
                result.add(
                    f"{item.name} is {item.duration:.0f}s; story videos can be "
                    f"at most {STORY_VIDEO_MAX_SECONDS}s."
                )


def _check_declared_aspect(
    declared: Aspect, items: list[MediaItem], result: ValidationResult
) -> None:
    for item in items:
        ratio = _ratio(item)
        if ratio is None:
            continue
        if not ratio.matches(declared):
            result.add(
                f"{item.name} is {ratio} ({item.width}x{item.height}) but "
                f"post.md declares {declared}."
            )


def load_media(storage, post: Post) -> tuple[list[MediaItem], ValidationResult]:
    """Read and measure a post's media.

    Returns the items alongside any problems reading them, so an unreadable
    file is reported like every other problem rather than crashing the run.
    """
    items: list[MediaItem] = []
    result = ValidationResult()

    for path in post.media:
        try:
            data = storage.read(path)
        except FileNotFoundError:
            result.add(f"{path.rsplit('/', 1)[-1]} could not be read from storage.")
            continue

        try:
            items.append(media_mod.probe(data, path))
        except ValueError as exc:
            result.add(f"{path.rsplit('/', 1)[-1]} {exc}")

    return items, result
