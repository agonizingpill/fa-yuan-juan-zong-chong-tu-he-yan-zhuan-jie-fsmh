import {store} from '../stores/workspace.js';import {field,input,select,table,badge} from '../components/controls.js';import {topics,triageStates,sampleStates} from '../constants/enums.js';import {esc} from '../utils/format.js';import {bindForm,request} from '../utils/forms.js';
function statusBadge(t){const kind=t.status==='reviewed'?'good':t.status==='invalid'?'warning':'';let text=triageStates[t.status]||t.status;if(t.status==='invalid'&&t.invalid_reason)text+='：'+t.invalid_reason;return badge(text,kind);}
export function render(){
 const d=store.data,c=d.intakes.find(i=>i.id===store.selected);
 const rows=d.triages.filter(t=>t.intake_id===c.id);const last=rows.at(-1);
 const confirmed=rows.filter(t=>t.status==='reviewed');
 const sample=d.training_samples.find(s=>s.intake_id===c.id);
 const sampleBook=d.training_samples.map(s=>{const sc=d.intakes.find(i=>i.id===s.intake_id);return [esc(sc.code)+' '+esc(sc.alias),topics[s.topic],sampleStates[s.status],s.revoked_at?esc(s.revoked_at):'—'];});
 const traceId=Number(localStorage.getItem('aid-trace-'+c.id)||0);
 const trace=traceId?d._traces?.[traceId]:null;
 return `<h2>咨询主题与专长分流</h2>
<p class="intro">仅以主管确认过的卷宗训练主题分类；同来访方旧卷自动隔离。送分流时锁定当批样本，撤回授权后排队任务作废、已确认建议只按原批其余样本重算。</p>
<div class="two-col">
<form id="enqueue" class="paper-form"><h3>按当前事实提交分流</h3><p>入队时记录实际采用的样本编号；撤回卷原话不会进入训练或留痕。</p><button>提交行政主题识别</button><p class="hint">合资格样本不足两个主题时会被拒绝。本页不生成法律意见或办案建议。</p></form>
<form id="sample" class="paper-form"><h3>主管登记训练样本</h3>${field('依据的已确认建议',select('triage_id',confirmed.length?confirmed.map(t=>`<option value="${t.id}">#${t.id} / ${topics[t.approved]} / 版本${t.facts_version}</option>`).join(''):'<option value="">当前卷宗尚无已确认建议</option>'))}${field('样本主题（须与确认方向一致）',select('topic',Object.entries(topics).map(([v,l])=>`<option value="${v}">${l}</option>`).join('')))}${field('登记依据',input('basis','text','主管复核确认，纳入主题训练样本册'))}<button ${confirmed.length?'':'disabled'}>登记 / 重新纳入样本册</button><p class="hint">${sample?('本卷样本状态：'+sampleStates[sample.status]):'本卷尚未进入样本册'}</p></form>
</div>
<form id="review" class="paper-form"><h3>主管主题复核</h3><div class="two-col">${field('待复核记录',select('id',rows.filter(t=>t.status==='done').map(t=>`<option value="${t.id}">#${t.id} / 版本${t.facts_version} / ${topics[t.predicted]} ${Math.round(t.confidence*100)}%</option>`).join('')||'<option value="">暂无待复核建议</option>'))}${field('确认专长方向',select('topic',Object.entries(topics).map(([v,l])=>`<option value="${v}">${l}</option>`).join('')))}</div>${field('行政核验说明',input('note','text','已核对来访描述和机构接待范围'))}<button ${rows.some(t=>t.status==='done')?'':'disabled'}>确认专长方向</button></form>
${table(['记录 / 事实版本','推荐主题','置信度 / 人工','状态与重算来源','样本留痕'],rows.slice().reverse().map(t=>['#'+t.id+' / v'+t.facts_version+(t.recomputed_from?' / 重算自 #'+t.recomputed_from:''),topics[t.predicted]||'待识别',t.confidence===null?'—':Math.round(t.confidence*100)+'% / '+(t.manual_required?'低置信退回人工':'等待主管确认'),statusBadge(t),`<button type="button" class="text-button" data-trace="${t.id}">查看所用样本与重算原因</button>`]))}
<section class="paper-form" id="trace-panel" ${trace?'':'hidden'}><h3>样本留痕 #${traceId}</h3>${trace?`<p>${esc(trace.triage.status==='invalid'?('失效原因：'+(trace.triage.invalid_reason||'—')):'当前状态：'+(triageStates[trace.triage.status]||trace.triage.status))}</p>${trace.recomputed_from?`<p class="hint">由 #${trace.recomputed_from.triage_id} 重算而来，原建议：${esc(trace.recomputed_from.invalid_reason||trace.recomputed_from.status)}</p>`:''}${trace.recomputed_to?.length?`<p class="hint">已派生重算：${trace.recomputed_to.map(x=>'#'+x.triage_id).join('、')}</p>`:''}${table(['样本卷宗编号','主题','快照状态','当前状态','是否采用 / 排除原因'],trace.samples.map(s=>[esc(s.intake_code)+' '+esc(s.alias),topics[s.topic],sampleStates[s.status_at_snapshot],sampleStates[s.current_status]||s.current_status,s.excluded?badge('排除：'+s.exclude_reason,'warning'):badge('实际采用','good')]))}<p class="hint">留痕仅保留卷宗编号与主题，不显示撤回卷的事实原话。</p>`:''}</section>
<section class="validation"><div><span class="kicker">训练样本册（全局）</span><h3>主管确认卷宗才可入册</h3><p>撤回授权即移出样本集，撤回原话不再显示；同来访方旧卷对本卷隔离。</p>${table(['卷宗','确认主题','样本状态','撤回时间'],sampleBook)}</div><div><span class="kicker">内部留一评价</span><h3>TF-IDF + 朴素贝叶斯</h3><p>${esc(last?.metrics?.source||'尚无结果')}</p><p>训练 ${last?.metrics?.train_count||0} 条 / 阈值68% / 样本摘要 ${esc((last?.metrics?.dataset_sha256||'—').slice(0,12))}</p>${last?.metrics?.macro_f1!=null?`<b>留一 Macro F1 ${last.metrics.macro_f1.toFixed(4)}</b>`:'<b>样本不足，暂无交叉验证</b>'}${last?.metrics?.confusion_matrix?table(['实际\\预测','劳动','住房','消费'],last.metrics.confusion_matrix.map((r,i)=>[topics[last.metrics.labels[i]],...r])):''}</div></section>`;}
export function bind(r){
 bindForm('enqueue',()=>request('/triages',{intake_id:store.selected}),r);
 bindForm('sample',d=>request('/training-samples',{intake_id:store.selected,topic:d.topic,basis:d.basis}),r);
 bindForm('review',d=>request('/triages/'+d.id+'/review',{topic:d.topic,note:d.note}),r);
 document.querySelectorAll('[data-trace]').forEach(btn=>btn.addEventListener('click',async()=>{
   const id=Number(btn.dataset.trace);localStorage.setItem('aid-trace-'+store.selected,id);
   store.data._traces=store.data._traces||{};store.data._traces[id]=await request('/triages/'+id+'/trace');r('留痕已载入');}));
}
