"""MappingCommands command implementations."""

from ..common import *
from ..shell_core import CmdArgumentParser


class MappingCommands:

    def do_map(self, arg):
        '''Create or modify a persistent mapping: map <name/id> <name>

        Usage:
          map [#1] backup    Assigns friendly name to discovery ID (e.g., map #1 backup)
          map 1a backup      Renames an existing mapping (e.g., map 1a backup)

        Note: Raw device paths (e.g., /dev/sdb) are NOT allowed.

        UNDER THE HOOD:
        1.  Input Resolution:
            - discovery ID (e.g., [#1]): Resolves the temporary device to its Persistent Device Path (PDP).
            - mapping name (e.g., 1a): Selects an existing mapping for RENAME operations.
        2.  PDP Linking: Extracts the /dev/disk/by-id/ path for the target hardware.
        3.  Conflict Check: Ensures the new friendly name is not already in use.
        4.  Persistence: Writes the [Name <TAB> PDP] pair to diskmap.tsv.

        This ensures the disk is recognized correctly regardless of USB port or device node changes.
        '''
        args = arg.split()
        if len(args) != 2:
            log("Usage: map <id/name> <new_name>", 'ERROR')
            return

        target, name = args
        self.mappings = read_luks_map() # Refresh

        clean_target = target.strip('[]')
        old_name = None
        real_target = None

        # 1. Rename by current mapping name.
        if target in self.mappings:
            old_name = target
            real_target = self.mappings[target]
            log(f"Renaming mapping {target} -> {name}")
        else:
            # 2. Rename by missing-mapping list ID (#N) from latest `list`.
            if clean_target.startswith('#') and clean_target[1:].isdigit():
                missing_old = self.missing_map_id_cache.get(clean_target[1:])
                if missing_old and missing_old in self.mappings:
                    old_name = missing_old
                    real_target = self.mappings[missing_old]
                    log(f"Renaming missing mapping {missing_old} -> {name}")

        # 3. New map (target is a normal discovery ID/path).
        if real_target is None:
            real_target = self.resolve_target(target)
            if not real_target:
                log(f"Invalid target: '{target}'. Use a Discovery ID (e.g., [#1]) or an existing name.", 'ERROR')
                return
            log(f"Resolved {target} -> {real_target}")

        normalized_target = self._normalize_mapping_target(real_target)
        if normalized_target != real_target:
            log(f"Normalized mapping target: {real_target} -> {normalized_target}")
            real_target = normalized_target

        if name in self.mappings and name != old_name:
            log(f"Mapping '{name}' already exists.", 'ERROR')
            return

        # Collision Prevention: Prevent names that look like IDs
        clean_name = name.strip('[]')
        if (
            (clean_name.startswith('#') and clean_name[1:].isdigit()) or
            (clean_name.startswith('U') and clean_name[1:].isdigit()) or
            clean_name.isdigit()
        ):
            log(f"Invalid name: '{name}'. Names cannot be simple numbers or match discovery ID formats like '#1'.", 'ERROR')
            return

        if old_name and old_name in self.mappings:
            del self.mappings[old_name]
        self.mappings[name] = real_target
        save_luks_map(self.mappings)
        log(f"Mapping saved: {name} -> {real_target}")

    def do_unmap(self, arg):
        '''Remove a persistent mapping: unmap <name/id>

        UNDER THE HOOD:
        1.  Resolution:
            - Name mode: removes the exact mapping name.
            - ID mode (#N): resolves to a device and removes mapping(s) pointing to that device.
        2.  Removal: Deletes the [Name <TAB> PDP] pair(s) from the internal dictionary.
        3.  Persistence: Re-writes diskmap.tsv with the mapping(s) removed.
        '''
        target = arg.strip()
        if not target:
            log("Usage: unmap <name/id>", 'ERROR')
            return

        self.mappings = read_luks_map()
        if target in self.mappings:
            del self.mappings[target]
            save_luks_map(self.mappings)
            log(f"Mapping '{target}' removed successfully.")
            return

        # ID path: resolve target and remove any mapping(s) that point to that same real device.
        resolved = self.resolve_target(target, allow_id=True)
        if not resolved:
            log(f"Unknown target: '{target}'. Use a mapping name or discovery ID (#N).", 'ERROR')
            log("Tip: run 'list' first to refresh discovery IDs.", 'ERROR')
            return

        resolved_real = os.path.realpath(resolved)
        to_remove = []
        for name, path in list(self.mappings.items()):
            try:
                if os.path.realpath(path) == resolved_real:
                    to_remove.append(name)
            except Exception:
                continue

        if not to_remove:
            log(f"No mapping found for target: '{target}' ({resolved_real})", 'ERROR')
            return

        for name in to_remove:
            del self.mappings[name]
        save_luks_map(self.mappings)
        if len(to_remove) == 1:
            log(f"Mapping '{to_remove[0]}' removed successfully.")
        else:
            log(f"Removed {len(to_remove)} mappings for {resolved_real}: {', '.join(to_remove)}")
