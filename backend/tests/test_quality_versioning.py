"""验证套件：逐指标质量隔离、版本化冻结、冻结撞写入与失败恢复。

本地无 Postgres/Redis，使用 SQLite + 内存假 Redis 验证服务与算法语义；
行锁 with_for_update 在 SQLite 为 no-op，撞写入由 (study_id,checksum)/(study_id,version)
两条唯一约束 + IntegrityError 幂等重查兜底（与 Postgres 上的行锁+约束行为等价）。
"""
import os,threading,sqlite3
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import select,create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.exc import IntegrityError

os.environ.setdefault('DATABASE_URL','sqlite:////tmp/tea_test.db')
os.environ.setdefault('JWT_SECRET','test-secret')
os.environ.setdefault('REDIS_URL','redis://unused/0')
os.environ.setdefault('ARTIFACT_DIR','/tmp/tea_test_artifacts')

from src.config.database import Base
from src.models import user,study,observation,run,review,audit
from src.models.review import Review
from src.models.run import Run
from src.models.study import Study
from src.models.observation import Observation
from src.services import studies,runs
from src.services.quality import build_quality_report
from src.worker import process_run,recover
from src.schemas.study import StudyInput
from src.schemas.observation import ObservationInput
from src.schemas.run import RunInput
from src.schemas.review import ReviewInput

ADMIN=SimpleNamespace(email='admin@demo.local',role='admin')
ANALYST=SimpleNamespace(email='analyst@demo.local',role='analyst')
VIEWER=SimpleNamespace(email='viewer@demo.local',role='viewer')

class FakeRedis:
    def __init__(self): self.q=[];self.store={}
    def rpush(self,k,v): self.q.append(int(v));return len(self.q)
    def blpop(self,k,timeout=0): return None
    def incr(self,k,amount=1): self.store[k]=self.store.get(k,0)+amount;return self.store[k]
    def expire(self,*a): return True
    def ping(self): return True


@pytest.fixture()
def db_factory(tmp_path):
    engine=create_engine(f'sqlite:///{tmp_path}/test.db')
    Base.metadata.create_all(engine)
    factory=sessionmaker(bind=engine,expire_on_commit=False)
    fake=FakeRedis()
    with patch('src.services.runs._redis',lambda: fake),\
         patch('src.middlewares.rate_limit.Redis') as R:
        R.from_url.return_value=fake
        yield factory,fake
    engine.dispose()


def _seed_study(factory,sid_rows,name='批次',admin=ADMIN):
    with factory() as db:
        s=studies.create(db,StudyInput(name=name,source_filename='t.csv'),admin)
        sid=s['id']
        ids={}
        for key,values in sid_rows:
            r=studies.save_observation(db,sid,ObservationInput(source_key=key,values=values),admin)
            ids[key]=r['id']
        return sid,ids


def _good_rows(prefix,n,extract_holes=()):
    return [(f'{prefix}-{i:03}',{'moisture':5.0+i*.1,
            'extract':None if i in extract_holes else 30.0+i*.2}) for i in range(n)]


# ---------- 场景一：交错补录 + 逐指标质量状态与分母 ----------
class TestInterleavedBackfill:
    def test_partial_row_isolates_missing_per_field(self,db_factory):
        factory,_=db_factory
        rows=_good_rows('K',6,extract_holes=(1,3))
        sid,ids=_seed_study(factory,rows)
        with factory() as db:
            saved=studies.observations(db,sid)
            q1=next(x for x in saved if x['source_key']=='K-001')
            assert q1['quality']['extract']['state']=='missing'
            assert q1['quality']['moisture']['state']=='included'
            assert q1['values']['extract'] is None
            r=runs.create(db,RunInput(study_id=sid,name='v1',parameters={'field':'extract'}),ANALYST)
            process_run(db,db.get(Run,r['id']))
            runobj=db.get(Run,r['id'])
            qf=runobj.quality_report['fields']
            assert qf['extract']['denominator']==4 and qf['extract']['missing_count']==2
            assert qf['moisture']['denominator']==6
            # 缺测没有静默进入 extract 分母；算法结果只基于4份
            assert runobj.result['metrics']['纳入分母']==4
            assert set(qf['extract']['included_source_keys'])=={'K-000','K-002','K-004','K-005'}

    def test_backfill_creates_new_version_with_silent_hole_closed(self,db_factory):
        factory,fake=db_factory
        rows=_good_rows('K',5,extract_holes=(2,))
        sid,ids=_seed_study(factory,rows)
        with factory() as db:
            r1=runs.create(db,RunInput(study_id=sid,name='研究',parameters={'field':'extract'}),ANALYST)
            process_run(db,db.get(Run,r1['id']))
            assert db.get(Run,r1['id']).quality_report['fields']['extract']['denominator']==4
        # 交错补录：只补齐 K-002 的 extract（修订同一行）
        with factory() as db:
            studies.save_observation(db,sid,ObservationInput(
                source_key='K-002',values={'moisture':5.2,'extract':33.3}),ADMIN,row_id=ids['K-002'])
        with factory() as db:
            r2=runs.create(db,RunInput(study_id=sid,name='研究',parameters={'field':'extract'}),ANALYST)
            assert r2['id']!=r1['id'] and (r2['version'],r1['version'])==(2,1)
            process_run(db,db.get(Run,r2['id']))
            assert db.get(Run,r2['id']).quality_report['fields']['extract']['denominator']==5
            # 旧版本快照与分母原样保留
            assert db.get(Run,r1['id']).quality_report['fields']['extract']['denominator']==4

    def test_anomalous_value_isolated_with_reason(self,db_factory):
        factory,_=db_factory
        rows=_good_rows('K',4)
        sid,ids=_seed_study(factory,rows)
        with factory() as db:
            # 显式异常登记 + 原因；越界值也被自动判异常
            studies.save_observation(db,sid,ObservationInput(source_key='K-000',values={
                'moisture':{'state':'anomalous','value':5.0,'reason':'仪器漂移复测前隔离'},
                'extract':150.0}),ADMIN,row_id=ids['K-000'])
            r=runs.create(db,RunInput(study_id=sid,name='研究',parameters={'field':'moisture'}),ANALYST)
            process_run(db,db.get(Run,r['id']))
            qf=db.get(Run,r['id']).quality_report['fields']
            assert qf['moisture']['anomalous_count']==1
            assert qf['moisture']['anomalous'][0]['reason']=='仪器漂移复测前隔离'
            assert qf['extract']['anomalous_count']==1 and qf['extract']['denominator']==3
            iso=db.get(Run,r['id']).quality_report['isolated_rows']
            assert any(x['source_key']=='K-000' for x in iso)


# ---------- 场景二：重复修订只产生新版本；同版本重试不新增研究 ----------
class TestRepeatedRevisionsAndRetry:
    def test_each_distinct_freeze_is_a_new_version_retry_reuses(self,db_factory):
        factory,fake=db_factory
        rows=_good_rows('K',5)
        sid,ids=_seed_study(factory,rows)
        created=[]
        with factory() as db:
            first=runs.create(db,RunInput(study_id=sid,name='研究',parameters={'field':'moisture'}),ANALYST)
            created.append(first['id'])
            # 同内容重复提交：同一运行、同一版本、重新排队
            again=runs.create(db,RunInput(study_id=sid,name='研究',parameters={'field':'moisture'}),ANALYST)
            assert again['id']==first['id'] and again['version']==1
            process_run(db,db.get(Run,first['id']))
            # 完成后再提交同内容：仍不新增研究
            done_again=runs.create(db,RunInput(study_id=sid,name='研究',parameters={'field':'moisture'}),ANALYST)
            assert done_again['id']==first['id']
        for i in range(3):  # 重复修订同一行三次，每次冻结内容不同
            with factory() as db:
                studies.save_observation(db,sid,ObservationInput(
                    source_key='K-001',values={'moisture':6.0+i,'extract':31.0}),ADMIN,row_id=ids['K-001'])
            with factory() as db:
                rr=runs.create(db,RunInput(study_id=sid,name='研究',parameters={'field':'moisture'}),ANALYST)
                created.append(rr['id'])
        with factory() as db:
            versions=db.scalars(select(Run.version).where(Run.study_id==sid).order_by(Run.version)).all()
            assert versions==[1,2,3,4]
            checksums=db.scalars(select(Run.checksum).where(Run.study_id==sid)).all()
            assert len(set(checksums))==4  # 版本间 checksum 互不相同
            assert len(created)==4 and len(set(created))==4


# ---------- 场景三：冻结撞写入 ----------
class TestFreezeCollision:
    def test_concurrent_identical_freeze_collapses_to_one_run(self,db_factory):
        factory,fake=db_factory
        rows=_good_rows('K',6)
        sid,_=_seed_study(factory,rows,name='并发批次')
        outcomes,errors=[],[]
        def worker():
            try:
                with factory() as db:
                    r=runs.create(db,RunInput(study_id=sid,name='并发',parameters={'field':'moisture'}),ANALYST)
                    outcomes.append(r['id'])
            except Exception as e:  # SQLite 写锁可能抛 OperationalError/HTTPException
                errors.append(type(e).__name__)
        threads=[threading.Thread(target=worker) for _ in range(6)]
        for t in threads:t.start()
        for t in threads:t.join()
        with factory() as db:
            ids=db.scalars(select(Run.id).where(Run.study_id==sid)).all()
            # 无论锁冲突如何，最终只有一条研究、一个版本、一个checksum
            assert len(ids)==1
            runobj=db.get(Run,ids[0])
            assert runobj.version==1
            assert db.scalar(select(__import__('sqlalchemy').func.count()).select_from(Run)
                             .where(Run.study_id==sid,Run.checksum==runobj.checksum))==1
        assert set(outcomes).issubset(set(ids)) or errors  # 成功的都拿到同一id

    def test_unique_constraints_reject_duplicate_checksum_and_version(self,db_factory):
        factory,_=db_factory
        rows=_good_rows('K',4)
        sid,_=_seed_study(factory,rows,name='约束批次')
        with factory() as db:
            r=runs.create(db,RunInput(study_id=sid,name='研究',parameters={'field':'moisture'}),ANALYST)
            # 绕过服务直接插重复 checksum / version，约束必须拒绝
            dup=Run(study_id=sid,version=99,name='dup',parameters=r['parameters'],
                    snapshot=r['snapshot'],quality_report=r['quality_report'],
                    checksum=r['checksum'],created_at=r['created_at'])
            db.add(dup)
            with pytest.raises(IntegrityError): db.flush()
            db.rollback()
            same_version=Run(study_id=sid,version=r['version'],name='v',parameters={'field':'extract','resamples':500},
                             snapshot=r['snapshot'],quality_report=r['quality_report'],
                             checksum='x'*64,created_at=r['created_at'])
            db.add(same_version)
            with pytest.raises(IntegrityError): db.flush()


# ---------- 场景四：失败恢复 + 一次性切换 + 开放门禁 ----------
class TestFailureRecoveryAndRelease:
    def test_failed_run_kept_retry_same_version_fix_forward_switches(self,db_factory):
        factory,_=db_factory
        # 全部 extract 缺测 -> 分母0，必然 failed
        bad=[(f'F-{i:03}',{'moisture':1.0+i,'extract':None}) for i in range(5)]
        sid,ids=_seed_study(factory,bad,name='失败批次')
        with factory() as db:
            rf=runs.create(db,RunInput(study_id=sid,name='fail',parameters={'field':'extract'}),ANALYST)
            assert process_run(db,db.get(Run,rf['id']))=='failed'
            failed=db.get(Run,rf['id'])
            assert failed.error and failed.result is None
        # 同版本重试：id/version 不变，重新 queued，再算仍失败
        with factory() as db:
            retry=runs.create(db,RunInput(study_id=sid,name='fail',parameters={'field':'extract'}),ANALYST)
            assert retry['id']==rf['id'] and retry['version']==rf['version'] and retry['status']=='queued'
            assert process_run(db,db.get(Run,rf['id']))=='failed'
            # 失败版本不能复核
            with pytest.raises(Exception) as ei:
                runs.approve(db,rf['id'],ReviewInput(note='失败版本不应通过复核'),ADMIN)
            assert getattr(ei.value,'status_code',None)==409
        # 修复数据后冻结新版本 -> 成功并一次性切换
        for i in range(5):
            with factory() as db:
                studies.save_observation(db,sid,ObservationInput(
                    source_key=f'F-{i:03}',values={'moisture':1.0+i,'extract':20.0+i}),ADMIN,row_id=ids[f'F-{i:03}'])
        with factory() as db:
            ok=runs.create(db,RunInput(study_id=sid,name='fix',parameters={'field':'extract'}),ANALYST)
            assert ok['version']==2
            process_run(db,db.get(Run,ok['id']))
            runs.approve(db,ok['id'],ReviewInput(note='修复后各指标分母核对通过，切换'),ADMIN)
            study=db.get(Study,sid)
            assert study.current_run_id==ok['id']
            assert runs.get(db,ok['id'])['released'] is True
            # 失败运行原样保留可追溯
            assert db.get(Run,rf['id']).status=='failed'

    def test_switch_failure_keeps_old_result(self,db_factory):
        factory,_=db_factory
        rows=_good_rows('K',5)
        sid,ids=_seed_study(factory,rows,name='切换批次')
        with factory() as db:
            r1=runs.create(db,RunInput(study_id=sid,name='r1',parameters={'field':'moisture'}),ANALYST)
            process_run(db,db.get(Run,r1['id']))
            runs.approve(db,r1['id'],ReviewInput(note='第一版核对通过先切换'),ADMIN)
            assert db.get(Study,sid).current_run_id==r1['id']
        # 第二版复核事务内注入失败：旧指针与旧结果保留
        with factory() as db:
            r2=runs.create(db,RunInput(study_id=sid,name='r2',parameters={'field':'moisture','resamples':500}),ANALYST)
            process_run(db,db.get(Run,r2['id']))
            with patch.object(db,'commit',side_effect=RuntimeError('simulated commit crash')):
                with pytest.raises(RuntimeError):
                    runs.approve(db,r2['id'],ReviewInput(note='第二版核对但提交失败'),ADMIN)
            db.rollback()
        with factory() as db:
            assert db.get(Study,sid).current_run_id==r1['id']  # 旧结果保留
            assert db.scalar(select(Review).where(Review.run_id==r2['id'])) is None
            assert runs.get(db,r1['id'])['released'] is True

    def test_results_and_download_only_open_to_released_version(self,db_factory):
        factory,_=db_factory
        rows=_good_rows('K',5)
        sid,ids=_seed_study(factory,rows,name='门禁批次')
        with factory() as db:
            r1=runs.create(db,RunInput(study_id=sid,name='r1',parameters={'field':'moisture'}),ANALYST)
            process_run(db,db.get(Run,r1['id']))
            runs.approve(db,r1['id'],ReviewInput(note='第一版核对通过并生效'),ADMIN)
        # 新版本完成但未复核：不是当前版本
        with factory() as db:
            studies.save_observation(db,sid,ObservationInput(
                source_key='K-000',values={'moisture':9.9,'extract':30.0}),ADMIN,row_id=ids['K-000'])
            r2=runs.create(db,RunInput(study_id=sid,name='r2',parameters={'field':'moisture'}),ANALYST)
            process_run(db,db.get(Run,r2['id']))
            # analyst 可看未开放版本用于核对，但不能下载
            assert runs.get(db,r2['id'],ANALYST)['released'] is False
            with pytest.raises(Exception) as ei: runs.export(db,r2['id'],ANALYST)
            assert ei.value.status_code==409
            # viewer 直链403、列表过滤
            with pytest.raises(Exception) as ev: runs.get(db,r2['id'],VIEWER)
            assert ev.value.status_code==403
            visible=runs.list_runs(db,VIEWER)
            assert {x['id'] for x in visible}=={r1['id']}
            # 旧当前版本可下载，CSV 含各指标分母与隔离依据
            csv_text=runs.export(db,r1['id'],VIEWER)
            assert '纳入分母' in csv_text and '缺测隔离数' in csv_text
        # 复核切换后 r2 开放，r1 转为旧版本
        with factory() as db:
            runs.approve(db,r2['id'],ReviewInput(note='第二版核对通过并切换'),ADMIN)
            assert runs.get(db,r2['id'])['released'] is True
            assert runs.get(db,r1['id'],ANALYST)['released'] is False
            with pytest.raises(Exception) as ev: runs.get(db,r1['id'],VIEWER)
            assert ev.value.status_code==403


# ---------- 冻结审计：纳入/隔离/原因均留痕 ----------
class TestFreezeAuditTrail:
    def test_freeze_records_versions_and_isolation_reasons(self,db_factory):
        factory,_=db_factory
        rows=_good_rows('K',4,extract_holes=(0,))
        sid,ids=_seed_study(factory,rows,name='审计批次')
        with factory() as db:
            r=runs.create(db,RunInput(study_id=sid,name='研究',parameters={'field':'extract'}),ANALYST)
            process_run(db,db.get(Run,r['id']))
            report=db.get(Run,r['id']).quality_report
            missing=report['fields']['extract']['missing']
            assert missing[0]['source_key']=='K-000'
            entries=db.scalars(select(audit.Audit).where(audit.Audit.action.in_(['run.queue','run.retry']))).all()
            assert any(f'v{r["version"]}' in e.detail for e in entries)
            # 观测保存审计带逐指标状态
            save_logs=db.scalars(select(audit.Audit).where(audit.Audit.action=='observation.save')).all()
            assert any('extract:missing' in e.detail for e in save_logs)


def test_worker_recover_requeues_interrupted_jobs(db_factory):
    factory,fake=db_factory
    rows=_good_rows('K',5)
    sid,_=_seed_study(factory,rows,name='恢复批次')
    with factory() as db:
        r=runs.create(db,RunInput(study_id=sid,name='待恢复研究',parameters={'field':'moisture'}),ANALYST)
        rid=r['id']
        # 模拟 worker 取出后崩溃：状态停在 running，队列已空
        db.get(Run,rid).status='running';db.commit()
    fake.q.clear()
    with patch('src.worker.Session',factory):
        recovered=recover(fake)
    assert recovered==[rid] and fake.q==[rid]
    # 恢复后重新处理成功
    with factory() as db:
        assert process_run(db,db.get(Run,rid))=='completed'


def test_quality_report_unit_shape():
    rows=[{'source_key':'A','values':{'moisture':1.0,'extract':None},
           'quality':{'moisture':{'state':'included','reason':''},'extract':{'state':'missing','reason':'仪器未回传'}}},
          {'source_key':'B','values':{'moisture':2.0,'extract':40.0},
           'quality':{'moisture':{'state':'included','reason':''},'extract':{'state':'included','reason':''}}},
          {'source_key':'C','values':{'moisture':200.0,'extract':41.0},
           'quality':{'moisture':{'state':'anomalous','reason':'越界'},'extract':{'state':'included','reason':''}}}]
    rep=build_quality_report(rows)
    assert rep['fields']['moisture']['denominator']==2
    assert rep['fields']['extract']['denominator']==2
    assert rep['fields']['moisture']['anomalous'][0]['reason']=='越界'
    assert rep['fields']['extract']['missing'][0]['reason']=='仪器未回传'
    assert {x['source_key'] for x in rep['isolated_rows']}=={'A','C'}
