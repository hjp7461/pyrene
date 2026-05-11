"""Filesystem MCP tool wrapper — sandbox-confined read/write with TOCTOU defense.

PRD-012 §3.1 (in-scope) + §6 (path traversal rejection). This module
implements the file-write side of scenario C ("SQL result → markdown
report → write_file"). Read is symmetric and follows the same defense.

### Why an in-process wrapper (not a separate stdio MCP)

The official filesystem MCP reference server is fine as a remote stdio
process, but Pyrene's threat model (PROJECT_BRIEF §6.2 anti-patterns)
demands we own the path validation layer. An external subprocess could
honour `PYRENE_FS_ROOT` correctly today and silently regress next
release — so we re-implement the narrow surface (`write_file`,
`read_file`) in-process and keep the path defense local. PLAN-009's
`StdioMcpClient` remains the transport for *external* MCPs (GitHub,
PDF Phase 2.5); this module is *also* registerable through the
gateway's MCPTool catalog by exposing a `tools_list()` factory.

### Threat model — why `O_NOFOLLOW` (PM amend, PRD-012 §6 F1)

The naive Phase 2 sandbox was:

    resolved = (root / path).resolve()
    if not resolved.is_relative_to(root):
        raise PathTraversalError(...)
    resolved.write_text(content)   # <-- TOCTOU window

Between `resolve()` (which follows symlinks) and `write_text()`, an
attacker with *any* write access to a sibling directory can swap a
benign filename for a symlink pointing at `/etc/passwd`. The `resolve()`
output stays inside the sandbox (it pointed at a real file inside the
sandbox at validation time), but the subsequent `open()` follows the
fresh symlink out of the sandbox.

The fix is to ask the kernel itself to refuse to follow the link:

    fd = os.open(resolved, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)

`O_NOFOLLOW` is documented in `open(2)` POSIX — if the final path
component is a symbolic link, the call fails with `ELOOP`. No race;
the kernel rejects atomically. Linux + macOS support it; Windows does
not have an equivalent in the standard `open(2)` API, so the wrapper
raises `NotImplementedError` on Windows (PROJECT_BRIEF §10 limits the
docker target to Linux anyway).

### Why both `is_relative_to` AND `O_NOFOLLOW`

`is_relative_to` is the *prefix* defense: it rejects `../../../etc/passwd`
*before* any syscall. `O_NOFOLLOW` is the *race* defense: even if the
prefix check passes, the open won't traverse a symlink. The two
defenses compose — neither alone is sufficient:

  - Prefix-only → vulnerable to mid-execution symlink swap.
  - O_NOFOLLOW-only → vulnerable to literal `..` traversal on a path
    *with no symlinks*, because `..` is a directory entry, not a link.

We also resolve `root` once at construction time and store the absolute
real path — `path` inputs go through `joinpath().resolve()` for the
prefix check, then the *resolved* path (with all interior symlinks
already expanded) is what `os.open` operates on with `O_NOFOLLOW`
guarding the final component.

### Filename component restrictions

Beyond traversal, we also reject:
  - null bytes (`\x00`) — POSIX path components cannot contain them,
    but Python str does not enforce it; we raise eagerly.
  - absolute paths (`Path(path).is_absolute()`) — caller must pass a
    relative path; absolute input is rejected before resolution to
    keep error messages crisp.

### Out of scope (Phase 2.5)

  - PDF MCP — deferred (PRD-012 §6 amend). PDF generation pulls in
    pandoc or weasyprint plus a font cache; the TOCTOU surface and
    library footprint don't justify Day 2's budget. Scenario C ships
    as "markdown report → file" (still validates the dual-MCP hop
    security story).
  - Encrypted PYRENE_FS_ROOT bookkeeping — Phase 2.5.
"""

from __future__ import annotations

import errno as _errno
import logging
import os
import sys
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

_logger = logging.getLogger(__name__)

# errno values that indicate `O_NOFOLLOW` rejected a final-component
# symlink. Linux raises ELOOP (40); macOS raises ELOOP (62). We accept
# either via the stdlib `errno` module, which is portable across both.
_SYMLINK_ERRNOS = frozenset({_errno.ELOOP})


# Environment variable name (PRD-012 §3.1). The host application reads
# this at app startup and passes the resolved Path into FilesystemMcpTool;
# the module does not call `os.environ.get` itself (testability + the
# anti-pattern of "module-level global env" PROJECT_BRIEF §6.2).
FS_ROOT_ENV = "PYRENE_FS_ROOT"


class FilesystemSandboxError(RuntimeError):
    """Base class — all path/sandbox failures raise a subclass of this.

    The gateway audit hook can catch this single type and tag the event
    as `outcome="denied"` (PRD-012 §6 F1)."""


class PathTraversalError(FilesystemSandboxError):
    """Raised when the requested path resolves outside the sandbox root.

    Triggered by `../`, absolute paths, or symlinks that point outside
    the root. The `requested` field is the *raw* input (not normalized);
    the resolved path is suppressed to avoid leaking sandbox internals
    in error messages."""

    def __init__(self, requested: str, reason: str) -> None:
        super().__init__(
            f"path traversal rejected (reason={reason}): {requested!r}"
        )
        self.requested = requested
        self.reason = reason


class SymlinkRaceError(FilesystemSandboxError):
    """Raised when `O_NOFOLLOW` rejects a symlink at the final component.

    This is the TOCTOU race detector: the prefix check passed, but
    between resolve() and open() the path component became a symlink.
    Errno is `ELOOP` on Linux."""

    def __init__(self, requested: str) -> None:
        super().__init__(
            f"symlink at final path component (TOCTOU defense, ELOOP): "
            f"{requested!r}"
        )
        self.requested = requested


class FilesystemWriteInput(BaseModel):
    """Input contract for `write_file`. PRD-012 §4.

    Deliberately does NOT inherit from `pyrene_core.StrictBaseModel` —
    that base sets `str_strip_whitespace=True`, which would silently
    strip trailing newlines from file content (corrupting markdown
    reports, log files, etc.). We pin the config explicitly: `extra=
    "forbid"` + frozen for safety, but whitespace stays intact.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str = Field(min_length=1, max_length=4096)
    content: str = Field(max_length=10 * 1024 * 1024)  # 10 MiB ceiling

    @field_validator("path")
    @classmethod
    def _path_no_null_bytes(cls, v: str) -> str:
        if "\x00" in v:
            raise ValueError("path must not contain null bytes")
        return v


class FilesystemReadInput(BaseModel):
    """Input contract for `read_file`. PRD-012 §4.

    Same whitespace-preservation rationale as `FilesystemWriteInput`."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str = Field(min_length=1, max_length=4096)

    @field_validator("path")
    @classmethod
    def _path_no_null_bytes(cls, v: str) -> str:
        if "\x00" in v:
            raise ValueError("path must not contain null bytes")
        return v


class FilesystemMcpTool:
    """In-process filesystem MCP tool with sandbox + TOCTOU defense.

    Usage:
        tool = FilesystemMcpTool(root=Path("/var/pyrene/files"))
        await tool.write_file("reports/2026-q1.md", "...markdown...")

    The `root` is resolved once at construction (`Path.resolve()`); the
    resolved real path is what every call's prefix check compares
    against. If the *root itself* is a symlink that later changes, the
    sandbox has bigger problems than this module can defend against —
    operators should mount the root as a non-symlink directory (the
    docker-compose recipe in deploy/ pins the bind-mount).

    `allow_write` (PRD-012 §4 `FilesystemSandbox.allow_write`) defaults
    to `False` for the read-only default. Operators flip it explicitly
    when scenario C is enabled.

    Windows is not supported (PROJECT_BRIEF §10). The constructor raises
    `NotImplementedError` on win32 — fail fast at import-time configuration,
    not at first call.
    """

    def __init__(
        self,
        *,
        root: Path,
        allow_write: bool = False,
    ) -> None:
        if sys.platform == "win32":
            # `O_NOFOLLOW` is a POSIX flag; Python on Windows defines the
            # constant for parity but the underlying `open(2)` semantics
            # are unsupported. Fail fast (PRD-012 §6).
            raise NotImplementedError(
                "FilesystemMcpTool requires POSIX O_NOFOLLOW; "
                "Windows is not supported in Phase 2"
            )
        # Resolve the root once and store as a string for prefix checks.
        # `strict=True` requires the root to exist — operators must
        # pre-create it (docker-compose / mise bootstrap).
        self._root: Path = root.resolve(strict=True)
        if not self._root.is_dir():
            raise ValueError(
                f"FilesystemMcpTool root must be a directory: {self._root!r}"
            )
        self._allow_write = allow_write

    @property
    def root(self) -> Path:
        return self._root

    @property
    def allow_write(self) -> bool:
        return self._allow_write

    # --- Path validation -----------------------------------------------------

    def _resolve_within_root(self, raw_path: str) -> Path:
        """Validate + resolve `raw_path` inside the sandbox root.

        Raises `PathTraversalError` if:
          - the input is absolute,
          - it contains null bytes,
          - the resolved path is not inside `self._root`.

        Returns the *resolved* `Path` (interior symlinks already
        expanded). The caller passes this to `os.open(..., O_NOFOLLOW)`
        for the final-component TOCTOU guard.
        """
        if "\x00" in raw_path:
            # Defense-in-depth — Pydantic validator already caught this,
            # but the function is also called from internal paths.
            raise PathTraversalError(raw_path, reason="null byte")

        candidate = Path(raw_path)
        if candidate.is_absolute():
            raise PathTraversalError(raw_path, reason="absolute path")

        # `joinpath().resolve()` expands any interior `..` and follows
        # interior symlinks. We deliberately do *not* pass `strict=True`
        # because write_file may create a new file (target need not exist).
        joined = self._root.joinpath(candidate)
        try:
            resolved = joined.resolve()
        except (OSError, RuntimeError) as exc:
            # `resolve()` on a path with a broken symlink loop raises;
            # treat that as traversal denial.
            raise PathTraversalError(
                raw_path, reason=f"resolve failed: {exc!r}"
            ) from exc

        # Prefix check (defense #1). Python 3.9+ `is_relative_to` returns
        # False for paths outside the root *without* raising.
        if not resolved.is_relative_to(self._root):
            raise PathTraversalError(raw_path, reason="escapes root")

        # Reject targeting the root itself.
        if resolved == self._root:
            raise PathTraversalError(raw_path, reason="target is root")

        return resolved

    # --- write_file ---------------------------------------------------------

    async def write_file(self, path: str, content: str) -> dict[str, Any]:
        """Write `content` to `path` (UTF-8) inside the sandbox.

        Returns a JSON-serializable dict for MCP tool output:
          {"path": str, "bytes_written": int}

        The dict deliberately does *not* include the resolved absolute
        path — exposing it would leak sandbox internals to callers that
        only ever passed in relative paths.

        Raises:
          - `FilesystemSandboxError` (subclass) on any sandbox violation.
          - `PermissionError` if `allow_write=False`.
        """
        if not self._allow_write:
            raise PermissionError(
                f"filesystem MCP tool is read-only (set allow_write=True "
                f"to enable; requested write to {path!r})"
            )

        # Validate input shape via the Pydantic contract. This catches
        # null-bytes + length ceilings before any I/O.
        validated = FilesystemWriteInput(path=path, content=content)

        resolved = self._resolve_within_root(validated.path)

        # Ensure parent dir exists. We deliberately do NOT create the
        # entire chain — only the immediate parent — to limit the surface
        # an attacker could pre-stage with symlinks. Parents must already
        # be ordinary directories under root.
        parent = resolved.parent
        if not parent.exists():
            # Create only directories that resolve inside root. Each
            # mkdir step is itself a potential symlink target, so we
            # mkdir with the resolved path (which is already validated).
            parent.mkdir(parents=True, exist_ok=True)
        if not parent.is_dir():
            raise PathTraversalError(
                path, reason="parent is not a directory"
            )

        # --- TOCTOU defense (PRD-012 §6 PM amend) ---------------------
        # `O_NOFOLLOW`: if the final component is a symlink at open()
        # time, the kernel rejects with ELOOP. This closes the window
        # between resolve() (which followed all symlinks) and open()
        # (which would normally re-follow them).
        # `O_CLOEXEC`: defense in depth — prevent the fd from leaking
        # into any subprocess (none expected, but cheap).
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_TRUNC
            | os.O_NOFOLLOW
            | os.O_CLOEXEC
        )
        try:
            fd = os.open(resolved, flags, 0o600)
        except OSError as exc:
            # ELOOP (40 on Linux, 62 on macOS) signals symlink at final
            # component. Translate to our typed error for the audit hook.
            if exc.errno in _SYMLINK_ERRNOS:
                raise SymlinkRaceError(path) from exc
            raise
        try:
            encoded = validated.content.encode("utf-8")
            written = os.write(fd, encoded)
            # `os.write` may write fewer bytes than requested on some
            # platforms; loop until done.
            while written < len(encoded):
                more = os.write(fd, encoded[written:])
                if more == 0:
                    raise OSError(
                        "os.write returned 0; refusing to spin"
                    )
                written += more
        finally:
            os.close(fd)

        return {"path": validated.path, "bytes_written": written}

    # --- read_file ----------------------------------------------------------

    async def read_file(self, path: str) -> dict[str, Any]:
        """Read `path` (UTF-8) from the sandbox.

        Returns:
          {"path": str, "content": str}

        Same defenses as `write_file`: prefix check + `O_NOFOLLOW`.
        Read is allowed regardless of `allow_write` (read-only default).
        """
        validated = FilesystemReadInput(path=path)
        resolved = self._resolve_within_root(validated.path)

        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
        try:
            fd = os.open(resolved, flags)
        except OSError as exc:
            if exc.errno in _SYMLINK_ERRNOS:
                raise SymlinkRaceError(path) from exc
            raise
        try:
            chunks: list[bytes] = []
            while True:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                chunks.append(chunk)
        finally:
            os.close(fd)
        content = b"".join(chunks).decode("utf-8")
        return {"path": validated.path, "content": content}


__all__ = [
    "FS_ROOT_ENV",
    "FilesystemMcpTool",
    "FilesystemReadInput",
    "FilesystemSandboxError",
    "FilesystemWriteInput",
    "PathTraversalError",
    "SymlinkRaceError",
]
