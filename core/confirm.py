"""One-time confirmation tokens for high-risk actions."""

from __future__ import annotations

import secrets
import time


class ConfirmPool:
    """Holds pending high-risk requests until the user confirms them."""

    def __init__(self, ttl: int = 300, max_pending: int = 32):
        self.ttl = max(30, int(ttl or 300))
        self.max_pending = max(4, int(max_pending or 32))
        self._pending = {}

    # 64 bits: rejections are audited, but a short token is a needless risk.
    TOKEN_BYTES = 8

    def _gc(self) -> None:
        now = time.time()
        for key in [k for k, v in self._pending.items() if v["expires"] < now]:
            self._pending.pop(key, None)

    def issue(self, request: dict, sid: str = "", uid: str = "") -> dict:
        """Store the pending request and return a token record."""
        self._gc()
        if len(self._pending) >= self.max_pending:
            oldest = min(self._pending.items(), key=lambda kv: kv[1]["created"])[0]
            self._pending.pop(oldest, None)
        token = secrets.token_hex(self.TOKEN_BYTES)
        record = {
            "token": token,
            # 4-hex public label for the challenge message; NOT a secret,
            # it only tells several pending requests apart.
            "code": secrets.token_hex(2),
            "created": time.time(),
            "expires": time.time() + self.ttl,
            "sid": str(sid or ""),
            "uid": str(uid or ""),
            "request": request,
        }
        self._pending[token] = record
        return record

    def latest(self) -> dict:
        """Newest non-expired pending record (or {}). Used by the DM approval
        channel, which approves the most recent request when the plain
        password arrives in the confirm session."""
        self._gc()
        if not self._pending:
            return {}
        return max(self._pending.values(), key=lambda r: r["created"])

    def peek(self, token: str) -> dict:
        self._gc()
        return self._pending.get(str(token or "").strip())

    def take(self, token: str, sid: str = "", uid: str = ""):
        """Consume a token; returns (True, request) or (False, reason)."""
        self._gc()
        record = self._pending.get(str(token or "").strip())
        if not record:
            return False, "token not found or expired"
        if sid and record.get("sid") and str(sid) != record["sid"]:
            return False, "token was issued to another session"
        if uid and record.get("uid") and str(uid) != record["uid"]:
            return False, "token was issued to another user"
        self._pending.pop(record["token"], None)
        return True, record["request"]

    def cancel(self, token: str) -> bool:
        return self._pending.pop(str(token or "").strip(), None) is not None

    def clear(self) -> None:
        self._pending.clear()

    @property
    def pending_count(self) -> int:
        self._gc()
        return len(self._pending)
