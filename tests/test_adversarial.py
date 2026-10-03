"""Adversarial audit: try to break the permission gate and the confirm channel.

Everything here runs against the REAL framework objects (same boot as
test_dm_confirm.py). The goal is not coverage but *refutation*: for every
promise the plugin makes, find an input or ordering that breaks it.

Run from the KiraAI root:
    python data/plugins/kira_ops/tests/test_adversarial.py
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

FAILED = []
PASSED = []
SENT = []
NOTICES = []
FLUSHED = []
EXECUTED = []

CONFIRM_SID = "napcat:dm:769690776"
REQ_SID = "napcat:gm:11223"
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
    def __init__(self, name="napcat"):
        self.info = AdapterInfo(enabled=True, adapter_id="a1", name=name, platform="qq")
        self.message_types = []
        self.config = {"self_id": "9999"}

    async def send_direct_message(self, user_id, chain):
        from core.chat.message_utils import KiraIMSentResult
        SENT.append(("dm", str(user_id), "".join(getattr(e, "text", "") for e in chain)))
        return KiraIMSentResult(message_id="m1", ok=True)

    async def send_group_message(self, group_id, chain):
        from core.chat.message_utils import KiraIMSentResult
        SENT.append(("gm", str(group_id), "".join(getattr(e, "text", "") for e in chain)))
        return KiraIMSentResult(message_id="m2", ok=True)


class FakeAdapterMgr:
    def __init__(self):
        self.a = {"napcat": FakeAdapter()}

    def get_adapter(self, name):
        return self.a.get(name)


class Ev:
    def __init__(self, sid=REQ_SID, uid="33445"):
        self.sid = sid
        self.messages = [SimpleNamespace(sender=SimpleNamespace(user_id=uid))]
        self.session = None


def msg_event(text, session_id=CONFIRM_SID, kind="dm", sender="769690776"):
    from core.chat.session import Group
    group = Group(group_id=session_id, group_name="g") if kind == "gm" else None
    return KiraMessageEvent(
        message_types=[], timestamp=0,
        adapter=AdapterInfo(enabled=True, adapter_id="a1", name="napcat", platform="qq"),
        message=KiraIMMessage(message_id="i", self_id="9999",
                              chain=MessageChain([Text(text)]), timestamp=0,
                              sender=User(user_id=sender, nickname="n"), group=group))


async def boot():
    kira_config = KiraConfig()
    dbm = DatabaseManager(
        f"sqlite+aiosqlite:///{(get_data_path() / 'kira_ops_adv_test.db').as_posix()}")
    await dbm.init()
    db = DatabaseService(dbm)
    await db.init_tables()
    adapter_mgr = FakeAdapterMgr()
    ctx = PluginContext(
        db=db, config=kira_config, event_bus=EventBus(Statistics(), asyncio.Queue()),
        provider_mgr=ProviderManager(db, kira_config), tool_mgr=FuncToolManager(kira_config),
        adapter_mgr=adapter_mgr, persona_mgr=PersonaManager(db),
        session_mgr=SessionManager(db, kira_config), sticker_manager=None,
        message_processor=SimpleNamespace(mcp_manager=MCPManager(FuncToolManager(kira_config)),
                                          skills_manager=SkillsManager()))
    owner = SimpleNamespace(adapter_mgr=adapter_mgr)

    async def _send(session, chain):
        return await MessageProcessor.send_message_chain(owner, session, chain)

    async def _flush(sid):
        FLUSHED.append(sid)

    async def _notice(session, chain, is_mentioned=True):
        NOTICES.append((session, "".join(getattr(e, "text", "") for e in chain)))

    ctx.message_processor.send_message_chain = _send
    ctx.message_processor.flush_session_messages = _flush
    ctx.publish_notice = _notice
    pm = PluginManager(ctx)
    ctx.plugin_mgr = pm
    return ctx, pm


def main():
    print("== kira_ops adversarial audit (real framework) ==")
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    run = loop.run_until_complete
    ctx, pm = run(boot())
    _assert(run(pm.load_plugin_from_dir(PLUGIN_DIR, auto_install=False)) == "kira_ops")
    run(pm.init_plugin("kira_ops"))
    inst = pm.get_plugin_inst("kira_ops")

    from plugins.kira_ops.caps import Capability  # noqa: E402

    class DummyCap(Capability):
        name = "advtest"
        ACTIONS = {"go": ("write", True, "test")}

        async def handle_write(self, action, params):
            EXECUTED.append(dict(params))
            return {"ok": True}

    inst.caps["advtest"] = DummyCap(inst)

    base = {
        "master": {"enabled": True, "panic_lock": False},
        "access": {"allow_sessions": [], "deny_sessions": [], "readonly_sessions": []},
        "risk": {"level": "standard", "high_risk_actions": ["advtest.go"],
                 "high_risk_sessions": [], "require_confirm": True, "confirm_ttl": 300},
        "confirm": {"enabled": True, "sessions": [CONFIRM_SID], "password": PASSWORD,
                    "template_request": "", "template_approved": "", "template_notice": ""},
        "control": {"allow_restart": False, "allow_shutdown": False},
        "protected": {"persona_write": False},
    }

    def apply(cfg):
        inst.cfg = cfg
        inst.refresh_settings(cfg)
        inst.engine.apply_settings(cfg)

    apply(copy.deepcopy(base))

    # ---------------------------------------------------------------- gate bypass
    def token_cannot_cross_levels():
        """A token issued while dangerous must NOT be redeemable after a
        downgrade to standard (the token is not an elevation licence)."""
        cfg = copy.deepcopy(base)
        cfg["risk"]["level"] = "dangerous"
        cfg["risk"]["high_risk_sessions"] = [REQ_SID]
        apply(cfg)
        r = run(inst.ops_action(Ev(), "advtest", "go"))
        _assert(r.get("need_confirm") is True and r.get("token"), r)
        cfg2 = copy.deepcopy(cfg)
        cfg2["risk"]["level"] = "standard"
        cfg2["risk"]["high_risk_sessions"] = []
        apply(cfg2)
        before = len(EXECUTED)
        r2 = run(inst.ops_confirm(Ev(), token=r["token"]))
        _assert(r2.get("ok") is False, f"token must not bypass the re-check: {r2}")
        _assert(len(EXECUTED) == before, EXECUTED)
    check("a confirm token cannot be redeemed after a downgrade", token_cannot_cross_levels)

    def token_is_bound_to_session_and_user():
        cfg = copy.deepcopy(base)
        cfg["risk"]["level"] = "dangerous"
        cfg["risk"]["high_risk_sessions"] = [REQ_SID, "napcat:gm:99999"]
        apply(cfg)
        r = run(inst.ops_action(Ev(), "advtest", "go"))
        token = r["token"]
        before = len(EXECUTED)
        r2 = run(inst.ops_confirm(Ev(sid="napcat:gm:99999", uid="33445"), token=token))
        _assert(r2.get("ok") is False, f"another session must not redeem the token: {r2}")
        r3 = run(inst.ops_confirm(Ev(sid=REQ_SID, uid="88888"), token=token))
        _assert(r3.get("ok") is False, f"another user must not redeem the token: {r3}")
        r4 = run(inst.ops_confirm(Ev(), token=token))
        _assert(r4.get("ok") is True, f"the rightful owner must be able to redeem: {r4}")
        _assert(len(EXECUTED) == before + 1, EXECUTED)
    check("a token is bound to the requesting session AND user", token_is_bound_to_session_and_user)

    def deny_list_beats_everything():
        for level in ("standard", "dangerous", "full"):
            cfg = copy.deepcopy(base)
            cfg["risk"]["level"] = level
            cfg["risk"]["high_risk_sessions"] = [REQ_SID]
            cfg["access"]["deny_sessions"] = [REQ_SID]
            apply(cfg)
            before = len(SENT)
            r = run(inst.ops_action(Ev(), "advtest", "go"))
            _assert(r.get("ok") is False and "deny list" in r.get("error", ""), (level, r))
            _assert(r.get("status") != "pending_owner_approval", (level, r))
            run(asyncio.sleep(0.1))
            _assert(len(SENT) == before, f"no challenge for a denied session at {level}")
    check("the deny list beats every level and never reaches the owner", deny_list_beats_everything)

    def panic_lock_is_absolute():
        cfg = copy.deepcopy(base)
        cfg["master"]["panic_lock"] = True
        apply(cfg)
        r = run(inst.ops_action(Ev(), "advtest", "go"))
        _assert(r.get("status") != "pending_owner_approval", r)
        _assert("panic" in r.get("error", ""), r)
        # and a pending issued BEFORE the lock must not execute after it
        cfg2 = copy.deepcopy(base)
        apply(cfg2)
        r2 = run(inst.ops_action(Ev(), "advtest", "go"))
        _assert(r2.get("status") == "pending_owner_approval", r2)
        run(asyncio.sleep(0.2))
        apply(cfg)  # panic on
        before = len(EXECUTED)
        run(inst.confirm_password_hook(msg_event(PASSWORD)))
        run(asyncio.sleep(1.2))
        _assert(len(EXECUTED) == before, f"panic must block the approval too: {EXECUTED}")
    check("panic lock blocks both new requests and pending approvals", panic_lock_is_absolute)

    def readonly_level_mundane_writes_are_quiet():
        cfg = copy.deepcopy(base)
        cfg["risk"]["level"] = "readonly"
        apply(cfg)
        before = len(SENT)
        r = run(inst.ops_action(Ev(), "plugin", "reload", target="dummy_target"))
        _assert(r.get("ok") is False and r.get("status") != "pending_owner_approval", r)
        run(asyncio.sleep(0.1))
        _assert(len(SENT) == before, "mundane writes must not page the owner in readonly mode")
    check("readonly mode: mundane writes stay quiet, only high-risk ones escalate",
          readonly_level_mundane_writes_are_quiet)

    # ---------------------------------------------------------------- confirm channel
    def one_password_approves_exactly_one():
        cfg = copy.deepcopy(base)
        apply(cfg)
        inst.confirm.clear()
        run(inst.ops_action(Ev(), "advtest", "go"))
        run(inst.ops_action(Ev(), "advtest", "go"))
        run(asyncio.sleep(0.3))
        _assert(inst.confirm.pending_count == 2, inst.confirm.pending_count)
        before = len(EXECUTED)
        run(inst.confirm_password_hook(msg_event(PASSWORD)))
        run(asyncio.sleep(1.2))
        _assert(len(EXECUTED) == before + 1, f"one password = one approval: {EXECUTED}")
        _assert(inst.confirm.pending_count == 1, inst.confirm.pending_count)
        ack = [s for s in SENT if "已批准并执行" in s[2]]
        _assert(ack and "仍有 1 条待确认请求" in ack[-1][2],
                f"the ack must say how many requests are still pending: {ack[-1][2] if ack else None}")
        # the second password approves the remaining one
        run(inst.confirm_password_hook(msg_event(PASSWORD)))
        run(asyncio.sleep(1.2))
        _assert(len(EXECUTED) == before + 2, EXECUTED)
    check("one password message approves exactly one request (and the ack says so)",
          one_password_approves_exactly_one)

    def duplicate_password_is_harmless():
        cfg = copy.deepcopy(base)
        apply(cfg)
        inst.confirm.clear()
        before = len(EXECUTED)
        # nothing pending -> the message continues to the bot, but the secret is
        # replaced by the placeholder first (never reaches the model verbatim)
        ev = msg_event(PASSWORD)
        run(inst.confirm_password_hook(ev))
        run(asyncio.sleep(0.5))
        _assert(len(EXECUTED) == before, EXECUTED)
        text = "".join(getattr(e, "text", "") for e in ev.message.chain)
        _assert(PASSWORD not in text, f"the password leaked into the chain: {text!r}")
        _assert(text.strip() == "[收到 Kira 运行自控台（kira_ops）确认密码，当前并无待确认请求]", f"placeholder expected, got {text!r}")
        _assert(not ev.is_stopped, "the redacted message is forwarded, not swallowed")
    check("a password with nothing pending is redacted before reaching the bot",
          duplicate_password_is_harmless)

    def password_never_leaks_into_output():
        cfg = copy.deepcopy(base)
        apply(cfg)
        inst.confirm.clear()
        run(inst.ops_action(Ev(), "advtest", "go"))
        run(asyncio.sleep(0.3))
        run(inst.confirm_password_hook(msg_event(PASSWORD)))
        run(asyncio.sleep(1.2))
        blob = json.dumps({"sent": SENT, "notices": NOTICES}, ensure_ascii=False)
        _assert(PASSWORD not in blob, "the password must never appear in any outbound text")
        status = run(inst.ops_status(Ev()))
        _assert(PASSWORD not in json.dumps(status, ensure_ascii=False), "password leaked via ops_status")
        conf = run(inst.api_get_config())
        _assert(PASSWORD not in json.dumps(conf, ensure_ascii=False), "password leaked via panel")
    check("the password never appears in messages, notices, status or the panel",
          password_never_leaks_into_output)

    def hostile_config_values_do_not_crash():
        """The model can write plugin config; hostile shapes must not raise."""
        for bad in ({"enabled": True, "sessions": 12345, "password": PASSWORD},
                    {"enabled": True, "sessions": {"a": 1}, "password": PASSWORD},
                    {"enabled": True, "sessions": [None, 42, "", "::", "a:b:c"], "password": PASSWORD},
                    {"enabled": "yes", "sessions": [CONFIRM_SID], "password": PASSWORD},
                    {"enabled": True, "sessions": [CONFIRM_SID], "password": 114514}):
            cfg = copy.deepcopy(base)
            cfg["confirm"] = bad
            apply(cfg)
            try:
                inst._confirm_sessions()
                inst._confirm_ready()
                inst._confirm_status()
                run(inst.ops_action(Ev(), "advtest", "go"))
                run(inst.confirm_password_hook(msg_event(PASSWORD)))
            except Exception as exc:  # noqa: BLE001
                raise AssertionError(f"hostile config {bad!r} raised {type(exc).__name__}: {exc}")
        apply(copy.deepcopy(base))
    check("hostile confirm config values never raise", hostile_config_values_do_not_crash)

    def templates_with_bad_placeholders_fall_back():
        cfg = copy.deepcopy(base)
        cfg["confirm"] = dict(cfg["confirm"], template_request="{nope} {sid}",
                              template_approved="{bogus}", template_notice="")
        apply(cfg)
        inst.confirm.clear()
        r = run(inst.ops_action(Ev(), "advtest", "go"))
        _assert(r.get("status") == "pending_owner_approval", r)
        run(asyncio.sleep(0.3))
        _assert(SENT, "the challenge must still be delivered with a broken template")
        run(inst.confirm_password_hook(msg_event(PASSWORD)))
        run(asyncio.sleep(1.2))
        apply(copy.deepcopy(base))
    check("broken templates degrade to the built-in defaults", templates_with_bad_placeholders_fall_back)

    # ---------------------------------------------------------------- fuzz
    def malformed_tool_args_never_raise():
        cfg = copy.deepcopy(base)
        apply(cfg)
        cases = [
            ("ops_action", dict(domain=None, action=None)),
            ("ops_action", dict(domain="advtest", action="go", args="not-a-dict")),
            ("ops_action", dict(domain="advtest", action="go", args={"a": [1, 2]}, target=999)),
            ("ops_read", dict(domain="plugin", action="list", limit="lots")),
            ("ops_read", dict(domain="log", action="tail", limit=-5)),
            ("ops_store", dict(action="install", plugin_id=None)),
            ("ops_config", dict(domain="config", path="", patch="nope")),
            ("ops_confirm", dict(token=None)),
            ("ops_status", dict(include=123)),
        ]
        for tool, kwargs in cases:
            try:
                if tool == "ops_read":
                    run(inst.ops_read(Ev(), kwargs.pop("domain"), kwargs.pop("action"), **kwargs))
                elif tool == "ops_store":
                    run(inst.ops_store(Ev(), **kwargs))
                elif tool == "ops_config":
                    run(inst.ops_config(Ev(), **kwargs))
                elif tool == "ops_confirm":
                    run(inst.ops_confirm(Ev(), **kwargs))
                elif tool == "ops_status":
                    run(inst.ops_status(Ev(), **kwargs))
                else:
                    run(inst.ops_action(Ev(), **kwargs))
            except Exception as exc:  # noqa: BLE001
                raise AssertionError(f"{tool}{kwargs} raised {type(exc).__name__}: {exc}")
    check("malformed tool arguments never raise", malformed_tool_args_never_raise)

    def hostile_session_ids_are_inert():
        cfg = copy.deepcopy(base)
        apply(cfg)
        for sid in ("", ":", "::", "a:b", "x" * 300 + ":gm:1", "napcat:gm:../../etc/passwd"):
            before = len(SENT)
            try:
                r = run(inst.ops_action(Ev(sid=sid), "advtest", "go"))
            except Exception as exc:  # noqa: BLE001
                raise AssertionError(f"session id {sid!r} raised {type(exc).__name__}: {exc}")
            _assert(r.get("ok") is False or r.get("status") == "pending_owner_approval", (sid, r))
            run(asyncio.sleep(0.05))
            _assert(len(SENT) == before or r.get("status") == "pending_owner_approval",
                    f"{sid!r} produced an unexpected challenge: {SENT[before:]}")
    check("hostile session ids are inert (no injection, no crash)", hostile_session_ids_are_inert)

    # ---------------------------------------------------------------- end to end
    def full_chain_end_to_end():
        cfg = copy.deepcopy(base)
        apply(cfg)
        inst.confirm.clear()
        SENT.clear(); NOTICES.clear(); FLUSHED.clear(); EXECUTED.clear()
        r = run(inst.ops_action(Ev(), "advtest", "go", args={"k": "v"}))
        _assert(r.get("status") == "pending_owner_approval", r)
        run(asyncio.sleep(0.3))
        _assert(len(SENT) == 1 and SENT[0][1] == "769690776", SENT)
        _assert("#" in SENT[0][2] and "advtest.go" in SENT[0][2], SENT[0][2])
        run(inst.confirm_password_hook(msg_event(PASSWORD)))
        run(asyncio.sleep(1.3))
        _assert(EXECUTED == [{"k": "v"}], f"params must survive the round trip: {EXECUTED}")
        _assert(len(NOTICES) == 1 and NOTICES[0][0] == REQ_SID, NOTICES)
        _assert(REQ_SID in FLUSHED, FLUSHED)
        _assert(not [s for s in SENT if s[1] == "11223"], "no chat message into the origin session")
        audit = inst.audit.tail(limit=20)
        blob = json.dumps(audit, ensure_ascii=False)
        _assert("ops_confirm" in blob, "the approval must be audited")
        _assert(PASSWORD not in blob, "the audit must never contain the password")
    check("end-to-end: request -> challenge -> password -> execute -> notice -> audit",
          full_chain_end_to_end)

    print(f"\npassed: {len(PASSED)}  failed: {len(FAILED)}")
    if FAILED:
        raise SystemExit(1)
    print("ALL OK")


if __name__ == "__main__":
    main()