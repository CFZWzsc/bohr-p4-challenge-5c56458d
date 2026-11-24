---
name: sqlite-query-auditor
description: Use when a research data-analysis case provides a SQLite database and asks for schema exploration, safe read-only SQL execution, field dictionaries, join fan-out checks, or query results with provenance; run scripts/run_skill.py for a deterministic result.json.
---

# 数据库查询工具与智能检索

## 用途与适用条件

在本地科研数据库查询任务中使用：先读结构再查数据，只执行只读语句，返回结果时附 SQL、参数、行数与来源表，识别 JOIN 放大与全表扫描风险。输入为 `/input/case.json`、可选 `/input/task.md` 与 `.sqlite`/`.db` 文件。

不适用：远程数据库、写入/迁移、权限管理、自然语言直接生成复杂 SQL。脚本不连接网络，不修改数据库文件。

## Quickstart

包内自带一个合成演示案例，可直接跑通：

```powershell
python scripts/run_skill.py --case examples/demo-case/case.json --out "$env:TEMP\demo_5c56458d\result.json" --work "$env:TEMP\demo_5c56458d\work"
```

- 演示输入：`examples/demo-case/case.json` + `examples/demo-case/materials/study.sqlite`（`sample` 4 行、`measurement` 5 行，含外键与索引）。
- 演示 SQL 共 3 条：分组计数与联表求均值各返回真实结果；第 3 条 `DELETE FROM measurement` 被只读守卫拒绝，因此整体 `status=partial` 属预期。
- 输出：`$env:TEMP\demo_5c56458d\result.json`，`schema`、`dictionary`、`query_results` 均有内容。
- `output_contract` 的三个键均非空；同一输入连续两次运行的 result.json 字节一致。

> 数据库为合成样本，不代表任何真实研究或官方评测数据。

## 输入

- `case.json`：`case_id`、`output_contract`；可选 `queries`（字符串或 `{sql, params, purpose}` 列表）、`database`、`question`、`max_rows`。
- 数据库：`input_dir`、`materials`、`database`、`path` 指定；回退 `/input` 扫描 `.sqlite`、`.sqlite3`、`.db`。
- 输出：`/app/output/result.json`。

## 处理流程（对应评分维度）

1. **连接（任务完成度）**：用 SQLite URI `mode=ro` 只读打开，校验 `PRAGMA query_only`；数据库不存在或非 SQLite 时记录错误，不尝试创建。
2. **结构盘点（正确性）**：列出表、列、声明类型、主键、外键、索引与行数；空库也如实输出。
3. **字段字典（完整性）**：为每列给出类型、可空性、去重值数、空值数与单位提示；从列名提取 `(...)` 单位，不做语义臆测。
4. **查询执行（正确性）**：只允许 `SELECT`、`WITH`、`EXPLAIN`；拒绝 `INSERT/UPDATE/DELETE/DROP/ALTER/ATTACH/PRAGMA` 与多语句；每条结果限制行数并附 `sql`、`params`、`row_count`、`truncated`。
5. **风险检查（可核验性）**：用 `EXPLAIN QUERY PLAN` 记录 `SCAN`/`SEARCH`；结果行数超过最大基表行数时标 `possible_join_fanout`；无索引扫描标 `full_scan`。
6. **检索辅助（使用增益）**：把问题词与列名做词法匹配，给出候选列与参数化查询模板，明确标注为启发式建议，不冒充已执行结果。
7. **失败与恢复（可靠体验）**：SQL 语法错误、未知列、只读拒绝、行数截断分别进入 `errors`、`checks`，不返回伪造行。

## 输出契约

| 字段 | 含义 |
|-|-|
| `case_id` | 与输入一致 |
| `output_contract` 各字段 | 命中别名时填真实值，否则 `null` + `contract_notes` |
| `status` | `success` / `partial` / `insufficient_input` / `failed` |
| `result` | `schema`、`dictionary`、`query_results`、`checks`、`suggested_queries` |
| `evidence` | 数据库路径、大小、哈希、只读方式、执行上限 |
| `uncertainty` | 查询为词法建议、未做执行计划成本估算 |
| `errors` / `stats` | 真实错误与计数 |

## 异常处理表

| 情况 | 处置 | 状态 |
|-|-|-|
| case.json 缺失/非法 | 空结果 + 错误 | `failed` |
| 无数据库文件 | 明确说明并给出支持的扩展名 | `insufficient_input` |
| 文件不是 SQLite | 捕获 `DatabaseError`，不创建新库 | `failed` |
| 无查询语句 | 仅返回结构与字典 | `partial` |
| 含写操作/多语句 | 拒绝执行并说明原因 | `partial` |
| SQL 语法或未知列错误 | 记录错误文本与 SQL | `partial` |
| 结果被行数截断 | `truncated=true` 并提示加 LIMIT | `partial` |
| 疑似 JOIN 放大 | 标注 `possible_join_fanout` | `partial` |

## 限制

- 只读，不建表、不写索引、不做迁移。
- 行数统计为全表 `COUNT(*)`；超大表可能较慢，受执行行数上限保护。
- 不做 SQL 等价改写、不估算代价、不做自然语言到复杂 SQL 的自动生成。
- 不返回 BLOB 原始字节，仅给长度。

## 最小调用示例（示例，非官方案例结果）

```bash
python /skills/weekly-skill/scripts/run_skill.py --case /input/case.json --out /app/output/result.json --work /app/work
```

示意输入 `{"case_id":"synthetic-demo","output_contract":{"schema":"object","query_results":"array"},"queries":["SELECT COUNT(*) AS n FROM t"]}`；示意输出保留 `case_id`、`schema`、`query_results`、`status`，无数据库时为 `insufficient_input`。仅为接口示例。


## 交付保证与评分对齐

调用顺序（固定）：

1. 先运行包内脚本（若存在）：`python scripts/run_skill.py`（默认读 `/input/case.json`，写 `/app/output/result.json`）；路径不同时用 `--case` / `--output` 指定。
2. 再完成脚本无法自动完成的实质工作（译文、论述、图表、修订、结论等），把结果写回 `result.json` 对应字段。
3. 最后自检：`case_id` 与输入一致；`output_contract` 每个字段都存在；`status` 按契约取 `success`/`partial`/`insufficient_input`/`failed`（缺输入或证据不足时用 `insufficient_input`/`partial`）；无 `pending` 占位；数字、引用、来源可逐条回溯。

运行保证：

- 输入缺失、JSON 损坏、字段异常、输出目录不存在时，仍须写出结构化 `result.json` 并正常结束，不得崩溃或非零退出。
- 不联网、不伪造数据、不声称平台评分或官方案例结果。
- 交付物必须是可直接编辑与复核的成品，而不是流程说明。

评分对齐（本题五个维度）：

- 实际效果（40）：完整产出任务要求的最终成品，覆盖全部输入条目，可直接进入修改流程。
- 使用增益（25）：体现相对基础 Agent 的可验证改进（结构化、术语/数字/引用核对、可编辑性）。
- 创新增益（15）：保留方法学增量（分块、对齐、二次校验、版本比对等），并用结果证明其价值。
- 可靠体验（10）：异常输入有明确降级与说明，不同长度/文体/重复运行下稳定产出。
- 生态贡献（10）：用途、调用、输入输出、限制、示例、维护信息齐全，资源可被他人复用。

## 最终交付强制要求

1. **调用 Agent 必须交付成品，不是流程说明**：脚本产出只是索引、核对或规格骨架；调用 Agent 必须在同一 `result.json` 中补齐本题要求的最终产物（正文/译文/审稿意见/综述/分析结论/图表源文件与导出件/幻灯片页内容与讲稿/实验方案等）。交付字段为空、只有标题骨架，或含 `pending`、`TODO` 之类占位，一律视为未完成。
2. **必须覆盖全部输入条目**：输入中的每个章节、问题、文献、图、页面、指标都要有对应条目，不得只处理示例或首条；确实缺失的条目写入 `uncertainty` / `limitations` 并说明原因，不得静默跳过。
3. **必须自检后再交付**：`case_id` 与输入一致；`output_contract` 每个字段都存在；状态值取本题契约规定的枚举；数字、单位、引用、来源可逐条回溯；结果内部无自相矛盾。
4. **参数等价**：`python scripts/run_skill.py --case <输入> --out <输出>` 与 `--output <输出>` 等价；默认 `/input/case.json` → `/app/output/result.json`。
5. **平台用 3 个固定案例分别运行**：逻辑要由输入驱动，不针对某一份示例硬编码；字段命名差异走别名匹配与显式降级，并在 `uncertainty` 记录假设。
