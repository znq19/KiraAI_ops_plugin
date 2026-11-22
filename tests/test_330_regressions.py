"""KiraAI 3.3.0 skill-install contract for kira_ops.

Covers the tighter ``SKILL.md`` parser introduced in 3.3.0:

  V1 valid archive installs; the returned identity is the frontmatter name
  V2 leading blank line is rejected before anything is written
  V3 empty description is rejected
  V4 overwrite of a working skill is refused when the new archive is invalid
  V5 a mismatched frontmatter name is reported as a warning, not a silent rename

Runs against the real framework parser with an isolated temporary data
directory. No network and no user data are touched. Older frameworks skip.
"""

from __future__ import annotations

import asyncio
import importlib.machinery
import importlib.util
import os
import re
import shutil
import sys
import tempfile
import types
import zipfile
from io import BytesIO
from pathlib import Path

FILE = Path(__file__).resolve()
ROOT = FILE.parents[4]
PLUGIN_DIR = FILE.parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packaging.version import Version

version_text = (ROOT / "core" / "config" / "default.py").read_text(encoding="utf-8")
VERSION = re.search(r'^VERSION\s*=\s*["\']([^"\']+)', version_text, re.MULTILINE).group(1)

if Version(VERSION.lstrip("v")) < Version("3.3.0"):
    print(f"SKIP: 3.3 skill-install contract suite on {VERSION}")
    raise SystemExit(0)

PASSED, FAILED = [], []


async def check(name, fn):
    try:
        result = fn()
        if hasattr(result, "__await__"):
            result = await result
        assert result is not False
    except Exception as exc:
        import traceback
        FAILED.append((name, exc))
        print(f"FAIL  {name}: {type(exc).__name__}: {exc}")
        traceback.print_exc()
    else:
        PASSED.append(name)
        print(f"  ok  {name}")


def skill_zip(files):
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def load_plugin_package():
    if "kira_ops" not in sys.modules:
        spec = importlib.machinery.ModuleSpec("kira_ops", None, is_package=True)
        module = importlib.util.module_from_spec(spec)
        module.__path__ = [str(PLUGIN_DIR)]
        sys.modules["kira_ops"] = module
    return importlib.import_module("kira_ops.main")


class Stub:
    store = types.SimpleNamespace(timeout=5)

    def __init__(self, skills_mgr):
        self._sm = skills_mgr

    def _skills_manager(self):
        return self._sm


async def run():
    original_cwd = Path.cwd()
    workspace = Path(tempfile.mkdtemp(prefix="kira_ops_330_suite_"))
    (workspace / "data" / "skills").mkdir(parents=True)
    (workspace / "data" / "config").mkdir(parents=True)
    os.chdir(workspace)
    try:
        from core.agent.skills_mgr import SkillsManager

        ops_main = load_plugin_package()
        from kira_ops.store import store_client as store_mod

        sm = SkillsManager()
        stub = Stub(sm)
        original_download = store_mod.download_direct_file
        original_validator = store_mod.StoreClient.validate_direct_url
        payloads = {"value": b""}

        async def fake_download(url, destination, **kwargs):
            Path(destination).write_bytes(payloads["value"])

        store_mod.download_direct_file = fake_download
        store_mod.StoreClient.validate_direct_url = staticmethod(lambda url: "")

        async def install(blob, name, overwrite=False):
            payloads["value"] = blob
            return await ops_main.KiraOpsPlugin.install_skill_from_url(
                stub, "https://public.test/skill.zip", name, overwrite=overwrite)

        async def v1_valid_install_returns_frontmatter_identity():
            blob = skill_zip({"dir_name/SKILL.md":
                              "---\nname: real_name\ndescription: real desc\n---\nbody\n"})
            result = await install(blob, "dir_name")
            assert result.get("ok"), result
            assert result["name"] == "real_name", result
            assert result["description"] == "real desc", result
            assert result["enabled"] is True, result
            assert "warning" in result, result
            assert any(s.name == "real_name" for s in sm.skills_info), sm.skills_info
            assert (workspace / "data" / "skills" / "dir_name" / "SKILL.md").is_file()

        async def v2_leading_blank_line_rejected():
            blob = skill_zip({"blank/SKILL.md":
                              "\n---\nname: blank\ndescription: d\n---\nbody\n"})
            result = await install(blob, "blank")
            assert not result.get("ok"), result
            assert "missing_frontmatter" in result["error"], result
            assert not (workspace / "data" / "skills" / "blank").exists()

        async def v3_empty_description_rejected():
            blob = skill_zip({"empty/SKILL.md":
                              "---\nname: empty\ndescription: '   '\n---\nbody\n"})
            result = await install(blob, "empty")
            assert not result.get("ok"), result
            assert "invalid_metadata" in result["error"], result
            assert not (workspace / "data" / "skills" / "empty").exists()

        async def v4_invalid_overwrite_keeps_working_version():
            good = skill_zip({"keep/SKILL.md":
                              "---\nname: keep\ndescription: good\n---\nGOOD\n"})
            first = await install(good, "keep")
            assert first.get("ok"), first
            bad = skill_zip({"keep/SKILL.md":
                             "---\nname: keep\n---\nBAD\n"})
            second = await install(bad, "keep", overwrite=True)
            assert not second.get("ok"), second
            text = (workspace / "data" / "skills" / "keep" / "SKILL.md").read_text(
                encoding="utf-8")
            assert "GOOD" in text and "BAD" not in text, text
            assert any(s.name == "keep" for s in sm.scan_skill_dir())

        await check("V1 valid install returns the frontmatter identity",
                    v1_valid_install_returns_frontmatter_identity)
        await check("V2 leading blank line is rejected before writing",
                    v2_leading_blank_line_rejected)
        await check("V3 empty description is rejected",
                    v3_empty_description_rejected)
        await check("V4 invalid overwrite keeps the working version",
                    v4_invalid_overwrite_keeps_working_version)

        store_mod.download_direct_file = original_download
        store_mod.StoreClient.validate_direct_url = original_validator
    finally:
        os.chdir(original_cwd)
        shutil.rmtree(workspace, ignore_errors=True)

    print(f"\npassed: {len(PASSED)}  failed: {len(FAILED)}")
    if FAILED:
        raise SystemExit(1)
    print("ALL OK")


if __name__ == "__main__":
    asyncio.run(run())
