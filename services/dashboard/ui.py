"""Dashboard page (read-only except HUM-1 approve/reject and the HUM-3
operator controls). Single file, no external assets — served from the
approval surface itself."""

PAGE_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FORGE Readiness Console</title>
<style>
:root{--bg:#101418;--panel:#1a2027;--ink:#e6ebef;--mut:#93a1ad;--line:#2c3640;
--ok:#5fbf8b;--warn:#d9b45e;--bad:#e08a5e;--acc:#6fa3c7}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.55 system-ui,sans-serif;padding:1.2rem}
h1{font-size:1.25rem;margin:0 0 1rem}h2{font-size:1rem;margin:0 0 .5rem;color:var(--acc)}
.grid{display:grid;gap:1rem;grid-template-columns:280px 1fr}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding: .9rem 1rem;margin-bottom:1rem}
table{border-collapse:collapse;width:100%;font-size:.86rem}
td,th{padding:.3rem .5rem;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
th{color:var(--mut);font-weight:600;text-transform:uppercase;font-size:.7rem;letter-spacing:.06em}
.tag{display:inline-block;border-radius:999px;padding:.05rem .5rem;font-size:.72rem;background:#243040}
.tag.RELEASED,.tag.COMPLETED,.tag.APPROVED,.tag.HEALTHY,.tag.ACTIVE{background:#1e3328;color:var(--ok)}
.tag.BLOCKED_AGENT_FAILURE,.tag.FAILED,.tag.vetoed{background:#3a281e;color:var(--bad)}
.tag.STALE,.tag.RESERVE,.tag.SUSPENDED_AWAITING_PART{background:#33301d;color:var(--warn)}
.tag.CANCELLED{background:#2b2f36;color:var(--mut)}
button{background:var(--acc);border:0;color:#0d1216;font-weight:700;border-radius:6px;
padding:.4rem .9rem;cursor:pointer;margin-right:.5rem}
button.reject,button.danger{background:var(--bad)}
button.small{padding:.1rem .5rem;font-size:.72rem;font-weight:600;margin:0 .15rem 0 0}
input,select{background:#12181e;border:1px solid var(--line);color:var(--ink);
border-radius:6px;padding:.35rem .5rem;font-size:.85rem;width:100%;margin:.15rem 0 .4rem}
label{font-size:.7rem;color:var(--mut);text-transform:uppercase;letter-spacing:.06em}
li{margin:.15rem 0}.mut{color:var(--mut)}pre{white-space:pre-wrap;font-size:.8rem}
a{color:var(--acc);cursor:pointer}
.headrow{display:flex;align-items:baseline;gap:1rem;flex-wrap:wrap}
.clockchip{background:#243040;border-radius:999px;padding:.1rem .7rem;font-size:.8rem}
.lanes{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:.6rem}
.lane{border:1px solid var(--line);border-radius:8px;padding:.55rem .7rem;background:#12181e;cursor:pointer}
.lane.selected{outline:2px solid var(--acc)}
.lane.working{border-color:var(--acc);animation:lanepulse 1.6s ease-in-out infinite}
@keyframes lanepulse{0%,100%{box-shadow:0 0 0 0 rgba(111,163,199,0)}50%{box-shadow:0 0 0 4px rgba(111,163,199,.25)}}
.lane.flash{animation:laneflash 1.2s ease-out 1}
@keyframes laneflash{0%{box-shadow:0 0 0 4px rgba(95,191,139,.55)}100%{box-shadow:0 0 0 0 rgba(95,191,139,0)}}
.lane.failedlane{border-color:var(--bad)}
.lane .role{font-weight:700;font-size:.85rem}
.lane .nowline{font-size:.78rem;min-height:2.3em;margin:.25rem 0}
.lane .mini{font-size:.68rem;color:var(--mut);border-top:1px solid var(--line);padding-top:.25rem}
.mini .new{animation:slidein .35s ease-out 1}
@keyframes slidein{from{opacity:0;transform:translateY(-4px)}to{opacity:1;transform:none}}
@media (prefers-reduced-motion: reduce){.lane,.mini .new{animation:none}}
</style></head><body>
<div class="headrow">
<h1>FORGE Readiness Console <span class="mut">· governed operator console (HUM-1 / HUM-3)</span></h1>
<span class="clockchip">Day <span id="clockday">—</span></span>
<span class="mut" id="operator"></span>
</div>
<div class="grid">
<div>
  <div class="panel"><h2>Operator Controls (HUM-3)</h2>
   <label>Equipment</label>
   <select id="op-equip"></select>
   <label>Discrepancy code</label>
   <input id="op-dsc" value="DSC-0042">
   <label>Description</label>
   <input id="op-desc" value="Failed hydraulic actuator on lift assembly">
   <p><button onclick="startWorkflow()">Report NMC</button></p>
   <label>Advance clock (days)</label>
   <input id="op-days" value="21">
   <p><button onclick="advanceClock()">Advance</button></p>
  </div>
  <div class="panel"><h2>Workflows</h2><ul id="wfs"></ul></div>
  <div class="panel"><h2>Agent Catalog (REG-4)</h2><div id="catalog"></div></div>
</div>
<div id="detail"><div class="panel mut">Select a workflow.</div></div>
</div>
<div class="panel"><h2>Agent Operations <span class="mut" id="lanefilter"></span></h2>
 <div id="agentstrip" class="lanes mut">—</div></div>
<div class="panel"><h2>Live Agent Activity</h2><div id="activity" class="mut">—</div></div>
<script>
let CUR=null;let FILTER=null;let LANEWAS={};
async function j(u,opt){const r=await fetch(u,opt);if(!r.ok)throw new Error(await r.text());return r.json()}
function tag(v){return `<span class="tag ${v}">${v}</span>`}
function esc(s){return String(s??'').replace(/[&<>"']/g,c=>'&#'+c.charCodeAt(0)+';')}
async function loadWorkflows(){
  const list=await j('/api/workflows');
  document.getElementById('wfs').innerHTML=list.map(w=>
    `<li><a onclick="show('${w.workflow_id}')">${w.workflow_id}</a> ${tag(w.status)}<br>
     <span class="mut">day ${w.logical_time}${w.due_at!=null?' · due '+w.due_at:''} · ${w.equipment_id}</span></li>`).join('')||'<li class="mut">none</li>';
}
async function loadCatalog(){
  const cat=await j('/api/catalog');
  document.getElementById('catalog').innerHTML=cat.map(d=>
    `<div style="margin-bottom:.6rem"><strong>${d.agent_id}</strong> v${d.version} ${tag(d.lifecycle_status)}<br>
     <span class="mut">${d.department_owner} · ${d.capabilities.join(', ')}</span><br>
     ${d.instances.map(i=>`${i.instance_id} ${tag(i.state)} ${tag(i.health)}
       ${i.state==='FAILED'?`<button class="small" onclick="instanceAction('${i.instance_id}','restore')">Restore</button>`
                           :`<button class="small danger" onclick="instanceAction('${i.instance_id}','fail')">Fail</button>`}`).join('<br>')}</div>`).join('');
}
async function loadClock(){
  try{const c=await j('/api/clock');document.getElementById('clockday').textContent=c.logical_time}catch(e){}
}
async function loadActivity(){
  const feed=await j('/api/activity?limit=25'+(FILTER?'&agent='+FILTER:''));
  document.getElementById('activity').innerHTML=feed.length?`<table>
   <tr><th>Observed</th><th>Day</th><th>Agent</th><th>Kind</th><th>Reason</th><th>Workflow</th><th>State</th></tr>
   ${feed.map(e=>`<tr><td class="mut">${e.observed_at.slice(11,19)}</td><td>${e.effective_at}</td><td>${e.agent_identity}</td><td>${e.event_kind}</td><td>${e.reason_code}</td><td class="mut">${e.workflow_id}</td><td>${e.state_after?tag(e.state_after):''}</td></tr>`).join('')}</table>`:'—';
}
// Agent Operations lanes: every rendered fact is governed state (claims,
// packages, registry, audit trail); motion is EVENT-DRIVEN only — pulse
// while a claim is live, one flash when work completes, one slide-in when
// a new event lands. Calm fleet = calm wall.
async function loadLanes(){
  const lanes=await j('/api/agents/now');
  document.getElementById('agentstrip').classList.remove('mut');
  document.getElementById('agentstrip').innerHTML=lanes.map(l=>{
    const was=LANEWAS[l.definition_id]||{};
    const working=l.now.kind==='processing'||l.now.kind==='executing';
    const flash=was.working&&!working&&l.now.kind!=='failed';
    const top=l.recent[0];const newEvt=top&&was.top!==top.observed_at+top.reason_code;
    LANEWAS[l.definition_id]={working,top:top?top.observed_at+top.reason_code:was.top};
    const cls=['lane',working?'working':'',flash?'flash':'',
      l.now.kind==='failed'?'failedlane':'',FILTER===l.definition_id?'selected':''].join(' ');
    return `<div class="${cls}" onclick="laneFilter('${l.definition_id}')">
     <div class="role">${esc(l.definition_id.replace('forge-',''))}
       <span class="mut">${esc(l.acting_instance_id||'')}</span></div>
     <div class="nowline">${working?'&#9679; ':''}${esc(l.now.text)}
       ${l.now.workflow_id?`<span class="mut">· ${esc(l.now.workflow_id)}</span>`:''}</div>
     <div class="mini">${l.recent.map((e,n)=>`<div${n===0&&newEvt?' class="new"':''}>
       ${e.observed_at.slice(11,19)} ${esc(e.reason_code)}</div>`).join('')||'&mdash;'}</div>
    </div>`}).join('');
}
function laneFilter(d){FILTER=FILTER===d?null:d;
  document.getElementById('lanefilter').textContent=FILTER?('· feed filtered: '+FILTER):'';
  loadLanes();loadActivity();}
async function whoami(){
  try{const w=await j('/api/whoami');document.getElementById('operator').textContent='operator: '+w.approver_identity}catch(e){}
}
async function show(id){
  CUR=id;
  const d=await j('/api/workflows/'+id);
  const terminal=['RELEASED','CANCELLED'].includes(d.state.status);
  const approvals=d.pending_approvals.map(p=>`
   <div class="panel"><h2>Approval required — ${p.action_type} <span class="mut">(${p.approval_id})</span></h2>
    <p><strong>${p.recommended_action}</strong> <span class="mut">confidence ${p.confidence}</span></p>
    <table>
     <tr><th>Sources</th><td>${p.source_refs.join('; ')}</td></tr>
     <tr><th>Facts</th><td><ul>${p.extracted_facts.map(f=>`<li>${f}</li>`).join('')}</ul></td></tr>
     <tr><th>Rules</th><td>${p.applicable_rules.join(', ')}</td></tr>
     <tr><th>Constraints</th><td>${(p.constraints||[]).join('; ')||'—'}</td></tr>
     <tr><th>Alternatives</th><td>${(p.alternatives_considered||[]).map(a=>`${a.option} <span class="mut">— ${a.rejected_reason}</span>`).join('<br>')||'—'}</td></tr>
     <tr><th>Versions</th><td class="mut">${p.versions.agent_id} · ${p.versions.model_id} · prompt ${p.versions.prompt_version} · ${p.versions.schema_version}</td></tr>
    </table>
    <p><button onclick="decide('${id}','${p.approval_id}','approved')">Approve</button>
       <button class="reject" onclick="decide('${id}','${p.approval_id}','rejected')">Reject</button></p>
   </div>`).join('');
  document.getElementById('detail').innerHTML=`
   <div class="panel"><h2>${id} ${tag(d.state.status)}</h2>
    <table><tr><th>Package</th><th>Role</th><th>Owner</th><th>Status</th><th>Seq</th></tr>
    ${d.work_packages.map(p=>`<tr><td>${p.work_package_id}</td><td>${p.role}</td><td>${p.owner_instance_id}${p.reassigned_from?` <span class="mut">(from ${p.reassigned_from})</span>`:''}</td><td>${tag(p.status)}</td><td>${p.assignment_seq}</td></tr>`).join('')}</table>
    ${terminal?'':`<p><button class="danger" onclick="cancelWorkflow('${id}')">Cancel workflow</button>
       <button onclick="injectBulletin('${id}')">Inject poisoned bulletin</button></p>`}
   </div>
   ${approvals}
   <div class="panel"><h2>Audit trail — reconstructed from Firestore alone (AUD-2)</h2>
    <table><tr><th>Day</th><th>Observed</th><th>Kind</th><th>Reason</th><th>Agent</th><th>State</th><th>Origin/Trust</th></tr>
    ${d.audit_trail.map(e=>`<tr><td>${e.effective_at}</td><td class="mut">${e.observed_at.slice(0,19)}</td><td>${e.event_kind}</td><td>${e.reason_code}</td><td class="mut">${e.agent_identity}</td><td>${e.state_after?tag(e.state_after):''}</td><td>${tag(e.data_origin)} ${tag(e.trust_state)}</td></tr>`).join('')}</table>
   </div>`;
}
async function decide(wf,apr,decision){
  try{await j('/api/workflows/'+wf+'/decide',{method:'POST',
    headers:{'content-type':'application/json'},
    body:JSON.stringify({approval_id:apr,decision})});
    await show(wf);await loadWorkflows();}
  catch(e){alert(e.message)}
}
async function startWorkflow(){
  try{const r=await j('/api/workflows',{method:'POST',
    headers:{'content-type':'application/json'},
    body:JSON.stringify({equipment_id:document.getElementById('op-equip').value,
      discrepancy_code:document.getElementById('op-dsc').value,
      description:document.getElementById('op-desc').value})});
    await loadWorkflows();await show(r.workflow_id);}
  catch(e){alert(e.message)}
}
async function cancelWorkflow(wf){
  if(!confirm('Cancel '+wf+'? Cancellation is terminal and audited.'))return;
  try{await j('/api/workflows/'+wf+'/cancel',{method:'POST',
    headers:{'content-type':'application/json'},body:'{}'});
    await show(wf);await loadWorkflows();}
  catch(e){alert(e.message)}
}
async function advanceClock(){
  const days=parseInt(document.getElementById('op-days').value,10);
  try{await j('/api/clock/advance',{method:'POST',
    headers:{'content-type':'application/json'},body:JSON.stringify({days})});
    await loadClock();await loadWorkflows();if(CUR)await show(CUR);}
  catch(e){alert(e.message)}
}
async function injectBulletin(wf){
  try{const r=await j('/api/anomalies/bulletin',{method:'POST',
    headers:{'content-type':'application/json'},body:JSON.stringify({workflow_id:wf})});
    alert('Bulletin quarantined ('+r.doc_id+'); verdict published: '+r.verdict_published);
    await show(wf);}
  catch(e){alert(e.message)}
}
async function instanceAction(iid,action){
  if(action==='fail'&&!confirm('Mark '+iid+' FAILED? The induction is audited.'))return;
  try{await j('/api/catalog/instances/'+iid,{method:'POST',
    headers:{'content-type':'application/json'},body:JSON.stringify({action})});
    await loadCatalog();}
  catch(e){alert(e.message)}
}
document.getElementById('op-equip').innerHTML=Array.from({length:12},(_,n)=>{
  const id='GX12-'+String(n+1).padStart(2,'0');return `<option>${id}</option>`}).join('');
whoami();loadClock();loadWorkflows();loadCatalog();loadActivity();loadLanes();
setInterval(()=>{loadWorkflows();loadCatalog();loadActivity();loadClock()},5000);
setInterval(loadLanes,3000);
</script></body></html>"""
