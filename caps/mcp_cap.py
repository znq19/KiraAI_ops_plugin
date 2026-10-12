"""MCP domain: servers, tools, scopes - thin wrapper over MCPManager."""

from __future__ import annotations

from . import Capability, fail, ok, paged, register


@register
class McpCap(Capability):
    name = "mcp"
    ACTIONS = {
        "list":        ("read", False, "列出全部 MCP 服务器"),
        "info":        ("read", False, "MCP 服务器详情（含编辑器配置与工具列表）"),
        "add":         ("write", False, "新增 MCP 服务器"),
        "update":      ("write", False, "更新 MCP 服务器配置"),
        "enable":      ("write", False, "启用 MCP 服务器"),
        "disable":     ("write", False, "停用 MCP 服务器"),
        "tool_toggle": ("write", False, "启用/禁用单个 MCP 工具"),
        "scope":       ("write", False, "设置 MCP 服务器的会话范围"),
        "delete":      ("write", True, "删除 MCP 服务器"),
    }

    def _mgr(self):
        # 3.0 起规范入口是 PluginContext.mcp_mgr；旧路径兜底（特征识别，不绑版本号）
        mgr = getattr(self.ctx, "mcp_mgr", None)
        if mgr is not None:
            return mgr
        mp = getattr(self.ctx, "message_processor", None)
        return getattr(mp, "mcp_manager", None)

    def _server(self, sid):
        mgr = self._mgr()
        if not mgr:
            return None, fail("MCP manager is unavailable")
        wanted = str(sid or "").strip()
        for s in mgr.servers:
            if s.id == wanted:
                return s, None
        return None, fail(f"MCP server '{wanted}' not found")

    # ------------------------------------------------------------------

    async def handle_read(self, action, params):
        mgr = self._mgr()
        if not mgr:
            return fail("MCP manager is unavailable")
        if action == "list":
            items = []
            for s in mgr.servers:
                entry = {
                    "id": s.id,
                    "name": s.name,
                    "enabled": bool(s.enabled),
                    "type": s.type,
                }
                if s.tools:
                    entry["tools"] = len(s.tools)
                if s.disabled_tools:
                    entry["disabled_tools"] = list(s.disabled_tools)
                items.append(entry)
            return ok(**paged(items, params, default=50))
        if action == "info":
            s, err = self._server(params.get("server_id"))
            if err:
                return err
            data = {
                "id": s.id,
                "name": s.name,
                "enabled": bool(s.enabled),
                "type": s.type,
                "description": s.description,
                "scope": mgr.get_server_scope(s.id) or {},
                "disabled_tools": list(s.disabled_tools or []),
            }
            try:
                data["editor_config"] = self.plugin.mask(mgr.get_server_config_for_editor(s.id))
            except Exception as exc:
                data["editor_config_error"] = str(exc)
            if str(params.get("detail") or "") == "full":
                try:
                    tools = mgr.list_tools(s, keep_connection=False)
                    if hasattr(tools, "__await__"):
                        tools = await tools
                    names = []
                    for t in tools or []:
                        names.append(getattr(t, "name", None) or (t.get("name") if isinstance(t, dict) else str(t)))
                    data["tools"] = names
                except Exception as exc:
                    data["tools_error"] = str(exc)
            return ok(**data)
        return fail(f"unknown read action '{action}'")

    # ------------------------------------------------------------------

    def backup_files(self, action, params):
        from core.utils.path_utils import get_config_path
        return [get_config_path() / "mcp.json"]

    def backup_label(self, action, params):
        return f"mcp_{action}"

    # ------------------------------------------------------------------

    async def handle_write(self, action, params):
        mgr = self._mgr()
        if not mgr:
            return fail("MCP manager is unavailable")
        if action == "add":
            name = str(params.get("name") or "").strip()
            desc = str(params.get("description") or "")
            cfg = params.get("config") or {}
            if not name or not isinstance(cfg, dict) or not cfg:
                return fail("name and a non-empty config JSON are required")
            try:
                server = mgr.add_or_update_server_from_config(name, desc, cfg)
            except Exception as exc:
                return fail(f"add MCP server failed: {exc!r}")
            return ok(server_id=server.id, name=server.name)
        if action == "update":
            s, err = self._server(params.get("server_id"))
            if err:
                return err
            editor = params.get("config") or {}
            if not isinstance(editor, dict) or not editor:
                return fail("config must be a non-empty JSON object")
            try:
                await mgr.update_server_from_editor(s.id, params.get("name") or s.name,
                                                    str(params.get("description") or s.description), editor)
            except Exception as exc:
                return fail(f"update MCP server failed: {exc!r}")
            return ok(server_id=s.id)
        if action in ("enable", "disable"):
            s, err = self._server(params.get("server_id"))
            if err:
                return err
            try:
                if action == "enable":
                    await mgr.enable_server(s.id)
                else:
                    await mgr.disable_server(s.id)
            except Exception as exc:
                return fail(f"{action} MCP server failed: {exc!r}")
            return ok(server_id=s.id, enabled=(action == "enable"))
        if action == "tool_toggle":
            s, err = self._server(params.get("server_id"))
            if err:
                return err
            tool = str(params.get("tool") or "").strip()
            if not tool:
                return fail("tool name is required")
            try:
                mgr.set_tool_enabled(s.id, tool, bool(params.get("enabled", True)))
            except Exception as exc:
                # the framework raises ValueError for an unknown tool name
                return fail(f"cannot toggle '{tool}' on server '{s.id}': {exc}")
            return ok(server_id=s.id, tool=tool, enabled=bool(params.get("enabled", True)))
        if action == "scope":
            s, err = self._server(params.get("server_id"))
            if err:
                return err
            mode = params.get("mode")
            sessions = params.get("sessions") or []
            if mode not in (None, "", "allow", "deny"):
                return fail("mode must be 'allow', 'deny' or empty (clear)")
            mgr.set_server_scope(s.id, mode or None, [str(x) for x in sessions or []])
            return ok(server_id=s.id, scope=mgr.get_server_scope(s.id) or {})
        if action == "delete":
            s, err = self._server(params.get("server_id"))
            if err:
                return err
            try:
                await mgr.delete_server(s.id)
            except Exception as exc:
                return fail(f"delete MCP server failed: {exc!r}")
            return ok(server_id=s.id, deleted=True)
        return fail(f"unknown write action '{action}'")
