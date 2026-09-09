"""Content discovery, parsing, media handling and validation.

Images are generated in-process with Pillow rather than committed as fixtures,
so the tests state exactly what they are testing — an image with alpha, an
image whose EXIF says "rotate 90°" — instead of hiding it in a binary blob.
"""

from __future__ import annotations

import io
import shutil

import pytest
from PIL import Image

from autogram.content import media as media_mod
from autogram.content.aspect import AspectError, Aspect
from autogram.content.aspect import parse as parse_aspect
from autogram.content.media import MediaKind, probe, probe_image
from autogram.content.post import (
    Post,
    PostError,
    PostType,
    build,
    discover,
    natural_key,
    parse_post_md,
)
from autogram.content.validate import load_media, validate
from autogram.storage.local import LocalStorage


def make_image(width=1080, height=1080, fmt="JPEG", mode="RGB", orientation=None) -> bytes:
    image = Image.new(mode, (width, height), (120, 140, 160) if mode == "RGB" else None)
    if mode == "RGBA":
        image = Image.new("RGBA", (width, height), (120, 140, 160, 128))

    buffer = io.BytesIO()
    kwargs = {}
    if orientation is not None:
        exif = Image.Exif()
        exif[274] = orientation
        kwargs["exif"] = exif
    image.save(buffer, format=fmt, **kwargs)
    return buffer.getvalue()


@pytest.fixture
def storage(tmp_path):
    return LocalStorage(tmp_path)


class TestAspectParsing:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            (61, 1.0),        # PyYAML reads unquoted 1:1 as base-60
            (245, 0.8),       # 4:5
            (969, 16 / 9),    # 16:9
            (556, 9 / 16),    # 9:16
        ],
    )
    def test_recovers_yaml_sexagesimal_integers(self, raw, expected):
        # This is the trap: `aspect: 1:1` in YAML 1.1 is the integer 61.
        assert parse_aspect(raw).value == pytest.approx(expected)

    @pytest.mark.parametrize("raw", ["1:1", "4:5", "16:9", "1.91:1"])
    def test_parses_quoted_strings(self, raw):
        left, right = raw.split(":")
        assert parse_aspect(raw).value == pytest.approx(float(left) / float(right))

    def test_unquoted_and_quoted_agree(self):
        assert parse_aspect(61).value == parse_aspect("1:1").value

    @pytest.mark.parametrize("raw", ["nonsense", "1:0", "0:1", 1.91, True, "1:-1"])
    def test_rejects_nonsense(self, raw):
        with pytest.raises(AspectError):
            parse_aspect(raw)

    def test_tolerates_near_misses(self):
        # 1080x1081 is square by any reasonable standard.
        assert Aspect(1080, 1081).matches(Aspect(1, 1))

    def test_rejects_real_differences(self):
        assert not Aspect(1080, 1350).matches(Aspect(1, 1))


class TestNaturalSort:
    def test_numbers_sort_as_numbers(self):
        paths = ["p/10.jpg", "p/2.jpg", "p/1.jpg"]
        assert sorted(paths, key=natural_key) == ["p/1.jpg", "p/2.jpg", "p/10.jpg"]

    def test_zero_padded_names_still_work(self):
        paths = ["p/03.jpg", "p/01.jpg", "p/02.jpg"]
        assert sorted(paths, key=natural_key) == ["p/01.jpg", "p/02.jpg", "p/03.jpg"]


class TestPostMdParsing:
    def test_front_matter_and_caption(self):
        fields = parse_post_md("---\ntype: carousel\n---\nHello there\n\n#tag")
        assert fields["type"] == "carousel"
        assert fields["caption"] == "Hello there\n\n#tag"

    def test_no_front_matter_is_all_caption(self):
        assert parse_post_md("Just a caption")["caption"] == "Just a caption"

    def test_empty_file(self):
        assert parse_post_md("")["caption"] == ""

    def test_crlf_line_endings(self):
        fields = parse_post_md("---\r\ntype: single\r\n---\r\nCaption")
        assert fields["type"] == "single"
        assert fields["caption"] == "Caption"

    def test_byte_order_mark_is_stripped(self):
        fields = parse_post_md("﻿---\ntype: single\n---\nCaption")
        assert fields["type"] == "single"

    def test_internal_blank_lines_are_preserved(self):
        fields = parse_post_md("---\ntype: single\n---\nOne\n\nTwo")
        assert fields["caption"] == "One\n\nTwo"

    def test_invalid_yaml_is_reported(self):
        with pytest.raises(PostError, match="not valid YAML"):
            parse_post_md("---\ntype: [unclosed\n---\nCaption")


class TestPostBuilding:
    def test_unknown_settings_are_rejected(self):
        # A typo like "aspcet:" would otherwise be silently ignored.
        with pytest.raises(PostError, match="aspcet"):
            build("queue/p", ["queue/p/01.jpg"], "---\naspcet: 1:1\n---\n")

    def test_unknown_type_is_rejected(self):
        with pytest.raises(PostError, match="unknown type"):
            build("queue/p", ["queue/p/01.jpg"], "---\ntype: tweet\n---\n")

    def test_media_is_naturally_ordered(self):
        post = build("queue/p", ["queue/p/10.jpg", "queue/p/2.jpg"], None)
        assert post.media == ["queue/p/2.jpg", "queue/p/10.jpg"]

    def test_user_tags_from_strings(self):
        post = build("queue/p", ["queue/p/01.jpg"], "---\nuser_tags:\n  - '@someone'\n---\n")
        assert post.user_tags[0].username == "someone"
        assert post.user_tags[0].as_dict() == {"username": "someone"}

    def test_user_tags_with_coordinates(self):
        post = build(
            "queue/p",
            ["queue/p/01.jpg"],
            "---\nuser_tags:\n  - username: someone\n    x: 0.5\n    y: 0.4\n---\n",
        )
        assert post.user_tags[0].as_dict() == {"username": "someone", "x": 0.5, "y": 0.4}

    def test_unquoted_aspect_survives_yaml(self):
        post = build("queue/p", ["queue/p/01.jpg"], "---\naspect: 1:1\n---\n")
        assert post.aspect.value == pytest.approx(1.0)


class TestTypeInference:
    def test_one_image_is_single(self):
        assert build("q/p", ["q/p/01.jpg"], None).resolved_type is PostType.SINGLE

    def test_several_images_is_carousel(self):
        post = build("q/p", ["q/p/01.jpg", "q/p/02.jpg"], None)
        assert post.resolved_type is PostType.CAROUSEL

    def test_one_video_is_reel(self):
        assert build("q/p", ["q/p/clip.mp4"], None).resolved_type is PostType.REEL

    def test_story_is_never_inferred(self):
        # Guessing "story" would publish to the wrong surface entirely.
        assert build("q/p", ["q/p/01.jpg"], None).resolved_type is not PostType.STORY

    def test_declared_type_wins(self):
        post = build("q/p", ["q/p/01.jpg"], "---\ntype: story\n---\n")
        assert post.resolved_type is PostType.STORY

    def test_no_media_is_an_error(self):
        with pytest.raises(PostError, match="no media"):
            build("q/p", [], None).resolved_type


class TestDiscovery:
    def test_finds_posts_in_folder_order(self, storage):
        storage.write("queue/2026-09-20-b/01.jpg", make_image())
        storage.write("queue/2026-09-19-a/01.jpg", make_image())

        posts = discover(storage, "queue")
        assert [p.name for p in posts] == ["2026-09-19-a", "2026-09-20-b"]

    def test_reads_caption_from_post_md(self, storage):
        storage.write("queue/p/01.jpg", make_image())
        storage.write("queue/p/post.md", b"---\ntype: single\n---\nMy caption")

        post = discover(storage, "queue")[0]
        assert post.caption == "My caption"
        assert post.type is PostType.SINGLE

    def test_folder_without_post_md_is_still_valid(self, storage):
        storage.write("queue/p/01.jpg", make_image())

        post = discover(storage, "queue")[0]
        assert post.caption == ""
        assert post.resolved_type is PostType.SINGLE

    def test_reserved_files_are_not_media(self, storage):
        storage.write("queue/p/01.jpg", make_image())
        storage.write("queue/p/post.md", b"caption")
        storage.write("queue/p/error.txt", b"previous failure")
        storage.write("queue/p/publishing.json", b"{}")

        assert len(discover(storage, "queue")[0].media) == 1

    def test_cover_image_is_not_carousel_media(self, storage):
        # One video plus a cover would otherwise infer as a carousel.
        storage.write("queue/p/clip.mp4", b"video-bytes")
        storage.write("queue/p/cover.jpg", make_image())

        post = discover(storage, "queue")[0]
        assert post.media == ["queue/p/clip.mp4"]
        assert post.cover == "queue/p/cover.jpg"
        assert post.resolved_type is PostType.REEL

    def test_uppercase_extensions_are_recognised(self, storage):
        # Phones write .JPG and .HEIC.
        storage.write("queue/p/IMG_0001.JPG", make_image())
        assert len(discover(storage, "queue")[0].media) == 1

    def test_empty_root_is_no_posts(self, storage):
        assert discover(storage, "queue") == []


class TestImageProbing:
    def test_measures_dimensions(self):
        item = probe_image(make_image(1080, 1350), "p/01.jpg")
        assert (item.width, item.height) == (1080, 1350)
        assert item.kind is MediaKind.IMAGE

    def test_plain_jpeg_passes_through_untouched(self):
        original = make_image(1080, 1080)
        item = probe_image(original, "p/01.jpg")
        # Recompressing a correct JPEG would only lose quality.
        assert item.upload_bytes == original

    def test_png_is_converted_to_jpeg(self):
        item = probe_image(make_image(fmt="PNG"), "p/01.png")
        assert item.upload_bytes[:2] == b"\xff\xd8"  # JPEG magic
        assert item.content_type == "image/jpeg"
        assert item.upload_name == "01.jpg"

    def test_transparency_is_flattened(self):
        item = probe_image(make_image(fmt="PNG", mode="RGBA"), "p/01.png")
        with Image.open(io.BytesIO(item.upload_bytes)) as converted:
            assert converted.mode == "RGB"

    def test_exif_rotation_changes_measured_dimensions(self):
        # Orientation 6 means "rotate 90 clockwise": stored 1600x1200 landscape
        # is displayed as 1200x1600 portrait. Measuring raw pixels would
        # validate the wrong aspect ratio entirely.
        item = probe_image(make_image(1600, 1200, orientation=6), "p/01.jpg")
        assert (item.width, item.height) == (1200, 1600)

    def test_exif_rotation_is_baked_into_output(self):
        item = probe_image(make_image(1600, 1200, orientation=6), "p/01.jpg")
        with Image.open(io.BytesIO(item.upload_bytes)) as out:
            assert out.size == (1200, 1600)
            # A leftover tag would make Instagram rotate it a second time.
            assert out.getexif().get(274, 1) in (1, None)

    def test_unreadable_file_reports_clearly(self):
        with pytest.raises(ValueError, match="could not be read"):
            probe_image(b"not an image", "p/01.jpg")

    def test_unsupported_extension_is_rejected(self):
        with pytest.raises(ValueError, match="unsupported file type"):
            probe(b"data", "p/clip.avi")

    @pytest.mark.skipif(
        not media_mod.HEIC_SUPPORTED, reason="pillow-heif not installed"
    )
    def test_heic_is_converted(self):
        buffer = io.BytesIO()
        Image.new("RGB", (1080, 1080), (10, 20, 30)).save(buffer, format="HEIF")
        item = probe_image(buffer.getvalue(), "p/IMG_0001.HEIC")
        assert item.upload_bytes[:2] == b"\xff\xd8"
        assert (item.width, item.height) == (1080, 1080)


class TestFfprobeParsing:
    def test_reads_dimensions_and_duration(self):
        raw = (
            '{"streams":[{"width":1080,"height":1920}],'
            '"format":{"duration":"12.480000"}}'
        )
        assert media_mod.parse_ffprobe(raw) == (1080, 1920, pytest.approx(12.48))

    def test_missing_fields_are_none(self):
        assert media_mod.parse_ffprobe('{"streams":[{}],"format":{}}') == (None, None, None)

    def test_empty_output(self):
        assert media_mod.parse_ffprobe(b"") == (None, None, None)


class TestValidation:
    def _items(self, storage, post):
        items, result = load_media(storage, post)
        assert result.ok, result.problems
        return items

    def test_valid_single_passes(self, storage):
        storage.write("queue/p/01.jpg", make_image(1080, 1080))
        post = discover(storage, "queue")[0]
        assert validate(post, self._items(storage, post)).ok

    def test_collects_every_problem_at_once(self, storage):
        # A user should fix everything in one pass, not one per hour.
        storage.write("queue/p/01.jpg", make_image(1080, 1080))
        storage.write("queue/p/02.jpg", make_image(1080, 1920))
        storage.write(
            "queue/p/post.md",
            ("---\ntype: single\n---\n" + "x" * 2300 + " " + "#tag " * 35).encode(),
        )

        post = discover(storage, "queue")[0]
        result = validate(post, self._items(storage, post))

        assert len(result.problems) >= 3
        joined = " ".join(result.problems)
        assert "caption" in joined
        assert "hashtags" in joined
        assert "single" in joined

    def test_carousel_ratio_mismatch_is_caught(self, storage):
        # Instagram would crop these to match the first, silently.
        storage.write("queue/p/01.jpg", make_image(1080, 1080))
        storage.write("queue/p/02.jpg", make_image(1080, 1350))

        post = discover(storage, "queue")[0]
        result = validate(post, self._items(storage, post))

        assert any("aspect ratio" in p for p in result.problems)

    def test_carousel_with_matching_ratios_passes(self, storage):
        storage.write("queue/p/01.jpg", make_image(1080, 1080))
        storage.write("queue/p/02.jpg", make_image(1080, 1081))  # within tolerance

        post = discover(storage, "queue")[0]
        assert validate(post, self._items(storage, post)).ok

    def test_declared_aspect_mismatch_is_caught(self, storage):
        storage.write("queue/p/01.jpg", make_image(1080, 1350))
        storage.write("queue/p/post.md", b"---\naspect: 1:1\n---\n")

        post = discover(storage, "queue")[0]
        result = validate(post, self._items(storage, post))

        assert any("declares" in p for p in result.problems)

    def test_feed_ratio_bounds_are_enforced(self, storage):
        storage.write("queue/p/01.jpg", make_image(1080, 2400))  # far too tall
        post = discover(storage, "queue")[0]
        result = validate(post, self._items(storage, post))

        assert any("4:5" in p for p in result.problems)

    def test_story_is_exempt_from_feed_bounds(self, storage):
        # 9:16 is correct for a story and would fail the feed check.
        storage.write("queue/p/01.jpg", make_image(1080, 1920))
        storage.write("queue/p/post.md", b"---\ntype: story\n---\n")

        post = discover(storage, "queue")[0]
        assert validate(post, self._items(storage, post)).ok

    def test_carousel_item_limit(self, storage):
        for i in range(11):
            storage.write(f"queue/p/{i:02d}.jpg", make_image(1080, 1080))

        post = discover(storage, "queue")[0]
        result = validate(post, self._items(storage, post))
        assert any("between 2 and 10" in p for p in result.problems)

    def test_reel_declared_without_video(self, storage):
        storage.write("queue/p/01.jpg", make_image(1080, 1920))
        storage.write("queue/p/post.md", b"---\ntype: reel\n---\n")

        post = discover(storage, "queue")[0]
        result = validate(post, self._items(storage, post))
        assert any("no video" in p for p in result.problems)

    def test_unreadable_media_is_reported_not_raised(self, storage):
        storage.write("queue/p/01.jpg", b"this is not an image")

        post = discover(storage, "queue")[0]
        items, result = load_media(storage, post)

        assert not result.ok
        assert "01.jpg" in result.problems[0]
        assert items == []

    def test_error_text_is_readable(self, storage):
        storage.write("queue/p/01.jpg", make_image(1080, 2400))
        post = discover(storage, "queue")[0]
        result = validate(post, self._items(storage, post))

        text = result.as_error_text("2026-09-20-lisbon")
        assert "2026-09-20-lisbon" in text
        assert "4:5" in text
        assert text.count("  - ") == len(result.problems)


@pytest.mark.skipif(not shutil.which("ffprobe"), reason="ffprobe not installed")
class TestLiveFfprobe:
    def test_probes_a_generated_video(self, tmp_path):
        import subprocess

        if not shutil.which("ffmpeg"):
            pytest.skip("ffmpeg not installed")

        path = tmp_path / "clip.mp4"
        subprocess.run(
            ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=1080x1920:rate=30",
             "-t", "4", "-pix_fmt", "yuv420p", str(path)],
            check=True,
        )

        item = probe(path.read_bytes(), "queue/p/clip.mp4")
        assert item.kind is MediaKind.VIDEO
        assert (item.width, item.height) == (1080, 1920)
        assert item.duration == pytest.approx(4, abs=0.5)
