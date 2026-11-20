# TESTING

记录逐指标质量状态、版本化冻结与一次性切换的实际验证结果。

## 环境与执行

- 本机无 Docker/Postgres/Redis，使用独立 venv（`/tmp/venv`，Python 3.11）安装 `backend/requirements.txt` + `pytest`/`httpx`。
- 数据库：SQLite 临时文件库；Redis：进程内 FakeRedis（仅 rpush/incr/expire/ping）。
- 行锁 `with_for_update` 在 SQLite 为 no-op，撞写入由 `(study_id,checksum)`、`(study_id,version)` 唯一约束 + `IntegrityError` 幂等重查兜底；Postgres 上额外有批次行锁串行化，语义等价。
- 执行：

```bash
cd backend
python -m pytest tests/ -v
```

## 结果（2026-11-20，13 passed）

| # | 场景 | 测试 | 断言要点 | 结果 |
|---|---|---|---|---|
| 1 | 交错补录 | `test_partial_row_isolates_missing_per_field` | 只填一个指标时另一指标判 `missing`；冻结后该指标分母只含纳入行（6 行中 extract 分母 4），缺测未静默拖入 | PASSED |
| 2 | 交错补录 | `test_backfill_creates_new_version_with_silent_hole_closed` | 补齐缺测后冻结产生 v2，分母 4→5；v1 快照与分母原样保留 | PASSED |
| 3 | 异常隔离 | `test_anomalous_value_isolated_with_reason` | 显式异常带原因隔离；越界值（150%）自动判异常；`isolated_rows` 可查依据 | PASSED |
| 4 | 重复修订/重试 | `test_each_distinct_freeze_is_a_new_version_retry_reuses` | 同内容重试（含完成后）沿用同一运行/版本不新增研究；三次修订产生 v2/v3/v4，checksum 互异 | PASSED |
| 5 | 冻结撞写入 | `test_concurrent_identical_freeze_collapses_to_one_run` | 6 线程并发相同冻结，最终仅 1 条运行、1 个版本、1 个 checksum | PASSED |
| 6 | 冻结撞写入 | `test_unique_constraints_reject_duplicate_checksum_and_version` | 绕过服务直插重复 checksum/version 均触发 IntegrityError | PASSED |
| 7 | 失败恢复 | `test_failed_run_kept_retry_same_version_fix_forward_switches` | 分母 0 任务 failed 且保留 error/无 result；同版本重试 id/version 不变；修复后新版本成功并切换；失败运行保留 | PASSED |
| 8 | 失败恢复 | `test_switch_failure_keeps_old_result` | 复核事务提交注入崩溃：旧指针、旧复核结果保留，新版本无 Review、未生效 | PASSED |
| 9 | 开放门禁 | `test_results_and_download_only_open_to_released_version` | 未开放版本 analyst 可看不可下载(409)，viewer 直链 403、列表过滤；仅 released 版本可下载且 CSV 含各指标分母与隔离依据 | PASSED |
| 10 | 审计留痕 | `test_freeze_records_versions_and_isolation_reasons` | 观测保存审计含逐指标状态（如 `extract:missing`），排队审计含版本号，隔离原因入质量报告 | PASSED |
| 11 | worker 恢复 | `test_worker_recover_requeues_interrupted_jobs` | running 中断任务重启后重新入队并处理成功 | PASSED |
| 12 | 质量报告单元 | `test_quality_report_unit_shape` | 逐指标分母、缺测/异常清单与原因、行级隔离视角结构正确 | PASSED |
| 13 | HTTP 全链路 | `test_full_http_flow`（FastAPI TestClient） | 登录→逐指标录入→冻结 v1→重试不新增→角色 403/409→admin 复核切换→补录 v2 再切换→viewer 仅见当前版本→审计含 `run.switch` | PASSED |

## 其他验证

- `scripts/seed_demo.py` 在 SQLite 上执行：3 批次 360 条观测全部回填 `quality`，无遗漏；新批次 `current_run_id` 为空。
- 前端全部 ES module 通过 `node --check`；`utils/quality.js` 的表单值归一、质量徽章、状态选项在 node 下单独执行验证。
- 未做浏览器端到端与真实 Postgres/Redis 容器验证（环境无 Docker）；Nginx 仅代理 `/api/`，前端契约与 API 字段一致。
