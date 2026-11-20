from src.config.domain import DOMAIN
from src.services.domain_validation import STATE_INCLUDED,STATE_MISSING,STATE_ANOMALOUS

STATE_LABELS={
    STATE_INCLUDED:'纳入',
    STATE_MISSING:'缺测隔离',
    STATE_ANOMALOUS:'异常隔离',
}

def _label_of(field_key):
    for key,label,kind in DOMAIN['fields']:
        if key==field_key: return label
    return field_key

def build_quality_report(rows):
    """冻结时刻逐指标汇总：每个指标的纳入分母、缺测/异常隔离清单（含原因）。
    rows 为观测记录字典，至少含 source_key/values/quality。"""
    fields=[(key,label) for key,label,kind in DOMAIN['fields'] if kind!='text']
    report={'fields':{},'row_count':len(rows),'isolated_rows':[]}
    for key,label in fields:
        included,missing,anomalous=[],[],[]
        for row in rows:
            quality=(row.get('quality') or {}).get(key) or {'state':STATE_INCLUDED,'reason':''}
            state=quality.get('state',STATE_INCLUDED)
            reason=quality.get('reason','')
            item={'source_key':row['source_key'],'value':row['values'].get(key),'reason':reason}
            if state==STATE_MISSING: missing.append(item)
            elif state==STATE_ANOMALOUS: anomalous.append(item)
            else: included.append(item)
        report['fields'][key]={
            'label':label,
            'state':'included',
            'denominator':len(included),
            'included_count':len(included),
            'missing_count':len(missing),
            'anomalous_count':len(anomalous),
            'included_source_keys':[x['source_key'] for x in included],
            'missing':missing,
            'anomalous':anomalous,
        }
    # 行级视角：任一指标被隔离即列出，页面可直接查隔离依据
    for row in rows:
        q=row.get('quality') or {}
        bad={k:v for k,v in q.items() if v.get('state')!=STATE_INCLUDED}
        if bad:
            report['isolated_rows'].append({
                'source_key':row['source_key'],
                'values':row.get('values'),
                'isolation':{k:{'state':v.get('state'),'label':STATE_LABELS.get(v.get('state'),v.get('state')),'reason':v.get('reason','')} for k,v in bad.items()},
            })
    return report

def field_denominator(quality_report,field_key):
    return quality_report['fields'][field_key]['denominator']
