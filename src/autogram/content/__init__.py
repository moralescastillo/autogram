"""Post discovery, parsing and validation.

A folder is a post. Validation runs fully before anything is uploaded, so a
malformed post costs nothing and explains itself (DESIGN.md §5.4).
"""

from autogram.content.post import Post, PostType, UserTag

__all__ = ["Post", "PostType", "UserTag"]
