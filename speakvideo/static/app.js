'use strict';
const $ = id => document.getElementById(id);
let current = '', data = null, config = {}, voices = {}, referenceId = '', dirty = false;
let dragged = null, taskStatus = new Map();
const escape = value => String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
function toast(message) { $('toast').textContent = message; $('toast').hidden = false; clearTimeout(toast.timer); toast.timer = setTimeout(() => $('toast').hidden = true, 6000); }
async function api(path, method='GET', body) {
  const response = await fetch(path, {method, headers:body instanceof FormData ? {} : {'Content-Type':'application/json'}, body:body === undefined ? undefined : body instanceof FormData ? body : JSON.stringify(body)});
  const result = await response.json();
  if (!response.ok) throw Error(typeof result.detail === 'string' ? result.detail : JSON.stringify(result.detail));
  return result;
}
async function queue(path, body){const task=await api(path,'POST',body);taskStatus.set(task.id,'queued');await pollTasks();return task;}
const url = suffix => `/api/projects/${encodeURIComponent(current)}${suffix}`;
function markDirty(){ dirty = true; $('saveState').textContent = '有未保存修改'; }
async function inventory(){
  voices = await api('/api/voices');
  $('voiceInventory').innerHTML = Object.entries(voices).map(([role, emotions]) => `<div class="voiceItem">${escape(role)} <small>${emotions.map(escape).join(' / ')}</small></div>`).join('') || '<small>尚未导入音色</small>';
  const projects = await api('/api/projects');
  $('projects').innerHTML = projects.map(p => `<button data-project="${escape(p.name)}" class="${p.name===current?'selected':''}">${escape(p.name)}<br><small>${escape(p.topic)}</small></button>`).join('');
  $('projects').querySelectorAll('button').forEach(b => b.onclick = () => selectProject(b.dataset.project));
}
async function selectProject(name){
  if (dirty && !confirm('有未保存修改，仍要切换工程吗？')) return;
  current = name; data = await api(url('')); dirty = false;
  const link = new URL(window.location.href); link.searchParams.set('project', current); history.replaceState(null, '', link);
  $('welcome').hidden = true; $('workspace').hidden = false;
  $('projectName').textContent = current; $('title').textContent = data.script.meta.topic || '未命名对话';
  $('topic').value = data.script.meta.topic; $('keywords').value = data.script.meta.keywords.join(',');
  $('guidance').value = data.script.meta.guidance || ''; $('brief').value = data.script.meta.brief;
  $('saveState').textContent = '已保存';
  $('personas').innerHTML = data.script.cast.map(m => `<label>${escape(m.slot)} 人设<input data-persona="${escape(m.slot)}" value="${escape(m.persona)}"></label>`).join('');
  $('personas').querySelectorAll('input').forEach(input => input.oninput = () => {data.script.cast.find(m=>m.slot===input.dataset.persona).persona=input.value;markDirty();});
  renderLines(); renderBindings(); renderReport(data.preflight); renderMedia(); updateExports(); await inventory();
}
function options(values, selected, placeholder){
  return (placeholder ? `<option value="">${escape(placeholder)}</option>` : '') + values.map(v => `<option value="${escape(v)}" ${v===selected?'selected':''}>${escape(config.moods?.[v] ? `${v} · ${config.moods[v]}` : v)}</option>`).join('');
}
function renderLines(){
  $('lines').innerHTML = data.script.lines.map(line => {
    const status = data.manifest.lines[line.id]?.status || '未合成';
    const missing = !data.cast[line.slot]?.mood_map[line.mood];
    return `<div class="line" data-id="${escape(line.id)}"><span class="handle" draggable="true">⠿</span><small>${escape(line.id)}<br>${escape(status)}</small><select data-field="slot">${options(data.script.cast.map(c=>c.slot),line.slot)}</select><textarea data-field="words" rows="2">${escape(line.words)}</textarea><select data-field="mood" class="${missing?'bad':''}">${options(Object.keys(config.moods),line.mood)}</select><input data-field="pause_after_ms" type="number" min="0" max="10000" value="${line.pause_after_ms}" title="句后停顿（毫秒）"><div class="tools"><button class="mini" data-retry>重试</button><button class="mini" data-delete>删除</button></div></div>`;
  }).join('');
  $('lines').querySelectorAll('.line').forEach(row => {
    row.querySelectorAll('[data-field]').forEach(input => input.oninput = () => {
      const line = data.script.lines.find(l=>l.id===row.dataset.id);
      line[input.dataset.field] = input.dataset.field==='pause_after_ms' ? Number(input.value) : input.value;
      markDirty();
    });
    row.querySelector('[data-delete]').onclick=()=>{data.script.lines=data.script.lines.filter(l=>l.id!==row.dataset.id);markDirty();renderLines();renderBindings();};
    row.querySelector('[data-retry]').onclick=()=>safe(()=>action('retry',row.dataset.id));
    row.querySelector('.handle').ondragstart=event=>{dragged=row.dataset.id;event.dataTransfer.setData('text/plain',dragged);};
    row.ondragover=event=>event.preventDefault();
    row.ondrop=event=>{event.preventDefault();if(!dragged||dragged===row.dataset.id)return;const from=data.script.lines.findIndex(l=>l.id===dragged);const to=data.script.lines.findIndex(l=>l.id===row.dataset.id);const [line]=data.script.lines.splice(from,1);data.script.lines.splice(to,0,line);dragged=null;markDirty();renderLines();};
  });
}
function renderBindings(){
  $('bindings').innerHTML = data.script.cast.map(member=>{
    const binding=data.cast[member.slot] ||= {role:'',mood_map:{},default_emotion:'平静'};
    const emotions=voices[binding.role]||[];
    const moods=[...new Set(data.script.lines.filter(l=>l.slot===member.slot).map(l=>l.mood))];
    return `<div class="binding" data-slot="${escape(member.slot)}"><h3>${escape(member.slot.toUpperCase())} <small>${escape(member.persona)}</small></h3><label>角色音色<select data-role>${options(Object.keys(voices),binding.role,'请选择角色')}</select></label><div class="moodgrid">${moods.map(m=>`<label class="${emotions.includes(binding.mood_map[m])?'':'bad'}">${escape(config.moods[m])} · ${escape(m)}<select data-mood="${escape(m)}">${options(emotions,binding.mood_map[m],'未映射')}</select></label>`).join('')}</div></div>`;
  }).join('');
  $('bindings').querySelectorAll('.binding').forEach(div=>{
    const binding=data.cast[div.dataset.slot];
    div.querySelector('[data-role]').onchange=event=>{binding.role=event.target.value;binding.mood_map={};markDirty();renderBindings();renderLines();};
    div.querySelectorAll('[data-mood]').forEach(select=>select.onchange=()=>{if(select.value)binding.mood_map[select.dataset.mood]=select.value;else delete binding.mood_map[select.dataset.mood];markDirty();renderLines();});
  });
}
function collect(){
  data.script.meta.topic=$('topic').value;
  data.script.meta.keywords=$('keywords').value.split(/[,，]/).map(v=>v.trim()).filter(Boolean);
  data.script.meta.guidance=$('guidance').value;data.script.meta.brief=$('brief').value;
}
async function save(){if(!data)return;collect();const result=await api(url(''),'PUT',{script:data.script,cast:data.cast});dirty=false;$('saveState').textContent='已保存';renderReport(result.preflight);}
function renderReport(report){
  $('report').innerHTML=`<p class="${report.ok?'good':'bad'}">${report.line_count} 行 · ${report.reusable} 行可复用 · ${report.ok?'本地检查通过':'需要处理问题'}</p>` + report.errors.map(v=>`<div class="bad">• ${escape(v)}</div>`).join('') + report.warnings.map(v=>`<div class="muted">${escape(v)}</div>`).join('');
}
function renderMedia(){ $('mediaState').textContent=`BGM：${data.media.bgm ? '已选择' : '未选择'}`;$('bgmVolume').value=data.media.bgm_volume ?? 0.18; }
async function action(type,lineId){
  if(type==='generate' && data.script.lines.length && !confirm('重新生成将替换全部台词并清空复用记录，继续吗？'))return;
  await save();const task=await queue(url('/actions'),{action:type,line_id:lineId||null});toast(`任务已排队：${task.label}`);pollTasks();
}
async function approval(gate){await save();await api(url(`/approve/${gate}`),'POST',{});toast({script:'剧本已定稿',binding:'音色绑定已确认',audio:'完整音频已确认'}[gate]);if(gate==='script')switchTab('binding');if(gate==='binding')switchTab('production');}
function switchTab(id){document.querySelectorAll('.tab').forEach(e=>e.hidden=e.id!==id);document.querySelectorAll('[data-tab]').forEach(e=>e.classList.toggle('active',e.dataset.tab===id));}
function updateExports(){
  const meta=data.audio_export||{};
  const has=!!meta.files?.['master.mp3'];
  $('masterAudio').hidden=!has;
  $('audioState').textContent=has ? `${meta.mode==='demo'?'测试音频 · ':''}${Number(meta.duration).toFixed(2)} 秒 · ${data.audio_current?'与当前工程一致':'旧音频：工程、音色或混音设置已变化，请重新导出'}` : '先合成分句，再合并导出。';
  if(has){const source=url('/files/exports/'+meta.files['master.mp3'].path);if($('masterAudio').getAttribute('src')!==source)$('masterAudio').src=source;$('exports').innerHTML=Object.entries(meta.files).map(([name,file])=>`<a href="${url('/files/exports/'+file.path)}" download>${escape(name)}</a>`).join(' · ');}
  else $('exports').textContent='完成配音后，点击合并 / 混音导出。';
}
async function pollTasks(){
  const tasks=await api('/api/tasks');
  $('tasks').innerHTML=tasks.map(t=>`<div class="task"><span>${escape(t.project||'音色工厂')} · ${escape(t.label)} ${t.progress?` · ${t.progress.completed}/${t.progress.total} 行${t.progress.current_line?' · 正在生成 '+escape(t.progress.current_line):''}`:''}<br><small class="${t.status==='failed'?'bad':''}">${escape(t.error||t.result||'')}</small></span><b>${escape({queued:'排队中',running:'执行中',succeeded:'完成',failed:'失败',paused:'已暂停',interrupted:'被中断'}[t.status]||t.status)}</b>${['queued','running'].includes(t.status)?`<button class="outline" data-pause="${t.id}">暂停</button>`:''}</div>`).join('')||'暂无任务';
  $('tasks').querySelectorAll('[data-pause]').forEach(b=>b.onclick=()=>safe(async()=>{await api(`/api/tasks/${b.dataset.pause}/pause`,'POST',{});toast('已请求暂停；正在执行的直连请求/ffmpeg 会完成当前步骤后停止。');}));
  let refresh=false;
  for(const t of tasks){if(taskStatus.has(t.id)&&taskStatus.get(t.id)!==t.status&&t.status==='succeeded'){
    if(t.project===current)refresh=true;
    if(!t.project&&t.label==='design'){referenceId=t.result;$('voicePreview').src=`/api/previews/${referenceId}`;$('voicePreview').hidden=false;$('referenceState').textContent='试音已生成；确认满意后固化，台词须与试音一致。';}
    if(!t.project&&t.label==='create'){await inventory();if(data)renderBindings();toast('音色已保存到 Mac voices/');}
  }taskStatus.set(t.id,t.status);}
  if(refresh&&!dirty&&current)await selectProject(current);
}
async function safe(fn){try{return await fn();}catch(error){toast(error.message);}}
$('new').onclick=$('welcomeNew').onclick=()=>$('newDialog').showModal();$('closeDialog').onclick=()=>$('newDialog').close();
$('newForm').onsubmit=event=>{event.preventDefault();safe(async()=>{const fields=new FormData(event.target);const p=await api('/api/projects','POST',{name:fields.get('name'),topic:fields.get('topic'),keywords:fields.get('keywords').split(/[,，]/).map(v=>v.trim()).filter(Boolean)});$('newDialog').close();await selectProject(p.name);});};
$('save').onclick=()=>safe(async()=>{await save();toast('修改已保存');});
['topic','keywords','guidance','brief'].forEach(id=>$(id).oninput=markDirty);
$('addLine').onclick=()=>{data.script.lines.push({id:'L'+crypto.randomUUID().replaceAll('-','').slice(0,12),slot:data.script.cast[0].slot,words:'新台词',mood:'normal',pause_after_ms:300});markDirty();renderLines();renderBindings();};
document.querySelectorAll('[data-tab]').forEach(b=>b.onclick=()=>switchTab(b.dataset.tab));
document.querySelectorAll('[data-action]').forEach(b=>b.onclick=()=>safe(()=>action(b.dataset.action)));
document.querySelectorAll('[data-approve]').forEach(b=>b.onclick=()=>safe(()=>approval(b.dataset.approve)));
$('preflight').onclick=()=>safe(async()=>{await save();renderReport(await api(url('/preflight'),'POST',{}));});
for(const [id,kind] of [['bgmFile','bgm']])$(id).onchange=()=>safe(async()=>{for(const file of $(id).files){const f=new FormData();f.append('file',file);data.media=await api(url(`/assets/${kind}`),'POST',f);}renderMedia();toast('音乐已保存；重新合并后生效');});
$('voiceFile').onchange=()=>safe(async()=>{const f=new FormData();f.append('file',$('voiceFile').files[0]);const result=await api('/api/voices/reference','POST',f);referenceId=result.reference_id;$('referenceState').textContent='参考音频已上传；填写对应台词后固化。';});
function voiceBody(action){return {action,role:$('voiceRole').value,emotion:$('voiceEmotion').value,text:$('voiceText').value,instruct:$('voiceDescription').value,reference_id:referenceId};}
$('designVoice').onclick=()=>safe(async()=>{await queue('/api/voices/actions',voiceBody('design'));toast('试音任务已排队');});
$('createVoice').onclick=()=>safe(async()=>{if(!referenceId)throw Error('请先上传参考音频或生成试音');await queue('/api/voices/actions',voiceBody('create'));toast('音色固化任务已排队');});
$('syncVoices').onclick=()=>safe(async()=>{await queue('/api/voices/actions',{action:'sync'});toast('同步任务已排队');});
window.addEventListener('beforeunload',event=>{if(dirty){event.preventDefault();event.returnValue='';}});
safe(async()=>{config=await api('/api/config');$('mode').textContent=config.tts_mode==='demo'?'测试模式 · 非真实配音':config.tts_mode==='gateway'?'网关模式':'Qwen 直连模式';await inventory();await pollTasks();const names=await api('/api/projects');const requested=new URLSearchParams(location.search).get('project');if(names.length)await selectProject(names.find(p=>p.name===requested)?.name||names[0].name);setInterval(()=>safe(pollTasks),2000);});

$('saveMix').onclick=()=>safe(async()=>{data.media=await api(url('/mix'),'PUT',{bgm_volume:Number($('bgmVolume').value)});renderMedia();toast('混音设置已保存，重新导出后生效');});
$('removeBgm').onclick=()=>safe(async()=>{data.media=await api(url('/mix'),'PUT',{bgm_volume:Number($('bgmVolume').value),remove_bgm:true});renderMedia();toast('已移除音乐，重新导出后生效');});
