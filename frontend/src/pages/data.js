import {DOMAIN} from '../constants/domain.js';
import {store,load,loadRows} from '../stores/research.js';
import {studies} from '../api/research.js';
import {studySelect} from '../components/common/study-select.js';
import {table} from '../components/common/table.js';
import {esc} from '../utils/format.js';
import {bindForm} from '../hooks/forms.js';
import {fieldValues,qualityBadge,stateOptions} from '../utils/quality.js';

function fieldInputs(){
 return DOMAIN.fields.map(([key,label,kind])=>{
  if(kind==='text') return `<label>${esc(label)}<input name="${key}" required></label>`;
  return `<fieldset class="quality-field" data-field="${key}">
   <legend>${esc(label)}</legend>
   <label class="q-state">质量状态<select name="${key}.state">${stateOptions('included')}</select></label>
   <label class="q-value">数值<input name="${key}" type="${kind}" step="any" min="0"></label>
   <label class="q-reason" hidden>隔离原因<input name="${key}.reason" maxlength="200" placeholder="缺测/异常依据，冻结时随快照记录"></label>
  </fieldset>`;
 }).join('');
}
export async function render(){await loadRows();const writable=localStorage.getItem('research-role')!=='viewer';return `<div class="eyebrow">01 / SOURCE REGISTER</div><h1>${DOMAIN.study_label}与${DOMAIN.row_label}</h1><p>${DOMAIN.note}</p><div class="method-note">逐指标质量状态：同一源记录只补齐一个指标时，另一指标选择“缺测隔离/异常隔离”并写明原因；冻结后只把“纳入”值计入该指标分母。</div><div class="toolbar">${studySelect()}</div>${writable?`<details><summary>新增${DOMAIN.study_label}</summary><form id="study-form" class="inline-form"><label>名称<input name="name" required minlength="2"></label><label>来源文件<input name="source_filename" value="field-notes-202611.csv" required></label><label>说明<input name="description"></label><button type="submit">保存批次</button></form></details><h2 id="row-heading">录入${DOMAIN.row_label}</h2><form id="row-form" class="row-form"><label>源记录编号<input name="source_key" placeholder="如 FIELD-001" required></label>${fieldInputs()}<button type="submit">保存观测</button><button type="button" id="cancel-edit" hidden>取消编辑</button></form>`:''}<h2>观测明细 <small>${store.rows.length} 条 · 运行后更改不会改写历史快照</small></h2>${table(['源记录',...DOMAIN.fields.map(f=>f[1]+' / 质量状态'),...(writable?['操作']:[])],store.rows.map(row=>[row.source_key,...DOMAIN.fields.map(([key])=>({html:`${esc(row.values[key]??'—')} ${qualityBadge((row.quality||{})[key])}`})),...(writable?[{html:`<button class="text" data-edit="${row.id}">编辑</button> <button class="text" data-delete="${row.id}">移除</button>`}]:[])]))}`;}
export function bind(refresh){
 let editing=null;
 const wireStateToggles=form=>form.querySelectorAll('.quality-field').forEach(fs=>{
  const sel=fs.querySelector('.q-state select'),reason=fs.querySelector('.q-reason'),value=fs.querySelector('.q-value input');
  const sync=()=>{const isolated=sel.value!=='included';reason.hidden=!isolated;value.required=false;};
  sel.onchange=sync;sync();
 });
 const form=document.getElementById('row-form');if(form)wireStateToggles(form);
 const sel=document.getElementById('study-select');if(sel)sel.onchange=()=>{store.studyId=+sel.value;refresh();};
 bindForm('study-form',async data=>{const s=await studies.create(data);store.studyId=s.id;await load();refresh();});
 bindForm('row-form',async data=>{const source_key=data.source_key;delete data.source_key;const values=fieldValues(data,DOMAIN.fields);await studies.save(store.studyId,{source_key,values},editing);await load();refresh();});
 document.querySelectorAll('[data-edit]').forEach(el=>el.onclick=()=>{
  const row=store.rows.find(r=>r.id===+el.dataset.edit),f=document.getElementById('row-form');
  editing=row.id;f.elements.source_key.value=row.source_key;
  DOMAIN.fields.forEach(([key,kind])=>{
   if(kind==='text'){f.elements[key].value=row.values[key]??'';return;}
   const q=(row.quality||{})[key]||{state:'included',reason:''};
   f.elements[`${key}.state`].value=q.state;f.elements[key].value=row.values[key]??'';
   f.elements[`${key}.reason`].value=q.reason||'';
   f.elements[`${key}.state`].dispatchEvent(new Event('change'));
  });
  document.getElementById('row-heading').textContent='编辑 '+row.source_key;
  document.getElementById('cancel-edit').hidden=false;f.scrollIntoView({behavior:'smooth',block:'center'});
 });
 const cancel=document.getElementById('cancel-edit');if(cancel)cancel.onclick=()=>refresh();
 document.querySelectorAll('[data-delete]').forEach(el=>el.onclick=async()=>{try{await studies.remove(store.studyId,+el.dataset.delete);await load();refresh();}catch(e){document.getElementById('notice').textContent=e.message;}});
}
