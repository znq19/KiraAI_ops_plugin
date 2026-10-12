"""Integration tests driven by the REAL framework (no stubs).

The other suites use fake managers, which is why a class-vs-instance bug and a
stale-submodule bug once shipped unnoticed: stubs prove the logic, not the
contract. This suite boots the actual KiraAI objects (KiraConfig,
DatabaseService, ProviderManager, FuncToolManager, PersonaManager,
SessionManager, MCPManager, SkillsManager, PluginManager), loads the plugin the
way the framework does, and then exercises the tools against those managers.

Run from the KiraAI root:
    python data/plugins/kira_ops/tests/test_integration_real.py

No network access is required; a throw-away plugin and the plugin's own data
directory are the only things written.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
import tempfile
import types
import uuid
import zipfile
from io import BytesIO
from pathlib import Path
def _ctx_extra() -> dict:
    """按真实 PluginContext 签名延迟求值（调用时 core 已可导入）：
    2.x 带 sticker_manager；3.0 改名 sticker_mgr 且新增 7 个必填字段。"""
    try:
        import inspect as _i
        from core.plugin import PluginContext as _P
        params = _i.signature(_P.__init__).parameters
        out = {f: None for f in ("prompt_mgr", "skills_mgr", "mcp_mgr", "message_history",
                                 "image_desc_cache", "session_buffer_mgr", "message_delivery")
               if f in params}
        if "sticker_manager" in params:
            out["sticker_manager"] = None
        else:
            out["sticker_mgr"] = None
        return out
    except Exception:
        return {"sticker_manager": None}

from types import SimpleNamespace

FILE = Path(__file__).resolve()
ROOT = FILE.parents[4]
PLUGIN_DIR = FILE.parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if "plugins" not in sys.modules:
    pkg = types.ModuleType("plugins")
    pkg.__path__ = [str(ROOT / "data" / "plugins")]
    sys.modules["plugins"] = pkg

from core.agent.func_tool_manager import FuncToolManager  # noqa: E402
from core.agent.mcp_mgr import MCPManager  # noqa: E402
from core.agent.skills_mgr import SkillsManager  # noqa: E402
from core.chat.message_utils import KiraCustomEvent  # noqa: E402
from core.chat.session_manager import SessionManager  # noqa: E402
from core.config import KiraConfig  # noqa: E402
from core.db.db_mgr import DatabaseManager  # noqa: E402
from core.db.service import DatabaseService  # noqa: E402
from core.event_bus import EventBus  # noqa: E402
from core.persona import PersonaManager  # noqa: E402
from core.plugin import PluginManager  # noqa: E402
from core.plugin.plugin_context import PluginContext  # noqa: E402
from core.provider import ProviderManager  # noqa: E402
from core.statistics import Statistics  # noqa: E402
from core.utils.path_utils import get_config_path, get_data_path  # noqa: E402

FAILED = []
PASSED = []


def _assert(condition, detail=""):
    if not condition:
        raise AssertionError(detail or "assertion failed")


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


class Ev:
    """Minimal batch event - the tools only read .sid / .messages."""

    def __init__(self, sid="qq:gm:980001", uid="980001"):
        self.sid = sid
        self.messages = [SimpleNamespace(sender=SimpleNamespace(user_id=uid))]
        self.session = None


def plugin_zip(plugin_id: str, value: int) -> bytes:
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{plugin_id}/manifest.json", json.dumps({
            "plugin_id": plugin_id, "display_name": plugin_id, "version": "9.9.9",
            "author": "test", "description": "installer harness",
        }))
        zf.writestr(f"{plugin_id}/main.py",
                    "from core.plugin import BasePlugin\n"
                    "from . import impl\n\n"
                    "class P(BasePlugin):\n"
                    "    async def initialize(self):\n        pass\n"
                    "    async def terminate(self):\n        pass\n")
        zf.writestr(f"{plugin_id}/impl.py", f"VALUE = {value}\n")
    return buf.getvalue()


async def boot():
    kira_config = KiraConfig()
    db_manager = DatabaseManager(
        f"sqlite+aiosqlite:///{(get_data_path() / 'kira_ops_test.db').as_posix()}")
    await db_manager.init()
    db = DatabaseService(db_manager)
    await db.init_tables()

    ctx = PluginContext(
        db=db,
        config=kira_config,
        event_bus=EventBus(Statistics(), asyncio.Queue()),
        provider_mgr=ProviderManager(db, kira_config),
        tool_mgr=FuncToolManager(kira_config),
        adapter_mgr=None,
        persona_mgr=PersonaManager(db),
        session_mgr=SessionManager(db, kira_config),
        **_ctx_extra(),
        message_processor=SimpleNamespace(
            mcp_manager=MCPManager(FuncToolManager(kira_config)),
            skills_manager=SkillsManager()),
    )
    pm = PluginManager(ctx)
    ctx.plugin_mgr = pm
    return ctx, pm, kira_config


def main():
    print("== kira_ops real-framework integration ==")
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def run(coro):
        return loop.run_until_complete(coro)

    ctx, pm, kira_config = run(boot())

    # ---------------------------------------------------------------- load
    loaded = run(pm.load_plugin_from_dir(PLUGIN_DIR, auto_install=False))
    check("framework loads the plugin from disk",
          lambda: _assert(loaded == "kira_ops", f"loaded={loaded}"))

    info = pm.get_plugin_info("kira_ops")
    check("plugin reports status=ready",
          lambda: _assert(info.status == "ready", f"{info.status} {info.error}"))

    run(pm.init_plugin("kira_ops"))
    inst = pm.get_plugin_inst("kira_ops")
    check("plugin instance exists", lambda: _assert(inst is not None))

    # Hermetic: the plugin config file persists between runs, so pin the runtime
    # settings the assertions depend on instead of trusting whatever is on disk.
    cfg_dump = pm.get_plugin_config("kira_ops")
    cfg_dump["master"] = {"enabled": True, "panic_lock": False}
    cfg_dump["access"] = {"allow_sessions": [], "deny_sessions": [], "readonly_sessions": []}
    cfg_dump["risk"] = dict(cfg_dump.get("risk") or {},
                            level="standard", high_risk_sessions=[])
    inst.cfg = cfg_dump
    inst.refresh_settings(cfg_dump)
    inst.engine.apply_settings(cfg_dump)
    inst.backups.apply_settings(cfg_dump.get("backup") or {})
    inst.audit.apply_settings(cfg_dump.get("audit") or {})
    inst.store.apply_settings(cfg_dump.get("store") or {})
    inst.audit.audit_reads = True

    check("7 tools registered by the framework",
          lambda: _assert(len(pm.get_plugin_tools("kira_ops")) == 7,
                          sorted(pm.get_plugin_tools("kira_ops"))))
    check("12 capability domains", lambda: _assert(len(inst.caps) == 12, sorted(inst.caps)))
    # note: no assertion on *values* here - the config file persists between
    # runs, so only the presence of every schema section is guaranteed.
    check("plugin config filled from schema",
          lambda: _assert(
              {"master", "access", "risk", "control", "protected", "backup", "audit", "store"}
              <= set(cfg_dump.keys()), sorted(cfg_dump.keys())))

    ev = Ev()
    item = {"n": 0}

    # ---------------------------------------------------------------- every read
    reads = [
        ("plugin", "list", {}), ("plugin", "info", {"target": "kira_ops"}),
        ("plugin", "config_get", {"target": "kira_ops"}),
        ("skill", "list", {}), ("provider", "list", {}), ("mcp", "list", {}),
        ("session", "list", {}), ("persona", "list", {}),
        ("config", "get", {}), ("log", "tail", {"limit": 5}), ("log", "history", {}),
        ("store", "sources", {}), ("backup", "list", {}),
        ("agent", "get", {}), ("agent", "info", {}), ("control", "info", {}),
    ]
    for domain, action, args in reads:
        def probe(domain=domain, action=action, args=args):
            r = run(inst.ops_read(Ev(), domain, action, **args))
            _assert(r.get("ok"), json.dumps(r, ensure_ascii=False)[:200])
        check(f"read {domain}.{action}", probe)

    # ---------------------------------------------------------------- output format
    def output_format():
        """The bytes the model sees: compact JSON, not a Python dict repr."""
        from core.agent.tool import ToolResult
        tool = pm.ctx.tool_mgr.tool_set.get("ops_status")
        _assert(tool is not None, "ops_status is missing from the global tool set")
        raw = run(tool.execute(ev))
        _assert(isinstance(raw, dict), type(raw))
        text = run(ToolResult(str(raw)).assemble_result())
        text = text if isinstance(text, str) else str(text)
        _assert(text.startswith('{"ok":true'), text[:80])
        _assert("': " not in text, f"python repr leaked: {text[:80]}")
        _assert("True" not in text and "None" not in text, text[:120])
    check("tool results reach the model as compact JSON", output_format)

    # ---------------------------------------------------------------- S3 gate
    def s3():
        try:
            inst.master = {"enabled": False, "panic_lock": False}
            r = run(inst.ops_status(ev))
            _assert(not r.get("ok"), r)
        finally:
            inst.master = dict(inst.cfg.get("master") or {})
    check("S3 ops_status refuses while master.enabled=false", s3)

    def persona_active():
        r = run(inst.ops_read(ev, "persona", "get_active"))
        _assert(r.get("ok") or "no active persona" in (r.get("error") or ""), r)
    check("read persona.get_active", persona_active)

    # ---------------------------------------------------------------- S4 truncation
    def s4_log():
        import core.logging_manager as lm
        original = lm.log_cache_manager.get_cache
        lm.log_cache_manager.get_cache = lambda: [
            {"time": "t", "level": "INFO", "name": "big", "message": "X" * 4000}
        ] * 30
        try:
            r = run(inst.ops_read(ev, "log", "tail", limit=30))
            size = len(json.dumps(r, ensure_ascii=False))
            _assert(size < 15000, f"log.tail returned {size} chars")
            _assert("…(+" in r["items"][0]["message"], r["items"][0])
        finally:
            lm.log_cache_manager.get_cache = original
    check("S4 log.tail clips every message", s4_log)

    def s4_config():
        r = run(inst.ops_read(ev, "config", "get"))
        _assert(r.get("ok") and "sections" in r, r)
        size = len(json.dumps(r, ensure_ascii=False))
        _assert(size < 4000, f"config.get summary is {size} chars")
        _assert("value" not in r)
    check("S4 config.get without path returns a summary", s4_config)

    def s4_readfile():
        probe_file = get_data_path() / "kira_ops_test.log"
        probe_file.write_text("Z" * 50000, encoding="utf-8")
        try:
            r = run(inst.ops_read(ev, "log", "read_file",
                                  args={"path": "kira_ops_test.log", "limit": 10 ** 9}))
            _assert(r.get("ok"), r)
            _assert(r["limit"] <= 20000, r)
            _assert(len(r["tail"]) <= 20000, len(r["tail"]))
        finally:
            probe_file.unlink(missing_ok=True)
    check("S4 log.read_file clamps limit", s4_readfile)

    # ---------------------------------------------------------------- M3 brief/full
    def m3():
        brief = run(inst.ops_status(ev, include="audit,permission"))
        full = run(inst.ops_status(ev, include="audit,permission", detail="full"))
        _assert(brief.get("detail") == "brief", brief)
        _assert("hint" not in full and full.get("detail") == "full", full)
        trimmed = inst._brief({"plugins": {"errors": [f"e{i}" for i in range(20)]}})
        errs = trimmed["plugins"]["errors"]
        _assert(errs["count"] == 20 and len(errs["items"]) == 5 and errs["truncated"], errs)
        _assert("hint" in trimmed, trimmed)
        plain = inst._brief({"plugins": {"errors": ["only one"]}})
        _assert("hint" not in plain, plain)
        _assert(inst._trim([1, 2, 3]) == [1, 2, 3])
    check("M3 brief trims long lists, full does not", m3)

    # ---------------------------------------------------------------- M1 phantom session
    def m1():
        ghost = f"qq:gm:ghost-{uuid.uuid4().hex[:8]}"
        store = ctx.session_mgr.chat_memory
        before = len(store)
        r = run(inst.ops_read(ev, "session", "info", target=ghost))
        after = len(store)
        _assert(not r.get("ok"), r)
        _assert(before == after, f"session count {before} -> {after}")
    check("M1 session.info never creates the session", m1)

    # ---------------------------------------------------------------- S5 agent truth
    def s5():
        has = pm.has_plugin("agent")
        r = run(inst.ops_read(ev, "agent", "get"))
        _assert(r.get("ok"), r)
        _assert(r["installed"] == has, r)
        _assert(r["plugin_enabled"] == (has and pm.is_plugin_enabled("agent")), r)
    check("S5 agent domain reports installed/enabled truthfully", s5)

    def s5_status():
        r = run(inst.ops_status(ev, include="plugins"))
        ready = pm.has_plugin("agent") and pm.is_plugin_enabled("agent")
        notes = " ".join(r.get("notes") or [])
        _assert((not ready) == ("agent" in notes), (ready, notes))
    check("S5 ops_status note matches the agent plugin state", s5_status)

    # ---------------------------------------------------------------- M7 models
    check("M7 provider.models refuses an unknown provider",
          lambda: _assert(not run(inst.ops_read(
              ev, "provider", "models", args={"provider_id": "nope"})).get("ok")))

    # ---------------------------------------------------------------- M5 limit
    def m5():
        r = run(inst.ops_read(ev, "plugin", "list", limit=1))
        _assert(r.get("ok") and r["count"] <= 1 and "total" in r, r)
    check("M5 list honours limit and reports total", m5)

    # ---------------------------------------------------------------- M2 extension point
    def m2():
        from plugins.kira_ops.caps import Capability, REGISTRY, register_capability

        class ProbeCap(Capability):
            name = "probe"
            ACTIONS = {"ping": ("read", False, "probe")}

            def handle_read(self, action, params):
                return {"ok": True, "pong": True}

        ok, detail = register_capability(ProbeCap)
        _assert(ok, detail)
        _assert(register_capability(ProbeCap)[0] is False, "duplicate accepted")
        _assert(register_capability(object)[0] is False, "non-class accepted")
        _assert(register_capability(type("Bad", (Capability,),
                                         {"name": "bad", "ACTIONS": {}}))[0] is False,
                "empty ACTIONS accepted")
        _assert(register_capability(type("Bad2", (Capability,),
                                         {"name": "BadName",
                                          "ACTIONS": {"x": ("read", False, "d")}}))[0] is False,
                "illegal name accepted")
        _assert(register_capability(type("Bad3", (Capability,),
                                         {"name": "bad3",
                                          "ACTIONS": {"x": ("nope", False, "d")}}))[0] is False,
                "illegal kind accepted")
        try:
            inst.caps["probe"] = ProbeCap(inst)
            r = run(inst.ops_read(ev, "probe", "ping"))
            _assert(r.get("ok") and r.get("pong"), r)
        finally:
            inst.caps.pop("probe", None)
            REGISTRY.pop("probe", None)
    check("M2 register_capability accepts valid classes and rejects junk", m2)

    def m2_event():
        """The event bus only enqueues; dispatch runs in the lifecycle consumer.
        So assert on the registered hook (name filter + live binding) and run it."""
        try:
            from core.plugin.handlers import EventType, event_handler_reg  # 3.0+
        except ImportError:
            from core.plugin.plugin_handlers import EventType, event_handler_reg  # 2.x
        from plugins.kira_ops.caps import Capability, REGISTRY

        class EventCap(Capability):
            name = "eventcap"
            ACTIONS = {"ping": ("read", False, "probe")}

            def handle_read(self, action, params):
                return {"ok": True, "via": "event"}

        hooks = [h for h in event_handler_reg.get_handlers(EventType.ON_CUSTOM_EVENT)
                 if getattr(h.handler, "_custom_event_name", None) == "kira_ops.register_capability"]
        _assert(hooks, "no ON_CUSTOM_EVENT handler registered for kira_ops.register_capability")
        _assert(getattr(hooks[0].handler, "__self__", None) is inst, "handler is not bound to the plugin")
        try:
            run(hooks[0].exec_handler(KiraCustomEvent(
                event_name="kira_ops.register_capability",
                source_plugin="test_suite",
                payload={"class": EventCap})))
            _assert("eventcap" in REGISTRY, sorted(REGISTRY))
            _assert("eventcap" in inst.caps, sorted(inst.caps))
            r = run(inst.ops_read(ev, "eventcap", "ping"))
            _assert(r.get("ok") and r.get("via") == "event", r)
        finally:
            inst.caps.pop("eventcap", None)
            REGISTRY.pop("eventcap", None)
    check("M2 the register_capability hook is bound and works", m2_event)

    # ---------------------------------------------------------------- M4 audit
    def deny_count():
        return sum(1 for row in inst.audit.tail(20000) if row.get("kind") == "deny")

    def m4_read_deny():
        inst.audit.audit_reads = False
        try:
            settings = dict(inst.cfg)
            settings["access"] = {"allow_sessions": ["qq:gm:somewhere-else"],
                                  "deny_sessions": [], "readonly_sessions": []}
            settings["master"] = {"enabled": True, "panic_lock": False}
            inst.engine.apply_settings(settings)
            before = deny_count()
            r = run(inst.ops_read(ev, "plugin", "list"))
            _assert(not r.get("ok"), r)
            _assert(deny_count() == before + 1, "denied read was not audited")
            _assert(inst.audit.tail(1)[-1].get("kind") == "deny")
        finally:
            inst.engine.apply_settings(inst.cfg)
            inst.audit.audit_reads = True
    check("M4 a denied read is written to the audit trail", m4_read_deny)

    def m4_confirm_deny():
        before = deny_count()
        r = run(inst.ops_confirm(ev, "deadbeefdeadbeef"))
        _assert(not r.get("ok"), r)
        _assert(deny_count() == before + 1, "rejected token was not audited")
        _assert(inst.audit.tail(1)[-1].get("kind") == "deny")
    check("M4 a rejected confirm token is written to the audit trail", m4_confirm_deny)

    # ---------------------------------------------------------------- S1/S2 installer
    def installer():
        from plugins.kira_ops.store.installer import (
            install_plugin_from_zip_bytes, install_skill_from_zip_bytes)

        pid = "zip_harness_plugin"
        target = Path(pm.plugin_dir) / pid
        try:
            r1 = run(install_plugin_from_zip_bytes(pm, plugin_zip(pid, 1), pid))
            _assert(r1.get("ok"), r1)
            r2 = run(install_plugin_from_zip_bytes(pm, plugin_zip(pid, 2), pid, update=True))
            _assert(r2.get("ok"), r2)
            mod = sys.modules.get(f"plugins.{pid}.impl")
            value = getattr(mod, "VALUE", None)
            _assert(value == 2, (
                f"S1: submodule still shows the previous build (VALUE={value}); "
                "the update path is missing prepare_plugin_reload"))
        finally:
            try:
                run(pm.uninstall_plugin(pid))
            except Exception:
                pass
            shutil.rmtree(target, ignore_errors=True)

        bomb = BytesIO()
        with zipfile.ZipFile(bomb, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("evil_plugin/manifest.json", json.dumps({
                "plugin_id": "evil_plugin", "display_name": "e", "version": "1",
                "author": "a", "description": "d"}))
            zf.writestr("evil_plugin/big.txt", "0" * (12 * 1024 * 1024))
        r3 = run(install_plugin_from_zip_bytes(pm, bomb.getvalue(), "evil_plugin"))
        _assert(not r3.get("ok"), r3)
        _assert(not (Path(pm.plugin_dir) / "evil_plugin").exists(), "bomb was installed")

        skill_zip = BytesIO()
        with zipfile.ZipFile(skill_zip, "w") as zf:
            zf.writestr("demo/SKILL.md", "---\nname: demo\ndescription: d\n---\nbody\n")
        skills_root = get_data_path() / "skills"
        try:
            installed = run(install_skill_from_zip_bytes(skills_root, skill_zip.getvalue(), "demo"))
            _assert(installed.is_dir() and (installed / "SKILL.md").is_file(), installed)
        finally:
            shutil.rmtree(skills_root / "demo", ignore_errors=True)
    check("S1/S2 installer reloads submodules and rejects hostile archives", installer)

    # ---------------------------------------------------------------- L2 mcp toggle
    def l2():
        inst.engine.apply_settings(inst.cfg)  # guarantee the write gate is open
        r = run(inst.ops_action(ev, "mcp", "add", args={
            "name": "harness", "description": "d",
            "config": {"type": "sse", "url": "https://example.com/sse"}}))
        _assert(r.get("ok"), r)
        sid = r["server_id"]
        try:
            bad = run(inst.ops_action(ev, "mcp", "tool_toggle",
                                      args={"server_id": sid, "tool": "nope", "enabled": False}))
            _assert(not bad.get("ok"), bad)
            _assert("cannot toggle" in (bad.get("error") or ""), bad)
        finally:
            run(inst.ops_action(ev, "mcp", "delete", args={"server_id": sid}))
    check("L2 mcp.tool_toggle fails cleanly on an unknown tool", l2)
    # ---------------------------------------------------------------- security
    def read_file_scope():
        """read_file is a *log* reader: it must not become an unmasked arbitrary read."""
        data = get_data_path()
        cfg_file = get_config_path() / "system_config.json"
        raw = json.loads(cfg_file.read_text(encoding="utf-8"))
        raw.setdefault("providers", {})["canary_prov"] = {
            "format": "OpenAI", "name": "Canary",
            "provider_config": {"api_key": "sk-CANARY-DO-NOT-LEAK"},
            "model_config": {"llm": {"m": {"model_id": "m"}}},
        }
        cfg_file.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")

        blocked = run(inst.ops_read(ev, "log", "read_file",
                                    args={"path": "config/system_config.json"}))
        _assert(not blocked.get("ok"), blocked)
        _assert("sk-CANARY-DO-NOT-LEAK" not in json.dumps(blocked), blocked)

        ctx.session_mgr.write_memory(ev.sid, [[{"role": "user", "content": "chat-canary"}]])
        mem = run(inst.ops_read(ev, "log", "read_file",
                                args={"path": "memory/chat_memory.json"}))
        _assert(not mem.get("ok"), mem)
        _assert("chat-canary" not in json.dumps(mem), mem)

        probe = data / "kira_ops_probe.log"
        probe.write_text("log-line-1\nlog-line-2\n", encoding="utf-8")
        try:
            ok_read = run(inst.ops_read(ev, "log", "read_file", args={"path": "kira_ops_probe.log"}))
            _assert(ok_read.get("ok"), ok_read)
            _assert("log-line-1" in ok_read["tail"], ok_read)
        finally:
            probe.unlink(missing_ok=True)
    check("read_file only reads logs (no unmasked config/memory)", read_file_scope)

    def mask_floor():
        from plugins.kira_ops.core.redact import ALWAYS_MASK, key_matches, with_floor
        for key in ("Authorization", "Bearer", "Cookie", "api_key", "token"):
            _assert(key_matches(key, with_floor([])), f"{key} is not masked")
        _assert(len(ALWAYS_MASK) >= 8)

        srv = ctx.message_processor.mcp_manager.add_or_update_server_from_config(
            "canary_srv", "probe", {"type": "sse", "url": "https://x/sse",
                                    "headers": {"Authorization": "Bearer sk-MCP-CANARY",
                                                "X-Api-Key": "sk-XKEY-CANARY"}})
        try:
            info = run(inst.ops_read(ev, "mcp", "info", args={"server_id": srv.id}))
            blob = json.dumps(info, ensure_ascii=False)
            _assert("sk-MCP-CANARY" not in blob, blob[:200])
            _assert("sk-XKEY-CANARY" not in blob, blob[:200])
        finally:
            run(ctx.message_processor.mcp_manager.delete_server(srv.id))
    check("mask floor hides Authorization/Bearer/Cookie", mask_floor)

    # ---------------------------------------------------------------- consistency
    def snapshot_uniqueness():
        from plugins.kira_ops.core.backup import BackupManager
        bm = BackupManager(get_data_path() / "plugin_data" / "kira_ops", {"keep_last": 50})
        target = get_config_path() / "system_config.json"

        async def race():
            return await asyncio.gather(*[
                asyncio.to_thread(bm.snapshot, [target], "race_probe", "probe")
                for _ in range(5)])

        snaps = loop.run_until_complete(race())
        ids = [s.get("id") for s in snaps]
        _assert(len(set(ids)) == len(ids), f"snapshots merged: {ids}")
        for snap in snaps:
            meta = snap.get("path")
            _assert(meta, snap)
    check("concurrent snapshots never share a folder", snapshot_uniqueness)

    def audit_args_capped():
        rows = inst.audit.tail(5)
        for row in rows:
            size = len(json.dumps(row, ensure_ascii=False))
            _assert(size < 6000, f"audit row is {size} chars")
    check("audit rows are size-capped", audit_args_capped)

    def malformed_args():
        bad = run(inst.ops_read(ev, "log", "tail", limit="abc"))
        _assert(bad.get("ok"), bad)              # falls back to the default limit
        bad2 = run(inst.ops_action(ev, "session", "title", target=ev.sid, args="oops"))
        _assert(bad2.get("ok") or bad2.get("error"), bad2)
        bad3 = run(inst.ops_read(ev, "config", "get", args=["a", "b"]))
        _assert(bad3.get("ok"), bad3)            # non-dict args are ignored, not fatal
    check("malformed model arguments never raise", malformed_args)

    def session_id_validation():
        """A junk session id used to poison chat_memory and break the framework."""
        store = ctx.session_mgr.chat_memory
        before = set(store)
        r = run(inst.ops_action(ev, "session", "title", target="x",
                                args={"session_id": "x", "title": "boom"}))
        _assert(not r.get("ok"), r)
        for action in ("caps", "memory_clear"):
            out = run(inst.ops_action(ev, "session", action, target="bad-key",
                                      args={"session_id": "bad-key"}))
            _assert(not out.get("ok"), out)
        _assert(set(store) == before, f"session store changed: {set(store) ^ before}")
    check("malformed session ids are refused (no chat_memory poisoning)",
          session_id_validation)

    def session_inventory_tolerates_junk():
        """Even with a poisoned store, the session domain must keep working."""
        store = ctx.session_mgr.chat_memory
        settings = dict(inst.cfg)
        settings["risk"] = dict(settings.get("risk") or {}, level="dangerous",
                                high_risk_sessions=[ev.sid])
        try:
            # repair whatever an earlier run (or a 1.0.0 deployment) left behind
            inst.engine.apply_settings(settings)
            for key in [k for k in list(store) if len(str(k).split(":")) < 3]:
                out = run(inst.ops_action(ev, "session", "delete", target=key))
                if out.get("need_confirm"):
                    out = run(inst.ops_confirm(ev, out["token"]))
                _assert(out.get("ok"), out)
            _assert(all(len(str(k).split(":")) >= 3 for k in store),
                    f"junk keys remain: {[k for k in store if len(str(k).split(':')) < 3]}")
            _assert(ctx.session_mgr.get_session_info() is not None,
                    "the framework enumerator should work again after the repair")

            store["junk_key"] = {"title": "", "description": "", "timestamp": None, "memory": []}
            broken = False
            try:
                ctx.session_mgr.get_session_info()
            except Exception:
                broken = True
            _assert(broken, "expected the framework enumerator to raise on a junk key")

            listed = run(inst.ops_read(ev, "session", "list"))
            _assert(listed.get("ok"), listed)
            _assert("junk_key" in (listed.get("malformed_keys") or []), listed)
            status = run(inst.ops_status(ev, include="sessions"))
            _assert(status.get("ok"), status)

            removed = run(inst.ops_action(ev, "session", "delete", target="junk_key"))
            if removed.get("need_confirm"):
                removed = run(inst.ops_confirm(ev, removed["token"]))
            _assert(removed.get("ok"), removed)
            _assert("junk_key" not in ctx.session_mgr.chat_memory)
            _assert(ctx.session_mgr.get_session_info() is not None)
        finally:
            inst.engine.apply_settings(inst.cfg)
            ctx.session_mgr.chat_memory.pop("junk_key", None)
    check("malformed chat_memory keys degrade gracefully and can be repaired",
          session_inventory_tolerates_junk)

    def rollback_points_are_instance_local():
        """A rollback point copied with the folder must not write into the other instance."""
        target = get_config_path() / "system_config.json"
        snap = inst.backups.snapshot([target], "instance_origin_probe", "test")
        _assert(snap.get("id"), snap)
        meta_path = Path(snap["path"]) / "_meta.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        entry = meta["files"][0]
        _assert(entry.get("origin_rel"), f"origin_rel not recorded: {entry}")
        _assert(meta.get("data"), f"data root not recorded: {meta.keys()}")

        # same-tree restore resolves through the current data dir
        own = inst.backups.restore(snap["id"], force=True)
        _assert(own.get("ok"), own)
        _assert(all(str(target) == p for p in own["restored"]), own)

        # now pretend the folder was copied from another instance: absolute origin,
        # no relative record (what the old format produced)
        # realistic shape: another KiraAI tree, as it looks after copying the folder
        foreign_root = Path(tempfile.mkdtemp(prefix="KiraAI_other_"))
        foreign_target = foreign_root / "data" / "config" / "system_config.json"
        foreign_target.parent.mkdir(parents=True, exist_ok=True)
        foreign_target.write_text("{}", encoding="utf-8")
        entry.pop("origin_rel", None)
        entry["origin"] = str(foreign_target)
        meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        try:
            blocked = inst.backups.restore(snap["id"], force=True)
            _assert(not blocked.get("ok"), blocked)
            _assert(blocked.get("foreign"), blocked)
            _assert(blocked["restored"] == [], blocked)
            _assert(foreign_target.read_text(encoding="utf-8") == "{}",
                    "the other instance's file was overwritten")
            listed = [i for i in inst.backups.list(50) if i["id"] == snap["id"]]
            _assert(listed and listed[0].get("foreign"), listed)
        finally:
            shutil.rmtree(foreign_root, ignore_errors=True)
            shutil.rmtree(Path(snap["path"]), ignore_errors=True)
    check("rollback points never write into another instance's tree",
          rollback_points_are_instance_local)

    def read_file_never_leaks():
        blocked = run(inst.ops_read(ev, "log", "read_file",
                                    args={"path": "config/plugins/kira_ops.json"}))
        _assert(not blocked.get("ok"), blocked)
    check("read_file refuses non-log files under data/", read_file_never_leaks)

    loop.close()
    print()
    print(f"passed: {len(PASSED)}  failed: {len(FAILED)}")
    if FAILED:
        for name, exc in FAILED:
            print(f"  FAILED: {name}: {exc}")
        sys.exit(1)
    print("ALL OK")


if __name__ == "__main__":
    main()
