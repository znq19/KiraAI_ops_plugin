"""Store domain: listing search + install/update via the store client."""

from __future__ import annotations

from . import Capability, fail, ok, register, to_int


@register
class StoreCap(Capability):
    name = "store"
    ACTIONS = {
        "search":   ("read", False, "搜索插件商店"),
        "sources":  ("read", False, "查看商店数据源与缓存状态"),
        "install":  ("write", False, "从商店安装插件（standard 可用）"),
        "update":   ("write", False, "从商店更新插件（standard 可用）"),
    }

    async def handle_read(self, action, params):
        params = params or {}
        if action == "sources":
            return ok(**self.plugin.store.status())
        if action == "search":
            return await self.plugin.store_search(
                keyword=str(params.get("keyword") or ""),
                author=str(params.get("author") or ""),
                tag=str(params.get("tag") or ""),
                limit=to_int(params.get("limit"), 0, 0, 50),
            )
        return fail(f"unknown read action '{action}'")

    # ------------------------------------------------------------------

    def backup_label(self, action, params):
        return f"store_{action}"

    async def handle_write(self, action, params):
        if action in ("install", "update"):
            pid = str(params.get("plugin_id") or params.get("target") or "").strip()
            force = action == "update" or bool(params.get("force"))
            return await self.plugin.install_from_store(pid, force=force)
        return fail(f"unknown write action '{action}'")
