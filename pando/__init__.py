"""Pando — hookified core for building Claude Code companion bridges."""

from .server import create_app

# semver：0.x 阶段 API 仍可能变动，稳定后再 bump 到 1.0
__version__ = "0.1.0"

__all__ = ["create_app", "__version__"]
