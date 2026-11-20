import math
from src.config.domain import DOMAIN

# 逐指标质量三态：纳入分析 / 缺测隔离 / 异常隔离
QUALITY_STATES=('included','missing','anomalous')
STATE_MISSING='missing'
STATE_ANOMALOUS='anomalous'
STATE_INCLUDED='included'

def _clean_reason(reason):
    if reason is None: return ''
    if not isinstance(reason,str): raise ValueError('隔离原因必须为文本')
    reason=reason.strip()
    if len(reason)>200: raise ValueError('隔离原因不超过200字')
    return reason

def normalize_field_entry(key,label,kind,entry):
    """把单字段的提交内容归一为 (stored_value, state, reason)。
    接受：数字、None/空串（缺测）、{'state':...,'value':...,'reason':...}。
    只有纳入(included)的值参与业务校验与算法分母；缺测与异常只做登记和隔离。"""
    if isinstance(entry,dict):
        state=entry.get('state',STATE_INCLUDED)
        if state not in QUALITY_STATES: raise ValueError(label+'质量状态必须为 included/missing/anomalous')
        reason=_clean_reason(entry.get('reason'))
        raw=entry.get('value')
    else:
        state=STATE_INCLUDED
        reason=''
        raw=entry
    if kind=='text':
        if state!=STATE_INCLUDED: raise ValueError(label+'为分类字段，不支持隔离状态')
        if raw not in ('A','B'): raise ValueError('工艺组必须为A或B')
        return raw,state,reason
    # 数值字段
    if raw is None or (isinstance(raw,str) and raw.strip()==''):
        # 未提供数值：显式状态优先，否则按缺测隔离
        if state==STATE_INCLUDED: state=STATE_MISSING
        return None,state,reason
    if isinstance(raw,bool): raise ValueError(label+'必须是数值')
    try: value=float(raw)
    except (TypeError,ValueError): raise ValueError(label+'必须为数值或留空')
    if not math.isfinite(value) or abs(value)>1e9 or value<0:
        # 非法业务数值：登记为异常隔离，而不是整行拒绝
        if state==STATE_INCLUDED:
            state=STATE_ANOMALOUS
            reason=reason or ('非有限/越界数值，冻结时隔离，不进入分母')
        return value,state,reason
    if state==STATE_INCLUDED:
        return value,state,reason
    # 显式隔离但数值本身合法：保留数值用于追溯，不参与分母
    return value,state,reason

def validate_values(values):
    """整行归一：返回 (stored_values, quality)。
    同一源记录常只补齐一个指标，另一指标的缺测/异常在此被隔离，不会静默进入分母。"""
    expected={f[0] for f in DOMAIN['fields']}
    if not isinstance(values,dict) or set(values)!=expected:
        raise ValueError('字段必须为 '+', '.join(sorted(expected)))
    out,quality={},{}
    for key,label,kind in DOMAIN['fields']:
        value,state,reason=normalize_field_entry(key,label,kind,values[key])
        out[key]=value
        quality[key]={'state':state,'reason':reason}
    # 仅对纳入的数值执行口径校验
    numeric=[(k,lbl) for k,lbl,kind in DOMAIN['fields'] if kind!='text']
    i=DOMAIN['index']
    if i==0:
        for key,label in numeric:
            if quality[key]['state']==STATE_INCLUDED and out[key]>100:
                quality[key]['state']=STATE_ANOMALOUS
                quality[key]['reason']=quality[key]['reason'] or '百分比超出0到100，隔离不纳入分母'
    if i==1:
        for key in ('success','failure'):
            if quality[key]['state']==STATE_INCLUDED:
                v=out[key]
                if v!=int(v) or v>100000:
                    quality[key]['state']=STATE_ANOMALOUS
                    quality[key]['reason']=quality[key]['reason'] or '计数必须为0到100000的整数，隔离'
        if all(quality[k]['state']==STATE_INCLUDED for k in ('success','failure')) and out['success']+out['failure']==0:
            raise ValueError('组内总数必须大于0')
    if i==4 and all(quality[k]['state']==STATE_INCLUDED for k in ('p0','p1')):
        if not 0<out['p0']<1 or not 0<out['p1']<1 or out['p0']==out['p1']:
            raise ValueError('两概率须在0和1之间且不同')
        if quality['cost_per_contact']['state']==STATE_INCLUDED and out['cost_per_contact']<=0: raise ValueError('成本与预算须为正')
        if quality['budget']['state']==STATE_INCLUDED and out['budget']<=0: raise ValueError('成本与预算须为正')
    return out,quality

def validate_parameters(params):
    defaults=DOMAIN['defaults']
    if not isinstance(params,dict) or set(params)-set(defaults): raise ValueError('存在未知分析参数')
    p=defaults|params
    if 'field' in p and p['field'] not in [f[0] for f in DOMAIN['fields']]: raise ValueError('未知指标')
    for key in ('resamples','simulations'):
        if key in p:
            v=p[key]
            if isinstance(v,bool) or not isinstance(v,(int,float)) or int(v)!=v or not 200<=v<=20000: raise ValueError('重采样次数必须是200到20000的整数')
            p[key]=int(v)
    for key in ('alpha','power'):
        if key in p:
            p[key]=float(p[key])
            if not math.isfinite(p[key]) or not (0.001<=p[key]<=0.2 if key=='alpha' else 0.5<=p[key]<=0.99): raise ValueError('显著性或功效超出范围')
    return p
