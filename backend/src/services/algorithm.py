import math
import numpy as np
from scipy import stats
from src.services.domain_validation import STATE_INCLUDED

def included_values(snapshot,field):
    """按指标质量状态取值：只有 included 的观测进入该指标的分母。
    缺测/异常在冻结阶段已隔离，这里不再让它们静默拖入分母。"""
    if not snapshot: raise ValueError('空批次无法分析')
    values=[]
    for row in snapshot:
        quality=(row.get('quality') or {}).get(field) or {'state':STATE_INCLUDED}
        if quality.get('state',STATE_INCLUDED)!=STATE_INCLUDED: continue
        raw=row['values'].get(field)
        if raw is None: raise ValueError('纳入状态的观测缺少指标值')
        x=float(raw)
        if not math.isfinite(x): raise ValueError('观测必须为有限数值')
        values.append(x)
    return values

def quantile(values,q):
    values=sorted(float(x) for x in values)
    if not values: raise ValueError('无样本')
    h=(len(values)-1)*q;i=int(math.floor(h));j=int(math.ceil(h))
    return values[i]+(values[j]-values[i])*(h-i)

def calculate(snapshot,params,quality_report=None):
    field=params.get('field','moisture');B=int(params.get('resamples',2000))
    if field not in ('moisture','extract'): raise ValueError('未知指标')
    x=included_values(snapshot,field)
    if len(x)<3: raise ValueError(f"{field} 纳入分母仅{len(x)}份，至少3份独立茶样才能估计分布")
    if len(x)>5000 or not 200<=B<=20000: raise ValueError('样本/重采样超出上限')
    x=np.asarray(x,dtype=float)
    rng=np.random.default_rng(42)
    # Each selected TeaLot is one independent batch. Resampling stays within batch.
    boot=[]
    for start in range(0,B,100):
        draws=rng.choice(x,size=(min(100,B-start),len(x)),replace=True)
        boot.extend(np.median(draws,axis=1).tolist())
    lower,upper=quantile(boot,.025),quantile(boot,.975)
    q={label:quantile(x,p) for label,p in [('最小值',0),('P25',.25),('中位数',.5),('P75',.75),('最大值',1)]}
    unique=np.unique(x);ecdf=[[float(v),float(np.count_nonzero(x<=v)/len(x))] for v in unique]
    # Plot a sampled ECDF for large batches; full ECDF is retained in result JSON.
    step=max(1,len(ecdf)//24)
    denominator=int(len(x))
    if quality_report:
        qf=quality_report['fields'][field]
        isolated=qf['missing_count']+qf['anomalous_count']
        isolation_note=f"分母{denominator}份（缺测隔离{qf['missing_count']}份、异常隔离{qf['anomalous_count']}份）"
    else:
        isolation_note=f"分母{denominator}份"
    field_label={'moisture':'含水率(%)','extract':'水浸出率(%)'}[field]
    return {'metrics':{'纳入分母':denominator,'样本量':denominator,'均值':float(np.mean(x)),'样本标准差':float(np.std(x,ddof=1)),**q,'中位数95%区间':[lower,upper],'IQR':q['P75']-q['P25'],'重采样次数':B,'随机种子':42},'interpretation':f"本批{field_label}共{isolation_note}，中位数为{q['中位数']:.3f}，95% percentile bootstrap置信区间为[{lower:.3f}, {upper:.3f}]。",'caveat':'本区间描述本批总体中位数估计的不确定性，不是单份茶样预测区间；独立同分布的批内抽样假设需人工核对。常量批次区间会退化。缺测与异常观测未进入分母，隔离依据见质量报告。','columns':['分位点','线性插值结果'],'rows':[[k,v] for k,v in q.items()],'table_title':'分布位置与离散程度','ecdf':ecdf,'chart':{'title':'经验累积分布 ECDF · 横条为累计比例','points':ecdf[::step]}}
