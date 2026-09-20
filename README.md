# safemv

Verified file copy and move for macOS and Linux.

`safemv` copies files with `rsync`, independently verifies the destination with SHA-256, and removes source files in `mv` mode only after the required checks and remount have succeeded. It saves a transfer plan and an event log automatically. Existing files are never overwritten, and existing directories are never merged.

## Requirements

- Python 3.9 or later and an installed `rsync`. No additional Python packages are required at runtime.
- macOS: `diskutil` and `ls`. Writing to NTFS requires a compatible driver. The system-provided `openrsync` is supported.
- Linux: util-linux (`findmnt`, `lsblk`, `mount`, and `umount`). UDisks mode also requires `udisksctl`, the UDisks service, and appropriate polkit permissions.

The test suite contains 74 tests and has passed on macOS using the installed `openrsync`. Volume discovery and remount operations are mocked in tests. Native Linux execution and compatibility with all external filesystem drivers have not been verified. The project is currently alpha software.

## Installation and usage

Run the script directly from a checkout:

```bash
python3 safemv.py cp SOURCE DEST
python3 safemv.py mv SOURCE DEST
```

To install the `safemv` command in a virtual environment:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/safemv --help
```

Once the command is available on your PATH:

```bash
safemv cp SOURCE DEST
safemv mv SOURCE DEST
safemv cp SOURCE1 SOURCE2 DEST_DIRECTORY
```

`cp` retains the source. `mv` automatically removes the specified sources after verification and, when required, a successful remount. There is no additional deletion prompt: selecting `mv` selects that behavior. Remounting an external volume always requires confirmation.

## Source and destination paths

Operands follow the familiar `cp`/`mv` order:

| Destination | Result |
|---|---|
| An existing directory | Places the source inside it under the source's original name. |
| A nonexistent path with one source | Uses that path as the new file or directory name. Its parent must exist. |
| Multiple sources | The final operand must be an existing directory. |
| The resulting file or directory already exists | Stops without overwriting or merging. |

Regular files, nested directories, empty directories, and hidden files are supported. Directories are always processed recursively; `-r` and `-R` are accepted but optional. This is not a complete implementation of the system `cp` and `mv` options.

Use `SOURCE/.` to copy a directory's contents into an existing directory. For `cp` on macOS, `SOURCE/` has the same meaning, matching BSD `cp -R`. On Linux, a trailing `/` retains the source directory's name. In `mv` mode, a trailing `/` does not remove the source directory's name; `.` and `..` operands are rejected. Quote paths containing spaces. Use `--` before relative paths that start with `-`.

## Transfer sequence

1. Hash the source files with SHA-256 and save a JSONL plan containing paths, sizes, file state, and destination volume identity.
2. Check the sources and destination volume, copy the files with `rsync`, then run `fsync` and `sync`.
3. **Read each destination file once and verify its SHA-256 before remounting.** A verification failure stops the transfer before any source removal.
4. For an external volume, request confirmation, unmount it, and mount it again. Skip this step for an internal volume.
5. Check the volume identity and file state. Destination file contents are not read again after remounting.
6. In `mv` mode only, recheck the sources and supported metadata, remove the verified source files listed in the plan, and remove the directories once they are empty.

Rsync runs separately for each file with `--whole-file --ignore-existing --partial`. A skipped existing file is not accepted as a successful copy. Rsync deletion options are not used; only the final `mv` stage removes sources.

Multiple sources are processed as separate, sequential jobs. If a later job fails, earlier jobs may already have completed, including source removal in `mv` mode. There is no transaction spanning all jobs and no automatic rollback.

## Remount confirmation

The prompt identifies the mount path, UUID, and device:

```text
Confirm remount of volume '/Volumes/External' (UUID ..., device ...)? [y/N]:
```

Only `y`, case-insensitively, permits the operation. `n`, an empty response, or EOF stops the job; copies are retained and sources are not removed. Close other applications using the volume: the **entire destination volume** is briefly unavailable during this step. Forced unmounts, ejects, and power cycling are not used.

On macOS, the script uses `diskutil mount DEVICE` and verifies the original mount path and UUID afterward. On Linux, the default is `--linux-remount system`; use `--linux-remount udisks` for volumes managed by UDisks. `--sudo-remount` explicitly enables `sudo -n` only for the Linux system mount and unmount commands. The necessary privileges must already be available. You do not need to run the entire script with sudo for this purpose.

If mounting fails after a successful unmount, the volume may remain unmounted. The recovery command is recorded in the `unmount_start` log event. Verify the device and resolve the error before running that command separately.

## Plans and logs

The default state directory remains compatible with earlier versions:

```text
~/.local/state/safe-transfer/<timestamp>-<id>/plan.jsonl
~/.local/state/safe-transfer/<timestamp>-<id>/plan.log
```

`--state-dir PATH` changes the job directory. It must be outside all transferred trees and off the external destination volume. The optional `--log PATH` overrides the log path for a single job. By default, the plan's final filename extension is replaced with `.log`. Existing plans and logs are never overwritten. They contain absolute paths; review their contents before sharing them.

Separate planning, execution, and verification are also available:

```bash
safemv plan --source SOURCE --destination NEW_PATH --manifest /local/path/plan.jsonl
safemv run --manifest /local/path/plan.jsonl
safemv verify --manifest /local/path/plan.jsonl --log /local/path/verify-01.log
```

For `plan`, the destination is the exact new path; the source's name is not appended. Passing a plan to `run` means you have reviewed it. `run` copies, verifies, and remounts when required, but never removes sources. `verify` checks an existing destination and creates a log without remounting or removing files.

The `content_verified` event marks completion of the full SHA-256 pass. The final `verified` event marks successful completion of `cp`, `run`, or `verify`. A completed `mv` requires the final `moved` event. Transfer logs explicitly record `checksum_verification_phase: before_remount` and `content_rechecked_after_remount: false`.

## Limitations and failure handling

Do not modify the source or destination while a transfer is running. These checks do not replace a filesystem snapshot or exclusive access. After remounting, the script checks each object's type, size, mtime, ctime, inode, and device. Content changes that leave those fields unchanged may go undetected. If a driver changes those fields during remounting, the job stops. Remounting does not prove durability after power loss or guarantee that controller caches have been bypassed.

`cp` verifies the main data stream; complete metadata preservation is not guaranteed. `mv` also copies and verifies accessible extended attributes, including macOS resource forks. If the destination cannot preserve them, source removal is blocked. Basic permissions and timestamps are set, but ownership, filesystem flags, and arbitrary NTFS alternate data streams are not covered by the transfer guarantee.

The following are not supported:

- Symbolic links, special files, and nested source filesystems.
- Extended ACLs and files with multiple hard links in `mv` mode.
- Transferring an entire filesystem root or home directory.
- Names that conflict under case folding or Unicode normalization.
- Network destinations; Linux LVM, RAID, crypt, loop, bind mounts, and Btrfs subvolumes; virtual external volumes on macOS.
- Active databases, virtual machines, application media libraries, and system backups.

The operation is rejected if the script cannot determine whether the destination volume is internal or external. A source on the same external volume as the destination is rejected because remounting would interrupt access to it.

A failure before source removal leaves all sources in place. A failure during removal may leave a partially removed source tree; the verified copy is retained. Compare the log with the actual files: a crash between deletion and its log event can leave the log incomplete. There is no automatic cleanup, resume, or rollback. Use the verified copy and plan for recovery; repeating `mv` into the same destination does not resume the previous job.

Exit codes: `0` for success, `3` for a checksum mismatch detected while hashing, `2` for other errors or declined confirmation, and `130` for a keyboard interrupt.

## Development checks

```bash
python3 -B tests/test_safemv.py
```

Tests create new temporary files, perform real copies with rsync, and remove only source files created by the tests. External volumes and remounts are mocked, as is the system-wide `sync` call. Remaining test artifacts are retained, and their path is printed at the start of the test output. No external drive is required.

## License

[MIT](LICENSE). Copyright (c) 2026 Alexey Shatlovsky.
