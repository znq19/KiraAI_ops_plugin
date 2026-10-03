"""Concurrency / ordering audit: race the confirm channel against itself.

Uses the real asyncio loop and the real ConfirmPool. Every case either proves
an invariant or finds a way to break it.

Run from the KiraAI root:
    python data/plugins/kira_ops/tests/test_races.py
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

from core.adapter.adapter_info import AdapterInfo  # noqa: E402
from core.agent.func_tool_manager import FuncToolManager  # noqa: E402
from core.agent.mcp_mgr import MCPManager  # noqa: E402
from core.agent.skills_mgr import SkillsManager  # noqa: E402
from core.chat.message_elements import Text  # noqa: E402
from core.chat.message_utils import KiraIMMessage, KiraMessageEvent, MessageChain  # noqa: E402
from core.chat.session import User  # noqa: E402
from core.chat.session_manager import SessionManager  # noqa: E402
from core.config import KiraConfig  # noqa: E402
from core.db.db_mgr import DatabaseManager  # noqa: E402
from core.db.service import DatabaseService  # noqa: E402
from core.event_bus import EventBus  # noqa: E402
from core.message_manager import MessageProcessor  # noqa: E402
from core.persona import PersonaManager  # noqa: E402
from core.plugin import PluginManager  # noqa: E402
from core.plugin.plugin_context import PluginContext  # noqa: E402
from core.provider import ProviderManager  # noqa: E402
from core.statistics import Statistics  # noqa: E402
from core.utils.path_utils import get_data_path  # noqa: E402

FAILED, PASSED = [], []
SENT, EXECUTED = [], []
CONFIRM = "napcat:dm:769690776"
REQ = "napcat:gm:11223"
PASSWORD = "114514"


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


class FakeAdapter:
    def __init__(self):
        self.info = AdapterInfo(enabled=True, adapter_id="a1", name="napcat", platform="qq")
        self.message_types = []
        self.config = {"self_id": "9"}
        self.calls = 0

    async def send_direct_message(self, uid, chain):
        await asyncio.sleep(0.01)          # a real adapter is not instant
        self.calls += 1
        from core.chat.message_utils import KiraIMSentResult
        SENT.append(("dm", str(uid), "".join(getattr(e, "text", "") for e in chain)))
        return KiraIMSentResult(message_id="x", ok=True)

    async def send_group_message(self, gid, chain):
        await asyncio.sleep(0.01)
        from core.chat.message_utils import KiraIMSentResult
        SENT.append(("gm", str(gid), "".join(getattr(e, "text", "") for e in chain)))
        return KiraIMSentResult(message_id="y", ok=True)


class FakeAdapterMgr:
    def __init__(self):
        self.adapter = FakeAdapter()

    def get_adapter(self, name):
        return self.adapter if name == "napcat" else None


class Ev:
    def __init__(self, sid=REQ, uid="1"):
        self.sid = sid
        self.messages = [SimpleNamespace(sender=SimpleNamespace(user_id=uid))]
        self.session = None


def pw_event(text=PASSWORD):
    return KiraMessageEvent(
        message_types=[], timestamp=0,
        adapter=AdapterInfo(enabled=True, adapter_id="a1", name="napcat", platform="qq"),
        message=KiraIMMessage(message_id="i", self_id="9", chain=MessageChain([Text(text)]),
                              timestamp=0, sender=User(user_id="769690776", nickname="o"),
                              group=None))


async def boot():
    kc = KiraConfig()
    dbm = DatabaseManager(f"sqlite+aiosqlite:///{(get_data_path() / 'kira_ops_race.db').as_posix()}")
    await dbm.init()
    db = DatabaseService(dbm)
    await db.init_tables()
    am = FakeAdapterMgr()
    ctx = PluginContext(
        db=db, config=kc, event_bus=EventBus(Statistics(), asyncio.Queue()),
        provider_mgr=ProviderManager(db, kc), tool_mgr=FuncToolManager(kc),
        adapter_mgr=am, persona_mgr=PersonaManager(db), session_mgr=SessionManager(db, kc),
        sticker_manager=None,
        message_processor=SimpleNamespace(mcp_manager=MCPManager(FuncToolManager(kc)),
                                          skills_manager=SkillsManager()))
    owner = SimpleNamespace(adapter_mgr=am)

    async def _send(session, chain):
        return await MessageProcessor.send_message_chain(owner, session, chain)

    async def _flush(sid):
        pass

    async def _notice(*a, **k):
        pass

    ctx.message_processor.send_message_chain = _send
    ctx.message_processor.flush_session_messages = _flush
    ctx.publish_notice = _notice
    pm = PluginManager(ctx)
    ctx.plugin_mgr = pm
    return ctx, pm


def main():
    print("== kira_ops concurrency / ordering audit ==")
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    run = loop.run_until_complete
    ctx, pm = run(boot())
    _assert(run(pm.load_plugin_from_dir(PLUGIN_DIR, auto_install=False)) == "kira_ops")
    run(pm.init_plugin("kira_ops"))
    inst = pm.get_plugin_inst("kira_ops")

    from plugins.kira_ops.caps import Capability  # noqa: E402

    class SlowCap(Capability):
        name = "race"
        ACTIONS = {"go": ("write", True, "slow action")}

        async def handle_write(self, action, params):
            await asyncio.sleep(0.15)      # a slow install/uninstall
            EXECUTED.append(dict(params))
            return {"ok": True}

    inst.caps["race"] = SlowCap(inst)

    cfg = {
        "master": {"enabled": True, "panic_lock": False},
        "access": {"allow_sessions": [], "deny_sessions": [], "readonly_sessions": []},
        "risk": {"level": "standard", "high_risk_actions": ["race.go"],
                 "high_risk_sessions": [], "require_confirm": True, "confirm_ttl": 300},
        "confirm": {"enabled": True, "sessions": [CONFIRM], "password": PASSWORD,
                    "template_request": "", "template_approved": "", "template_notice": ""},
        "control": {}, "protected": {},
    }
    inst.cfg = cfg
    inst.refresh_settings(cfg)
    inst.engine.apply_settings(cfg)

    # ------------------------------------------------------------------ races
    async def race_double_password():
        """Two password messages delivered back-to-back must not double-execute
        the same pending request."""
        inst.confirm.clear()
        EXECUTED.clear()
        await inst.ops_action(Ev(), "race", "go")
        await asyncio.sleep(0.05)
        e1, e2 = pw_event(), pw_event()
        await asyncio.gather(inst.confirm_password_hook(e1), inst.confirm_password_hook(e2))
        await asyncio.sleep(0.6)
        _assert(len(EXECUTED) == 1, f"exactly one execution expected: {EXECUTED}")
        return True
    check("double password delivery executes the action exactly once",
          lambda: run(race_double_password()))

    async def race_many_requests_one_password():
        inst.confirm.clear()
        EXECUTED.clear()
        await asyncio.gather(*[inst.ops_action(Ev(), "race", "go", args={"i": i}) for i in range(5)])
        await asyncio.sleep(0.3)
        n = inst.confirm.pending_count
        _assert(n == 5, f"all five must be pending: {n}")
        await inst.confirm_password_hook(pw_event())
        await asyncio.sleep(0.6)
        _assert(len(EXECUTED) == 1, f"one password releases one request: {EXECUTED}")
        _assert(inst.confirm.pending_count == 4, inst.confirm.pending_count)
        return True
    check("5 pending + 1 password = exactly 1 execution, 4 still pending",
          lambda: run(race_many_requests_one_password()))

    async def race_approval_while_disabled():
        """Disabling the channel mid-flight must stop new approvals."""
        inst.confirm.clear()
        EXECUTED.clear()
        await inst.ops_action(Ev(), "race", "go")
        await asyncio.sleep(0.05)
        cfg2 = copy.deepcopy(cfg)
        cfg2["confirm"]["enabled"] = False
        inst.refresh_settings(cfg2)
        inst.engine.apply_settings(cfg2)
        await inst.confirm_password_hook(pw_event())
        await asyncio.sleep(0.4)
        _assert(not EXECUTED, f"a disabled channel must not approve: {EXECUTED}")
        inst.refresh_settings(cfg)
        inst.engine.apply_settings(cfg)
        return True
    check("disabling the channel mid-flight blocks the approval",
          lambda: run(race_approval_while_disabled()))

    async def race_slow_action_does_not_block_messages():
        """The approval runs in a spawned task: the message hook must return
        immediately even when the action takes a while."""
        inst.confirm.clear()
        EXECUTED.clear()
        await inst.ops_action(Ev(), "race", "go")
        await asyncio.sleep(0.05)
        t0 = loop.time()
        await inst.confirm_password_hook(pw_event())
        dt = loop.time() - t0
        _assert(dt < 0.05, f"the hook must not await the action (took {dt:.3f}s)")
        await asyncio.sleep(0.6)
        _assert(len(EXECUTED) == 1, EXECUTED)
        return True
    check("the hook returns immediately; the slow action runs in the background",
          lambda: run(race_slow_action_does_not_block_messages()))

    async def race_ttl_boundary():
        """A request that expires between check and take must not execute."""
        inst.confirm.clear()
        EXECUTED.clear()
        await inst.ops_action(Ev(), "race", "go")
        await asyncio.sleep(0.05)
        rec = inst.confirm.latest()
        rec["expires"] = 0
        await inst.confirm_password_hook(pw_event())
        await asyncio.sleep(0.4)
        _assert(not EXECUTED, f"an expired request must not run: {EXECUTED}")
        return True
    check("expiry between matching and taking still refuses", lambda: run(race_ttl_boundary()))

    async def race_pool_pressure():
        """Flood the pool: the oldest entries must be evicted, not crash."""
        inst.confirm.clear()
        await asyncio.gather(*[inst.ops_action(Ev(), "race", "go", args={"i": i})
                               for i in range(60)])
        await asyncio.sleep(0.5)
        _assert(inst.confirm.pending_count <= 32,
                f"the pool must stay bounded: {inst.confirm.pending_count}")
        return True
    check("a flood of requests keeps the pool bounded", lambda: run(race_pool_pressure()))

    print(f"\npassed: {len(PASSED)}  failed: {len(FAILED)}")
    if FAILED:
        raise SystemExit(1)
    print("ALL OK")


if __name__ == "__main__":
    main()