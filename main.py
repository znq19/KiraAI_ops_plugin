"""Kira Ops Console - runtime control console for KiraAI (v1.1.0).

A small, merged tool surface (ops_status / ops_read / ops_config /
ops_action / ops_store / ops_confirm / ops_panic) backed by:

  * a permission engine with three knobs (allow list empty = everyone,
    blacklist first, read-only list) plus a risk ladder and a mandatory
    high-risk session list;
  * automatic pre-write backups with retention and restore;
  * a JSONL audit trail that records rejections as well as writes;
  * a capability registry (caps/) that keeps the tool surface constant
    while domains grow - extensible at runtime through the
    ``kira_ops.register_capability`` custom event;
  * a WebUI panel where every setting is hot-editable and takes effect
    without a restart;
  * plugin install/update delegated to the framework installer
    (core.plugin.plugin_installer: size / entry-count / compression-ratio /
    zip-slip guards) plus the reload step multi-file plugins need, with a
    directory-level rollback when an update fails;
  * mutual exclusion with the standalone plugin store plugin.

All paths are resolved from the framework (get_data_path / get_root_path);
nothing personal is hard-coded, so the plugin is drop-in for any KiraAI
deployment.
"""

from __future__ import annotations

import asyncio
import copy
import functools
import json
import secrets
import time
from pathlib import Path

from core.plugin import BasePlugin, PageMenu, PluginPage, Priority, logger, on, register
from core.chat import Session
from core.chat.message_elements import Text
from core.chat.message_utils import KiraMessageBatchEvent, MessageChain
from core.provider import LLMRequest
from core.utils.path_utils import get_config_path, get_data_path

from . import caps as caps_pkg
from .caps import load_all
from .core.audit import AuditLog
from .core.backup import BackupManager
from .core.confirm import ConfirmPool
from .core.permission import PermissionEngine
from .core.redact import MASK
from .store import StoreClient, PLUGIN_ID_RE
from .store.installer import (
    install_plugin_from_direct_url,
    install_plugin_from_repo,
    install_skill_from_zip_bytes,
)
from core.prompt_manager import Prompt

# Default texts for the DM password-confirmation channel. The schema carries
# the same strings as field defaults; both are user-editable, these are only
# the fallback when a template is missing or fails to format.
DEFAULT_CONFIRM_REQUEST = (
    "[kira_ops 高危确认 #{code}]\n"
    "会话 {sid} 的用户 {uid} 请求执行高危动作：{action}\n"
    "参数：{params}\n"
    "如确认执行，请在 {ttl} 秒内于本会话直接回复【确认密码】"
    "（只发密码本身，不要带其他文字）；不回复或超时即自动放弃。"
)
DEFAULT_CONFIRM_PLACEHOLDER = "[收到 Kira 运行自控台（kira_ops）确认密码，当前并无待确认请求]"
DEFAULT_CONFIRM_APPROVED = "[kira_ops] 高危动作 {action} 已批准并执行：{result}"
DEFAULT_CONFIRM_NOTICE = "[kira_ops 系统通知] 你先前请求的动作 {action} 已获管理员批准并已执行；结果：{result}。本通知仅供你参考，是否向用户说明由你决定。"

# Plugins whose functionality kira_ops fully absorbs. When they are enabled,
# kira_ops disables them (never uninstalls) so nothing double-registers.
CONFLICT_PLUGINS = ("plugin_store_search",)

# target alias -> the id field each capability expects
TARGET_ALIASES = {
    "plugin": "plugin_id",
    "store": "plugin_id",
    "skill": "name",
    "provider": "provider_id",
    "mcp": "server_id",
    "session": "session_id",
    "persona": "persona_id",
    "backup": "backup_id",
}


class Payload(dict):
    """Tool-result dict that renders as compact JSON.

    The framework builds the tool message with ``ToolResult(str(result))``, so a
    plain dict would reach the model as a Python repr (``{'ok': True, ...}``,
    with spaces after ':' and ','). Rendering compact JSON instead is ~7%
    smaller and hands the model valid JSON (``true`` / ``null`` rather than
    ``True`` / ``None``). It is still a dict everywhere else, so internal
    callers and tests keep using ``result["ok"]``.
    """

    def __str__(self):
        try:
            return json.dumps(self, ensure_ascii=False, separators=(",", ":"), default=str)
        except Exception:
            return super().__str__()


TOOL_METHODS = ("ops_status", "ops_read", "ops_config", "ops_action",
                "ops_store", "ops_confirm", "ops_panic")


def wrap_tool_results(cls):
    """Wrap every @register.tool method so its result renders as compact JSON."""
    for name in TOOL_METHODS:
        original = cls.__dict__.get(name)
        if original is None or getattr(original, "_kira_ops_rendered", False):
            continue

        @functools.wraps(original)
        async def wrapper(self, *args, __original=original, **kwargs):
            return Payload(await __original(self, *args, **kwargs) or {})

        wrapper._kira_ops_rendered = True
        setattr(cls, name, wrapper)
    return cls


class KiraOpsPlugin(BasePlugin):
    plugin_id = "kira_ops"

    # 1.1.0's materialized high-risk default list. 1.2.0 moves plugin/store
    # install+update out of the defaults; configs saved by 1.1.0 carry this
    # exact list and are migrated (customized lists are left untouched).
    OLD_DEFAULT_HIGH_RISK = frozenset({
        "plugin.uninstall", "plugin.disable", "plugin.install", "plugin.update",
        "skill.remove", "skill.install",
        "provider.delete_model", "provider.update_model", "provider.sync", "mcp.delete",
        "session.delete", "session.memory_clear",
        "persona.create", "persona.update", "persona.delete", "persona.set_active",
        "store.install", "store.update", "backup.restore",
    })
    INSTALL_ACTIONS = frozenset({"plugin.install", "plugin.update",
                                 "store.install", "store.update"})

    def __init__(self, ctx, cfg: dict):
        super().__init__(ctx, cfg)
        self.cfg = cfg or {}
        self.refresh_settings(self.cfg)
        self.data_dir = self._resolve_data_dir()
        self.engine = PermissionEngine(self.cfg)
        self.backups = BackupManager(self.data_dir, self.backup_cfg)
        self.audit = AuditLog(self.data_dir, self.audit_cfg)
        self.confirm = ConfirmPool(ttl=int(self.risk.get("confirm_ttl") or 300))
        self.store = StoreClient(self.store_cfg)
        self.tasks: set = set()
        self.warnings: list = []
        self._lifecycle_cache = None
        self._lifecycle_warned = False
        self._warned_keys: set = set()

        load_all()
        self.caps = {name: cls(self) for name, cls in caps_pkg.REGISTRY.items()}

    # ------------------------------------------------------------------
    # config plumbing
    # ------------------------------------------------------------------

    def refresh_settings(self, cfg: dict) -> None:
        self.master = (cfg or {}).get("master") or {}
        self.access = (cfg or {}).get("access") or {}
        self.risk = (cfg or {}).get("risk") or {}
        self.control = (cfg or {}).get("control") or {}
        self.protected = (cfg or {}).get("protected") or {}
        self.backup_cfg = (cfg or {}).get("backup") or {}
        self.audit_cfg = (cfg or {}).get("audit") or {}
        self.store_cfg = (cfg or {}).get("store") or {}
        self.confirm_cfg = (cfg or {}).get("confirm") or {}

    def _resolve_data_dir(self) -> Path:
        try:
            d = self.ctx.get_plugin_data_dir()
        except Exception:
            d = None
        if not d:
            d = get_data_path() / "plugin_data" / self.plugin_id
        d = Path(d)
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def kira_config(self):
        return self.ctx.config

    @property
    def lifecycle(self):
        """Best-effort handle on the running KiraLifecycle instance.

        The framework hangs the WebUI app off the lifecycle
        (``lifecycle.webui_app``) but publishes no reverse link, so a plugin
        cannot obtain the lifecycle through the public context. Resolution
        order, most stable first:

          1. ``app.state.lifecycle`` - used if a future core sets it;
          2. the WebUI route handlers, which all keep the instance on self;
          3. the running ``init_and_run_system`` task frame;
          4. a gc sweep as the last resort.

        The result is cached; when every path fails a warning is logged once
        so the reason control.restart/shutdown refuses to run is visible.
        """
        cached = getattr(self, "_lifecycle_cache", None)
        if cached is not None:
            return cached
        found = None
        pm = getattr(self.ctx, "plugin_mgr", None)
        app = getattr(pm, "_web_app", None)
        if app is not None:
            found = getattr(app.state, "lifecycle", None)
            if found is None:
                found = self._scan_web_routes_for_lifecycle(app)
        if found is None:
            found = self._scan_tasks_for_lifecycle()
        if found is not None:
            self._lifecycle_cache = found
        elif not getattr(self, "_lifecycle_warned", False):
            self._lifecycle_warned = True
            logger.warning(
                "[kira_ops] KiraLifecycle handle not found; "
                "control.restart / control.shutdown will refuse to run")
        return found

    @staticmethod
    def _scan_web_routes_for_lifecycle(app):
        """Every WebUI route object holds the lifecycle instance on self."""
        try:
            for route in getattr(app, "routes", []) or []:
                holder = getattr(getattr(route, "endpoint", None), "__self__", None)
                candidate = getattr(holder, "lifecycle", None)
                if candidate is not None and candidate.__class__.__name__ == "KiraLifecycle":
                    return candidate
        except Exception:
            pass
        return None

    @staticmethod
    def _scan_tasks_for_lifecycle():
        try:
            for task in asyncio.all_tasks():
                coro = task.get_coro() if hasattr(task, "get_coro") else None
                frame = getattr(coro, "cr_frame", None)
                hops = 0
                while frame is not None and hops < 60:
                    obj = frame.f_locals.get("self")
                    if obj is not None and obj.__class__.__name__ == "KiraLifecycle":
                        return obj
                    frame = frame.f_back
                    hops += 1
        except Exception:
            pass
        try:
            import gc
            from core.lifecycle import KiraLifecycle
            for obj in gc.get_objects():
                if isinstance(obj, KiraLifecycle):
                    return obj
        except Exception:
            pass
        return None

    def log(self, message: str):
        logger.info(f"[kira_ops] {message}")

    def session_inventory(self) -> tuple:
        """List sessions, tolerating malformed keys in chat_memory.

        ``SessionManager.get_session_info()`` (no argument) splits every key on
        ':' and indexes part 2, so a single junk key - e.g. one written by an
        older build with a bogus session id - makes it raise IndexError for
        *everyone*, including the builtin session tools. Fall back to the raw
        store instead of failing the whole session domain, and report the bad
        keys so they can be cleaned up.
        """
        sm = getattr(self.ctx, "session_mgr", None)
        if sm is None:
            return [], []
        try:
            return list(sm.get_session_info() or []), []
        except Exception as exc:
            logger.warning(f"[kira_ops] session enumeration failed ({exc}); using raw keys")
        raw = getattr(sm, "chat_memory", None)
        if not isinstance(raw, dict):
            return [], []
        sessions, skipped = [], []
        for sid in list(raw.keys()):
            parts = str(sid).split(":", 2)
            if len(parts) < 3 or not parts[0] or not parts[1] or not parts[2]:
                skipped.append(str(sid))
                continue
            data = raw.get(sid) or {}
            sessions.append(Session(
                adapter_name=parts[0], session_type=parts[1], session_id=parts[2],
                session_title=data.get("title"), session_description=data.get("description"),
                timestamp=data.get("timestamp")))
        return sessions, skipped

    def mask(self, data):
        return self.engine.mask_for_read(data)

    @staticmethod
    def deep_merge(base: dict, patch: dict) -> dict:
        out = dict(base or {})
        for key, value in (patch or {}).items():
            if isinstance(value, dict) and isinstance(out.get(key), dict):
                out[key] = KiraOpsPlugin.deep_merge(out[key], value)
            else:
                out[key] = value
        return out

    def validate_agent_policy_patch(self, patch: dict) -> list:
        """Guard writes to the agent plugin's policy (paths + fields)."""
        from .core.redact import flatten

        violations = []
        if isinstance(patch, dict):
            for section in ("file_access", "exec_access"):
                sec = patch.get(section) or {}
                if not isinstance(sec, dict):
                    continue
                for key, _value in sec.items():
                    good, why = self.engine.check_field_write(key, restrict=False)
                    if not good:
                        violations.append(f"{section}.{key}: {why}")
                for path_key in ("extra_read_paths", "extra_write_paths"):
                    for p in sec.get(path_key) or []:
                        ok_read = self.engine.check_path("read", p)
                        ok_write = self.engine.check_path("write", p)
                        if not ok_read[0]:
                            violations.append(f"{section}.{path_key} {p}: {ok_read[1]}")
                        if not ok_write[0]:
                            violations.append(f"{section}.{path_key} {p}: {ok_write[1]}")
            for path, key, _v in flatten(patch):
                good, why = self.engine.check_field_write(key, restrict=False)
                if not good:
                    violations.append(f"{path}: {why}")
        return sorted(set(violations))

    # ------------------------------------------------------------------
    # store operations (shared with caps and tools)
    # ------------------------------------------------------------------

    async def install_from_store(self, plugin_id: str, force: bool = False) -> dict:
        """Install or update a plugin from the store.

        The download + archive handling is delegated to the framework
        installer, which enforces the 50 MiB / 10 000 entries / 100:1 ratio /
        512 KiB central-directory limits and the zip-slip guard. kira_ops adds
        the reload step (``prepare_plugin_reload``) the framework's own WebUI
        update flow also performs, plus a rollback to the previous build when
        the new one fails to load.
        """
        if not PLUGIN_ID_RE.match(plugin_id or ""):
            return {"ok": False, "error": f"illegal plugin id '{plugin_id}'"}
        pm = getattr(self.ctx, "plugin_mgr", None)
        if not pm:
            return {"ok": False, "error": "plugin manager is unavailable"}
        if plugin_id == self.plugin_id:
            return {"ok": False, "error": "kira_ops refuses to overwrite itself"}
        try:
            plugins = await self.store.fetch()
        except Exception as exc:
            return {"ok": False, "error": f"store fetch failed: {exc}"}
        target = self.store.find_plugin(plugins, plugin_id)
        if not target:
            return {"ok": False, "error": f"plugin '{plugin_id}' not found in the store"}

        repo, direct = StoreClient.entry_links(target)
        if not repo and not direct:
            return {"ok": False, "error": f"plugin '{plugin_id}' has no repo or download url"}

        update = bool(pm.has_plugin(plugin_id))
        if update and not force:
            return {"ok": False,
                    "error": f"plugin '{plugin_id}' already exists; pass force=true to overwrite"}

        snapshot = []
        for f in (get_config_path() / "plugins" / f"{plugin_id}.json",
                  get_config_path() / "plugins.json"):
            if Path(f).exists():
                snapshot.append(f)
        label = f"store_{'update' if update else 'install'}_{plugin_id}"
        backup = await asyncio.to_thread(self.backups.snapshot, snapshot, label, "store install")

        if repo:
            result = await install_plugin_from_repo(
                pm, repo, plugin_id,
                update=update, gh_proxy=self.store.gh_proxy_argument())
        else:
            result = await install_plugin_from_direct_url(
                pm, direct, plugin_id,
                update=update, validate_url=StoreClient.validate_direct_url,
                timeout=max(60.0, self.store.timeout))

        if result.get("ok"):
            if backup.get("id"):
                await asyncio.to_thread(self.backups.mark_applied, backup["id"])
                result["backup"] = backup
                result["hint"] = (f"{'updated' if update else 'installed'} from "
                                  f"{result.get('source') or repo or direct}"
                                  f" ｜ rollback point: backups/{backup['id']}")
        elif backup.get("id"):
            result["backup"] = backup
        if not result.get("ok"):
            logger.error(f"[kira_ops] store install of {plugin_id} failed: {result.get('error')}")
        return result

    async def store_search(self, keyword: str = "", author: str = "",
                           tag: str = "", limit: int = 0) -> dict:
        """Search the store listing (shared by ops_store and the store cap)."""
        try:
            plugins = await self.store.fetch()
        except Exception as exc:
            return {"ok": False, "error": f"store fetch failed: {exc}"}
        needle = (keyword or "").strip().lower()
        items = []
        for p in plugins or []:
            if not isinstance(p, dict):
                continue
            pid = str(p.get("plugin_id") or p.get("id") or "")
            name = str(p.get("display_name") or p.get("name") or pid)
            desc = str(p.get("description") or "")
            author_v = str(p.get("author") or "")
            tags = p.get("tags") or []
            hay = f"{pid} {name} {desc} {author_v} {' '.join(map(str, tags))}".lower()
            if needle and needle not in hay:
                continue
            if author and author.lower() not in author_v.lower():
                continue
            if tag and tag.lower() not in [str(t).lower() for t in tags]:
                continue
            items.append({
                "plugin_id": pid, "name": name, "version": str(p.get("version") or ""),
                "author": author_v, "description": desc[:180],
                "tags": list(tags)[:6],
                "downloads": p.get("downloads") or p.get("download_count") or 0,
                "likes": p.get("likes") or 0,
            })
        items.sort(key=lambda x: -(x.get("downloads") or 0))
        cap = max(1, min(int(limit or self.store.max_results or 10), 50))
        return {"ok": True, "count": len(items), "items": items[:cap],
                "truncated": len(items) > cap}

    async def install_skill_from_url(self, url: str, name: str, overwrite: bool = False) -> dict:
        """Install a skill from a GitHub repo or a direct zip URL.

        The archive is validated with the same limits the framework applies to
        plugins (size / entry count / compression ratio / zip-slip) before
        anything is written under data/skills.
        """
        from core.plugin.plugin_installer import MAX_PLUGIN_ARCHIVE_BYTES
        from core.utils.network import download_file

        target_url = str(url or "").strip()
        if "github.com" in target_url:
            try:
                from core.utils.github_api import parse_github_url
                owner, repo = parse_github_url(target_url)
            except Exception as exc:
                return {"ok": False, "error": f"cannot parse repo: {target_url} ({exc})"}
            target_url = f"https://github.com/{owner}/{repo}/archive/HEAD.zip"

        why = StoreClient.validate_direct_url(target_url)
        if why:
            return {"ok": False, "error": f"download blocked: {why}"}

        temp_dir = get_data_path() / "temp"
        temp_zip = temp_dir / f"kira_ops_skill_{int(time.time())}.zip"
        try:
            temp_dir.mkdir(parents=True, exist_ok=True)
            await download_file(target_url, str(temp_zip),
                                timeout=max(60.0, self.store.timeout),
                                max_bytes=MAX_PLUGIN_ARCHIVE_BYTES)
            payload = await asyncio.to_thread(temp_zip.read_bytes)
        except Exception as exc:
            return {"ok": False, "error": f"skill download failed: {exc!r}"}
        finally:
            temp_zip.unlink(missing_ok=True)

        try:
            target = await install_skill_from_zip_bytes(
                get_data_path() / "skills", payload, name, overwrite=overwrite)
        except Exception as exc:
            return {"ok": False, "error": f"skill install failed: {exc!r}"}

        sm = getattr(getattr(self.ctx, "message_processor", None), "skills_manager", None)
        if sm:
            sm.skills_info = sm.scan_skill_dir()
        return {"ok": True, "name": name, "path": str(target)}

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    async def initialize(self):
        if not bool(self.master.get("enabled", True)):
            # The framework registers plugin tools right after initialize()
            # whatever we return, so they stay in the prompt; the permission
            # engine is what keeps them refusing everything. Disable the plugin
            # in the plugin list to remove the tools from the prompt entirely.
            logger.warning(
                "[kira_ops] master.enabled=false: every read and write is refused "
                "(the framework still registers the 7 tools; use the plugin list "
                "to remove them from the prompt)")
            return
        if self.backups.cleanup_on_start:
            stats = await asyncio.to_thread(self.backups.cleanup)
            removed_audit = await asyncio.to_thread(self.audit.cleanup)
            self.log(f"startup cleanup: backups removed={stats.get('removed', 0)}, "
                     f"audit files removed={removed_audit}")
        self._migrate_high_risk_defaults()
        self._check_mandatory_settings()
        if bool(self.store_cfg.get("takeover_store", True)):
            await self._takeover_store()
        self.log("ready: tools + panel registered")

    def _migrate_high_risk_defaults(self) -> None:
        """Persist the 1.2.0 high-risk default change into old configs.

        1.1.0 materialized the old default list into every saved config, so a
        schema-default change alone never reaches existing users. Only lists
        that still equal the old default exactly are migrated; customized
        lists are respected as-is.
        """
        saved = self.risk.get("high_risk_actions") or []
        if {str(x) for x in saved} != self.OLD_DEFAULT_HIGH_RISK:
            return
        migrated = [str(x) for x in saved if str(x) not in self.INSTALL_ACTIONS]
        self.log("migrating high-risk defaults: plugin/store install+update are no "
                 "longer high-risk (usable at standard level)")
        try:
            cfg_file = get_config_path() / "plugins" / f"{self.plugin_id}.json"
            data = json.loads(cfg_file.read_text(encoding="utf-8")) if cfg_file.is_file() else {}
            data.setdefault("risk", {})["high_risk_actions"] = migrated
            cfg_file.parent.mkdir(parents=True, exist_ok=True)
            cfg_file.write_text(json.dumps(data, indent=4, ensure_ascii=False), encoding="utf-8")
        except Exception as exc:
            logger.warning(f"[kira_ops] high-risk default migration could not persist: {exc}")
        try:
            cached = (getattr(getattr(self.ctx, "plugin_mgr", None), "plugin_configs", None)
                      or {}).get(self.plugin_id)
            if isinstance(cached, dict):
                cached.setdefault("risk", {})["high_risk_actions"] = migrated
        except Exception:
            pass
        self.cfg.setdefault("risk", {})["high_risk_actions"] = migrated
        self.risk["high_risk_actions"] = migrated
        self.engine.apply_settings(self.cfg)

    async def terminate(self):
        for task in list(self.tasks):
            if not task.done():
                task.cancel()
        self.tasks.clear()
        self.confirm.clear()

    def _check_mandatory_settings(self):
        """Recompute the standing warnings; log each distinct one only once."""
        confirm_enabled = bool(self.confirm_cfg.get("enabled", False))
        checks = (
            (not (self.risk.get("high_risk_sessions") or []) and not self._confirm_ready(),
             "高危名单为空且密码确认未就绪：超档动作当前无人可执行（安全默认，非故障）"),
            (not self.protected.get("persona_write", False),
             "人设只读（protected.persona_write=false）"),
            (confirm_enabled and not self._confirm_sessions(),
             "密码确认已启用但没有可用的确认会话（每行一个 adapter:dm:QQ号 或 adapter:gm:群号），该通道当前不可用"),
            (confirm_enabled and self._confirm_sessions()
             and not str(self.confirm_cfg.get("password") or ""),
             "密码确认已启用但未设置确认密码，该通道当前不可用"),
        )
        self.warnings = [message for active, message in checks if active]
        for message in self.warnings:
            if message not in self._warned_keys:
                self._warned_keys.add(message)
                logger.warning(f"[kira_ops] {message}")
        for stale in [m for m in self._warned_keys if m not in self.warnings]:
            self._warned_keys.discard(stale)

    async def _takeover_store(self):
        conflicts = self.conflicts()
        if not conflicts:
            return
        pm = getattr(self.ctx, "plugin_mgr", None)
        for pid in conflicts:
            try:
                await pm.set_plugin_enabled(pid, False)
                self.log(f"takeover: disabled conflicting plugin '{pid}'")
            except Exception as exc:
                logger.error(f"[kira_ops] failed to disable conflicting plugin {pid}: {exc}")

    def conflicts(self) -> list:
        pm = getattr(self.ctx, "plugin_mgr", None)
        if not pm or not hasattr(pm, "has_plugin"):
            return []
        return [pid for pid in CONFLICT_PLUGINS
                if pm.has_plugin(pid) and pm.is_plugin_enabled(pid)]

    # ------------------------------------------------------------------
    # anomaly awareness (dynamic chat_env note, in-memory only, no I/O)
    # ------------------------------------------------------------------

    @on.llm_request(priority=Priority.LOW)
    async def note_anomaly(self, event, req: LLMRequest, tag_set, *_):
        """Append one short line to chat_env only in abnormal states.

        Keeps the system prefix untouched (chat_env is a dynamic prompt that
        the framework relocates to the latest user message), reads nothing
        from disk and stays silent during normal operation.
        """
        notes = []
        if bool(self.master.get("enabled", True)) and bool(self.master.get("panic_lock", False)):
            notes.append("控制台处于全锁（只读）状态：写操作已被紧急刹车")
        try:
            conflicts = self.conflicts()
        except Exception:
            conflicts = []
        if conflicts:
            notes.append("检测到商店插件冲突：" + ",".join(conflicts))
        if not notes:
            return
        text = "\n[kira_ops] " + "；".join(notes)
        for p in req.system_prompt:
            if getattr(p, "name", "") == "chat_env":
                p.content += text
                return
        req.system_prompt.append(Prompt(text.strip(), name="chat_env", source="kira_ops"))


    # ------------------------------------------------------------------
    # Password confirmation channel (multi-session)
    # ------------------------------------------------------------------
    # Flow: a high-risk / over-level action issues the usual pending record
    # AND, when confirm.enabled is on, mechanically posts a challenge to
    # every configured confirm session (dm or gm, several allowed at once).
    # Replying with the plain password in ANY of them within the TTL
    # approves the most recent pending request: that message is stopped in
    # the hook below, so it never reaches the buffer or the LLM. Anything
    # else in those sessions passes through untouched.

    def _spawn(self, coro) -> None:
        """Fire-and-forget a coroutine on the running loop, tracked for terminate()."""
        try:
            task = asyncio.create_task(coro)
        except RuntimeError:
            return
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    def _confirm_sessions(self) -> list:
        """Configured confirm sessions, filtered to valid `adapter:dm|gm:id` ids."""
        raw = self.confirm_cfg.get("sessions") or []
        if isinstance(raw, str):  # tolerate a single string
            raw = [raw]
        elif not isinstance(raw, (list, tuple, set)):
            # a dict/int/bool here (the model can write config) must not crash
            return []
        out = []
        for item in raw:
            s = str(item or "").strip()
            parts = s.split(":")
            if len(parts) == 3 and parts[1] in ("dm", "gm") and parts[0] and parts[2]:
                out.append(s)
        return out

    def _confirm_ready(self) -> bool:
        """True only when the password channel is fully configured."""
        if not bool(self.confirm_cfg.get("enabled", False)):
            return False
        if not self._confirm_sessions():
            return False
        return bool(str(self.confirm_cfg.get("password") or ""))

    def _confirm_status(self) -> dict:
        sessions = self._confirm_sessions()
        return {
            "enabled": bool(self.confirm_cfg.get("enabled", False)),
            "sessions": len(sessions),
            "password_set": bool(str(self.confirm_cfg.get("password") or "")),
            "ready": self._confirm_ready(),
        }

    def _render_tpl(self, key: str, default: str, **kw) -> str:
        tpl = str(self.confirm_cfg.get(key) or "").strip() or default
        try:
            return tpl.format(**kw)
        except Exception:
            try:
                return default.format(**kw)
            except Exception:
                return default

    async def _confirm_send(self, session: str, text: str) -> bool:
        """Mechanically post a message to a confirm session (no LLM involved)."""
        try:
            await self.ctx.send_message_chain(session, MessageChain([Text(text)]))
            return True
        except Exception as exc:
            logger.warning(f"[kira_ops] confirm message to {session} failed: {exc}")
            return False

    async def _send_confirm_challenge(self, record: dict) -> None:
        """Post the challenge to ALL confirm sessions at once; the first
        password reply in any of them wins."""
        request = record.get("request") or {}
        try:
            preview = json.dumps(self.mask(request.get("params") or {}), ensure_ascii=False)
        except Exception:
            preview = "{}"
        if len(preview) > 200:
            preview = preview[:200] + "…"
        action = str(request.get("key") or f"{request.get('cap')}.{request.get('action')}")
        text = self._render_tpl(
            "template_request", DEFAULT_CONFIRM_REQUEST,
            sid=record.get("sid") or "?", uid=record.get("uid") or "?",
            action=action, params=preview, ttl=self.confirm.ttl,
            code=record.get("code") or "?")
        sent = 0
        for session in self._confirm_sessions():
            if await self._confirm_send(session, text):
                sent += 1
        self.log(f"confirm challenge #{record.get('code')} sent to {sent} session(s)")

    @on.im_message(priority=90)
    async def confirm_password_hook(self, event):
        """Consume the plain password in ANY confirm session and approve.

        Priority 90 sits above the builtin chat plugin (HIGH=50) so the
        password is swallowed before any chat logic sees it; event.stop()
        keeps it out of the buffer and away from the LLM entirely. The
        approval executes in a spawned task: this hook runs under the
        message-processing semaphore, so a long action must not live here.
        """
        if not self._confirm_ready():
            return
        try:
            sid = str(getattr(getattr(event, "session", None), "sid", "") or "")
        except Exception:
            return
        if sid not in self._confirm_sessions():
            return
        password = str(self.confirm_cfg.get("password") or "")
        try:
            chain = getattr(getattr(event, "message", None), "chain", None)
            text = "".join(
                str(getattr(el, "text", "") or "")
                for el in (chain or []) if isinstance(el, Text)
            ).strip()
        except Exception:
            return
        if not text:
            return
        try:
            # bytes on both sides: compare_digest(str, str) raises TypeError
            # on non-ASCII text (e.g. normal Chinese chat in the session)
            matched = secrets.compare_digest(text.encode("utf-8"), password.encode("utf-8"))
        except Exception:
            matched = False
        if not matched:
            return  # not the password: leave the message to normal chat
        record = self.confirm.latest()
        if record:
            # a valid approval: swallow it (the owner asked for no LLM reply
            # here) and execute in the background
            event.stop()
            self.log(f"confirm: password accepted in {sid} for #{record.get('code')}")
            self._spawn(self._approve_via_password(record, approver_sid=sid))
            return
        # nothing to approve: hand the message back to the bot with the secret
        # replaced by a short placeholder, so chat flows naturally and the
        # password itself never reaches the model
        placeholder = (str(self.confirm_cfg.get("template_placeholder") or "").strip()
                       or DEFAULT_CONFIRM_PLACEHOLDER)
        if self._redact_password(event, placeholder):
            self.log(f"confirm: password received in {sid} with nothing pending (redacted)")
        else:
            # cannot rewrite the chain -> fail closed rather than leak it
            event.stop()
            self.log(f"confirm: password received in {sid}, nothing pending, chain not redactable")

    @staticmethod
    def _redact_password(event, placeholder: str) -> bool:
        """Rewrite the password text in place (first Text element becomes the
        placeholder, the rest are emptied). Returns False when there was
        nothing safe to rewrite."""
        try:
            chain = getattr(getattr(event, "message", None), "chain", None)
            if chain is None:
                return False
            replaced = False
            for element in chain:
                if not isinstance(element, Text):
                    continue
                element.text = placeholder if not replaced else ""
                replaced = True
            return replaced
        except Exception:
            return False

    async def _approve_via_password(self, record: dict, approver_sid: str) -> None:
        """Execute the approved request; ack every confirm session, notify the
        requesting session."""
        code = str(record.get("code") or "?")
        ok_tok, payload = self.confirm.take(str(record.get("token") or ""))
        if not ok_tok:
            await self._confirm_send(approver_sid, self._render_tpl(
                "template_approved", DEFAULT_CONFIRM_APPROVED,
                action="?", result=f"该请求已处理或已过期（{payload}）",
                sid="", uid="", code=code))
            return
        cap = self._cap(str(payload.get("cap") or ""))
        act = str(payload.get("action") or "")
        params = payload.get("params") or {}
        key = str(payload.get("key") or f"{payload.get('cap')}.{act}")
        rsid = str(record.get("sid") or "")
        ruid = str(record.get("uid") or "")

        result = None
        if cap is None:
            result = {"ok": False, "error": f"capability '{payload.get('cap')}' no longer exists"}
        else:
            # The owner's password elevates past the MODE boundary (level /
            # high-risk session list / the full-level requirement of control).
            # Baselines never move: master switch, panic lock, deny/allow/
            # readonly lists, and the independent control switches.
            if cap.name == "control":
                decision = self.engine.evaluate_control(rsid, key, elevated=True)
            else:
                decision = self.engine.evaluate(rsid, key, kind="write", elevated=True)
            if not decision.allowed:
                result = {"ok": False, "error": f"gate re-check refused: {decision.reason}"}
                self.audit.write(kind="deny", tool="ops_confirm", domain=cap.name,
                                 action=act, sid=rsid, uid=ruid, ok=False,
                                 error=str(decision.reason))
            else:
                pre = cap.preflight(act, params)
                if pre:
                    result = {"ok": False, "error": f"preflight refused: {pre}"}
                else:
                    result = await self._execute_write(cap, act, params, key, rsid, ruid,
                                                       tool="ops_confirm")
        ok_flag = bool((result or {}).get("ok"))
        summary = "成功" if ok_flag else f"失败：{str((result or {}).get('error') or '')[:160]}"
        self.audit.write(kind="write", tool="ops_confirm", domain=cap.name if cap else "?",
                         action=act,
                         target=str(params.get("target") or params.get("plugin_id") or ""),
                         sid=rsid, uid=ruid, ok=ok_flag,
                         note=f"approved via confirm session {approver_sid} #{code}",
                         error="" if ok_flag else str((result or {}).get("error") or ""))
        remaining = self.confirm.pending_count
        ack = self._render_tpl("template_approved", DEFAULT_CONFIRM_APPROVED,
                               action=key, result=summary, sid=rsid, uid=ruid, code=code)
        if remaining:
            # one password message approves exactly one request - say so, or the
            # owner may think everything pending was handled
            ack += f"\n（仍有 {remaining} 条待确认请求）"
        # every confirm session gets the outcome, so the others know it is handled
        for session in self._confirm_sessions():
            await self._confirm_send(session, ack)
        await self._notify_session(rsid, self._render_tpl(
            "template_notice", DEFAULT_CONFIRM_NOTICE, action=key, result=summary, code=code))

    async def _notify_session(self, sid: str, text: str) -> None:
        """Inject a system notice into the requesting session so its LLM can
        relay the outcome. publish_notice hands the message to the normal
        pipeline (the chat plugin buffers it), so flush shortly afterwards
        to actually trigger a turn."""
        if not sid:
            return
        try:
            await self.ctx.publish_notice(sid, MessageChain([Text(text)]), is_mentioned=True)
            await asyncio.sleep(0.8)
            await self.ctx.flush_session_messages(sid)
        except Exception as exc:
            logger.warning(f"[kira_ops] confirm notice to {sid} failed: {exc}")

    # ------------------------------------------------------------------
    # request context helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _text(value, default: str = "") -> str:
        """Coerce a tool argument to a clean string.

        Arguments arrive straight from the model, so ``include``/``action``/
        ``keyword`` can be an int, a bool or a dict. Fuzzing 2k hostile calls
        produced 177 AttributeErrors from ``.split()``/``.strip()`` on such
        values - every string parameter goes through here now.
        """
        if value is None:
            return default
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (int, float)):
            return str(value)
        return default if default else str(value).strip()

    @staticmethod
    def _sid(event) -> str:
        sid = getattr(event, "sid", None)
        if sid:
            return str(sid)
        session = getattr(event, "session", None)
        if session is not None:
            return str(getattr(session, "sid", "") or session)
        return ""

    @staticmethod
    def _uid(event) -> str:
        try:
            messages = getattr(event, "messages", None) or []
            if messages:
                sender = getattr(messages[-1], "sender", None)
                if sender is not None:
                    return str(getattr(sender, "user_id", "") or "")
            session = getattr(event, "session", None)
            if session is not None and getattr(session, "session_type", "") == "dm":
                return str(getattr(session, "session_id", "") or "")
        except Exception:
            pass
        return ""

    @staticmethod
    def _normalize_params(cap_name: str, params, target: str = "") -> dict:
        # tool arguments come from the model: a str/list here must not raise
        params = dict(params) if isinstance(params, dict) else {}
        if target:
            params.setdefault(TARGET_ALIASES.get(cap_name, "target"), target)
            params.setdefault("target", target)
        return params

    def _confirm_needed(self, decision, payload: dict, sid: str, uid: str) -> dict:
        """Stash a high-risk request and hand back a one-shot confirm token."""
        record = self.confirm.issue(payload, sid=sid, uid=uid)
        self.audit.write(kind="write", tool="ops_confirm", domain=payload.get("cap", ""),
                         action=payload.get("action", ""), sid=sid, uid=uid,
                         ok=True, note="confirm token issued (pending user approval)")
        password_on = self._confirm_ready()
        if password_on:
            # fire-and-forget: never block the tool result on IM delivery
            self._spawn(self._send_confirm_challenge(record))
        next_step = (f"call ops_confirm(token=...) within {self.confirm.ttl}s")
        if password_on:
            next_step += "; the owner may also approve with the password in a confirm session"
        return {
            "ok": False,
            "status": "pending_confirmation",
            "need_confirm": True,
            "token": record["token"],
            "expires_in": self.confirm.ttl,
            "password_approval": password_on,
            "preview": {
                "cap": payload.get("cap"),
                "action": payload.get("action"),
                "params": self.mask(payload.get("params") or {}),
            },
            "next_step": next_step,
        }

    def _confirm_elevation_needed(self, payload: dict, sid: str, uid: str) -> dict:
        """A high-risk request that the current mode refuses: instead of a hard
        denial, ask the owner for a one-off authorization in the confirm DM.
        No token is handed out - only the owner's password can release this.
        """
        record = self.confirm.issue(payload, sid=sid, uid=uid)
        self.audit.write(kind="write", tool="ops_confirm", domain=payload.get("cap", ""),
                         action=payload.get("action", ""), sid=sid, uid=uid,
                         ok=True, note="dm elevation requested (pending owner password)")
        self._spawn(self._send_confirm_challenge(record))
        # Tool result only: the requesting session sees nothing, this is data
        # for the model to reason about. The bot may tell the user whatever it
        # thinks is appropriate (or stay silent) - the plugin never speaks.
        return {
            "ok": False,
            "status": "pending_owner_approval",
            "reason": "action exceeds the current capability level",
            "action": f"{payload.get('cap')}.{payload.get('action')}",
            "request_code": record.get("code"),
            "expires_in": self.confirm.ttl,
            "approval": "requested from the owner's confirm sessions",
            "on_approval": "the action is executed automatically; this session then receives a notice",
            "preview": {
                "cap": payload.get("cap"),
                "action": payload.get("action"),
                "params": self.mask(payload.get("params") or {}),
            },
        }

    def _cap(self, domain: str):
        """Return the live capability *instance* for a domain (not the class)."""
        name = str(domain or "")
        cap = self.caps.get(name)
        if cap is not None:
            return cap
        # defensive: registry may have gained a domain after __init__
        load_all()
        cls = caps_pkg.REGISTRY.get(name)
        if cls is None:
            return None
        cap = cls(self)
        self.caps[name] = cap
        return cap

    # ------------------------------------------------------------------
    # write pipeline
    # ------------------------------------------------------------------

    async def _execute_write(self, cap, action: str, params: dict, key: str,
                             sid: str, uid: str, tool: str = "ops_action") -> dict:
        backup = {}
        try:
            files = [f for f in (cap.backup_files(action, params) or []) if Path(f).is_file()]
            if files:
                backup = await asyncio.to_thread(
                    self.backups.snapshot, files, cap.backup_label(action, params), key)
        except Exception as exc:
            logger.warning(f"[kira_ops] snapshot before {key} failed: {exc}")

        started = time.time()
        try:
            result = cap.handle_write(action, params)
            if hasattr(result, "__await__"):
                result = await result
        except Exception as exc:
            logger.exception(f"[kira_ops] {key} failed: {exc}")
            result = {"ok": False, "error": f"{key} failed: {exc}"}
        ms = int((time.time() - started) * 1000)
        if not isinstance(result, dict):
            result = {"ok": True, "result": result}

        if result.get("ok") and backup.get("id"):
            try:
                await asyncio.to_thread(self.backups.mark_applied, backup["id"])
            except Exception:
                pass
        if result.get("ok") and backup.get("id"):
            result["backup"] = backup
            hint = result.get("hint") or ""
            rollback = f"回滚点: backups/{backup['id']}"
            result["hint"] = (hint + " ｜ " + rollback).strip(" ｜")

        self.audit.write(kind="write", tool=tool, domain=cap.name, action=action,
                         target=str(params.get("target") or params.get("plugin_id")
                                    or params.get("session_id") or params.get("name") or ""),
                         sid=sid, uid=uid, args=self.mask(params), ok=bool(result.get("ok")),
                         error=str(result.get("error") or ""), ms=ms,
                         backup=str(backup.get("id") or ""))
        return result

    def _deny(self, tool, cap_name, action, sid, uid, reason, params=None):
        """Record a refused request (kind='deny' is never filtered) and answer."""
        self.audit.write(kind="deny", tool=tool, domain=cap_name, action=action,
                         sid=sid, uid=uid, ok=False, error=reason,
                         args=self.mask(params) if params else None)
        return {"ok": False, "error": f"permission denied: {reason}"}

    # ==================================================================
    # TOOLS
    # ==================================================================

    @register.tool(
        "ops_status",
        "查看 KiraAI 运行总览：插件/技能/Provider/MCP/会话计数、权限档位、告警、备份与审计概况。"
        "系统体检、自检、看整体状况时调用。",
        {
            "type": "object",
            "properties": {
                "include": {"type": "string",
                            "description": "逗号分隔的段落：resources,plugins,skills,providers,mcp,sessions,store,audit,permission（默认 resources,plugins,providers,permission）"},
                "detail": {"type": "string", "description": "brief（默认，长列表只给前几条+总数）或 full（完整明细）"}
            },
            "required": []
        }
    )
    async def ops_status(self, event: KiraMessageBatchEvent, include: str = "", detail: str = "brief"):
        """Runtime overview.

        detail=brief (default) keeps list-valued fields short; detail=full
        returns the complete content of every requested section.
        """
        if not bool(self.master.get("enabled", True)):
            return {"ok": False, "error": "kira_ops is disabled in settings"}
        full = self._text(detail, "brief").lower() == "full"
        pm = getattr(self.ctx, "plugin_mgr", None)
        include = self._text(include, "resources,plugins,providers,permission")
        parts = [p.strip() for p in include.split(",") if p.strip()]
        out = {"ok": True, "detail": "full" if full else "brief"}
        if "resources" in parts:
            try:
                import psutil
                out["resources"] = {
                    "cpu_percent": psutil.cpu_percent(interval=None),
                    "memory_percent": psutil.virtual_memory().percent,
                    "disk_percent": psutil.disk_usage(str(get_data_path())).percent,
                }
            except Exception:
                out["resources"] = {"note": "psutil unavailable"}
        if "plugins" in parts and pm:
            plugins = pm.list_plugins()
            out["plugins"] = {
                "total": len(plugins),
                "enabled": sum(1 for p in plugins if pm.is_plugin_enabled(p.plugin_id)),
                "errors": [p.plugin_id for p in plugins if (p.status or "") in ("error",)],
                "conflicts": self.conflicts(),
            }
        if "skills" in parts:
            sm = getattr(getattr(self.ctx, "message_processor", None), "skills_manager", None)
            out["skills"] = {"total": len(sm.skills_info) if sm else 0,
                             "enabled": sum(1 for s in (sm.skills_info if sm else []) if s.enabled)}
        if "providers" in parts:
            pmgr = getattr(self.ctx, "provider_mgr", None)
            provs = pmgr.get_all_providers() if pmgr else {}
            out["providers"] = {"total": len(provs or {})}
        if "mcp" in parts:
            mcp = getattr(getattr(self.ctx, "message_processor", None), "mcp_manager", None)
            out["mcp"] = {"total": len(mcp.servers) if mcp else 0}
        if "sessions" in parts:
            sessions, skipped = self.session_inventory()
            out["sessions"] = {"total": len(sessions)}
            if skipped:
                out["sessions"]["malformed_keys"] = len(skipped)
        if "store" in parts:
            out["store"] = {"plugin": self.plugin_id, "takeover": bool(self.store_cfg.get("takeover_store", True)),
                            "cached": self.store._cache is not None}
        if "audit" in parts:
            out["audit"] = {"files": len(self.audit.history(30)),
                            "pending_confirms": self.confirm.pending_count,
                            "backups": self.backups.count()}
        if "permission" in parts:
            out["permission"] = self.engine.summary()
        if self.warnings:
            out["warnings"] = self.warnings
        agent_ready = bool(pm and pm.has_plugin("agent") and pm.is_plugin_enabled("agent"))
        if not agent_ready:
            out.setdefault("notes", []).append(
                "agent 插件未安装或未启用：文件/命令能力当前不可用（本插件只托管其策略）")
        if not full:
            out = self._brief(out)
        return out

    @staticmethod
    def _trim(value, limit: int = 5):
        """Shrink one list-valued field instead of dropping the whole section."""
        if isinstance(value, list) and len(value) > limit:
            return {"count": len(value), "items": value[:limit], "truncated": True}
        return value

    @classmethod
    def _brief(cls, payload: dict, limit: int = 5) -> dict:
        """Shrink long lists; only advertise detail=full when something was cut."""
        out = {}
        trimmed = False
        for key, value in payload.items():
            if isinstance(value, dict):
                section = {}
                for sub_key, sub_value in value.items():
                    shrunk = cls._trim(sub_value, limit)
                    trimmed = trimmed or shrunk is not sub_value
                    section[sub_key] = shrunk
                out[key] = section
            else:
                shrunk = cls._trim(value, limit)
                trimmed = trimmed or shrunk is not value
                out[key] = shrunk
        out["detail"] = "brief"
        if trimmed:
            out["hint"] = "长列表已裁剪，需要完整明细时传 detail=full"
        return out

    @register.tool(
        "ops_read",
        "只读查看 Kira 各域明细。domain 可选：plugin, skill, provider, mcp, session, persona, config, log, store, backup, agent, control。"
        "action 常用：list / info / config_get / models / memory_count / content / tail / search / history。",
        {
            "type": "object",
            "properties": {
                "domain": {"type": "string", "description": "域，见工具描述"},
                "action": {"type": "string", "description": "动作，缺省为 list"},
                "target": {"type": "string", "description": "目标对象 ID（插件ID/技能名/会话ID/ProviderID/服务器ID/人设ID/备份ID）"},
                "keyword": {"type": "string", "description": "关键词过滤/搜索"},
                "limit": {"type": "integer", "description": "条数上限（list 类默认 50，上限 200）"},
                "args": {"type": "object", "description": "附加参数（JSON 对象）"}
            },
            "required": ["domain"]
        }
    )
    async def ops_read(self, event: KiraMessageBatchEvent, domain: str, action: str = "list",
                       target: str = "", keyword: str = "", limit: int = 0, args: dict = None):
        domain = self._text(domain)
        target = self._text(target)
        keyword = self._text(keyword)
        cap = self._cap(domain)
        if cap is None:
            return {"ok": False, "error": f"unknown domain '{domain}'",
                    "domains": sorted(caps_pkg.REGISTRY.keys())}
        act = self._text(action, "list")
        if act not in cap.ACTIONS:
            return {"ok": False, "error": f"unknown action '{act}' for domain '{domain}'",
                    "actions": sorted(cap.ACTIONS.keys())}
        kind = cap.ACTIONS[act][0]
        if kind != "read":
            # allow read-kind fallback for write entries used as listing
            return {"ok": False, "error": f"'{act}' is a write action; use ops_action/ops_config"}
        sid = self._sid(event)
        decision = self.engine.evaluate(sid, f"{domain}.{act}", kind="read")
        if not decision.allowed:
            return self._deny("ops_read", domain, act, sid, self._uid(event), decision.reason)
        params = dict(args) if isinstance(args, dict) else {}
        if target:
            params.setdefault(TARGET_ALIASES.get(domain, "target"), target)
        if keyword:
            params.setdefault("keyword", keyword)
        if limit:
            params.setdefault("limit", limit)
        try:
            result = cap.handle_read(act, params)
            if hasattr(result, "__await__"):
                result = await result
        except Exception as exc:
            logger.exception(f"[kira_ops] read {domain}.{act} failed: {exc}")
            return {"ok": False, "error": f"{domain}.{act} failed: {exc}"}
        if not isinstance(result, dict):
            result = {"ok": True, "result": result}
        self.audit.write(kind="read", tool="ops_read", domain=domain, action=act,
                         sid=sid, uid=self._uid(event), ok=bool(result.get("ok")),
                         error=str(result.get("error") or ""))
        return result

    @register.tool(
        "ops_config",
        "写配置。domain=config 写 KiraAI 系统配置（path+patch）；domain=plugin 写插件配置（target=插件ID）；"
        "domain=agent 写内置 agent 插件的文件/命令策略。敏感字段与保护路径一律拒绝。",
        {
            "type": "object",
            "properties": {
                "domain": {"type": "string", "description": "config / plugin / agent"},
                "target": {"type": "string", "description": "插件ID（domain=plugin 时）"},
                "path": {"type": "string", "description": "配置路径（domain=config 时，如 bot_config.bot）"},
                "patch": {"type": "object", "description": "要写入的 JSON 对象"},
                "confirm": {"type": "string", "description": "高危动作的确认令牌（如返回需要）"}
            },
            "required": ["domain", "patch"]
        }
    )
    async def ops_config(self, event: KiraMessageBatchEvent, domain: str, patch: dict,
                         target: str = "", path: str = "", confirm: str = ""):
        domain = self._text(domain)
        target = self._text(target)
        path = self._text(path)
        confirm = self._text(confirm)
        sid = self._sid(event)
        uid = self._uid(event)
        if domain == "config":
            cap = self._cap("config")
            params = {"path": path, "patch": patch}
            key = "config.set"
            act = "set"
        elif domain == "plugin":
            cap = self._cap("plugin")
            params = {"plugin_id": target, "patch": patch}
            key = "plugin.config_set"
            act = "config_set"
        elif domain == "agent":
            cap = self._cap("agent")
            params = {"patch": patch}
            key = "agent.set"
            act = "set"
        else:
            return {"ok": False, "error": "domain must be config / plugin / agent"}

        decision = self.engine.evaluate(sid, key, kind="write")
        if not decision.allowed:
            if decision.high_risk and self._confirm_ready():
                return self._confirm_elevation_needed(
                    {"cap": cap.name, "action": act, "params": params, "key": key}, sid, uid)
            return self._deny("ops_config", cap.name, act, sid, uid, decision.reason, params)
        pre = cap.preflight(act, params)
        if pre:
            return self._deny("ops_config", cap.name, act, sid, uid, pre)
        if decision.need_confirm:
            if not confirm:
                return self._confirm_needed(
                    decision, {"cap": cap.name, "action": act, "params": params, "key": key},
                    sid, uid)
            ok_tok, payload = self.confirm.take(confirm, sid, uid)
            if not ok_tok:
                return self._deny("ops_config", cap.name, act, sid, uid,
                                  f"confirm token rejected: {payload}")
            params = payload.get("params") or params
        return await self._execute_write(cap, act, params, key, sid, uid, tool="ops_config")

    @register.tool(
        "ops_action",
        "执行 Kira 各域动作（生命周期/写操作）。domain+action："
        "plugin(enable/disable/reload/install/update/uninstall)、"
        "skill(refresh/enable/disable/set_scope/install/remove)、"
        "provider(add_model/update_model/delete_model/sync/set_provider)、"
        "mcp(add/update/enable/disable/tool_toggle/scope/delete)、"
        "session(title/caps/memory_clear/delete)、persona(set_active/create/update/delete)、"
        "backup(restore)、control(restart/shutdown)。"
        "高危动作先返回确认令牌，需再调 ops_confirm(token=...)。",
        {
            "type": "object",
            "properties": {
                "domain": {"type": "string", "description": "域"},
                "action": {"type": "string", "description": "动作"},
                "target": {"type": "string", "description": "目标 ID"},
                "args": {"type": "object", "description": "附加参数（JSON）"},
                "confirm": {"type": "string", "description": "确认令牌（如返回需要）"}
            },
            "required": ["domain", "action"]
        }
    )
    async def ops_action(self, event: KiraMessageBatchEvent, domain: str, action: str,
                         target: str = "", args: dict = None, confirm: str = ""):
        domain = self._text(domain)
        target = self._text(target)
        confirm = self._text(confirm)
        cap = self._cap(domain)
        if cap is None:
            return {"ok": False, "error": f"unknown domain '{domain}'",
                    "domains": sorted(caps_pkg.REGISTRY.keys())}
        act = self._text(action)
        if act not in cap.ACTIONS:
            return {"ok": False, "error": f"unknown action '{act}' for domain '{domain}'",
                    "actions": sorted(cap.ACTIONS.keys())}
        kind, dangerous, _desc = cap.ACTIONS[act]
        if kind != "write":
            return {"ok": False, "error": f"'{act}' is a read action; use ops_read"}

        sid = self._sid(event)
        uid = self._uid(event)
        key = (cap.classify(act, args or {}) or f"{domain}.{act}")

        if domain == "control":
            decision = self.engine.evaluate_control(sid, key)
        else:
            decision = self.engine.evaluate(sid, key, kind="write")

        params = self._normalize_params(domain, args or {}, target)
        if not decision.allowed:
            if decision.high_risk and self._confirm_ready():
                return self._confirm_elevation_needed(
                    {"cap": domain, "action": act, "params": params, "key": key}, sid, uid)
            return self._deny("ops_action", domain, act, sid, uid, decision.reason, params)

        pre = cap.preflight(act, params)
        if pre:
            return self._deny("ops_action", domain, act, sid, uid, pre)

        if decision.need_confirm:
            if not confirm:
                return self._confirm_needed(decision, {"cap": domain, "action": act,
                                                       "params": params, "key": key}, sid, uid)
            ok_tok, payload = self.confirm.take(confirm, sid, uid)
            if not ok_tok:
                return self._deny("ops_action", domain, act, sid, uid,
                                  f"confirm token rejected: {payload}")
            return await self._execute_write(cap, act, payload.get("params") or params, key,
                                             sid, uid, tool="ops_action")
        return await self._execute_write(cap, act, params, key, sid, uid, tool="ops_action")

    @register.tool(
        "ops_store",
        "插件商店：action=search 搜索插件（可带 keyword/author/tag），action=install/update 安装或更新，"
        "action=sources 查看商店源与缓存状态。安装/更新在 standard 档位可直接执行；"
        "若返回 status=pending_owner_approval 则说明需管理员授权，此时不要重试。",
        {
            "type": "object",
            "properties": {
                "action": {"type": "string", "description": "search / install / update / sources"},
                "keyword": {"type": "string", "description": "搜索关键词"},
                "author": {"type": "string", "description": "按作者过滤"},
                "tag": {"type": "string", "description": "按标签过滤"},
                "plugin_id": {"type": "string", "description": "安装/更新时的插件ID"},
                "force": {"type": "boolean", "description": "覆盖安装"},
                "confirm": {"type": "string", "description": "确认令牌（如需）"}
            },
            "required": ["action"]
        }
    )
    async def ops_store(self, event: KiraMessageBatchEvent, action: str, keyword: str = "",
                        author: str = "", tag: str = "", plugin_id: str = "",
                        force: bool = False, confirm: str = ""):
        act = self._text(action)
        keyword = self._text(keyword)
        author = self._text(author)
        tag = self._text(tag)
        plugin_id = self._text(plugin_id)
        confirm = self._text(confirm)
        sid = self._sid(event)
        uid = self._uid(event)
        if act == "sources":
            return {"ok": True, **self.store.status()}
        if act == "search":
            decision = self.engine.evaluate(sid, "store.search", kind="read")
            if not decision.allowed:
                return self._deny("ops_store", "store", "search", sid, uid, decision.reason)
            return await self.store_search(keyword=keyword, author=author, tag=tag)
        if act in ("install", "update"):
            key = f"store.{act}"
            decision = self.engine.evaluate(sid, key, kind="write")
            if not decision.allowed:
                if decision.high_risk and self._confirm_ready():
                    return self._confirm_elevation_needed(
                        {"cap": "store", "action": act,
                         "params": {"plugin_id": plugin_id, "force": force or act == "update"},
                         "key": key}, sid, uid)
                return self._deny("ops_store", "store", act, sid, uid, decision.reason,
                                  {"plugin_id": plugin_id, "force": force})
            if decision.need_confirm and not confirm:
                payload = {"cap": "store", "action": act,
                           "params": {"plugin_id": plugin_id, "force": force or act == "update"},
                           "key": key}
                return self._confirm_needed(decision, payload, sid, uid)
            if confirm:
                ok_tok, payload = self.confirm.take(confirm, sid, uid)
                if not ok_tok:
                    return self._deny("ops_store", "store", act, sid, uid,
                                      f"confirm token rejected: {payload}")
                plugin_id = (payload.get("params") or {}).get("plugin_id", plugin_id)
                force = (payload.get("params") or {}).get("force", force)
            result = await self.install_from_store(plugin_id, force=force or act == "update")
            self.audit.write(kind="write", tool="ops_store", domain="store", action=act,
                             target=plugin_id, sid=sid, uid=uid, ok=bool(result.get("ok")),
                             error=str(result.get("error") or ""))
            return result
        return {"ok": False, "error": "action must be search / install / update / sources"}

    @register.tool(
        "ops_confirm",
        "用高危动作返回的一次性令牌确认执行（status=pending_confirmation 时使用）。"
        "令牌有效期默认 300 秒，同会话同用户才能使用，用后即焚。"
        "注意：管理员也可以改在确认会话里回复密码批准，届时令牌会作废并返回相应错误，属正常情况。",
        {"type": "object",
         "properties": {"token": {"type": "string", "description": "ops_action/ops_config/ops_store 返回的令牌"}},
         "required": ["token"]}
    )
    async def ops_confirm(self, event: KiraMessageBatchEvent, token: str):
        token = self._text(token)
        sid = self._sid(event)
        uid = self._uid(event)
        ok_tok, payload = self.confirm.take(token, sid, uid)
        if not ok_tok:
            self.audit.write(kind="deny", tool="ops_confirm", domain="master",
                             action="confirm", sid=sid, uid=uid, ok=False,
                             error=str(payload))
            return {"ok": False, "error": f"confirm failed: {payload}"}
        cap = self._cap(payload.get("cap"))
        if cap is None:
            return self._deny("ops_confirm", str(payload.get("cap") or "unknown"),
                              str(payload.get("action") or "unknown"), sid, uid,
                              "capability no longer exists")
        act = payload.get("action")
        params = payload.get("params") or {}
        key = payload.get("key") or f"{cap.name}.{act}"
        if cap.name == "control":
            decision = self.engine.evaluate_control(sid, key)
        else:
            decision = self.engine.evaluate(sid, key, kind="write")
        if not decision.allowed:
            return self._deny("ops_confirm", cap.name, act, sid, uid, decision.reason, params)
        pre = cap.preflight(act, params)
        if pre:
            return self._deny("ops_confirm", cap.name, act, sid, uid, pre)
        result = await self._execute_write(cap, act, params, key, sid, uid, tool="ops_confirm")
        return result

    @register.tool(
        "ops_panic",
        "紧急刹车：lock=true 立即把所有写操作降级为只读（不影响读）；lock=false 解除。",
        {"type": "object",
         "properties": {"lock": {"type": "boolean", "description": "true=全锁只读，false=解锁"}},
         "required": ["lock"]}
    )
    async def ops_panic(self, event: KiraMessageBatchEvent, lock: bool):
        sid = self._sid(event)
        uid = self._uid(event)
        # dedicated gate: unlocking must work even while the lock is engaged
        if not bool(self.master.get("enabled", True)):
            return {"ok": False, "error": "kira_ops is disabled in settings"}
        if self.engine._in(sid, self.engine.access.get("deny_sessions")):
            return self._deny("ops_panic", "master", "panic", sid, uid, "session is in the deny list")
        allow = self.engine.access.get("allow_sessions") or []
        if allow and not self.engine._in(sid, allow):
            return self._deny("ops_panic", "master", "panic", sid, uid, "session is not in the allow list")
        pm = getattr(self.ctx, "plugin_mgr", None)
        if not pm:
            return {"ok": False, "error": "plugin manager is unavailable"}
        cfg = dict(self.cfg or {})
        master = dict(cfg.get("master") or {})
        master["panic_lock"] = bool(lock)
        cfg["master"] = master
        await pm.update_plugin_config(self.plugin_id, cfg)
        self.audit.write(kind="write", tool="ops_panic", domain="master", action="panic",
                         sid=sid, uid=uid, ok=True, note=f"lock={bool(lock)}")
        return {"ok": True, "panic_lock": bool(lock),
                "hint": "已全锁（只读）" if lock else "已解除全锁"}

    # ==================================================================
    # WEBUI PANEL
    # ==================================================================

    @register.page(
        "/index",
        # icon: an SVG shipped with the plugin (v2.34.5+) - a control panel with a
        # gauge, deliberately unlike the Element Plus "Monitor" glyph other
        # plugins already use in the sidebar.
        menu=PageMenu(label={"zh": "运行自控台", "en": "Ops Console"},
                      icon="assets/icon.svg", order=86),
    )
    def page(self):
        return PluginPage.from_folder("./web")

    def _panel_payload(self):
        pm = getattr(self.ctx, "plugin_mgr", None)
        plugins = pm.list_plugins() if pm else []
        sm = getattr(getattr(self.ctx, "message_processor", None), "skills_manager", None)
        mcp = getattr(getattr(self.ctx, "message_processor", None), "mcp_manager", None)
        sessions, _skipped = self.session_inventory()
        provs = getattr(self.ctx, "provider_mgr", None)
        return {
            "ok": True,
            "version": self._manifest_version(),
            "warnings": self.warnings,
            "permission": self.engine.summary(),
            "counts": {
                "plugins": len(plugins),
                "plugins_enabled": sum(1 for p in plugins if pm and pm.is_plugin_enabled(p.plugin_id)),
                "skills": len(sm.skills_info) if sm else 0,
                "providers": len(provs.get_all_providers() or {}) if provs else 0,
                "mcp": len(mcp.servers) if mcp else 0,
                "sessions": len(sessions),
                "backups": self.backups.count(),
                "pending_confirms": self.confirm.pending_count,
            },
            "conflicts": self.conflicts(),
            "agent": {
                "installed": bool(pm and pm.has_plugin("agent")),
                "enabled": bool(pm and pm.has_plugin("agent") and pm.is_plugin_enabled("agent")),
            },
            "confirm": self._confirm_status(),
            "lang": self._panel_lang(),
        }

    def _panel_lang(self) -> str:
        try:
            lang = str(self.ctx.get_lang() or "en").lower()
        except Exception:
            lang = "zh"
        return "zh" if lang.startswith("zh") else "en"

    @staticmethod
    def _manifest_version() -> str:
        try:
            import json as _json
            data = _json.loads((Path(__file__).parent / "manifest.json").read_text(encoding="utf-8"))
            return str(data.get("version") or "")
        except Exception:
            return ""

    @register.api(method="GET", path="/overview", auth=True)
    async def api_overview(self):
        return self._panel_payload()

    @register.api(method="GET", path="/config", auth=True)
    async def api_get_config(self):
        cfg = copy.deepcopy(self.cfg) if isinstance(self.cfg, dict) else {}
        confirm = cfg.get("confirm")
        if isinstance(confirm, dict) and str(confirm.get("password") or ""):
            confirm["password"] = MASK  # never ship the real password to the browser
        return {"ok": True, "config": cfg, "warnings": self.warnings}

    @register.api(method="POST", path="/config", auth=True)
    async def api_set_config(self, payload: dict):
        pm = getattr(self.ctx, "plugin_mgr", None)
        if not pm:
            return {"ok": False, "error": "plugin manager is unavailable"}
        patch = (payload or {}).get("config") or {}
        if not isinstance(patch, dict):
            return {"ok": False, "error": "config must be a JSON object"}
        confirm_patch = patch.get("confirm")
        if isinstance(confirm_patch, dict):
            pw = str(confirm_patch.get("password") or "").strip()
            if not pw or pw == MASK:
                # empty / masked placeholder means "keep the stored password"
                confirm_patch = dict(confirm_patch)
                confirm_patch.pop("password", None)
                patch = dict(patch)
                patch["confirm"] = confirm_patch
        current = pm.get_plugin_config(self.plugin_id) or {}
        merged = self.deep_merge(current, patch)
        await pm.update_plugin_config(self.plugin_id, merged)
        self.audit.write(kind="write", tool="panel", domain="kira_ops", action="config",
                         ok=True, note="panel config update")
        return {"ok": True, "config": merged}

    @register.api(method="POST", path="/panic", auth=True)
    async def api_panic(self, payload: dict):
        pm = getattr(self.ctx, "plugin_mgr", None)
        if not pm:
            return {"ok": False, "error": "plugin manager is unavailable"}
        lock = bool((payload or {}).get("lock"))
        cfg = dict(self.cfg or {})
        master = dict(cfg.get("master") or {})
        master["panic_lock"] = lock
        cfg["master"] = master
        await pm.update_plugin_config(self.plugin_id, cfg)
        self.audit.write(kind="write", tool="panel", domain="master", action="panic",
                         ok=True, note=f"lock={lock}")
        return {"ok": True, "panic_lock": lock}

    @register.api(method="GET", path="/backups", auth=True)
    async def api_backups(self):
        return {"ok": True, "items": self.backups.list(100)}

    @register.api(method="POST", path="/backups/restore", auth=True)
    async def api_restore(self, payload: dict):
        bid = str((payload or {}).get("id") or "").strip()
        force = bool((payload or {}).get("force"))
        result = self.backups.restore(bid, force=force)
        self.audit.write(kind="write", tool="panel", domain="backup", action="restore",
                         target=bid, ok=bool(result.get("ok")),
                         error=str(result.get("errors") or ""))
        return result

    @register.api(method="GET", path="/audit", auth=True)
    async def api_audit(self, limit: int = 50, keyword: str = ""):
        return {"ok": True, "items": self.audit.tail(int(limit or 50), keyword=keyword or None)}

    @register.api(method="GET", path="/describe", auth=True)
    async def api_describe(self):
        load_all()
        return {"ok": True, "capabilities": [c(self).describe() for c in caps_pkg.REGISTRY.values()]}

    # ==================================================================
    # extension point: kira_ops.register_capability
    # ==================================================================

    @on.custom_event(event_name="kira_ops.register_capability")
    async def register_capability(self, event, *_):
        """Let another plugin contribute an extra domain without touching caps/.

        Payload: ``{"class": <Capability subclass>}`` (or ``{"capability": cls}``).
        The class must declare ``name`` and ``ACTIONS``; registration is
        rejected when the name is taken, illegal or the shape is wrong.
        """
        payload = getattr(event, "payload", None) or {}
        cap_cls = payload.get("class") or payload.get("capability")
        source = getattr(event, "source_plugin", "unknown")
        ok, detail = caps_pkg.register_capability(cap_cls)
        if ok:
            self.caps[cap_cls.name] = cap_cls(self)
            self.log(f"capability '{cap_cls.name}' contributed by plugin '{source}'")
        else:
            logger.warning(f"[kira_ops] rejected capability from '{source}': {detail}")
        return {"ok": ok, "detail": detail}


# Tool results are stringified by the framework when the tool message is built:
# render them as compact JSON instead of a Python dict repr (see Payload).
wrap_tool_results(KiraOpsPlugin)
