# falsification-audit-ledger — 使用与方法说明

## 这是什么

一个面向"科研代码生成与执行"数据分析案例的 Agent Skill，采用**作者-审计双通道**结构。作者通道自由完成分析；证伪通道由随附的 `audit/ledger.py`（纯 Python 标准库、零依赖）在交付前对结果做对抗性审计：重新哈希输入、独立重算每个数值结论、核对图的真实性与元数据、重新读取 output_contract 逐字段验收、核对 status 诚实性。审计不通过，结果不得标记为 success。

## 设计取舍

| 决策 | 理由 |
|---|---|
| 审计与分析分离 | 自检防不住静默错误——同一个人/同一段代码犯的错查不出来。ledger.py 是独立实现，刻意不复用作者的任何函数。 |
| claim 登记制 | 可交付的数字 = claims.jsonl 里登记过、且能被独立重算的数字。登记成本逼作者想清楚每个数字的来源、口径与容差。 |
| 审计器零依赖、只读 | 与作者环境完全解耦；只读打开 SQLite、只读重算 CSV，绝不修改作者的任何文件，只写 audit_ledger.json。 |
| 失败即降级 | 两轮修复仍有未决项则 status=partial + anomalies 逐条说明；诚实半成品优于带静默错误的"完整"结果。 |

## 文件

| 文件 | 作用 |
|---|---|
| `SKILL.md` | 双通道协议：作者留痕要求、审计运行方式、修复循环、红线 |
| `audit/ledger.py` | 独立审计器（子命令见下） |
| `audit/silent_error_checklist.md` | 语义层静默错误十查（审计器覆盖不到的部分） |

## 完整调用流程

```bash
# 作者通道（方式不限）：分析、出图、填 /app/output/result.json
# 同时强制留痕：
#   - 每报一个数字：/app/work/claims.jsonl 追加一条
#     {"id":"m1","kind":"mean","source":"/app/work/clean.csv","column":"strength","value":42.84,"tolerance":0.01}
#   - 每张图：/app/output/figX.meta.json（png/data_file/columns/title/xlabel/ylabel）
# 证伪通道（交付前）：
python3 /skills/weekly-skill/audit/ledger.py --input /input --work /app/work --output /app/output
# 退出码 0 = 全部 CONFIRMED；1 = 有 FAIL/MISMATCH/UNVERIFIABLE，详情见 /app/output/audit_ledger.json
```

claim 类型：`row_count`、`n_missing`、`count`、`mean`、`median`（线性插值）、`std`（样本 n-1）、`min`、`max`、`count_where`（`op` ∈ ==,!=,>,>=,<,<= + `threshold`）、`sql_scalar`（`db` + `query`，重执行核对）。容差 `tolerance` 缺省：整数精确、浮点相对 1e-6。

## 审计器检查项一览

1. 输入完整性：/input 全部文件重新哈希，与 result.json provenance.inputs 逐一比对（用了被改动/过期的数据即 FAIL）。
2. 契约验收：从 case.json 重新读取 case_id 与 output_contract 字段清单，核对 result.json 逐字段存在且非 null。
3. 数值独立重算：claims.jsonl 每条声明从原始输入重算（mean/std/median/min/max/count/row_count/n_missing/count_where/sql_scalar）。
4. 图真实性：PNG/JPEG magic bytes、最小字节数（防空图）、.meta.json 中绘图列真实存在于数据文件、标签非空。
5. 诚实性：provenance 含 Python 版本与 seed 声明；status=success 与失败检查项不得共存。

## 适用与限制

适用于有明确 output_contract、数值结论密集的 CSV/SQLite 分析案例。审计器只核验"登记过的声明"——作者漏报的错误结论查不出来，因此协议要求 claims.jsonl 覆盖 result.json 的全部数值结论，并用静默错误十查兜底语义层问题。不做机器学习模型评估，不做文本类结论核验。

## 最小调用示例（示意，非正式案例结果）

作者报告 `mean_strength=42.84`，登记 claim `{"id":"m3","kind":"mean","source":"clean.csv","column":"strength","value":42.84}`；审计器从 clean.csv 独立重算得 42.8376…，相对差 < 1e-6 → CONFIRMED。若作者误报 42.0，审计器输出 MISMATCH 并以退出码 1 阻止 status=success，作者在修复循环中改正后复审通过。
