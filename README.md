# safemv

Verified file copy and move for macOS and Linux.

`safemv` copies files with `rsync`, independently verifies the destination with SHA-256, and removes source files in `mv` mode only after the required checks and remount have succeeded. The `rm` command verifies existing copies before removing originals with confirmation. It saves a transfer plan and an event log automatically. Existing files are never overwritten, and existing directories are never merged.

## Requirements

- Python 3.9 or later and an installed `rsync`. No additional Python packages are required at runtime.
- macOS: `diskutil` and `ls`. Writing to NTFS requires a compatible driver. The system-provided `openrsync` is supported.
- Linux: util-linux (`findmnt`, `lsblk`, `mount`, and `umount`). UDisks mode also requires `udisksctl`, the UDisks service, and appropriate polkit permissions.

The test suite contains 112 tests and has passed on macOS using the installed `openrsync`. Volume discovery and remount operations are mocked in tests. Native Linux execution and compatibility with all external filesystem drivers have not been verified. The project is currently alpha software.

## Installation and usage

Run the script directly from a checkout:

```bash
python3 safemv.py cp SOURCE DEST
python3 safemv.py mv SOURCE DEST
python3 safemv.py rm SOURCE DEST
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
safemv rm SOURCE DEST
safemv cp SOURCE1 SOURCE2 DEST_DIRECTORY
```

`cp` retains the source. `mv` automatically removes the specified sources after verification and, when required, a successful remount. There is no additional deletion prompt: selecting `mv` selects that behavior. Remounting an external volume always requires confirmation.

`rm` checks existing copies and removes originals after a separate confirmation. It accepts the same operand order as `mv`, creates its plan automatically, and performs no copying or remounting. See [Remove originals after an existing copy](#remove-originals-after-an-existing-copy).

## Source and destination paths

For copying and moving, operands follow the familiar `cp`/`mv` order. The `rm` command uses the same directory/basename mapping but requires the resulting copies to exist:

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
5. Check the volume identity and file state. Only a change in the mount-local device number (`st_dev`) is accepted across remounting, after confirming that every object belongs to the expected volume. Save the accepted state for strict checks before source removal. Destination file contents are not read again after remounting.
6. In `mv` mode only, recheck the sources and supported metadata, remove the verified source files listed in the plan, and remove the directories once they are empty.

Rsync runs separately for each file with `--whole-file --ignore-existing --partial`. A skipped existing file is not accepted as a successful copy. Rsync deletion options are not used; only the final `mv` stage removes sources.

On macOS with the `ufsd_NTFS` driver, new destination modification times are rounded down to whole seconds before checksum verification. This avoids a cached fractional timestamp being lost at remount. The policy applies to copied files and directories, including directory times restored from the source in `mv` mode. Source timestamps are not rounded; fractional modification times are not preserved in the copies. Other drivers retain the original timestamp-setting behavior. Timestamp comparisons after verification remain exact, and standalone `verify` never changes timestamps. The policy is printed and recorded as `destination_timestamp_policy` in the log.

Multiple sources are processed as separate, sequential jobs. If a later job fails, earlier jobs may already have completed, including source removal in `mv` or `rm` mode. There is no transaction spanning all jobs and no automatic rollback.

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

The checksum pass records file state in `file_verified` and directory state in `destination_snapshot`. Subsequent `destination_state_checked` events record the check phase, expected and actual state, each differing field with its before/after values, and whether the comparison was accepted. Rejected differences also appear in the terminal error. Missing or inaccessible paths and symlink components are recorded as `destination_state_unavailable` during tree checks. A state-check failure does not by itself mean that file contents failed SHA-256 verification.

## Remove originals after an existing copy

Use `rm` with the same source and destination operands as `mv` when all copies already exist:

```bash
safemv rm SOURCE DESTINATION
safemv rm SOURCE1 SOURCE2 DESTINATION_DIRECTORY
```

If `DESTINATION` is an existing directory, each copy must be at `DESTINATION/SOURCE.name`. For example, `safemv rm ./album /media/archive` checks `/media/archive/album`. For a source file, an existing destination file is the exact copied file, even if its name differs. A trailing slash on the source retains its basename, as with `mv`; `.` and `..` operands are refused. The command does not guess another destination when the expected copy is missing.

The command hashes the current sources and automatically creates a plan and log under `~/.local/state/safe-transfer/`; `--state-dir` and `--log` work as in `cp` and `mv`. No prior plan is needed. It does not copy, remount, alter destination timestamps, or rely on an old verification log. It reads every destination file and checks its SHA-256 and size against the new plan, checks the full source tree and source hashes, and verifies the volume identity and supported move metadata. A missing file, checksum mismatch, changed source tree, or missing destination metadata blocks removal of that source's entire batch. Extra destination files are left untouched. Multiple source operands are separate sequential jobs, each with its own checks and confirmation.

An existing plan can still be used instead of positional operands, including after an interrupted transfer:

```bash
safemv rm --manifest /local/path/plan.jsonl
```

This alternative uses the exact source and destination paths recorded in the plan. Previous `mv`, `cp`, and `plan/run` manifests are supported; plans without move metadata capture the current source metadata and require the copies to contain it too. Positional paths and `--manifest` cannot be combined.

After all checks pass, the command displays the source, destination, file count, byte count, and recovery information, then asks:

```text
Confirm removal of these verified originals? [y/N]:
```

Only `y` permits removal. Other input or EOF leaves the originals in place. Sources and destination state are rechecked after confirmation. Only source entries listed in the plan are removed; directories must be empty. Removal bypasses Trash. If removal fails partway through, the remaining sources are retained and the log records partial progress; recover removed files by copying them back from the verified destination. The command requires the complete original source tree and does not resume partial deletion.

Positional operands create a new job with `plan.jsonl` and `plan.log`. With `--manifest`, the default log instead replaces the plan's final extension with `.rm.log`, keeping an existing transfer log intact. Use `--log /local/path/remove-02.log` for a different new log; existing logs are never overwritten. Successful completion prints `REMOVED` and records a final `moved` event in a log whose mode is `rm`. File contents are checked once at the destination, before confirmation; subsequent state checks have the same concurrent-modification limitations as `mv`.

## Limitations and failure handling

Do not modify the source or destination while a transfer is running. These checks do not replace a filesystem snapshot or exclusive access. After remounting, the script checks each object's type, size, mtime, ctime, and inode against the checksum pass. It checks the device number against the current mount, whose UUID, path, filesystem type, and external status must still match the plan. Within a single mount, device numbers must also remain unchanged. Content changes that leave the checked fields unchanged may go undetected. If a driver changes inode, size, or timestamps during remounting, the job still stops; the differences are logged rather than silently ignored. Remounting does not prove durability after power loss or guarantee that controller caches have been bypassed.

Timestamp precision depends on the operating system and filesystem; nanosecond API units do not guarantee nanosecond storage precision. See the [Python timestamp documentation](https://docs.python.org/3/library/os.html#os.utime).

`cp` verifies the main data stream; complete metadata preservation is not guaranteed. `mv` also copies and verifies accessible extended attributes, including macOS resource forks. If the destination cannot preserve them, source removal is blocked. Basic permissions and timestamps are set, but ownership, filesystem flags, and arbitrary NTFS alternate data streams are not covered by the transfer guarantee.

On macOS, `com.apple.provenance` is treated separately: the operating system associates it with the application creating or modifying an object, so source and copy values may differ. `mv` leaves the destination's provenance value to macOS instead of writing the source value. `mv` and `rm` record differences in `destination_metadata_difference` events without rejecting the copy solely for that attribute. They do not delete or rewrite provenance attributes or change Gatekeeper settings. Source metadata must still match its snapshot. All other expected attributes, including resource forks, Finder information, quarantine and access-related attributes, remain strict; failures identify the attribute names and expected/actual hashes. This exception applies only to macOS. See [FFRI's provenance research](https://github.com/FFRI/ShowProvenanceInfo).

The following are not supported:

- Symbolic links, special files, and nested source filesystems.
- Extended ACLs and files with multiple hard links in `mv` and `rm` modes.
- Transferring an entire filesystem root or home directory.
- Names that conflict under case folding or Unicode normalization.
- Network destinations; Linux LVM, RAID, crypt, loop, bind mounts, and Btrfs subvolumes; virtual external volumes on macOS.
- Active databases, virtual machines, application media libraries, and system backups.

The operation is rejected if the script cannot determine whether the destination volume is internal or external. Copying or moving a source on the same external volume as the destination is rejected because remounting would interrupt access to it. The `rm` command does not remount and can check separate source and destination trees on the same volume.

A failure before source removal leaves all sources in place. A failure during removal may leave a partially removed source tree; the verified copy is retained. Compare the log with the actual files: a crash between deletion and its log event can leave the log incomplete. There is no automatic cleanup, resume, or rollback. Use the verified copy and plan for recovery; repeating `mv` into the same destination does not resume the previous job.

Exit codes: `0` for success, `3` for a checksum mismatch detected while hashing, `2` for other errors or declined confirmation, and `130` for a keyboard interrupt.

## Development checks

```bash
python3 -B tests/test_safemv.py
```

Tests create new temporary files, perform real copies with rsync, and remove only source files created by the tests. External volumes and remounts are mocked, as is the system-wide `sync` call. Remaining test artifacts are retained, and their path is printed at the start of the test output. No external drive is required.

## License

[MIT](LICENSE). Copyright (c) 2026 Alexey Shatlovsky.
