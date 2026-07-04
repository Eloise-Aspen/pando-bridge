"""Pando — hookified core for building Claude Code companion bridges."""

from .server import create_app

__version__ = "0.1.0"

__all__ = ["create_app", "__version__"]
