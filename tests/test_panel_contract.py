"""Static contract tests for the WebUI panel.

The panel is plain JS talking to the plugin APIs, so nothing in the Python
suites would notice a renamed backend field, a missing element id or a syntax
error - the page would simply stop working in the browser. These checks are
cheap and offline:

  * every DOM id / data-path / API path the JS uses must exist on the other side
  * every field the JS reads out of an API payload must exist in that payload
    (payloads are built from the real plugin instance with stub managers)
  * the JS must parse (uses node when available, skipped otherwise)

Run from the KiraAI root:
    python data/plugins/kira_ops/tests/test_panel_contract.py
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
import sys
import tempfile
import types
from pathlib import Path

FILE = Path(__file__).resolve()
ROOT = FILE.parents[4]
PLUGIN_DIR = FILE.parents[1]
WEB = PLUGIN_DIR / "web"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if "plugins" not in sys.modules:
    pkg = types.ModuleType("plugins")
    pkg.__path__ = [str(ROOT / "data" / "plugins")]
    sys.modules["plugins"] = pkg

FAILED = []
PASSED = []


def check(name, fn):
    try:
        fn()
    except AssertionError as exc:
        FAILED.append((name, exc))
        print(f"FAIL  {name}: {exc}")
    except Exception as exc:  # noqa: BLE001
        import traceback
        FAILED.append((name, exc))
        print(f"FAIL  {name}: {type(exc).__name__}: {exc}")
        traceback.print_exc()
    else:
        PASSED.append(name)
        print(f"  ok  {name}")


class FakeConfig(dict):
    def save_config(self):
        pass

    def get_config(self, path, default=None):
        cur = self
        for part in str(path or "").split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                return default
        return cur


class FakePluginInfo:
    plugin_id = "kira_ops"
    display_name = "Kira Ops Console"
    version = "1.1.0"
    author = "t"
    description = "d"
    repo = None
    core_version = None
    tags = []
    builtin = False
    uninstallable = True
    status = "ready"
    error = None


class FakePluginMgr:
    plugin_dir = PLUGIN_DIR.parent

    def list_plugins(self):
        return [FakePluginInfo()]

    def is_plugin_enabled(self, pid):
        return pid in ("kira_ops",)

    def has_plugin(self, pid):
        return pid == "kira_ops"

    def get_plugin_info(self, pid):
        return FakePluginInfo() if pid == "kira_ops" else None

    def get_plugin_config(self, pid):
        return {}

    def get_plugin_schema(self, pid):
        return []

    def get_plugin_load_errors(self):
        return {}

    def get_plugin_inst(self, pid):
        return None


class FakeCtx:
    def __init__(self, data_dir):
        self.config = FakeConfig({"bot_config": {"bot": {}}, "providers": {}})
        self.plugin_mgr = FakePluginMgr()
        self.provider_mgr = types.SimpleNamespace(get_all_providers=lambda: {})
        self.session_mgr = types.SimpleNamespace(
            get_session_info=lambda: [],
            get_effective_capabilities=lambda *a, **k: {},
            chat_memory={})
        self.message_processor = types.SimpleNamespace(mcp_manager=None, skills_manager=None)
        self._data_dir = data_dir

    def get_plugin_data_dir(self):
        return self._data_dir

    def get_lang(self):
        return "zh"


def load_plugin():
    """Instantiate the plugin against stub managers (no framework boot needed)."""
    import importlib
    main_mod = importlib.import_module("plugins.kira_ops.main")
    tmp = Path(tempfile.mkdtemp(prefix="kira_ops_panel_"))
    ctx = FakeCtx(tmp)
    schema = json.loads((PLUGIN_DIR / "schema.json").read_text(encoding="utf-8"))
    defaults = {section: {k: f.get("default") for k, f in (spec.get("fields") or {}).items()}
                for section, spec in schema.items() if spec.get("type") == "section"}
    plugin = main_mod.KiraOpsPlugin(ctx, defaults)
    plugin.refresh_settings(defaults)
    plugin.engine.apply_settings(defaults)
    return plugin, tmp


def main():
    print("== kira_ops panel contract ==")
    js = (WEB / "app.js").read_text(encoding="utf-8")
    html = (WEB / "index.html").read_text(encoding="utf-8")

    # ---------------------------------------------------------------- syntax
    def js_parses():
        node = shutil.which("node")
        if not node:
            print("      (node not available - skipped)")
            return
        proc = subprocess.run([node, "--check", str(WEB / "app.js")],
                              capture_output=True, text=True)
        assert proc.returncode == 0, f"app.js failed to parse: {proc.stderr[:300]}"
    check("app.js parses (node --check)", js_parses)

    # ---------------------------------------------------------------- ids
    def ids_exist():
        ids = set(re.findall(r"\$\('#([A-Za-z0-9_-]+)'\)", js))
        ids |= set(re.findall(r"getElementById\('([A-Za-z0-9_-]+)'\)", js))
        missing = sorted(i for i in ids if f'id="{i}"' not in html)
        assert not missing, f"ids used by app.js but missing in index.html: {missing}"
        assert ids, "no ids found - regex drift?"
    check("every element id used by app.js exists in index.html", ids_exist)

    def panes_exist():
        panes = set(re.findall(r'data-tab="([a-z]+)"', html))
        missing = sorted(p for p in panes if f'id="pane-{p}"' not in html)
        assert not missing, f"tabs without a pane: {missing}"
        assert panes == {"access", "risk", "control", "protected", "backup", "audit", "store"}, panes
    check("every tab has a matching pane", panes_exist)

    # ---------------------------------------------------------------- assets
    def manifest_icon_asset():
        """If the manifest declares an icon it must ship and stay inside the root."""
        manifest = json.loads((PLUGIN_DIR / "manifest.json").read_text(encoding="utf-8"))
        for field in ("icon", "icon-dark"):
            rel = manifest.get(field)
            if not rel:
                continue
            asset = (PLUGIN_DIR / rel).resolve()
            assert asset.is_file(), f"manifest {field} missing: {rel}"
            assert asset.is_relative_to(PLUGIN_DIR.resolve()), f"{field} escapes plugin root: {rel}"
            assert asset.suffix.lower() in (".png", ".svg", ".jpg", ".jpeg", ".webp"), rel
    check("the manifest icon ships with the plugin", manifest_icon_asset)

    def menu_icon_asset():
        """The sidebar icon must ship with the plugin and stay inside the root."""
        main_py = (PLUGIN_DIR / "main.py").read_text(encoding="utf-8")
        paths = re.findall(r'icon="([^"]+\.svg)"', main_py)
        assert paths, "no SVG menu icon referenced in main.py"
        for rel in paths:
            asset = (PLUGIN_DIR / rel).resolve()
            assert asset.is_file(), f"menu icon missing: {rel}"
            assert asset.is_relative_to(PLUGIN_DIR.resolve()), f"icon escapes plugin root: {rel}"
            text = asset.read_text(encoding="utf-8")
            assert text.lstrip().startswith("<svg"), f"{rel} is not an SVG"
            assert "currentColor" in text, f"{rel} should inherit the menu colour (currentColor)"
    check("the sidebar icon ships with the plugin", menu_icon_asset)

    # ---------------------------------------------------------------- api paths
    def api_paths_exist():
        main_py = (PLUGIN_DIR / "main.py").read_text(encoding="utf-8")
        registered = set(re.findall(r'@register\.api\(method="(\w+)", path="([^"]+)"', main_py))
        registered_paths = {p for _m, p in registered}
        called = set(re.findall(r"api\('(/[^'?]+)", js))
        missing = sorted(c for c in called if c not in registered_paths)
        assert not missing, f"app.js calls unregistered API paths: {missing}"
    check("every API path the panel calls is registered", api_paths_exist)

    # ---------------------------------------------------------------- payload fields
    plugin, tmp = load_plugin()
    try:
        overview = asyncio.run(plugin._panel_payload())  # rc1 起会话枚举异步化
        backups = {"ok": True, "items": plugin.backups.list(5)}
        audit = {"ok": True, "items": plugin.audit.tail(5)}
        config_resp = {"ok": True, "config": plugin.cfg, "warnings": plugin.warnings}

        def payload_fields():
            used = set(re.findall(r"\boverview\.([a-zA-Z_]+)", js))
            missing = sorted(f for f in used if f not in overview)
            assert not missing, f"overview fields read by the panel but absent: {missing}"
            used = set(re.findall(r"\bcounts\.([a-zA-Z_]+)", js))
            missing = sorted(f for f in used if f not in overview["counts"])
            assert not missing, f"counts fields missing: {missing}"
            used = set(re.findall(r"\bagent\.([a-zA-Z_]+)", js))
            missing = sorted(f for f in used if f not in overview["agent"])
            assert not missing, f"agent fields missing: {missing}"
            used = set(re.findall(r"\bperm\.([a-zA-Z_]+)", js))
            missing = sorted(f for f in used if f not in overview["permission"])
            assert not missing, f"permission fields missing: {missing}"
        check("overview payload carries every field the panel reads", payload_fields)

        def config_fields():
            assert "config" in config_resp and "warnings" in config_resp
            paths = set(re.findall(r"field(?:Text|Switch|List|Select)\('([a-zA-Z_.]+)'", js))
            paths |= set(re.findall(r"getPath\(cfg, '([a-zA-Z_.]+)'\)", js))
            assert paths, "no cfg paths found - regex drift?"
            missing = [p for p in sorted(paths)
                       if _resolve(plugin.cfg, p) is None and p not in _schema_paths()]
            assert not missing, f"panel binds config paths the schema does not define: {missing}"
        check("every config path bound in the panel exists in the schema", config_fields)

        def table_fields():
            item = backups["items"][0] if backups["items"] else {
                "id": "", "created": "", "label": "", "files": 0, "reason": "", "applied": False}
            # scope the extraction to the render function so unrelated local
            # variables in other functions do not produce false positives
            backups_js = _function_body(js, "renderBackups")
            used = set(re.findall(r"\bb\.([a-zA-Z_]+)", backups_js))
            # "foreign" is optional on purpose: it is only present when a
            # rollback point came from another instance, which keeps the
            # common payload small. Reading it when absent yields undefined.
            optional = {"foreign"}
            missing = sorted(u for u in used - optional if u not in item)
            assert not missing, f"backup fields missing: {missing}"
            row = audit["items"][0] if audit["items"] else {
                "ts": "", "domain": "", "action": "", "ok": True, "err": "", "note": ""}
            audit_js = _function_body(js, "renderAudit")
            used = set(re.findall(r"\br\.([a-zA-Z_]+)", audit_js))
            missing = sorted(u for u in used if u not in row)
            assert not missing, f"audit fields missing: {missing}"
        check("backup + audit rows carry the fields the tables read", table_fields)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    print(f"passed: {len(PASSED)}  failed: {len(FAILED)}")
    if FAILED:
        for name, exc in FAILED:
            print(f"  FAILED: {name}: {exc}")
        sys.exit(1)
    print("ALL OK")


def _function_body(source: str, name: str) -> str:
    """Return the body of ``function name() {...}`` (brace matched)."""
    start = source.find(f"function {name}(")
    if start < 0:
        return ""
    i = source.find("{", start)
    depth = 0
    for j in range(i, len(source)):
        if source[j] == "{":
            depth += 1
        elif source[j] == "}":
            depth -= 1
            if depth == 0:
                return source[i:j + 1]
    return source[i:]


def _resolve(cfg, dotted):
    cur = cfg
    for part in dotted.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


def _schema_paths():
    schema = json.loads((PLUGIN_DIR / "schema.json").read_text(encoding="utf-8"))
    out = set()
    for section, spec in schema.items():
        for key in (spec.get("fields") or {}):
            out.add(f"{section}.{key}")
    return out


if __name__ == "__main__":
    main()
