"""Coverage-gap audit: exercise the actions no other suite touches, plus the
panel endpoints, against the real framework.

Gaps found by static analysis: plugin.config_set, provider.health,
skill.set_scope - plus the panel API surface (GET/POST config, overview,
panic, backups).

Run from the KiraAI root:
    python data/plugins/kira_ops/tests/test_gaps.py
"""

from __future__ import annotations

import asyncio
import copy
import json
import sys
import types
from pathlib import Path
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
from core.chat.message_utils import MessageChain  # noqa: E402
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

FAILED, PASSED = [], []
SENT, NOTICES = [], []


def _assert(cond, detail=""):
    if not cond:
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
    def __init__(self, sid="napcat:gm:11223", uid="33445"):
        self.sid = sid
        self.messages = [SimpleNamespace(sender=SimpleNamespace(user_id=uid))]
        self.session = None


async def boot():
    kira_config = KiraConfig()
    dbm = DatabaseManager(
        f"sqlite+aiosqlite:///{(get_data_path() / 'kira_ops_gap_test.db').as_posix()}")
    await dbm.init()
    db = DatabaseService(dbm)
    await db.init_tables()
    ctx = PluginContext(
        db=db, config=kira_config, event_bus=EventBus(Statistics(), asyncio.Queue()),
        provider_mgr=ProviderManager(db, kira_config), tool_mgr=FuncToolManager(kira_config),
        adapter_mgr=None, persona_mgr=PersonaManager(db),
        session_mgr=SessionManager(db, kira_config), sticker_manager=None,
        message_processor=SimpleNamespace(mcp_manager=MCPManager(FuncToolManager(kira_config)),
                                          skills_manager=SkillsManager())
    )
    pm = PluginManager(ctx)
    ctx.plugin_mgr = pm
    return ctx, pm


def main():
    print("== kira_ops coverage-gap audit (real framework) ==")
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    run = loop.run_until_complete
    ctx, pm = run(boot())
    _assert(run(pm.load_plugin_from_dir(PLUGIN_DIR, auto_install=False)) == "kira_ops")
    run(pm.init_plugin("kira_ops"))
    inst = pm.get_plugin_inst("kira_ops")

    cfg = pm.get_plugin_config("kira_ops")
    cfg["master"] = {"enabled": True, "panic_lock": False}
    cfg["access"] = {"allow_sessions": [], "deny_sessions": [], "readonly_sessions": []}
    cfg["risk"] = dict(cfg.get("risk") or {}, level="standard", high_risk_sessions=[])
    # the panel mask contract: never hand the real password to the browser
    cfg["confirm"] = dict(cfg.get("confirm") or {}, enabled=False, sessions=[],
                          password="panel-secret-xyz")
    inst.cfg = cfg
    inst.refresh_settings(cfg)
    inst.engine.apply_settings(cfg)

    # ---------------------------------------------------------- plugin.config_set
    def plugin_config_set_roundtrip():
        """Write a harmless key through the plugin domain and read it back."""
        info = pm.get_plugin_info("dummy_target")
        if info is None:
            # no dummy plugin in this deployment: fall back to a config round trip
            r = run(inst.ops_action(Ev(), "plugin", "config_set",
                                    target="kira_ops", args={"patch": {"__probe": 1}}))
            _assert(r.get("ok") is True, r)
            back = run(inst.ops_read(Ev(), "plugin", "config_get", target="kira_ops"))
            _assert(back.get("config", {}).get("__probe") == 1, back)
            run(inst.ops_action(Ev(), "plugin", "config_set",
                                target="kira_ops", args={"patch": {"__probe": None}}))
            return
        r = run(inst.ops_action(Ev(), "plugin", "config_set",
                                target="dummy_target", args={"patch": {"probe": 7}}))
        _assert(r.get("ok") is True, r)
        back = run(inst.ops_read(Ev(), "plugin", "config_get", target="dummy_target"))
        _assert(back.get("config", {}).get("probe") == 7, back)
    check("plugin.config_set round-trips through the real manager", plugin_config_set_roundtrip)

    def plugin_config_set_refuses_sensitive_and_bad_patches():
        r = run(inst.ops_action(Ev(), "plugin", "config_set",
                                target="kira_ops", args={"patch": {"api_key": "x"}}))
        _assert(r.get("ok") is False, f"write_deny must block api_key: {r}")
        r2 = run(inst.ops_action(Ev(), "plugin", "config_set",
                                 target="kira_ops", args={"patch": {"confirm": {"password": "hacked"}}}))
        _assert(r2.get("ok") is False, f"the model must not be able to set the password: {r2}")
        r3 = run(inst.ops_action(Ev(), "plugin", "config_set",
                                 target="kira_ops", args={"patch": "nope"}))
        _assert(r3.get("ok") is False, r3)
    check("plugin.config_set refuses sensitive keys and non-dict patches",
          plugin_config_set_refuses_sensitive_and_bad_patches)

    # ---------------------------------------------------------- provider.health
    def provider_health_reports_cleanly():
        """No network here: the call must come back as a structured result
        (or a clean error), never raise and never hang."""
        provs = getattr(ctx, "provider_mgr", None)
        ids = list((provs.get_all_providers() or {}).keys()) if provs else []
        pid = ids[0] if ids else "does-not-exist"
        r = run(inst.ops_read(Ev(), "provider", "health", target=pid,
                              args={"model_type": "llm", "model_id": "probe"}))
        _assert(isinstance(r, dict), r)
        _assert("ok" in r, r)
    check("provider.health answers with a dict and never raises", provider_health_reports_cleanly)

    # ---------------------------------------------------------- skill.set_scope
    def skill_set_scope_roundtrip():
        sm = getattr(ctx.message_processor, "skills_manager", None)
        names = []
        if sm is not None:
            try:
                names = [s.name for s in (sm.skills_info or [])] if isinstance(sm.skills_info, list) else list(sm.skills_info)
            except Exception:
                names = []
        if not names:
            r = run(inst.ops_action(Ev(), "skill", "set_scope",
                                    target="no-such-skill", args={"scope": ["napcat:gm:1"]}))
            _assert(r.get("ok") is False, f"unknown skill must fail cleanly: {r}")
            return
        name = names[0]
        r = run(inst.ops_action(Ev(), "skill", "set_scope",
                                target=name, args={"scope": ["napcat:gm:11223"]}))
        _assert(isinstance(r, dict), r)
    check("skill.set_scope handles both a real skill and an unknown one", skill_set_scope_roundtrip)

    # ---------------------------------------------------------- panel endpoints
    def panel_endpoints_answer():
        ov = run(inst.api_overview())
        _assert(ov.get("ok") and "confirm" in ov, sorted(ov.keys()))
        _assert(set(ov["confirm"]) == {"enabled", "sessions", "password_set", "ready"},
                ov["confirm"])
        # store a password through the real (file-backed, hot-reloading) path
        run(inst.api_set_config({"config": {"confirm": {"password": "panel-secret-xyz"}}}))
        fresh = pm.get_plugin_inst("kira_ops")
        _assert(fresh.confirm_cfg.get("password") == "panel-secret-xyz", "panel set failed")
        conf = run(fresh.api_get_config())
        blob = json.dumps(conf, ensure_ascii=False)
        _assert("panel-secret-xyz" not in blob, "the panel must never receive the password")
        _assert(conf["config"]["confirm"]["password"] == "***", conf["config"]["confirm"])
        # saving the mask keeps the stored value
        run(fresh.api_set_config({"config": {"confirm": {"password": "***"}}}))
        fresh2 = pm.get_plugin_inst("kira_ops")
        _assert(fresh2.confirm_cfg.get("password") == "panel-secret-xyz", "mask must not overwrite")
        # panic toggle works and is reversible
        p1 = run(inst.api_panic({"lock": True}))
        _assert(p1.get("ok") and p1.get("panic_lock") is True, p1)
        # api_panic saves config -> the plugin hot-reloads -> use the live instance
        live = pm.get_plugin_inst("kira_ops")
        _assert(live.master.get("panic_lock") is True, live.master)
        r = run(live.ops_action(Ev(), "plugin", "enable", target="dummy_target"))
        _assert(r.get("ok") is False and "panic" in r.get("error", ""),
                f"panic lock must refuse writes: {r}")
        p2 = run(pm.get_plugin_inst("kira_ops").api_panic({"lock": False}))
        _assert(p2.get("ok") and p2.get("panic_lock") is False, p2)
        b = run(inst.api_backups())
        _assert(isinstance(b, dict), b)
    check("panel endpoints (/overview /config /panic /backups) behave", panel_endpoints_answer)

    def backup_restore_is_guarded():
        """restore must refuse a bogus id and never touch the filesystem."""
        r = run(inst.api_restore({"id": "no-such-backup"}))
        _assert(r.get("ok") is False, f"a bogus backup id must be refused: {r}")
    check("panel restore refuses unknown backup ids", backup_restore_is_guarded)

    print(f"\npassed: {len(PASSED)}  failed: {len(FAILED)}")
    if FAILED:
        raise SystemExit(1)
    print("ALL OK")


if __name__ == "__main__":
    main()