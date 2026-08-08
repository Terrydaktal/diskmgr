"""Composed interactive diskmgr shell."""

import cmd

from .shell_core import ShellCoreMixin
from .shell_helpers import ShellHelpersMixin
from .commands.boot import BootCommands
from .commands.block import BlockCommands
from .commands.entropy import EntropyCommands
from .commands.filesystem import FilesystemCommands
from .commands.health import HealthCommands
from .commands.luks import LuksCommands
from .commands.mapping import MappingCommands
from .commands.mounting import MountingCommands
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
    BlockCommands,
    EntropyCommands,
    HealthCommands,
    TransferCommands,
    FilesystemCommands,
    cmd.Cmd,
):
    """Interactive command shell assembled from workflow-focused mixins."""
