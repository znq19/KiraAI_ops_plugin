"""Plugin domain: list / info / config / lifecycle for installed plugins."""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from core.utils.path_utils import get_config_path, get_data_path

from ..core.redact import flatten
from . import Capability, fail, ok, paged, register

PLUGIN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


@register
class PluginCap(Capability):
    name = "plugin"
    ACTIONS = {
        "list":       ("read", False, "列出全部插件（含状态/启用情况）"),
        "info":       ("read", False, "插件详情（manifest、配置、加载错误）"),
        "config_get": ("read", False, "读取插件配置（敏感字段打码）"),
        "config_set": ("write", False, "修改插件配置并热重载该插件"),
        "enable":     ("write", False, "启用插件"),
        "disable":    ("write", True, "停用插件"),
        "reload":     ("write", False, "热重载插件（重新导入并初始化）"),
        "install":    ("write", False, "从插件商店安装插件（standard 可用）"),
        "update":     ("write", False, "从插件商店重新安装/更新插件（standard 可用）"),
        "uninstall":  ("write", True, "卸载并从磁盘删除插件"),
    }

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _pm(self):
        return getattr(self.ctx, "plugin_mgr", None)

    def _pid(self, params) -> str:
        return str(params.get("plugin_id") or params.get("target") or "").strip()

    def _info(self, params):
        pm = self._pm()
        if not pm:
            return None, fail("plugin manager is unavailable")
        pid = self._pid(params)
        if not pid:
            return None, fail("plugin_id is required")
        info = pm.get_plugin_info(pid)
        if info is None:
            return None, fail(f"plugin '{pid}' is not installed")
        return info, None

    # ------------------------------------------------------------------
    # reads
    # ------------------------------------------------------------------

    def handle_read(self, action, params):
        pm = self._pm()
        if not pm:
            return fail("plugin manager is unavailable")
        if action == "list":
            keyword = str(params.get("keyword") or "").lower()
            items = []
            for p in pm.list_plugins():
                hay = f"{p.plugin_id} {p.display_name or ''}".lower()
                if keyword and keyword not in hay:
                    continue
                items.append({
                    "id": p.plugin_id,
                    "name": p.display_name or p.plugin_id,
                    "version": p.version or "-",
                    "status": p.status or "ready",
                    "enabled": pm.is_plugin_enabled(p.plugin_id),
                    "builtin": bool(p.builtin),
                    "error": (p.error or "")[:160],
                })
            items.sort(key=lambda x: (not x["enabled"], x["id"].lower()))
            return ok(**paged(items, params, default=50))

        if action == "info":
            info, err = self._info(params)
            if err:
                return err
            load_errors = pm.get_plugin_load_errors() or {}
            err_info = load_errors.get(info.plugin_id) or {}
            data = {
                "id": info.plugin_id,
                "name": info.display_name,
                "version": info.version,
                "author": info.author,
                "description": str(info.description or "")[:120],
                "repo": info.repo,
                "core_version": info.core_version,
                "tags": list(info.tags or []),
                "builtin": bool(info.builtin),
                "uninstallable": bool(info.uninstallable),
                "status": info.status,
                "enabled": pm.is_plugin_enabled(info.plugin_id),
                "load_error": (err_info.get("error") if isinstance(err_info, dict) else str(err_info or "")) or (info.error or ""),
                "has_schema": bool(pm.get_plugin_schema(info.plugin_id)),
            }
            if str(params.get("detail") or "") == "full":
                data["config"] = self.plugin.mask(pm.get_plugin_config(info.plugin_id))
            return ok(**data)

        if action == "config_get":
            info, err = self._info(params)
            if err:
                return err
            return ok(plugin_id=info.plugin_id,
                      config=self.plugin.mask(pm.get_plugin_config(info.plugin_id)))

        return fail(f"unknown read action '{action}'")

    # ------------------------------------------------------------------
    # helpers for writes
    # ------------------------------------------------------------------

    def backup_files(self, action, params):
        pid = self._pid(params)
        files = []
        if action in ("config_set", "uninstall"):
            files.append(get_config_path() / "plugins" / f"{pid}.json")
        elif action in ("enable", "disable"):
            files.append(get_config_path() / "plugins.json")
        return [f for f in files if Path(f).exists()]

    def backup_label(self, action, params):
        return f"plugin_{action}_{self._pid(params) or 'x'}"

    # ------------------------------------------------------------------
    # writes
    # ------------------------------------------------------------------

    async def handle_write(self, action, params):
        pm = self._pm()
        if not pm:
            return fail("plugin manager is unavailable")
        pid = self._pid(params)
        if not pid:
            return fail("plugin_id is required")

        if action == "config_set":
            info, err = self._info(params)
            if err:
                return err
            patch = params.get("patch") or {}
            if not isinstance(patch, dict) or not patch:
                return fail("patch must be a non-empty JSON object")
            for path, key, _value in flatten(patch):
                good, why = self.plugin.engine.check_field_write(key, restrict=False)
                if not good:
                    return fail(f"{path}: {why}")
            if info.plugin_id == "agent":
                violations = self.plugin.validate_agent_policy_patch(patch)
                if violations:
                    return fail("agent policy blocked: " + "; ".join(violations))
            current = pm.get_plugin_config(info.plugin_id) or {}
            merged = self.plugin.deep_merge(current, patch)
            await pm.update_plugin_config(info.plugin_id, merged)
            return ok(plugin_id=info.plugin_id, applied=sorted(str(k) for k in patch.keys()),
                      hint="plugin config updated and hot-reloaded")

        if action in ("enable", "disable"):
            if pid == self.plugin.plugin_id and action == "disable":
                return fail("cannot disable kira_ops from inside itself; use the WebUI or framework settings")
            if not pm.has_plugin(pid):
                return fail(f"plugin '{pid}' is not installed")
            await pm.set_plugin_enabled(pid, action == "enable")
            return ok(plugin_id=pid, enabled=(action == "enable"))

        if action == "reload":
            if pid == self.plugin.plugin_id:
                return fail("cannot reload kira_ops from inside its own tool call; use the WebUI")
            if not pm.has_plugin(pid):
                return fail(f"plugin '{pid}' is not installed")
            await pm.reload(pid)
            info = pm.get_plugin_info(pid)
            return ok(plugin_id=pid,
                      status=(info.status if info else "unknown"),
                      error=((info.error or "") if info else ""))

        if action in ("install", "update"):
            force = action == "update" or bool(params.get("force"))
            return await self.plugin.install_from_store(pid, force=force)

        if action == "uninstall":
            return await self._uninstall(pm, pid, params)

        return fail(f"unknown write action '{action}'")

    async def _uninstall(self, pm, pid: str, params) -> dict:
        if pid == self.plugin.plugin_id:
            return fail("kira_ops cannot uninstall itself")
        if not PLUGIN_ID_RE.match(pid):
            return fail(f"illegal plugin id '{pid}'")
        is_uninstallable = getattr(pm, "is_plugin_uninstallable", None)
        if callable(is_uninstallable):
            try:
                if not is_uninstallable(pid):
                    return fail(f"plugin '{pid}' is builtin-protected and cannot be removed")
            except Exception:
                pass

        plugin_dir = None
        candidate = Path(pm.plugin_dir) / pid
        if candidate.exists():
            plugin_dir = candidate
        else:
            try:
                p = pm.get_plugin_module_path(pid)
                if p:
                    p = Path(p)
                    plugin_dir = p if p.is_dir() else p.parent
            except Exception:
                plugin_dir = None

        if plugin_dir is None or not plugin_dir.exists():
            if not pm.has_plugin(pid):
                return fail(f"plugin '{pid}' has no directory and is not registered")
            return fail(f"cannot locate the directory of plugin '{pid}'")

        plugins_root = Path(pm.plugin_dir).resolve()
        resolved = plugin_dir.resolve()
        if resolved == plugins_root or resolved.parent != plugins_root:
            return fail("refusing to delete: target is not a direct child of the plugin directory")

        try:
            result = pm.uninstall_plugin(pid)
            if hasattr(result, "__await__"):
                await result
        except ValueError as exc:
            self.plugin.log(f"uninstall: plugin {pid} was not fully registered: {exc}")
        except Exception as exc:
            return fail(f"failed to unregister plugin '{pid}': {exc}")

        try:
            shutil.rmtree(resolved)
        except Exception as exc:
            return fail(f"plugin '{pid}' was unregistered but its directory could not be deleted: {exc}",
                        directory=str(resolved))

        removed = {"directory": str(resolved)}
        if bool(params.get("cleanup_config")):
            cfg = get_config_path() / "plugins" / f"{pid}.json"
            try:
                if cfg.exists():
                    cfg.unlink()
                    removed["config"] = str(cfg)
            except Exception:
                pass
        if bool(params.get("cleanup_data")):
            data_dir = get_data_path() / "plugin_data" / pid
            try:
                if data_dir.exists():
                    shutil.rmtree(data_dir)
                    removed["plugin_data"] = str(data_dir)
            except Exception:
                pass
        return ok(plugin_id=pid, removed=removed,
                  hint="removed from memory and disk; config/plugin_data kept unless cleanup flags were set")
