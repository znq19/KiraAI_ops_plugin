"""Installer helpers for kira_ops.

All archive handling is delegated to the framework's official installer
(``core.plugin.plugin_installer``) so kira_ops never re-implements the
protection it already ships: 50 MiB size cap, 10 000 entry cap, 100:1
compression-ratio cap, 512 KiB central-directory cap, zip-slip guard,
GitHub mirror ranking and staged extraction.

What this module *adds* on top of the framework:

* **the activation step.** ``prepare_plugin_reload()`` must run before the
  plugin is re-imported, otherwise a multi-file plugin keeps executing its
  cached submodules (``caps/``, ``core/``, ...) while only ``main.py`` is
  re-executed. The framework's own WebUI update flow does this; a bare
  ``load_plugin_from_dir()`` after replacing files does not.
* **directory-level rollback.** A failed update restores the previous build
  and re-activates it instead of leaving a half-replaced plugin behind.
* **a guarded extractor for skills**, which the framework does not install.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import threading
import time
import uuid
import zipfile
from pathlib import Path
from typing import Optional

from core.plugin import logger
from core.utils.path_utils import get_data_path
from core.plugin.plugin_installer import (
    MAX_PLUGIN_ARCHIVE_BYTES,
    MAX_PLUGIN_ARCHIVE_COMPRESSION_RATIO,
    MAX_PLUGIN_ARCHIVE_FILE_COUNT,
    install_from_github,
    install_from_zip,
    install_requirements,
)

# ---------------------------------------------------------------------------
# small async helpers
# ---------------------------------------------------------------------------


async def _to_thread(func, *args):
    return await asyncio.to_thread(func, *args)


def _pypi_mirror(plugin_mgr) -> Optional[str]:
    getter = getattr(plugin_mgr, "_get_pypi_mirror", None)
    if callable(getter):
        try:
            return getter()
        except Exception:
            return None
    return None


def _existing_plugin_dir(plugin_mgr, plugin_id: str) -> Optional[Path]:
    candidate = Path(plugin_mgr.plugin_dir) / plugin_id
    return candidate if candidate.is_dir() else None


# ---------------------------------------------------------------------------
# rollback support
# ---------------------------------------------------------------------------


def _rollback_root() -> Path:
    """Scratch space for rollback copies.

    It must live outside the plugins directory: the framework scans
    ``data/plugins/*`` at startup and would try to load a leftover copy as a
    plugin (reporting it as a broken plugin in the WebUI).
    """
    return get_data_path() / "temp" / "kira_ops_rollback"


async def _stage_backup(dest: Optional[Path]) -> Optional[Path]:
    """Copy the current plugin directory aside so a failed update can roll back."""
    if dest is None or not dest.exists():
        return None
    root = _rollback_root()
    root.mkdir(parents=True, exist_ok=True)
    backup = root / f"{dest.name}-{int(time.time())}-{uuid.uuid4().hex[:6]}"
    try:
        shutil.rmtree(backup, ignore_errors=True)
        await _to_thread(shutil.copytree, dest, backup)
        return backup
    except Exception as exc:
        logger.warning(f"[kira_ops] could not stage a rollback copy of {dest}: {exc}")
        return None


async def _restore_backup(plugin_mgr, plugin_id: str, dest: Path, backup: Optional[Path]) -> bool:
    if backup is None or not backup.exists():
        return False
    try:
        if dest.exists():
            await _to_thread(shutil.rmtree, dest, True)
        await _to_thread(shutil.move, str(backup), str(dest))
        await plugin_mgr.prepare_plugin_reload(plugin_id)
        await plugin_mgr.load_plugin_from_dir(dest)
        logger.warning(f"[kira_ops] rolled back plugin '{plugin_id}' to the previous build")
        return True
    except Exception as exc:
        logger.error(f"[kira_ops] rollback of '{plugin_id}' failed: {exc}")
        return False


def _drop_backup(backup: Optional[Path]) -> None:
    if backup is not None:
        shutil.rmtree(backup, ignore_errors=True)
        try:
            parent = backup.parent
            if parent.name == "kira_ops_rollback" and not any(parent.iterdir()):
                parent.rmdir()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# activation
# ---------------------------------------------------------------------------


async def _activate(plugin_mgr, plugin_id: str, dest: Path) -> str:
    """Re-import a freshly installed plugin directory and verify its identity."""
    # Purge the plugin's cached modules first: without this only main.py is
    # re-executed and every submodule keeps running the previous version.
    if plugin_mgr.has_plugin(plugin_id):
        await plugin_mgr.prepare_plugin_reload(plugin_id)
    loaded = await plugin_mgr.load_plugin_from_dir(dest)
    if not loaded:
        raise RuntimeError(f"plugin files were installed to {dest} but loading failed")
    if str(loaded) != str(plugin_id):
        raise RuntimeError(
            f"plugin identity changed after install: expected '{plugin_id}', loaded '{loaded}'"
        )
    return str(loaded)


async def _install_and_activate(
    plugin_mgr,
    plugin_id: str,
    *,
    update: bool,
    runner,
) -> dict:
    """Run an installer coroutine, activate the result, roll back on failure.

    ``runner(dest, target_dir)`` performs the download + extraction + move using
    the framework installer and returns the installed directory path.
    """
    dest = _existing_plugin_dir(plugin_mgr, plugin_id) if update else None
    backup = await _stage_backup(dest)
    if update and backup is None:
        return {
            "ok": False,
            "plugin_id": plugin_id,
            "error": "could not stage a rollback copy; update refused before replacing any files",
        }
    try:
        installed = await runner(dest)
        warnings = await install_requirements(Path(installed), pypi_mirror=_pypi_mirror(plugin_mgr))
        for warning in warnings or []:
            logger.warning(f"[kira_ops] dependency warning for {plugin_id}: {warning}")
        await _activate(plugin_mgr, plugin_id, Path(installed))
        info = plugin_mgr.get_plugin_info(plugin_id)
        result = {
            "ok": True,
            "plugin_id": plugin_id,
            "version": (getattr(info, "version", "") or ""),
            "status": (getattr(info, "status", "") or "unknown"),
            "directory": str(installed),
        }
        if warnings:
            result["dependency_warnings"] = list(warnings)
        _drop_backup(backup)
        return result
    except Exception as exc:
        rolled_back = False
        if update:
            rolled_back = await _restore_backup(plugin_mgr, plugin_id, dest, backup)
        _drop_backup(backup)
        message = f"{type(exc).__name__}: {exc}"
        if update and not rolled_back:
            message += " (previous build could not be restored)"
        elif rolled_back:
            message += " (previous build restored)"
        return {"ok": False, "plugin_id": plugin_id, "error": message}


async def install_plugin_from_repo(
    plugin_mgr,
    repo: str,
    plugin_id: str,
    *,
    update: bool = False,
    gh_proxy: str = "auto",
    commit_sha: Optional[str] = None,
) -> dict:
    """Install or update a plugin from a GitHub repository URL."""
    plugins_dir = Path(plugin_mgr.plugin_dir)

    async def runner(dest: Optional[Path]) -> Path:
        return await install_from_github(
            repo,
            plugins_dir,
            gh_proxy=gh_proxy or "auto",
            commit_sha=commit_sha,
            is_plugin_installed=None if update else (lambda pid: plugin_mgr.has_plugin(pid)),
            target_dir=dest,
            expected_plugin_id=plugin_id if update else None,
        )

    result = await _install_and_activate(plugin_mgr, plugin_id, update=update, runner=runner)
    if result.get("ok"):
        result["source"] = repo
    return result


async def install_plugin_from_zip_bytes(
    plugin_mgr,
    zip_bytes: bytes,
    plugin_id: str,
    *,
    update: bool = False,
    preferred_name: str = "",
    source: str = "",
) -> dict:
    """Install or update a plugin from an in-memory zip archive."""
    plugins_dir = Path(plugin_mgr.plugin_dir)

    async def runner(dest: Optional[Path]) -> Path:
        return await install_from_zip(
            zip_bytes,
            plugins_dir,
            preferred_name=preferred_name or plugin_id,
            is_plugin_installed=None if update else (lambda pid: plugin_mgr.has_plugin(pid)),
            target_dir=dest,
            expected_plugin_id=plugin_id if update else None,
        )

    result = await _install_and_activate(plugin_mgr, plugin_id, update=update, runner=runner)
    if result.get("ok") and source:
        result["source"] = source
    return result


async def install_plugin_from_direct_url(
    plugin_mgr,
    url: str,
    plugin_id: str,
    *,
    update: bool = False,
    validate_url=None,
    timeout: float = 60.0,
) -> dict:
    """Install from an arbitrary https zip URL (SSRF-guarded, size-capped)."""
    from .store_client import download_direct_file

    temp_dir = Path(plugin_mgr.plugin_dir).parent / "temp"
    temp_zip = temp_dir / f"kira_ops_{plugin_id}_{int(time.time())}_{uuid.uuid4().hex[:8]}.zip"
    try:
        temp_dir.mkdir(parents=True, exist_ok=True)
        try:
            await download_direct_file(
                url, temp_zip, validate_url=validate_url,
                timeout=timeout, max_bytes=MAX_PLUGIN_ARCHIVE_BYTES,
            )
        except ValueError as exc:
            return {"ok": False, "plugin_id": plugin_id, "error": f"download rejected: {exc}"}
        except Exception as exc:
            return {"ok": False, "plugin_id": plugin_id, "error": f"download failed: {exc}"}
        payload = await _to_thread(temp_zip.read_bytes)
    finally:
        temp_zip.unlink(missing_ok=True)
    return await install_plugin_from_zip_bytes(
        plugin_mgr, payload, plugin_id, update=update, source=url
    )


# ---------------------------------------------------------------------------
# skills (the framework ships no skill installer)
# ---------------------------------------------------------------------------


def _validate_zip_directory(zip_path: Path) -> None:
    """Best-effort central-directory size check (mirrors the framework cap)."""
    from core.plugin.plugin_installer import MAX_PLUGIN_ARCHIVE_CENTRAL_DIRECTORY_BYTES

    try:
        with open(zip_path, "rb") as archive:
            end_record = zipfile._EndRecData(archive)
        if not end_record:
            raise ValueError("Not a valid zip archive")
        directory_size = (
            end_record[zipfile._ECD_SIZE]
            + end_record[zipfile._ECD_OFFSET]
            + 22
            + len(end_record[zipfile._ECD_COMMENT])
        )
        if directory_size > MAX_PLUGIN_ARCHIVE_CENTRAL_DIRECTORY_BYTES:
            raise ValueError("Archive central directory exceeds the 512 KiB size limit")
    except ValueError:
        raise
    except Exception as exc:  # pragma: no cover - defensive, never blocks installs
        logger.debug(f"[kira_ops] central directory pre-check skipped: {exc}")


def extract_zip_safely(zip_path: Path, staging: Path, *, expected: Optional[str] = None) -> Path:
    """Extract an archive with the same guards the framework uses for plugins.

    Returns the directory that contains ``expected`` (default: the shallowest
    ``manifest.json`` / ``SKILL.md`` found in the archive).
    """
    zip_path = Path(zip_path)
    staging = Path(staging)
    if zip_path.stat().st_size > MAX_PLUGIN_ARCHIVE_BYTES:
        raise ValueError("archive exceeds the 50 MiB size limit")
    _validate_zip_directory(zip_path)
    staging.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(zip_path) as zf:
        members = zf.infolist()
        if len(members) > MAX_PLUGIN_ARCHIVE_FILE_COUNT:
            raise ValueError(f"archive exceeds the {MAX_PLUGIN_ARCHIVE_FILE_COUNT} file limit")
        total = 0
        names = [m.filename for m in members]
        for item in members:
            if item.is_dir():
                continue
            if item.file_size > MAX_PLUGIN_ARCHIVE_BYTES:
                raise ValueError("an archive entry exceeds the 50 MiB size limit")
            if item.file_size and (
                item.compress_size == 0
                or item.file_size / item.compress_size > MAX_PLUGIN_ARCHIVE_COMPRESSION_RATIO
            ):
                raise ValueError(
                    f"archive entry exceeds the {MAX_PLUGIN_ARCHIVE_COMPRESSION_RATIO}:1 "
                    "compression ratio limit"
                )
            total += item.file_size
            if total > MAX_PLUGIN_ARCHIVE_BYTES:
                raise ValueError("archive exceeds the 50 MiB uncompressed size limit")

        tops = {n.split("/")[0] for n in names if n.split("/")[0]}
        prefix = list(tops)[0] + "/" if len(tops) == 1 else ""
        for item in members:
            rel = item.filename[len(prefix):]
            if not rel or rel.endswith("/"):
                continue
            target = (staging / rel).resolve()
            if not str(target).startswith(str(staging.resolve()) + os.sep):
                raise ValueError(f"unsafe path in archive (zip-slip blocked): {item.filename}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(zf.read(item.filename))

    marker = expected or "SKILL.md"
    matches = sorted(staging.rglob(marker), key=lambda p: len(p.relative_to(staging).parts))
    if not matches:
        raise ValueError(f"{marker} was not found in the archive")
    return matches[0].parent


_SKILL_INSTALL_LOCK = threading.Lock()


def validate_skill_dir(skill_root: Path) -> None:
    """Refuse a skill the running framework would not load.

    KiraAI 3.3.0 tightened ``SKILL.md`` parsing: the frontmatter must start at
    byte 0 (a leading blank line is now rejected) and ``name``/``description``
    must be non-empty strings. Without this pre-check an archive could be
    installed "successfully" while the framework silently hides it, and with
    ``overwrite=True`` it would also have replaced a working version.

    Validation runs on the extracted copy *before* the atomic swap, so a bad
    archive never touches ``data/skills``. The framework's own parser is used
    when available (older versions fall back to ``parse_skill_info``), keeping
    this check identical to what the host applies at scan time.
    """
    root = Path(skill_root)
    skill_md = root / "SKILL.md"
    if not skill_md.is_file():
        raise ValueError("SKILL.md was not found at the skill root")
    try:
        from core.agent.skills_mgr import SkillsManager
    except Exception:  # pragma: no cover - framework too old to expose it
        return
    try:
        text = skill_md.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"SKILL.md is not valid UTF-8: {exc}") from exc

    parser = getattr(SkillsManager, "parse_skill_content", None)
    if callable(parser):
        info = parser(text, root)  # raises SkillValidationError(code, message)
        if not str(getattr(info, "name", "") or "").strip():
            raise ValueError("SKILL.md declares an empty skill name")
        return
    legacy = getattr(SkillsManager, "parse_skill_info", None)
    if callable(legacy) and legacy(root) is None:
        raise ValueError("SKILL.md is not recognized by this KiraAI version")


def install_skill_dir(skills_root: Path, src_root: Path, skill_name: str,
                      overwrite: bool = False) -> Path:
    """Serialize target replacement; calls run in worker threads, not the loop."""
    with _SKILL_INSTALL_LOCK:
        return _install_skill_dir_locked(skills_root, src_root, skill_name, overwrite)


def _install_skill_dir_locked(skills_root: Path, src_root: Path, skill_name: str,
                              overwrite: bool = False) -> Path:
    """Install a skill with a same-filesystem atomic swap and rollback.

    Staging and rollback directories start with '_' so SkillsManager.scan_skill_dir
    ignores them even if a refresh runs concurrently with the replacement.
    """
    skills_root = Path(skills_root).resolve()
    skills_root.mkdir(parents=True, exist_ok=True)
    target = (skills_root / skill_name).resolve()
    if not target.is_relative_to(skills_root) or target.parent != skills_root:
        raise ValueError(f"illegal skill name: {skill_name!r}")
    if target.exists() and not overwrite:
        raise FileExistsError(f"skill '{skill_name}' already exists (pass overwrite=true to replace)")

    token = uuid.uuid4().hex
    staging = skills_root / f"_kira_ops_install_{token}"
    previous = skills_root / f"_kira_ops_previous_{token}"
    old_moved = False
    try:
        # Copy first: the archive workspace may be on another filesystem.
        shutil.copytree(src_root, staging)
        if target.exists():
            os.replace(target, previous)
            old_moved = True
        try:
            os.replace(staging, target)
        except Exception:
            if old_moved:
                os.replace(previous, target)
                old_moved = False
            raise
        if old_moved:
            shutil.rmtree(previous, ignore_errors=True)
            old_moved = False
        return target
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        # If an unexpected failure happened after the old directory was moved,
        # never leave the old version hidden in a rollback directory.
        if old_moved and previous.exists() and not target.exists():
            os.replace(previous, target)


async def install_skill_from_zip_bytes(
    skills_root: Path,
    zip_bytes: bytes,
    skill_name: str,
    *,
    overwrite: bool = False,
) -> Path:
    """Validate + extract a skill archive, then move it into data/skills/."""
    import tempfile

    if len(zip_bytes) > MAX_PLUGIN_ARCHIVE_BYTES:
        raise ValueError("skill archive exceeds the 50 MiB size limit")
    workspace = Path(tempfile.mkdtemp(prefix="kira_ops_skill_"))
    try:
        zip_path = workspace / "skill.zip"
        await _to_thread(zip_path.write_bytes, zip_bytes)
        root = await _to_thread(extract_zip_safely, zip_path, workspace / "extract")
        await _to_thread(validate_skill_dir, root)
        return await _to_thread(install_skill_dir, skills_root, root, skill_name, overwrite)
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


def read_skill_marker(json_path: Path) -> dict:
    try:
        return json.loads(Path(json_path).read_text(encoding="utf-8"))
    except Exception:
        return {}
