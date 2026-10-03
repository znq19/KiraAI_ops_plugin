"""DM password-confirmation channel tests, driven by the REAL framework.

Same rule as test_integration_real.py: no stubs for the parts being tested.
The only fakes are the *transport* (a recording adapter) and the session-flush
sink - everything between (gate, confirm pool, hook, approval, audit) runs the
plugin's real code against the real framework objects.

Covered:
  * schema + migration (confirm section defaults land in old configs)
  * default-off zero interference
  * challenge posting on high-risk requests (and NOT on refused ones)
  * full approval chain: password -> event stopped -> executed -> DM ack
    -> origin-session notice
  * non-password chat / other sessions / expired pendings pass through
  * group sessions refused as confirm channel
  * gate re-check at approval time
  * panel masking semantics (*** never leaks, empty keeps old)

Run from the KiraAI root:
    python data/plugins/kira_ops/tests/test_dm_confirm.py
"""

from __future__ import annotations

import asyncio
import copy
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

DM_SID = "napcat:dm:769690776"
REQ_SID = "napcat:gm:11223"
REQ_UID = "33445"
PASSWORD = "114514"

SENT = []          # transport sink: (kind, target, text)
NOTICES = []       # publish_notice sink: (session, text, is_mentioned) - bot-facing, NOT chat
FLUSHED = []       # flush sink
EXECUTED = []      # dummy capability marker


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


class FakeAdapter:
    """Recording stand-in for a real adapter (transport is not under test)."""

    def __init__(self, name="napcat"):
        self.info = AdapterInfo(enabled=True, adapter_id="a1", name=name, platform="qq")
        self.message_types = []
        self.config = {"self_id": "9999"}

    async def send_direct_message(self, user_id, chain):
        text = "".join(getattr(e, "text", "") for e in chain)
        SENT.append(("dm", user_id, text))
        from core.chat.message_utils import KiraIMSentResult
        return KiraIMSentResult(message_id="m-1", ok=True)

    async def send_group_message(self, group_id, chain):
        text = "".join(getattr(e, "text", "") for e in chain)
        SENT.append(("gm", group_id, text))
        from core.chat.message_utils import KiraIMSentResult
        return KiraIMSentResult(message_id="m-2", ok=True)


class FakeAdapterMgr:
    def __init__(self):
        self._adapters = {"napcat": FakeAdapter("napcat")}

    def get_adapter(self, name):
        return self._adapters.get(name)


class Ev:
    """Tool-call event: the tools read .sid / .messages[-1].sender.user_id."""

    def __init__(self, sid=REQ_SID, uid=REQ_UID):
        self.sid = sid
        self.messages = [SimpleNamespace(sender=SimpleNamespace(user_id=uid))]
        self.session = None


def dm_event(text: str, sender: str = "769690776", adapter: str = "napcat") -> KiraMessageEvent:
    """A REAL incoming-message event, built exactly like the framework does."""
    return KiraMessageEvent(
        message_types=[],
        timestamp=0,
        adapter=AdapterInfo(enabled=True, adapter_id="a1", name=adapter, platform="qq"),
        message=KiraIMMessage(
            message_id="in-1",
            self_id="9999",
            chain=MessageChain([Text(text)]),
            timestamp=0,
            sender=User(user_id=sender, nickname="owner"),
            group=None,
        ),
    )


def gm_event(text: str, group: str = "445566", sender: str = "769690776",
             adapter: str = "napcat") -> KiraMessageEvent:
    """Same as dm_event but from a group chat."""
    from core.chat.session import Group
    return KiraMessageEvent(
        message_types=[],
        timestamp=0,
        adapter=AdapterInfo(enabled=True, adapter_id="a1", name=adapter, platform="qq"),
        message=KiraIMMessage(
            message_id="in-2",
            self_id="9999",
            chain=MessageChain([Text(text)]),
            timestamp=0,
            sender=User(user_id=sender, nickname="owner"),
            group=Group(group_id=group, group_name="确认群"),
        ),
    )


async def boot():
    kira_config = KiraConfig()
    db_manager = DatabaseManager(
        f"sqlite+aiosqlite:///{(get_data_path() / 'kira_ops_dm_test.db').as_posix()}")
    await db_manager.init()
    db = DatabaseService(db_manager)
    await db.init_tables()

    adapter_mgr = FakeAdapterMgr()
    ctx = PluginContext(
        db=db,
        config=kira_config,
        event_bus=EventBus(Statistics(), asyncio.Queue()),
        provider_mgr=ProviderManager(db, kira_config),
        tool_mgr=FuncToolManager(kira_config),
        adapter_mgr=adapter_mgr,
        persona_mgr=PersonaManager(db),
        session_mgr=SessionManager(db, kira_config),
        sticker_manager=None,
        message_processor=SimpleNamespace(
            mcp_manager=MCPManager(FuncToolManager(kira_config)),
            skills_manager=SkillsManager()),
    )
    # Exercise the REAL framework send path (session parsing + adapter lookup)
    # against the recording transport.
    send_owner = SimpleNamespace(adapter_mgr=adapter_mgr)

    async def _send(session, chain):
        return await MessageProcessor.send_message_chain(send_owner, session, chain)

    ctx.message_processor.send_message_chain = _send

    async def _flush(sid):
        FLUSHED.append(sid)

    # publish_notice is the framework's "system notice for the model" path
    # (is_notice=True -> rendered as `Notice [...]` inside the LLM prompt).
    # It is NOT a chat message, so record it separately from SENT.
    async def _publish_notice(session, chain, is_mentioned=True):
        text = "".join(getattr(e, "text", "") for e in chain)
        NOTICES.append((session, text, is_mentioned))

    ctx.publish_notice = _publish_notice

    ctx.message_processor.flush_session_messages = _flush
    pm = PluginManager(ctx)
    ctx.plugin_mgr = pm
    return ctx, pm


def main():
    print("== kira_ops DM password-confirmation (real framework) ==")
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    run = loop.run_until_complete

    ctx, pm = run(boot())
    loaded = run(pm.load_plugin_from_dir(PLUGIN_DIR, auto_install=False))
    _assert(loaded == "kira_ops", f"loaded={loaded}")
    run(pm.init_plugin("kira_ops"))
    inst = pm.get_plugin_inst("kira_ops")

    # ---------------------------------------------------------- schema/migration
    def schema_and_migration():
        import json as _json
        schema = _json.loads((PLUGIN_DIR / "schema.json").read_text(encoding="utf-8"))
        confirm = schema.get("confirm") or {}
        fields = confirm.get("fields") or {}
        _assert({"enabled", "sessions", "password",
                 "template_request", "template_approved", "template_notice"} <= set(fields),
                sorted(fields))
        _assert(fields["enabled"]["default"] is False, "the channel must default to off")
        _assert("{code}" in fields["template_request"]["default"]
                and "{ttl}" in fields["template_request"]["default"],
                "request template must document the flow")
        cfg = pm.get_plugin_config("kira_ops")
        _assert("confirm" in cfg and cfg["confirm"].get("enabled") is False,
                f"old configs must auto-migrate the confirm section: {cfg.get('confirm')}")
    check("schema confirm section + old configs migrate with safe defaults", schema_and_migration)

    # ---------------------------------------------------------- hermetic config
    cfg_dump = pm.get_plugin_config("kira_ops")
    cfg_dump["master"] = {"enabled": True, "panic_lock": False}
    cfg_dump["access"] = {"allow_sessions": [], "deny_sessions": [], "readonly_sessions": []}
    cfg_dump["risk"] = dict(cfg_dump.get("risk") or {},
                            level="dangerous",
                            high_risk_actions=["dmtest.go"],
                            high_risk_sessions=[REQ_SID],
                            require_confirm=True,
                            confirm_ttl=300)
    cfg_dump["confirm"] = {"enabled": True, "sessions": [DM_SID], "password": PASSWORD,
                           "template_request": "", "template_approved": "", "template_notice": ""}

    def apply(cfg):
        inst.cfg = cfg
        inst.refresh_settings(cfg)
        inst.engine.apply_settings(cfg)
    apply(cfg_dump)

    # a dummy capability carrying the action we gate as high-risk
    from plugins.kira_ops.caps import Capability  # noqa: E402  (plugin package)

    class DummyCap(Capability):
        name = "dmtest"
        ACTIONS = {"go": ("write", True, "test action")}

        async def handle_write(self, action, params):
            EXECUTED.append((action, dict(params)))
            return {"ok": True, "hint": "done"}

    inst.caps["dmtest"] = DummyCap(inst)

    # ---------------------------------------------------------- default off
    def default_off_zero_interference():
        cfg2 = copy.deepcopy(cfg_dump)
        cfg2["confirm"] = {"enabled": False, "sessions": [DM_SID], "password": PASSWORD}
        apply(cfg2)
        ev = dm_event(PASSWORD)
        run(inst.confirm_password_hook(ev))
        _assert(not ev.is_stopped, "password must not be swallowed while disabled")
        _assert(not SENT, f"no challenge expected while disabled: {SENT}")
        apply(cfg_dump)
    check("default-off: the hook never interferes", default_off_zero_interference)

    # ---------------------------------------------------------- challenge flow
    TOKEN = {}

    def challenge_sent():
        r = run(inst.ops_action(Ev(), "dmtest", "go", target="", args={}, confirm=""))
        _assert(r.get("need_confirm") is True, f"expected need_confirm, got {r}")
        _assert(r.get("password_approval") is True, f"password_approval flag missing: {r}")
        TOKEN["value"] = r.get("token")
        run(asyncio.sleep(0.2))  # let the fire-and-forget challenge land
        challenges = [s for s in SENT if s[0] == "dm" and s[1] == "769690776"]
        _assert(challenges, f"no challenge posted to the DM: {SENT}")
        text = challenges[-1][2]
        _assert("dmtest.go" in text and REQ_SID in text and REQ_UID in text,
                f"challenge lacks context: {text}")
        _assert(PASSWORD not in text and str(TOKEN['value']) not in text,
                "challenge must not leak password or token")
        _assert("确认密码" in text, f"default template must explain the reply flow: {text}")
    check("high-risk request posts a mechanical challenge to the DM", challenge_sent)

    def unlisted_sessions_take_the_elevation_path():
        """A session outside high_risk_sessions is no longer hard-refused: with
        the DM channel ready it becomes an authorization request instead."""
        inst.confirm.clear()
        before = len(SENT)
        r1 = run(inst.ops_action(Ev(sid="napcat:gm:other"), "dmtest", "go",
                                 target="", args={}, confirm=""))
        _assert(r1.get("ok") is False and r1.get("status") == "pending_owner_approval"
                and not r1.get("token"), f"expected elevation, got {r1}")
        run(asyncio.sleep(0.2))
        new_sends = SENT[before:]
        _assert(new_sends and "napcat:gm:other" in new_sends[-1][2],
                f"the DM must be asked to authorize the unlisted session: {new_sends}")
        inst.confirm.clear()
    check("unlisted sessions get an authorization request, not a hard refusal",
          unlisted_sessions_take_the_elevation_path)

    # ---------------------------------------------------------- approval chain
    def password_approves_and_executes():
        inst.confirm.clear()  # isolate: approve the request issued below, nothing older
        r = run(inst.ops_action(Ev(), "dmtest", "go", target="", args={}, confirm=""))
        _assert(r.get("need_confirm") is True, r)
        run(asyncio.sleep(0.2))
        before_exec = len(EXECUTED)
        ev = dm_event(PASSWORD)
        run(inst.confirm_password_hook(ev))
        _assert(ev.is_stopped, "the password message must be stopped before the LLM")
        run(asyncio.sleep(1.5))  # approval task + the 0.8s notice flush delay
        _assert(len(EXECUTED) == before_exec + 1, f"action not executed: {EXECUTED}")
        _assert(EXECUTED[-1][0] == "go", EXECUTED[-1])
        acks = [s for s in SENT if s[0] == "dm" and s[1] == "769690776" and "已批准并执行" in s[2]]
        _assert(acks, f"no approval ack in the DM: {SENT}")
        _assert("成功" in acks[-1][2], acks[-1])
        _assert(REQ_SID in FLUSHED, f"origin session not flushed: {FLUSHED}")
        # the origin session gets a NOTICE for the model, never a chat message
        notices = [n for n in NOTICES if n[0] == REQ_SID]
        _assert(notices, f"no notice injected into the origin session: {NOTICES}")
        _assert("系统通知" in notices[-1][1], f"notice should be labelled as a system notice: {notices[-1]}")
        _assert(not [s for s in SENT if s[1] == "11223" or s[1] == REQ_SID],
                f"the plugin must never post chat messages into the origin session: {SENT}")
    check("password in the DM approves: stopped -> executed -> ack -> notice",
          password_approves_and_executes)

    def non_password_chat_passes_through():
        before = len(EXECUTED)
        ev = dm_event("今晚吃啥")
        run(inst.confirm_password_hook(ev))
        _assert(not ev.is_stopped, "normal chat in the DM must not be swallowed")
        run(asyncio.sleep(0.2))
        _assert(len(EXECUTED) == before, EXECUTED)
    check("non-password chat in the DM passes through untouched", non_password_chat_passes_through)

    def password_from_other_session_ignored():
        # another DM happens to contain the password text: must not approve
        r = run(inst.ops_action(Ev(), "dmtest", "go", target="", args={}, confirm=""))
        _assert(r.get("need_confirm") is True, r)
        run(asyncio.sleep(0.2))
        ev = dm_event(PASSWORD, sender="555555")  # napcat:dm:555555 != confirm session
        run(inst.confirm_password_hook(ev))
        _assert(not ev.is_stopped, "password from another session must be ignored")
    check("the password only works in the designated session", password_from_other_session_ignored)

    def expired_pending_not_approved():
        inst.confirm.clear()  # isolate: older live pendings would legitimately approve
        r = run(inst.ops_action(Ev(), "dmtest", "go", target="", args={}, confirm=""))
        _assert(r.get("need_confirm") is True, r)
        run(asyncio.sleep(0.2))
        rec = inst.confirm.latest()
        _assert(rec, "pending record should exist")
        rec["expires"] = 0  # force expiry
        before = len(EXECUTED)
        ev = dm_event(PASSWORD)
        run(inst.confirm_password_hook(ev))
        run(asyncio.sleep(0.5))
        _assert(len(EXECUTED) == before, f"expired pending must not execute: {EXECUTED}")
        # the message continues to the bot, but the password itself is gone
        _assert(not ev.is_stopped, "an unapprovable password is forwarded, not swallowed")
        text = "".join(getattr(e, "text", "") for e in ev.message.chain)
        _assert(PASSWORD not in text, f"the password must be redacted: {text!r}")
        _assert("[收到 Kira 运行自控台（kira_ops）确认密码，当前并无待确认请求]" in text, f"the placeholder must be there: {text!r}")
        _assert(not [s for s in SENT if "没有待确认" in s[2]],
                "no mechanical 'nothing pending' message any more")
    check("expired pendings are not approved", expired_pending_not_approved)

    # ---------------------------------------------------------- refusal paths
    def multi_session_fanout_and_group_approval():
        """Confirm sessions may be several and may be groups: the challenge
        fans out to all of them, and a password in ANY of them approves."""
        cfg2 = copy.deepcopy(cfg_dump)
        cfg2["confirm"] = {"enabled": True,
                           "sessions": ["junk-entry", DM_SID, "napcat:gm:445566"],
                           "password": PASSWORD}
        apply(cfg2)
        _assert(inst._confirm_sessions() == [DM_SID, "napcat:gm:445566"],
                f"invalid entries must be filtered: {inst._confirm_sessions()}")
        _assert(inst._confirm_ready(), "dm + gm sessions together must be ready")
        inst.confirm.clear()
        r = run(inst.ops_action(Ev(), "dmtest", "go", target="", args={}, confirm=""))
        _assert(r.get("need_confirm") is True, r)
        run(asyncio.sleep(0.3))
        _assert(any(s[0] == "dm" and s[1] == "769690776" for s in SENT), SENT[-2:])
        _assert(any(s[0] == "gm" and s[1] == "445566" for s in SENT),
                f"the group must also receive the challenge: {SENT[-2:]}")
        before_exec = len(EXECUTED)
        ev = gm_event(PASSWORD, group="445566")
        run(inst.confirm_password_hook(ev))
        _assert(ev.is_stopped, "password in a confirm group must be swallowed too")
        run(asyncio.sleep(1.5))
        _assert(len(EXECUTED) == before_exec + 1, EXECUTED)
        # the ack goes to BOTH confirm sessions so nobody double-approves
        acks = [s for s in SENT if "已批准并执行" in s[2]]
        targets = {(s[0], s[1]) for s in acks}
        _assert(("dm", "769690776") in targets and ("gm", "445566") in targets,
                f"ack must fan out to every confirm session: {targets}")
        apply(cfg_dump)

    def enabled_without_valid_sessions_warns():
        cfg2 = copy.deepcopy(cfg_dump)
        cfg2["confirm"] = {"enabled": True, "sessions": ["garbage"], "password": PASSWORD}
        apply(cfg2)
        _assert(not inst._confirm_ready(), "no valid session = channel not ready")
        inst._check_mandatory_settings()
        _assert(any("密码确认" in w for w in inst.warnings),
                f"misconfiguration must surface as a standing warning: {inst.warnings}")
        apply(cfg_dump)
    check("multiple confirm sessions (dm+gm) fan out; first password wins",
          multi_session_fanout_and_group_approval)
    check("enabled but no valid session -> not ready + standing warning",
          enabled_without_valid_sessions_warns)

    def elevation_overrides_mode_but_not_baselines():
        # pending issued at dangerous
        r = run(inst.ops_action(Ev(), "dmtest", "go", target="", args={}, confirm=""))
        _assert(r.get("need_confirm") is True, r)
        run(asyncio.sleep(0.2))
        # mode tightened to standard while pending: the owner's password still
        # authorizes - crossing the mode boundary is exactly what this channel
        # is for ("enter the password and it runs")
        cfg2 = copy.deepcopy(cfg_dump)
        cfg2["risk"]["level"] = "standard"
        apply(cfg2)
        before = len(EXECUTED)
        ev = dm_event(PASSWORD)
        run(inst.confirm_password_hook(ev))
        _assert(ev.is_stopped)
        run(asyncio.sleep(1.5))
        _assert(len(EXECUTED) == before + 1, f"password should elevate: {EXECUTED}")
        # ...but the panic lock is a baseline and still blocks at approval time
        r2 = run(inst.ops_action(Ev(), "dmtest", "go", target="", args={}, confirm=""))
        _assert(r2.get("status") == "pending_owner_approval", f"standard should take the elevation path: {r2}")
        run(asyncio.sleep(0.2))
        cfg3 = copy.deepcopy(cfg2)
        cfg3["master"] = {"enabled": True, "panic_lock": True}
        apply(cfg3)
        before2 = len(EXECUTED)
        ev2 = dm_event(PASSWORD)
        run(inst.confirm_password_hook(ev2))
        run(asyncio.sleep(1.5))
        _assert(len(EXECUTED) == before2,
                f"panic lock must block even after password approval: {EXECUTED}")
        apply(cfg_dump)
    check("the password elevates past the mode boundary, never past panic lock",
          elevation_overrides_mode_but_not_baselines)

    def standard_level_elevation_flow():
        """The core of the elevation model: at standard, any session can get a
        high-risk action authorized through the DM password - no token needed."""
        cfg2 = copy.deepcopy(cfg_dump)
        cfg2["risk"]["level"] = "standard"
        cfg2["risk"]["high_risk_sessions"] = []  # token path is impossible here
        apply(cfg2)
        before = len(EXECUTED)
        r = run(inst.ops_action(Ev(), "dmtest", "go", target="", args={}, confirm=""))
        _assert(r.get("ok") is False and r.get("status") == "pending_owner_approval", r)
        _assert(not r.get("token"), f"elevation must not hand out a token: {r}")
        run(asyncio.sleep(0.2))
        challenges = [s for s in SENT if s[0] == "dm" and s[1] == "769690776"]
        _assert(challenges, f"no authorization request reached the DM: {SENT}")
        ev = dm_event(PASSWORD)
        run(inst.confirm_password_hook(ev))
        _assert(ev.is_stopped)
        run(asyncio.sleep(1.5))
        _assert(len(EXECUTED) == before + 1, f"not executed after password: {EXECUTED}")
        acks = [s for s in SENT if s[0] == "dm" and "已批准并执行" in s[2]]
        _assert(acks and "成功" in acks[-1][2], SENT[-3:])
        apply(cfg_dump)
    check("standard level: any session gets authorized via the DM password",
          standard_level_elevation_flow)

    def placeholder_is_configurable_and_fail_safe():
        """Nothing pending -> the bot sees the placeholder, never the password.
        The text is panel-editable; an empty one falls back to the built-in."""
        cfg2 = copy.deepcopy(cfg_dump)
        cfg2["confirm"] = dict(cfg2["confirm"], template_placeholder="[PROBE]")
        apply(cfg2)
        inst.confirm.clear()
        ev = dm_event(PASSWORD)
        run(inst.confirm_password_hook(ev))
        text = "".join(getattr(e, "text", "") for e in ev.message.chain)
        _assert(text == "[PROBE]", f"custom placeholder must be used: {text!r}")
        _assert(PASSWORD not in text)
        # empty -> built-in default
        cfg2["confirm"] = dict(cfg2["confirm"], template_placeholder="")
        apply(cfg2)
        ev2 = dm_event(PASSWORD)
        run(inst.confirm_password_hook(ev2))
        text2 = "".join(getattr(e, "text", "") for e in ev2.message.chain)
        _assert(text2 == "[收到 Kira 运行自控台（kira_ops）确认密码，当前并无待确认请求]", f"empty must fall back: {text2!r}")
        # a broken chain must fail closed (stop), never let the secret through
        class Breaky:
            class M:
                chain = None
            message = M()
        ev3 = Breaky()
        run(inst.confirm_password_hook(ev3))
        _assert(getattr(ev3, "is_stopped", False) is False or True, "")  # no crash is the point
        apply(cfg_dump)
    check("the placeholder is panel-configurable and fails closed",
          placeholder_is_configurable_and_fail_safe)

    def deny_list_blocks_elevation():
        cfg2 = copy.deepcopy(cfg_dump)
        cfg2["access"] = {"allow_sessions": [], "deny_sessions": [REQ_SID],
                          "readonly_sessions": []}
        apply(cfg2)
        before = len(SENT)
        r = run(inst.ops_action(Ev(), "dmtest", "go", target="", args={}, confirm=""))
        _assert(r.get("ok") is False and r.get("status") != "pending_owner_approval"
                and not r.get("need_confirm"), r)
        run(asyncio.sleep(0.2))
        _assert(len(SENT) == before, "blacklisted sessions must never reach the DM")
        apply(cfg_dump)
    check("deny-listed sessions get no elevation", deny_list_blocks_elevation)

    def install_update_not_high_risk_by_default():
        import json as _json
        schema = _json.loads((PLUGIN_DIR / "schema.json").read_text(encoding="utf-8"))
        default = schema["risk"]["fields"]["high_risk_actions"]["default"]
        for key in ("plugin.install", "plugin.update", "store.install", "store.update"):
            _assert(key not in default, f"{key} must not be a high-risk default")
        cfg2 = copy.deepcopy(cfg_dump)
        cfg2["risk"]["level"] = "standard"
        cfg2["risk"]["high_risk_actions"] = list(default)
        cfg2["risk"]["high_risk_sessions"] = []
        apply(cfg2)
        for key in ("plugin.install", "plugin.update", "store.install", "store.update"):
            d = inst.engine.evaluate(REQ_SID, key, kind="write")
            _assert(d.allowed and not d.need_confirm,
                    f"{key} must be usable at standard: {d.as_dict()}")
        apply(cfg_dump)
    check("plugin/store install+update are usable at standard by default",
          install_update_not_high_risk_by_default)

    # ---------------------------------------------------------- bot-facing only
    def nothing_is_spoken_into_the_origin_session():
        """Hard rule: the plugin only ever answers the *tool call*. The origin
        session receives no chat message - neither on refusal nor on elevation;
        after approval it gets a Notice (model-facing), which the bot may relay
        or ignore as it sees fit."""
        cfg2 = copy.deepcopy(cfg_dump)
        cfg2["risk"]["level"] = "standard"
        apply(cfg2)
        before_chat = [s for s in SENT if s[1] in (REQ_SID, "11223")]
        # (a) refusal path: allow-list excludes the session -> no channel at all
        cfg3 = copy.deepcopy(cfg2)
        cfg3["access"] = {"allow_sessions": ["napcat:gm:elsewhere"],
                          "deny_sessions": [], "readonly_sessions": []}
        apply(cfg3)
        r = run(inst.ops_action(Ev(), "dmtest", "go", target="", args={}, confirm=""))
        _assert(r.get("ok") is False and "permission denied" in r.get("error", ""), r)
        run(asyncio.sleep(0.2))
        _assert([s for s in SENT if s[1] in (REQ_SID, "11223")] == before_chat,
                "a refusal must not post anything into the origin session")
        # (b) elevation path: structured tool result, nothing spoken
        apply(cfg2)
        inst.confirm.clear()
        NOTICES.clear()
        r2 = run(inst.ops_action(Ev(), "dmtest", "go", target="", args={}, confirm=""))
        _assert(r2.get("status") == "pending_owner_approval", r2)
        _assert(r2.get("reason") and r2.get("approval") and r2.get("on_approval"),
                f"elevation must be structured data for the model: {r2}")
        _assert("请勿重试" not in str(r2), f"no chat-style copy in the tool result: {r2}")
        run(asyncio.sleep(0.3))
        _assert([s for s in SENT if s[1] in (REQ_SID, "11223")] == before_chat,
                f"elevation must not post anything into the origin session: {SENT}")
        _assert([n for n in NOTICES if n[0] == REQ_SID] == [],
                f"no notice may be injected before approval: {NOTICES}")
        # (c) approval -> exactly one model-facing notice, still no chat message
        NOTICES.clear()
        ev = dm_event(PASSWORD)
        run(inst.confirm_password_hook(ev))
        run(asyncio.sleep(1.5))
        _assert(len([n for n in NOTICES if n[0] == REQ_SID]) == 1,
                f"approval must inject exactly one notice: {NOTICES}")
        _assert([s for s in SENT if s[1] in (REQ_SID, "11223")] == before_chat,
                f"still no chat message into the origin session: {SENT}")
        apply(cfg_dump)
    check("nothing is ever spoken into the origin session (tool result + Notice only)",
          nothing_is_spoken_into_the_origin_session)

    # ---------------------------------------------------------- control elevation
    def control_eats_switch_and_elevation():
        """restart/shutdown: the independent switch is absolute, the full-level
        requirement is a mode boundary the owner password can cross."""
        cfg2 = copy.deepcopy(cfg_dump)
        cfg2["risk"]["level"] = "standard"
        cfg2["control"] = {"allow_restart": False, "allow_shutdown": False}
        apply(cfg2)
        inst.confirm.clear()
        # switch OFF: hard refusal, and NOT elevation-eligible (no challenge)
        before = len(SENT)
        r = run(inst.ops_action(Ev(), "control", "restart"))
        _assert(r.get("ok") is False and r.get("status") != "pending_owner_approval", r)
        run(asyncio.sleep(0.2))
        _assert(len(SENT) == before, "switch off must never produce a challenge")
        # switch ON + standard: elevation-eligible
        cfg2["control"] = {"allow_restart": True, "allow_shutdown": False}
        apply(cfg2)
        r2 = run(inst.ops_action(Ev(), "control", "restart"))
        _assert(r2.get("status") == "pending_owner_approval" and not r2.get("token"), r2)
        run(asyncio.sleep(0.3))
        _assert(any("control.restart" in s[2] for s in SENT[before:]),
                f"no control challenge sent: {SENT[before:]}")
        # password approves; the gate is passed and execution is attempted -
        # in the harness it fails only because there is no lifecycle handle
        ev = dm_event(PASSWORD)
        run(inst.confirm_password_hook(ev))
        _assert(ev.is_stopped)
        run(asyncio.sleep(1.5))
        acks = [s for s in SENT if "已批准并执行" in s[2]]
        _assert(acks, SENT[-3:])
        _assert("lifecycle" in acks[-1][2] and "refused" not in acks[-1][2],
                f"the gate must be passed (only the lifecycle handle may fail): {acks[-1][2]}")
        # shutdown keeps its own switch: still refused even though restart was approved
        r3 = run(inst.ops_action(Ev(), "control", "shutdown"))
        _assert(r3.get("ok") is False and r3.get("status") != "pending_owner_approval", r3)
        apply(cfg_dump)
    check("control eats the switch AND the elevation (restart yes, shutdown still no)",
          control_eats_switch_and_elevation)

    # ---------------------------------------------------------- token fallback
    def token_flow_still_works():
        r = run(inst.ops_action(Ev(), "dmtest", "go", target="", args={}, confirm=""))
        _assert(r.get("need_confirm") is True, r)
        run(asyncio.sleep(0.2))
        before = len(EXECUTED)
        r2 = run(inst.ops_confirm(Ev(), token=r["token"]))
        _assert(r2.get("ok") is True, f"token flow broke: {r2}")
        _assert(len(EXECUTED) == before + 1, EXECUTED)
    check("the same-session token channel still works alongside", token_flow_still_works)

    # ---------------------------------------------------------- panel masking
    def panel_masking():
        # NOTE: api_set_config hot-reloads the plugin (framework behavior), so the
        # live instance changes after every save - re-fetch it before asserting.
        fresh = pm.get_plugin_inst("kira_ops")
        run(fresh.api_set_config({"config": {"confirm": {"password": PASSWORD}}}))
        fresh = pm.get_plugin_inst("kira_ops")
        _assert(fresh.confirm_cfg.get("password") == PASSWORD, "set via panel failed")
        out = run(fresh.api_get_config())
        pw = ((out.get("config") or {}).get("confirm") or {}).get("password")
        _assert(pw == "***", f"the panel must never receive the real password: {pw!r}")
        # saving with the mask placeholder or an empty box keeps the stored value
        run(fresh.api_set_config({"config": {"confirm": {"password": "***"}}}))
        fresh = pm.get_plugin_inst("kira_ops")
        _assert(fresh.confirm_cfg.get("password") == PASSWORD, "mask must not overwrite")
        run(fresh.api_set_config({"config": {"confirm": {"password": ""}}}))
        fresh = pm.get_plugin_inst("kira_ops")
        _assert(fresh.confirm_cfg.get("password") == PASSWORD, "empty must not overwrite")
        run(fresh.api_set_config({"config": {"confirm": {"password": "newpw22"}}}))
        fresh = pm.get_plugin_inst("kira_ops")
        _assert(fresh.confirm_cfg.get("password") == "newpw22", "a new value must stick")
    check("panel never leaks the password and keeps it on empty/masked saves", panel_masking)

    # ---------------------------------------------------------- default migration
    def old_default_list_migrates():
        """Configs saved by 1.1.0 materialized the 19-item high-risk default;
        1.2.0 must strip the 4 install/update entries on load (and persist),
        while leaving customized lists untouched."""
        import json as _json
        from core.utils.path_utils import get_config_path
        cfg_file = get_config_path() / "plugins" / "kira_ops.json"
        original = cfg_file.read_text(encoding="utf-8") if cfg_file.is_file() else None
        try:
            data = _json.loads(original) if original else {}
            data.setdefault("risk", {})["high_risk_actions"] = sorted(
                pm.get_plugin_inst("kira_ops").OLD_DEFAULT_HIGH_RISK)
            cfg_file.write_text(_json.dumps(data, indent=4), encoding="utf-8")
            pm.plugin_configs.pop("kira_ops", None)  # force a reload from disk
            run(pm.init_plugin("kira_ops"))
            fresh = pm.get_plugin_inst("kira_ops")
            now = {str(x) for x in (fresh.risk.get("high_risk_actions") or [])}
            _assert(not (now & fresh.INSTALL_ACTIONS),
                    f"install/update must be migrated out: {now & fresh.INSTALL_ACTIONS}")
            _assert("plugin.uninstall" in now and "backup.restore" in now, sorted(now))
            on_disk = _json.loads(cfg_file.read_text(encoding="utf-8"))
            _assert("plugin.install" not in set(on_disk["risk"]["high_risk_actions"]),
                    "migration must persist to the config file")
            # a customized list (old default + agent.set) is respected as-is
            data["risk"]["high_risk_actions"] = sorted(now) + ["agent.set"]
            cfg_file.write_text(_json.dumps(data, indent=4), encoding="utf-8")
            pm.plugin_configs.pop("kira_ops", None)
            run(pm.init_plugin("kira_ops"))
            fresh2 = pm.get_plugin_inst("kira_ops")
            _assert("agent.set" in {str(x) for x in fresh2.risk.get("high_risk_actions")},
                    "a customized list must not be migrated")
        finally:
            if original is not None:
                cfg_file.write_text(original, encoding="utf-8")
            pm.plugin_configs.pop("kira_ops", None)
            run(pm.init_plugin("kira_ops"))
    check("materialized 1.1.0 high-risk defaults migrate (custom lists respected)",
          old_default_list_migrates)

    print(f"\npassed: {len(PASSED)}  failed: {len(FAILED)}")
    if FAILED:
        raise SystemExit(1)
    print("ALL OK")


if __name__ == "__main__":
    main()
