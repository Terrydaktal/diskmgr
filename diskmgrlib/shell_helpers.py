"""Compatibility composition of the shared helper interfaces.

Command mixins historically call helper methods on ``DiskMgrShell``.  The
implementations now live in responsibility-focused helper modules, while this
composition class preserves that internal interface during the refactor.
"""

from .mount_policy import MountPolicyMixin
from .inventory import InventoryMixin
from .safety import SafetyMixin


class ShellHelpersMixin(MountPolicyMixin, InventoryMixin, SafetyMixin):
    """Compose mount, inventory, and safety helpers for the interactive shell."""


__all__ = ["ShellHelpersMixin"]
