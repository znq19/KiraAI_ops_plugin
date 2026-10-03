"""Control domain: restart / shutdown, each with its own switch (default off).

Execution delegates to the very same code path the WebUI uses:
lifecycle.stop() -> uvicorn should_exit -> os._exit(code). No shell, no new
privileges; the supervisor in main.py restarts the child on exit code 42.
"""

from __future__ import annotations

import asyncio
import os

from . import Capability, fail, ok, register

RESTART_EXIT_CODE = 42


@register
class ControlCap(Capability):
    name = "control"
    ACTIONS = {
        "restart":  ("write", True, "重启 KiraAI（独立开关必需；另需 full 档位或密码授权）"),
        "shutdown": ("write", True, "关闭 KiraAI（独立开关必需；另需 full 档位或密码授权）"),
        "info":     ("read", False, "查询重启/关机开关状态"),
    }

    def classify(self, action, params):
        return f"control.{action}"

    # ------------------------------------------------------------------

    def handle_read(self, action, params):
        if action == "info":
            return ok(restart_enabled=bool(self.plugin.engine.control.get("allow_restart", False)),
                      shutdown_enabled=bool(self.plugin.engine.control.get("allow_shutdown", False)))
        return fail(f"unknown read action '{action}'")

    # ------------------------------------------------------------------

    async def handle_write(self, action, params):
        if action not in ("restart", "shutdown"):
            return fail(f"unknown write action '{action}'")

        if action == "restart":
            if not bool(self.plugin.engine.control.get("allow_restart", False)):
                return fail("restart is disabled in settings")
            exit_code = RESTART_EXIT_CODE
        else:
            if not bool(self.plugin.engine.control.get("allow_shutdown", False)):
                return fail("shutdown is disabled in settings")
            exit_code = 0

        lifecycle = getattr(self.plugin, "lifecycle", None)
        if lifecycle is None:
            return fail("lifecycle handle is unavailable; cannot control the process")

        def _exit_later():
            os._exit(exit_code)

        async def _sequence():
            # Give the tool result a moment to be delivered before the
            # process goes down, mirroring webui/routes/system.py.
            await asyncio.sleep(1.0)
            try:
                await lifecycle.stop()
            except Exception:
                pass
            server = getattr(lifecycle, "uvicorn_server", None)
            if server is not None:
                try:
                    server.should_exit = True
                except Exception:
                    pass
            asyncio.get_running_loop().call_later(0.5, _exit_later)

        task = asyncio.create_task(_sequence())
        self.plugin.tasks.add(task)
        task.add_done_callback(self.plugin.tasks.discard)
        return ok(action=action,
                  hint=("restart scheduled - the supervisor will bring KiraAI back"
                        if action == "restart" else
                        "shutdown scheduled - the process will exit"))
