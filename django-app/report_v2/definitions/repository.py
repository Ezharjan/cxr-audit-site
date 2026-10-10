# PRIMER - LLM-based Chest X-Ray Audit Tool
# Copyright (C) 2026 Goh Shu Wen
# Licensed under AGPL-3.0-or-later. See LICENSE at the repository root.
"""File-backed definition drafts + immutable publication with atomic pointer swaps.

Mechanism:
- Revision tokens are SHA-256 hex digests of the YAML text (content-addressed).
- Publication writes blobs under root/blobs/ then atomically flips the pointer file
  root/pointers/<def_id> via os.replace (inode swap, safe for bind-mounts).
- Concurrency: fcntl.flock(LOCK_EX) on a per-def_id lockfile under root/.locks/
  serialises publishes of the SAME def_id; different def_ids are independent.
- Immutable means published blobs are never mutated; republishing changed content
  creates a NEW version blob; old blobs stay forever.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import os
import re

# ---------------------------------------------------------------------------
# Cross-platform advisory lock (Task 16 portability fix).
# Production (Linux/Docker) keeps the exact fcntl.flock semantics; Windows local
# development/test runs -- where the fcntl module does not exist -- fall back to
# msvcrt byte-range locks on the same lockfile, preserving the same
# acquire-exclusively / release / close discipline.
# ---------------------------------------------------------------------------
try:
    import fcntl
except ImportError:  # pragma: no cover -- exercised only on Windows hosts

    def _flock_ex(fd: int) -> None:
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_LOCK, 1)

    def _flock_unlock(fd: int) -> None:
        import msvcrt

        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            # Unlocking an unlocked region (e.g. when acquisition itself timed out)
            # must not mask the original failure; closing the fd releases the lock.
            pass
else:

    def _flock_ex(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX)

    def _flock_unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .loader import DefinitionError, load_report_definition
from .validation import (
    DisplayValidationError,
    ensure_no_calculated_baseline_band,
    ensure_whisker_is_not_ci,
    validate_display,
)
from ..projects.registry import CrossProjectReferenceError, ProjectRegistry, require_project_context
from ..projects.base import ProjectCatalogError

# ---------------------------------------------------------------------------
# Default private persistent root
# ---------------------------------------------------------------------------
# The runtime definitions/publish root is PRIVATE and lives OUTSIDE every public
# static surface: never under STATIC_ROOT (the collectstatic / WhiteNoise-served
# dir) and never under a STATICFILES_DIRS entry. An operator may point it
# elsewhere via settings.REPORT_V2_ROOT; the fallback below is derived from
# BASE_DIR and is deliberately placed under a ``private_data`` tree that
# collectstatic does not harvest. default_root() is pure path math (creates no
# directories) so importing this module never touches the filesystem.
_DEFAULT_ROOT_SUBPATH = ("private_data", "report_v2_definitions")


def _fsync_path(path: Path) -> None:
    """fsync an existing file's bytes to disk.

    Opens read-write because Windows ``_commit`` (what ``os.fsync`` calls there)
    rejects read-only handles with EBADF; POSIX behaviour is unchanged.
    """
    fd = os.open(str(path), os.O_RDWR)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def default_root() -> Path:
    """Return the configured runtime publish root (a private, non-public path).

    Reads ``settings.REPORT_V2_ROOT`` when set; otherwise falls back to a
    documented ``private_data`` tree under ``settings.BASE_DIR``. The fallback is
    guaranteed NOT to live under ``STATIC_ROOT`` or any ``STATICFILES_DIRS``
    entry, so definition blobs are never collected by ``collectstatic`` nor served
    by WhiteNoise. No directories are created here.
    """
    from django.conf import settings as _s

    override = getattr(_s, "REPORT_V2_ROOT", None)
    if override:
        return Path(override)
    base = Path(str(getattr(_s, "BASE_DIR", Path.cwd())))
    return base.joinpath(*_DEFAULT_ROOT_SUBPATH)


# ---------------------------------------------------------------------------
# Typed errors
# ---------------------------------------------------------------------------

class RepositoryError(Exception):
    """Base class for all repository-level errors."""


class InvalidDefinitionIdError(RepositoryError):
    """Definition ID is malformed or unsafe."""


class PathTraversalError(RepositoryError):
    """Resolved path escapes the configured root (symlink-free traversal detected)."""


class PathEscapeError(RepositoryError):
    """A resolved symlink target escapes the configured root."""


class StaleRevisionError(RepositoryError):
    """Optimistic concurrency: expected_revision does not match current on-disk revision."""


class DraftNotFoundError(RepositoryError):
    """A draft does not exist at the given definition id."""


class PublishRejectedError(RepositoryError):
    """Validation failed; the pointer was NOT moved."""


class DuplicateVersionError(RepositoryError):
    """Target version blob already exists with different content."""


# ---------------------------------------------------------------------------
# Receipt dataclass
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PublishReceipt:
    """Proof-of-publication returned on success."""
    version: str
    blob_path: Path
    pointer_path: Path


# ---------------------------------------------------------------------------
# ID validation
# ---------------------------------------------------------------------------

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,63}$")


def validate_definition_id(def_id: str | None) -> str:
    """Reject unsafe or malformed definition IDs. Raise InvalidDefinitionIdError."""
    if def_id is None or def_id == "":
        raise InvalidDefinitionIdError("definition id must not be None or empty")
    if not isinstance(def_id, str):
        raise InvalidDefinitionIdError(f"definition id must be a string, got {type(def_id).__name__}")
    if _ID_RE.match(def_id) is None:
        raise InvalidDefinitionIdError(f"invalid definition id: {def_id!r}")
    # belt-and-suspenders explicit checks (regex already excludes these, but spec demands them)
    if "/" in def_id or "\\" in def_id:
        raise InvalidDefinitionIdError(f"path separator in id: {def_id!r}")
    if ".." in def_id:
        raise InvalidDefinitionIdError(f"parent traversal in id: {def_id!r}")
    if "~" in def_id:
        raise InvalidDefinitionIdError(f"tilde in id: {def_id!r}")
    if def_id.startswith("."):
        raise InvalidDefinitionIdError(f"dotfile id: {def_id!r}")
    if os.path.isabs(def_id):
        raise InvalidDefinitionIdError(f"absolute id: {def_id!r}")
    # NUL / control chars
    for ch in def_id:
        if ord(ch) < 0x20 or ord(ch) == 0x7F:
            raise InvalidDefinitionIdError(f"control char in id: {def_id!r}")
    return def_id


# ---------------------------------------------------------------------------
# Path building with containment assertions
# ---------------------------------------------------------------------------

def _build_path(root: Path, def_id: str, *sub: str) -> Path:
    """Validate id, join, and assert logical containment under root (no symlink resolution)."""
    validate_definition_id(def_id)
    resolved_root = root.resolve()
    candidate = resolved_root.joinpath(*sub, def_id)
    # Check logical path doesn't escape (without following symlinks)
    # Use os.path.normpath to collapse .. components without filesystem access
    normed = os.path.normpath(str(candidate))
    if not normed.startswith(str(resolved_root) + os.sep) and normed != str(resolved_root):
        raise PathTraversalError(f"path escapes root: {normed!r}")
    return Path(normed)


def _realpath_within(root: Path, path: Path) -> Path:
    """Check that realpath of path stays under realpath'd root; raise PathEscapeError if not."""
    real_root = Path(os.path.realpath(root))
    real_target = Path(os.path.realpath(path))
    if not real_target.is_relative_to(real_root):
        raise PathEscapeError(f"symlink target escapes root: {real_target!r}")
    return real_target


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------

def _revision_of(yaml_text: str) -> str:
    """Content-addressed revision token (SHA-256 hex)."""
    return hashlib.sha256(yaml_text.encode("utf-8")).hexdigest()[:16]


class DefinitionRepository:
    """File-backed definition repository rooted at *root*, scoped to *project_id*."""

    def __init__(self, root: Path | str, *, project_id: str, registry: ProjectRegistry | None = None) -> None:
        self._root = Path(root)
        self._project_id = project_id
        self._registry = registry  # None => use production default
        # ensure subdirectory layout exists
        self._drafts_dir = self._root / "drafts"
        self._blobs_dir = self._root / "blobs"
        self._pointers_dir = self._root / "pointers"
        self._locks_dir = self._root / ".locks"
        self._tmp_dir = self._root / ".tmp"
        self._deleted_dir = self._root / ".deleted"
        for d in (self._drafts_dir, self._blobs_dir, self._pointers_dir, self._locks_dir, self._tmp_dir, self._deleted_dir):
            d.mkdir(parents=True, exist_ok=True)

    # -- Draft operations ---------------------------------------------------

    def _draft_path(self, def_id: str) -> Path:
        return _build_path(self._root, def_id, "drafts")

    def is_deleted(self, def_id: str) -> bool:
        return _build_path(self._root, def_id, ".deleted").exists()

    @contextmanager
    def _locked(self, def_id: str):
        path = self._lock_path(def_id)
        _realpath_within(self._root, path)
        fd = os.open(str(path), os.O_CREAT | os.O_RDWR)
        try:
            _flock_ex(fd)
            yield
        finally:
            _flock_unlock(fd)
            os.close(fd)

    def save_draft(self, def_id: str, yaml_text: str, *, expected_revision: str | None,
                   create_only: bool = False) -> str:
        """Atomically save a draft under the publication/deletion lock."""
        path = self._draft_path(def_id)
        _realpath_within(self._root, path)
        with self._locked(def_id):
            if self.is_deleted(def_id):
                raise RepositoryError("This report was deleted. Create a report with a different name.")
            if create_only and (path.exists() or self._read_pointer(def_id)
                                or any(self._blobs_dir.glob(f"{def_id}@r*.yaml"))):
                raise StaleRevisionError("A report with this name already exists.")
            current_rev = _revision_of(path.read_text(encoding="utf-8")) if path.exists() else None
            if expected_revision is not None and current_rev != expected_revision:
                raise StaleRevisionError("The draft changed. Reload before saving.")
            tmp = self._tmp_dir / f"draft-{def_id}-{os.getpid()}"
            _realpath_within(self._root, tmp)
            tmp.write_bytes(yaml_text.encode("utf-8"))
            _fsync_path(tmp)
            os.replace(str(tmp), str(path))
        return _revision_of(yaml_text)

    def delete(self, def_id: str, *, expected_revision: str | None,
               expected_version: str | None) -> None:
        """Remove active report content; preserve immutable publication history."""
        draft = self._draft_path(def_id)
        pointer = self._pointer_path(def_id)
        marker = _build_path(self._root, def_id, ".deleted")
        for path in (draft, pointer, marker):
            _realpath_within(self._root, path)
        with self._locked(def_id):
            if self.is_deleted(def_id) or not (draft.exists() or pointer.exists()):
                raise DraftNotFoundError("Report not found.")
            revision = _revision_of(draft.read_text(encoding="utf-8")) if draft.exists() else None
            if revision != expected_revision or self._read_pointer(def_id) != expected_version:
                raise StaleRevisionError("The report changed. Reload before deleting.")
            # Hide the report before removing active files; stale clients cannot recreate it.
            marker.touch(exist_ok=False)
            _fsync_path(marker)
            pointer.unlink(missing_ok=True)
            draft.unlink(missing_ok=True)

    def unpublish(self, def_id: str, *, expected_version: str) -> None:
        """Remove availability while retaining the draft and immutable version history."""
        pointer = self._pointer_path(def_id)
        with self._locked(def_id):
            current = self._read_pointer(def_id)
            if not current or current != expected_version:
                raise StaleRevisionError("The publication changed. Reload before unpublishing.")
            draft = self._draft_path(def_id)
            if not draft.exists():
                temporary = self._tmp_dir / f"unpublish-{def_id}-{os.getpid()}"
                temporary.write_bytes(self._blob_path(def_id, current).read_bytes())
                _fsync_path(temporary)
                os.replace(str(temporary), str(draft))
            pointer.unlink()

    def read_draft(self, def_id: str) -> tuple[str, str]:
        """Return (yaml_text, revision). Raise DraftNotFoundError if absent."""
        path = self._draft_path(def_id)
        if self.is_deleted(def_id) or not path.exists():
            raise DraftNotFoundError(f"draft not found: {def_id!r}")
        # symlink safety
        _realpath_within(self._root, path)
        text = path.read_text(encoding="utf-8")
        return text, _revision_of(text)

    def validate_preview(self, def_id: str, yaml_text: str) -> list[str]:
        """Validate without writing. Returns list of violation strings (empty = ok)."""
        violations: list[str] = []
        try:
            data = load_report_definition(yaml_text)
        except (DefinitionError,) as exc:
            errors = getattr(exc, "errors", None)
            if errors:
                violations.extend(f"{error.get('path') or '$'}: {error['message']}" for error in errors)
            else:
                violations.append(str(exc))
            return violations
        # display validations
        try:
            ensure_whisker_is_not_ci(data)
        except DisplayValidationError as exc:
            violations.append(str(exc))
        try:
            ensure_no_calculated_baseline_band(data)
        except DisplayValidationError as exc:
            violations.append(str(exc))
        try:
            validate_display(data)
        except DisplayValidationError as exc:
            violations.append(str(exc))
        return violations

    # -- Publication --------------------------------------------------------

    def _blob_path(self, def_id: str, version: str) -> Path:
        """Path for a published blob. Version token is safe because def_id is already validated."""
        validate_definition_id(def_id)
        resolved_root = self._root.resolve()
        blob_name = f"{version}.yaml"
        candidate = (resolved_root / "blobs" / blob_name).resolve(strict=False)
        if not candidate.is_relative_to(resolved_root):
            raise PathTraversalError(f"blob path escapes root: {candidate!r}")
        return candidate

    def _pointer_path(self, def_id: str) -> Path:
        return _build_path(self._root, def_id, "pointers")

    def _lock_path(self, def_id: str) -> Path:
        return _build_path(self._root, def_id, ".locks")

    def _read_pointer(self, def_id: str) -> str | None:
        pp = self._pointer_path(def_id)
        if self.is_deleted(def_id) or not pp.exists():
            return None
        _realpath_within(self._root, pp)
        return pp.read_text(encoding="utf-8").strip()

    def _next_version(self, def_id: str, current_pointer: str | None) -> str:
        """Compute the next version string for def_id."""
        if current_pointer is None:
            revisions = [int(path.stem.split("@r")[-1]) for path in self._blobs_dir.glob(f"{def_id}@r*.yaml")
                         if path.stem.split("@r")[-1].isdigit()]
            return f"{def_id}@r{max(revisions, default=0) + 1}"
        # parse trailing number after @r
        parts = current_pointer.split("@r")
        try:
            n = int(parts[-1])
        except (ValueError, IndexError):
            n = 0
        return f"{def_id}@r{n + 1}"

    def _validate_for_publish(self, yaml_text: str) -> dict[str, Any]:
        """Run loader + display validation; raise PublishRejectedError on failure. Return parsed data."""
        try:
            data = load_report_definition(yaml_text)
        except DefinitionError as exc:
            raise PublishRejectedError(str(exc)) from exc
        try:
            ensure_whisker_is_not_ci(data)
        except DisplayValidationError as exc:
            raise PublishRejectedError(str(exc)) from exc
        try:
            ensure_no_calculated_baseline_band(data)
        except DisplayValidationError as exc:
            raise PublishRejectedError(str(exc)) from exc
        try:
            validate_display(data)
        except DisplayValidationError as exc:
            raise PublishRejectedError(str(exc)) from exc
        try:
            from ..field_semantics import widget_project, validate_bindings
            for section in data.get('sections', []):
                for widget in section.get('widgets', []):
                    if 'field_semantics' in widget.get('query', {}):
                        from .visual import validate_card
                        validate_card(widget)
        except (ValueError, TypeError, KeyError) as exc:
            raise PublishRejectedError(str(exc)) from exc
        return data

    def _check_project_refs(self, data: dict[str, Any]) -> None:
        """Walk parsed data for project-scoped identifiers; raise CrossProjectReferenceError on foreign refs.

        Collects measurement ids from top-level and nested sections[*].widgets[*].query, then calls
        the registry guard once per collected id (via require_project_context).
        """
        measurement_ids: list[str] = []
        # Top-level
        m = data.get("measurement") or data.get("measurement_id")
        if isinstance(m, str):
            measurement_ids.append(m)
        # Nested widgets
        for section in data.get("sections") or []:
            for widget in section.get("widgets") or []:
                q = widget.get("query") or {}
                mid = q.get("measurement") or q.get("measurement_id")
                if isinstance(mid, str):
                    measurement_ids.append(mid)
        for mid in measurement_ids:
            require_project_context(
                self._project_id,
                registry=self._registry,
                measurement_id=mid,
            )

    def publish(
        self,
        def_id: str,
        yaml_text: str,
        *,
        expected_revision: str | None = None,
        expected_version: str | None = None,
    ) -> PublishReceipt:
        """Publish a definition immutably. Returns PublishReceipt.

        Steps (ordered):
          1. Validate id.
          2. Validate YAML + display (raises PublishRejectedError, pointer unchanged).
          3. Check project references (raises CrossProjectReferenceError).
          4. Compute version; check duplicate blob (DuplicateVersionError if content differs).
          5. Write blob atomically (tmp + fsync + os.replace on same fs).
          6. Flip pointer atomically (tmp + fsync + os.replace) under flock — the very last move.
        """
        # (1) validate id
        validate_definition_id(def_id)

        # (2) validate content
        data = self._validate_for_publish(yaml_text)

        # (3) project refs
        self._check_project_refs(data)

        # Acquire per-def_id lock for pointer operations
        lock_path = self._lock_path(def_id)
        lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR)
        try:
            _flock_ex(lock_fd)
            if self.is_deleted(def_id):
                raise RepositoryError("This report was deleted.")

            # (3.5) optimistic concurrency: when the caller asserts an expected
            # revision, the current on-disk draft revision must match it exactly.
            # Checked here, under the per-def_id lock and BEFORE any durable move,
            # so a racing writer that advanced the draft first makes this loser
            # fail loudly with no blob written and no pointer moved (no clobber).
            if expected_revision is not None:
                draft_rev: str | None = None
                draft_path = self._draft_path(def_id)
                if draft_path.exists():
                    draft_rev = _revision_of(draft_path.read_text(encoding="utf-8"))
                # Compare by VALUE (not identity): the digest re-read from disk is a
                # distinct str object with the same content hash.
                if draft_rev != expected_revision:
                    raise StaleRevisionError(
                        f"expected revision {expected_revision!r} but current is {draft_rev!r}"
                    )

            # (4) version computation + duplicate check
            current_pointer = self._read_pointer(def_id)
            if expected_version is not None and current_pointer != expected_version:
                raise StaleRevisionError("The publication changed. Reload before saving.")
            version = self._next_version(def_id, current_pointer)
            blob_path = self._blob_path(def_id, version)
            if blob_path.exists():
                existing = blob_path.read_text(encoding="utf-8")
                if existing == yaml_text:
                    # idempotent no-op: same content already published
                    return PublishReceipt(version=version, blob_path=blob_path, pointer_path=self._pointer_path(def_id))
                raise DuplicateVersionError(
                    f"version {version!r} already exists with different content"
                )

            # (5) atomic blob write
            tmp_blob = self._tmp_dir / f"blob-{def_id}-{os.getpid()}-{id(object())}"
            tmp_blob.write_bytes(yaml_text.encode("utf-8"))
            _fsync_path(tmp_blob)
            os.replace(str(tmp_blob), str(blob_path))

            # (6) atomic pointer flip (inode swap via os.replace)
            pp = self._pointer_path(def_id)
            tmp_ptr = self._tmp_dir / f"ptr-{def_id}-{os.getpid()}-{id(object())}"
            tmp_ptr.write_bytes(version.encode("utf-8"))
            _fsync_path(tmp_ptr)
            os.replace(str(tmp_ptr), str(pp))

        finally:
            _flock_unlock(lock_fd)
            os.close(lock_fd)

        return PublishReceipt(version=version, blob_path=blob_path, pointer_path=pp)

    def get_current_version(self, def_id: str) -> str | None:
        """Return the current published version token for def_id, or None."""
        return self._read_pointer(def_id)
