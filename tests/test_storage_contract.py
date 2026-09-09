"""The contract every storage backend must satisfy.

Runs against LocalStorage always. Drive and the object stores run the *same*
suite when credentials are present in the environment, which is what stops the
backends drifting apart behaviourally:

    AUTOGRAM_TEST_GDRIVE='gdrive://id:secret:token@folder'  pytest
    AUTOGRAM_TEST_OBJECTSTORE='gs://key:secret@bucket/test' pytest

These are deliberately not mocked. A mocked Drive client proves only that the
mock behaves as written, and every real problem here — duplicate names, missing
paths, folder moves — lives in the API's behaviour rather than in our calls.
"""

from __future__ import annotations

import os
import uuid

import pytest

from autogram.storage.local import LocalStorage


def _live_backend(env_var: str):
    dsn = os.getenv(env_var)
    if not dsn:
        return None
    from autogram.storage.factory import from_dsn

    # Isolate each run so a failure never touches real content.
    return from_dsn(f"{dsn.rstrip('/')}/test-{uuid.uuid4().hex[:8]}")


@pytest.fixture(
    params=[
        pytest.param("local", id="local"),
        pytest.param("moto", id="objectstore-moto"),
        pytest.param("gdrive", id="gdrive-live"),
        pytest.param("objectstore", id="objectstore-live"),
    ]
)
def storage(request, tmp_path):
    if request.param == "local":
        yield LocalStorage(tmp_path)
        return

    if request.param == "moto":
        # A real S3 implementation in-process, so the ObjectStore code paths
        # are genuinely exercised rather than mocked away.
        moto = pytest.importorskip("moto")
        import boto3

        from autogram.storage.objectstore import ObjectStore

        with moto.mock_aws():
            boto3.client("s3", region_name="us-east-1").create_bucket(
                Bucket="test-bucket"
            )
            yield ObjectStore(
                access_key="testing",
                secret_key="testing",
                bucket="test-bucket",
                prefix="autogram",
                region="us-east-1",
            )
        return

    env_var = f"AUTOGRAM_TEST_{request.param.upper()}"
    backend = _live_backend(env_var)
    if backend is None:
        pytest.skip(f"{env_var} not set")
    yield backend
    try:
        backend.delete("")
    except Exception:  # cleanup is best-effort
        pass


class TestStorageContract:
    def test_write_then_read(self, storage):
        storage.write("queue/post-a/01.jpg", b"image-bytes")
        assert storage.read("queue/post-a/01.jpg") == b"image-bytes"

    def test_write_creates_missing_folders(self, storage):
        storage.write("deeply/nested/path/file.txt", b"x")
        assert storage.read("deeply/nested/path/file.txt") == b"x"

    def test_write_overwrites_rather_than_duplicating(self, storage):
        storage.write("queue/post-a/post.md", b"first")
        storage.write("queue/post-a/post.md", b"second")
        assert storage.read("queue/post-a/post.md") == b"second"
        files = [e for e in storage.list("queue/post-a") if not e.is_dir]
        assert len(files) == 1

    def test_read_missing_raises_file_not_found(self, storage):
        with pytest.raises(FileNotFoundError):
            storage.read("queue/nope/missing.jpg")

    def test_list_missing_returns_empty(self, storage):
        assert storage.list("does/not/exist") == []

    def test_list_returns_sorted_children(self, storage):
        for name in ("03.jpg", "01.jpg", "02.jpg"):
            storage.write(f"queue/post-a/{name}", b"x")

        entries = storage.list("queue/post-a")
        assert [e.path.rsplit("/", 1)[-1] for e in entries] == [
            "01.jpg",
            "02.jpg",
            "03.jpg",
        ]
        assert all(not e.is_dir for e in entries)

    def test_list_distinguishes_folders_from_files(self, storage):
        storage.write("queue/post-a/01.jpg", b"x")
        storage.write("queue/loose.txt", b"x")

        by_name = {e.path.rsplit("/", 1)[-1]: e for e in storage.list("queue")}
        assert by_name["post-a"].is_dir
        assert not by_name["loose.txt"].is_dir

    def test_move_file(self, storage):
        storage.write("queue/post-a/01.jpg", b"data")
        storage.move("queue/post-a/01.jpg", "published/post-a/01.jpg")

        assert storage.read("published/post-a/01.jpg") == b"data"
        with pytest.raises(FileNotFoundError):
            storage.read("queue/post-a/01.jpg")

    def test_move_folder_takes_contents(self, storage):
        # This is how a published post is retired, so it has to work.
        storage.write("queue/post-a/01.jpg", b"one")
        storage.write("queue/post-a/post.md", b"caption")

        storage.move("queue/post-a", "published/post-a")

        assert storage.read("published/post-a/01.jpg") == b"one"
        assert storage.read("published/post-a/post.md") == b"caption"
        assert storage.list("queue/post-a") == []

    def test_move_missing_raises_file_not_found(self, storage):
        with pytest.raises(FileNotFoundError):
            storage.move("queue/nope", "published/nope")

    def test_delete_file(self, storage):
        storage.write("queue/post-a/01.jpg", b"x")
        storage.delete("queue/post-a/01.jpg")
        with pytest.raises(FileNotFoundError):
            storage.read("queue/post-a/01.jpg")

    def test_delete_folder_removes_contents(self, storage):
        storage.write("staging/post-a/01.jpg", b"x")
        storage.write("staging/post-a/02.jpg", b"x")
        storage.delete("staging/post-a")
        assert storage.list("staging/post-a") == []

    def test_delete_missing_is_silent(self, storage):
        storage.delete("never/existed")  # must not raise

    def test_binary_round_trip(self, storage):
        payload = bytes(range(256)) * 16
        storage.write("queue/post-a/photo.jpg", payload)
        assert storage.read("queue/post-a/photo.jpg") == payload


class TestLocalStorageSpecifics:
    def test_refuses_to_escape_root(self, tmp_path):
        storage = LocalStorage(tmp_path)
        with pytest.raises(ValueError, match="escapes storage root"):
            storage.read("../../etc/passwd")

    def test_cannot_serve_media(self, tmp_path):
        from datetime import timedelta

        storage = LocalStorage(tmp_path)
        assert not storage.can_serve
        with pytest.raises(NotImplementedError):
            storage.fetchable_url("x.jpg", timedelta(hours=1))
