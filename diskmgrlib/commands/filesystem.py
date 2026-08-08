"""Filesystem command compatibility composition.

Maintenance and provisioning implementations are kept in separate modules,
while this class preserves the historical command mixin import surface.
"""

from .filesystem_maintenance import FilesystemMaintenanceCommands
from .provisioning import ProvisioningCommands


class FilesystemCommands(FilesystemMaintenanceCommands, ProvisioningCommands):
    """Expose filesystem maintenance and provisioning commands."""


__all__ = ["FilesystemCommands"]
