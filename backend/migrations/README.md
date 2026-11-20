## Schema 版本

- **v1**：初始表结构（studies / observations / runs / reviews / audits / users），启动时由 `Base.metadata.create_all` 创建。
- **v2（当前）**：逐指标质量状态与版本化冻结。
  - `observations` 新增 `quality` JSON：`{字段: {state: included|missing|anomalous, reason}}`；`values` 中被隔离指标保存为数值或 `null`。
  - `runs` 新增 `version`（同批次递增）、`quality_report` JSON（冻结时刻逐指标纳入分母、缺测/异常隔离清单与原因），并增加唯一约束 `(study_id, checksum)` 与 `(study_id, version)`。
  - `studies` 新增 `current_run_id`：核对完整并一次性切换后生效的运行指针；无 FK 约束以避免与 runs 的循环依赖，运行不做物理删除。

本地小型应用不执行破坏性迁移：`create_all` 只会补建新表/新列中缺失的表，**已存在表的新增列与唯一约束需在升级时手动执行**（PostgreSQL 示例）：

```sql
ALTER TABLE observations ADD COLUMN IF NOT EXISTS quality JSON;
UPDATE observations SET quality = '{}' WHERE quality IS NULL;
ALTER TABLE runs ADD COLUMN IF NOT EXISTS version INTEGER;
ALTER TABLE runs ADD COLUMN IF NOT EXISTS quality_report JSON;
UPDATE runs SET version = id, quality_report = '{"fields":{},"row_count":0,"isolated_rows":[]}' WHERE version IS NULL;
ALTER TABLE runs ALTER COLUMN version SET NOT NULL;
ALTER TABLE runs ALTER COLUMN quality_report SET NOT NULL;
ALTER TABLE studies ADD COLUMN IF NOT EXISTS current_run_id INTEGER;
CREATE UNIQUE INDEX IF NOT EXISTS uq_run_study_checksum ON runs(study_id, checksum);
CREATE UNIQUE INDEX IF NOT EXISTS uq_run_study_version ON runs(study_id, version);
```

升级前备份 `db-data` 卷；`scripts/seed_demo.py` 会为历史观测回填全量 `included` 质量状态。
