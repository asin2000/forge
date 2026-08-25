"""Dashboard page (read-only except HUM-1 approve/reject and the HUM-3
operator controls). Single file, no external assets — served from the
approval surface itself.

Presentation layer per the entrant's product review (2026-08-24): a
mission-story panel narrates the selected recovery in plain English; agent
lanes translate telemetry into human activity (schema names demoted to
secondary text); five hero-moment banners fire on the events the demo
hinges on; and the fleet strip plays the readiness payoff when a vehicle
returns to mission capable. Everything rendered remains a projection of
governed state — banners and stories derive from the same audit events the
trail records, never from invented client state.
"""

PAGE_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FORGE Readiness Console</title>
<style>
:root{--bg:#101418;--panel:#1a2027;--ink:#e6ebef;--mut:#93a1ad;--line:#2c3640;
--ok:#5fbf8b;--warn:#d9b45e;--bad:#e08a5e;--acc:#6fa3c7}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.55 system-ui,sans-serif;padding:1.2rem 1.2rem 185px}
h1{font-size:1.25rem;margin:0 0 1rem}h2{font-size:1rem;margin:0 0 .5rem;color:var(--acc)}
.grid{display:grid;gap:1rem;grid-template-columns:280px 1fr}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding: .9rem 1rem;margin-bottom:1rem}
table{border-collapse:collapse;width:100%;font-size:.86rem}
td,th{padding:.3rem .5rem;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
th{color:var(--mut);font-weight:600;text-transform:uppercase;font-size:.7rem;letter-spacing:.06em}
.tag{display:inline-block;border-radius:999px;padding:.05rem .5rem;font-size:.72rem;background:#243040}
.tag.RELEASED,.tag.COMPLETED,.tag.APPROVED,.tag.HEALTHY,.tag.MC{background:#1e3328;color:var(--ok)}
.tag.BLOCKED_AGENT_FAILURE,.tag.FAILED,.tag.vetoed,.tag.BLOCKED{background:#3a281e;color:var(--bad)}
.tag.STALE,.tag.RESERVE,.tag.SUSPENDED_AWAITING_PART,.tag.NMC,
.tag.AWAITING_SCHEDULE_APPROVAL,.tag.AWAITING_RELEASE_APPROVAL{background:#33301d;color:var(--warn)}
.tag.INTAKE,.tag.PLANNING,.tag.VALIDATING,.tag.ASSEMBLY_RESUMED,.tag.ACTIVE{background:#1d2c3d;color:var(--acc)}
.tag.CANCELLED,.tag.IDLE{background:#2b2f36;color:var(--mut)}
button{background:var(--acc);border:0;color:#0d1216;font-weight:700;border-radius:6px;
padding:.4rem .9rem;cursor:pointer;margin-right:.5rem}
button.reject,button.danger{background:var(--bad)}
button.big{padding:.6rem 1.6rem;font-size:1rem}
button.small{padding:.1rem .5rem;font-size:.72rem;font-weight:600;margin:0 .15rem 0 0}
input,select{background:#12181e;border:1px solid var(--line);color:var(--ink);
border-radius:6px;padding:.35rem .5rem;font-size:.85rem;width:100%;margin:.15rem 0 .4rem}
label{font-size:.7rem;color:var(--mut);text-transform:uppercase;letter-spacing:.06em}
li{margin:.15rem 0}.mut{color:var(--mut)}pre{white-space:pre-wrap;font-size:.8rem}
a{color:var(--acc);cursor:pointer}
.headrow{display:flex;align-items:baseline;gap:1rem;flex-wrap:wrap}
.clockchip{background:#243040;border-radius:999px;padding:.1rem .7rem;font-size:.8rem}
.fleet{display:flex;align-items:center;gap:.45rem;flex-wrap:wrap;margin:0 0 1rem}
.fleetchip{background:#243040;border-radius:999px;padding:.3rem 1.1rem;font-size:1rem;font-weight:700}
.fleetchip.payoff{background:#1e3328;color:var(--ok)}
.vehicle{border:1px solid var(--line);border-radius:6px;padding:.15rem .55rem;font-size:.74rem;
background:#12181e;cursor:default;text-align:center;line-height:1.35}
.vehicle.mc{border-color:#1e3328}.vehicle.mc .vstat{color:var(--ok)}
.vehicle.nmc{border-color:var(--warn);cursor:pointer}.vehicle.nmc .vstat{color:var(--warn)}
.vehicle.blocked{border-color:var(--bad);cursor:pointer}.vehicle.blocked .vstat{color:var(--bad)}
.vehicle .vstat{font-weight:700}
.vehicle.restored{animation:restored 2.4s ease-out 1}
@keyframes restored{0%{box-shadow:0 0 0 6px rgba(95,191,139,.6)}100%{box-shadow:0 0 0 0 rgba(95,191,139,0)}}
.wfitem{border-left:3px solid transparent;padding-left:.45rem}
.wfitem.selected{border-left-color:var(--acc);background:#1d2733;border-radius:4px}
.story{border:1px solid var(--acc);border-radius:8px;background:#151c24;padding:1rem 1.1rem;margin-bottom:1rem}
.story .headline{font-size:1.25rem;font-weight:700;margin-bottom:.2rem}
.story .sub{color:var(--mut);margin-bottom:.7rem}
.stages{display:flex;flex-wrap:wrap;gap:.3rem;margin:.4rem 0 .8rem;font-size:.74rem}
.stage{border:1px solid var(--line);border-radius:999px;padding:.1rem .6rem;color:var(--mut)}
.stage.done{border-color:#1e3328;color:var(--ok)}
.stage.current{border-color:var(--acc);color:var(--acc);font-weight:700;animation:stagepulse 1.6s ease-in-out infinite}
@keyframes stagepulse{0%,100%{box-shadow:0 0 0 0 rgba(111,163,199,0)}50%{box-shadow:0 0 0 3px rgba(111,163,199,.3)}}
.storygrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(13rem,1fr));gap:.6rem;font-size:.88rem}
.storygrid .k{color:var(--mut);font-size:.7rem;text-transform:uppercase;letter-spacing:.06em}
.veto{border:1px solid var(--bad);border-radius:8px;background:#241a14;padding:.8rem 1rem;margin-bottom:1rem}
.veto .vt{color:var(--bad);font-weight:700;letter-spacing:.04em}
.approval-rec{font-size:1.12rem}
.lanes{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:.6rem}
.lane{border:1px solid var(--line);border-radius:8px;padding:.55rem .7rem;background:#12181e;cursor:pointer}
.lane.selected{outline:2px solid var(--acc)}
.lane.working{border-color:var(--acc);animation:lanepulse 1.6s ease-in-out infinite}
@keyframes lanepulse{0%,100%{box-shadow:0 0 0 0 rgba(111,163,199,0)}50%{box-shadow:0 0 0 4px rgba(111,163,199,.25)}}
.lane.flash{animation:laneflash 1.2s ease-out 1}
@keyframes laneflash{0%{box-shadow:0 0 0 4px rgba(95,191,139,.55)}100%{box-shadow:0 0 0 0 rgba(95,191,139,0)}}
.lane.failedlane{border-color:var(--bad)}
.lane .role{font-weight:700;font-size:.85rem}
.lane .nowline{font-size:.8rem;min-height:2.3em;margin:.25rem 0}
.lane .sub2{font-size:.68rem;color:var(--mut)}
.lane .dot{display:inline-block;width:.55em;height:.55em;border-radius:50%;background:var(--acc);
margin-right:.3em;animation:dotpulse 1.2s ease-in-out infinite}
@keyframes dotpulse{0%,100%{opacity:.5}50%{opacity:1}}
.lane .mini{font-size:.68rem;color:var(--mut);border-top:1px solid var(--line);padding-top:.25rem;margin-top:.3rem}
.mini .new{animation:slidein .35s ease-out 1}
@keyframes slidein{from{opacity:0;transform:translateY(-4px)}to{opacity:1;transform:none}}
.dock{position:fixed;left:0;right:0;bottom:0;background:var(--panel);
border-top:1px solid var(--line);padding:.45rem 1.2rem .55rem;z-index:10}
.dock h2{margin:0 0 .3rem;font-size:.8rem;display:flex;align-items:center;gap:.6rem}
.dock.closed .lanes{display:none}
.docktoggle{background:#243040;color:var(--ink);font-weight:600;font-size:.7rem;
padding:.05rem .55rem;margin-left:auto}
body.dockclosed{padding-bottom:56px}
#banners{position:fixed;top:1.2rem;left:50%;transform:translateX(-50%);z-index:30;
display:flex;flex-direction:column;gap:.5rem;align-items:center;pointer-events:none}
.banner{font-size:1.35rem;font-weight:800;letter-spacing:.05em;padding:.7rem 1.6rem;border-radius:10px;
background:var(--panel);border:2px solid var(--acc);color:var(--acc);
box-shadow:0 6px 30px rgba(0,0,0,.55);animation:bannerin 2.8s ease-in-out 1 forwards}
.banner.good{border-color:var(--ok);color:var(--ok)}
.banner.hostile{border-color:var(--bad);color:var(--bad)}
@keyframes bannerin{0%{opacity:0;transform:translateY(-12px)}8%{opacity:1;transform:none}
85%{opacity:1}100%{opacity:0;transform:translateY(-8px)}}
@media (prefers-reduced-motion: reduce){.lane,.mini .new,.stage.current,.vehicle.restored,
.lane .dot,.banner{animation:none}}
</style></head><body>
<div id="banners"></div>
<div class="headrow">
<h1>FORGE Readiness Console <span class="mut">· governed operator console (HUM-1 / HUM-3)</span></h1>
<span class="mut" id="operator"></span>
</div>
<div class="fleet" id="fleet"></div>
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
   <p><button onclick="advanceClock()">Advance</button>
      <span class="mut" style="font-size:.75rem">sim clock: Day <span id="simday">—</span></span></p>
  </div>
  <div class="panel"><h2>Synthetic Demo Controls</h2>
   <p class="mut" style="font-size:.78rem;margin:0 0 .5rem">Anomaly injection for the demonstration —
   every action audited with the operator identity (HUM-3).</p>
   <p><button onclick="injectBulletin(CUR)">Simulate hostile vendor bulletin</button></p>
   <label>Agent instance</label>
   <select id="demo-instance"></select>
   <p><button class="danger" onclick="instanceAction(document.getElementById('demo-instance').value,'fail')">Inject agent failure</button>
      <button onclick="instanceAction(document.getElementById('demo-instance').value,'restore')">Restore</button></p>
  </div>
  <div class="panel"><h2>Workflows</h2><ul id="wfs" style="padding-left:.4rem;list-style:none;margin:0"></ul></div>
  <div class="panel"><h2>Agent Catalog (REG-4)</h2><div id="catalog"></div></div>
</div>
<div id="detail"><div class="panel mut">Select a workflow.</div></div>
</div>
<div class="panel"><h2>Live Agent Activity</h2><div id="activity" class="mut">—</div></div>
<div class="dock" id="dock"><h2>Agent Operations <span class="mut" id="lanefilter"></span>
  <span class="mut" id="docksum"></span>
  <button class="docktoggle" onclick="toggleDock()" id="dockbtn">Collapse &#9662;</button></h2>
 <div id="agentstrip" class="lanes mut">—</div></div>
<script>
let CUR=null;let CURTERMINAL=false;let FILTER=null;let LANEWAS={};let LANES=[];let META={};
let SEEN=null;let FLEETWAS=null;let AUTOPICKED=false;let CLOCKNOW=0;
// day numbers a VIEWER reads are RELATIVE to the recovery — the global sim
// clock is monotonic bookkeeping (it is never reset), shown only beside the
// Advance control
function dueIn(due){const k=due-CLOCKNOW;return k<=0?'due now':'due in '+k+(k===1?' day':' days')}
async function j(u,opt){const r=await fetch(u,opt);if(!r.ok)throw new Error(await r.text());return r.json()}
function tag(v){return `<span class="tag ${v}">${v}</span>`}
function esc(s){return String(s??'').replace(/[&<>"']/g,c=>'&#'+c.charCodeAt(0)+';')}
function shortId(id){return '…'+String(id).slice(-6)}
const ROLE_BUSY={'orchestrator':'Coordinating specialist results','maintenance':'Building recovery plan',
 'supply':'Checking approved parts inventory','workforce':'Matching qualified technicians',
 'safety':'Validating against safety policies','cyber-trust':'Screening external document'};
const SCHEMA_BUSY={'nmc_event.v2':'Breaking the NMC into work packages',
 'approval_decision.v2':"Applying the operator's decision",
 'due_event.v2':'Due event — resuming the workflow',
 'agent_failure_event.v2':'Handling an agent failure'};
function busyCaption(role,now){return (now&&SCHEMA_BUSY[now.schema_version])||ROLE_BUSY[role]||'Working'}
const REASON_HUMAN={'WORKFLOW_CREATED':'Recovery opened','DECOMPOSED':'Broke the NMC into work packages',
 'DOMAIN_OUTPUT_PRODUCED':'Delivered its work product','ACTION_APPROVED':'Validated a proposed action',
 'ACTION_VETOED':'VETOED an unauthorized action','VERDICT_APPROVED':'Accepted the safety verdict',
 'VERDICT_VETOED':'Verdict: vetoed','PLAN_RECEIVED':'Recovery plan received',
 'SCHEDULE_OVERRIDE_APPROVED':'Schedule override approved by the operator',
 'RELEASE_APPROVED':'Equipment released by the operator','PART_ETA_REACHED':'Part ETA reached — resuming',
 'WORKFORCE_RESERVE_DEPLOYED':'Reserve deployed for failed primary',
 'AGENT_ACTIVATED':'Activated for a work package','DOCUMENT_QUARANTINED':'Quarantined an external document',
 'DOCUMENT_SCREENED':'Screening verdict published','WORKFLOW_CANCELLED':'Recovery cancelled by the operator',
 'CLOCK_ADVANCED':'Logical clock advanced','APPROVAL_RECORDED':'Human decision recorded',
 'ROSTER_VERDICT_RECORDED':'Roster verdict held as completion evidence',
 'OPERATOR_BULLETIN_INJECTED':'Hostile bulletin simulated by the operator',
 'OPERATOR_INSTANCE_FAILED':'Agent failure injected by the operator',
 'OPERATOR_INSTANCE_RESTORED':'Agent instance restored by the operator'};
function humanReason(code){if(REASON_HUMAN[code])return REASON_HUMAN[code];
 if(String(code).endsWith('_STALE'))return 'Ignored a stale message (audited)';return code}
const STAGES=[['INTAKE','Reported'],['PLANNING','Planning'],['VALIDATING','Validation'],
 ['AWAITING_SCHEDULE_APPROVAL','Schedule approval'],['SUSPENDED_AWAITING_PART','Waiting for part'],
 ['ASSEMBLY_RESUMED','Repair'],['AWAITING_RELEASE_APPROVAL','Release approval'],['RELEASED','Released']];
function nextExpected(s,due){return {INTAKE:'Orchestrator decomposition',
 PLANNING:'Recovery plan + sourcing report',VALIDATING:'Safety verdicts → approval request',
 AWAITING_SCHEDULE_APPROVAL:'Your schedule-override decision',
 SUSPENDED_AWAITING_PART:'Part '+dueIn(due)+' — advance the clock',
 ASSEMBLY_RESUMED:'Roster validation → release request',
 AWAITING_RELEASE_APPROVAL:'Your release decision',RELEASED:'None — recovery complete',
 BLOCKED_AGENT_FAILURE:'Operator attention: restore the agent or cancel',
 CANCELLED:'None — cancelled'}[s]||'—'}
function showBanner(text,cls){const b=document.createElement('div');
 b.className='banner '+(cls||'');b.textContent=text;
 document.getElementById('banners').appendChild(b);setTimeout(()=>b.remove(),2900)}
let FLEETSEQ=0;
async function loadFleet(){
  const seq=++FLEETSEQ;
  const f=await j('/api/fleet');
  if(seq!==FLEETSEQ)return; // stale response — a newer poll already landed
  const now={};f.vehicles.forEach(v=>now[v.equipment_id]=v);
  let restored=[];
  if(FLEETWAS){for(const id in now){
    // the payoff celebrates REPAIRS only: NMC -> MC with the last recovery
    // RELEASED — a cancelled recovery frees the tile without fanfare
    if(FLEETWAS[id]&&FLEETWAS[id].status!=='MISSION_CAPABLE'
       &&now[id].status==='MISSION_CAPABLE'&&now[id].last_outcome==='RELEASED')restored.push(id)}}
  if(restored.length)showBanner('OPERATIONAL READINESS RESTORED — '+f.readiness.capable+'/'+f.readiness.total+' MISSION CAPABLE','good');
  FLEETWAS=now;
  document.getElementById('fleet').innerHTML=
   `<span class="fleetchip${f.readiness.capable===f.readiness.total?' payoff':''}">FLEET READINESS ${f.readiness.capable}/${f.readiness.total} MC</span>`+
   f.vehicles.map(v=>{
    const cls=v.status==='MISSION_CAPABLE'?'mc':v.status==='BLOCKED'?'blocked':'nmc';
    const label=v.status==='MISSION_CAPABLE'?'MC':v.status==='BLOCKED'?'BLOCKED':'NMC';
    const open=v.workflow_id?` onclick="show('${v.workflow_id}')" title="${esc(v.workflow_status)} · ${esc(v.workflow_id)}"`:'';
    const flash=restored.includes(v.equipment_id)?' restored':'';
    return `<span class="vehicle ${cls}${flash}"${open}>${v.equipment_id.slice(5)}<br><span class="vstat">${label}</span></span>`;
   }).join('');
}
async function loadWorkflows(){
  const list=await j('/api/workflows');
  document.getElementById('wfs').innerHTML=list.map(w=>
    `<li class="wfitem${CUR===w.workflow_id?' selected':''}"><a onclick="show('${w.workflow_id}')" title="${esc(w.workflow_id)}">${esc(w.equipment_id)} recovery <span class="mut">${shortId(w.workflow_id)}</span></a> ${tag(w.status)}${w.due_at!=null?`<br><span class="mut">part ${dueIn(w.due_at)}</span>`:''}</li>`).join('')||'<li class="mut">none</li>';
  if(!AUTOPICKED&&!CUR&&list.length){AUTOPICKED=true;
    const live=list.filter(w=>!['RELEASED','CANCELLED'].includes(w.status));
    const pick=(live.length?live:list).sort((a,b)=>String(b.updated_observed_at).localeCompare(String(a.updated_observed_at)))[0];
    if(pick)show(pick.workflow_id);}
}
async function loadCatalog(){
  const cat=await j('/api/catalog');
  document.getElementById('catalog').innerHTML=cat.map(d=>
    `<div style="margin-bottom:.6rem"><strong>${d.agent_id}</strong> v${d.version} ${tag(d.lifecycle_status)}<br>
     <span class="mut">${d.department_owner} · ${d.capabilities.join(', ')}</span><br>
     ${d.instances.map(i=>`${i.instance_id} ${tag(i.state)} ${tag(i.health)}`).join('<br>')}</div>`).join('');
  const sel=document.getElementById('demo-instance');const had=sel.value;
  const options=cat.flatMap(d=>d.instances).map(i=>`<option value="${i.instance_id}">${i.instance_id} (${i.state})</option>`).join('');
  if(sel.dataset.options!==options){ // rebuild only on change: an open
    sel.innerHTML=options;sel.dataset.options=options; // dropdown survives polls
    if(had)sel.value=had;}
}
async function loadClock(){
  try{const c=await j('/api/clock');CLOCKNOW=c.logical_time;
    document.getElementById('simday').textContent=c.logical_time}catch(e){}
}
const BANNER_RULES={'DOCUMENT_QUARANTINED':['HOSTILE BULLETIN QUARANTINED','hostile'],
 'ACTION_VETOED':['SAFETY VETO — UNAUTHORIZED ACTION BLOCKED','good'],
 'WORKFORCE_RESERVE_DEPLOYED':['WORKFORCE AGENT FAILED — RESERVE DEPLOYED',''],
 'PART_ETA_REACHED':['PART ETA REACHED — WORKFLOW RESUMED','']};
async function loadActivity(){
  // banner bookkeeping ALWAYS runs on the unfiltered stream — a lane filter
  // narrows the table, never the hero moments (and never causes a replay
  // burst when the filter clears)
  const full=await j('/api/activity?limit=25');
  if(SEEN===null){SEEN=new Set(full.map(e=>e.observed_at+e.reason_code))}else{
    for(const e of full.slice().reverse()){const k=e.observed_at+e.reason_code;
      if(SEEN.has(k))continue;SEEN.add(k);
      if(BANNER_RULES[e.reason_code])showBanner(...BANNER_RULES[e.reason_code]);
      else if(e.state_after==='RELEASED')showBanner('EQUIPMENT RELEASED — RECOVERY COMPLETE','good');}}
  const feed=FILTER?await j('/api/activity?limit=25&agent='+FILTER):full;
  document.getElementById('activity').innerHTML=feed.length?`<table>
   <tr><th>Observed</th><th>Day</th><th>Agent</th><th>What happened</th><th>Workflow</th><th>State</th></tr>
   ${feed.map(e=>`<tr><td class="mut">${e.observed_at.slice(11,19)}</td><td>${e.effective_at}</td><td>${e.agent_identity}</td><td>${esc(humanReason(e.reason_code))} <span class="mut">· ${e.reason_code}</span></td><td class="mut" title="${esc(e.workflow_id)}">${shortId(e.workflow_id)}</td><td>${e.state_after?tag(e.state_after):''}</td></tr>`).join('')}</table>`:'—';
}
// Agent Operations lanes: governed state only (claims, packages, registry,
// audit trail); plain-English activity with the schema demoted to secondary
// text; motion is EVENT-DRIVEN only.
async function loadLanes(){
  const lanes=await j('/api/agents/now');LANES=lanes;
  const working=lanes.filter(l=>l.now.kind==='processing'||l.now.kind==='executing').length;
  document.getElementById('docksum').textContent=working?('· '+working+' working'):'· all quiet';
  document.getElementById('agentstrip').classList.remove('mut');
  document.getElementById('agentstrip').innerHTML=lanes.map(l=>{
    const was=LANEWAS[l.definition_id]||{};
    const role=l.definition_id.replace('forge-','');
    const working=l.now.kind==='processing'||l.now.kind==='executing';
    const flash=was.working&&!working&&l.now.kind!=='failed';
    const top=l.recent[0];const newEvt=top&&was.top!==top.observed_at+top.reason_code;
    LANEWAS[l.definition_id]={working,top:top?top.observed_at+top.reason_code:was.top};
    const cls=['lane',working?'working':'',flash?'flash':'',
      l.now.kind==='failed'?'failedlane':'',FILTER===l.definition_id?'selected':''].join(' ');
    const acting=(l.instances||[]).find(i=>i.instance_id===l.acting_instance_id)||{};
    // CURRENT STATUS always leads; the last outcome rides second (entrant
    // correction: outcomes augment status, never replace it)
    let main,sub='';
    if(working){main=busyCaption(role,l.now);sub=esc(l.now.text)+(l.now.workflow_id?' · '+shortId(l.now.workflow_id):'');}
    else if(l.now.kind==='failed'){main='FAILED — restore required';}
    else if(l.now.kind==='reserve'){main='Standing by (reserve)';sub=top?'last: '+esc(humanReason(top.reason_code))+' · '+top.observed_at.slice(11,19):'';}
    else{main='Idle — ready';sub=top?'last: '+esc(humanReason(top.reason_code))+' · '+top.observed_at.slice(11,19):'';}
    return `<div class="${cls}" onclick="laneFilter('${l.definition_id}')">
     <div class="role">${esc(role)} ${acting.state?tag(acting.state):''} ${acting.health?tag(acting.health):''} <span class="mut">${esc(l.acting_instance_id||'')}</span></div>
     <div class="nowline">${working?'<span class="dot"></span>':''}${esc(main)}</div>
     <div class="sub2">${sub||'&nbsp;'}</div>
     <div class="mini">${l.recent.map((e,n)=>`<div${n===0&&newEvt?' class="new"':''}>
       ${e.observed_at.slice(11,19)} ${esc(humanReason(e.reason_code))}</div>`).join('')||'&mdash;'}</div>
    </div>`}).join('');
}
function laneFilter(d){FILTER=FILTER===d?null:d;
  document.getElementById('lanefilter').textContent=FILTER?('· feed filtered: '+FILTER):'';
  loadLanes();loadActivity();}
function applyDock(){
  const open=localStorage.getItem('forge-dock')!=='closed';
  document.getElementById('dock').classList.toggle('closed',!open);
  document.body.classList.toggle('dockclosed',!open);
  document.getElementById('dockbtn').innerHTML=open?'Collapse &#9662;':'Expand &#9652;';
}
function toggleDock(){
  const open=localStorage.getItem('forge-dock')!=='closed';
  localStorage.setItem('forge-dock',open?'closed':'open');
  applyDock();
}
function storyPanel(id,d){
  const s=d.state.status;const terminal=['RELEASED','CANCELLED'].includes(s);
  const idx=STAGES.findIndex(x=>x[0]===s);
  // furthest stage genuinely reached, from the audit trail — so CANCELLED
  // and BLOCKED show honest progress and RELEASED shows a finished rail
  const reached=Math.max(idx,...d.audit_trail.map(e=>STAGES.findIndex(x=>x[0]===e.state_after)));
  const lane=LANES.find(l=>l.now&&l.now.workflow_id===id);
  const role=lane?lane.definition_id.replace('forge-',''):null;
  let action,owner;
  if(d.pending_approvals.length){action='Awaiting your decision — '+d.pending_approvals[0].action_type;owner='YOU (human gate)';}
  else if(lane){action=busyCaption(role,lane.now);owner=lane.acting_instance_id||role;}
  else if(s==='SUSPENDED_AWAITING_PART'){action='Suspended — waiting for the part';owner='Logical Clock';}
  else if(s==='RELEASED'){action='Recovery complete';owner='—';}
  else if(s==='CANCELLED'){action='Cancelled by the operator';owner='—';}
  else if(s==='BLOCKED_AGENT_FAILURE'){action='Blocked — agent failure escalated';owner='operator attention';}
  else{action='Agents working';owner='fleet';}
  const headline=s==='RELEASED'?esc(d.state.equipment_id)+' · MISSION CAPABLE — RELEASED'
    :s==='CANCELLED'?esc(d.state.equipment_id)+' · RECOVERY CANCELLED'
    :esc(d.state.equipment_id)+' · NON-MISSION-CAPABLE';
  const trace=d.state.trace_id||'';
  const traceLink=META.project_id?` · <a href="https://console.cloud.google.com/traces/list?project=${encodeURIComponent(META.project_id)}&tid=${encodeURIComponent(trace)}" target="_blank" rel="noopener">View distributed trace ↗</a>`:'';
  return `<div class="story">
   <div class="headline">${headline}</div>
   <div class="sub">${esc(d.state.equipment_id)} · DSC recovery ${shortId(id)} · one trace: <span class="mut">${esc(trace).slice(0,12)}…</span>${traceLink}</div>
   <div class="stages">${STAGES.map((st,n)=>{
     const cls=s==='RELEASED'?'done'
       :(terminal||s==='BLOCKED_AGENT_FAILURE')?(n<=reached?'done':'')
       :n<idx?'done':n===idx?'current':'';
     return `<span class="stage ${cls}">${st[1]}</span>`}).join('<span class="mut">›</span>')}
     ${s==='BLOCKED_AGENT_FAILURE'?tag('BLOCKED_AGENT_FAILURE'):''}${s==='CANCELLED'?tag('CANCELLED'):''}</div>
   <div class="storygrid">
    <div><div class="k">Current action</div>${esc(action)}</div>
    <div><div class="k">Owned by</div>${esc(owner)}</div>
    <div><div class="k">Next expected event</div>${esc(nextExpected(s,d.state.due_at))}</div>
    <div><div class="k">Recovery day</div>Day ${Math.max(0,CLOCKNOW-(d.audit_trail.length?d.audit_trail[0].effective_at:CLOCKNOW))}${d.state.due_at!=null?' · part '+dueIn(d.state.due_at):''}</div>
   </div></div>`;
}
function vetoCallout(d){
  const vetoes=d.audit_trail.filter(e=>e.event_kind==='veto'||e.reason_code==='ACTION_VETOED'
    ||e.reason_code==='VERDICT_VETOED'||e.reason_code==='RELEASE_VERDICT_VETOED');
  if(!vetoes.length)return '';
  const v=vetoes[vetoes.length-1];let why=[];
  try{const parsed=JSON.parse(v.detail||'{}');
    why=(parsed.reasons||parsed.violations||[]).concat(parsed.rule_refs||[]);}catch(e){}
  if(!why.length&&v.detail)why=[v.detail.slice(0,300)];
  return `<div class="veto"><div class="vt">SAFETY VETO — why:</div>
   <ul>${why.map(r=>`<li>${esc(r)}</li>`).join('')}</ul></div>`;
}
async function show(id){
  CUR=id;
  const d=await j('/api/workflows/'+id);
  if(CUR!==id)return; // the user selected another workflow while we fetched
  const terminal=['RELEASED','CANCELLED'].includes(d.state.status);
  CURTERMINAL=terminal;
  const approvals=d.pending_approvals.map(p=>`
   <div class="panel"><h2>Approval required — ${p.action_type} <span class="mut">(${p.approval_id})</span></h2>
    <p class="approval-rec"><strong>${esc(p.recommended_action)}</strong> <span class="mut">confidence ${p.confidence}</span></p>
    <table>
     <tr><th>Sources</th><td>${esc(p.source_refs.join('; '))}</td></tr>
     <tr><th>Facts</th><td><ul>${p.extracted_facts.map(f=>`<li>${esc(f)}</li>`).join('')}</ul></td></tr>
     <tr><th>Rules</th><td>${esc(p.applicable_rules.join(', '))}</td></tr>
     <tr><th>Constraints</th><td>${(p.constraints||[]).map(esc).join('; ')||'—'}</td></tr>
     <tr><th>Alternatives</th><td>${(p.alternatives_considered||[]).map(a=>`${esc(a.option)} <span class="mut">— ${esc(a.rejected_reason)}</span>`).join('<br>')||'—'}</td></tr>
     <tr><th>Versions</th><td class="mut">${esc(p.versions.agent_id)} · ${esc(p.versions.model_id)} · prompt ${esc(p.versions.prompt_version)} · ${esc(p.versions.schema_version)}</td></tr>
    </table>
    <p><button class="big" onclick="decide('${id}','${p.approval_id}','approved')">Approve</button>
       <button class="big reject" onclick="decide('${id}','${p.approval_id}','rejected')">Reject</button></p>
   </div>`).join('');
  document.getElementById('detail').innerHTML=`
   ${storyPanel(id,d)}
   ${vetoCallout(d)}
   ${approvals}
   <div class="panel"><h2>Work packages</h2>
    <table><tr><th>Package</th><th>Role</th><th>Owner</th><th>Status</th><th>Seq</th></tr>
    ${d.work_packages.map(p=>`<tr><td>${p.work_package_id}</td><td>${p.role}</td><td>${p.owner_instance_id}${p.reassigned_from?` <span class="mut">(from ${p.reassigned_from})</span>`:''}</td><td>${tag(p.status)}</td><td>${p.assignment_seq}</td></tr>`).join('')}</table>
    ${terminal?'':`<p style="margin-top:.6rem"><button class="danger" onclick="cancelWorkflow('${id}')">Cancel workflow</button></p>`}
   </div>
   <div class="panel"><h2>Audit trail — reconstructed from Firestore alone (AUD-2)</h2>
    <table><tr><th>Day</th><th>Observed</th><th>Kind</th><th>Reason</th><th>Agent</th><th>State</th><th>Origin/Trust</th></tr>
    ${d.audit_trail.map(e=>`<tr><td>${e.effective_at}</td><td class="mut">${e.observed_at.slice(0,19)}</td><td>${e.event_kind}</td><td>${e.reason_code}</td><td class="mut">${e.agent_identity}</td><td>${e.state_after?tag(e.state_after):''}</td><td>${tag(e.data_origin)} ${tag(e.trust_state)}</td></tr>`).join('')}</table>
   </div>`;
  loadWorkflows();
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
  if(!confirm('Cancel this recovery? Cancellation is terminal and audited.'))return;
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
  if(!wf||CURTERMINAL){alert('Select a live (non-terminal) workflow first.');return;}
  try{const r=await j('/api/anomalies/bulletin',{method:'POST',
    headers:{'content-type':'application/json'},body:JSON.stringify({workflow_id:wf})});
    await show(wf);}
  catch(e){alert(e.message)}
}
async function instanceAction(iid,action){
  if(!iid)return;
  if(action==='fail'&&!confirm('Inject a failure into '+iid+'? The induction is audited.'))return;
  try{await j('/api/catalog/instances/'+iid,{method:'POST',
    headers:{'content-type':'application/json'},body:JSON.stringify({action})});
    await loadCatalog();await loadLanes();}
  catch(e){alert(e.message)}
}
async function whoami(){
  try{await j('/api/whoami'); // probe only: the identity stays OUT of the DOM
    document.getElementById('operator').textContent='✓ operator authenticated'}
  catch(e){}
}
async function loadMeta(){try{META=await j('/api/meta')}catch(e){META={}}}
document.getElementById('op-equip').innerHTML=Array.from({length:12},(_,n)=>{
  const id='GX12-'+String(n+1).padStart(2,'0');return `<option>${id}</option>`}).join('');
applyDock();loadMeta();whoami();loadClock();loadFleet();loadWorkflows();loadCatalog();loadActivity();loadLanes();
setInterval(()=>{loadFleet();loadWorkflows();loadCatalog();loadActivity();loadClock()},5000);
setInterval(loadLanes,3000);
</script></body></html>"""
