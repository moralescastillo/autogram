"""Shared test doubles."""

from __future__ import annotations

from datetime import timedelta

import pytest

from autogram.storage.local import LocalStorage


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


class ServingStorage(LocalStorage):
    """A local backend that can hand out URLs, standing in for GCS."""

    can_serve = True

    def fetchable_url(self, path: str, expires_in: timedelta = timedelta(hours=1)) -> str:
        return f"https://fake-bucket.example/{path}"


@pytest.fixture
def authoring(tmp_path):
    return LocalStorage(tmp_path / "drive")


@pytest.fixture
def serving(tmp_path):
    return ServingStorage(tmp_path / "bucket")
