import os,json,logging
from pathlib import Path
from redis import Redis
from sqlalchemy import select
from src.config.database import Session
from src.models.run import Run
from src.models import user,study,observation,review,audit
from src.services.algorithm import calculate
from src.utils.serializers import checksum

ARTIFACT_DIR=Path(os.environ.get('ARTIFACT_DIR','/app/artifacts'))

def process_run(db,run):
    """单任务处理：冻结快照校验失败或算法失败只置 failed，绝不覆盖既有完成结果。
    返回最终状态字符串，供 worker 循环与失败恢复测试复用。"""
    if run.status in ('completed','failed'): return run.status
    run.status='running';db.commit()
    try:
        if checksum({'parameters':run.parameters,'snapshot':run.snapshot})!=run.checksum:
            raise ValueError('输入快照校验失败')
        result=calculate(run.snapshot,run.parameters,run.quality_report)
        result['input_sha256']=run.checksum
        run.result=result;run.status='completed';run.error=''
        try:
            ARTIFACT_DIR.mkdir(parents=True,exist_ok=True)
            (ARTIFACT_DIR/f'run-{run.id}.json').write_text(json.dumps(result,ensure_ascii=False,allow_nan=False))
        except OSError:
            logging.exception('artifact write failed; DB result retained')
    except Exception as e:
        logging.exception('analysis failed')
        # 失败保留旧结果：从未成功过的运行没有 result；已切换版本不受影响
        run.status='failed';run.error=str(e)
    db.commit()
    return run.status

def recover(redis):
    """恢复中断的 queued/running 任务；单 worker 模型，重新入队幂等。"""
    with Session() as db:
        pending=list(db.scalars(select(Run).where(Run.status.in_(['queued','running']))))
        for run in pending: redis.rpush('analysis-queue',run.id)
        return [r.id for r in pending]

def main():
    redis=Redis.from_url(os.environ['REDIS_URL'])
    ARTIFACT_DIR.mkdir(parents=True,exist_ok=True)
    recover(redis)
    while True:
        Path('/tmp/worker-heartbeat').touch()
        job=redis.blpop('analysis-queue',timeout=2)
        if not job: continue
        id=int(job[1])
        with Session() as db:
            run=db.get(Run,id)
            if not run: continue
            process_run(db,run)

if __name__=='__main__':
    main()
