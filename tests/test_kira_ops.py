"""Offline self-tests for kira_ops (run inside the KiraAI project root).

Covers the framework-independent core (permission / redact / backup /
confirm) plus an import smoke test that loads main.py the same way the
plugin loader does, verifying the tool/page/api decorators register.

Run:
    python data/plugins/kira_ops/tests/test_kira_ops.py
"""

from __future__ import annotations

import importlib
import shutil
import sys
import tempfile
import types
from pathlib import Path

FILE = Path(__file__).resolve()
ROOT = FILE.parents[4]          # KiraAI root
PLUGIN_DIR = FILE.parents[1]    # data/plugins/kira_ops

FAILED = []
PASSED = []


def check(name, fn):
    try:
        fn()
        PASSED.append(name)
        print(f"  ok  {name}")
    except Exception as exc:
        FAILED.append((name, exc))
        print(f"FAIL  {name}: {exc}")


def bootstrap():
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    if "plugins" not in sys.modules:
        pkg = types.ModuleType("plugins")
        pkg.__path__ = [str(ROOT / "data" / "plugins")]
        sys.modules["plugins"] = pkg
    return importlib.import_module("plugins.kira_ops.main")


# ----------------------------------------------------------------------

def main():
    print("== kira_ops self-tests ==")
    mod = bootstrap()

    perm_mod = importlib.import_module("plugins.kira_ops.core.permission")
    redact = importlib.import_module("plugins.kira_ops.core.redact")
    backup_mod = importlib.import_module("plugins.kira_ops.core.backup")
    confirm_mod = importlib.import_module("plugins.kira_ops.core.confirm")

    base_cfg = {
        "master": {"enabled": True, "panic_lock": False},
        "access": {"allow_sessions": [], "deny_sessions": [], "readonly_sessions": []},
        "risk": {
            "level": "standard",
            "high_risk_actions": ["plugin.uninstall", "plugin.disable", "session.delete"],
            "high_risk_sessions": [],
            "require_confirm": True,
            "confirm_ttl": 300,
        },
        "control": {"allow_restart": False, "allow_shutdown": False},
        "protected": {
            "read_mask": ["api_key", "access_token", "secret", "password", "credential",
                          "token", "private_key"],
            "write_deny": ["api_key", "access_token", "secret", "password", "credential",
                           "private_key"],
            "write_allow": ["base_url", "api_base", "endpoint", "model_id", "model_name",
                            "models", "timeout", "temperature", "headers", "enabled",
                            "remark", "display_name"],
            "path_deny_read": ["data/webui.json"],
            "path_deny_write": ["core", "webui"],
            "path_deny_delete": ["core", "data/plugins"],
            "persona_write": False,
        },
    }

    def engine(cfg=None):
        return perm_mod.PermissionEngine(cfg or base_cfg)

    # ---- permission: allow list empty = everyone --------------------
    def t_allow_empty():
        e = engine()
        assert e.evaluate("qq:gm:1", "plugin.reload", "write").allowed
        assert e.evaluate("qq:dm:9", "plugin.reload", "write").allowed
    check("allow list empty = everyone allowed", t_allow_empty)

    # ---- blacklist wins ---------------------------------------------
    def t_deny_first():
        cfg = dict(base_cfg)
        cfg["access"] = {"allow_sessions": ["qq:gm:1"], "deny_sessions": ["qq:gm:1"],
                         "readonly_sessions": []}
        e = engine(cfg)
        d = e.evaluate("qq:gm:1", "plugin.reload", "write")
        assert not d.allowed and "deny" in d.reason
    check("blacklist checked before allow list", t_deny_first)

    # ---- allow list non-empty restricts ------------------------------
    def t_allow_restrict():
        cfg = dict(base_cfg)
        cfg["access"] = {"allow_sessions": ["qq:gm:1"], "deny_sessions": [],
                         "readonly_sessions": []}
        e = engine(cfg)
        assert e.evaluate("qq:gm:1", "plugin.reload", "write").allowed
        assert not e.evaluate("qq:gm:2", "plugin.reload", "write").allowed
    check("non-empty allow list restricts others", t_allow_restrict)

    # ---- read-only sessions ------------------------------------------
    def t_readonly():
        cfg = dict(base_cfg)
        cfg["access"] = {"allow_sessions": [], "deny_sessions": [],
                         "readonly_sessions": ["qq:gm:1"]}
        e = engine(cfg)
        assert e.evaluate("qq:gm:1", "plugin.list", "read").allowed
        assert not e.evaluate("qq:gm:1", "plugin.reload", "write").allowed
    check("read-only session blocks writes only", t_readonly)

    # ---- panic lock ---------------------------------------------------
    def t_panic():
        cfg = dict(base_cfg)
        cfg["master"] = {"enabled": True, "panic_lock": True}
        e = engine(cfg)
        assert e.evaluate("x", "plugin.list", "read").allowed
        assert not e.evaluate("x", "plugin.reload", "write").allowed
    check("panic lock downgrades writes", t_panic)

    # ---- level readonly ----------------------------------------------
    def t_level_readonly():
        cfg = dict(base_cfg)
        cfg["risk"] = dict(base_cfg["risk"], level="readonly")
        e = engine(cfg)
        assert e.evaluate("x", "plugin.list", "read").allowed
        assert not e.evaluate("x", "plugin.reload", "write").allowed
    check("readonly level forbids writes", t_level_readonly)

    # ---- high-risk: empty session list = nobody -----------------------
    def t_hr_empty():
        e = engine()
        d = e.evaluate("qq:gm:1", "plugin.uninstall", "write")
        assert not d.allowed
    check("high-risk with empty session list = denied", t_hr_empty)

    # ---- high-risk happy path: dangerous + listed + confirm -----------
    def t_hr_ok():
        cfg = dict(base_cfg)
        cfg["risk"] = dict(base_cfg["risk"], level="dangerous",
                           high_risk_sessions=["qq:gm:1"])
        e = engine(cfg)
        d = e.evaluate("qq:gm:1", "plugin.uninstall", "write")
        assert d.allowed and d.need_confirm and d.high_risk
        d2 = e.evaluate("qq:gm:2", "plugin.uninstall", "write")
        assert not d2.allowed
    check("high-risk requires dangerous + listed session + confirm", t_hr_ok)

    # ---- DM elevation: the owner's password crosses the mode boundary -----
    def t_elevated():
        e = engine()
        d = e.evaluate("qq:gm:1", "plugin.uninstall", "write", elevated=True)
        assert d.allowed and d.high_risk and not d.need_confirm
        # baseline checks still apply under elevation
        cfg = dict(base_cfg)
        cfg["access"] = {"allow_sessions": [], "deny_sessions": ["qq:gm:1"],
                         "readonly_sessions": []}
        assert not engine(cfg).evaluate(
            "qq:gm:1", "plugin.uninstall", "write", elevated=True).allowed
        cfg2 = dict(base_cfg)
        cfg2["master"] = {"enabled": True, "panic_lock": True}
        assert not engine(cfg2).evaluate(
            "qq:gm:1", "plugin.uninstall", "write", elevated=True).allowed
        cfg3 = dict(base_cfg)
        cfg3["access"] = {"allow_sessions": [], "deny_sessions": [],
                          "readonly_sessions": ["qq:gm:1"]}
        assert not engine(cfg3).evaluate(
            "qq:gm:1", "plugin.uninstall", "write", elevated=True).allowed
        # normal writes are unaffected by the flag
        assert e.evaluate("qq:gm:1", "plugin.reload", "write", elevated=True).allowed
    check("elevated crosses the mode boundary but never the baselines", t_elevated)

    # ---- control: switch is absolute, full-level is elevatable ------------
    def t_control_elevation():
        cfg = dict(base_cfg)
        cfg["control"] = {"allow_restart": True, "allow_shutdown": False}
        cfg["risk"] = dict(base_cfg["risk"], level="standard")
        e = engine(cfg)
        d = e.evaluate_control("qq:gm:1", "control.restart")
        assert not d.allowed and d.high_risk  # refused but elevation-eligible
        d2 = e.evaluate_control("qq:gm:1", "control.restart", elevated=True)
        assert d2.allowed and d2.high_risk and not d2.need_confirm
        # switch off: never elevatable
        d3 = e.evaluate_control("qq:gm:1", "control.shutdown", elevated=True)
        assert not d3.allowed and not d3.high_risk
        # full level keeps the token flow
        cfg["risk"] = dict(cfg["risk"], level="full")
        d4 = engine(cfg).evaluate_control("qq:gm:1", "control.restart")
        assert d4.allowed and d4.need_confirm
    check("control: switch absolute, full-level elevatable by the owner password",
          t_control_elevation)

    # ---- readonly level: only high-risk rejections are elevatable ----------
    def t_readonly_marking():
        cfg = dict(base_cfg)
        cfg["risk"] = dict(base_cfg["risk"], level="readonly")
        e = engine(cfg)
        d = e.evaluate("qq:gm:1", "plugin.uninstall", "write")
        assert not d.allowed and d.high_risk          # may be DM-elevated
        d2 = e.evaluate("qq:gm:1", "plugin.reload", "write")
        assert not d2.allowed and not d2.high_risk    # mundane writes: no elevation
        d3 = e.evaluate("qq:gm:1", "plugin.uninstall", "write", elevated=True)
        assert d3.allowed
    check("readonly level: high-risk marked elevatable, mundane writes not",
          t_readonly_marking)

    # ---- high-risk refusals are marked elevation-eligible ------------------
    def t_high_risk_marked():
        e = engine()
        d = e.evaluate("qq:gm:1", "plugin.uninstall", "write")
        assert not d.allowed and d.high_risk
        d2 = e.evaluate("qq:gm:1", "plugin.reload", "write")
        assert d2.allowed and not d2.high_risk
    check("high-risk refusals carry the elevation-eligible marker", t_high_risk_marked)

    # ---- control: independent switches, default off -------------------
    def t_control():
        e = engine()
        assert not e.evaluate_control("x", "control.restart").allowed
        assert not e.evaluate_control("x", "control.shutdown").allowed
        cfg = dict(base_cfg)
        cfg["control"] = {"allow_restart": True, "allow_shutdown": False}
        cfg["risk"] = dict(base_cfg["risk"], level="full")
        e2 = engine(cfg)
        d = e2.evaluate_control("x", "control.restart")
        assert d.allowed and d.need_confirm
        assert not e2.evaluate_control("x", "control.shutdown").allowed
        cfg2 = dict(base_cfg)
        cfg2["control"] = {"allow_restart": True, "allow_shutdown": True}
        e3 = engine(cfg2)  # standard level
        assert not e3.evaluate_control("x", "control.restart").allowed
    check("restart/shutdown independent, need full + switch", t_control)

    # ---- redact: keyword matching -------------------------------------
    def t_keywords():
        assert redact.key_matches("api_key", ["api_key"])
        assert redact.key_matches("apiKey", ["api_key"])
        assert redact.key_matches("access_token", ["token"])
        assert not redact.key_matches("max_tokens", ["token"])
        assert not redact.key_matches("base_url", ["token"])
    check("keyword matching is component aware", t_keywords)

    # ---- redact: masking ----------------------------------------------
    def t_mask():
        data = {"api_key": "secret-value", "base_url": "https://x",
                "nested": {"client_secret": "s"}, "max_tokens": 123}
        masked = redact.mask_data(data, base_cfg["protected"]["read_mask"])
        assert masked["api_key"] == "***"
        assert masked["base_url"] == "https://x"
        assert masked["nested"]["client_secret"] == "***"
        assert masked["max_tokens"] == 123
    check("read mask hides secrets, keeps harmless fields", t_mask)

    # ---- redact: write guards ------------------------------------------
    def t_write_guard():
        p = base_cfg["protected"]
        ok, _ = redact.check_field_write("base_url", p, restrict=True)
        assert ok
        ok2, _ = redact.check_field_write("api_key", p, restrict=True)
        assert not ok2
        ok3, _ = redact.check_field_write("arbitrary", p, restrict=True)
        assert not ok3
        result = redact.check_patch({"provider_config": {"base_url": "u", "api_key": "k"}}, p, restrict=True)
        assert not result["ok"]
        assert any("api_key" in v for v in result["violations"])
    check("write guards: deny list + allow list", t_write_guard)

    # ---- confirm pool ---------------------------------------------------
    def t_confirm():
        pool = confirm_mod.ConfirmPool(ttl=60)
        rec = pool.issue({"cap": "plugin", "action": "x"}, sid="s1", uid="u1")
        ok_tok, payload = pool.take(rec["token"], sid="s2", uid="u1")
        assert not ok_tok
        ok_tok2, payload2 = pool.take(rec["token"], sid="s1", uid="u1")
        assert ok_tok2 and payload2["cap"] == "plugin"
        ok_tok3, _ = pool.take(rec["token"], sid="s1", uid="u1")
        assert not ok_tok3  # one-shot
    check("confirm token: session/user bound, one-shot", t_confirm)

    # ---- backup: snapshot / restore / conflict --------------------------
    def t_backup():
        tmp = Path(tempfile.mkdtemp(prefix="kira_ops_test_"))
        try:
            target = tmp / "cfg.json"
            target.write_text('{"v":1}', encoding="utf-8")
            bm = backup_mod.BackupManager(tmp / "plugin_data", {"enabled": True})
            snap = bm.snapshot([target], "cfg", reason="test")
            assert snap.get("id"), snap
            target.write_text('{"v":2}', encoding="utf-8")
            bm.mark_applied(snap["id"])
            target.write_text('{"v":3}', encoding="utf-8")   # external edit
            res = bm.restore(snap["id"])
            assert res.get("need_force") and not res.get("ok")
            res2 = bm.restore(snap["id"], force=True)
            assert res2.get("ok")
            assert target.read_text(encoding="utf-8") == '{"v":1}'
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    check("backup snapshot + conflict aware restore", t_backup)

    # ---- backup: cleanup keeps newest ------------------------------------
    def t_backup_cleanup():
        tmp = Path(tempfile.mkdtemp(prefix="kira_ops_test_"))
        try:
            target = tmp / "f.json"
            bm = backup_mod.BackupManager(tmp / "b", {"enabled": True, "keep_last": 2})
            import time as _t
            for i in range(4):
                target.write_text(f'{{"v":{i}}}', encoding="utf-8")
                bm.snapshot([target], "same", reason="t")
                _t.sleep(1.05)
            kept = [d for d in (tmp / "b" / "backups").iterdir() if d.is_dir()]
            assert len(kept) == 2, f"kept={len(kept)}"
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    check("backup cleanup keeps newest per label", t_backup_cleanup)

    # ---- import smoke: decorators registered -----------------------------
    def t_smoke():
        from core.plugin.plugin_registry import _plugin_components
        comp = _plugin_components.get("kira_ops")
        assert comp is not None, "kira_ops components not registered"
        names = set(comp.tools.keys())
        expected = {"ops_status", "ops_read", "ops_config", "ops_action",
                    "ops_store", "ops_confirm", "ops_panic"}
        missing = expected - names
        assert not missing, f"missing tools: {missing}"
        assert len(comp.api_routes) >= 5, f"api_routes={len(comp.api_routes)}"
        assert len(comp.pages) >= 1, "no page registered"
    check("import smoke: 7 tools + api + page registered", t_smoke)

    # ---- caps registry ---------------------------------------------------
    def t_caps():
        from plugins.kira_ops.caps import REGISTRY, load_all
        load_all()
        expected = {"plugin", "skill", "provider", "mcp", "session", "persona",
                    "config", "log", "store", "agent", "control", "backup"}
        missing = expected - set(REGISTRY.keys())
        assert not missing, f"missing caps: {missing}"
    check("capability registry has all 12 domains", t_caps)

    # ---- new helpers: registry extension point, limit, log clipping -------
    def t_apply_limit():
        from plugins.kira_ops.caps import apply_limit
        items = list(range(50))
        assert apply_limit(items, {}) == (items, False)
        assert apply_limit(items, {"limit": 0}) == (items, False)
        assert apply_limit(items, {"limit": 3}, cap=10) == ([0, 1, 2], True)
        assert apply_limit(items, {"limit": 999}, cap=10) == (list(range(10)), True)
        assert apply_limit(items, {"limit": "junk"}, default=2)[0] == [0, 1]
    check("apply_limit honours limit / default / cap", t_apply_limit)

    def t_log_clip():
        from plugins.kira_ops.caps.log_cap import MAX_MESSAGE_CHARS, _clip
        assert len(_clip("a" * 5000)) <= MAX_MESSAGE_CHARS + 30
        assert "(+" in _clip("a" * 5000)
        assert _clip("short") == "short"
        assert _clip(None) == ""
    check("log messages are clipped before they reach the model", t_log_clip)

    def t_brief():
        from plugins.kira_ops.main import KiraOpsPlugin
        assert KiraOpsPlugin._trim([1, 2]) == [1, 2]
        trimmed = KiraOpsPlugin._trim(list(range(20)))
        assert trimmed["count"] == 20 and len(trimmed["items"]) == 5
        payload = KiraOpsPlugin._brief({"s": {"l": list(range(20))}})
        assert payload["detail"] == "brief" and "hint" in payload
        assert payload["s"]["l"]["truncated"] is True
    check("brief mode trims long lists instead of dropping sections", t_brief)

    def t_cap_name_re():
        from plugins.kira_ops.caps import CAP_NAME_RE
        assert CAP_NAME_RE.match("probe") and CAP_NAME_RE.match("a_b2")
        assert not CAP_NAME_RE.match("Probe")
        assert not CAP_NAME_RE.match("")
        assert not CAP_NAME_RE.match("a" * 40)
    check("capability names are validated", t_cap_name_re)

    print()
    print(f"passed: {len(PASSED)}  failed: {len(FAILED)}")
    if FAILED:
        for name, exc in FAILED:
            print(f"  FAILED: {name}: {exc}")
        sys.exit(1)
    print("ALL OK")


if __name__ == "__main__":
    main()
