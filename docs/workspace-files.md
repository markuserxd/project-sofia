# Sandboxed workspace file operations

SOFIA-010 adds explicitly registered local file tools. Nothing discovers roots,
registers tools, grants permission, or reads files during default CLI startup.
The implementation uses the standard library and the existing ToolRegistry,
RunGuardrails, PermissionPolicy, cancellation, accounting, and audit boundary.

## Supported security model

Use a dedicated, trusted local directory containing only data intended for SOFIA.
Model paths and file contents are untrusted data, never instructions or authority.
Every operation requires a configured root and exact action/path permissions.
Traversal, link following, special files, hard-linked files, and unknown Windows
reparse tags are rejected. Root identity is checked on each invocation.

**Mutations require `WorkspaceConfig(..., exclusive_access=True)`.** This is an
explicit assertion by trusted controller code that it owns the workspace and
excludes other writers for the entire mutation invocation, including its parent
directories and temporary files. It is not a lock, an ACL installer, a permission
grant, or a way to make a shared directory safe. The default is read-only; commit
tools return `exclusive_access_required`. If the controller cannot establish
exclusive local access, it must leave mutations disabled. Changes between preview
and invocation are allowed but invalidate the proposal when its version differs.

Do not use these tools as an operating-system isolation boundary against malicious
same-user processes, administrators, concurrent mount/drive remapping, filesystem
filter drivers, or malicious trusted Python callbacks. Shared/synced directories,
hostile concurrent writers, network filesystems, custom security labels/SACL
requirements, and general-purpose metadata-preserving editing are unsupported.
Read-only operation also assumes trustworthy local mount/namespace configuration.

## Public controller API and registration

```python
from decimal import Decimal
from pathlib import Path
from sofia.security import Action, RunGuardrails
from sofia.tools import ToolRegistry, WorkspaceConfig, WorkspaceFiles

run = RunGuardrails(max_steps=20, max_tokens=0, max_usd=Decimal(0))
registry = ToolRegistry(run=run)
# This must be an existing dedicated directory chosen by trusted application code.
files = WorkspaceFiles(WorkspaceConfig(Path("C:/sofia-workspace")), run=run)
files.register(registry)  # default prefix="workspace"

# Only after the trusted user/controller authorizes this exact read:
run.permissions.approve(Action.SENSITIVE_READ, files.permission_scope)
run.permissions.approve(Action.SENSITIVE_READ, files.path_scope("notes.txt"))
result = registry.execute("workspace_read", {"path": "notes.txt"})
```

`WorkspaceConfig` requires an absolute `Path`. There is no default root. Missing,
file, link, drive, home, and selected operating-system roots are refused. All
ancestors must be real directories. Configuration is not a model tool argument.
`max_bytes` defaults to 8192 (range 1–8192); `max_entries` defaults to 64 (1–64).
Those maxima keep complete outputs within the existing bounded JSON contract.

`WorkspaceFiles(config, run=...)` binds one root identity and one shared run.
It retains a root handle to prevent file-ID reuse; on Windows this also blocks
ordinary root rename/delete until closed. Use it as a context manager or call
`close()` before removing a workspace. Close revokes pending proposal approvals,
releases the handle and disables its registered tools. Garbage collection also
releases the handle, but explicit lifetime management is preferred.
`register(registry, prefix="workspace")` registers six tools, once per instance;
run mismatch and duplicate names are rejected. Only trusted code can register.
`permission_scope` identifies the workspace instance. `path_scope(path,
directory=False)` validates a relative path and returns an opaque exact scope.
Scope spellings are exact, including case, even on case-insensitive Windows.
`proposal(preview_id)` returns an immutable `WorkspacePreview` for local review.
`approval_scope(preview_id)` returns the corresponding permission-policy target;
the identifier alone grants nothing. `discard(preview_id)` removes a pending
proposal and revokes its approval. No public file-mutation or approval method is
provided outside registry execution. Python controllers remain trusted code.

| Tool suffix | Arguments | Registered action |
| --- | --- | --- |
| `list` | `path` (`"."` for root) | `SENSITIVE_READ` |
| `read` | `path` | `SENSITIVE_READ` |
| `preview` | `path`, `operation`, optional `content` | `SENSITIVE_READ` |
| `create` | `preview_id` | `FILE_WRITE` |
| `replace` | `preview_id` | `FILE_WRITE` |
| `delete` | `preview_id` | `FILE_DELETE` |

Registered authorization uses `files.permission_scope`. Handlers additionally
check the same action against the validated exact `path_scope`. Commits also
check the exact preview scope. All use **the existing `run.permissions`**; there
is no second approval policy. Only the registry consumes a step. An exact-scope
denial inside a handler retains the already-consumed step. No handler calls
`begin_step()` or creates another budget. Accounting-blocked runs stop.

## Paths and filesystem primitives

Model paths use forward slashes only and at most 240 characters. Absolute,
drive-relative, UNC, device, extended namespace, colon/ADS, backslash (including
mixed separators), wildcard, tilde/8.3 alias, control/format characters, empty
components, `.`/`..`, trailing dots/spaces, leading spaces and Windows reserved
device names (including extensions and superscript digit forms) fail validation.
Only directory listing accepts the exact special value `"."`.

Containment uses component-by-component traversal and identities, not string
prefix matching. An existing root is canonicalized and verified. Root and parent
identities are recorded in previews, and compared again during mutation.

* **Windows:** fixed local NTFS volumes only. UNC, mapped/SUBST drives, non-NTFS
  and unknown device mappings fail closed. `CreateFileW` opens every parent from
  the drive anchor with `OPEN_REPARSE_POINT` and `BACKUP_SEMANTICS`. Read access
  participates in sharing checks; omitting `FILE_SHARE_DELETE` pins parents
  against ordinary rename/delete while full-path operations run. Handle-based
  attribute queries reject every reparse tag before use. Regular read handles
  also deny ordinary concurrent write/delete while open. Named NTFS data streams
  on target files are rejected, so text previews cannot silently omit stream data.
* **Linux:** supported local ext2/3/4, XFS, Btrfs, tmpfs and overlay mounts only.
  The bounded mount table is checked before opening the root. `O_NOFOLLOW`,
  `O_DIRECTORY`, directory-relative operations and live directory descriptors
  anchor access. `/proc/self/fdinfo` mount IDs reject nested mounts, including
  same-device bind mounts; device identities are also checked. Other POSIX
  platforms/filesystems and inaccessible mount metadata fail closed.

File and directory symlinks, internal links, dangling links, junctions and
unsupported reparse types are rejected. Listing omits unsafe entries rather than
following them. All regular-file access rejects `st_nlink != 1`, including reads
and delete previews. Replacement never truncates an existing inode. A malicious
process adding hard links after the last check is outside the exclusive-access
contract; this is not a guarantee against privileged or hostile local aliasing.

These mechanisms follow the platform contracts for
[Windows CreateFile sharing and reparse handling](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createfilew)
and [Python directory-relative filesystem operations](https://docs.python.org/3/library/os.html).
They mitigate races; canonicalization alone does not make an operation race-free.

## Listing, reading and privacy

Listing is nonrecursive and returns sorted relative paths with only `file` or
`directory` types. It checks cancellation/deadlines during enumeration. Both
examined and returned entries are bounded: a directory exceeding `max_entries`
examined entries returns `entry_limit`, not a misleading partial selection.
Metadata enumeration never reads file contents. No absolute paths are returned.

Reads require a regular single-link file, bounded size and valid UTF-8. Reads use
chunks, a one-byte overrun check, before/after version checks and checkpoints.
NUL/unsupported ASCII controls, binary content and invalid encoding are refused.
Text is returned with byte count and SHA-256 digest. Text may contain prompt
injection and must remain data; it never executes or changes policy.

All dot-prefixed components are excluded (including `.git`, `.env`, `.venv`,
`.ssh`, `.aws`), along with `venv`, `env`, `node_modules`, `__pycache__`, names
containing credential/secret/token, common SSH private-key names and private-key
container suffixes. Common PEM private-key envelopes are blocked even when
renamed. These are conservative exclusions, **not general secret detection**.
Only place intended data in a workspace; renamed or unknown secret formats cannot
be reliably classified. Excluded targets stay blocked even with read approval.

No output, content or preview is automatically sent to any provider. The current
Agent Loop keeps observations local. Any future external data release needs a
separate trusted policy; permission to read is not permission to upload.

## Preview, approval, create, replace and delete

`preview` accepts `operation="create"`, `"replace"` or `"delete"`. Writes require
bounded UTF-8 `content`; deletion rejects any content argument. It performs no
mutation. The controller's immutable proposal includes workspace ID, exact
relative path, operation, parent identities, existing file identity/size/timestamps
and SHA-256 digest, proposed content/digest and byte count. Tool output contains
safe summary metadata and an opaque preview ID; raw proposed content is available
only through explicit local `proposal()` inspection. At most 32 proposals are
retained; discard unused proposals. They are never persisted.

After reviewing the exact proposal, trusted code approves all three scopes:

```python
proposal = files.proposal(preview_id)
action = Action.FILE_DELETE if proposal.operation == "delete" else Action.FILE_WRITE
run.permissions.approve(action, files.permission_scope)
run.permissions.approve(action, files.path_scope(proposal.path))
run.permissions.approve(action, files.approval_scope(preview_id))
result = registry.execute("workspace_" + proposal.operation, {"preview_id": preview_id})
```

This applies to creation too. A model cannot set `approved`, change content or
path at commit, approve a scope, renew a consumed proposal, or select another root.
PermissionPolicy membership is the trusted approval decision. Different proposal
IDs are different scopes even for identical content. Wrong operation/foreign ID
is rejected. A proposal remains reviewable after an admission permission denial.
Once admitted, it is consumed on success, stale detection, I/O failure or
cancellation. Its preview permission is revoked in `finally`; retry requires a
new preview and fresh approval. All permissions are checked again before commit.

The current target and parents are revalidated after approval delay and again
after writing the temporary file. Changed identity/content/version, disappearance,
new destination or changed parent causes refusal. Digest checks do not detect an
edit that has been completely reverted with identical observable metadata.

Writes use an unpredictable exclusively-created temporary file in the same
directory, bounded writes, flush via `fsync`, and final pre-commit checks.
New-file publication is no-clobber: Windows rename rejects existing destinations;
Linux creates an exclusive hard link to the completed temporary inode then removes
the temporary name. Replacement uses `os.replace`, never in-place truncation.
Failed pre-commit writes preserve the original and attempt temporary cleanup.
Windows replacement copies the original DACL to the temporary file before writing,
protecting the copied ACL from additional inheritance; inability to copy fails
closed. Linux temporary files are created with mode 0600 (subject to a stricter
umask). Replacement is a new file identity: ownership, timestamps, alternate
metadata, audit ACLs and extended attributes are not generally preserved. Do not
use it for files whose security depends on unsupported metadata.

Delete accepts only the previewed bounded UTF-8 regular file. It removes one
directory entry with `unlink`, after permission/version/cancellation checks.
There is no directory, recursive, wildcard or bulk delete. **Deletion is permanent;
no Trash, backup, undo or automatic retry is provided.**

Neither `os.replace` nor `unlink` offers a portable atomic compare-and-swap against
a preview digest. The final check-to-commit gap therefore relies on the explicit
exclusive-access contract. There is no promise of protection against a hostile
local writer in that gap. POSIX descriptors can continue naming a moved directory;
Windows target read handles must close before ordinary replacement. If those
races cannot be excluded by the controller, mutations are unsupported and must
remain disabled. File `fsync` and atomic publication are not a crash/power-loss
durability guarantee; parent-directory sync and transactional recovery are absent.

## External roots

Every path outside the configured root is rejected, regardless of ordinary
workspace approval. There is no “allow outside” toggle. A trusted controller can
instantiate a separate `WorkspaceFiles` for an explicitly allowlisted additional
root and register it under another trusted prefix. It has a different workspace
ID, all the same filesystem checks, separate action/path permissions, fresh
previews and fresh explicit approval. A primary-root capability cannot authorize
the second instance. No root discovery, model registration or wildcard exists.

## Results, cancellation and audit

Tool results keep the existing bounded JSON contract. Inspect both
`ToolResult.succeeded` and `output["status"]`: known workspace errors return
`{"status":"error","code":...}`. They are not successful filesystem effects.
Successful commits return `committed`, operation, digest, byte count and
`cleanup_failed`. Ordinary I/O errors are fixed codes without exception text.
Unexpected errors use the registry's existing `handler_failed`. Guardrail
exceptions propagate unchanged, and deadline expiry remains `timeout`.

Checkpoints run before/after path validation, during reads/listing/writing, before
temporary creation, before commit/delete and after return. There are no background
workers, hidden retries or hard thread interruptions. Blocking OS I/O remains
cooperative. Cancellation before commit prevents publication; cancellation after
commit cannot undo it. Registry post-checks still report a late stop rather than
success. Controllers must treat stopped/failed mutation results as potentially
committed and inspect state locally; never retry automatically.

The additional `sofia.workspace` audit event records only operation, whether
approval was checked, whether commit occurred and whether cleanup failed. This
records the commit boundary even when a late cancellation prevents a result.
Existing registry audit events remain intact. No filenames, paths, digests,
contents, previews, temp names or exception text enter logs. Persistence is best
effort; logging failure cannot authorize an action. Failed cleanup can leave a
private `.sofia-*.tmp` artifact (and, on Linux create, an extra hard link); the
controller must arrange manual inspection/cleanup. No automatic sweeping occurs.

## Agent integration and offline verification

Use the same run for the workspace, registry and AgentTask. Plans can invoke only
registered names and validated argument schemas through `ToolRegistry.execute`.
Do not put file mutation tools in the Agent Loop retry allowlist. Supply trusted
step and completion verifiers that check actual local results; a transport-success
envelope or model assertion alone is insufficient. The loop gains no direct
filesystem calls, approval facility or provider data-release path.

Run `python examples/workspace_files.py`. It creates and cleans up its own temporary
workspace using synthetic text, performs an approved create through the real Agent
Loop, lists/reads it, rejects an unapproved replacement, approves/replaces/verifies
the exact proposal, and rejects traversal and unauthorized deletion. No provider
requests or persistent user-file edits occur.

Focused tests are `test_workspace_paths.py`, `test_workspace_files.py`,
`test_workspace_security.py` and `test_workspace_integration.py`. They cover path
forms, roots/scopes, real links and Windows junctions, deterministic unknown reparse
attributes, hard links, stale versions, replacement races before final validation,
no-clobber create, I/O and cleanup failures, cancellation/revocation, guardrails,
privacy, one-shot approval and real registry/Agent Loop integration. Windows tests
report explicit symlink privilege skips (WinError 1314), if needed. The FIFO and
mount tests are Linux-specific. Tests are offline and use temporary fixtures.
Passing deterministic race tests is not proof of security against hostile writers.
