// 轻量 DOM/ESM 桩：验证 triage 页面 render/bind 在含失效、重算、留痕数据时不抛错
import {readFileSync,writeFileSync,mkdirSync} from 'node:fs';
import {pathToFileURL} from 'node:url';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
async function main(){
 const dir=join(tmpdir(),'aid-frontend-stub-'+Date.now());
 mkdirSync(join(dir,'stores'),{recursive:true});mkdirSync(join(dir,'components'),{recursive:true});
 mkdirSync(join(dir,'constants'),{recursive:true});mkdirSync(join(dir,'utils'),{recursive:true});mkdirSync(join(dir,'pages'),{recursive:true});
 const store={data:{selected:1,
  intakes:[{id:1,code:'LZ033',alias:'主题待核访客',facts:'模糊事实',facts_version:1,status:'open'},
           {id:2,code:'LZ030',alias:'青桥授权代表',facts:'撤回卷原话不能出现',facts_version:1,status:'open'}],
  consents:[{intake_id:1,authorized:true}],
  triages:[
   {id:7,intake_id:1,facts_version:1,status:'reviewed',predicted:'labor',confidence:.8,approved:'labor',manual_required:false,metrics:{source:'真实样本训练',train_count:4,macro_f1:.25,dataset_sha256:'abc123',labels:['labor','housing','consumer'],confusion_matrix:[[1,0,0],[0,1,0],[0,0,1]]},invalid_reason:'',recomputed_from:null},
   {id:8,intake_id:1,facts_version:1,status:'invalid',predicted:null,confidence:null,manual_required:true,metrics:{},invalid_reason:'样本卷授权撤回，相关旧建议作废',recomputed_from:7},
   {id:9,intake_id:1,facts_version:1,status:'queued',predicted:null,confidence:null,manual_required:true,metrics:{},invalid_reason:'',recomputed_from:8},
   {id:10,intake_id:1,facts_version:1,status:'done',predicted:'housing',confidence:.72,manual_required:false,metrics:{},invalid_reason:'',recomputed_from:null}],
  training_samples:[
   {intake_id:2,topic:'labor',status:'revoked',revoked_at:'2026-11-19T08:00:00Z',basis:''},
   {intake_id:1,topic:'housing',status:'active',revoked_at:null,basis:''}],
  triage_samples:[]},selected:1};
 const calls=[];globalThis.__calls=calls;
 const request=async(path,body,method)=>{globalThis.__calls.push({path,body,method});
  if(path.startsWith('/triages/')&&path.endsWith('/trace'))return {triage:{id:10,status:'queued',invalid_reason:''},samples:[
    {sample_id:1,intake_id:2,intake_code:'LZ030',alias:'青桥授权代表',topic:'labor',status_at_snapshot:'active',current_status:'revoked',excluded:true,exclude_reason:'样本卷撤回授权，已移出训练集'},
    {sample_id:2,intake_id:1,intake_code:'LZ033',alias:'主题待核访客',topic:'housing',status_at_snapshot:'active',current_status:'active',excluded:false,exclude_reason:''}],
    recomputed_from:{triage_id:8,status:'invalid',invalid_reason:'样本卷授权撤回，相关旧建议作废'},recomputed_to:[]};
  return {id:99};};
 writeFileSync(join(dir,'stores/workspace.js'),`export const store=${JSON.stringify(store)};export const selectCase=()=>{};`);
 writeFileSync(join(dir,'components/controls.js'),`export const field=(t,c)=>c;export const input=()=>'<input>';export const select=(n,o)=>'<select>'+o+'</select>';export const table=(h,r)=>'<table><thead>'+h.join('|')+'</thead><tbody>'+r.map(row=>'<tr>'+row.join('||')+'</tr>').join('')+'</tbody></table>';export const badge=(t,k)=>'<span class="badge '+k+'">'+t+'</span>';`);
 writeFileSync(join(dir,'constants/enums.js'),`export const topics={labor:'劳动报酬',housing:'住房租赁',consumer:'消费服务'};export const triageStates={queued:'排队中',running:'计算中',done:'待复核',reviewed:'已确认',invalid:'已失效'};export const sampleStates={active:'样本有效',revoked:'授权已撤回'};`);
 writeFileSync(join(dir,'utils/format.js'),`export const esc=s=>String(s??'').replaceAll('<','&lt;');`);
 writeFileSync(join(dir,'utils/forms.js'),`export const bindForm=()=>{};
export const request=${request.toString()};`);
 writeFileSync(join(dir,'pages/triage.js'),readFileSync('/workspace/frontend/src/pages/triage.js','utf8'));
 const elements=new Map();
 const makeEl=()=>({innerHTML:'',hidden:false,dataset:{},addEventListener(){},querySelector:()=>makeEl(),querySelectorAll:()=>[],_t:'',set textContent(v){this._t=v;},get textContent(){return this._t;}});
 globalThis.localStorage={getItem:k=>k==='aid-trace-1'?'10':null,setItem(){},removeItem(){}};
 globalThis.document={getElementById:id=>{if(!elements.has(id))elements.set(id,makeEl());return elements.get(id);},querySelectorAll:()=>[]};
 const mod=await import(pathToFileURL(join(dir,'pages/triage.js')).href);
 const html=mod.render();
 if(!html.includes('查看所用样本与重算原因'))throw new Error('缺少留痕按钮');
 if(!html.includes('重算自 #8'))throw new Error('缺少重算来源标注');
 if(!html.includes('已失效'))throw new Error('缺少失效状态');
 if(html.includes('撤回卷原话不能出现'))throw new Error('泄露了其他卷事实原话');
 mod.bind(()=>{});
 globalThis.document.querySelectorAll=sel=>sel==='[data-trace]'?[{dataset:{trace:10},addEventListener:async(ev,fn)=>{await fn();}}]:[];
 mod.bind(()=>{});
 await new Promise(r=>setTimeout(r,10));
 const traceCall=calls.find(c=>c.path==='/triages/10/trace');
 if(!traceCall)throw new Error('留痕请求未发出');
 console.log('FRONTEND RENDER OK, html length',html.length,'trace call',traceCall.path);
}
main();
