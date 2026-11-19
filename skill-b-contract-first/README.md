# contract-first-analysis — 使用与方法说明

## 这是什么

一个纯方法论的 Agent Skill（不含任何执行引擎），用于"科研代码生成与执行"类数据分析案例。它把 Agent 的工作顺序强制为 **契约 → 口径 → 代码 → 复核 → 收口** 五个阶段，每阶段留下可检查的物证，专门防两类失败：(1) 跑不起来（依赖、路径、字段名）；(2) 跑通了但算错了（缺失当 0、未去重、检验前提没查、图表与数据字典不符、查询漏条件）。

## 与"引擎式"方案的本质区别

本 Skill 不随附任何计算代码：所有统计、清洗、绘图代码由 Agent 按 `templates/script_skeleton.py.txt` 现场生成，但生成行为被协议约束——先填 `templates/rules_template.md` 口径表（列名逐字复制、策略有依据），代码头部必须有断言门（缺列/行数不符即退出），尾部必须有复核块（第二种实现独立重算每个数字，见 `templates/verification_block.py.txt`）。它改进的是**代码生成的约束条件**，而不是替 Agent 算。

## 文件

| 文件 | 作用 |
|---|---|
| `SKILL.md` | 五阶段协议与红线 |
| `templates/rules_template.md` | 口径表：字段映射/清洗规则/统计口径/查询口径/图口径/二义性登记 |
| `templates/script_skeleton.py.txt` | 单脚本骨架：断言门→清洗段→计算段→复核块 |
| `templates/verification_block.py.txt` | 独立复算块模板（stdlib 重算 mean/std/行数/SQL 标量） |
| `templates/failure_playbook.md` | 10 类典型失败的处置表（含"两次失败换策略类别"规则） |
| `checklists/final_checklist.md` | 交付前契约清单：契约完整性/数字可复核/图与查询/可复跑/诚实性 |

## 一次完整使用流程（最小调用示例，示意见括号）

1. `ls -laR /input`，登记文件哈希；读 case.json 摘契约字段；写 result.json 骨架（status=in_progress）。
2. 填 `/app/work/rules.md`（例：`strength` 列，单位 MPa，缺失标记 NA，策略 drop_rows，依据：任务要求完整case）。
3. 按骨架生成 `/app/work/analysis.py`；`python3 analysis.py 2>&1 | tee run.log`。
4. 断言门与 SELF-CHECK 全过后，把数字填入 result.json；过 `final_checklist.md`；status=success。
5. 交付：`/app/output/result.json` + 图 PNG + 查询 CSV；`/app/work/` 留 rules.md、analysis.py、run.log 供复核。

## 适用与限制

适用于有明确 output_contract 的结构化数据分析案例（CSV/SQLite 输入）。不适用于无契约的开放式探索、需要重型建模的任务。协议的有效性依赖 Agent 遵守红线（不删断言、不吞异常、不用占位值）；清单和模板把遵守成本降到最低，但无法物理阻止违规——这是它与引擎式方案的取舍：换取对任意任务形态的适应性。
