"""Composed interactive diskmgr shell."""

import cmd

from .shell_core import ShellCoreMixin
from .shell_helpers import ShellHelpersMixin
from .commands.boot import BootCommands
from .commands.destructive import DestructiveCommands
from .commands.entropy import EntropyCommands
from .commands.filesystem import FilesystemCommands
from .commands.health import HealthCommands
from .commands.luks import LuksCommands
from .commands.mapping import MappingCommands
from .commands.mounting import MountingCommands
from .commands.partition import PartitionCommands
from .commands.listing import ListingCommands
from .commands.transfer import TransferCommands


class DiskMgrShell(
    ShellCoreMixin,
    ShellHelpersMixin,
    ListingCommands,
    BootCommands,
    MappingCommands,
    MountingCommands,
    LuksCommands,
    DestructiveCommands,
    EntropyCommands,
    HealthCommands,
    TransferCommands,
    FilesystemCommands,
    PartitionCommands,
    cmd.Cmd,
):
    """Interactive command shell assembled from workflow-focused mixins."""
