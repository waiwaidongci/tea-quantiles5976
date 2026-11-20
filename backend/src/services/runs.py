import os,json,csv,io
from redis import Redis
from sqlalchemy import select,func
from sqlalchemy.exc import IntegrityError
from fastapi import HTTPException
from src.models.run import Run
from src.models.study import Study
from src.models.review import Review
from src.services import studies
from src.services.quality import build_quality_report
from src.services.domain_validation import validate_parameters
from src.services.audits import log
from src.utils.serializers import record,now,checksum

def _redis(): return Redis.from_url(os.environ['REDIS_URL'])

def _next_version(db,study_id):
    top=db.scalar(select(func.coalesce(func.max(Run.version),0)).where(Run.study_id==study_id))
    return int(top)+1

def _freeze(rows):
    """冻结内容同时携带逐指标质量状态，保证隔离依据随快照固化。"""
    return [{'source_key':x['source_key'],'values':x['values'],'quality':x.get('quality') or {}} for x in rows]

def create(db,data,user):
    study=studies.require(db,data.study_id)
    rows=studies.observations(db,data.study_id)
    if not rows: raise HTTPException(422,'先录入至少一条业务观测')
    try: params=validate_parameters(data.parameters)
    except (ValueError,TypeError) as e: raise HTTPException(422,str(e))
    snapshot=_freeze(rows)
    report=build_quality_report(snapshot)
    # checksum 覆盖参数与含质量状态的快照；同内容重试命中同一运行，不新增研究
    digest=checksum({'parameters':params,'snapshot':snapshot})
    # 行锁串行化同批次冻结撞写入（Postgres/MySQL 行锁；SQLite 退化为全库写锁）
    db.refresh(study)
    try: locked=db.scalar(select(Study).where(Study.id==study.id).with_for_update())
    except Exception: locked=study
    existing=db.scalar(select(Run).where(Run.study_id==study.id,Run.checksum==digest))
    if existing:
        if existing.status=='completed':
            log(db,user,'run.retry.duplicate',f'{existing.id}: 相同冻结内容已完成，未新增研究')
            db.commit();return record(existing)
        # 同版本重试：保留版本号与历史，只重新排队，不新增运行
        existing.status='queued';existing.error=''
        log(db,user,'run.retry',f'{existing.id}: 相同冻结内容重试，沿用版本v{existing.version}');db.commit()
        _redis().rpush('analysis-queue',existing.id)
        return record(existing)
    run=Run(study_id=study.id,version=_next_version(db,study.id),name=data.name,parameters=params,
            snapshot=snapshot,quality_report=report,checksum=digest,created_at=now(),status='queued')
    db.add(run)
    try:
        db.flush()
    except IntegrityError:
        # 冻结撞写入：唯一约束(study_id,checksum/version)兜底，回滚后重查走同一版本，不新增研究
        db.rollback()
        twin=db.scalar(select(Run).where(Run.study_id==study.id,Run.checksum==digest))
        if twin:
            if twin.status!='completed':
                twin.status='queued';twin.error='';db.commit();_redis().rpush('analysis-queue',twin.id)
            else:
                log(db,user,'run.retry.duplicate',f'{twin.id}: 并发相同冻结内容已存在，未新增研究');db.commit()
            return record(twin)
        raise HTTPException(409,'并发冻结冲突，请刷新后重试')
    log(db,user,'run.queue',f'{run.id} v{run.version}');db.commit()
    _redis().rpush('analysis-queue',run.id)
    return record(run)

def _is_current(run,study): return study is not None and study.current_run_id==run.id

def get(db,id,user=None):
    run=db.get(Run,id)
    if not run: raise HTTPException(404,'运行不存在')
    result=record(run)
    study=db.get(Study,run.study_id)
    result['is_current']=_is_current(run,study)
    review=db.scalar(select(Review).where(Review.run_id==id))
    result['review']=record(review) if review else None
    result['released']=bool(result['is_current'] and review)
    # 结果与下载只开放核对完整（已复核且为当前版本）的版本；观察员不可查看未开放版本
    if user is not None and user.role=='viewer' and not result['released']:
        raise HTTPException(403,'该版本尚未核对完整或未生效；观察员仅可查看已复核的当前版本')
    return result

def list_runs(db,user=None):
    ids=list(db.scalars(select(Run.id).order_by(Run.id.desc())))
    out=[]
    for i in ids:
        try: out.append(get(db,i,user))
        except HTTPException as e:
            # 观察员列表只呈现核对完整的版本，未开放版本静默过滤；直链仍返回403
            if user is not None and user.role=='viewer' and e.status_code==403: continue
            raise
    return out

def approve(db,id,data,user):
    run=db.get(Run,id)
    if not run: raise HTTPException(404,'运行不存在')
    study=db.get(Study,run.study_id)
    # 先锁批次行，串行化同一批次的复核切换；锁内重新检查状态与重复复核
    try: db.scalar(select(Study).where(Study.id==study.id).with_for_update())
    except Exception: pass
    db.refresh(run)
    if run.status!='completed': raise HTTPException(409,'只能复核已完成运行；失败版本请修数后冻结新版本')
    if db.scalar(select(Review).where(Review.run_id==id)): raise HTTPException(409,'已复核，记录不可覆盖')
    obj=Review(run_id=id,note=data.note,author=user.email,created_at=now())
    db.add(obj);db.flush()
    # 核对完成后一次性切换：复核归档与当前版本指针在同一事务提交；失败则旧结果保留
    previous=study.current_run_id
    study.current_run_id=run.id
    log(db,user,'review.approve',f'{id} v{run.version}: {data.note}')
    log(db,user,'run.switch',f'study {study.id}: {previous} -> {id} v{run.version}')
    db.commit()
    return record(obj)

def export(db,id,user):
    r=get(db,id,user)
    # 下载只开放核对完整的版本
    if not r['released']: raise HTTPException(409,'该版本未核对完整或不是当前生效版本，不能下载；请先完成复核切换')
    if not r['result']: raise HTTPException(409,'尚无完成结果')
    stream=io.StringIO();w=csv.writer(stream)
    w.writerow(['运行编号','版本','输入校验SHA256','指标','值'])
    for k,v in r['result']['metrics'].items():
        w.writerow([id,f"v{r['version']}",r['checksum'],k,json.dumps(v,ensure_ascii=False) if isinstance(v,(dict,list)) else v])
    w.writerow([id,f"v{r['version']}",r['checksum'],'解释',r['result']['interpretation']])
    # 各指标分母与隔离依据随下载一并给出
    w.writerow([]);w.writerow(['指标','纳入分母','缺测隔离数','异常隔离数','缺测依据(source_key:原因)','异常依据(source_key:原因)'])
    for key,qf in r['quality_report']['fields'].items():
        w.writerow([qf['label'],qf['denominator'],qf['missing_count'],qf['anomalous_count'],
                    '; '.join(f"{x['source_key']}:{x['reason'] or '缺测'}" for x in qf['missing']),
                    '; '.join(f"{x['source_key']}:{x['reason'] or '异常'}" for x in qf['anomalous'])])
    log(db,user,'run.export',f"{id} v{r['version']}");db.commit()
    return '﻿'+stream.getvalue()
