"""Provider domain: providers, models and their (protected) configuration."""

from __future__ import annotations

from ..core.redact import check_field_write, flatten
from . import Capability, fail, ok, paged, register


@register
class ProviderCap(Capability):
    name = "provider"
    ACTIONS = {
        "list":         ("read", False, "列出全部 Provider 与其模型数量"),
        "info":         ("read", False, "Provider 详情（配置打码）"),
        "models":       ("read", False, "列出 Provider 的全部模型（配置打码）"),
        "fetch_remote": ("read", False, "拉取远端模型列表（用于同步）"),
        "health":       ("read", False, "对指定模型做一次健康检查（会消耗少量额度）"),
        "add_model":    ("write", False, "新增一个模型配置"),
        "update_model": ("write", True, "修改一个模型配置"),
        "delete_model": ("write", True, "删除一个模型配置"),
        "sync":         ("write", False, "按远端差异批量增删模型"),
        "set_provider": ("write", False, "替换 Provider 级配置（受字段保护约束）"),
    }

    def _pm(self):
        return getattr(self.ctx, "provider_mgr", None)

    def _models(self, pm, pid: str) -> tuple:
        """Return (models, note).

        ``ProviderManager.get_models()`` maps every ``model_config`` key through
        the ``ModelType`` enum and raises ValueError for an unknown key (e.g. a
        legacy ``vlm`` entry in an older config). The framework's own WebUI
        reads ``model_config`` raw for exactly this reason, so fall back to the
        raw config instead of failing the whole provider domain.
        """
        try:
            return pm.get_models(pid) or {}, ""
        except Exception as exc:
            note = f"framework model index unavailable ({exc}); showing the raw config"
            try:
                providers = self.plugin.kira_config.get("providers", {}) or {}
                raw = (providers.get(pid) or {}).get("model_config") or {}
                fallback = {str(k): dict(v or {}) for k, v in raw.items()
                            if isinstance(v, dict)}
                return fallback, note
            except Exception:
                return {}, note

    # ------------------------------------------------------------------

    async def handle_read(self, action, params):
        pm = self._pm()
        if not pm:
            return fail("provider manager is unavailable")
        if action == "list":
            items = []
            configured = list((self.plugin.kira_config.get("providers", {}) or {}).keys())
            known = list((pm.get_all_providers() or {}).keys())
            for pid in sorted(set(known) | set(configured)):
                info = pm.get_provider_info(pid)
                models, note = self._models(pm, pid)
                entry = {
                    "provider_id": pid,
                    "name": getattr(info, "provider_name", "") or pid,
                    "format": getattr(info, "provider_type", "") or "",
                    "model_count": sum(len(v or {}) for v in models.values()),
                    "model_types": sorted(models.keys()),
                    "active": pid in known,
                }
                if note:
                    entry["warning"] = note
                items.append(entry)
            items.sort(key=lambda x: x["name"].lower())
            return ok(**paged(items, params, default=50))
        if action == "info":
            pid = str(params.get("provider_id") or "").strip()
            info = pm.get_provider_info(pid)
            if not info:
                return fail(f"provider '{pid}' not found")
            return ok(provider_id=pid,
                      name=info.provider_name,
                      format=info.provider_type,
                      config=self.plugin.mask(dict(info.provider_config or {})))
        if action == "models":
            pid = str(params.get("provider_id") or "").strip()
            if not pid:
                return fail("provider_id is required")
            if not pm.get_provider_info(pid):
                return fail(f"provider '{pid}' not found")
            models, note = self._models(pm, pid)
            counts = {str(k): len(v or {}) for k, v in models.items()}
            if params.get("full"):
                data = ok(provider_id=pid, counts=counts, models=self.plugin.mask(models))
            else:
                # ids only: the full per-model config costs hundreds of chars and
                # is rarely needed - pass args={"full": true} for it.
                # 3.0 起 key 是内部稳定 ID（uuid 形态），可读名在 entry["model_name"]：
                # 展示「可读名 [ID]」（同名时只显示一次），管理操作仍用 ID。
                def _disp(_models):
                    out = []
                    for _mid, _entry in (_models or {}).items():
                        _name = _entry.get("model_name") if isinstance(_entry, dict) else None
                        if _name and _name != _mid:
                            out.append(f"{_name} [{_mid}]")
                        else:
                            out.append(str(_mid))
                    return sorted(out)
                data = ok(provider_id=pid, counts=counts,
                          models={str(k): _disp(v) for k, v in models.items()})
            if note:
                data["warning"] = note
            return data
        if action == "fetch_remote":
            pid = str(params.get("provider_id") or "").strip()
            mtype = str(params.get("model_type") or "llm")
            try:
                remote = pm.fetch_remote_models(pid, mtype)
                if hasattr(remote, "__await__"):
                    remote = await remote
            except Exception as exc:
                return fail(f"fetch_remote_models failed: {exc!r}")
            ids = []
            for m in remote or []:
                if isinstance(m, dict):
                    ids.append(str(m.get("id") or m.get("model") or m.get("name") or ""))
                else:
                    ids.append(str(m))
            return ok(provider_id=pid, model_type=mtype, count=len(ids), models=ids)
        if action == "health":
            pid = str(params.get("provider_id") or "").strip()
            mtype = str(params.get("model_type") or "llm")
            mid = str(params.get("model_id") or "").strip()
            res = pm.health_check(pid, mtype, mid)
            if hasattr(res, "__await__"):
                res = await res
            return ok(provider_id=pid, model_type=mtype, model_id=mid, result=res)
        return fail(f"unknown read action '{action}'")

    # ------------------------------------------------------------------

    def backup_files(self, action, params):
        from core.utils.path_utils import get_config_path
        return [get_config_path() / "system_config.json"]

    def backup_label(self, action, params):
        return f"provider_{action}_{params.get('provider_id') or 'x'}"

    # ------------------------------------------------------------------

    def _check_cfg_fields(self, config):
        for path, key, _v in flatten(config or {}):
            good, why = check_field_write(key, self.plugin.engine.protected, restrict=True)
            if not good:
                return f"{path}: {why}"
        return ""

    async def handle_write(self, action, params):
        pm = self._pm()
        if not pm:
            return fail("provider manager is unavailable")
        pid = str(params.get("provider_id") or "").strip()
        if action == "add_model":
            mtype = str(params.get("model_type") or "").strip()
            mid = str(params.get("model_id") or "").strip()
            if not pid or not mtype or not mid:
                return fail("provider_id, model_type and model_id are required")
            cfg = params.get("config") or {}
            why = self._check_cfg_fields(cfg)
            if why:
                return fail(f"config blocked: {why}")
            res = pm.register_model(pid, mtype, mid, cfg)
            if hasattr(res, "__await__"):
                res = await res
            # 3.0 起 register_model 第三参是 model_name，返回生成的内部 ID；
            # 2.x 返回 None 且 key 即入参。两代分别用正确键核对。
            internal_id = res if isinstance(res, str) and res else None
            entries = (pm.get_models(pid) or {}).get(mtype) or {}
            found = (internal_id in entries) if internal_id else (mid in entries)
            return ok(provider_id=pid, model_type=mtype, model_id=internal_id or mid,
                      model_name=mid if internal_id else None, registered=bool(found))
        if action == "update_model":
            mtype = str(params.get("model_type") or "").strip()
            mid = str(params.get("model_id") or "").strip()
            cfg = params.get("config") or {}
            if not pid or not mtype or not mid:
                return fail("provider_id, model_type and model_id are required")
            why = self._check_cfg_fields(cfg)
            if why:
                return fail(f"config blocked: {why}")
            res = pm.update_model(pid, mtype, mid, cfg)
            if hasattr(res, "__await__"):
                res = await res
            return ok(provider_id=pid, model_type=mtype, model_id=mid, updated=bool(res))
        if action == "delete_model":
            mtype = str(params.get("model_type") or "").strip()
            mid = str(params.get("model_id") or "").strip()
            if not pid or not mtype or not mid:
                return fail("provider_id, model_type and model_id are required")
            res = pm.delete_model(pid, mtype, mid)
            if hasattr(res, "__await__"):
                res = await res
            return ok(provider_id=pid, model_type=mtype, model_id=mid, deleted=bool(res))
        if action == "sync":
            mtype = str(params.get("model_type") or "").strip()
            add_ids = [str(x) for x in (params.get("add_ids") or [])]
            del_ids = [str(x) for x in (params.get("delete_ids") or [])]
            if not pid or not mtype:
                return fail("provider_id and model_type are required")
            res = pm.sync_models(pid, mtype, add_ids, del_ids, params.get("config") or None)
            if hasattr(res, "__await__"):
                res = await res
            return ok(provider_id=pid, model_type=mtype, result=res)
        if action == "set_provider":
            cfg = params.get("config") or {}
            if not isinstance(cfg, dict) or not cfg:
                return fail("config must be a non-empty JSON object")
            why = self._check_cfg_fields(cfg)
            if why:
                return fail(f"config blocked: {why}")
            provs = self.plugin.kira_config.get("providers", {}) or {}
            cur = provs.get(pid)
            if not isinstance(cur, dict):
                return fail(f"provider '{pid}' not found")
            prov_cfg = cur.get("provider_config") or {}
            prov_cfg = self.plugin.deep_merge(prov_cfg, cfg)
            cur["provider_config"] = prov_cfg
            provs[pid] = cur
            self.plugin.kira_config["providers"] = provs
            self.plugin.kira_config.save_config()
            try:
                pm.set_provider(pid, cur)
            except Exception as exc:
                return fail(f"provider config saved but re-instantiate failed: {exc!r}")
            return ok(provider_id=pid, applied=sorted(str(k) for k in cfg.keys()))
        return fail(f"unknown write action '{action}'")
