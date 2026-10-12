"""Skill domain: list / refresh / enable / scope for data/skills."""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from core.utils.path_utils import get_data_path

from . import Capability, fail, ok, paged, register, to_int

SKILL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


@register
class SkillCap(Capability):
    name = "skill"
    ACTIONS = {
        "list":    ("read", False, "列出全部技能与启用状态"),
        "info":    ("read", False, "读取单个技能的详情"),
        "content": ("read", False, "读取技能的 SKILL.md 原文"),
        "scope":   ("read", False, "读取技能的会话范围设置"),
        "refresh": ("write", False, "重新扫描技能目录（热刷新技能列表）"),
        "enable":  ("write", False, "启用技能"),
        "disable": ("write", False, "停用技能"),
        "set_scope": ("write", False, "设置技能的会话范围"),
        "install": ("write", True, "从压缩包/仓库安装一个新的技能"),
        "remove":  ("write", True, "删除一个技能目录"),
    }

    def _sm(self):
        # 3.0 起规范入口是 PluginContext.skills_mgr；旧路径兜底（特征识别，不绑版本号）
        sm = getattr(self.ctx, "skills_mgr", None)
        if sm is not None:
            return sm
        mp = getattr(self.ctx, "message_processor", None)
        return getattr(mp, "skills_manager", None)

    def _skill(self, name):
        sm = self._sm()
        if not sm:
            return None, fail("skills manager is unavailable")
        wanted = str(name or "").strip()
        for s in sm.skills_info:
            if s.name == wanted:
                return s, None
        return None, fail(f"skill '{wanted}' not found")

    # ------------------------------------------------------------------

    def handle_read(self, action, params):
        sm = self._sm()
        if not sm:
            return fail("skills manager is unavailable")
        if action == "list":
            items = [{
                "name": s.name,
                "enabled": bool(s.enabled),
                "description": s.description,
                "path": str(s.path),
            } for s in sm.skills_info]
            items.sort(key=lambda x: (not x["enabled"], x["name"].lower()))
            return ok(**paged(items, params, default=50))
        if action == "info":
            s, err = self._skill(params.get("name"))
            if err:
                return err
            scope = sm.get_skill_scope(s.name)
            return ok(name=s.name, enabled=bool(s.enabled), description=s.description,
                      path=str(s.path), scope=scope or {})
        if action == "content":
            s, err = self._skill(params.get("name"))
            if err:
                return err
            path = Path(s.path) / "SKILL.md"
            try:
                text = path.read_text(encoding="utf-8")
            except Exception as exc:
                return fail(f"cannot read SKILL.md: {exc}")
            limit = to_int(params.get("limit"), 4000, 100, 20000)
            return ok(name=s.name, content=text[:limit], truncated=len(text) > limit)
        if action == "scope":
            s, err = self._skill(params.get("name"))
            if err:
                return err
            return ok(name=s.name, scope=sm.get_skill_scope(s.name) or {})
        return fail(f"unknown read action '{action}'")

    # ------------------------------------------------------------------

    def handle_write(self, action, params):
        sm = self._sm()
        if not sm:
            return fail("skills manager is unavailable")
        if action == "refresh":
            sm.skills_info = sm.scan_skill_dir()
            return ok(count=len(sm.skills_info),
                      names=[s.name for s in sm.skills_info])
        if action in ("enable", "disable"):
            s, err = self._skill(params.get("name"))
            if err:
                return err
            changed = sm.set_skill_enabled(s.name, action == "enable")
            return ok(name=s.name, enabled=(action == "enable"), changed=changed)
        if action == "set_scope":
            s, err = self._skill(params.get("name"))
            if err:
                return err
            mode = params.get("mode")
            sessions = params.get("sessions") or []
            if mode not in (None, "", "allow", "deny"):
                return fail("mode must be 'allow', 'deny' or empty (clear)")
            if mode and not isinstance(sessions, list):
                return fail("sessions must be a list")
            sm.set_skill_scope(s.name, mode or None, [str(x) for x in sessions or []])
            return ok(name=s.name, scope=sm.get_skill_scope(s.name) or {})
        if action == "install":
            return self._install(params)
        if action == "remove":
            return self._remove(params)
        return fail(f"unknown write action '{action}'")

    # ------------------------------------------------------------------

    def _remove(self, params):
        s, err = self._skill(params.get("name"))
        if err:
            return err
        skills_root = Path(get_data_path() / "skills").resolve()
        target = Path(s.path).resolve()
        if target == skills_root or target.parent != skills_root:
            return fail("refusing to delete: target is not a direct child of data/skills")
        try:
            shutil.rmtree(target)
        except Exception as exc:
            return fail(f"failed to delete skill directory: {exc}")
        sm = self._sm()
        sm.skills_info = sm.scan_skill_dir()
        return ok(name=s.name, removed=str(target))

    def _install(self, params):
        url = str(params.get("url") or "").strip()
        raw_name = str(params.get("name") or "").strip()
        if not url:
            return fail("url is required (a zip archive containing SKILL.md, or a GitHub repo)")
        if not SKILL_NAME_RE.match(raw_name):
            return fail("name is required and must be a simple identifier (letters/digits/_/-/.)")
        return self.plugin.install_skill_from_url(url, raw_name, overwrite=bool(params.get("overwrite")))
