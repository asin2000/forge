"""Dashboard page (read-only except approve/reject, §10). Single file,
no external assets — served from the approval surface itself."""

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
button{background:var(--acc);border:0;color:#0d1216;font-weight:700;border-radius:6px;
padding:.4rem .9rem;cursor:pointer;margin-right:.5rem}
button.reject{background:var(--bad)}
li{margin:.15rem 0}.mut{color:var(--mut)}pre{white-space:pre-wrap;font-size:.8rem}
a{color:var(--acc);cursor:pointer}
</style></head><body>
<h1>FORGE Readiness Console <span class="mut">· read-only except approve/reject (HUM-1)</span></h1>
<div class="grid">
<div>
  <div class="panel"><h2>Workflows</h2><ul id="wfs"></ul></div>
  <div class="panel"><h2>Agent Catalog (REG-4)</h2><div id="catalog"></div></div>
</div>
<div id="detail"><div class="panel mut">Select a workflow.</div></div>
</div>
<script>
async function j(u,opt){const r=await fetch(u,opt);if(!r.ok)throw new Error(await r.text());return r.json()}
function tag(v){return `<span class="tag ${v}">${v}</span>`}
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
     ${d.instances.map(i=>`${i.instance_id} ${tag(i.state)} ${tag(i.health)}`).join('<br>')}</div>`).join('');
}
async function show(id){
  const d=await j('/api/workflows/'+id);
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
loadWorkflows();loadCatalog();setInterval(()=>{loadWorkflows();loadCatalog()},5000);
</script></body></html>"""
