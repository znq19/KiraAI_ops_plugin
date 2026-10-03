"""Permission engine: three knobs + blacklist, fail-closed by design.

Decision order (fixed, first hit wins):
1. master.enabled / panic_lock (writes downgrade to read-only)
2. deny list (blacklist - always checked first)
3. allow list (empty = allow everyone)
4. read-only sessions (writes rejected)
5. risk level (readonly rejects all writes)
6. high-risk action gate (dangerous level + required session list + confirm)
7. control actions (restart/shutdown: switch + full level + confirm)
"""

from __future__ import annotations

from dataclasses import dataclass

LEVELS = ("readonly", "standard", "dangerous", "full")
LEVEL_RANK = {"readonly": 0, "standard": 1, "dangerous": 2, "full": 3}


@dataclass
class Decision:
    allowed: bool
    reason: str = ""
    need_confirm: bool = False
    high_risk: bool = False

    def as_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "need_confirm": self.need_confirm,
            "high_risk": self.high_risk,
        }


class PermissionEngine:
    """Evaluates every kira_ops request against the settings schema."""

    def __init__(self, settings: dict = None):
        self.apply_settings(settings or {})

    def apply_settings(self, settings: dict) -> None:
        """Hot-reload hook: swap settings without recreating the engine."""
        s = settings or {}
        self.master = s.get("master") or {}
        self.access = s.get("access") or {}
        self.risk = s.get("risk") or {}
        self.control = s.get("control") or {}
        self.protected = s.get("protected") or {}

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _in(sid: str, entries) -> bool:
        target = str(sid or "").strip().lower()
        if not target:
            return False
        for item in entries or []:
            if str(item or "").strip().lower() == target:
                return True
        return False

    @property
    def level(self) -> str:
        lv = str(self.risk.get("level") or "standard").strip().lower()
        return lv if lv in LEVEL_RANK else "standard"

    @property
    def rank(self) -> int:
        return LEVEL_RANK.get(self.level, 1)

    def is_high_risk(self, action: str) -> bool:
        return str(action or "") in [str(x) for x in (self.risk.get("high_risk_actions") or [])]

    # ------------------------------------------------------------------
    # main checks
    # ------------------------------------------------------------------

    def evaluate(self, sid: str, action: str, kind: str = "write",
                 elevated: bool = False) -> Decision:
        """Check a normal action. kind: 'read' or 'write'.

        ``elevated=True`` is the DM-password approval path: the owner's password
        authorizes crossing the *mode* boundary (risk level / high-risk session
        list / confirm requirement) for this one call. It never bypasses the
        baseline checks - master switch, panic lock, deny/allow lists and the
        read-only list all still apply, and control actions keep their own
        independent switches (``evaluate_control`` has no elevation).
        """
        if not bool(self.master.get("enabled", True)):
            return Decision(False, "kira_ops is disabled in settings")
        if kind == "write" and bool(self.master.get("panic_lock", False)):
            return Decision(False, "panic lock is on; kira_ops is read-only")
        # blacklist wins over everything
        if self._in(sid, self.access.get("deny_sessions")):
            return Decision(False, "session is in the deny list")
        allow = self.access.get("allow_sessions") or []
        if allow and not self._in(sid, allow):
            return Decision(False, "session is not in the allow list")
        if kind == "write" and self._in(sid, self.access.get("readonly_sessions")):
            return Decision(False, "session is read-only")
        if kind == "write" and self.rank < 1:
            if elevated:
                pass  # the owner's password crosses the mode boundary
            else:
                # elevation-eligible only for high-risk actions: a frozen
                # (readonly) deployment should not spam the owner with
                # challenges for mundane writes
                return Decision(False, "risk level 'readonly' forbids writes",
                                high_risk=self.is_high_risk(action))

        high_risk = self.is_high_risk(action)
        if high_risk:
            if elevated:
                # approved by the owner via the confirm DM - the mode boundary
                # (level / session list / token) is waived for this call
                return Decision(True, "ok (dm-elevated)", high_risk=True)
            if self.rank < 2:
                # high_risk=True marks the refusal as elevation-eligible: the
                # plugin turns it into a DM authorization request when the
                # confirm channel is configured
                return Decision(False, "high-risk action requires level 'dangerous' or 'full'",
                                high_risk=True)
            hr_sessions = self.risk.get("high_risk_sessions") or []
            if not hr_sessions:
                return Decision(False, "high_risk_sessions is empty: no session may run high-risk actions",
                                high_risk=True)
            if not self._in(sid, hr_sessions):
                return Decision(False, "session is not in the high-risk session list",
                                high_risk=True)
            if bool(self.risk.get("require_confirm", True)):
                return Decision(True, "ok", need_confirm=True, high_risk=True)
        return Decision(True, "ok", high_risk=high_risk)

    def evaluate_control(self, sid: str, action: str, elevated: bool = False) -> Decision:
        """restart / shutdown: the independent switch is absolute (never
        elevatable); the full-level requirement is a mode boundary and can be
        crossed by the owner's DM password (``elevated=True``)."""
        base = self.evaluate(sid, action, kind="write", elevated=elevated)
        if not base.allowed:
            return base
        if action == "control.restart":
            if not bool(self.control.get("allow_restart", False)):
                return Decision(False, "restart is disabled in settings")
        elif action == "control.shutdown":
            if not bool(self.control.get("allow_shutdown", False)):
                return Decision(False, "shutdown is disabled in settings")
        else:
            return Decision(False, f"unknown control action: {action}")
        if elevated:
            return Decision(True, "ok (dm-elevated)", high_risk=True)
        if self.rank < 3:
            # marked high_risk so the plugin turns this into a DM
            # authorization request when the confirm channel is configured
            return Decision(False, "control actions require level 'full'", high_risk=True)
        return Decision(True, "ok", need_confirm=True, high_risk=True)

    # ------------------------------------------------------------------
    # protected-data delegates
    # ------------------------------------------------------------------

    def mask_for_read(self, data):
        from .redact import mask_data, with_floor
        return mask_data(data, with_floor(self.protected.get("read_mask")))

    def check_field_write(self, key: str, restrict: bool = False):
        from .redact import check_field_write
        return check_field_write(key, self.protected, restrict=restrict)

    def check_patch(self, patch: dict, restrict: bool = False):
        from .redact import check_patch
        return check_patch(patch, self.protected, restrict=restrict)

    def check_path(self, kind: str, path):
        from .paths import check_path
        return check_path(kind, path, self.protected)

    @property
    def persona_write_allowed(self) -> bool:
        return bool(self.protected.get("persona_write", False))

    def summary(self) -> dict:
        return {
            "level": self.level,
            "enabled": bool(self.master.get("enabled", True)),
            "panic_lock": bool(self.master.get("panic_lock", False)),
            "allow_sessions": len(self.access.get("allow_sessions") or []),
            "deny_sessions": len(self.access.get("deny_sessions") or []),
            "readonly_sessions": len(self.access.get("readonly_sessions") or []),
            "high_risk_sessions": len(self.risk.get("high_risk_sessions") or []),
            "high_risk_actions": len(self.risk.get("high_risk_actions") or []),
            "allow_restart": bool(self.control.get("allow_restart", False)),
            "allow_shutdown": bool(self.control.get("allow_shutdown", False)),
        }
