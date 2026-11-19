---
name: falsification-audit-ledger
description: 科研数据分析结果的证伪审计与证据台账。当任务是在 /input 给定 case.json（含 output_contract、数据字典）、task.md 与科研数据（CSV/SQLite），要求完成清洗、统计、可视化、查询并在 /app/output 交付可复核的 result.json 时使用。核心方法：作者-审计双通道——先用任何顺手的工具完成分析（作者通道），但每报一个数字必须同步在 claims.jsonl 登记一条机器可重算的声明；交付前必须运行随附的独立审计器 audit/ledger.py（证伪通道），它从原始输入重新哈希、独立重算每个数字、核对图表元数据与契约完整性，任何 MISMATCH/FAIL 都禁止 status=success。重点解决"跑通了但算错了"的静默错误与无法复核的结果。不适用：无可核验数值结论的纯定性任务。
---

# falsification-audit-ledger：作者-审计双通道

## 核心主张

这类任务最危险的失败是**看起来对了**：缺失值被当 0、重复行没去重、查询漏了条件、图的轴与数据字典对不上。自检（"我觉得对"）对这些无效，因为写代码的人和检查代码的人犯的是同一个错误。本 Skill 强制双通道分离：**作者通道**可以用 pandas/SQL/任何工具自由地算；**证伪通道**（`audit/ledger.py`，纯标准库、独立实现）在交付前试图推翻每一个结论。结果不是"做完了"，而是"经得起独立重算"。

## 协议（作者通道 → 证伪通道 → 修复循环）

### A. 作者通道：自由分析，但强制留痕

1. 建档：`ls -laR /input`；读 case.json 摘出 `case_id` 与 `output_contract` 全部字段；读 task.md 与数据字典。**立即**写 `/app/output/result.json` 骨架（case_id 真值、契约字段占位 null、status=in_progress、provenance 含每个输入文件的 sha256、Python 版本、seed 或"无随机步骤"声明）。
2. 用任何方式完成清洗、统计、图、查询。工作文件放 `/app/work`，交付放 `/app/output`。
3. **每报一个数字，登记一条 claim**：向 `/app/work/claims.jsonl` 追加一行 JSON——
   ```json
   {"id": "m3", "kind": "mean", "source": "/app/work/clean.csv", "column": "strength", "value": 42.84, "tolerance": 0.01}
   ```
   支持的 kind：`row_count / n_missing / count / mean / median / std / min / max / count_where(带 op 与 threshold) / sql_scalar(带 db 与 query)`。result.json 里出现的每个数值结论都必须有对应 claim；没有 claim 的数字视为不可交付。
4. **每张图配一份元数据**：`/app/output/figX.meta.json`，内容为 `{"png": 绝对路径, "data_file": 绘图用的数据文件, "columns": [实际使用的列], "title": "...", "xlabel": "...", "ylabel": "..."}`。标题与轴标签逐字来自数据字典（含单位）。

### B. 证伪通道：交付前的强制审计

```bash
python3 /skills/weekly-skill/audit/ledger.py --input /input --work /app/work --output /app/output
```

审计器独立于你的分析代码，做五件事：重新哈希全部输入并与 provenance 对比（防止用了过期/改过的数据）；从 case.json 重新读取契约逐字段核对（不信任你的转述）；用独立实现从**原始输入**重算 claims.jsonl 的每条声明；检查每张图是真实 PNG、非空、绘图列真实存在于数据文件、标签非空；核对 status 与检查结果的诚实一致性。产出 `/app/output/audit_ledger.json`，有任何 FAIL/MISMATCH/UNVERIFIABLE 即以退出码 1 拒绝放行。

### C. 修复循环（最多两轮，之后如实降级）

审计失败时：逐个 FAIL 项定位——是分析算错了（回作者通道修正并更新 claim）、还是 claim 写错了（修正 claim 的 source/column/值）、还是确实无法核验（把该结论从 result.json 移除或标注 estimated 并在 anomalies 说明）。**禁止**通过删 claim、调大 tolerance、跳过审计来"通过"。两轮后仍有未决项：status=partial，把未决项逐条写进 anomalies，这属于合法交付；status=success 与审计失败共存属于违规交付。

### D. 静默错误十查（作者通道完成后、审计之前自查）

逐条过 `audit/silent_error_checklist.md`（缺失值口径、重复行、行数账、检验前提、图轴一致性、查询条件、单位有效数字、环境种子记录、空结果集处理、异常不静默）。这份清单针对的是审计器查不到的语义层问题（如"该检验的前提根本不成立但 p 值照报"），必须用文字在 anomalies 或 results 中给出处置说明。

## 异常处理原则

- 审计器自身报错（如列名不存在）：先怀疑 claim 的 source/column 写错，而不是审计器错了。
- SQL 空结果：核对表名/连接键/WHERE 后才允许报告"空结果已核实"。
- 任何步骤失败：保留失败的命令、报错原文到 anomalies；禁止换一个"看起来合理"的数继续。
- 时间不足：以 status=partial 交付已审计部分 + 未完成清单，优于交付未经审计的"完整"结果。
