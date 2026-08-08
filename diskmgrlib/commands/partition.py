"""Compatibility alias for partition commands now owned by provisioning."""

from .provisioning import ProvisioningCommands


class PartitionCommands(ProvisioningCommands):
    """Backward-compatible partition command mixin."""


__all__ = ["PartitionCommands"]
