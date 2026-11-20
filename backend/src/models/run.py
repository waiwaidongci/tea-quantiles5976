from sqlalchemy import Column,Integer,String,JSON,Text,ForeignKey,UniqueConstraint
from src.config.database import Base
class DistributionRun(Base):
    __tablename__='runs'
    __table_args__=(
        UniqueConstraint('study_id','checksum',name='uq_run_study_checksum'),
        UniqueConstraint('study_id','version',name='uq_run_study_version'),
    )
    id=Column(Integer,primary_key=True)
    study_id=Column(Integer,ForeignKey('studies.id'),nullable=False)
    # 同一业务批次内按冻结内容递增的研究版本；同内容重试不新增版本
    version=Column(Integer,nullable=False,default=1)
    name=Column(String(120),nullable=False)
    status=Column(String(20),default='queued')
    parameters=Column(JSON,nullable=False)
    snapshot=Column(JSON,nullable=False)
    # 冻结时刻的逐指标质量报告：纳入/隔离计数、隔离依据与各指标分母
    quality_report=Column(JSON,nullable=False,default=dict)
    checksum=Column(String(64),nullable=False)
    result=Column(JSON,nullable=True)
    error=Column(Text,default='')
    created_at=Column(String(40),nullable=False)
Run=DistributionRun
