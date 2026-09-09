"""Everything imports and the CLI is wired up."""

import pytest

from autogram.cli import build_parser
from autogram.storage import Storage


def test_modules_import():
    import autogram.content  # noqa: F401
    import autogram.instagram  # noqa: F401
    import autogram.scheduling  # noqa: F401
    import autogram.state  # noqa: F401


def test_run_accepts_dry_run():
    args = build_parser().parse_args(["run", "--dry-run"])
    assert args.command == "run"
    assert args.dry_run


def test_command_is_required():
    with pytest.raises(SystemExit):
        build_parser().parse_args([])


def test_authoring_only_backend_reports_it_cannot_serve():
    class Authoring(Storage):
        def list(self, path): return []
        def read(self, path): return b""
        def write(self, path, data): pass
        def move(self, src, dst): pass
        def delete(self, path): pass

    assert not Authoring().can_serve
