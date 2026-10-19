'use strict';
// Runs the production rendering and save functions with isolated synthetic data.
// No browser, installed frontend packages, or real PaperDesk records are used.
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const assert=require('node:assert/strict');
const source=fs.readFileSync(path.join(__dirname,'web','app.js'),'utf8');
const chunk=(a,b)=>{const start=source.indexOf(a);const end=source.indexOf(b,start);assert(start>=0&&end>start);return source.slice(start,end);};
const output=[];
function checked(name,fn){fn();output.push(name);}
function paper(stage,id=1){return {id,paper_code:`P${String(id).padStart(4,'0')}`,title:`人工测试论文${id}`,authors:'演示作者',tags:'',stage,stage_started_on:'2026-10-01',stage_elapsed_days:16,target_journal:{id:1,name:'目标期刊',url:''},round_count:['planned','writing'].includes(stage)?0:1,overdue_days:0,journal_numbers:['OLD-J-001'],past_journals:['曾投期刊'],current_submission:['planned','writing'].includes(stage)?null:{journal_id:1,journal_name:'期刊',journal_url:'',submitted_on:'2026-10-01',elapsed_days:16,ended_on:['accepted','archived'].includes(stage)?'2026-10-17':null,target_on:null,candidates:[]}};}
function renderer(papers,filters={}){
  const nodes={};
  const context={state:{papers},filters:{search:'',stage:'',journal:'',overdue:false,sort:'recent',...filters},$:(id)=>nodes[id]??={innerHTML:'',textContent:''}};
  vm.createContext(context);
  vm.runInContext(chunk('const esc =','const icon =')+chunk('function stagePill(','function header(')+chunk('function earlyStage(','function renderJournals(')+"\nconst stages={planned:'计划中',writing:'正在写',submitted:'已投稿',revision:'退修中',accepted:'已录用/发表',archived:'归档'};renderPaperTable();",context);
  return {html:nodes['paper-table'].innerHTML,context,nodes};
}

for(const stage of ['planned','writing','submitted','revision','accepted','archived']){
  checked(`${stage}: date and elapsed wording`,()=>{
    const {html}=renderer([paper(stage)],{stage});
    const headers=[...html.matchAll(/<th>(.*?)<\/th>/g)].map(m=>m[1]);
    const cells=[...html.matchAll(/<td(?: [^>]+)?>(.*?)<\/td>/g)].map(m=>m[1]);
    const title=stage==='planned'?'已历时':stage==='writing'?'写作天数':['accepted','archived'].includes(stage)?'本轮用时':'等待天数';
    assert.equal(headers[4],title);
    assert(!cells[4].includes(title),'Do not duplicate the header below the day count');
    assert(!html.includes('已等待'));
    if(['planned','writing'].includes(stage))assert(!headers.includes('计划转投'));
    else assert.equal(headers[3],'投稿日期');
  });
}
checked('visible stage controls headers after deselecting the card',()=>{
  const {html}=renderer([paper('writing')]);
  assert(html.includes('<th>开始撰写日期</th>'));
  assert(html.includes('<th>初始目标期刊 / 投稿入口</th>'));
});
checked('mixed list retains context without changing submission date semantics',()=>{
  const {html}=renderer([paper('writing',1),paper('submitted',2)]);
  assert(html.includes('<div class="paper-meta">写作</div>'));
  assert(html.includes('<div class="paper-meta">投稿日期</div>'));
});
checked('identifier search supports full width and case normalization',()=>{
  const {html}=renderer([paper('writing',1),paper('writing',2)],{search:'ｐ０００２'});
  assert(html.includes('人工测试论文2'));
  assert(!html.includes('人工测试论文1'));
});
checked('old journal manuscript numbers remain searchable',()=>{
  const p=paper('writing',1),q=paper('writing',2);q.journal_numbers=[];
  const {html}=renderer([p,q],{search:'old-j-001'});
  assert(html.includes('人工测试论文1'));
  assert(!html.includes('人工测试论文2'));
});
checked('writing sort uses writing elapsed time',()=>{
  const a=paper('writing',1),b=paper('writing',2);a.stage_elapsed_days=2;b.stage_elapsed_days=80;
  const {html}=renderer([a,b],{stage:'writing',sort:'wait'});
  assert(html.indexOf('人工测试论文2')<html.indexOf('人工测试论文1'));
});
checked('identifier and previously submitted journals are displayed and escaped',()=>{
  const p=paper('writing');p.title='<unsafe>';p.past_journals=['<journal>'];
  const {html}=renderer([p]);
  assert(html.includes('paper-code">P0001'));
  assert(html.includes('已投期刊：&lt;journal&gt;'));
  assert(!html.includes('<unsafe>'));
});
checked('empty filtered list explains records remain saved',()=>{
  const {html}=renderer([paper('writing')],{search:'not-present'});
  assert(html.includes('记录仍然保留'));
  assert(html.includes('清除筛选'));
});


for(const count of [0,1,3,4,5,8]){
  checked(`backup folding boundary: ${count} snapshots`,()=>{
    const backups=Array.from({length:count},(_,i)=>({name:`paper-added-2026-10-20-${i}.sqlite3`,counts:{papers:i+1,journals:1,submissions:0},modified_at:'2026-10-20T12:00:00',size:64000}));
    const context={esc:v=>String(v)};vm.createContext(context);
    const html=vm.runInContext(chunk('function backupLabel(','async function renderBackup(')+`\nbackupList(${JSON.stringify(backups)})`,context);
    assert.equal((html.match(/class="backup-row"/g)||[]).length,count);
    if(count<=4)assert(!html.includes('toggle-backups'));
    else {assert(html.includes('aria-expanded="false"'));assert(html.includes(`更多备份（${count-3} 份）`));const before=html.split('<div id="older-backups" hidden>')[0];assert.equal((before.match(/class="backup-row"/g)||[]).length,3);}
  });
}

async function testSaves(){
  const values={'p-code':'0001','p-title':'保存测试','p-authors':'作者','p-tags':'','p-stage':'writing','p-notes':'备注','p-folder':'','p-target-journal':'','p-planned-on':'','p-writing-on':'2026-10-17'};
  const nodes=Object.fromEntries(Object.entries(values).map(([id,value])=>[id,{value}]));
  const button={disabled:false,isConnected:true};
  let creates=0,closed=false,message='',saveError='';
  const context={saving:false,drawerDirty:true,drawerInfo:{paperId:null,operationId:'logical-save-001'},lastWriteWarning:null,
    $:id=>nodes[id],document:{querySelector:()=>button,querySelectorAll:()=>[]},
    api:async(_path,_method,_data,operationId)=>{assert.equal(operationId,'logical-save-001');creates++;return{id:20};},
    closeDrawer:async()=>{closed=true;button.isConnected=false;context.drawerInfo=null;},refresh:async()=>{throw Error('simulated refresh failure');},paperDetail:async()=>{},toast:m=>message=m,formError:e=>saveError=e.message};
  vm.createContext(context);
  vm.runInContext(chunk('async function afterSave(','async function paperDetail(')+chunk('async function savePaper(','function journalForm('),context);
  await context.savePaper({preventDefault(){}});
  assert.equal(creates,1);assert(closed);assert(message.includes('已保存'));assert(message.includes('刷新失败'));assert.equal(saveError,'');
  output.push('committed save closes the creation form even when refresh fails');
  context.drawerInfo={paperId:20,operationId:'logical-edit-002',expectedUpdatedAt:'old'};context.drawerDirty=true;button.isConnected=true;closed=false;
  context.api=async()=>{const e=Error('记录已在另一页修改');e.code='conflict';throw e;};
  await context.savePaper({preventDefault(){}});
  assert(!closed);assert(context.drawerDirty);assert.equal(nodes['p-notes'].value,'备注');assert(saveError.includes('另一页'));
  output.push('conflicting save preserves the unsaved form');
}

async function testReceipt(){
  const requests=[];
  const context={BUILD_VERSION:'1.3.0',state:{token:'test'},lastWriteWarning:null,fetch:async(url,options)=>{requests.push({url,options});if(requests.length===1)throw Error('response lost');return{ok:true,json:async()=>({found:true,result:{id:50},backup_warning:null})};}};
  vm.createContext(context);vm.runInContext(chunk('async function api(','function toast('),context);
  const result=await context.api('/api/papers','POST',{title:'测试'},'receipt-test-001');
  assert.equal(result.id,50);assert.equal(requests[0].options.headers['X-PaperDesk-Operation'],'receipt-test-001');assert(requests[1].url.endsWith('/receipt-test-001'));
  output.push('lost mutation response is recovered through its stable receipt');
}

async function testVersions(){
  const nodes={today:{},content:{}};
  const context={BUILD_VERSION:'1.3.0',state:null,versionMismatch:false,disconnected:false,currentView:'papers',$:id=>nodes[id],document:{querySelector:()=>({}),querySelectorAll:()=>[]},api:async()=>({app_version:'1.2.1',today:'2026-10-17'}),renderPapers:()=>{throw Error('Must not render incompatible controls');}};
  vm.createContext(context);vm.runInContext(chunk('const esc =','const icon =')+chunk('async function refresh(','function stagePill(')+chunk('function renderView()','function renderPapers()'),context);
  await context.refresh();assert(context.versionMismatch);assert(nodes.content.innerHTML.includes('重新启动'));
  assert(source.includes(`const BUILD_VERSION='${fs.readFileSync(path.join(__dirname,'VERSION'),'utf8').trim()}'`));
  output.push('version mismatch blocks incompatible controls and identifies restart');
}

async function testTransferForm(closed){
  const r={id:11,paper_id:10,journal_id:1,journal_name:'原期刊',submitted_on:'2026-09-01',ended_on:closed?'2026-10-15':null,result:closed?'rejected':null,updated_at:'round-version',candidates:[{id:2,name:'首选期刊'},{id:3,name:'剩余候选'}]};
  const p={id:10,paper_code:'P0010',title:'人工转投测试',updated_at:'paper-version',submissions:[r]};
  const nodes={};let html='',sent=null,completed=false;
  const button={disabled:false,isConnected:true};
  const context={drawerRequestGeneration:0,drawerInfo:null,drawerDirty:false,saving:false,state:{today:'2026-10-17',journals:[{id:1,name:'原期刊'},{id:2,name:'首选期刊'},{id:3,name:'剩余候选'}]},results:{rejected:'拒稿'},
    $:id=>nodes[id],document:{querySelector:()=>button},
    api:async(url,method,data,key)=>{if(!method)return p;sent={url,method,data,key};return{id:12,paper_id:10};},
    showDrawer:(markup,info)=>{html=markup;context.drawerInfo={...info,operationId:'transfer-form-001'};
      for(const match of markup.matchAll(/<([a-z]+)\b[^>]*\bid="([^"]+)"[^>]*>/g)){
        const [tag,type,id]=match;const el={value:tag.match(/\bvalue="([^"]*)"/)?.[1]||'',events:{},addEventListener(name,fn){this.events[name]=fn;}};
        if(type==='select'){const body=markup.slice(match.index+tag.length).split('</select>')[0];el.value=body.match(/<option value="([^"]*)"[^>]*selected/)?.[1]??body.match(/<option value="([^"]*)"/)?.[1]??'';}
        nodes[id]=el;
      }
    },afterSave:async(message,id)=>{assert.equal(id,10);completed=true;},formError:error=>{throw error;}};
  vm.createContext(context);
  vm.runInContext(chunk('const esc =','const icon =')+chunk('function drawerHeader(','function showDrawer(')+chunk('function formActions(','async function afterSave(')+chunk('function journalOptions(','async function submissionForm(')+chunk('async function transferForm(','async function confirmAction('),context);
  await context.transferForm(10,11);
  assert.equal(nodes['t-journal'].value,'2');assert(html.includes('稿件 P0010'));assert(html.includes('原期刊'));
  assert.equal(!!nodes['t-end'],!closed);
  if(nodes['t-end'])nodes['t-end'].value='2026-10-15';
  nodes['t-date'].value='2026-10-17';nodes['t-number'].value='NEW-J-001';nodes['t-progress'].value='已收稿';
  await nodes['transfer-form'].events.submit({preventDefault(){}});
  assert(completed);assert.equal(sent.url,'/api/submissions/11/transfer');assert.equal(sent.key,'transfer-form-001');
  assert.equal(sent.data.expected_updated_at,'round-version');assert.equal(sent.data.paper_expected_updated_at,'paper-version');
  assert.equal(sent.data.next_submission.journal_id,2);assert.equal(sent.data.next_submission.manuscript_number,'NEW-J-001');
  assert.equal(sent.data.ended_on,'2026-10-15');assert.equal(sent.data.result,'rejected');
  assert.equal(JSON.stringify(sent.data.next_submission.candidate_ids),'[3]');
  output.push(`${closed?'already ended':'active'} transfer form selects the first candidate and retains remaining order`);
}

async function testPickerLifecycle(mode){
  let resolvePick;const waitPick=new Promise(resolve=>{resolvePick=resolve;});const added=[];
  const button={textContent:'选择文件并关联',disabled:false,isConnected:true};
  const context={button,drawerGeneration:4,drawerDirty:false,toast(){},api:()=>waitPick,fileEditRow:entry=>JSON.stringify(entry),$:()=>({insertAdjacentHTML(_position,html){added.push(JSON.parse(html));}})};
  vm.createContext(context);vm.runInContext(chunk('async function selectLocalPath(','function fileEditRow('),context);
  const pending=vm.runInContext("selectLocalPath(button,'choose-and-add-file')",context);
  assert(button.disabled);
  if(mode==='switched')context.drawerGeneration=5;
  resolvePick(mode==='cancel'?{}:{path:'C:/papers/final.txt'});await pending;
  if(mode==='selected'){assert.equal(added.length,1);assert.equal(added[0].label,'final.txt');assert.equal(added[0].path,'C:/papers/final.txt');assert(context.drawerDirty);}
  else{assert.equal(added.length,0);assert.equal(context.drawerDirty,false);}
  assert.equal(button.disabled,false);assert.equal(button.textContent,'选择文件并关联');
  output.push(`file picker lifecycle: ${mode}`);
}

(async()=>{await testPickerLifecycle('selected');await testPickerLifecycle('cancel');await testPickerLifecycle('switched');await testSaves();await testReceipt();await testVersions();await testTransferForm(false);await testTransferForm(true);console.log(`${output.length} frontend checks passed`);for(const name of output)console.log(`  PASS ${name}`);})().catch(error=>{console.error(error);process.exitCode=1;});
