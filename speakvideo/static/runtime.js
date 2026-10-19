/* Model setup lives apart from the editor and keeps progress across reloads. */
let runtimeSnapshot = null;
let runtimePanelBusy = false;
for (const [key,label] of Object.entries({install:'安装推理环境',start:'加载本机模型',stop:'停止推理',restart:'重启推理',test:'真实模型试音',download:'下载模型',validate:'校验模型',remove:'移除模型'})) TASK_LABELS['runtime_'+key]=label;

async function loadRuntimePanel(fill=false) {
  if (runtimePanelBusy) return;
  runtimePanelBusy=true;
  try {
    const r=runtimeSnapshot=await api('/api/runtime');
    const busy=r.tasks.some(t=>t.label.startsWith('runtime_')&&activeTask(t.status));
    $('runtimeBadge').textContent=r.process.health.ready?'已就绪':busy?'准备中':r.installed?'待启动':'待安装';
    $('runtimeSummary').textContent=r.environment.error || (r.process.health.ready?'本机推理已就绪，可以开始创作。':busy?'后台准备中，页面可以关闭；进度会保存。':'按下方步骤准备本机推理。');
    const env=$('runtimeEnvironment');env.replaceChildren();
    for (const text of [`${r.environment.system} ${r.environment.machine} · ${r.environment.profile||'无可用推理设备'}`,`内存可用 ${r.environment.memory_available_gb} / ${r.environment.memory_total_gb} GB · 磁盘可用 ${r.environment.disk_free_gb} GB`,r.environment.ffmpeg&&r.environment.ffprobe?'FFmpeg / ffprobe 已找到':`音视频工具缺失，请执行：${r.environment.ffmpeg_install}`,r.environment.system==='Darwin'?'MPS 使用 float32，建议至少 24 GB 统一内存；实际速度和占用以设备为准。':'0.6B 至少 4 GiB 显存，1.7B / VoiceDesign 至少 6 GiB。']) { const p=document.createElement('div');p.textContent=text;env.append(p); }
    if (fill) { const c=r.config;$('runtimeModel').value=c.model;$('runtimeDevice').value=c.device;$('runtimeSource').value=c.source;$('runtimeModelsDir').value=c.models_dir||'';$('runtimeOffline').checked=c.offline;$('runtimePort').value=c.port; }
    const models=$('runtimeModels');models.replaceChildren();
    for (const m of r.models) {
      const article=document.createElement('article');article.className='runtime-model';
      const title=document.createElement('strong');title.textContent=`${m.key==='voicedesign'?'描述音色 · ':''}${m.repo.split('/')[1]} · ${m.complete?'完整':'未准备'}`;
      const path=document.createElement('p');path.className='hint';path.textContent=m.path;
      const size=document.createElement('p');size.className='hint';size.textContent=`已存 ${(m.bytes/1024**3).toFixed(2)} GiB${m.key==='voicedesign'?' · 使用描述音色前单独下载':''}`;
      article.append(title,path,size);
      for (const [action,label] of [['download',m.complete?'复用 / 修复':'下载模型'],['validate','重新校验'],['remove','移除']]) { const b=document.createElement('button');b.className='btn sm';b.textContent=label;b.disabled=busy||(action==='download'&&(r.config.offline||!r.installed))||(action==='remove'&&(r.process.owned||!!r.config.models_dir||!m.bytes));b.onclick=()=>safe(async()=>{if(action==='remove'&&!confirm('确认移除该模型？工程和音色会保留。'))return;await queue('/api/runtime/models/actions',{action,key:m.key});await loadRuntimePanel();});article.append(b); }
      models.append(article);
    }
    document.querySelectorAll('[data-runtime-action]').forEach(b=>{const a=b.dataset.runtimeAction;b.disabled=busy||(!r.environment.supported&&a==='install')||(a==='start'&&(!r.installed||r.process.owned))||(a==='stop'&&!r.process.owned)||(a==='test'&&!r.process.health.ready)||(a==='install'&&r.process.owned)||(a==='restart'&&!r.installed);});
    $('saveRuntime').disabled=busy||r.process.owned;
    const operations=r.tasks.filter(t=>t.label.startsWith('runtime_')).slice(-3);
    const lines=operations.flatMap(t=>[`${TASK_LABELS[t.label]} · ${TASK_STATES[t.status]||t.status}${t.error?' · '+t.error:''}`,...(t.log||[]).map(v=>v.message)]);
    if (r.logs.length) lines.push('── 推理服务 ──',...r.logs);
    const output=$('runtimeLogs');const stick=output.scrollHeight-output.scrollTop-output.clientHeight<50;output.textContent=lines.join('\n')||'尚无日志';if(stick)output.scrollTop=output.scrollHeight;
    const test=[...operations].reverse().find(t=>t.label==='runtime_test'&&t.status==='succeeded'&&t.result?.endsWith('.wav'));
    if(test&&$('runtimePreview').dataset.id!==test.id){$('runtimePreview').replaceChildren();const audio=document.createElement('audio');audio.controls=true;audio.src='/api/previews/'+encodeURIComponent(test.result);$('runtimePreview').append(audio);$('runtimePreview').dataset.id=test.id;}
  } finally { runtimePanelBusy=false; }
}
$('openRuntime').onclick=()=>safe(async()=>{$('runtimeDialog').showModal();await loadRuntimePanel(true);});
$('closeRuntime').onclick=()=>$('runtimeDialog').close();
$('refreshRuntimePanel').onclick=()=>safe(()=>loadRuntimePanel());
$('runtimeNextLLM').onclick=()=>{$('runtimeDialog').close();$('openLLM').click();};
$('runtimeForm').onsubmit=event=>{event.preventDefault();safe(async()=>{await api('/api/runtime/config','PUT',{model:$('runtimeModel').value,device:$('runtimeDevice').value.trim(),source:$('runtimeSource').value,models_dir:$('runtimeModelsDir').value.trim()||null,offline:$('runtimeOffline').checked,port:Number($('runtimePort').value)});config=await api('/api/config');toast('运行配置已保存');await loadRuntimePanel(true);});};
document.querySelectorAll('[data-runtime-action]').forEach(b=>b.onclick=()=>safe(async()=>{await queue('/api/runtime/actions',{action:b.dataset.runtimeAction});await loadRuntimePanel();}));
setInterval(()=>{if(!document.hidden&&$('runtimeDialog').open)safe(()=>loadRuntimePanel());},3000);
// Only a new, empty workspace opens onboarding automatically.
(async()=>{try{const r=await api('/api/runtime');$('runtimeBadge').textContent=r.process.health.ready?'已就绪':r.installed?'待启动':'待安装';if(!r.installed&&!store.get('runtimeWelcomeSeen',false)&&!(await api('/api/projects')).length){store.set('runtimeWelcomeSeen',true);$('openRuntime').click();}}catch(error){console.warn('Runtime setup:',error.message);}})();
