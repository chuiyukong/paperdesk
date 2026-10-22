'use strict';
const BUILD_VERSION='1.4.1';

const stages = {planned:'计划中',writing:'正在写',submitted:'已投稿',revision:'退修中',accepted:'已录用/发表',archived:'归档'};
const results = {accepted:'录用',rejected:'退稿',withdrawn:'撤稿',other:'其他结束'};
const $ = id => document.getElementById(id);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const icon = (name,className='') => `<svg class="icon ${className}" aria-hidden="true"><use href="/icons.svg#${name}"></use></svg>`;
let state = null;
let currentView = 'papers';
let filters = {search:'',stage:'',journal:'',overdue:false,sort:'recent'};
let drawerInfo = null;
let drawerDirty = false;
let drawerGeneration = 0;
let saving = false;
let previousFocus = null;
let modalResolve = null;
let toastTimer;
let disconnected = false;
let refreshing = false;
let versionMismatch=false;
let lastWriteWarning=null;
let drawerRequestGeneration=0;

async function api(path, method='GET', data, operationId) {
  const headers = {};
  if (method !== 'GET') {
    headers['Content-Type'] = 'application/json';
    headers['X-PaperDesk-Token'] = state?.token || '';
    headers['X-PaperDesk-Client']=BUILD_VERSION;
    if(operationId)headers['X-PaperDesk-Operation']=operationId;
  }
  let response;
  try {response = await fetch(path,{method,headers,body:data === undefined ? undefined : JSON.stringify(data)});}
  catch {
    if(operationId){try{const check=await fetch(`/api/operations/${encodeURIComponent(operationId)}`);const receipt=await check.json();if(check.ok&&receipt.found){lastWriteWarning=receipt.backup_warning;return {ok:true,...receipt.result};}}catch{}}
    throw new Error('暂时连接不到本机平台。保存结果尚未确认，请保持表单并重试；同一次保存不会重复新建。');
  }
  const payload = await response.json();
  if (!response.ok) {const error=new Error(payload.error || '操作没有完成，请重试。');error.code=payload.code;throw error;}
  if(method!=='GET')lastWriteWarning=payload.backup_warning||null;
  return payload;
}

function toast(message,error=false) {
  clearTimeout(toastTimer);
  $('toast').innerHTML = `<span>${esc(message)}</span>${error?'<button class="toast-close" data-action="dismiss-toast" aria-label="关闭提示">×</button>':''}`;
  $('toast').setAttribute('role',error?'alert':'status');
  $('toast').className = 'toast' + (error?' error':'');
  $('toast').hidden = false;
  if(!error)toastTimer = setTimeout(() => {$('toast').hidden=true;},4200);
}

async function refresh(render=true) {
  state = await api('/api/state');
  versionMismatch=state.app_version!==BUILD_VERSION;
  disconnected = false;
  const [year,month,day] = state.today.split('-');
  $('today').textContent = `${year}年${Number(month)}月${Number(day)}日`;
  document.querySelector('footer').textContent = `PaperDesk v${state.app_version || '1.0.0'} · 记录保存在本机，稿件保留在原文件夹`;
  if (render) renderView();
}

function stagePill(stage) {return `<span class="stage ${esc(stage)}">${esc(stages[stage])}</span>`;}
function journalAnchor(journal, label) {
  if (journal.url) return `<a class="journal-link" href="${esc(journal.url)}" target="_blank" rel="noopener noreferrer">${esc(label || journal.name)} <span aria-hidden="true">↗</span></a>`;
  return `<span class="journal-link">${esc(label || journal.name)}</span><button class="link-button subtle" data-action="edit-journal" data-id="${journal.id}">设置投稿入口</button>`;
}
function header(title,subtitle,action) {return `<header class="page-header"><div><h1>${title}</h1><p>${subtitle}</p></div>${action || ''}</header>`;}
function renderView() {
  if(versionMismatch){$('content').innerHTML=`<div class="connection-error"><h2>请重新启动平台完成更新</h2><p>页面已更新为 v${BUILD_VERSION}，当前服务仍是 v${esc(state.app_version)}。<br>请点击左侧“退出平台”，再双击启动入口。已保存记录保留在本机。</p></div>`;return;}
  document.querySelectorAll('[data-view]').forEach(b=>{b.classList.toggle('active',b.dataset.view===currentView);if(b.dataset.view===currentView)b.setAttribute('aria-current','page');else b.removeAttribute('aria-current');});
  if (currentView==='journals') renderJournals();
  else if (currentView==='backup') renderBackup().catch(error=>toast(error.message,true));
  else renderPapers();
}

function renderPapers() {
  $('content').innerHTML = header('论文总览','记录进度、投稿与每一轮等待。',`<button class="button primary" data-action="new-paper"><span aria-hidden="true">＋</span>新建论文</button>`) +
  `<div class="stats">${Object.entries(stages).map(([key,label])=>`<button class="stat ${key} ${filters.stage===key?'selected':''}" data-action="filter-stage" data-stage="${key}" aria-label="查看${label}论文" aria-pressed="${filters.stage===key}"><span class="stat-name"><span class="stat-marker">${icon(key,'stat-icon')}</span>${label}</span><strong>${state.counts[key]}<small>篇</small></strong></button>`).join('')}</div>` +
  (state.backup_warning?`<div class="save-warning">${esc(state.backup_warning)}<button class="link-button" data-view="backup">查看备份</button></div>`:'')+
  (state.overdue_count?`<div class="overdue-strip"><span aria-hidden="true">◷</span><span>${state.overdue_count} 篇论文超过计划转投时间，可以继续等待或调整计划。</span><button class="link-button" data-action="show-overdue">查看记录 →</button></div>`:'') +
  `<section class="panel"><div class="panel-title"><h2>我的论文</h2><span id="paper-total"></span></div>
  <div class="toolbar"><div class="searchbox">${icon('search','search-icon')}<input id="search" type="search" placeholder="搜索稿件编号、题目、作者或标签" value="${esc(filters.search)}" aria-label="搜索论文"></div>
  <select id="stage-filter" aria-label="按阶段筛选"><option value="">全部阶段</option>${Object.entries(stages).map(([k,v])=>`<option value="${k}" ${filters.stage===k?'selected':''}>${v}</option>`).join('')}</select>
  <select id="journal-filter" aria-label="按期刊筛选"><option value="">全部期刊</option>${state.journals.map(j=>`<option value="${j.id}" ${filters.journal===String(j.id)?'selected':''}>${esc(j.name)}</option>`).join('')}</select>
  <label class="filter-toggle"><input type="checkbox" id="overdue-filter" ${filters.overdue?'checked':''}>仅超期</label></div>
  <div id="active-filters" class="active-filters" hidden></div><div id="paper-table"></div><div class="table-footer"><span id="result-count"></span><label>排序：<select id="sort" aria-label="论文排序"><option value="recent">最近更新</option><option value="wait">${filters.stage==='writing'?'写作天数最多':filters.stage==='planned'?'计划历时最多':'投稿等待最多'}</option><option value="due">计划转投日期</option><option value="code">稿件编号</option><option value="title">论文标题</option></select></label></div></section>`;
  $('search').addEventListener('input',e=>{filters.search=e.target.value;renderPaperTable();});
  $('stage-filter').addEventListener('change',e=>{filters.stage=e.target.value;renderPapers();$('stage-filter').focus();});
  $('journal-filter').addEventListener('change',e=>{filters.journal=e.target.value;renderPaperTable();});
  $('overdue-filter').addEventListener('change',e=>{filters.overdue=e.target.checked;renderPaperTable();});
  $('sort').value=filters.sort;
  $('sort').addEventListener('change',e=>{filters.sort=e.target.value;renderPaperTable();});
  renderPaperTable();
}

function syncStats() {document.querySelectorAll('.stat').forEach(b=>{const selected=b.dataset.stage===filters.stage;b.classList.toggle('selected',selected);b.setAttribute('aria-pressed',String(selected));});}
function earlyStage(p) {return p.stage==='planned'||p.stage==='writing';}
function listedJournal(p) {
  if(earlyStage(p))return p.target_journal;
  const r=p.current_submission;
  return r?{id:r.journal_id,name:r.journal_name,url:r.journal_url}:null;
}
function renderPaperTable() {
  const activeFilters=$('active-filters');
  if(activeFilters){const labels=[filters.stage&&stages[filters.stage],filters.journal&&state.journals?.find(j=>String(j.id)===filters.journal)?.name,filters.search&&`搜索：${filters.search}`,filters.overdue&&'仅超期'].filter(Boolean);activeFilters.hidden=!labels.length;activeFilters.innerHTML=labels.map(label=>`<span>${esc(label)}</span>`).join('')+'<button class="link-button" data-action="clear-filters">清除筛选</button>';}

  const query=filters.search.trim().normalize('NFKC').toLocaleLowerCase();
  const papers=state.papers.filter(p=>
    (!query || [p.paper_code,p.title,p.authors,p.tags,...(p.journal_numbers||[])].join(' ').normalize('NFKC').toLocaleLowerCase().includes(query)) &&
    (!filters.stage || p.stage===filters.stage) &&
    (!filters.journal || listedJournal(p)?.id===Number(filters.journal)) &&
    (!filters.overdue || p.overdue_days>0));
  if(filters.sort==='wait')papers.sort((a,b)=>(earlyStage(b)?b.stage_elapsed_days??-1:b.current_submission?.elapsed_days??-1)-(earlyStage(a)?a.stage_elapsed_days??-1:a.current_submission?.elapsed_days??-1));
  else if(filters.sort==='due')papers.sort((a,b)=>(earlyStage(a)?'9999':a.current_submission?.ended_on?'9999':a.current_submission?.target_on||'9999').localeCompare(earlyStage(b)?'9999':b.current_submission?.ended_on?'9999':b.current_submission?.target_on||'9999'));
  else if(filters.sort==='code')papers.sort((a,b)=>a.paper_code.localeCompare(b.paper_code,'zh-CN',{numeric:true}));
  else if(filters.sort==='title')papers.sort((a,b)=>a.title.localeCompare(b.title,'zh-CN'));
  $('paper-total').textContent=`共 ${state.papers.length} 篇`;
  $('result-count').textContent=`显示 ${papers.length} / ${state.papers.length} 篇论文`;
  if(!papers.length){$('paper-table').innerHTML=state.papers.length?`<div class="empty compact"><h3>没有符合条件的论文</h3><p>记录仍然保留，请调整阶段、期刊或搜索条件。</p><button class="button secondary" data-action="clear-filters">清除筛选</button></div>`:`<div class="empty"><div class="empty-icon" aria-hidden="true">▤</div><h3>从第一篇论文开始</h3><p>记录正在写的想法，或已经投出的稿件。</p><button class="button primary" data-action="new-paper">＋ 新建论文</button></div>`;return;}
  const shownStages=new Set(papers.map(p=>p.stage));
  const shownStage=shownStages.size===1?papers[0].stage:'';
  const onlyEarly=papers.every(earlyStage);
  const submissionDates=papers.every(p=>!earlyStage(p));
  const allEnded=submissionDates&&papers.every(p=>!p.current_submission||p.current_submission.ended_on);
  const waitOption=$('sort')?.querySelector?.('option[value="wait"]');
  if(waitOption)waitOption.textContent=shownStage==='planned'?'计划历时最多':shownStage==='writing'?'写作天数最多':allEnded?'本轮用时最多':submissionDates?'投稿等待最多':'阶段用时最多';
  const dateTitle=shownStage==='planned'?'计划建立日期':shownStage==='writing'?'开始撰写日期':submissionDates?'投稿日期':'阶段起始日期';
  const daysTitle=shownStage==='planned'?'已历时':shownStage==='writing'?'写作天数':allEnded?'本轮用时':submissionDates?'等待天数':'阶段用时';
  $('paper-table').innerHTML=`<div class="table-wrap"><table class="papers-table ${onlyEarly?'early-table':''}"><thead><tr><th>稿件编号 / 论文标题</th><th>阶段</th><th>${onlyEarly?'初始目标期刊':'期刊'} / 投稿入口</th><th>${dateTitle}</th><th>${daysTitle}</th>${onlyEarly?'':'<th>计划转投</th><th>转投候选期刊</th>'}</tr></thead><tbody>${papers.map(p=>{
    const r=p.current_submission,early=earlyStage(p),j=listedJournal(p),plan=early?null:r;
    const date=early?p.stage_started_on:r?.submitted_on,days=early?p.stage_elapsed_days:r?.elapsed_days;
    const dateLabel=p.stage==='planned'?'计划建立':p.stage==='writing'?'开始撰写':'投稿日期';
    const dayNote=early?(!shownStage?(p.stage==='planned'?'计划历时':'写作'): ''):r?.ended_on&&!allEnded?'本轮已结束':'';
    return `<tr><td><button class="paper-title" data-action="paper-detail" data-id="${p.id}"><span class="paper-code">${esc(p.paper_code)}</span><span>${esc(p.title)}</span></button><div class="paper-meta">${esc(p.authors||'作者未填写')}${p.tags?` · ${esc(p.tags)}`:''}${p.round_count?`<br>${p.round_count} 轮投稿记录`:''}${r?.manuscript_number?` · 期刊稿号 ${esc(r.manuscript_number)}`:''}${p.past_journals?.length?`<br>已投期刊：${p.past_journals.map(esc).join(' · ')}`:''}</div></td>
      <td>${stagePill(p.stage)}</td><td>${j?journalAnchor(j):`<span class="subtle">${early?'未设置目标期刊':'尚未登记投稿'}</span>`}${!onlyEarly&&early?'<div class="paper-meta">初始目标期刊</div>':r?.ended_on?'<div class="paper-meta">上轮 · 已结束</div>':''}</td>
      <td class="cell-date">${date?esc(date):'<span class="subtle">未设置</span>'}${!shownStage&&!submissionDates?`<div class="paper-meta">${dateLabel}</div>`:''}</td><td>${days!=null?`<span class="days">${days}<small>天</small></span>${dayNote?`<div class="paper-meta">${dayNote}</div>`:''}`:'<span class="subtle">—</span>'}</td>
      ${onlyEarly?'':`<td class="cell-date">${plan?.target_on?esc(plan.target_on):`<span class="subtle">${early?'—':'未设置'}</span>`}${p.overdue_days?`<br><span class="overdue-badge">超过计划时间 ${p.overdue_days} 天</span>`:''}${plan?.ended_on&&plan.target_on?'<div class="paper-meta">上轮计划</div>':''}</td><td>${plan?.candidates?.length?`<span class="journal-name">${esc(plan.candidates[0].name)}</span>`:'<span class="subtle">—</span>'}${plan?.candidates?.length>1?`<div class="paper-meta">另有 ${plan.candidates.length-1} 个候选</div>`:''}</td>`}</tr>`;
  }).join('')}</tbody></table></div><div class="table-scroll-hint">表格内容较宽时，可左右滚动查看。</div>`;
  syncTableOverflow();
}

function syncTableOverflow(){const host=$('paper-table');const wrap=host?.querySelector?.('.table-wrap'),hint=host?.querySelector?.('.table-scroll-hint');if(wrap&&hint)hint.hidden=wrap.scrollWidth<=wrap.clientWidth+1;}

function renderJournals() {
  $('content').innerHTML=header('期刊管理','保存常用期刊，一次设置投稿入口。',`<button class="button primary" data-action="new-journal">＋ 新增期刊</button>`)+
    (state.journals.length?`<div class="journal-grid">${state.journals.map(j=>`<article class="journal-card"><h2>${esc(j.name)}</h2>${j.url?`<a class="journal-url" href="${esc(j.url)}" target="_blank" rel="noopener noreferrer" title="${esc(j.url)}">${esc(j.url)}</a>`:'<span class="journal-url subtle">尚未设置投稿入口</span>'}<p>${esc(j.notes||'暂无备注')}</p><div class="journal-guidelines"><b>投稿须知</b>${j.guidelines_url?`<a class="button secondary small" href="${esc(j.guidelines_url)}" target="_blank" rel="noopener noreferrer">查看投稿须知 ↗</a>`:`<button class="link-button" data-action="edit-journal" data-id="${j.id}">添加投稿须知链接</button>`}</div><div class="card-actions"><small>${j.usage_count} 轮投稿记录</small><div>${j.url?`<a class="button secondary small" href="${esc(j.url)}" target="_blank" rel="noopener noreferrer">投稿入口 ↗</a> `:''}<button class="button secondary small" data-action="edit-journal" data-id="${j.id}">编辑期刊</button></div></div></article>`).join('')}</div>`:
    `<div class="panel empty"><div class="empty-icon" aria-hidden="true">▥</div><h3>保存你的常用期刊</h3><p>填写期刊名称与投稿管理页链接。<br>之后登记投稿时，可以直接选择。</p><button class="button primary" data-action="new-journal">＋ 新增期刊</button></div>`);
}

function backupLabel(name) {
  if(name.startsWith('paper-added-'))return '新增论文后自动备份';
  if(name.startsWith('manual-'))return '手动完整备份';
  if(name.startsWith('daily-'))return '旧版 · 每日修改前快照';
  if(name.startsWith('latest-'))return '旧版 · 保存后完整备份';
  if(name.startsWith('damaged-'))return '保留的损坏备份';
  if(name.startsWith('delete-'))return '旧版 · 删除前备份';
  if(name.startsWith('restore-'))return '旧版 · 恢复前备份';
  if(name.startsWith('migration-'))return '升级迁移备份';
  if(name.startsWith('imported-'))return '保留的旧版备份';
  if(name.startsWith('upgrade-'))return '升级前自动备份';
  return '本机备份';
}
function backupRow(b) {
  return `<div class="backup-row"><div title="${esc(b.name)}"><b>${backupLabel(b.name)}</b><small>${b.counts?`${b.counts.papers} 篇论文 · ${b.counts.journals} 个期刊 · ${b.counts.submissions} 轮投稿`:'这份备份无法读取，原文件保留'}</small><small>${esc(b.modified_at.slice(0,19).replace('T',' '))} · ${Math.ceil(b.size/1024)} KB</small></div><button class="button secondary small" data-action="restore-snapshot" data-name="${esc(b.name)}" ${b.counts?'':'disabled'}>恢复</button></div>`;
}
function backupList(backups) {
  if(!backups.length)return '<div class="empty compact"><h3>尚无自动备份</h3><p>新增论文后会自动生成，也可以随时手动备份。</p></div>';
  if(backups.length<=4)return backups.map(backupRow).join('');
  return backups.slice(0,3).map(backupRow).join('')+`<div id="older-backups" hidden>${backups.slice(3).map(backupRow).join('')}</div><button class="button secondary backup-more" data-action="toggle-backups" aria-expanded="false" aria-controls="older-backups" data-count="${backups.length-3}">更多备份（${backups.length-3} 份）</button>`;
}
async function renderBackup() {
  $('content').innerHTML=header('数据与备份','正在核对本机记录与备份…')+'<div class="loading">正在读取备份信息…</div>';
  const information=await api('/api/backups');
  if(currentView!=='backup')return;
  state.backups=information.backups;state.diagnostics=information.diagnostics;
  const d=state.diagnostics;
  $('content').innerHTML=header('数据与备份','保存记录与生成备份，分别清楚可查。')+
    `<section class="panel diagnostic-panel"><div class="diagnostic-status"><span class="integrity-dot ${d.integrity_ok?'':'invalid'}"></span><div><b>${d.integrity_ok?'本机记录正常':'数据库检查未通过，请先保留文件'}</b><p>${d.counts.papers} 篇论文 · ${d.counts.journals} 个期刊 · ${d.counts.submissions} 轮投稿 · ${d.counts.files} 个文件关联</p></div></div><div class="diagnostic-times"><p>最近保存 <b>${esc(d.last_saved_at?.slice(0,19).replace('T',' ')||'尚无记录')}</b></p><p>最近备份 <b>${esc(d.latest_backup?.modified_at.slice(0,19).replace('T',' ')||'尚未生成')}</b></p></div>${d.backup_warning?`<p class="warning-text">${esc(d.backup_warning)}</p>`:''}</section>`+
    `<div class="backup-grid"><section class="panel backup-section"><div class="section-kicker">数据管理</div><h2>本机数据</h2><p>每次保存均写入数据库；关闭浏览器或清理缓存不会清除记录。</p><div class="path-block">${esc(state.database_path)}</div><button class="button secondary small" data-action="open-storage" data-kind="data">打开数据文件夹 ↗</button><div class="section-heading">导出与恢复</div><p>JSON 包含全部记录及文件路径，稿件正文仍在原文件夹。</p><div class="backup-actions"><button class="button primary" data-action="export">导出完整备份</button><button class="button secondary" data-action="import">从 JSON 恢复</button></div><p class="field-help">恢复会替换当前记录，不再自动备份；需要保留当前状态时，请先导出。</p></section>
    <section class="panel backup-section"><div class="section-kicker">备份记录 · 共 ${state.backups.length} 份</div><h2>自动与手动备份</h2><p>仅新增论文后、升级前自动备份。普通编辑、投稿进展、删除和恢复均不触发。手动备份可保存当前完整状态。</p><button class="button secondary" data-action="refresh-backup">立即生成完整备份</button><div class="backup-list">${backupList(state.backups)}</div><details class="backup-location"><summary>查看备份位置与保留规则</summary><p>按原规则保留最近 30 天；折叠只影响列表展示，不删除备份。不自动上传云端。</p><div class="path-block">${esc(state.backup_dir)}</div><button class="button secondary small" data-action="open-storage" data-kind="backups">打开备份文件夹 ↗</button></details></section></div>`;
}

function drawerHeader(title,subtitle='') {return `<div class="drawer-header"><div><div class="eyebrow">${esc(subtitle||'PAPERDESK')}</div><h2 id="drawer-title">${esc(title)}</h2></div><button class="close-button" data-action="close-drawer" aria-label="关闭侧栏">×</button></div>`;}
function showDrawer(html, info) {
  drawerRequestGeneration+=1;
  drawerGeneration += 1;
  if($('drawer-layer').hidden) previousFocus=document.activeElement;
  $('drawer').innerHTML=html;
  $('drawer-layer').hidden=false;
  drawerInfo=info;
  drawerInfo.operationId=crypto.randomUUID();
  drawerDirty=false;
  document.body.classList.add('drawer-open');
  $('drawer').focus();
  wireFormFeedback();
  $('drawer').oninput=event=>{drawerDirty=true;clearFieldFeedback(event.target);const error=$('form-error');if(error&&!error.hidden){error.hidden=true;error.textContent='';}};
}
async function allowNavigation() {
  if(saving){toast('正在保存，请稍等片刻。');return false;}
  if(drawerDirty && !await confirmAction('放弃尚未保存的修改？','当前表单的修改还没有保存。','放弃修改')) return false;
  return true;
}
async function closeDrawer(force=false) {
  if(!force && !await allowNavigation())return;
  $('drawer-layer').hidden=true;
  $('drawer').innerHTML='';
  drawerInfo=null;drawerDirty=false;
  drawerGeneration += 1;
  drawerRequestGeneration+=1;
  document.body.classList.remove('drawer-open');
  if(previousFocus?.isConnected)previousFocus.focus();
}
function clearFieldFeedback(field){
  if(!field?.getAttribute?.('aria-invalid')||!field.validity.valid)return;
  field.removeAttribute('aria-invalid');
  const message=field.closest('.form-field,.file-edit-row')?.querySelector('.field-message');
  if(message){const descriptions=(field.getAttribute('aria-describedby')||'').split(' ').filter(id=>id&&id!==message.id);if(descriptions.length)field.setAttribute('aria-describedby',descriptions.join(' '));else field.removeAttribute('aria-describedby');message.remove();}
}
function wireFormFeedback(){
  $('drawer').querySelectorAll('form').forEach(form=>form.addEventListener('invalid',event=>{
    event.preventDefault();const field=event.target;
    const host=field.closest('.form-field,.file-edit-row');if(!host)return;
    const first=!form.querySelector('[aria-invalid="true"]');
    field.setAttribute('aria-invalid','true');
    let message=host.querySelector('.field-message');if(!message){message=document.createElement('span');message.className='field-message';message.id=`${field.id||crypto.randomUUID()}-feedback`;message.setAttribute('role','alert');host.appendChild(message);field.setAttribute('aria-describedby',`${field.getAttribute('aria-describedby')||''} ${message.id}`.trim());}
    const label=(field.labels?.[0]?.textContent||field.getAttribute('aria-label')||'此项').replace(/\s*\*\s*$/,'').trim();
    message.textContent=field.validity.customError?field.validationMessage:field.validity.valueMissing?`请填写${label}。`:field.type==='url'?'请输入以 http:// 或 https:// 开头的完整链接。':`请检查${label}的格式或日期范围。`;
    if(first)field.focus();
  },true));
}
function formError(error) {
  const box=$('form-error');
  if(box){box.setAttribute('role','alert');box.innerHTML=`<b>操作未完成</b><span>${esc(error.message||String(error))}</span>${error.code==='conflict'?'<button type="button" class="link-button" data-action="reload-record">重新读取记录</button>':''}`;box.hidden=false;box.scrollIntoView({block:'nearest'});}
  else toast(error.message||String(error),true);
}
function formActions(form,backLabel='取消') {return `<div class="drawer-actions"><button class="button secondary" data-action="form-back">${backLabel}</button><span class="save-note">保存到本机</span><button type="submit" class="button primary" form="${form}">保存记录</button></div>`;}

async function afterSave(message,paperId=null) {
  const warning=lastWriteWarning;
  await closeDrawer(true);
  try{await refresh();if(paperId)await paperDetail(paperId);toast(warning?`${message}。${warning}`:message,!!warning);}
  catch{toast(`${message}，但页面刷新失败。请刷新查看，记录已保存在本机。${warning||''}`,true);}
}

async function paperDetail(id, expectedGeneration=null) {
  const request=++drawerRequestGeneration;
  const p=await api(`/api/papers/${id}`);
  if(request!==drawerRequestGeneration)return;
  if(expectedGeneration!==null && drawerGeneration!==expectedGeneration)return;
  const active=p.submissions.find(r=>!r.ended_on);
  showDrawer(drawerHeader(p.title,`稿件 ${p.paper_code} · 论文详情`)+`<div class="drawer-body"><div class="detail-top">${stagePill(p.stage)}<span class="muted">${p.submissions.length} 轮投稿</span><button class="button secondary small" data-action="edit-paper" data-id="${p.id}">编辑论文信息</button></div>
    <dl class="detail-meta"><dt>作者</dt><dd>${esc(p.authors||'未填写')}</dd><dt>标签</dt><dd>${esc(p.tags||'未填写')}</dd><dt>初始目标期刊</dt><dd>${p.target_journal?journalAnchor(p.target_journal):'未设置'}</dd><dt>计划建立日期</dt><dd>${esc(p.planned_on||'未设置')}</dd><dt>开始撰写日期</dt><dd>${esc(p.writing_started_on||'未设置')}</dd>${earlyStage(p)?`<dt>${p.stage==='planned'?'已历时':'写作天数'}</dt><dd>${p.stage_elapsed_days==null?'未设置起始日期':p.stage_elapsed_days+' 天'}</dd>`:''}<dt>更新时间</dt><dd>${esc(p.updated_at.slice(0,16).replace('T',' '))}</dd></dl>
    ${p.notes?`<div class="detail-note">${esc(p.notes)}</div>`:''}
    <div class="section-heading">论文文件${p.folder?`<button class="link-button" data-action="open-folder" data-id="${p.id}">打开文件夹 ↗</button>`:''}</div>
    ${p.folder?`<div class="path-block">${esc(p.folder)}</div>`:''}
    ${p.files.length?p.files.map(f=>`<div class="document-row"><div><b>${esc(f.label)}</b><small>${esc(f.path)}</small></div><button class="button secondary small" data-action="open-file" data-id="${f.id}">打开 ↗</button></div>`).join(''):'<p class="field-help">尚未关联稿件，可在“编辑论文信息”中添加。</p>'}
    <div class="section-heading">投稿记录</div>
    ${p.submissions.length?p.submissions.map((r,index)=>`<article class="history-card ${!r.ended_on?'active-round':''}"><div class="history-round">第 ${p.submissions.length-index} 轮${!r.ended_on?' · 当前投稿':' · 已结束'}</div><div class="history-heading"><b>${esc(r.journal_name)}</b><small>${r.ended_on?esc(results[r.result]):'进行中'}</small></div><div class="history-dates">投稿 ${esc(r.submitted_on)}${r.ended_on?` · 结束 ${esc(r.ended_on)}`:''}<br>${r.ended_on?'该轮总等待 ':''}${r.elapsed_days} 天${r.manuscript_number?` · 期刊稿号 ${esc(r.manuscript_number)}`:''}</div>${r.progress?`<div class="history-note">进展：${esc(r.progress)}</div>`:''}${r.notes?`<div class="history-note">${esc(r.notes)}</div>`:''}<div class="history-plan">${r.target_on?`计划转投：${esc(r.target_on)}${r.transfer_mode==='days'?`（投稿后 ${r.wait_days} 天）`:''}`:'未设置转投日期'}${r.candidates.length?`<br>候选：${r.candidates.map(j=>esc(j.name)).join(' → ')}`:''}</div>${r.overdue_days?`<span class="overdue-badge">超过计划时间 ${r.overdue_days} 天</span>`:''}<div class="history-actions">${r.journal_url?`<a class="link-button" href="${esc(r.journal_url)}" target="_blank" rel="noopener noreferrer">投稿入口 ↗</a>`:`<button class="link-button" data-action="edit-journal" data-id="${r.journal_id}">设置投稿入口</button>`}${r.ended_on?`<button class="link-button" data-action="edit-submission" data-paper="${p.id}" data-id="${r.id}">编辑本轮记录</button>${!active&&r.id===p.submissions[0].id&&!['accepted','archived'].includes(p.stage)?`<button class="link-button" data-action="transfer-submission" data-paper="${p.id}" data-id="${r.id}">登记转投 →</button>`:''}`:''}</div></article>`).join(''):'<div class="empty compact"><h3>还没有投稿记录</h3><p>点击下方“登记投稿”，填写期刊和日期即可开始计时。</p></div>'}
    ${active?`<details class="record-tools"><summary>补充旧投稿记录</summary><p class="field-help">用于补录此前已经结束的轮次，当前投稿继续保留。</p><button class="button secondary" data-action="new-submission" data-id="${p.id}">补录已结束轮次</button></details>`:''}
    </div><div class="drawer-actions"><button class="button secondary" data-action="close-drawer">关闭</button>${!active?`<button class="button primary push-right" data-action="new-submission" data-id="${p.id}">登记投稿</button>`:`<button class="button primary push-right" data-action="edit-submission" data-paper="${p.id}" data-id="${active.id}">管理当前投稿</button>`}</div>`,{kind:'detail',paperId:p.id,paper:p});
}

async function selectLocalPath(button,action){
      const generation=drawerGeneration;const label=button.textContent;button.disabled=true;button.textContent='正在选择…';toast('请在 Windows 文件选择窗口中选择；取消后可继续填写。');
      try{const selected=await api('/api/pick','POST',{kind:action==='pick-folder'?'folder':'file'});if(generation!==drawerGeneration)return;if(selected.path){if(action==='pick-folder')$('p-folder').value=selected.path;else if(action==='choose-and-add-file'){$('file-rows').insertAdjacentHTML('beforeend',fileEditRow({path:selected.path,label:selected.path.split(/[\\/]/).pop()}));}else{const row=button.closest('[data-file-row]');if(!row?.isConnected)return;row.querySelector('.file-path').value=selected.path;if(!row.querySelector('.file-label').value)row.querySelector('.file-label').value=selected.path.split(/[\\/]/).pop();}drawerDirty=true;toast('文件已选中，保存论文后完成关联。');}}
      finally{if(button.isConnected){button.disabled=false;button.textContent=label;}}
}

function fileEditRow(f={}) {return `<div class="file-edit-row" data-file-row><div class="file-row-top"><input class="file-label" placeholder="稿件名称，例如投稿终稿" aria-label="稿件名称" value="${esc(f.label||'')}" maxlength="200"><button class="icon-button" type="button" data-action="remove-file-row" aria-label="移除稿件关联">×</button></div><div class="inline-input"><input class="file-path" required placeholder="稿件完整路径" aria-label="稿件路径" value="${esc(f.path||'')}" maxlength="4000"><button class="button secondary small" type="button" data-action="pick-file">选择文件</button></div></div>`;}
async function paperForm(id) {
  const request=++drawerRequestGeneration;
  const stage=filters.stage||'writing';
  const p=id?await api(`/api/papers/${id}`):{paper_code:state.next_paper_code,title:'',authors:state.default_authors||'',tags:'',stage,notes:'',folder:'',planned_on:stage==='planned'?state.today:null,writing_started_on:stage==='writing'?state.today:null,target_journal_id:null,files:[],submissions:[]};
  if(request!==drawerRequestGeneration)return;
  const active=p.submissions.some(r=>!r.ended_on);
  const allowed=!id?Object.keys(stages):active?['submitted','revision','archived']:['planned','writing','accepted','archived'];
  showDrawer(drawerHeader(id?'编辑论文':'新建论文',id?'论文信息':'从一篇论文开始')+`<div class="drawer-body"><form id="paper-form"><div id="form-error" class="form-error" hidden></div>
    <fieldset class="form-section"><legend>基本信息</legend><p class="section-description">编号用于查找，标题和作者用于识别这篇论文。</p><div class="form-field"><label for="p-code">稿件编号 <span class="required">*</span></label><input id="p-code" required maxlength="64" value="${esc(p.paper_code)}" placeholder="例如：0001"><small>每篇论文唯一，默认接续已有编号，可修改。转投后继续使用此编号。</small></div>
    <div class="form-field"><label for="p-title">论文标题 <span class="required">*</span></label><input id="p-title" required maxlength="500" placeholder="填写论文标题" value="${esc(p.title)}"></div>
    <div class="form-field"><label for="p-authors">作者</label><input id="p-authors" maxlength="1000" placeholder="作者姓名" value="${esc(p.authors)}"></div></fieldset><fieldset class="form-section"><legend>阶段与投稿目标</legend><div class="form-field"><label for="p-stage">论文阶段</label><select id="p-stage">${Object.entries(stages).map(([k,v])=>`<option value="${k}" ${p.stage===k?'selected':''} ${!allowed.includes(k)?'disabled':''}>${v}</option>`).join('')}</select></div>
    <p id="p-stage-help" class="field-help">${active?'当前有进行中的投稿。录用或退稿时，请通过“管理当前投稿”将本轮状态设为“已结束”。':id?'登记投稿后，阶段会自动变为“已投稿”。':'之前已经投稿的论文，可以直接选择“已投稿”或“退修中”，填写原投稿信息。'}</p>
    ${!id?`<section id="p-initial-submission" class="initial-submission" hidden><div class="section-heading">当前投稿</div><div class="form-field"><label for="p-submission-journal">投稿期刊 <span class="required">*</span></label><select id="p-submission-journal" class="journal-select">${journalOptions()}</select><small>没有找到期刊？可在下方快速新增。</small></div><div class="form-grid"><div class="form-field"><label for="p-submitted-on">原投稿日期 <span class="required">*</span></label><input id="p-submitted-on" type="date" max="${state.today}" value="${state.today}"></div><div class="form-field"><label for="p-manuscript-number">期刊稿号（选填）</label><input id="p-manuscript-number" maxlength="200" placeholder="选填"></div></div><div class="form-field"><label for="p-submission-progress">当前进展</label><input id="p-submission-progress" maxlength="1000" placeholder="例如：外审中、已收到修改意见"></div><p class="field-help">按原投稿日期计算等待天数。保存后可在投稿记录中设置转投计划、补录更早的轮次。</p></section>`:''}
    <div id="p-target-field" class="form-field"><label for="p-target-journal">初始目标期刊</label><select id="p-target-journal" class="journal-select">${journalOptions(p.target_journal_id,'暂不设置')}</select><small>最初计划投稿的期刊，独立于每轮投稿和转投候选，可随时修改。</small></div>
    ${quickJournalForm('p-target-journal')}
    <div id="p-stage-dates"><div class="form-grid"><div class="form-field"><label for="p-planned-on">计划建立日期</label><input id="p-planned-on" type="date" max="${state.today}" value="${esc(p.planned_on||'')}"></div><div class="form-field"><label for="p-writing-on">开始撰写日期</label><input id="p-writing-on" type="date" max="${state.today}" value="${esc(p.writing_started_on||'')}"></div></div><p class="field-help">从对应日期开始计时，当天为第 0 天；可以补填或留空。</p></div>
    </fieldset><fieldset class="form-section"><legend>补充信息</legend><div class="form-field"><label for="p-tags">标签</label><input id="p-tags" maxlength="1000" placeholder="例如：人工智能、跨学科、教学设计" value="${esc(p.tags)}"></div>
    <div class="form-field"><label for="p-notes">备注</label><textarea id="p-notes" maxlength="10000" placeholder="写作思路、修改安排或刊出信息…">${esc(p.notes)}</textarea></div>
    </fieldset><fieldset class="form-section file-section"><legend>关联本地稿件</legend><p class="section-description">选择文件或粘贴路径，正文留在原位置。</p><div class="form-field"><label for="p-folder">论文文件夹</label><div class="inline-input"><input id="p-folder" maxlength="4000" placeholder="粘贴完整路径，或选择文件夹" value="${esc(p.folder)}"><button class="button secondary small" type="button" data-action="pick-folder">选择</button></div></div>
    <div id="file-rows">${p.files.map(fileEditRow).join('')}</div><div class="file-association-box">${icon('papers')}<div><b>关联本机文件</b><p>Word、PDF 或其他稿件文件，只保存路径。</p></div><div class="file-association-actions"><button class="button secondary" type="button" data-action="choose-and-add-file">选择文件并关联</button><button class="link-button" type="button" data-action="add-file-row">手动填写路径</button></div></div><p class="field-help">仅记录路径；移除关联不删除原文件，移动文件后可重新选择。</p></fieldset>
    ${id?`<details class="record-tools"><summary>记录管理</summary><p class="field-help">暂时不需要的论文可以设为归档，所有历史继续保留。</p><button class="button danger small" type="button" data-action="delete-paper" data-id="${id}">删除论文及投稿记录</button></details>`:''}
    </form></div>`+formActions('paper-form'),{kind:'paper-form',paperId:id||null,expectedUpdatedAt:p.updated_at});
  const checkCode=()=>{const code=$('p-code').value.normalize('NFKC').trim().toUpperCase();$('p-code').setCustomValidity(state.papers.some(other=>other.id!==id&&other.paper_code===code)?'这个稿件编号已存在，请使用其他编号。':'');};
  $('p-code').addEventListener('input',checkCode);checkCode();
  $('paper-form').addEventListener('submit',savePaper);
  let automaticWritingDate=!id;
  $('p-writing-on').addEventListener('input',()=>{automaticWritingDate=false;});
  const syncInitialSubmission=()=>{
    if(id)return;
    const direct=['submitted','revision'].includes($('p-stage').value);
    $('p-initial-submission').hidden=!direct;
    $('p-target-field').hidden=direct;$('p-stage-dates').hidden=direct;
    for(const key of ['p-submission-journal','p-submitted-on']){$(key).required=direct;$(key).disabled=!direct;}
    document.querySelector('[data-action="quick-journal"]').dataset.target=direct?'p-submission-journal':'p-target-journal';
    if(direct&&!$('p-submission-journal').value)$('p-submission-journal').value=$('p-target-journal').value;
    if($('p-stage').value!=='writing'&&automaticWritingDate)$('p-writing-on').value='';
  };
  $('p-stage').addEventListener('change',()=>{
    if($('p-stage').value==='planned'&&!$('p-planned-on').value)$('p-planned-on').value=state.today;
    if($('p-stage').value==='writing'&&!$('p-writing-on').value)$('p-writing-on').value=state.today;
    syncInitialSubmission();
  });
  syncInitialSubmission();
}

async function savePaper(event) {
  event.preventDefault();
  saving=true;
  const button=document.querySelector('[form="paper-form"]');
  button.disabled=true;button.textContent='保存中…';
  try {
    const data={paper_code:$('p-code').value,title:$('p-title').value,authors:$('p-authors').value,tags:$('p-tags').value,stage:$('p-stage').value,notes:$('p-notes').value,folder:$('p-folder').value,expected_updated_at:drawerInfo.expectedUpdatedAt,
      target_journal_id:$('p-target-journal').value?Number($('p-target-journal').value):null,planned_on:$('p-planned-on').value||null,writing_started_on:$('p-writing-on').value||null,
      files:[...document.querySelectorAll('[data-file-row]')].map(row=>({label:row.querySelector('.file-label').value,path:row.querySelector('.file-path').value}))};
    const id=drawerInfo.paperId;
    if(!id&&['submitted','revision'].includes(data.stage))data.initial_submission={journal_id:Number($('p-submission-journal').value),submitted_on:$('p-submitted-on').value,manuscript_number:$('p-manuscript-number').value,progress:$('p-submission-progress').value,transfer_mode:'none',candidate_ids:[]};
    const saved=await api(id?`/api/papers/${id}`:'/api/papers',id?'PUT':'POST',data,drawerInfo.operationId);
    drawerInfo.paperId=saved.id;
    drawerDirty=false;
    await afterSave('论文记录已保存',saved.id);
  } catch(error){formError(error);}
  finally{saving=false;if(button.isConnected){button.disabled=false;button.textContent='保存记录';}}
}

function journalForm(id) {
  const j=id?state.journals.find(j=>j.id===id):{name:'',url:'',notes:'',guidelines_url:''};
  if(!j)throw new Error('期刊不存在，请刷新页面。');
  const returnPaper=drawerInfo?.paperId;
  showDrawer(drawerHeader(id?'编辑期刊':'新增期刊','投稿入口与须知')+`<div class="drawer-body"><form id="journal-form"><div id="form-error" class="form-error" hidden></div><fieldset class="form-section"><legend>期刊信息</legend><div class="form-field"><label for="j-name">期刊名称 <span class="required">*</span></label><input id="j-name" required maxlength="200" placeholder="填写期刊名称" value="${esc(j.name)}"></div></fieldset><fieldset class="form-section"><legend>投稿入口与须知</legend><div class="form-field"><label for="j-url">投稿管理页链接</label><input id="j-url" type="url" pattern="[hH][tT][tT][pP][sS]?://.+" maxlength="3000" placeholder="https://…" value="${esc(j.url)}"><small>填写你平时登录的投稿系统入口；打开后仍由你正常登录。</small></div><div class="form-field"><label for="j-guidelines">投稿须知链接</label><input id="j-guidelines" type="url" pattern="[hH][tT][tT][pP][sS]?://.+" maxlength="3000" placeholder="https://…" value="${esc(j.guidelines_url||'')}"><small>填写期刊的投稿说明、征稿要求或作者指南页面。</small></div></fieldset><fieldset class="form-section"><legend>补充说明</legend><div class="form-field"><label for="j-notes">备注</label><textarea id="j-notes" maxlength="10000" placeholder="期刊方向、投稿要求或自己的记录…">${esc(j.notes)}</textarea></div></fieldset>${id?`<button class="button danger small" type="button" data-action="delete-journal" data-id="${id}">删除期刊</button><p class="field-help">已用于投稿或候选计划的期刊，需先修改相关记录才能删除。</p>`:''}</form></div>`+formActions('journal-form'),{kind:'journal-form',journalId:id||null,paperId:returnPaper});
  drawerInfo.expectedUpdatedAt=j.updated_at;
  $('journal-form').addEventListener('submit',async e=>{
    e.preventDefault();saving=true;const button=document.querySelector('[form="journal-form"]');button.disabled=true;button.textContent='保存中…';
    try{await api(id?`/api/journals/${id}`:'/api/journals',id?'PUT':'POST',{name:$('j-name').value,url:$('j-url').value,guidelines_url:$('j-guidelines').value,notes:$('j-notes').value,expected_updated_at:j.updated_at},drawerInfo.operationId);drawerDirty=false;await afterSave('期刊已保存',returnPaper);}
    catch(error){formError(error);}finally{saving=false;if(button.isConnected){button.disabled=false;button.textContent='保存记录';}}
  });
}

function journalOptions(selected='',emptyLabel='请选择期刊') {return `<option value="">${emptyLabel}</option>${state.journals.map(j=>`<option value="${j.id}" ${Number(selected)===j.id?'selected':''}>${esc(j.name)}</option>`).join('')}`;}
function quickJournalForm(target) {return `<details class="subform-box"><summary>＋ 快速新增期刊</summary><div class="form-field"><label for="quick-name">期刊名称</label><input id="quick-name" maxlength="200" placeholder="期刊名称"></div><div class="form-field"><label for="quick-url">投稿管理页链接（可选）</label><input id="quick-url" type="url" pattern="[hH][tT][tT][pP][sS]?://.+" maxlength="3000" placeholder="https://…"></div><button class="button secondary small" type="button" data-action="quick-journal" data-target="${target}">保存并选择期刊</button></details>`;}
function candidateRow(id='') {return `<div class="candidate-row" data-candidate-row><span class="candidate-number"></span><select class="journal-select candidate-select" required aria-label="候选期刊">${journalOptions(id)}</select><button type="button" class="icon-button" data-action="candidate-up" aria-label="向前移动候选期刊">↑</button><button type="button" class="icon-button" data-action="candidate-down" aria-label="向后移动候选期刊">↓</button><button type="button" class="icon-button" data-action="remove-candidate" aria-label="移除候选期刊">×</button></div>`;}
function syncCandidateNumbers(){document.querySelectorAll('[data-candidate-row]').forEach((r,i)=>{r.querySelector('.candidate-number').textContent=i+1;r.querySelector('[data-action="candidate-up"]').disabled=i===0;r.querySelector('[data-action="candidate-down"]').disabled=i===document.querySelectorAll('[data-candidate-row]').length-1;});}

async function submissionForm(paperId,roundId,end=false) {
  const request=++drawerRequestGeneration;
  const p=await api(`/api/papers/${paperId}`);
  if(request!==drawerRequestGeneration)return;
  const active=p.submissions.find(r=>!r.ended_on);
  const r=roundId?p.submissions.find(r=>r.id===roundId):{submitted_on:state.today,journal_id:p.submissions.length?'':p.target_journal_id||'',manuscript_number:'',progress:'',notes:'',transfer_mode:'none',wait_days:'',target_on:'',ended_on:null,result:null,candidates:[]};
  if(!r)throw new Error('投稿记录不存在，请刷新页面。');
  const historyOnly=!roundId&&!!active;
  const closed=!!r.ended_on||end||historyOnly;
  showDrawer(drawerHeader(end?'结束本轮投稿':roundId?(r.ended_on?'编辑本轮记录':'管理当前投稿'):historyOnly?'补录已结束轮次':'登记投稿',p.title)+`<div class="drawer-body"><form id="submission-form"><div id="form-error" class="form-error" hidden></div>
    ${historyOnly?'<div class="mini-banner">这篇论文已有进行中的投稿。这里可补录已结束的历史；转投前请先结束当前轮次。</div>':''}
    <fieldset class="form-section"><legend>本轮投稿信息</legend><div class="form-field"><label for="s-journal">投稿期刊 <span class="required">*</span></label><select id="s-journal" class="journal-select" required>${journalOptions(r.journal_id)}</select><small>没有找到期刊？可在下方直接新增。</small></div>
    ${quickJournalForm('s-journal')}
    <div class="form-grid"><div class="form-field"><label for="s-date">投稿日期 <span class="required">*</span></label><input id="s-date" type="date" required max="${state.today}" value="${esc(r.submitted_on)}"></div><div class="form-field"><label for="s-number">期刊稿号（选填）</label><input id="s-number" maxlength="200" placeholder="选填" value="${esc(r.manuscript_number)}"></div></div>
    </fieldset>${roundId&&!r.ended_on?`<div class="transfer-entry"><div><b>已实际转投到下一家期刊？</b><p>结束本轮并新增一轮投稿，原期刊与所有记录继续保留。</p></div><button class="button secondary" type="button" data-action="transfer-submission" data-id="${roundId}" data-paper="${paperId}">登记转投 →</button></div>`:''}
    <fieldset class="form-section"><legend>进展与结果</legend><div class="form-field"><label for="s-progress">当前进展</label><input id="s-progress" maxlength="1000" placeholder="例如：已收稿、外审中、已提交修改稿" value="${esc(r.progress)}"></div>
    <div class="form-field"><label for="s-status">本轮状态</label><select id="s-status"><option value="active" ${!closed?'selected':''} ${historyOnly?'disabled':''}>进行中</option><option value="closed" ${closed?'selected':''}>已结束</option></select></div>
    <div id="end-fields" class="form-grid"><div class="form-field"><label for="s-end">结束日期 <span class="required">*</span></label><input id="s-end" type="date" max="${state.today}" value="${esc(r.ended_on||state.today)}"></div><div class="form-field"><label for="s-result">本轮结果 <span class="required">*</span></label><select id="s-result"><option value="">请选择结果</option>${Object.entries(results).map(([k,v])=>`<option value="${k}" ${r.result===k?'selected':''}>${v}</option>`).join('')}</select></div></div>
    ${roundId&&r.ended_on&&p.stage!=='archived'&&!active?'<label class="sync-stage-choice"><input id="s-sync-stage" type="checkbox">按本轮结果同步论文阶段</label><p class="field-help">只修改旧备注或期刊稿号时，无需勾选，当前论文阶段会保留。</p>':''}
    </fieldset><fieldset class="form-section"><legend>转投安排</legend><div class="form-field"><label for="s-transfer">计划方式</label><select id="s-transfer"><option value="none" ${r.transfer_mode==='none'?'selected':''}>不设置转投日期</option><option value="days" ${r.transfer_mode==='days'?'selected':''}>投稿后等待若干天</option><option value="date" ${r.transfer_mode==='date'?'selected':''}>指定转投日期</option></select></div>
    <div id="wait-field" class="form-field"><label for="s-wait">等待天数</label><input id="s-wait" type="number" min="1" max="3650" step="1" placeholder="例如：60" value="${esc(r.wait_days??'')}"><small id="due-preview"></small></div>
    <div id="target-field" class="form-field"><label for="s-target">计划转投日期</label><input id="s-target" type="date" value="${esc(r.target_on||'')}"></div>
    <p class="field-help">只在超过计划日期后显示提示。可以继续等待、调整日期，或随时关闭计划。</p>
    <div class="section-heading">转投候选期刊 <button class="link-button" type="button" data-action="add-candidate">＋ 添加</button></div><div id="candidate-rows">${r.candidates.map(j=>candidateRow(j.id)).join('')}</div><p class="field-help">按顺序保存；第一个候选显示在列表中，与论文的初始目标期刊分别记录。</p>
    </fieldset><fieldset class="form-section"><legend>补充说明</legend><div class="form-field"><label for="s-notes">本轮备注</label><textarea id="s-notes" maxlength="10000" placeholder="编辑部反馈、联系记录或转投考虑…">${esc(r.notes)}</textarea></div>
    </fieldset>${roundId?`<button class="button danger small" type="button" data-action="delete-submission" data-id="${roundId}" data-paper="${paperId}">删除这轮记录</button>`:''}</form></div>`+formActions('submission-form','返回详情'),{kind:'submission-form',paperId,roundId:roundId||null,expectedUpdatedAt:r.updated_at,paperExpectedUpdatedAt:p.updated_at,historicalOnly:historyOnly});
  $('s-status').addEventListener('change',syncSubmissionFields);
  $('s-transfer').addEventListener('change',syncSubmissionFields);
  $('s-date').addEventListener('input',syncSubmissionFields);
  $('s-wait').addEventListener('input',syncSubmissionFields);
  syncSubmissionFields();syncCandidateNumbers();
  $('submission-form').addEventListener('submit',saveSubmission);
}

function syncSubmissionFields() {
  const closed=$('s-status').value==='closed';
  $('end-fields').hidden=!closed;
  for(const id of ['s-end','s-result']){$(id).required=closed;$(id).disabled=!closed;}
  $('s-end').min=$('s-date').value;
  const mode=$('s-transfer').value;
  $('wait-field').hidden=mode!=='days';$('target-field').hidden=mode!=='date';
  $('s-wait').required=mode==='days';$('s-wait').disabled=mode!=='days';
  $('s-target').required=mode==='date';$('s-target').disabled=mode!=='date';$('s-target').min=$('s-date').value;
  let preview='';
  if(mode==='days'&&$('s-date').value&&$('s-wait').value){const ms=Date.parse($('s-date').value+'T00:00:00Z')+Number($('s-wait').value)*86400000;if(Number.isFinite(ms))preview='计划转投日期：'+new Date(ms).toISOString().slice(0,10);}
  $('due-preview').textContent=preview;
}

async function saveSubmission(e) {
  e.preventDefault();saving=true;const button=document.querySelector('[form="submission-form"]');button.disabled=true;button.textContent='保存中…';
  try {
    const closed=$('s-status').value==='closed';const info={...drawerInfo};
    const data={journal_id:Number($('s-journal').value),submitted_on:$('s-date').value,manuscript_number:$('s-number').value,progress:$('s-progress').value,notes:$('s-notes').value,
      expected_updated_at:info.expectedUpdatedAt,paper_expected_updated_at:info.paperExpectedUpdatedAt,historical_only:info.historicalOnly,sync_stage:$('s-sync-stage')?.checked||false,
      ended_on:closed?$('s-end').value:null,result:closed?$('s-result').value:null,transfer_mode:$('s-transfer').value,wait_days:$('s-transfer').value==='days'?Number($('s-wait').value):null,
      target_on:$('s-transfer').value==='date'?$('s-target').value:null,candidate_ids:[...document.querySelectorAll('.candidate-select')].map(s=>Number(s.value))};
    await api(info.roundId?`/api/submissions/${info.roundId}`:`/api/papers/${info.paperId}/submissions`,info.roundId?'PUT':'POST',data,info.operationId);
    drawerDirty=false;await afterSave('投稿记录已保存',info.paperId);
  }catch(error){formError(error);}finally{saving=false;if(button.isConnected){button.disabled=false;button.textContent='保存记录';}}
}

async function transferForm(paperId,roundId) {
  const request=++drawerRequestGeneration;
  const p=await api(`/api/papers/${paperId}`);
  if(request!==drawerRequestGeneration)return;
  const r=p.submissions.find(row=>row.id===roundId);
  if(!r)throw new Error('原投稿记录已不存在，请刷新。');
  const target=r.candidates.find(j=>j.id!==r.journal_id)?.id||'';
  const visited=[...new Set(p.submissions.map(row=>row.journal_name))];
  showDrawer(drawerHeader('登记实际转投',`稿件 ${p.paper_code} · ${p.title}`)+`<div class="drawer-body"><form id="transfer-form"><div id="form-error" class="form-error" hidden></div>
    <div class="mini-banner">此操作用于已经实际投出的下一轮投稿。保存后保留原轮次，并开始新期刊的等待计时。</div>
    <fieldset class="form-section"><legend>上一轮 · ${esc(r.journal_name)}</legend>
    ${r.ended_on?`<p class="field-help">本轮已于 ${esc(r.ended_on)} 结束，结果：${esc(results[r.result])}。原记录保持不变。</p>`:`<div class="form-grid"><div class="form-field"><label for="t-end">上一轮结束日期 <span class="required">*</span></label><input id="t-end" type="date" required min="${r.submitted_on}" max="${state.today}" value="${state.today}"></div><div class="form-field"><label for="t-result">上一轮结果</label><select id="t-result"><option value="rejected">拒稿</option><option value="withdrawn">撤稿</option><option value="other">其他结束</option></select></div></div>`}
    </fieldset><fieldset class="form-section"><legend>新一轮投稿</legend><div class="form-field"><label for="t-journal">实际转投期刊 <span class="required">*</span></label><select id="t-journal" class="journal-select" required>${journalOptions(target)}</select><small>默认带出首选候选期刊，可以修改。</small></div>${quickJournalForm('t-journal')}
    <div class="form-grid"><div class="form-field"><label for="t-date">新投稿日期 <span class="required">*</span></label><input id="t-date" type="date" required min="${r.ended_on||state.today}" max="${state.today}" value="${state.today}"></div><div class="form-field"><label for="t-number">新期刊稿号（选填）</label><input id="t-number" maxlength="200" placeholder="期刊系统给出的稿号"></div></div>
    <div class="form-field"><label for="t-progress">新轮次进展</label><input id="t-progress" maxlength="1000" placeholder="例如：已收稿"></div>
    <div class="history-note">已投过的期刊：${visited.map(esc).join(' · ')}</div><p id="t-visited-note" class="field-help"></p>
    <p class="field-help">稿件编号 ${esc(p.paper_code)} 保持不变。新轮次不自动沿用旧的转投期限；剩余候选顺序会保留，可在新轮次中继续调整。</p></fieldset></form></div>`+formActions('transfer-form','返回详情'),{kind:'transfer-form',paperId,roundId,expectedUpdatedAt:r.updated_at,paperExpectedUpdatedAt:p.updated_at});
  const sync=()=>{if($('t-end')){$('t-date').min=$('t-end').value;}const journal=state.journals.find(j=>j.id===Number($('t-journal').value));$('t-visited-note').textContent=journal&&visited.includes(journal.name)?'这家期刊已有投稿记录。请确认这里登记的是一次新的实际投稿。':'';};
  $('t-end')?.addEventListener('input',sync);$('t-journal').addEventListener('change',sync);sync();
  $('transfer-form').addEventListener('submit',async event=>{
    event.preventDefault();saving=true;const button=document.querySelector('[form="transfer-form"]');button.disabled=true;button.textContent='保存中…';
    try{
      const info={...drawerInfo};const journalId=Number($('t-journal').value);
      await api(`/api/submissions/${roundId}/transfer`,'POST',{expected_updated_at:info.expectedUpdatedAt,paper_expected_updated_at:info.paperExpectedUpdatedAt,
        ended_on:r.ended_on||$('t-end').value,result:r.result||$('t-result').value,
        next_submission:{journal_id:journalId,submitted_on:$('t-date').value,manuscript_number:$('t-number').value,progress:$('t-progress').value,transfer_mode:'none',candidate_ids:r.candidates.map(j=>j.id).filter(id=>id!==journalId)}},info.operationId);
      drawerDirty=false;await afterSave('转投已登记，旧轮次已保留，新轮次开始计时',paperId);
    }catch(error){formError(error);}finally{saving=false;if(button.isConnected){button.disabled=false;button.textContent='保存记录';}}
  });
}

async function confirmAction(title,body,label='确认') {
  if(modalResolve)return false;
  $('modal-title').textContent=title;$('modal-body').textContent=body;$('modal-ok').textContent=label;
  $('modal-layer').hidden=false;
  $('modal-cancel').focus();
  return new Promise(resolve=>{modalResolve=resolve;});
}
function resolveModal(value){if(!modalResolve)return;const resolve=modalResolve;modalResolve=null;$('modal-layer').hidden=true;resolve(value);$('drawer-layer').hidden?$('content').querySelector('button')?.focus():$('drawer').focus();}
$('modal-cancel').addEventListener('click',()=>resolveModal(false));
$('modal-ok').addEventListener('click',()=>resolveModal(true));

async function restorePayload(payload) {
  if(payload?.app!=='paperdesk-local-v1'||![1,2,3,4].includes(payload.schema_version)||!payload.data)throw new Error('请选择本平台导出的完整 JSON 备份。');
  const {counts}=await api('/api/restore/preview','POST',payload);
  if(!await confirmRestore(counts))return;
  const result=await api('/api/restore','POST',{...payload,allow_empty:counts.papers===0});
  filters={search:'',stage:'',journal:'',overdue:false,sort:'recent'};
  await afterSave(`已恢复 ${result.counts.papers} 篇论文、${result.counts.submissions} 轮投稿`);
}

async function confirmRestore(counts,description='所选 JSON 备份') {
  const empty=counts.papers===0&&state.papers.length>0;
  return confirmAction(empty?'这份备份没有论文，仍要恢复？':'恢复这份备份？',`${description}\n已校验：${counts.papers} 篇论文、${counts.journals} 个期刊、${counts.submissions} 轮投稿、${counts.files} 个文件关联。\n${empty?`恢复后当前 ${state.papers.length} 篇论文将从列表中移除。\n`:''}恢复会替换当前记录，不会自动备份；需要保留当前状态时请先导出。稿件文件不会改变。`,empty?'确认恢复空备份':'恢复数据');
}

$('import-file').addEventListener('change',async e=>{
  const file=e.target.files[0];if(!file)return;
  try{if(file.size>20*1024*1024)throw new Error('备份文件不能超过 20 MB。');let payload;try{payload=JSON.parse(await file.text());}catch{throw new Error('文件不是有效的 JSON 备份，当前数据没有改变。');}await restorePayload(payload);}
  catch(error){toast(error.message,true);}finally{e.target.value='';}
});

document.addEventListener('click',async event=>{
  const button=event.target.closest('[data-action],[data-view]');if(!button||button.disabled)return;
  try {
    if(button.dataset.view){if(!await allowNavigation())return;await closeDrawer(true);currentView=button.dataset.view;renderView();return;}
    const action=button.dataset.action,id=Number(button.dataset.id);
    if(['new-paper','paper-detail','edit-paper','new-journal','edit-journal','new-submission','edit-submission','end-submission','transfer-submission'].includes(action)&&!await allowNavigation())return;
    if(action==='dismiss-toast')$('toast').hidden=true;
    else if(action==='toggle-backups'){const list=$('older-backups');list.hidden=!list.hidden;button.setAttribute('aria-expanded',String(!list.hidden));button.textContent=list.hidden?`更多备份（${button.dataset.count} 份）`:'收起其他备份';}
    else if(action==='reload-record'){if(!await allowNavigation())return;const info={...drawerInfo};if(info.kind==='paper-form')await paperForm(info.paperId);else if(info.kind==='journal-form')journalForm(info.journalId);else if(info.kind==='transfer-form')await transferForm(info.paperId,info.roundId);else await submissionForm(info.paperId,info.roundId);}
    else if(action==='new-paper')await paperForm();
    else if(action==='paper-detail')await paperDetail(id);
    else if(action==='edit-paper')await paperForm(id);
    else if(action==='close-drawer')await closeDrawer();
    else if(action==='form-back'){if(!await allowNavigation())return;if(drawerInfo.paperId)await paperDetail(drawerInfo.paperId);else await closeDrawer(true);}
    else if(action==='new-journal')journalForm();
    else if(action==='edit-journal')journalForm(id);
    else if(action==='new-submission')await submissionForm(id);
    else if(action==='transfer-submission')await transferForm(Number(button.dataset.paper),id);
    else if(action==='edit-submission'||action==='end-submission')await submissionForm(Number(button.dataset.paper),id,action==='end-submission');
    else if(action==='filter-stage'){const chosen=button.dataset.stage;filters.stage=filters.stage===chosen?'':chosen;renderPapers();document.querySelector(`[data-action="filter-stage"][data-stage="${chosen}"]`)?.focus();}
    else if(action==='show-overdue'){filters.overdue=true;filters.stage='';filters.search='';filters.journal='';renderPapers();}
    else if(action==='clear-filters'){filters={search:'',stage:'',journal:'',overdue:false,sort:'recent'};renderPapers();}
    else if(action==='add-file-row'){$('file-rows').insertAdjacentHTML('beforeend',fileEditRow());$('file-rows').lastElementChild.querySelector('.file-path').focus();drawerDirty=true;}
    else if(action==='remove-file-row'){button.closest('[data-file-row]').remove();drawerDirty=true;}
    else if(['pick-folder','pick-file','choose-and-add-file'].includes(action))await selectLocalPath(button,action);
    else if(action==='open-folder'||action==='open-file'||action==='open-storage')await api('/api/open','POST',{kind:action==='open-folder'?'paper':action==='open-file'?'file':'storage',id:action==='open-storage'?button.dataset.kind:id});
    else if(action==='add-candidate'){$('candidate-rows').insertAdjacentHTML('beforeend',candidateRow());syncCandidateNumbers();drawerDirty=true;}
    else if(action==='remove-candidate'){button.closest('[data-candidate-row]').remove();syncCandidateNumbers();drawerDirty=true;}
    else if(action==='candidate-up'||action==='candidate-down'){const row=button.closest('[data-candidate-row]');if(action==='candidate-up'&&row.previousElementSibling)row.parentNode.insertBefore(row,row.previousElementSibling);else if(action==='candidate-down'&&row.nextElementSibling)row.parentNode.insertBefore(row.nextElementSibling,row);syncCandidateNumbers();drawerDirty=true;}
    else if(action==='quick-journal'){
      const name=$('quick-name').value.trim();if(!name)throw new Error('请填写新期刊的名称。');
      const generation=drawerGeneration;const quickInfo=drawerInfo;quickInfo.quickOperationId||=crypto.randomUUID();
      const saved=await api('/api/journals','POST',{name,url:$('quick-url').value,notes:''},quickInfo.quickOperationId);
      await refresh(false);
      if(generation!==drawerGeneration){toast('期刊已保存，可在期刊管理中查看。');return;}
      document.querySelectorAll('.journal-select').forEach(select=>{const value=select.value;select.innerHTML=journalOptions(value,select.id==='p-target-journal'?'暂不设置':'请选择期刊');});
      $(button.dataset.target).value=saved.id;$(button.dataset.target).dispatchEvent(new Event('change',{bubbles:true}));$('quick-name').value='';$('quick-url').value='';button.closest('details').open=false;drawerInfo.quickOperationId=null;drawerDirty=true;toast('新期刊已保存并选中');
    }
    else if(action==='delete-paper'){
      if(!await confirmAction('删除这篇论文及全部投稿记录？','将删除论文与全部投稿记录，不会自动备份。原稿件文件保留；若暂时不用，建议设为归档。需要保留当前状态时请先导出备份。','删除记录'))return;
      await api(`/api/papers/${id}`,'DELETE',{expected_updated_at:drawerInfo.expectedUpdatedAt||drawerInfo.paper?.updated_at},drawerInfo.operationId);await afterSave('论文记录已删除');
    }
    else if(action==='delete-journal'){
      if(!await confirmAction('删除这个期刊？','期刊如已关联投稿或转投候选，平台会保留它并提示修改关联。','删除期刊'))return;
      const returnPaper=drawerInfo?.paperId;await api(`/api/journals/${id}`,'DELETE',{expected_updated_at:drawerInfo.expectedUpdatedAt},drawerInfo.operationId);await afterSave('期刊已删除',returnPaper);
    }
    else if(action==='delete-submission'){
      if(!await confirmAction('删除这轮投稿记录？','此操作不自动备份。如果是退稿、撤稿或录用，建议通过“管理当前投稿”将本轮设为“已结束”，以保留记录。','删除记录'))return;
      await api(`/api/submissions/${id}`,'DELETE',{expected_updated_at:drawerInfo.expectedUpdatedAt,paper_expected_updated_at:drawerInfo.paperExpectedUpdatedAt},drawerInfo.operationId);drawerDirty=false;await afterSave('这轮投稿记录已删除',Number(button.dataset.paper));
    }
    else if(action==='export'){
      const payload=await api('/api/export');const blob=new Blob([JSON.stringify(payload,null,2)],{type:'application/json;charset=utf-8'});if(blob.size>(state.json_limit_bytes||20*1024*1024))throw new Error('完整备份超过 JSON 恢复上限，请从备份文件夹保留 SQLite 完整快照。');const url=URL.createObjectURL(blob);const anchor=document.createElement('a');anchor.href=url;anchor.download=`论文管理台-完整备份-${state.today}.json`;document.body.append(anchor);anchor.click();anchor.remove();setTimeout(()=>URL.revokeObjectURL(url),10000);toast('完整备份已导出');
    }
    else if(action==='refresh-backup'){button.disabled=true;button.textContent='备份中…';try{await api('/api/backups/refresh','POST',{});await renderBackup();toast('完整备份已生成');}finally{if(button.isConnected){button.disabled=false;button.textContent='立即生成完整备份';}}}
    else if(action==='import')$('import-file').click();
    else if(action==='restore-snapshot'){
      const selected=state.backups.find(b=>b.name===button.dataset.name);
      const description=selected?`${backupLabel(selected.name)} · ${selected.modified_at.slice(0,19).replace('T',' ')}`:'所选备份';
      const {counts}=await api('/api/backups/preview','POST',{name:button.dataset.name});
      if(!await confirmRestore(counts,description))return;
      const result=await api('/api/backups/restore','POST',{name:button.dataset.name,allow_empty:counts.papers===0});filters={search:'',stage:'',journal:'',overdue:false,sort:'recent'};await afterSave(`已恢复 ${result.counts.papers} 篇论文、${result.counts.submissions} 轮投稿`);
    }
    else if(action==='reload')location.reload();
  }catch(error){formError(error);}
});

$('quit').addEventListener('click',async()=>{
  if(!await allowNavigation())return;
  if(!await confirmAction('退出论文管理平台？','已保存的记录保留在本机。下次双击启动入口即可继续使用。','退出平台'))return;
  try{await api('/api/shutdown','POST',{});await closeDrawer(true);disconnected=true;$('content').innerHTML='<div class="connection-error"><h2>平台已关闭</h2><p>你的记录已经保存在本机。<br>下次双击“启动论文管理平台”即可继续使用。</p></div>';$('quit').disabled=true;document.querySelectorAll('[data-view]').forEach(b=>{b.disabled=true;});}
  catch(error){toast(error.message,true);}
});

document.addEventListener('keydown',event=>{
  if(event.key==='Escape'){if(modalResolve)resolveModal(false);else if(!$('drawer-layer').hidden)closeDrawer();}
  if(event.key==='Tab'){
    const scope=modalResolve?document.querySelector('.modal'):!$('drawer-layer').hidden?$('drawer'):null;
    if(!scope)return;
    const nodes=[...scope.querySelectorAll('button:not([disabled]),a[href],input:not([disabled]),select:not([disabled]),textarea:not([disabled]),summary')].filter(el=>el.getClientRects().length);
    const first=nodes[0],last=nodes.at(-1);
    if(event.shiftKey&&(document.activeElement===first||document.activeElement===scope)){event.preventDefault();last?.focus();}
    else if(!event.shiftKey&&(document.activeElement===last||document.activeElement===scope)){event.preventDefault();first?.focus();}
  }
});
window.addEventListener('beforeunload',event=>{if(drawerDirty||saving){event.preventDefault();event.returnValue='';}});
async function refreshWhenVisible(){
  if(disconnected||refreshing||drawerDirty||saving||document.visibilityState!=='visible'||!state||['INPUT','TEXTAREA','SELECT'].includes(document.activeElement?.tagName))return;
  refreshing=true;
  const generation=drawerGeneration;
  const detailId=drawerInfo?.kind==='detail'?drawerInfo.paperId:null;
  try{await refresh();if(detailId&&drawerGeneration===generation)await paperDetail(detailId,generation);}
  catch{/* Preserve the last readable state during temporary disconnects. */}
  finally{refreshing=false;}
}
window.addEventListener('focus',refreshWhenVisible);
window.addEventListener('resize',syncTableOverflow);
document.addEventListener('visibilitychange',refreshWhenVisible);
setInterval(refreshWhenVisible,60000);
refresh().catch(error=>{disconnected=true;$('content').innerHTML=`<div class="connection-error"><h2>暂时无法读取记录</h2><p>${esc(error.message)}</p><button class="button primary" data-action="reload">重新连接</button></div>`;});
