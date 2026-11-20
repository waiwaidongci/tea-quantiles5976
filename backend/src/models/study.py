from sqlalchemy import Column,Integer,String,Text
from src.config.database import Base
class TeaLot(Base):
    __tablename__='studies'
    id=Column(Integer,primary_key=True)
    name=Column(String(120),unique=True,nullable=False)
    source_filename=Column(String(120),nullable=False)
    description=Column(Text,default='')
    # 已核对完整并一次性切换生效的运行；无外键约束以避免与 runs 的循环依赖，运行不做物理删除
    current_run_id=Column(Integer,nullable=True)
Study=TeaLot
