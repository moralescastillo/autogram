"""Post discovery, parsing and validation.

A folder is a post. Validation runs fully before anything is uploaded, so a
malformed post costs nothing and explains itself (DESIGN.md §5.4).
"""

from autogram.content.aspect import Aspect
from autogram.content.media import MediaItem, MediaKind, probe
from autogram.content.post import Post, PostError, PostType, UserTag, discover
from autogram.content.validate import ValidationResult, load_media, validate

__all__ = [
    "Aspect",
    "MediaItem",
    "MediaKind",
    "Post",
    "PostError",
    "PostType",
    "UserTag",
    "ValidationResult",
    "discover",
    "load_media",
    "probe",
    "validate",
]
