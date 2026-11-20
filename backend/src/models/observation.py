from sqlalchemy import Column,Integer,String,JSON,ForeignKey,UniqueConstraint
from src.config.database import Base
class TastingReading(Base):
    __tablename__='observations'
    __table_args__=(UniqueConstraint('study_id','source_key'),)
    id=Column(Integer,primary_key=True)
    study_id=Column(Integer,ForeignKey('studies.id'),nullable=False)
    source_key=Column(String(100),nullable=False)
    # values 仅保存各指标的原始数值或 None（缺测）；逐指标质量状态与原因在 quality
    values=Column(JSON,nullable=False)
    # {'moisture': {'state':'included|missing|anomalous','reason':''}, ...}
    quality=Column(JSON,nullable=False,default=dict)
Observation=TastingReading
