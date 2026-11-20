import {QUALITY_LABELS} from '../types/enums.js';
import {esc} from './format.js';
// 逐指标质量状态：同一源记录只补齐一个指标时，另一指标以缺测/异常隔离，不进入分母
export function fieldValues(data,fields){
 const values={};
 for(const [key,label,kind] of fields){
   if(kind==='text'){values[key]=data[key]??'';continue;}
   const state=data[`${key}.state`]||'included';
   const raw=data[key]??'';
   const reason=(data[`${key}.reason`]||'').trim();
   if(state==='included') values[key]=raw===''?null:Number(raw);
   else values[key]={state,value:raw===''?null:Number(raw),reason};
 }
 return values;
}
export const qualityBadge=q=>{
 const state=q?.state||'included';
 const reason=q?.reason?` · ${esc(q.reason)}`:'';
 return `<span class="quality ${state}">${QUALITY_LABELS[state]||state}${reason}</span>`;
};
export const stateOptions=current=>['included','missing','anomalous']
 .map(s=>`<option value="${s}" ${s===current?'selected':''}>${QUALITY_LABELS[s]}</option>`).join('');
