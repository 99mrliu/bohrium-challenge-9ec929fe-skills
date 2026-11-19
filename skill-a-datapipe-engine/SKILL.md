---
name: datapipe-engine
description: 科研数据分析案例的确定性执行引擎。当任务是在 /input 给定 case.json（含 output_contract、数据字典）、task.md 与科研数据（CSV 快照 / SQLite 数据库），要求完成清洗、统计、可视化、数据库查询并在 /app/output 产出可复核的 result.json 时使用。核心方法：Agent 不临时编写核心计算代码，而是调用随附的 casekit.py（纯标准库、零依赖、确定性）完成 profile→clean→describe/aggregate/stats/sql/figure→verify→finalize 全链路；Agent 只负责读题、写 rules.json 与 claims.json、把真实数值填进 result.json。引擎对缺列、缺失值、空查询、契约缺项全部显式报错或降级，杜绝"跑通了但算错了"。不适用：需要训练机器学习模型、需要文献综述或机理解释的任务。
---

# datapipe-engine：确定性数据分析执行引擎

## 为什么用引擎而不是临时写代码

本场景中 Agent 的典型失败不是"写不出代码"，而是依赖缺失跑不起来、字段名对不上、缺失值被当 0、去重没做就分组、查询漏条件、图表轴与数据字典不一致、隔天重跑数字变了。本 Skill 把这些环节全部固化进一个**纯 Python 标准库**的确定性工具 `engine/casekit.py`：不依赖 pandas/numpy/matplotlib 也能跑完全部统计（matplotlib 仅用于画图，缺失时显式降级）；相同输入必然得到相同输出；每条规则处理了多少行都写入报告，拒绝任何静默行为。

你的角色：**读题 + 配规则 + 调用命令 + 填结果**。不要重写引擎已有的计算。

## 固定路径

- 引擎：`/skills/weekly-skill/engine/casekit.py`（下文记为 `$CK`，用 `python3` 运行）
- 输入：`/input/case.json`、`/input/task.md`、数据文件
- 工作区：`/app/work`；交付：`/app/output`（result.json、图、查询 CSV）

## 执行协议（按顺序，不要跳步）

### 第 0 步：建档（最先做，保证任何时候都有可交付的半成品）

```bash
python3 $CK --work /app/work --output /app/output profile --input /input
```

产出：`/app/work/profile.json` + `profile.md`（每个文件的 sha256、编码、行列、每列类型/缺失数/数值范围/重复行数；SQLite 的表结构与 NULL 计数），并**立即**在 `/app/output/result.json` 写入含 `case_id`、全部 output_contract 字段（占位 null）、provenance 的骨架。之后所有工作都是填充这个骨架。

然后读 `profile.md`、`/input/task.md` 和 `/app/work/data_dictionary.json`（若存在），把任务拆成：清洗规则、统计项、图、SQL 查询四类。

### 第 1 步：清洗（规则显式化，先写 rules.json）

把清洗决定写成 `/app/work/rules.json`（这是"口径"，必须能在数据字典里找到依据）：

```json
{
  "rename": {"原始列名": "规范名"},
  "select_columns": ["要保留的列"],
  "coerce": {"col": "float|int|str"},
  "missing": {"policy": "drop_rows | impute | report_only",
              "columns": ["col1"], "impute": {"col": "mean|median|mode|字面值"}},
  "dedup": {"policy": "keep | drop_exact | drop_by", "keys": ["id_col"]},
  "filter": [{"column": "col", "op": "==|!=|>|>=|<|<=|na|notna", "value": "x"}]
}
```

```bash
python3 $CK --work /app/work --output /app/output clean \
  --table /input/数据文件.csv --rules /app/work/rules.json \
  --out /app/work/clean.csv --report /app/work/clean_report.json
```

引擎对每条规则报告 `rows_dropped / n_imputed / unparseable_set_to_missing` 和被删行号；引用了不存在的列会**直接拒绝执行**（这就是防静默错误的设计，不要绕过它去手写清洗代码）。缺失值默认**不参与**任何统计；只有任务明确要求时才 impute，并在 anomalies 里说明。拿不准该不该去重时：保留并在 anomalies 中记录候选重复数与决定理由。

### 第 2 步：统计与查询

```bash
python3 $CK ... describe  --table /app/work/clean.csv --columns col1,col2
python3 $CK ... aggregate --table /app/work/clean.csv --group-by g --metrics "y:mean,y:std,y:count"
python3 $CK ... stats ttest --table T.csv --column y --group g        # Welch，不要求等方差
python3 $CK ... stats corr  --table T.csv --x a --y b                 # Pearson r + p
python3 $CK ... stats ols   --table T.csv --y strength --x cement,age # 1–2 个自变量
python3 $CK ... sql --db /input/xxx.db --query "SELECT ..." --out /app/work/q1.csv
```

注意：stats 的 p 值由纯 Python 不完全贝塔函数计算；正态性等前提引擎**没有**替你验证，报告统计检验时必须在结果中说明使用了哪个检验、前提是否检查过。SQL 只允许只读查询；返回 0 行时引擎会给出 EMPTY RESULT 警告——先核对表名/连接键/WHERE 条件再下结论，不许把空结果当正常答案。

### 第 3 步：图（标题、轴标签、单位必须来自数据字典）

```bash
python3 $CK ... figure hist --table /app/work/clean.csv --x strength \
  --title "抗压强度分布" --xlabel "strength (MPa)" --out /app/output/fig1.png
# 也支持 scatter（--x --y）、bar、line；--bins 调直方图分箱
```

每张图生成 `.receipt.json`（画了哪个文件的哪一列、用了多少点、排除了多少缺失点、轴标签原文）。matplotlib 不可用时引擎输出 `SKIPPED_no_matplotlib` 并保存绘图数据 CSV——此时在 result.json 中如实报告图未渲染，**不得**交付不存在或内容不符的图。

### 第 4 步：验证（交付前的强制对账）

把 result.json 里每一个数值型结论登记成一条 claim，写入 `/app/work/claims.json`：

```json
[{"id": "c1", "kind": "row_count", "source": "/input/raw.csv", "value": 1030},
 {"id": "c2", "kind": "n_missing", "source": "/input/raw.csv", "column": "slump", "value": 3},
 {"id": "c3", "kind": "mean", "source": "/app/work/clean.csv", "column": "strength", "value": 35.82, "tolerance": 0.01},
 {"id": "c4", "kind": "sql_scalar", "db": "/input/lab.db", "query": "SELECT COUNT(*) FROM t", "value": 178}]
```

`kind` 支持 `row_count / n_missing / count_where(op,value) / mean / median / std / min / max / count / sql_scalar`。

```bash
python3 $CK ... verify --claims /app/work/claims.json
```

引擎从**原始输入**（不是你的工作表）独立重算每条 claim；任何 MISMATCH/UNVERIFIABLE 都会以退出码 1 失败。此时必须回到对应步骤修正数字或修正 claim，禁止调大 tolerance 蒙混。

### 第 5 步：填充 result.json 并过契约门

把真实数值填入 `/app/output/result.json`：每个 output_contract 字段都要有值，`figures` 用**绝对路径**，`anomalies` 如实记录缺失/异常/未决项，`provenance` 已由骨架提供（输入哈希、Python 版本、run_log 路径）。然后：

```bash
python3 $CK ... finalize --input /input
```

契约门检查：case_id 与 case.json 一致、每个 output_contract 字段存在且非 null、verify 全部 CONFIRMED、声明的图文件真实存在。任何一项不满足即把 status 降级为 partial 并列出问题清单；全部通过才允许 status=success。**不要用编辑 result.json 的方式绕过契约门。**

## 异常处理原则

- 任何命令非零退出：先读 stderr 的具体原因（缺列/空表/查询失败/只读限制），修正输入或规则后重跑该步；禁止换一段自己写的代码绕开。
- 数据与数据字典冲突（如列单位不明、重复记录是否该合并无法判断）：取保守口径，在 anomalies 中记录两种解释和你选择的一种。
- 时间紧张时：保证 profile 骨架 + 已完成步骤 + 未完成项说明以 status=partial 交付，优于仓促交付未验证的"完整"结果。
- 每次调用的命令、退出码、耗时都在 `/app/work/run_log.jsonl`，复核者可据此逐步重放。

## 最小调用示例（示意，非正式案例结果）

输入：某 CSV 含列 `strength` 与 3 个缺失标记 `NA`。流程：`profile`（发现 41 行、1 个缺失、1 个重复行）→ rules.json 指定 drop_rows + drop_exact → `clean`（41→38 行）→ `describe --columns strength`（mean=42.84）→ claims.json 登记 row_count=41、n_missing=1、mean=42.84 → `verify` 全部 CONFIRMED → `finalize` 通过，status=success。每一步的行数变化都留在 clean_report.json，每个数字都被独立重算确认。
