const state = {
  token: localStorage.getItem('museum_token'),
  user: JSON.parse(localStorage.getItem('museum_user') || 'null'),
  page: 'monitor',
  cases: [],
  anomalies: [],
  dashboard: {},
  selectedCase: null,
  selectedEvent: null,
  notice: '',
  rerunFilter: 'current'
};

const api = async (path, options = {}) => {
  const headers = { 'Content-Type': 'application/json', ...(options.headers || {}) };
  if (state.token) headers.Authorization = `Bearer ${state.token}`;
  const response = await fetch(`/api${path}`, { ...options, headers });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || '请求失败');
  return data;
};

const esc = (value) => String(value ?? '').replace(/[&<>'"]/g, (char) => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[char]));
const fmt = (value) => value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '—';
const label = (value) => ({active:'在岗', maintenance:'维护', retired:'停用', open:'待处置', triaged:'已接单', escalated:'已升级', resolved:'待关闭', closed:'已关闭', watch:'观察', high:'高', critical:'严重', door_interference:'门开干扰', environment_outlier:'环境离群', continuous_drift:'连续偏移', manual:'人工录入', seed:'演示基线', backfill:'离线补传', gateway_backfill:'网关补传', current:'当前版本', superseded:'已被重算取代', fresh:'重算新增', recalculated:'判定更新', revoked:'已撤销', merged:'异常合并', split:'异常拆分', escalation_added:'新增馆长升级', escalation_removed:'撤销馆长升级', type_change:'类型变化', unchanged:'判定一致', stale:'待重算', failed_pending:'重算失败待处理'}[value] || value);
const notify = (message) => { state.notice = message; render(); setTimeout(() => { state.notice = ''; render(); }, 3600); };

function render() {
  document.querySelector('#app').innerHTML = state.token ? shell() : login();
  bind();
}

function login() {
  return `<main class="login-screen">
    <section class="login-panel">
      <div class="login-mark">MCA / 1069</div>
      <p class="eyebrow">MUSEUM CASE ANOMALY</p>
      <h1>展柜环境异常<br><em>夜班值守台</em></h1>
      <p class="login-copy">把门磁上下文、环境采样与文物巡检放在同一条处置链上，让每一次异常都有来路、动作和恢复记录。</p>
      <form id="login-form" class="login-form">
        <label>值守账号<input name="username" placeholder="lin-operator" value="lin-operator" required></label>
        <label>通行口令<input name="password" type="password" placeholder="museum123" value="museum123" required></label>
        <button class="button primary" type="submit">进入夜班台 <span>↗</span></button>
      </form>
      <div class="login-hint">演示账号：lin-operator / guo-supervisor / viewer<br>统一口令 museum123</div>
    </section>
    <aside class="login-visual"><div class="visual-grid"></div><div class="visual-stamp">18:40<br><strong>展厅照明<br>已切换夜间模式</strong></div><div class="visual-line"></div></aside>
  </main>`;
}

function shell() {
  const title = { monitor: '展柜监测', anomalies: '异常事件', inspections: '文物巡检', case: '展柜档案' }[state.page];
  return `<div class="app-shell">
    <aside class="sidebar">
      <div class="brand"><span class="brand-dot"></span><div><strong>MCA</strong><small>夜班值守台</small></div></div>
      <div class="rail-note">馆藏环境 · 11 / 21<br><b>夜班窗口 18:00—07:00</b></div>
      <nav>
        ${nav('monitor','展柜监测','▧')}
        ${nav('anomalies','异常事件','◒')}
        ${nav('inspections','文物巡检','⊙')}
        ${nav('case','展柜档案','⌘')}
      </nav>
      <div class="sidebar-foot"><span class="pulse"></span>传感链路在线<div class="user-chip"><b>${esc(state.user?.display_name || '')}</b><small>${esc(state.user?.role || '')} · 末次登录刚刚</small></div><button id="logout" class="logout">退出夜班台</button></div>
    </aside>
    <main class="main-area">
      <header class="topbar"><div><p class="eyebrow">MUSEUM ENVIRONMENT CONTROL / 2026.11.21</p><h1>${title}</h1></div><div class="top-actions"><span class="clock">${new Date().toLocaleTimeString('zh-CN',{hour12:false})} <i>LOCAL</i></span><button class="icon-btn" id="refresh">↻</button></div></header>
      ${state.notice ? `<div class="notice">${esc(state.notice)}</div>` : ''}
      <section id="content"></section>
    </main>
  </div>`;
}
function nav(page, text, icon) { return `<button class="nav-item ${state.page===page?'active':''}" data-page="${page}"><span>${icon}</span>${text}<b>↗</b></button>`; }

async function loadData() {
  try {
    [state.dashboard, state.cases, state.anomalies] = await Promise.all([api('/dashboard'), api('/cases'), api('/anomalies')]);
    if (state.page === 'case' && !state.selectedCase && state.cases[0]) state.selectedCase = state.cases[0].id;
    if (state.page === 'anomalies' && !state.selectedEvent && state.anomalies[0]) state.selectedEvent = state.anomalies[0].id;
    renderContent();
  } catch (error) { notify(error.message); }
}

function renderContent() {
  const target = document.querySelector('#content');
  if (!target) return;
  if (state.page === 'monitor') target.innerHTML = monitorPage();
  if (state.page === 'anomalies') target.innerHTML = anomaliesPage();
  if (state.page === 'inspections') target.innerHTML = inspectionsPage();
  if (state.page === 'case') target.innerHTML = casePage();
  bindPage();
}

function monitorPage() {
  const cards = [
    ['在岗展柜', state.dashboard.cases || 0, '全部处于主动采样'],
    ['24h 环境采样', state.dashboard.samples_today || 0, '按展柜与时序留痕'],
    ['待处置异常', state.dashboard.open_events || 0, '值守动作尚未闭环'],
    ['馆长升级', state.dashboard.escalated_events || 0, '连续偏移需要复核']
  ];
  const pending = (state.dashboard.rerun_pending || 0) + (state.dashboard.stale_cases || 0);
  return `<div class="metric-row">${cards.map((card, i) => `<div class="metric ${i===2?'warm':''}"><span>${card[0]}</span><strong>${card[1]}</strong><small>${card[2]}</small></div>`).join('')}</div>
    <div class="rerun-banner ${pending ? 'pending' : ''}">
      <span class="rerun-dot"></span>
      补传与重算台账：迟到采样 <b>${state.dashboard.late_samples || 0}</b> 条 ·
      开放缺测 <b>${state.dashboard.open_gaps || 0}</b> 段 ·
      待重算/失败待处理 <b>${pending}</b> 个展柜
      ${state.dashboard.rerun_pending ? '<em>有重算失败任务保留旧版结果，请主管排查后重试</em>' : ''}
    </div>
    <div class="monitor-layout"><section class="panel cases-panel"><div class="panel-head"><div><p class="eyebrow">LIVE CASES / 01</p><h2>展厅里的四个呼吸点</h2></div><button class="text-button" data-page="case">查看档案 →</button></div><div class="case-list">${state.cases.map(caseRow).join('')}</div></section>
    <section class="panel side-panel"><div class="panel-head"><div><p class="eyebrow">NIGHT WATCH / 02</p><h2>值守提醒</h2></div></div><div class="watch-note"><div class="ring">${state.dashboard.door_interference || 0}</div><div><b>门开干扰</b><p>门磁开启段的突变只进入观察队列，关门复采后再判断设备状态。</p></div></div><div class="watch-rule"><span>算法链路</span><b>RobustScaler → IsolationForest</b><small>固定随机种子 1069 · 160 棵树 · 缺测断档不跨段计数</small></div><div class="watch-rule"><span>当前值守人</span><b>${esc(state.user?.display_name || '')}</b><small>角色 ${esc(state.user?.role || '')} · 审计实时写入</small></div></section></div>
    <section class="panel timeline-panel"><div class="panel-head"><div><p class="eyebrow">SHIFT TRACE / 03</p><h2>夜班交接线</h2></div><span class="status-dot">● 数据流动中</span></div><div class="timeline"><div><b>18:00</b><span>完成四柜夜间基线切换</span></div><div><b>18:20</b><span>MC-02 发现门开干扰，等待关门复采</span></div><div class="alert"><b>18:40</b><span>MC-03 连续偏移已升级馆长查看</span></div><div><b>19:00</b><span>文物巡检队列等待主管确认</span></div></div></section>`;
}
function caseRow(c) { const health = c.active_event_count ? 'attention' : 'steady'; const rerunTag = c.rerun_state && c.rerun_state !== 'current' ? `<small class="rerun-tag ${c.rerun_state}">${label(c.rerun_state)}</small>` : ''; return `<button class="case-row ${health}" data-case="${c.id}"><span class="case-code">${esc(c.code)}</span><span class="case-name"><b>${esc(c.artifact_title)}</b><small>${esc(c.gallery)} · ${esc(c.zone)} ${rerunTag}${c.open_gap_count ? `<small class="gap-tag">开放缺测 ${c.open_gap_count} 段</small>` : ''}${c.late_sample_count ? `<small class="late-tag">迟到采样 ${c.late_sample_count} 条</small>` : ''}</small></span><span class="case-meta"><b>${c.sample_count}</b><small>采样点</small></span><span class="case-meta"><b>${c.active_event_count}</b><small>未闭环</small></span><span class="case-state">${c.active_event_count ? '需要值守' : '稳态'} <i>↗</i></span></button>`; }

function anomaliesPage() {
  const selected = state.anomalies.find(item => item.id === state.selectedEvent) || state.anomalies[0];
  const supersededCount = state.anomalies.filter(item => item.revision_status === 'superseded').length;
  return `<div class="split-layout"><section class="panel anomaly-list"><div class="panel-head"><div><p class="eyebrow">EVENT QUEUE / ${state.anomalies.length.toString().padStart(2,'0')}</p><h2>异常事件队列</h2></div><span class="filter-pill">当前版本 · ${state.anomalies.length}</span></div>${supersededCount ? `<div class="superseded-note">另有 ${supersededCount} 条历史版本已被重算取代，可在详情查看血缘</div>` : ''}${state.anomalies.length ? state.anomalies.map(eventRow).join('') : empty('目前没有待处置异常', '继续保持采样，新的异常会进入这里。')}</section><section class="panel detail-panel">${selected ? eventDetail(selected) : empty('选择一个异常事件', '从左侧队列打开处置详情。')}</section></div>`;
}
function eventRow(e) { const frozen = e.status === 'closed'; const late = e.is_late ? '<small class="late-tag">迟到采样 · ' + esc(e.sample_source ? label(e.sample_source) : '') + '</small>' : ''; return `<button class="event-row ${state.selectedEvent===e.id?'selected':''}" data-event="${e.id}"><span class="severity ${e.severity}">${e.severity==='critical'?'!':'·'}</span><span><b>${esc(e.case_code)} · ${label(e.anomaly_type)}</b><small>${fmt(e.sampled_at)} · ${esc(e.reason)}</small>${late}${e.version_count ? `<small class="version-tag">重算版本 ×${e.version_count}</small>` : ''}</span><strong>${label(e.status)}${frozen ? '<small class="frozen-tag">已冻结</small>' : ''}</strong></button>`; }
function eventDetail(e) { return `<div class="detail-kicker"><span class="severity ${e.severity}">${e.severity}</span><span>${label(e.anomaly_type)}</span><span>${label(e.status)}</span>${e.revision_status === 'superseded' ? '<span class="kicker-warn">已被重算取代 · 只读留痕</span>' : ''}${e.status === 'closed' ? '<span class="kicker-frozen">已关闭 · 判定与巡检证据冻结</span>' : ''}</div><h2>${esc(e.case_code)} / ${esc(e.artifact_title)}</h2><p class="detail-copy">${esc(e.reason)}</p><div class="data-strip"><div><small>异常分数</small><b>${Number(e.score).toFixed(3)}</b></div><div><small>采样时间</small><b>${fmt(e.sampled_at)}</b></div><div><small>门磁</small><b>${e.door_open?'开启干扰':'关闭'}</b></div></div><div class="source-strip"><div><small>原始来源</small><b>${label(e.sample_source || 'manual')}${e.is_late ? ' · 迟到补传' : ''}</b></div><div><small>入库时间</small><b>${fmt(e.received_at)}</b></div><div><small>补传批次</small><b>${esc(e.backfill_batch || '—')}</b></div><div><small>重算版本</small><b>${e.version_count || 0} 次</b></div></div><div class="measure-grid"><div><span>温度</span><b>${e.temperature} °C</b></div><div><span>相对湿度</span><b>${e.humidity} %</b></div><div><span>CO₂</span><b>${e.co2_ppm} ppm</b></div><div><span>连续计数</span><b>${e.consecutive_count}${e.gap_affected ? ' · 断档边界' : ''}</b></div></div>${versionBlock(e)}<div class="action-box"><p class="eyebrow">DUTY ACTION / 值守处置</p><div class="action-buttons">${e.door_context?'<button class="button amber" data-action="door">完成关门复核</button>':'<button class="button" data-action="adjust">记录环境调整</button>'}<button class="button" data-action="inspect">安排文物巡检</button>${e.status==='escalated'?'<button class="button dark" data-action="curator">记录馆长处置</button>':''}<button class="button primary" data-action="close">关闭异常</button></div><small>关闭条件：已完成值守动作 + 文物巡检通过。升级事件需追加馆长处置记录。已关闭或被重算取代的事件只读。</small></div>`; }
function versionBlock(e) {
  if (!e.versions || !e.versions.length) return '';
  const rows = e.versions.slice().reverse().map(v => `<div class="version-row"><span class="change-pill ${v.change_type}">${label(v.change_type)}</span><b>${label(v.group_change || 'same')}</b><small>${fmt(v.created_at)} · 任务 #${v.algorithm_run_id || '—'}</small><p>${esc(v.change_note || '')}</p></div>`).join('');
  return `<div class="version-box"><p class="eyebrow">REBUILD LINEAGE / 重算前后血缘</p><div class="version-list">${rows}</div></div>`;
}

function inspectionsPage() {
  return `<div class="inspection-layout"><section class="panel inspection-intro"><p class="eyebrow">ARTIFACT CHECK / POST-INCIDENT</p><h2>巡检是异常闭环的最后一道门</h2><p>每一次环境异常都要留下文物状态证据。巡检通过后，主管才可以关闭异常。失败会把事件重新打开，并保留原因与操作者。</p><div class="inspection-mark">⊙</div></section><section class="panel inspection-table"><div class="panel-head"><div><p class="eyebrow">RECENT CHECKS</p><h2>最近巡检</h2></div><button class="text-button" data-page="anomalies">回到异常队列 →</button></div>${state.anomalies.map(e => `<div class="inspection-row"><span class="case-code">${esc(e.case_code)}</span><span><b>${esc(e.artifact_title)}</b><small>${label(e.anomaly_type)} · ${fmt(e.created_at)}</small></span><span class="result ${e.passed_checks ? 'pass' : 'pending'}">${e.passed_checks ? '已通过' : '待巡检'}</span><button class="text-button" data-event="${e.id}">打开 →</button></div>`).join('')}</section></div>`;
}
function casePage() { const current = state.cases.find(c => c.id === state.selectedCase) || state.cases[0]; return `<div class="case-layout"><section class="panel case-directory"><div class="panel-head"><div><p class="eyebrow">DISPLAY CASE REGISTER / 04</p><h2>展柜档案</h2></div><span class="filter-pill">${state.cases.length} 个在岗柜</span></div>${state.cases.map(c => `<button class="directory-item ${current?.id===c.id?'selected':''}" data-case="${c.id}"><span>${esc(c.code)}</span><b>${esc(c.artifact_title)}${c.rerun_state && c.rerun_state !== 'current' ? ` <em class="rerun-tag ${c.rerun_state}">${label(c.rerun_state)}</em>` : ''}</b><small>${esc(c.zone)}</small></button>`).join('')}</section><section class="panel case-detail">${current ? caseDetail(current) : empty('没有展柜档案', '先创建一个在岗展柜。')}</section></div>`; }
function caseDetail(c) { return `<div class="case-heading"><div><p class="eyebrow">${esc(c.gallery)} / ${esc(c.zone)}</p><h2>${esc(c.artifact_title)}</h2><span class="case-code">${esc(c.code)} · ${esc(c.artifact_code)}</span></div><span class="case-status">${label(c.status)}</span></div>${rerunStateBlock(c)}<div class="threshold-band"><div><small>温度窗口</small><b>${c.target_temp_min} — ${c.target_temp_max} °C</b></div><div><small>湿度窗口</small><b>${c.target_rh_min} — ${c.target_rh_max} %</b></div><div><small>门磁编号</small><b>${esc(c.door_sensor_id)}</b></div></div>${gapBlock(c)}${backfillBlock(c)}<div class="sample-table"><div class="table-title"><span>最近环境采样</span><small>${c.samples?.length || c.sample_count} 条记录 · 按采样时间排序</small></div>${(c.samples || []).slice(0,8).map(s => `<div class="sample-row"><span>${fmt(s.sampled_at)}</span><b>${s.temperature} °C</b><b>${s.humidity} %</b><b>${s.co2_ppm} ppm</b><span class="door-tag ${s.door_open?'open':''}">${s.door_open?'门开':'闭合'}</span><span class="source-tag ${s.is_late ? 'late' : ''}">${label(s.source || 'manual')}${s.is_late ? ' · 迟到' : ''}</span></div>`).join('')}</div><button class="button primary" id="analyze-case" data-case-id="${c.id}">重新运行异常识别 ↗</button></div>`; }
function rerunStateBlock(c) {
  if (!c.rerun_state || c.rerun_state === 'current') return '';
  if (c.rerun_state === 'failed_pending') {
    return `<div class="rerun-state failed"><b>重算失败待处理</b><p>旧版异常结果已保留。失败原因：${esc(c.last_rerun_error || '未知错误')}</p><button class="button dark" id="rerun-retry" data-case-id="${c.id}">发起重算重试</button></div>`;
  }
  return `<div class="rerun-state stale"><b>采样已变更，等待重算</b><p>补传采样已按采样时间入库，异常事件将由 worker 异步重建；已关闭事件保持原判定。</p></div>`;
}
function gapBlock(c) {
  if (!c.gaps || !c.gaps.length) return '<div class="gap-box empty-ledger"><p class="eyebrow">MEASUREMENT GAP / 缺测台账</p><small>没有超过 30 分钟的缺测断档，缺测不会被静默填成正常采样。</small></div>';
  const rows = c.gaps.map(g => `<div class="gap-row"><span>${fmt(g.previous_at)} → ${fmt(g.next_at)}</span><b>${Math.round(g.gap_seconds / 60)} 分钟</b><span class="gap-state ${g.state}">${g.state === 'open' ? '缺测待补' : '已补齐'}</span><small>${g.state === 'filled' ? '补齐批次 ' + esc(g.filled_batch || '—') : '发现批次 ' + esc(g.detected_batch || '—')}</small></div>`).join('');
  return `<div class="gap-box"><p class="eyebrow">MEASUREMENT GAP / 缺测断档台账（${c.gaps.length}）</p><div class="gap-list">${rows}</div></div>`;
}
function backfillBlock(c) {
  const batches = (c.backfill_batches || []).slice(0, 5);
  const batchRows = batches.map(b => `<div class="batch-row"><span>${esc(b.batch_ref)}</span><small>提交 ${b.sample_count} · 新增 ${b.inserted_count} · 重复 ${b.duplicate_count} · 缺测 ${b.gap_opened_count}/${b.gap_filled_count}</small><b>${b.rerun_queued || b.run_id ? '已排队重算' : '无需重算'}</b></div>`).join('');
  return `<div class="backfill-box"><p class="eyebrow">GATEWAY BACKFILL / 离线网关补传</p>
    <form id="backfill-form" data-case-id="${c.id}" class="backfill-form">
      <textarea name="samples" rows="4" placeholder='每行一条 JSON：{"sampled_at":"2026-11-21T19:00:00Z","temperature":21.0,"humidity":49.0,"co2_ppm":620,"door_open":false}' required></textarea>
      <input name="note" placeholder="补传说明（可选）" maxlength="500">
      <button class="button primary" type="submit">按采样时间补传入库</button>
    </form>
    <small class="backfill-hint">重复时间点不会新增采样；超过 30 分钟的间隔登记缺测并触发重算，缺测绝不填成正常数据。</small>
    ${batches.length ? `<div class="batch-list"><p class="eyebrow">BATCH LEDGER / 最近补传批次</p>${batchRows}</div>` : ''}
  </div>`;
}
function empty(title, copy) { return `<div class="empty"><div>○</div><h3>${title}</h3><p>${copy}</p></div>`; }

function bind() {
  document.querySelector('#login-form')?.addEventListener('submit', async (event) => { event.preventDefault(); const form = new FormData(event.currentTarget); try { const data = await api('/login', { method: 'POST', body: JSON.stringify({ username: form.get('username'), password: form.get('password') }) }); state.token = data.access_token; state.user = data.user; localStorage.setItem('museum_token', state.token); localStorage.setItem('museum_user', JSON.stringify(state.user)); render(); await loadData(); } catch (error) { alert(error.message); } });
  document.querySelectorAll('[data-page]').forEach(button => button.addEventListener('click', () => { state.page = button.dataset.page; render(); loadData(); }));
  document.querySelector('#logout')?.addEventListener('click', () => { state.token = null; state.user = null; localStorage.clear(); render(); });
  document.querySelector('#refresh')?.addEventListener('click', () => loadData());
  bindPage();
}
function bindPage() {
  document.querySelectorAll('[data-case]').forEach(button => button.addEventListener('click', async () => { state.selectedCase = Number(button.dataset.case); if (state.page === 'monitor') { state.page='case'; } render(); await loadData(); if (state.page === 'case') { const detail = await api(`/cases/${state.selectedCase}`); state.cases = state.cases.map(c => c.id===state.selectedCase ? {...c, ...detail} : c); renderContent(); } }));
  document.querySelectorAll('[data-event]').forEach(button => button.addEventListener('click', async () => { state.selectedEvent = Number(button.dataset.event); state.page='anomalies'; render(); await loadData(); await loadEventDetail(); }));
  document.querySelector('#analyze-case')?.addEventListener('click', async (event) => { try { const run = await api(`/analyze?case_id=${event.currentTarget.dataset.caseId}`, { method: 'POST' }); notify(`算法任务 #${run.id} 已入队，worker 正在处理`); pollRun(run.id); } catch (error) { notify(error.message); } });
  document.querySelector('#rerun-retry')?.addEventListener('click', async (event) => { try { const run = await api(`/cases/${event.currentTarget.dataset.caseId}/rerun-retry`, { method: 'POST' }); notify(`重试任务 #${run.id} 已入队，旧版结果在成功前保持不变`); } catch (error) { notify(error.message); } });
  document.querySelector('#backfill-form')?.addEventListener('submit', handleBackfill);
  document.querySelectorAll('[data-action]').forEach(button => button.addEventListener('click', () => handleAction(button.dataset.action)));
}

async function loadEventDetail() {
  if (!state.selectedEvent) return;
  try {
    const detail = await api(`/anomalies/${state.selectedEvent}`);
    state.anomalies = state.anomalies.map(item => item.id === state.selectedEvent ? { ...item, ...detail } : item);
    renderContent();
  } catch (error) { notify(error.message); }
}

async function handleBackfill(event) {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  const caseId = event.currentTarget.dataset.caseId;
  let samples;
  try {
    samples = form.get('samples').split('\n').map(line => line.trim()).filter(Boolean).map(line => JSON.parse(line));
  } catch (error) {
    notify('补传内容必须是每行一条合法 JSON');
    return;
  }
  try {
    const summary = await api(`/cases/${caseId}/backfill`, {
      method: 'POST',
      body: JSON.stringify({ samples, note: form.get('note') || '' }),
    });
    notify(`补传完成：新增 ${summary.inserted} 条，重复 ${summary.duplicates} 条，缺测 ${summary.gap_opened} 段${summary.rerun_queued ? '，重算已排队' : ''}`);
    await loadData();
    const detail = await api(`/cases/${caseId}`);
    state.cases = state.cases.map(c => c.id === Number(caseId) ? { ...c, ...detail } : c);
    renderContent();
  } catch (error) { notify(error.message); }
}
async function pollRun(id) { for (let i=0;i<15;i++) { await new Promise(r => setTimeout(r, 1000)); const run = await api(`/runs/${id}`); if (run.status === 'succeeded') { notify(`异常识别完成，发现 ${run.metrics.outlier_count} 个候选事件（重算统计 ${JSON.stringify(run.metrics.rebuild_stats || {})}）`); await loadData(); return; } if (run.status === 'failed') { notify(`重算失败，旧版结果已保留并标记待处理：${run.error}`); await loadData(); return; } } }
async function handleAction(action) { const e = state.anomalies.find(item => item.id === state.selectedEvent); if (!e) return; if (e.revision_status === 'superseded') { notify('该事件已被重算取代，证据已冻结，不能再追加处置'); return; } try { if (action === 'close') { await api(`/anomalies/${e.id}/close`, { method: 'POST' }); notify('异常已关闭，判定与巡检证据已冻结，后续重算不再修改'); } else if (action === 'inspect') { await api(`/anomalies/${e.id}/artifact-checks`, { method: 'POST', body: JSON.stringify({case_id:e.case_id,event_id:e.id,check_type:'post_incident',result:'pass',notes:'夜班巡检确认展柜玻璃、底座与文物状态正常'}) }); notify('文物巡检已记录为通过'); } else { const map = { door:'verify_door_sealed', adjust:'adjust_climate', curator:'call_curator' }; await api(`/anomalies/${e.id}/duty-actions`, { method:'POST', body: JSON.stringify({action_type:map[action], notes: action==='door'?'门已关闭，复采前完成门磁复核':'已按值守清单完成处置记录', status:'completed'}) }); notify('值守动作已写入审计'); } await loadData(); await loadEventDetail(); } catch (error) { notify(error.message); } }

render();
if (state.token) loadData();
