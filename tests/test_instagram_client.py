"""The Instagram client.

HTTP is faked here, unlike the storage tests. The risk in the storage backends
lived in the APIs' real behaviour, so those run against real implementations.
The risk here is in *this* code — the poll loop, error classification, and
whether each media type sends the parameters Instagram expects — so a
controllable fake is what actually exercises it.

Response shapes are taken from Meta's documented payloads.
"""

from __future__ import annotations

import json

import pytest

from autogram.instagram.client import ContainerStatus, InstagramClient
from autogram.instagram.errors import Disposition, InstagramError, MediaNotReady


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.ok = status_code < 400

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeSession:
    """Records every call and replays queued responses."""

    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        if not self.responses:
            raise AssertionError(f"Unexpected call: {method} {url}")
        return self.responses.pop(0)

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)

    @property
    def last(self):
        return self.calls[-1]

    def data_of(self, index):
        return self.calls[index].get("data", {})


@pytest.fixture
def slept():
    return []


@pytest.fixture
def client_factory(slept):
    def build(responses, *, now=None):
        ticks = iter(now or range(0, 100000, 1))
        session = FakeSession(responses)
        client = InstagramClient(
            access_token="TOKEN-SECRET",
            ig_user_id="17841400000000000",
            session=session,
            sleep=slept.append,
            clock=lambda: next(ticks),
        )
        return client, session

    return build


class TestTokenHandling:
    def test_token_is_sent_as_a_header_never_in_the_url(self, client_factory):
        client, session = client_factory([FakeResponse({"id": "container-1"})])
        client.create_image_container("https://example.com/a.jpg")

        call = session.last
        assert "TOKEN-SECRET" not in call["url"]
        assert "TOKEN-SECRET" not in json.dumps(call.get("params") or {})
        assert call["headers"]["Authorization"] == "Bearer TOKEN-SECRET"

    def test_repr_does_not_leak_the_token(self, client_factory):
        client, _ = client_factory([])
        assert "TOKEN-SECRET" not in repr(client)

    def test_refresh_returns_token_and_lifetime(self, client_factory):
        client, _ = client_factory(
            [FakeResponse({"access_token": "NEW-TOKEN", "expires_in": 5184000})]
        )
        token, expires_in = client.refresh_access_token()

        assert token == "NEW-TOKEN"
        assert expires_in == 5184000

    def test_refresh_updates_the_token_used_for_later_calls(self, client_factory):
        client, session = client_factory(
            [
                FakeResponse({"access_token": "NEW-TOKEN", "expires_in": 5184000}),
                FakeResponse({"id": "container-1"}),
            ]
        )
        client.refresh_access_token()
        client.create_image_container("https://example.com/a.jpg")

        assert session.last["headers"]["Authorization"] == "Bearer NEW-TOKEN"

    def test_malformed_refresh_response_is_an_error(self, client_factory):
        client, _ = client_factory([FakeResponse({"access_token": "x"})])
        with pytest.raises(InstagramError, match="Malformed refresh"):
            client.refresh_access_token()


class TestSingleImage:
    def test_sends_caption_and_url(self, client_factory):
        client, session = client_factory([FakeResponse({"id": "container-1"})])

        container = client.create_image_container(
            "https://example.com/a.jpg", caption="Hello", alt_text="A photo"
        )

        assert container == "container-1"
        data = session.last["data"]
        assert data["image_url"] == "https://example.com/a.jpg"
        assert data["caption"] == "Hello"
        assert data["alt_text"] == "A photo"
        assert "is_carousel_item" not in data

    def test_omits_empty_optional_parameters(self, client_factory):
        client, session = client_factory([FakeResponse({"id": "c1"})])
        client.create_image_container("https://example.com/a.jpg")

        data = session.last["data"]
        assert "caption" not in data
        assert "alt_text" not in data
        assert "location_id" not in data

    def test_user_tags_are_json_encoded(self, client_factory):
        client, session = client_factory([FakeResponse({"id": "c1"})])
        client.create_image_container(
            "https://example.com/a.jpg",
            user_tags=[{"username": "someone", "x": 0.5, "y": 0.4}],
        )

        assert json.loads(session.last["data"]["user_tags"]) == [
            {"username": "someone", "x": 0.5, "y": 0.4}
        ]

    def test_missing_container_id_is_an_error(self, client_factory):
        client, _ = client_factory([FakeResponse({})])
        with pytest.raises(InstagramError, match="no container id"):
            client.create_image_container("https://example.com/a.jpg")


class TestCarousel:
    def test_children_are_marked_and_carry_no_caption(self, client_factory):
        client, session = client_factory([FakeResponse({"id": "child-1"})])

        client.create_image_container(
            "https://example.com/a.jpg", caption="ignored", is_carousel_item=True
        )

        data = session.last["data"]
        assert data["is_carousel_item"] == "true"
        # Instagram ignores captions on children; sending one invites confusion
        # about which caption wins.
        assert "caption" not in data
        assert "alt_text" not in data

    def test_parent_preserves_child_order(self, client_factory):
        client, session = client_factory([FakeResponse({"id": "parent-1"})])

        client.create_carousel_container(
            ["child-1", "child-2", "child-3"], caption="Three photos"
        )

        data = session.last["data"]
        assert data["media_type"] == "CAROUSEL"
        # Order decides display order, and the first child sets the aspect
        # ratio every other item is cropped to.
        assert data["children"] == "child-1,child-2,child-3"
        assert data["caption"] == "Three photos"

    @pytest.mark.parametrize("count", [0, 1, 11])
    def test_rejects_invalid_child_counts(self, client_factory, count):
        client, _ = client_factory([])
        with pytest.raises(ValueError, match="between 2 and 10"):
            client.create_carousel_container([f"c{i}" for i in range(count)])


class TestReel:
    def test_sends_reel_parameters(self, client_factory):
        client, session = client_factory([FakeResponse({"id": "reel-1"})])

        client.create_reel_container(
            "https://example.com/v.mp4",
            caption="A reel",
            cover_url="https://example.com/cover.jpg",
        )

        data = session.last["data"]
        assert data["media_type"] == "REELS"
        assert data["video_url"] == "https://example.com/v.mp4"
        assert data["cover_url"] == "https://example.com/cover.jpg"

    def test_share_to_feed_is_spelled_correctly(self, client_factory):
        # The previous system sent "shate_to_feed" for years, so this parameter
        # never once took effect.
        client, session = client_factory([FakeResponse({"id": "reel-1"})])
        client.create_reel_container("https://example.com/v.mp4", share_to_feed=False)

        assert session.last["data"]["share_to_feed"] == "false"
        assert "shate_to_feed" not in session.last["data"]

    def test_user_tags_drop_coordinates(self, client_factory):
        # Reels accept usernames only; coordinates are an image concept.
        client, session = client_factory([FakeResponse({"id": "reel-1"})])
        client.create_reel_container(
            "https://example.com/v.mp4",
            user_tags=[{"username": "someone", "x": 0.5, "y": 0.5}],
        )

        assert json.loads(session.last["data"]["user_tags"]) == [{"username": "someone"}]


class TestStory:
    def test_image_story(self, client_factory):
        client, session = client_factory([FakeResponse({"id": "story-1"})])
        client.create_story_container(image_url="https://example.com/a.jpg")

        data = session.last["data"]
        assert data["media_type"] == "STORIES"
        assert data["image_url"] == "https://example.com/a.jpg"

    def test_requires_exactly_one_media_url(self, client_factory):
        client, _ = client_factory([])
        with pytest.raises(ValueError, match="exactly one"):
            client.create_story_container()
        with pytest.raises(ValueError, match="exactly one"):
            client.create_story_container(image_url="a", video_url="b")


class TestPolling:
    def test_finished_immediately_does_not_sleep(self, client_factory, slept):
        client, _ = client_factory([FakeResponse({"status_code": "FINISHED"})])
        client.wait_until_ready("container-1")
        assert slept == []

    def test_polls_until_finished(self, client_factory, slept):
        client, session = client_factory(
            [
                FakeResponse({"status_code": "IN_PROGRESS"}),
                FakeResponse({"status_code": "IN_PROGRESS"}),
                FakeResponse({"status_code": "FINISHED"}),
            ]
        )
        client.wait_until_ready("container-1")

        assert len(session.calls) == 3
        assert len(slept) == 2

    def test_backs_off_between_polls(self, client_factory, slept):
        client, _ = client_factory(
            [FakeResponse({"status_code": "IN_PROGRESS"})] * 4
            + [FakeResponse({"status_code": "FINISHED"})]
        )
        client.wait_until_ready("container-1")

        assert slept == sorted(slept)
        assert slept[-1] > slept[0]

    @pytest.mark.parametrize("status", ["ERROR", "EXPIRED"])
    def test_terminal_states_fail_immediately(self, client_factory, slept, status):
        client, session = client_factory([FakeResponse({"status_code": status})])

        with pytest.raises(InstagramError) as exc:
            client.wait_until_ready("container-1")

        assert exc.value.disposition is Disposition.FAIL_POST
        assert len(session.calls) == 1
        assert slept == []

    def test_already_published_is_not_an_error(self, client_factory):
        # A previous run published but died before recording it.
        client, _ = client_factory([FakeResponse({"status_code": "PUBLISHED"})])
        client.wait_until_ready("container-1")

    def test_timeout_is_retryable_not_a_post_failure(self, client_factory):
        client, _ = client_factory(
            [FakeResponse({"status_code": "IN_PROGRESS"})] * 50,
            now=[0, 0, 100, 200, 400, 901, 902, 903],
        )

        with pytest.raises(MediaNotReady) as exc:
            client.wait_until_ready("container-1", timeout=900)

        # A slow video is not the user's fault; the next run retries.
        assert exc.value.disposition is Disposition.RETRY_LATER

    def test_unknown_status_is_an_error(self, client_factory):
        client, _ = client_factory([FakeResponse({"status_code": "SOMETHING_NEW"})])
        with pytest.raises(InstagramError, match="Unrecognised container status"):
            client.container_status("container-1")


class TestPublish:
    def test_publishes_by_container_id(self, client_factory):
        client, session = client_factory([FakeResponse({"id": "media-99"})])

        published = client.publish("container-1")

        assert published.id == "media-99"
        assert session.last["data"]["creation_id"] == "container-1"
        assert session.last["url"].endswith("/media_publish")

    def test_missing_media_id_is_an_error(self, client_factory):
        client, _ = client_factory([FakeResponse({})])
        with pytest.raises(InstagramError, match="no media id"):
            client.publish("container-1")


class TestErrorClassification:
    def test_expired_token_is_an_auth_problem(self, client_factory):
        client, _ = client_factory(
            [
                FakeResponse(
                    {
                        "error": {
                            "message": "Error validating access token: Session has expired",
                            "type": "OAuthException",
                            "code": 190,
                            "fbtrace_id": "ABC123",
                        }
                    },
                    status_code=400,
                )
            ]
        )

        with pytest.raises(InstagramError) as exc:
            client.whoami()

        assert exc.value.disposition is Disposition.AUTH
        assert exc.value.code == 190
        assert exc.value.fbtrace_id == "ABC123"

    @pytest.mark.parametrize("code", [4, 17, 32, 613])
    def test_rate_limits_are_retryable(self, client_factory, code):
        client, _ = client_factory(
            [FakeResponse({"error": {"message": "limit", "code": code}}, status_code=400)]
        )
        with pytest.raises(InstagramError) as exc:
            client.whoami()
        assert exc.value.disposition is Disposition.RETRY_LATER

    def test_server_errors_are_retryable(self, client_factory):
        client, _ = client_factory([FakeResponse({}, status_code=503)])
        with pytest.raises(InstagramError) as exc:
            client.whoami()
        assert exc.value.disposition is Disposition.RETRY_LATER

    def test_media_errors_fail_the_post(self, client_factory):
        # The 2207xxx family covers unfetchable URLs, bad formats and aspect
        # ratios — all things the user must fix.
        client, _ = client_factory(
            [
                FakeResponse(
                    {
                        "error": {
                            "message": "The image is not a valid format",
                            "code": 100,
                            "error_subcode": 2207009,
                        }
                    },
                    status_code=400,
                )
            ]
        )
        with pytest.raises(InstagramError) as exc:
            client.create_image_container("https://example.com/a.png")

        assert exc.value.disposition is Disposition.FAIL_POST
        assert exc.value.subcode == 2207009

    def test_error_string_includes_diagnostics(self):
        error = InstagramError(
            "Something failed", code=100, subcode=2207009, fbtrace_id="XYZ"
        )
        rendered = str(error)
        assert "code 100" in rendered
        assert "subcode 2207009" in rendered
        assert "XYZ" in rendered

    def test_non_json_response_still_raises_usefully(self, client_factory):
        client, _ = client_factory([FakeResponse(None, status_code=502)])
        with pytest.raises(InstagramError) as exc:
            client.whoami()
        assert exc.value.http_status == 502
        assert exc.value.disposition is Disposition.RETRY_LATER


class TestApiVersion:
    def test_version_appears_in_the_url(self, client_factory):
        client, session = client_factory([FakeResponse({"id": "c1"})])
        client.create_image_container("https://example.com/a.jpg")
        assert "/v25.0/" in session.last["url"]

    def test_version_is_configurable(self):
        session = FakeSession([FakeResponse({"id": "c1"})])
        client = InstagramClient("t", "user", api_version="v26.0", session=session)
        client.create_image_container("https://example.com/a.jpg")
        assert "/v26.0/" in session.last["url"]

    def test_uses_the_instagram_login_host(self, client_factory):
        client, session = client_factory([FakeResponse({"id": "c1"})])
        client.create_image_container("https://example.com/a.jpg")
        # graph.instagram.com, not graph.facebook.com: no linked Page needed.
        assert session.last["url"].startswith("https://graph.instagram.com/")
