/* Kira Ops Console panel logic - vanilla JS, talks to the plugin API.
   All user-facing text lives in STR so the panel follows the KiraAI language
   (ctx.get_lang()) and can be switched from the header button. */
(function () {
  'use strict'

  var STR = {
    zh: {
      brand: 'Kira 运行自控台',
      reload: '重载面板',
      panic: '全锁/解锁',
      save: '保存全部设置（热生效）',
      saving: '保存中…',
      saved: '已保存并热生效',
      saveFailed: '保存失败: ',
      loadFailed: '加载失败: ',
      reloaded: '已重载',
      lang: 'EN',
      tabAccess: '访问控制', tabRisk: '风险分级', tabControl: '重启关机', tabProtected: '数据保护',
      tabBackup: '备份', tabAudit: '审计', tabStore: '商店',
      cardPlugins: '插件', cardSkills: '技能', cardProviders: 'Provider', cardMcp: 'MCP',
      cardSessions: '会话', cardLevel: '当前档位', cardPending: '待确认', cardBackups: '备份',
      cardConflicts: '被接管的商店插件', cardAgent: 'agent 插件',
      agentOn: '已启用', agentOff: '未启用', agentMissing: '未安装',
      legendAccess: '会话名单（允许名单留空 = 放行所有；黑名单优先）',
      legendMaster: '总闸',
      allow: '允许名单 allow_sessions', allowHint: '每行一个会话 ID，如 qq:gm:123456；留空=放行所有',
      deny: '禁止名单 deny_sessions', denyHint: '命中直接拒绝，优先级最高',
      readonly: '只读名单 readonly_sessions', readonlyHint: '名单内会话只能读，写操作一律拒绝',
      enabled: '启用插件', panicLock: '全锁（紧急只读）',
      legendRisk: '风险分级',
      level: '能力档位', levelHint: 'readonly=只读；standard=常规（默认，含安装/升级）；超档可密码授权；dangerous=高危名单+令牌；full=全开',
      highActions: '超档（高危）动作清单', highActionsHint: '每行一个动作键，如 plugin.uninstall；standard 下可经密码授权执行',
      highSessions: '高危会话名单（必填，不继承）',
      highSessionsHint: '只有名单内的会话能执行高危动作；留空 = 无人可执行（安全默认）',
      requireConfirm: '高危动作需确认令牌', confirmTtl: '确认令牌有效期（秒）', default300: '默认 300',
      legendDm: '密码确认授权（超档动作的批准通道）',
      dmEnabled: '启用密码确认授权',
      dmSession: '确认会话（可多个，任一批准即可）',
      dmSessionHint: '每行一个 adapter:dm:QQ号 或 adapter:gm:群号；同时发送，任一回复密码即批准。⚠️ 群里密码会以明文出现，建议私聊',
      dmPassword: '确认密码', dmPasswordHint: '在任一确认会话直接发此密码即批准；留空不变，永不回显原值',
      dmTplReq: '确认请求提示语模板', dmTplPh: '密码占位符（无待确认时）', dmTplAck: '批准回执模板', dmTplNotice: '原会话通知模板',
      dmTplHint: '占位符：请求 {sid} {uid} {action} {params} {ttl} {code}；回执/通知 {action} {result} {code}',
      dmStateReady: '状态：已就绪（超档动作触发时会向确认会话发授权请求）',
      dmStateOff: '状态：未启用', dmStateBad: '状态：已启用但未配齐（至少一个确认会话 + 密码）',
      legendControl: '重启与关机（默认双关，互不牵连）',
      allowRestart: '允许重启 KiraAI', allowShutdown: '允许关闭 KiraAI',
      controlHint: '仍需 full 档位 + 确认令牌',
      legendProtected: '数据保护（任何域都绕不过）',
      readMask: '读取打码字段关键词', writeDeny: '禁止写入字段关键词', writeAllow: '允许写入字段白名单',
      pathRead: '禁止读取路径', pathWrite: '禁止写入路径', pathDelete: '禁止删除路径',
      personaWrite: '允许修改人设',
      legendBackup: '备份与审计',
      backupEnabled: '启用自动备份', keepLast: '每目标保留份数', maxAge: '最长保留天数',
      maxTotal: '总大小上限（MB）', cleanupOnStart: '启动时清理旧备份',
      auditEnabled: '启用审计', auditReads: '记录读取操作', auditMaxAge: '审计保留天数',
      legendStore: '插件商店',
      takeover: '接管插件商店（互斥，检测到原插件即关闭它）',
      storeUrl: '商店数据源URL', ghProxy: 'GitHub 加速代理（留空自动测速）',
      timeout: '请求超时（秒）', cacheTtl: '列表缓存秒数', maxResults: '单次搜索结果上限',
      legendBackups: '自动备份（写动作前的快照）',
      thId: '回滚点', thCreated: '时间', thLabel: '目标', thFiles: '文件', thAction: '',
      rollback: '回滚', rollbackForce: '强制', noBackups: '暂无备份',
      foreign: '其它实例', foreignHint: '这个回滚点来自另一个 KiraAI 实例（随文件夹一起复制过来的），本实例不会去写别人的目录',
      confirmRestore: '确定要恢复 ', confirmRestore2: ' 吗？',
      restored: '已回滚', restoreFailed: '回滚失败: ', needForce: '目标已被外部改动，勾选“强制”再试',
      readBackupsFailed: '读取备份失败: ',
      legendAudit: '审计（最近记录）',
      thTime: '时间', thDomain: '域', thOp: '动作', thOk: '结果', thNote: '说明',
      noAudit: '暂无记录', readAuditFailed: '读取审计失败: ',
      panicOn: '已全锁（只读）', panicOff: '已解除全锁'
    },
    en: {
      brand: 'Kira Ops Console',
      reload: 'Reload',
      panic: 'Lock / Unlock',
      save: 'Save all settings (hot reload)',
      saving: 'Saving…',
      saved: 'Saved and applied',
      saveFailed: 'Save failed: ',
      loadFailed: 'Load failed: ',
      reloaded: 'Reloaded',
      lang: '中',
      tabAccess: 'Access', tabRisk: 'Risk', tabControl: 'Restart', tabProtected: 'Protection',
      tabBackup: 'Backups', tabAudit: 'Audit', tabStore: 'Store',
      cardPlugins: 'Plugins', cardSkills: 'Skills', cardProviders: 'Providers', cardMcp: 'MCP',
      cardSessions: 'Sessions', cardLevel: 'Level', cardPending: 'Pending', cardBackups: 'Backups',
      cardConflicts: 'Absorbed store plugin', cardAgent: 'agent plugin',
      agentOn: 'enabled', agentOff: 'disabled', agentMissing: 'not installed',
      legendAccess: 'Session lists (empty allow list = everyone; deny wins)',
      legendMaster: 'Master switch',
      allow: 'Allow list (allow_sessions)', allowHint: 'One session id per line, e.g. qq:gm:123456; empty = all',
      deny: 'Deny list (deny_sessions)', denyHint: 'Matched requests are refused first',
      readonly: 'Read-only list (readonly_sessions)', readonlyHint: 'These sessions may read but never write',
      enabled: 'Plugin enabled', panicLock: 'Panic lock (read-only)',
      legendRisk: 'Risk ladder',
      level: 'Capability level', levelHint: 'readonly / standard (default, incl. install+update) / over-level actions can be DM-approved / dangerous (list+token) / full',
      highActions: 'Over-level (high-risk) actions', highActionsHint: 'One action key per line; at standard these can be authorized via the DM password',
      highSessions: 'High-risk sessions (required, not inherited)',
      highSessionsHint: 'Only these sessions may run high-risk actions; empty = nobody (safe default)',
      requireConfirm: 'High-risk actions need a confirm token', confirmTtl: 'Confirm token TTL (seconds)',
      default300: 'default 300',
      legendDm: 'Password approval channel (for over-level actions)',
      dmEnabled: 'Enable password approval',
      dmSession: 'Confirm sessions (several allowed; first password wins)',
      dmSessionHint: 'one adapter:dm:<id> or adapter:gm:<id> per line; all are notified at once. Groups expose the password - prefer DMs',
      dmPassword: 'Approval password', dmPasswordHint: 'send this exact text in any confirm session to approve; blank keeps the old value, never echoed back',
      dmTplReq: 'Challenge message template', dmTplPh: 'Password placeholder (nothing pending)', dmTplAck: 'Approval ack template', dmTplNotice: 'Origin-session notice template',
      dmTplHint: 'placeholders: request {sid} {uid} {action} {params} {ttl} {code}; ack/notice {action} {result} {code}',
      dmStateReady: 'state: ready (a challenge is posted here for every high-risk request)',
      dmStateOff: 'state: disabled', dmStateBad: 'state: enabled but incomplete (needs a dm session + password)',
      legendControl: 'Restart and shutdown (both off by default, independent)',
      allowRestart: 'Allow restarting KiraAI', allowShutdown: 'Allow shutting down KiraAI',
      controlHint: 'also needs level=full + a confirm token',
      legendProtected: 'Data protection (applies to every domain)',
      readMask: 'Masked-on-read keywords', writeDeny: 'Write-denied keywords', writeAllow: 'Write allow list',
      pathRead: 'Read-denied paths', pathWrite: 'Write-denied paths', pathDelete: 'Delete-denied paths',
      personaWrite: 'Allow persona edits',
      legendBackup: 'Backups and audit',
      backupEnabled: 'Automatic backups', keepLast: 'Snapshots kept per target', maxAge: 'Max age (days)',
      maxTotal: 'Total size cap (MB)', cleanupOnStart: 'Clean up on startup',
      auditEnabled: 'Audit enabled', auditReads: 'Also record reads', auditMaxAge: 'Audit retention (days)',
      legendStore: 'Plugin store',
      takeover: 'Absorb the standalone store plugin (disable it, never uninstall)',
      storeUrl: 'Store feed URL', ghProxy: 'GitHub mirror (empty = auto speed test)',
      timeout: 'Request timeout (s)', cacheTtl: 'Listing cache (s)', maxResults: 'Max search results',
      legendBackups: 'Automatic backups (snapshot taken before every write)',
      thId: 'Rollback point', thCreated: 'Created', thLabel: 'Target', thFiles: 'Files', thAction: '',
      rollback: 'Restore', rollbackForce: 'force', noBackups: 'No backups yet',
      foreign: 'other instance', foreignHint: 'This rollback point was copied over from another KiraAI instance; this instance will not write outside its own tree',
      confirmRestore: 'Restore ', confirmRestore2: '?',
      restored: 'Restored', restoreFailed: 'Restore failed: ',
      needForce: 'Target changed outside kira_ops - tick "force" and retry',
      readBackupsFailed: 'Could not read backups: ',
      legendAudit: 'Audit (latest records)',
      thTime: 'Time', thDomain: 'Domain', thOp: 'Action', thOk: 'OK', thNote: 'Note',
      noAudit: 'No records yet', readAuditFailed: 'Could not read audit: ',
      panicOn: 'Panic lock engaged (read-only)', panicOff: 'Panic lock released'
    }
  }

  var lang = 'zh'
  var ctx = null
  var cfg = {}
  var warnings = []
  var overview = {}

  function t(key) {
    var table = STR[lang] || STR.zh
    if (table[key] !== undefined) return table[key]
    return STR.zh[key] !== undefined ? STR.zh[key] : key
  }

  function $(sel) { return document.querySelector(sel) }
  function el(tag, cls, html) {
    var n = document.createElement(tag)
    if (cls) n.className = cls
    if (html !== undefined) n.innerHTML = html
    return n
  }
  function token() {
    try { return localStorage.getItem('jwt_token') } catch (e) { return null }
  }
  function api(path, body) {
    var base = '/api/plugin/kira_ops' + path
    var headers = { 'Content-Type': 'application/json' }
    var tk = token()
    if (tk) headers['Authorization'] = 'Bearer ' + tk
    return fetch(base, {
      method: body === undefined ? 'GET' : 'POST',
      credentials: 'same-origin',
      cache: 'no-store',
      headers: headers,
      body: body === undefined ? undefined : JSON.stringify(body || {})
    }).then(function (r) {
      if (!r.ok) throw new Error('HTTP ' + r.status)
      return r.json()
    })
  }
  function toast(msg) {
    var box = $('#toast')
    box.textContent = msg
    box.classList.remove('hide')
    clearTimeout(toast._h)
    toast._h = setTimeout(function () { box.classList.add('hide') }, 2600)
  }

  // ---------------------------------------------------------------
  // form helpers: cfg is a nested object; inputs bind to a dotted path
  // ---------------------------------------------------------------

  function getPath(obj, path) {
    var parts = path.split('.')
    var cur = obj
    for (var i = 0; i < parts.length; i++) {
      if (cur == null || typeof cur !== 'object') return undefined
      cur = cur[parts[i]]
    }
    return cur
  }
  function setPath(obj, path, value) {
    var parts = path.split('.')
    var cur = obj
    for (var i = 0; i < parts.length - 1; i++) {
      if (typeof cur[parts[i]] !== 'object' || cur[parts[i]] === null) cur[parts[i]] = {}
      cur = cur[parts[i]]
    }
    cur[parts[parts.length - 1]] = value
  }

  function fieldText(path, label, hint, kind) {
    var wrap = el('label')
    wrap.appendChild(el('span', 'k', label))
    var input = el('input', '', '')
    input.type = kind === 'number' ? 'number' : 'text'
    var v = getPath(cfg, path)
    input.value = v == null ? '' : v
    input.dataset.path = path
    input.dataset.kind = kind || 'text'
    wrap.appendChild(input)
    if (hint) wrap.appendChild(el('span', 'hint', hint))
    return wrap
  }

  function fieldPassword(path, label, hint) {
    // write-only: the backend masks the stored value with '***', which is
    // rendered as a placeholder; submitting empty/'***' keeps the old value
    var wrap = el('label')
    wrap.appendChild(el('span', 'k', label))
    var input = el('input', '', '')
    input.type = 'password'
    input.autocomplete = 'new-password'
    var v = getPath(cfg, path)
    if (v) input.placeholder = v === '***' ? '(set - hidden)' : '(set)'
    input.dataset.path = path
    input.dataset.kind = 'password'
    wrap.appendChild(input)
    if (hint) wrap.appendChild(el('span', 'hint', hint))
    return wrap
  }

  function fieldArea(path, label, hint) {
    var wrap = el('label')
    wrap.appendChild(el('span', 'k', label))
    var input = el('textarea', '', '')
    input.rows = 4
    var v = getPath(cfg, path)
    input.value = v == null ? '' : v
    input.dataset.path = path
    input.dataset.kind = 'text'
    wrap.appendChild(input)
    if (hint) wrap.appendChild(el('span', 'hint', hint))
    return wrap
  }

  function fieldSwitch(path, label, hint) {
    var wrap = el('label', 'switch')
    var input = el('input', '', '')
    input.type = 'checkbox'
    input.checked = !!getPath(cfg, path)
    input.dataset.path = path
    input.dataset.kind = 'switch'
    wrap.appendChild(input)
    wrap.appendChild(el('span', 'k', label))
    if (hint) wrap.appendChild(el('span', 'hint', hint))
    return wrap
  }

  function fieldList(path, label, hint) {
    var wrap = el('label')
    wrap.appendChild(el('span', 'k', label))
    var ta = el('textarea', '', '')
    var v = getPath(cfg, path) || []
    ta.value = Array.isArray(v) ? v.join('\n') : String(v)
    ta.dataset.path = path
    ta.dataset.kind = 'list'
    wrap.appendChild(ta)
    if (hint) wrap.appendChild(el('span', 'hint', hint))
    return wrap
  }

  function fieldSelect(path, label, options, hint) {
    var wrap = el('label')
    wrap.appendChild(el('span', 'k', label))
    var sel = el('select', '', '')
    options.forEach(function (o) {
      var op = el('option', '', o)
      op.value = o
      sel.appendChild(op)
    })
    sel.value = getPath(cfg, path) || options[0]
    sel.dataset.path = path
    sel.dataset.kind = 'select'
    wrap.appendChild(sel)
    if (hint) wrap.appendChild(el('span', 'hint', hint))
    return wrap
  }

  function collect() {
    var patch = {}
    document.querySelectorAll('[data-path]').forEach(function (n) {
      var path = n.dataset.path
      var kind = n.dataset.kind
      var value
      var usable = true
      if (kind === 'switch') value = !!n.checked
      else if (kind === 'number') {
        // an empty or non-numeric box means "leave the setting alone": writing 0
        // there would silently reset it to the default
        var num = Number(n.value)
        if (n.value === '' || !isFinite(num)) usable = false
        else value = num
      }
      else if (kind === 'list') value = n.value.split('\n').map(function (s) { return s.trim() }).filter(Boolean)
      else if (kind === 'password') {
        // empty or the mask placeholder means "leave the stored password alone"
        if (!n.value || n.value === '***') usable = false
        else value = n.value
      }
      else value = n.value
      if (usable) setPath(patch, path, value)
    })
    return patch
  }

  // ---------------------------------------------------------------
  // render
  // ---------------------------------------------------------------

  function renderChrome() {
    document.documentElement.lang = lang === 'zh' ? 'zh-CN' : 'en'
    document.title = t('brand')
    $('#brandText').textContent = t('brand')
    $('#btnReload').textContent = t('reload')
    $('#btnPanic').textContent = t('panic')
    $('#btnLang').textContent = t('lang')
    $('#btnSave').textContent = t('save')
    var labels = [t('tabAccess'), t('tabRisk'), t('tabControl'), t('tabProtected'),
                  t('tabBackup'), t('tabAudit'), t('tabStore')]
    document.querySelectorAll('.tab').forEach(function (tab, i) {
      if (labels[i]) tab.textContent = labels[i]
    })
  }

  function renderWarnings() {
    var box = $('#warnings')
    if (!warnings || !warnings.length) { box.classList.add('hide'); return }
    box.classList.remove('hide')
    box.innerHTML = warnings.map(function (w) { return '⚠ ' + w }).join('<br>')
  }

  function renderCards() {
    var box = $('#cards')
    box.innerHTML = ''
    var counts = overview.counts || {}
    var perm = overview.permission || {}
    var agent = overview.agent || {}
    var agentText = !agent.installed ? t('agentMissing') : (agent.enabled ? t('agentOn') : t('agentOff'))
    var rows = [
      [t('cardPlugins'), (counts.plugins_enabled || 0) + '/' + (counts.plugins || 0)],
      [t('cardSkills'), counts.skills || 0],
      [t('cardProviders'), counts.providers || 0],
      [t('cardMcp'), counts.mcp || 0],
      [t('cardSessions'), counts.sessions || 0],
      [t('cardLevel'), perm.level || '-'],
      [t('cardPending'), counts.pending_confirms || 0],
      [t('cardBackups'), counts.backups || 0],
      [t('cardAgent'), agentText]
    ]
    rows.forEach(function (r) {
      var c = el('div', 'card')
      c.appendChild(el('b', '', String(r[1])))
      c.appendChild(el('span', '', r[0]))
      box.appendChild(c)
    })
    if (overview.conflicts && overview.conflicts.length) {
      var c2 = el('div', 'card')
      c2.appendChild(el('b', '', String(overview.conflicts.length)))
      c2.appendChild(el('span', '', t('cardConflicts')))
      box.appendChild(c2)
    }
  }

  function renderAccess() {
    var pane = $('#pane-access')
    pane.innerHTML = ''
    var fs = el('fieldset')
    fs.appendChild(el('legend', '', t('legendAccess')))
    fs.appendChild(fieldList('access.allow_sessions', t('allow'), t('allowHint')))
    fs.appendChild(fieldList('access.deny_sessions', t('deny'), t('denyHint')))
    fs.appendChild(fieldList('access.readonly_sessions', t('readonly'), t('readonlyHint')))
    pane.appendChild(fs)

    var fs2 = el('fieldset')
    fs2.appendChild(el('legend', '', t('legendMaster')))
    fs2.appendChild(fieldSwitch('master.enabled', t('enabled')))
    fs2.appendChild(fieldSwitch('master.panic_lock', t('panicLock')))
    pane.appendChild(fs2)
  }

  function renderRisk() {
    var pane = $('#pane-risk')
    pane.innerHTML = ''
    var fs = el('fieldset')
    fs.appendChild(el('legend', '', t('legendRisk')))
    fs.appendChild(fieldSelect('risk.level', t('level'),
      ['readonly', 'standard', 'dangerous', 'full'], t('levelHint')))
    fs.appendChild(fieldList('risk.high_risk_actions', t('highActions'), t('highActionsHint')))
    fs.appendChild(fieldList('risk.high_risk_sessions', t('highSessions'), t('highSessionsHint')))
    fs.appendChild(fieldSwitch('risk.require_confirm', t('requireConfirm')))
    fs.appendChild(fieldText('risk.confirm_ttl', t('confirmTtl'), t('default300'), 'number'))
    pane.appendChild(fs)

    var st = (overview && overview.confirm) || {}
    var stateText = st.ready ? t('dmStateReady') : (st.enabled ? t('dmStateBad') : t('dmStateOff'))
    var fs2 = el('fieldset')
    fs2.appendChild(el('legend', '', t('legendDm')))
    var state = el('div', 'muted', stateText)
    fs2.appendChild(state)
    fs2.appendChild(fieldSwitch('confirm.enabled', t('dmEnabled')))
    fs2.appendChild(fieldList('confirm.sessions', t('dmSession'), t('dmSessionHint')))
    fs2.appendChild(fieldPassword('confirm.password', t('dmPassword'), t('dmPasswordHint')))
    fs2.appendChild(fieldArea('confirm.template_request', t('dmTplReq'), t('dmTplHint')))
    fs2.appendChild(fieldText('confirm.template_placeholder', t('dmTplPh'), t('dmTplHint')))
    fs2.appendChild(fieldArea('confirm.template_approved', t('dmTplAck'), t('dmTplHint')))
    fs2.appendChild(fieldArea('confirm.template_notice', t('dmTplNotice'), t('dmTplHint')))
    pane.appendChild(fs2)
  }

  function renderControl() {
    var pane = $('#pane-control')
    pane.innerHTML = ''
    var fs = el('fieldset')
    fs.appendChild(el('legend', '', t('legendControl')))
    fs.appendChild(fieldSwitch('control.allow_restart', t('allowRestart'), t('controlHint')))
    fs.appendChild(fieldSwitch('control.allow_shutdown', t('allowShutdown'), t('controlHint')))
    pane.appendChild(fs)
  }

  function renderProtected() {
    var pane = $('#pane-protected')
    pane.innerHTML = ''
    var fs = el('fieldset')
    fs.appendChild(el('legend', '', t('legendProtected')))
    fs.appendChild(fieldList('protected.read_mask', t('readMask')))
    fs.appendChild(fieldList('protected.write_deny', t('writeDeny')))
    fs.appendChild(fieldList('protected.write_allow', t('writeAllow')))
    fs.appendChild(fieldList('protected.path_deny_read', t('pathRead')))
    fs.appendChild(fieldList('protected.path_deny_write', t('pathWrite')))
    fs.appendChild(fieldList('protected.path_deny_delete', t('pathDelete')))
    fs.appendChild(fieldSwitch('protected.persona_write', t('personaWrite')))
    pane.appendChild(fs)

    var fs2 = el('fieldset')
    fs2.appendChild(el('legend', '', t('legendBackup')))
    fs2.appendChild(fieldSwitch('backup.enabled', t('backupEnabled')))
    fs2.appendChild(fieldText('backup.keep_last', t('keepLast'), '', 'number'))
    fs2.appendChild(fieldText('backup.max_age_days', t('maxAge'), '', 'number'))
    fs2.appendChild(fieldText('backup.max_total_mb', t('maxTotal'), '', 'number'))
    fs2.appendChild(fieldSwitch('backup.cleanup_on_start', t('cleanupOnStart')))
    fs2.appendChild(fieldSwitch('audit.enabled', t('auditEnabled')))
    fs2.appendChild(fieldSwitch('audit.audit_reads', t('auditReads')))
    fs2.appendChild(fieldText('audit.max_age_days', t('auditMaxAge'), '', 'number'))
    pane.appendChild(fs2)
  }

  function renderStore() {
    var pane = $('#pane-store')
    pane.innerHTML = ''
    var fs = el('fieldset')
    fs.appendChild(el('legend', '', t('legendStore')))
    fs.appendChild(fieldSwitch('store.takeover_store', t('takeover')))
    fs.appendChild(fieldText('store.store_url', t('storeUrl')))
    fs.appendChild(fieldText('store.github_proxy', t('ghProxy')))
    fs.appendChild(fieldText('store.request_timeout', t('timeout'), '', 'number'))
    fs.appendChild(fieldText('store.cache_ttl', t('cacheTtl'), '', 'number'))
    fs.appendChild(fieldText('store.max_results', t('maxResults'), '', 'number'))
    pane.appendChild(fs)
  }

  function renderBackups() {
    var pane = $('#pane-backup')
    pane.innerHTML = ''
    var fs = el('fieldset')
    fs.appendChild(el('legend', '', t('legendBackups')))
    var table = el('table')
    table.innerHTML = '<thead><tr><th>' + t('thId') + '</th><th>' + t('thCreated') + '</th><th>' +
      t('thLabel') + '</th><th>' + t('thFiles') + '</th><th>' + t('thAction') + '</th></tr></thead>'
    var tbody = el('tbody')
    table.appendChild(tbody)
    fs.appendChild(table)
    pane.appendChild(fs)
    api('/backups').then(function (res) {
      (res.items || []).forEach(function (b) {
        var tr = el('tr')
        var label = (b.label || '') + (b.foreign ? ' ⚠ ' + t('foreign') : '')
        tr.innerHTML = '<td>' + b.id + '</td><td>' + (b.created || '') + '</td><td>' +
          label + '</td><td>' + (b.files || 0) + '</td>'
        if (b.foreign) tr.title = t('foreignHint')
        var td = el('td')
        var forceWrap = el('label', 'inline')
        var force = el('input', '', '')
        force.type = 'checkbox'
        forceWrap.appendChild(force)
        forceWrap.appendChild(el('span', '', t('rollbackForce')))
        var btn = el('button', 'ghost', t('rollback'))
        btn.onclick = function () {
          if (!confirm(t('confirmRestore') + b.id + t('confirmRestore2'))) return
          api('/backups/restore', { id: b.id, force: !!force.checked }).then(function (r) {
            if (r.ok) { toast(t('restored')); return }
            if (r.need_force) { toast(t('needForce')); return }
            var detail = r.error || (r.errors || []).join('; ') ||
              (r.foreign || []).join('; ') || r.hint || ''
            toast(t('restoreFailed') + detail)
          })
        }
        td.appendChild(forceWrap)
        td.appendChild(btn)
        tr.appendChild(td)
        tbody.appendChild(tr)
      })
      if (!tbody.children.length) {
        tbody.innerHTML = '<tr><td colspan="5" class="muted">' + t('noBackups') + '</td></tr>'
      }
    }).catch(function (e) { toast(t('readBackupsFailed') + e.message) })
  }

  function renderAudit() {
    var pane = $('#pane-audit')
    pane.innerHTML = ''
    var fs = el('fieldset')
    fs.appendChild(el('legend', '', t('legendAudit')))
    var table = el('table')
    table.innerHTML = '<thead><tr><th>' + t('thTime') + '</th><th>' + t('thDomain') + '</th><th>' +
      t('thOp') + '</th><th>' + t('thOk') + '</th><th>' + t('thNote') + '</th></tr></thead>'
    var tbody = el('tbody')
    table.appendChild(tbody)
    fs.appendChild(table)
    pane.appendChild(fs)
    api('/audit?limit=50').then(function (res) {
      (res.items || []).slice().reverse().forEach(function (r) {
        var tr = el('tr')
        tr.innerHTML = '<td>' + (r.ts || '') + '</td><td>' + (r.domain || '') + '</td><td>' +
          (r.action || '') + '</td><td>' + (r.ok ? '✓' : '✗') + '</td><td>' +
          ((r.err || r.note || '')).toString().slice(0, 60) + '</td>'
        tbody.appendChild(tr)
      })
      if (!tbody.children.length) {
        tbody.innerHTML = '<tr><td colspan="5" class="muted">' + t('noAudit') + '</td></tr>'
      }
    }).catch(function (e) { toast(t('readAuditFailed') + e.message) })
  }

  function render() {
    renderChrome()
    renderWarnings()
    renderCards()
    renderAccess()
    renderRisk()
    renderControl()
    renderProtected()
    renderStore()
    renderBackups()
    renderAudit()
  }

  async function load() {
    overview = await api('/overview')
    if (overview.lang) lang = overview.lang
    var conf = await api('/config')
    cfg = conf.config || {}
    // /config and /overview both carry the standing warnings - show each once
    var seen = {}
    warnings = (conf.warnings || []).concat(overview.warnings || []).filter(function (w) {
      if (seen[w]) return false
      seen[w] = true
      return true
    })
    $('#ver').textContent = overview.version ? ('v' + overview.version) : ''
    render()
  }

  function save() {
    var patch = collect()
    $('#btnSave').textContent = t('saving')
    api('/config', { config: patch }).then(function (r) {
      $('#btnSave').textContent = t('save')
      if (r.ok) {
        toast(t('saved'))
        cfg = r.config || cfg
        load().catch(function () { /* keep the toast, the next reload will retry */ })
      } else {
        toast(t('saveFailed') + (r.error || ''))
      }
    }).catch(function (e) {
      $('#btnSave').textContent = t('save')
      toast(t('saveFailed') + e.message)
    })
  }

  function panicToggle() {
    var locked = !!getPath(cfg, 'master.panic_lock')
    api('/panic', { lock: !locked }).then(function (r) {
      if (r.ok) {
        toast(r.panic_lock ? t('panicOn') : t('panicOff'))
        setPath(cfg, 'master.panic_lock', r.panic_lock)
        load()
      }
    })
  }

  document.addEventListener('DOMContentLoaded', function () {
    document.querySelectorAll('.tab').forEach(function (tab) {
      tab.onclick = function () {
        document.querySelectorAll('.tab').forEach(function (x) { x.classList.remove('active') })
        document.querySelectorAll('.pane').forEach(function (p) { p.classList.remove('active') })
        tab.classList.add('active')
        var pane = $('#pane-' + tab.dataset.tab)
        if (pane) pane.classList.add('active')
      }
    })
    $('#btnSave').onclick = save
    $('#btnReload').onclick = function () { load().then(function () { toast(t('reloaded')) }) }
    $('#btnPanic').onclick = panicToggle
    $('#btnLang').onclick = function () { lang = (lang === 'zh' ? 'en' : 'zh'); render() }
    load().catch(function (e) { toast(t('loadFailed') + e.message) })
  })
})()
