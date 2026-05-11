"""Unit tests for FilesystemMcpTool — 5 traversal cases + TOCTOU race.

PRD-012 §6 F1 ("path traversal 시도 거부 (sandbox 검증 5 케이스)") +
PM amend (TOCTOU `O_NOFOLLOW`). Each test isolates one defense layer:

1. `../` parent escape       → `PathTraversalError("escapes root")`
2. absolute path             → `PathTraversalError("absolute path")`
3. interior symlink escape   → `PathTraversalError("escapes root")`
4. null byte in path         → Pydantic validation error
5. encoding/URL-decoded `..` → traversal at literal level, rejected
6. TOCTOU symlink swap       → `SymlinkRaceError` (ELOOP)

The TOCTOU test is the key new artifact (PM amend). We simulate the
race by patching `os.open` to *swap* a symlink between the resolve()
call and the actual open(). The wrapper must catch the swap via
`O_NOFOLLOW` rejection.

Test files are named with `_mcp_tools` suffix per Wave 8 unique-naming
guard (parallel PLAN-011/014 packages must not collide on test names).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Skip the entire module on Windows — FilesystemMcpTool raises
# NotImplementedError at construction and there is nothing to test.
pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="FilesystemMcpTool requires POSIX O_NOFOLLOW",
)

from pyrene_mcp_tools import (  # noqa: E402
    FilesystemMcpTool,
    PathTraversalError,
    SymlinkRaceError,
)
from pyrene_mcp_tools.filesystem import FilesystemWriteInput  # noqa: E402

# --- fixtures ---------------------------------------------------------------


@pytest.fixture
def sandbox_root(tmp_path: Path) -> Path:
    """A throwaway sandbox root for each test. tmp_path is per-test."""
    root = tmp_path / "fs-root"
    root.mkdir()
    return root


@pytest.fixture
def tool(sandbox_root: Path) -> FilesystemMcpTool:
    return FilesystemMcpTool(root=sandbox_root, allow_write=True)


# --- 1. parent escape via `../` --------------------------------------------


async def test_traversal_case_1_parent_escape_dotdot(
    tool: FilesystemMcpTool,
) -> None:
    """`../etc/passwd` resolves outside root → PathTraversalError."""
    with pytest.raises(PathTraversalError) as exc:
        await tool.write_file("../etc/passwd", "evil")
    assert exc.value.reason == "escapes root"


async def test_traversal_case_1b_multi_dotdot(
    tool: FilesystemMcpTool,
) -> None:
    """`../../../../../tmp/x` resolves outside root."""
    with pytest.raises(PathTraversalError) as exc:
        await tool.write_file("../../../../../tmp/x", "evil")
    assert exc.value.reason == "escapes root"


# --- 2. absolute path -------------------------------------------------------


async def test_traversal_case_2_absolute_path(
    tool: FilesystemMcpTool,
) -> None:
    """An absolute path is rejected before resolution."""
    with pytest.raises(PathTraversalError) as exc:
        await tool.write_file("/etc/passwd", "evil")
    assert exc.value.reason == "absolute path"


# --- 3. interior symlink escape --------------------------------------------


async def test_traversal_case_3_interior_symlink_escape(
    sandbox_root: Path, tool: FilesystemMcpTool, tmp_path: Path
) -> None:
    """An interior dir is a symlink pointing OUTSIDE root → escapes."""
    outside = tmp_path / "outside-dir"
    outside.mkdir()
    # Create a symlink INSIDE root that points OUTSIDE root.
    link = sandbox_root / "escape-link"
    link.symlink_to(outside)

    with pytest.raises(PathTraversalError) as exc:
        await tool.write_file("escape-link/target.txt", "evil")
    assert exc.value.reason == "escapes root"


# --- 4. null byte in path --------------------------------------------------


async def test_traversal_case_4_null_byte(tool: FilesystemMcpTool) -> None:
    """A null byte in the path is rejected by the Pydantic validator."""
    # The Pydantic contract raises ValidationError before reaching the
    # sandbox method body.
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        FilesystemWriteInput(path="ok\x00.txt", content="x")


async def test_traversal_case_4b_null_byte_direct(
    tool: FilesystemMcpTool,
) -> None:
    """`_resolve_within_root` rejects null bytes even if called directly."""
    with pytest.raises(PathTraversalError) as exc:
        tool._resolve_within_root("ok\x00.txt")
    assert exc.value.reason == "null byte"


# --- 5. encoded `..` / unusual encodings -----------------------------------


async def test_traversal_case_5_url_encoded_dotdot(
    tool: FilesystemMcpTool,
) -> None:
    """`%2e%2e/etc/passwd` is treated as a literal filename containing
    `%2e%2e`, which Path normalises but does not decode.

    The point: the wrapper does NOT URL-decode (no double-decoding bug).
    The literal `%2e%2e` is just a weird filename inside root — the
    write SUCCEEDS as `<root>/%2e%2e/etc/passwd` would be created. We
    assert that NO escape happens.
    """
    # This path would create directories named "%2e%2e" and "etc" under
    # root. That's a contained, intended file. We just verify there is
    # no escape (resolved path stays under root).
    resolved = tool._resolve_within_root("%2e%2e/etc/passwd")
    assert resolved.is_relative_to(tool.root)


async def test_traversal_case_5b_target_is_root(
    tool: FilesystemMcpTool,
) -> None:
    """`.` resolves to the root directory itself → reject."""
    with pytest.raises(PathTraversalError) as exc:
        await tool.write_file(".", "evil")
    assert exc.value.reason == "target is root"


# --- TOCTOU race — the PM amend headline test ------------------------------


async def test_toctou_symlink_swap_mid_execution(
    sandbox_root: Path,
    tool: FilesystemMcpTool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Simulate the canonical TOCTOU window: resolve() validates against
    a benign target; between validation and open(), an attacker swaps
    the path component for a symlink pointing outside the sandbox.

    Defense: `os.open(resolved, ..., O_NOFOLLOW)` MUST reject the
    swapped symlink and the wrapper MUST raise `SymlinkRaceError`.

    We can't actually win the race in a deterministic test, so we patch
    `os.open` to *simulate* the post-validation symlink swap: just
    before the syscall executes, replace the target file with a symlink
    pointing outside root. If `O_NOFOLLOW` is in the flags (which our
    wrapper enforces), the patched os.open will see the symlink and
    raise ELOOP — exactly the race outcome we are defending against.
    """
    # Pre-stage a benign file at the validated path (so resolve() works
    # and the parent dir exists).
    benign = sandbox_root / "report.md"
    benign.write_text("benign initial content")

    outside_target = tmp_path / "attacker-target"
    outside_target.write_text("secret payload")

    real_os_open = os.open
    swap_done: list[bool] = []

    def _swapping_os_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
    ) -> int:
        # Confirm the wrapper passed O_NOFOLLOW — without it, this
        # entire defense collapses. The test fails LOUD if the wrapper
        # forgets the flag.
        assert flags & os.O_NOFOLLOW, (
            "wrapper must call os.open with O_NOFOLLOW; "
            "TOCTOU defense missing"
        )
        # On the FIRST call (the wrapper's open), swap the file for a
        # symlink pointing outside root — exactly what an attacker would
        # do mid-syscall. After the swap, the real os.open should fail
        # with ELOOP because O_NOFOLLOW is set.
        if not swap_done:
            swap_done.append(True)
            target_path = Path(os.fsdecode(path))
            target_path.unlink()
            target_path.symlink_to(outside_target)
        return real_os_open(path, flags, mode)

    monkeypatch.setattr(os, "open", _swapping_os_open)

    with pytest.raises(SymlinkRaceError) as exc:
        await tool.write_file("report.md", "agent output")
    assert "report.md" in str(exc.value)

    # Verify the attack actually placed the symlink (sanity — proves
    # the test really simulated the race) and that the outside target
    # was NOT modified.
    assert (sandbox_root / "report.md").is_symlink()
    assert outside_target.read_text() == "secret payload"


# --- normal write/read round-trip ------------------------------------------


async def test_write_then_read_roundtrip(tool: FilesystemMcpTool) -> None:
    """Sanity — the happy path actually works."""
    write_result = await tool.write_file("subdir/note.md", "hello world")
    assert write_result == {
        "path": "subdir/note.md",
        "bytes_written": len(b"hello world"),
    }
    read_result = await tool.read_file("subdir/note.md")
    assert read_result == {"path": "subdir/note.md", "content": "hello world"}


async def test_write_unicode_content(tool: FilesystemMcpTool) -> None:
    """UTF-8 multi-byte content round-trips correctly."""
    content = "한글 + emoji"
    write_result = await tool.write_file("k.md", content)
    assert write_result["bytes_written"] == len(content.encode("utf-8"))
    read_result = await tool.read_file("k.md")
    assert read_result["content"] == content


async def test_allow_write_false_rejects_write(sandbox_root: Path) -> None:
    """Default allow_write=False blocks write_file but permits read_file."""
    ro_tool = FilesystemMcpTool(root=sandbox_root, allow_write=False)
    (sandbox_root / "pre.txt").write_text("preexisting")

    with pytest.raises(PermissionError, match="read-only"):
        await ro_tool.write_file("new.txt", "x")

    # Read still works.
    assert (await ro_tool.read_file("pre.txt"))["content"] == "preexisting"


async def test_root_must_be_directory(tmp_path: Path) -> None:
    not_a_dir = tmp_path / "a-file"
    not_a_dir.write_text("x")
    with pytest.raises(ValueError, match="must be a directory"):
        FilesystemMcpTool(root=not_a_dir, allow_write=True)


async def test_root_must_exist(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist"
    with pytest.raises(FileNotFoundError):
        FilesystemMcpTool(root=missing, allow_write=True)
