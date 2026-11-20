"""HTTP 级集成：登录、逐指标录入、冻结版本、复核切换、角色下载门禁。"""
import os
from types import SimpleNamespace
from unittest.mock import patch
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine,select
from sqlalchemy.orm import sessionmaker

os.environ.setdefault('DATABASE_URL','sqlite:////tmp/tea_http.db')
os.environ.setdefault('JWT_SECRET','http-secret')
os.environ.setdefault('REDIS_URL','redis://unused/0')
os.environ.setdefault('ARTIFACT_DIR','/tmp/tea_http_art')

from src.config.database import Base,get_db
from src.main import app
from src.models.user import User
from src.models.run import Run
from src.models.study import Study
from src.utils.security import hash_password

class FakeRedis:
    def __init__(self): self.kv={};self.q=[]
    def rpush(self,k,v): self.q.append(int(v))
    def incr(self,k,amount=1): self.kv[k]=self.kv.get(k,0)+1;return self.kv[k]
    def expire(self,*a): return True
    def ping(self): return True

@pytest.fixture()
def client(tmp_path):
    engine=create_engine(f'sqlite:///{tmp_path}/http.db')
    Base.metadata.create_all(engine)
    factory=sessionmaker(bind=engine,expire_on_commit=False)
    with factory() as db:
        for role in ('admin','analyst','viewer'):
            db.add(User(email=f'{role}@demo.local',role=role,password_hash=hash_password('pw123456')))
        db.commit()
    def override():
        with factory() as s: yield s
    app.dependency_overrides[get_db]=override
    fake=FakeRedis()
    from src.middlewares import rate_limit
    with patch.object(rate_limit,'redis',fake), \
         patch('src.services.runs._redis',lambda: fake), \
         patch('src.main.Redis') as MR:
        MR.from_url.return_value=fake
        c=TestClient(app);yield c,factory,fake
    app.dependency_overrides.clear()

def tok(client,role):
    r=client.post('/auth/login',json={'email':f'{role}@demo.local','password':'pw123456'})
    assert r.status_code==200
    return r.headers and r.json()['token']

def H(t): return {'Authorization':f'Bearer {t}'}

def _process(factory):
    from src.worker import process_run
    with factory() as db:
        for run in db.scalars(select(Run).where(Run.status.in_(['queued','running']))):
            process_run(db,run)

def test_full_http_flow(client):
    c,factory,fake=client
    admin,analyst,viewer=tok(c,'admin'),tok(c,'analyst'),tok(c,'viewer')
    # 建批次
    r=c.post('/studies',json={'name':'HTTP批次','source_filename':'h.csv','description':''},headers=H(admin))
    assert r.status_code==200; sid=r.json()['id']
    # 交错补录：K1 缺 extract；K2 显式异常 moisture
    rows=[('K1',{'moisture':5.1,'extract':None}),
          ('K2',{'moisture':{'state':'anomalous','value':6.0,'reason':'复测偏离'},'extract':33.0}),
          ('K3',{'moisture':5.3,'extract':34.0}),
          ('K4',{'moisture':5.4,'extract':35.0})]
    ids={}
    for key,values in rows:
        rr=c.post(f'/studies/{sid}/observations',json={'source_key':key,'values':values},headers=H(analyst))
        assert rr.status_code==200,rr.text; ids[key]=rr.json()['id']
    got=c.get(f'/studies/{sid}/observations',headers=H(analyst)).json()
    k1=next(x for x in got if x['source_key']=='K1')
    assert k1['quality']['extract']['state']=='missing'
    # 冻结 v1（extract 分母3）
    r=c.post('/runs',json={'study_id':sid,'name':'第一版研究','parameters':{'field':'extract','resamples':300}},headers=H(analyst))
    assert r.status_code==200; run1=r.json(); assert run1['version']==1
    _process(factory)
    r1=c.get(f"/runs/{run1['id']}",headers=H(analyst)).json()
    assert r1['status']=='completed' and r1['quality_report']['fields']['extract']['denominator']==3
    # 同内容重试不新增
    r=c.post('/runs',json={'study_id':sid,'name':'第一版研究','parameters':{'field':'extract','resamples':300}},headers=H(analyst))
    assert r.json()['id']==run1['id']
    # 未复核：viewer 403，下载拒绝
    assert c.get(f"/runs/{run1['id']}",headers=H(viewer)).status_code==403
    assert c.get(f"/runs/{run1['id']}/export",headers=H(analyst)).status_code==409
    # analyst 不能复核
    assert c.post(f"/runs/{run1['id']}/review",json={'note':'分析师无权复核'},headers=H(analyst)).status_code==403
    # admin 复核切换
    r=c.post(f"/runs/{run1['id']}/review",json={'note':'各指标分母与隔离依据核对通过'},headers=H(admin))
    assert r.status_code==200
    rel=c.get(f"/runs/{run1['id']}",headers=H(viewer)).json()
    assert rel['released'] is True
    csv_text=c.get(f"/runs/{run1['id']}/export",headers=H(viewer)).content.decode()
    assert '纳入分母' in csv_text and '复测偏离' in csv_text
    # 补录后冻结 v2，复核切换；v1 对 viewer 关闭
    r=c.put(f"/studies/{sid}/observations/{ids['K1']}",
            json={'source_key':'K1','values':{'moisture':5.1,'extract':32.1}},headers=H(analyst))
    assert r.status_code==200
    r=c.post('/runs',json={'study_id':sid,'name':'第二版研究','parameters':{'field':'extract','resamples':300}},headers=H(analyst))
    run2=r.json(); assert run2['version']==2
    _process(factory)
    r2=c.get(f"/runs/{run2['id']}",headers=H(analyst)).json()
    assert r2['quality_report']['fields']['extract']['denominator']==4
    # 切换前 v1 仍可下载，v2 不可
    assert c.get(f"/runs/{run2['id']}/export",headers=H(admin)).status_code==409
    c.post(f"/runs/{run2['id']}/review",json={'note':'补录后分母4核对通过，切换'},headers=H(admin))
    assert c.get(f"/runs/{run2['id']}",headers=H(viewer)).json()['released'] is True
    assert c.get(f"/runs/{run1['id']}",headers=H(viewer)).status_code==403
    # viewer 列表只见当前版本
    visible=c.get('/runs',headers=H(viewer)).json()
    assert {x['id'] for x in visible}=={run2['id']}
    # 审计含切换记录
    audits=c.get('/audits',headers=H(admin)).json()
    assert any(a['action']=='run.switch' for a in audits)
