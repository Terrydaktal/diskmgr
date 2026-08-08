"""Compatibility alias for raw block-device operations."""

from .block import BlockCommands


class DestructiveCommands(BlockCommands):
    """Backward-compatible destructive command mixin."""


__all__ = ["DestructiveCommands"]
