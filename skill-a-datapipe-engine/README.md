# datapipe-engine — 使用与方法说明

## 这是什么

一个可被 Agent 调用的 Skill，用于"科研代码生成与执行"场景下的固定数据分析案例（CSV 快照 + SQLite 数据库 + case.json/task.md 输入契约）。核心资源是 `engine/casekit.py`：一个**纯 Python 标准库**（>=3.8，零第三方依赖）的确定性命令行工具，把数据建档、清洗、统计、检验、查询、绘图、独立复算、契约校验做成九个子命令。Agent 只写两份 JSON（清洗规则 rules.json、待核验结论 claims.json）并依次调用子命令，不临时编写核心计算代码。

## 设计取舍

| 决策 | 理由 |
|-|-|
| 零依赖 | "跑不起来"（依赖缺失、版本不匹配）是此类任务的最高频失败；stdlib 在任何评测机上必然可运行。matplotlib 仅在可用时用于出图，缺失时显式降级为绘图数据 CSV + 收据。 |
| 确定性 | 无随机、无采样、无网络；相同输入字节级相同输出，隔天重跑数字不变。 |
| 规则文件而非代码 | 清洗口径（缺列处理、缺失值策略、去重策略）集中在一个可审阅的 JSON 中，每条规则处理了多少行都进 clean_report.json，杜绝"缺失值被当 0"这类静默错误。 |
| 双通道验证 | verify 子命令从**原始输入**独立重算 result.json 里的每个数字，与报告值带容差对账；finalize 子命令强制 output_contract 字段齐全、case_id 一致、图文件存在，否则拒绝 status=success。 |
| 提前落盘 | profile 第一步就写出含全部契约字段的 result.json 骨架，任何时刻中断都有诚实半成品。 |

## 完整调用步骤

```bash
CK=/skills/weekly-skill/engine/casekit.py
# 0 建档（最先做）
python3 $CK --work /app/work --output /app/output profile --input /input
# 1 清洗（先写 /app/work/rules.json，见 SKILL.md 第 1 步的 schema）
python3 $CK --work /app/work --output /app/output clean --table /input/data.csv \
  --rules /app/work/rules.json --out /app/work/clean.csv --report /app/work/clean_report.json
# 2 统计 / 检验 / 查询
python3 $CK --work /app/work --output /app/output describe --table /app/work/clean.csv --columns y
python3 $CK --work /app/work --output /app/output aggregate --table /app/work/clean.csv \
  --group-by g --metrics "y:mean,y:std,y:count"
python3 $CK --work /app/work --output /app/output stats ttest --table T.csv --column y --group g
python3 $CK --work /app/work --output /app/output stats corr --table T.csv --x a --y b
python3 $CK --work /app/work --output /app/output stats ols --table T.csv --y y --x x1,x2
python3 $CK --work /app/work --output /app/output sql --db /input/lab.db --query "SELECT ..." \
  --out /app/work/q1.csv
# 3 图（hist/scatter/bar/line）
python3 $CK --work /app/work --output /app/output figure hist --table /app/work/clean.csv \
  --x y --title "..." --xlabel "y (unit)" --out /app/output/fig1.png
# 4 验证（先写 /app/work/claims.json）
python3 $CK --work /app/work --output /app/output verify --claims /app/work/claims.json
# 5 填充 result.json 后过契约门
python3 $CK --work /app/work --output /app/output finalize --input /input
```

## 输入 / 输出

- 输入：`/input/case.json`（含 `case_id`、`output_contract`、可选 `data_dictionary`）、`/input/task.md`、任意数量的 CSV/TSV/SQLite/JSON/文本材料。
- 工作产物（`/app/work`）：profile.json/md、rules.json、clean_report.json、各统计 JSON、claims.json、verification.json、run_log.jsonl（每次调用的命令、退出码、耗时）。
- 交付（`/app/output`）：result.json（含全部 output_contract 字段 + case_id + status + anomalies + provenance + verification）、图 PNG 及其 .receipt.json、查询结果 CSV。

## 关键方法说明

- 统计检验为纯 Python 实现：Welch t 检验（不假设等方差）、Pearson 相关、1–2 自变量 OLS（正规方程 + 部分主元消元）；p 值经正则化不完全贝塔函数（连分式，Numerical Recipes 算法）计算，与 scipy 结果在 1e-10 量级一致。正态性前提不由工具验证，调用方须在结果中如实声明。
- 百分位数采用线性插值（与 numpy 默认一致）；样本标准差用 n-1。
- SQLite 仅允许只读查询（SELECT/WITH/PRAGMA/EXPLAIN），以只读 URI 打开；空结果集给出强制警告。

## 适用场景与限制

适用于：结构化科研数据的清洗、描述统计、分组聚合、双组比较、相关/简单回归、直方图/散点/条形/折线图、SQLite 查询、结果复核。不适用于：机器学习建模、非结构化文本、大规模数据（单机内存内处理，百万行级以内）、需要多自变量回归/非参数检验的统计设计（引擎会明确拒绝而不是给出错误答案）。

## 失败处理与自查

- 任一子命令非零退出：stderr 给出具体原因与可用列名/表名；修正 rules.json 或查询后重跑该步即可，全部调用可在 run_log.jsonl 中重放。
- 自查清单：clean_report.json 的行数账是否平（rows_in = rows_out + 各规则删除数）？claims.json 是否覆盖 result.json 的每个数字？verify 是否全 CONFIRMED？finalize 门是否通过？
