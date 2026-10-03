# Kira 运行自控台（kira_ops）

给 **KiraAI** 用的运行自控台插件：让你（或者你的 AI）用一个统一入口，看懂并管理整个 KiraAI 的运行状态——
插件、技能、Provider、MCP、会话、人设、系统配置、日志、插件商店，全都能查、能改、能热生效，
而且**该拦的都拦得住、改坏了能回滚、干了什么都留痕**。

面向所有 KiraAI 部署，不绑定任何特定群、特定用户或特定目录，装上就能用。

- 版本：**1.1.0** ｜ 要求：KiraAI **≥ 2.34.6**（用到 2.34.6 的 `download_file(max_bytes)`、
  2.34.5 的内置 `agent` 插件、2.34.4 的 MCP 单工具开关、2.34.0 的 `multi_select` 动态源）
- 作者：**AiriLife-ai+znq19** ｜ 仓库：https://github.com/znq19/KiraAI_ops_plugin

---

## 一、它能干什么（30 秒版）

| 能力 | 说明 |
| --- | --- |
| 看状态 | 插件/技能/Provider/MCP/会话数量、启用情况、加载错误、权限档位、告警 |
| 管插件 | 启停、热重载、改配置、从商店安装/更新/卸载（带备份**和目录级回滚**） |
| 管技能 | 刷新、启停、会话范围、查看 SKILL.md、安装/删除 |
| 管 Provider | 查看（密钥打码）、增改删模型、同步远端模型、健康检查 |
| 管 MCP | 增改删服务器、启停、单工具开关、会话范围 |
| 管会话 | 标题、能力覆盖、记忆条数、清记忆、删除（联动清理技能/MCP 范围） |
| 管人设 | 查看列表与正文、切换激活（改内容默认只读，可放开） |
| 改配置 | 系统配置与插件配置热修改，敏感字段一律拒绝写入 |
| 看日志 | 最近日志、关键词搜索、日志文件概况、按关键词读文件尾部（**单条自动截断，不爆上下文**） |
| 插件商店 | 搜索/安装/更新，自动选择最快的 GitHub 加速代理 |
| 重启/关机 | 两个**独立开关**，默认都关，开启后还要"full 档位 + 确认令牌" |
| 文件/命令 | **不重复实现**：只托管内置 `agent` 插件的文件/命令策略，执行仍走 agent 插件 |
| 自保机制 | 自动备份 + 一键回滚、审计留痕（含被拒操作）、全锁（紧急只读）、数据保护 |

---

## 二、三步上手（小白版）

### 第 1 步：确认插件在跑

1. 打开 KiraAI 的 WebUI（默认 `http://127.0.0.1:5267`）。
2. 左侧进入「插件」，找到 **Kira 运行自控台（Kira Ops Console）**，确认是「启用」状态。
3. 如果看到「冲突处理」提示：本插件会自动把旧的独立商店插件（plugin_store_search）**关闭**
   （不会删除），避免功能重复。想保留旧插件互不干扰，就去本插件面板把「接管插件商店」关掉。

> **升级注意**：请**整目录替换**升级（停用 → 删除 `data/plugins/kira_ops` → 拷入新目录 → 启用）。
> 用插件列表里的"更新"按钮也可以（1.1.0 起更新会自动重载子模块并在失败时回滚），
> 但手工替换最干净。

### 第 2 步：配置"谁能用、能用到什么程度"（重要！）

左侧进入插件自带的「**运行自控台**」面板（或点插件卡片上的入口），主要看三块：

1. **访问控制**
   - `允许名单`：**留空 = 所有会话都能用**；想收紧就填会话 ID（如 `qq:gm:123456`）。
   - `禁止名单`：优先级最高，填进去的会话一律拒绝。
   - `只读名单`：名单内的会话只能看，不能改。
2. **风险分级**
   - `能力档位`：默认 `standard`（日常读 + 常规写，**含插件安装/升级**，够用）。想更保守改 `readonly`。
   - `高危动作清单`：哪些操作算"超档"（删/停插件、删模型、清记忆、改人设等），默认已列好，可增删。
     建议把 `agent.set` 也勾上——它会放宽文件/命令策略。
   - **超档怎么办（两条路）**：
     ① 开了「密码确认授权」：**任何会话**触发超档动作，都会向你的确认会话发一条授权请求，
        **你直接回复密码就执行**（最省事，推荐）；
     ② 没开：就得走老规矩——档位升到 `dangerous` + 把会话加进 `高危会话名单` + 确认令牌，三者齐备。
   - `高危会话名单`：走路径②时必填、不继承；**留空 = 路径②无人可用**（安全默认）。
   - `确认令牌`：路径②下高危动作要先拿令牌再确认，默认 300 秒有效，防止手滑。
3. **重启与关机**：两个开关默认都关。真要开，还得切 `full` 档位 + 拿令牌才能执行。

> 面板里改任何设置，**保存即热生效**，不用重启 KiraAI。面板支持中英切换
> （默认跟随 KiraAI 的语言设置，标题栏按钮可手动切）。

### 第 3 步：开始用

直接对你的 AI 说人话就行，例如：

- 「看看系统状态」→ AI 调用 `ops_status`
- 「列出所有插件，看看有没有加载失败的」→ `ops_read(domain=plugin)`
- 「把 xxx 插件停用/重载」→ `ops_action(domain=plugin, action=disable/reload)`
- 「帮我搜一下商店里有没有语音插件」→ `ops_store(action=search, keyword=语音)`
- 「把某个 Provider 的模型列表拉下来同步一下」→ `ops_action(domain=provider, action=sync)`
- 「回滚刚才那次配置修改」→ `ops_read(domain=backup)` 找到回滚点 → `ops_action(domain=backup, action=restore)`

高危动作的流程：AI 第一次调用会拿到一个**确认令牌**，再调 `ops_confirm(token=...)` 才真正执行。

**密码确认授权（v1.2.0 新增）**：在面板「风险分级」区配置**确认会话**（可多个，私聊/群均可）后，
它就是高危/超档动作的**授权通道**——任何会话（包括 standard 档位下）触发超档动作时，
控制台会向**所有确认会话同时**机械发送一条授权请求（不走 AI）；你在有效期内于**任一会话**
**直接回复确认密码本身**（比如密码是 `114514` 就只发 `114514`）即视为批准，
**动作随即以发起会话/发起人的身份执行**——"输了就能用"。这条密码消息会被插件拦截，
**不会触发 AI 回复**，这些会话里的其他聊天完全不受影响；执行结果会回执到**所有**确认会话
（免得其他人重复操作）、并以系统通知注入发起会话让 AI 转述。
确认请求 / 批准回执 / 原会话通知三条文案都可在面板修改（支持占位符）。
**密码绝不过期也不漏**：没有待确认请求时，你发的密码会被替换成一段短占位符
（默认 `[收到 Kira 运行自控台（kira_ops）确认密码，当前并无待确认请求]`，面板可改）后交回给 bot——聊天照常自然进行，而密码本身
永远不会进入 AI 的上下文与会话记忆。

**插件从不向聊天说话**：拒绝、超档、待确认都只是**工具结果**（返回给 bot 自己看），
聊天里不会出现任何机械文案；批准后注入的也是一条 `Notice`（同样是给 bot 看的，是否转述由它决定）。
密码放行的是"档位/名单"这一层（越档授权）；**底线永远不放行**：总闸关闭、全锁（panic）、
禁止名单、允许名单、只读名单在批准时依然会拦。**重启/关机 = 开关 + 越权都要**：
独立开关关着就永远拒绝（密码也没用）；开关开着时，不到 full 档位可以密码授权执行。
密码字段被 `write_deny` 内置关键词保护（模型改不了，只能面板/设置页修改），面板不回显原值；
默认关闭，不配置时行为与旧版完全一致。
⚠️ 若把群配成确认会话，密码会以明文出现在群里（消息虽不进 AI，但群成员可见）——建议优先私聊。

---

## 三、安全设计（为什么可以放心）

- **三旋钮权限**：谁能用（允许/禁止/只读名单）＋ 档位（readonly/standard/dangerous/full）＋
  高危动作清单。判定顺序固定、失败即拒绝（fail-closed）：
  禁止名单最优先 → 允许名单 → 只读 → 档位 → 高危门槛 → 数据保护。
- **高危必填**：`高危会话名单` 留空时，**任何会话都不能执行高危动作**——这是故意的，
  防止"能用工具的人"顺带获得删插件/清记忆的权力。面板里会看到这条告警，不是故障。
- **数据保护**（任何域都绕不过，包括 agent 策略）：
  - 读取时对 `api_key`、`token`、`secret` 等字段自动打码；
  - **禁止写入敏感字段**（改密钥请走 KiraAI 官方界面——这是硬拦，没有后门动作）；
  - `core/`、`webui/`、记忆库等关键路径禁止读写删。
- **自动备份**：每个写动作执行前，自动把目标文件快照到
  `data/plugin_data/kira_ops/backups/`，保留份数/天数/总大小都可配，自动清理。
  改坏了用 `ops_action(domain=backup, action=restore, target=回滚点ID)` 一键恢复；
  面板上的「回滚」按钮在目标被外部改动时，勾选 `force` 即可强制覆盖。
  写成功的返回值里会"搭车"带上回滚点路径。
- **插件安装/更新也是双保险**：
  - 安装包一律由**框架官方安装器**处理（≤50 MiB、≤10000 条目、压缩比 ≤100:1、
    中央目录 ≤512 KiB、Zip-Slip 防护、GitHub 镜像自动测速）；
  - 更新前先把整个插件目录备份，新代码加载失败会**自动恢复旧版本并重新激活**，
    不会留下"新 main.py + 旧子模块"的半残状态。
- **审计**：所有写动作**和所有被拒操作**都记录到
  `data/plugin_data/kira_ops/audit/audit-日期.jsonl`（读操作可选开启）。
- **全锁**：`ops_panic(lock=true)` 一键把全部写操作降级成只读，紧急刹车用。
- **重启/关机**：独立开关（底线，不可越权）+ full 档位或密码授权 + 确认令牌；实现走的是 KiraAI 官方同款
  进程退出流程（supervisor 自动拉起）。

---

## 四、常见问题（FAQ）

**Q：AI 说"高危动作被拒绝"？**
A：两种情况。① 没开「密码确认授权」：那得三件套齐备——档位 dangerous/full、高危会话名单加了
这个会话、带确认令牌。② 开了「密码确认授权」（v1.2.0+）：超档动作会改为**向你的确认会话发授权请求**，
你回复密码后自动执行；看不到这条请求就检查 dm_enabled/dm_session/密码是否配齐（面板有状态行）。
注意插件的**安装/升级从 v1.2.0 起不算超档**，standard 直接可用。
按提示补齐即可（高危名单填好后立即生效）。

**Q：我想改人设，为什么被拒？**
A：人设默认只读。到面板「数据保护」里打开「允许修改人设」，并且需要 dangerous 档位 + 确认令牌。

**Q：为什么搜到的旧商店插件被关了？**
A：本插件默认"接管"旧商店插件（只关不删）。不需要接管就把「接管插件商店」关掉。

**Q：备份在哪？会占空间吗？**
A：`data/plugin_data/kira_ops/backups/`。默认每目标保留 20 份、最长 7 天、总量 200MB，
自动清理；全都可以在面板调。

**Q：文件/命令呢？**
A：本插件**不重复实现**。文件读写、命令执行由 KiraAI 内置的 `agent` 插件负责；
本插件提供 `agent` 域来统一查看和修改它的访问策略（还能阻止把受保护路径加进白名单），
避免"两个地方配权限"的混乱。agent 插件没装/没启用时，`ops_status` 会明确提示，
`agent.get` 也会如实报告 `installed: false`。

**Q：`config.get` 为什么只给我一堆键名？**
A：不带 `path` 时只回顶层概况（键名/类型/规模）——整棵系统配置动辄几万字符，
会把上下文挤爆。要具体节点就传 `path`，例如 `bot_config.bot`。

**Q：`provider.models` 只给了模型 ID？**
A：默认只回 `{类型: [模型ID]}`（够用且省 token）；要每个模型的完整配置就传
`args={"full": true}`。`session.info` 的能力也是同理（默认只回 `{能力: 是否启用}`）。

**Q：`backup.list` 里没有路径？**
A：回滚只需要 `id`（`ops_action(domain=backup, action=restore, target=<id>)`），
写动作的返回里也会带 `回滚点: backups/<id>`；文件都在
`data/plugin_data/kira_ops/backups/<id>/`，路径可以省下来不占上下文。

**Q：我把 KiraAI 目录整份复制成新实例（KiraAI9 → KiraAI10），旧的回滚点还能用吗？**
A：**不会写坏老实例，这是关键**。复制过去的旧回滚点会被标成「⚠ 其它实例」，
点回滚会被明确拒绝（并告诉你是谁的回滚点）——因为它们记录的是老实例的绝对路径。
新版本产生的回滚点记录的是**相对路径**，复制到新实例后指向新实例自己的文件，可以正常用。
要清理这些外来回滚点，直接删掉 `data/plugin_data/kira_ops/backups/` 里对应的目录即可。

**Q：我在同一个目录里把 KiraAI 启动了两次会怎样？**
A：实测不会损坏文件（审计追加、快照目录都用原子占位），但两边会共用同一份 `data/`：
配置写入是"最后写入者胜"，且框架写配置文件是"先清空再写"，极端情况下另一个进程会读到空文件。
建议一份目录只跑一个实例（正常部署就是这样，各实例独立目录互不影响）。

**Q：`read_file` 读不了我的配置文件？**
A：故意的。它只读**日志文件**（`*.log` / `log.log*`）——曾经它能读 `data/` 下任意文件，
于是「读取打码」形同虚设（密钥、聊天记忆都能被读进模型）。要看配置用 `ops_read(domain=config)`，
看插件配置用 `plugin.config_get`，看技能正文用 `skill.content`。

**Q：权限里说的"读取打码"可靠吗？**
A：可靠，而且是**内置底线**：`api_key / authorization / bearer / cookie / secret / password /
credential / token / private_key` 这些字段名永远打码，就算把面板里的关键词表清空也照样生效。

**Q：`session.list` 报 `malformed_keys` 是怎么回事？**
A：说明 `data/memory/chat_memory.json` 里有格式不对的会话键（1.0.0 可能写进去过）。
它会让**框架自己的会话枚举**（含内置 session_tools 插件）报 `IndexError`。
本插件会给容错列表 + 报出脏键，按提示删掉即可修复：
`ops_action(domain=session, action=delete, target=<那个键>)`。

**Q：工具返回的是 JSON 吗？**
A：是。工具返回 `Payload`（dict 子类），框架把它字符串化时输出**紧凑 JSON**
（`{"ok":true,...}`）——比 Python dict 的 repr 更短，而且是合法 JSON。

**Q：日志读出来是截断的？**
A：单条日志默认截断到 400 字符（结尾标 `…(+N chars)`）。KiraAI 的日志里常有几 KB 的
JSON 单行，不截断一次就能吐 12 万字符（≈6 万 token）。要更多就配合 `keyword` 搜索。

**Q：怎么卸载？**
A：WebUI → 插件 → Kira Ops Console → 卸载。它不会动你的插件数据；
`data/plugin_data/kira_ops/` 可留作存档，也可手动删。

**Q：面板打不开/接口 404？**
A：确认插件「启用」，然后点一次「重载」。插件页面由框架用 no-store 提供，无需清缓存。

---

## 五、给开发者

### 新增一个能力域（改代码）

在 `caps/` 里加一个文件，继承 `Capability`（声明 `name` 和 `ACTIONS`），
在 `caps/__init__.py` 的 `load_all()` 里补一行 import 即可——**工具数量永远不变**（7 个）。

### 不碰核心代码：运行时注册能力域（第三方插件）

发一个自定义事件即可（`main.KiraOpsPlugin.register_capability` 会接）：

```python
from core.plugin import logger
from plugins.kira_ops.caps import Capability     # 或自行复制基类契约

class MyCap(Capability):
    name = "mycap"                                # 必须 ^[a-z][a-z0-9_]{0,31}$
    ACTIONS = {"ping": ("read", False, "示例只读动作")}

    def handle_read(self, action, params):
        return {"ok": True, "pong": True}
    async def handle_write(self, action, params):  # 有 write 动作时才需要
        return {"ok": True}

# 在你自己的插件里：
await self.ctx.emit_custom_event("kira_ops.register_capability", {"class": MyCap})
```

约束（不满足会被拒绝并记 warning，不会破坏工具面）：必须是 `Capability` 子类、
`name` 合法且未被占用、`ACTIONS` 非空且每项是 `(kind, dangerous, description)`、`kind ∈ {read, write}`。
注册后即可用 `ops_read(domain="mycap", action="ping")`，并自动受权限引擎与审计约束。

### 自测（共 **158** 项，全部离线可跑）

| 套件 | 项数 | 覆盖 |
| --- | --- | --- |
| `tests/test_kira_ops.py` | 25 | 权限引擎全链路、打码与写保护、确认令牌、备份快照/冲突/清理、导入冒烟、12 域注册表、`limit`/日志截断/brief 裁剪/能力名校验 |
| `tests/test_live_ops.py` | 14 | 桩上下文驱动 ops_status / ops_read 六域 / ops_config 拒密钥 / 高危令牌与会话绑定 / ops_panic / 审计落盘 / 商店搜索 |
| `tests/test_live_ops2.py` | 16 | provider 五条路径、persona 三条、mcp 三条、config 写入、agent 策略路径守卫、control 开关与令牌门 |
| `tests/test_integration_real.py` | 49 | **真实框架对象**加载插件并逐域驱动：12 域全部只读动作 + 更新后子模块必须是新代码 + 恶意压缩包必须被拒 + 全部修复点的反向断言 |
| `tests/test_panel_contract.py` | 9 | 面板静态契约：JS 可解析（node --check）、元素 id / 页签 / API 路径 / 配置路径 / 载荷字段交叉校验 |
| `tests/test_dm_confirm.py` | 20 | 密码确认授权：多会话扇出（群亦可）/ 任一批准 / 批准全链路 / standard 越档 / control 开关+越权 / 底线不越权 / **插件从不向聊天发言** / 安装升级出清单 / 旧默认迁移 / 面板脱敏（真实框架） |
| `tests/test_adversarial.py` | 13 | 对抗性审查：令牌越级/绑定、黑名单与全锁绝对性、密码不外泄、恶意配置与畸形入参、端到端链路 |
| `tests/test_gaps.py` | 6 | 覆盖盲区：plugin.config_set / provider.health / skill.set_scope / 面板 API |
| `tests/test_races.py` | 6 | 并发：双密码只执行一次、多请求一密码、热禁用、慢动作不阻塞、TTL 边界、池上限 |
| `tests/validate_schema.py` | — | schema 与 `core_version` 校验 |

```bash
python data/plugins/kira_ops/tests/test_panel_contract.py
python data/plugins/kira_ops/tests/test_kira_ops.py
python data/plugins/kira_ops/tests/test_live_ops.py
python data/plugins/kira_ops/tests/test_live_ops2.py
python data/plugins/kira_ops/tests/test_integration_real.py
python data/plugins/kira_ops/tests/validate_schema.py
```

> 前三套用的是桩件（FakeCtx/FakeMcpMgr/FakeProviderMgr…）。
> **桩件能证明逻辑，证明不了契约**——历史上有两个 bug（类当实例调用、更新后子模块陈旧）
> 正是靠"全是桩件"漏过去的，所以第 4 套必须用真实框架对象。

```
kira_ops/
  manifest.json      # plugin_id / core_version / repo / locales / tags
  schema.json        # 8 个 section：master / access / risk / control / protected / backup / audit / store
  main.py            # 7 工具 + 面板 API + 异常态 chat_env 注入 + 冲突接管 + 能力扩展事件
  caps/              # 能力域（plugin/skill/provider/mcp/session/persona/
                     #           config/log/store/agent/control/backup）
  core/              # 权限引擎 / 保护 / 备份 / 审计 / 确认令牌 / 路径守卫 / 打码
  store/             # 商店目录客户端（下载与解压复用框架官方安装器）
  web/               # WebUI 面板（中英双语，全设置热改）
  tests/             # 白盒 + 桩件 + 真实框架集成测试
  requirements.txt
```

---

## 六、版本与兼容

- 框架要求：`core_version >= 2.34.6`。缺能力时能自动降级（例如没有内置 `agent` 插件时，
  `agent` 域明确报告未安装而不是报错）。
- 版本：1.1.0 ｜ 作者：AiriLife-ai+znq19
- 仓库：https://github.com/znq19/KiraAI_ops_plugin
- 变更历史见 `CHANGELOG.md`，设计说明见 `kira_ops_设计方案_v0.5.md`。
