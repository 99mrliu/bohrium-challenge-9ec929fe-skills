---
name: contract-first-analysis
description: 契约先行的科研数据分析代码生成协议。当任务是在 /input 给定 case.json（含 output_contract 与数据字典）、task.md 与科研数据（CSV/SQLite），要求生成并执行清洗、统计、可视化、查询代码，在 /app/output 交付与契约逐字段对齐的 result.json 时使用。核心方法：先契约后代码——读题后立即写出 result.json 字段骨架与"口径表"（字段映射/缺失值策略/去重策略/单位），再生成一个自带断言门和双算法复核块的单文件脚本，一次跑通；最后用契约清单逐字段收口。不随附任何执行引擎，全部方法论以模板和清单给出，模型按协议自己生成代码。不适用：无明确输出契约的开放式探索分析。
---

# contract-first-analysis：契约先行·口径锁定·单脚本断言门

## 核心主张

数据分析 Agent 的失败大多不是代码写不出来，而是**先写代码后想口径**：字段名按猜测拼、缺失值默认当 0、重复行没人管、输出字段最后才凑。本协议把顺序强制倒过来：**契约 → 口径 → 代码 → 复核 → 收口**，每一步都有可检查的物证（模板见 `templates/`，清单见 `checklists/`）。协议不替你算任何东西——它约束你生成的每一段代码在写之前就声明好自己算什么、不算什么都报错。

## 五个阶段（严格按序）

### 阶段 0：读题取证（约 2 次模型调用内完成）

1. 列目录：`ls -laR /input`；全部文件计入 `/app/work/inventory.md`（文件名、字节数、`shasum -a 256` 哈希）。
2. 读 `/input/case.json`：摘出 `case_id` 与 `output_contract` 的**逐字段清单**；读 `task.md` 逐句标出要求交付物（统计量/图/查询/清洗规则）。
3. 读数据字典；对数据文件只读表头和前几行（`head -3`、SQLite 用 `.schema` 和 `SELECT COUNT(*)`），**不要**在这一阶段做任何计算。
4. 立即写 `/app/output/result.json` 骨架：`case_id` 填真值，契约每个字段先置 null，`status: "in_progress"`，`provenance` 填入 Python 版本与输入哈希。**此后任何时刻中断都有合法半成品。**

### 阶段 1：口径表（写代码之前的强制签字画押）

把 `templates/rules_template.md` 复制为 `/app/work/rules.md` 并逐行填写：每个用到的列的**真实列名**（从表头复制，不凭记忆）、单位、缺失值标记与处理策略、重复行策略、筛选条件、统计口径（样本 std 用 n-1、百分位插值方法、检验名称与前提）。两条铁律：

- 任何策略必须能在数据字典或任务文本里找到依据；找不到依据的默认**保守策略**（缺失不参与统计、重复不合并），并在 result.json 的 anomalies 中记录"存在另一种解释"。
- 口径表中没有的列，代码里不许出现。

### 阶段 2：生成单脚本（一个文件，一次跑通）

按 `templates/script_skeleton.py.txt` 生成**一个** `/app/work/analysis.py`，结构固定为四段：

1. **断言门（脚本头部）**：载入后立刻断言——必需列全部存在（缺失时打印实际列名后退出码 2）、原始行数等于数据字典声明值（不符时打印两者后退出）、关键列类型可解析。断言失败 = 停止，不是警告。
2. **清洗段**：严格按 rules.md 实现，每一步打印 `行数: N -> M (规则: ...)`；被排除的行数必须单独计数，**不许** `dropna()` 一把梭后说不清删了什么。
3. **计算段**：每个契约要求的统计量/图/查询各一小节，图保存到 `/app/output/`（标题、轴标签、单位逐字来自数据字典），查询结果同时落 CSV。
4. **复核块（脚本尾部，必需）**：按 `templates/verification_block.py.txt` 用**第二种实现**（pandas 算的就用纯 csv+stdlib 重算；stdlib 算的就用 sqlite3 重算）独立复算每个 headline 数字，容差 1e-6 相对误差，全部一致才打印 `SELF-CHECK PASSED`，否则打印差异表并以退出码 1 结束。

依赖策略：先 `import pandas` 试；失败则回退纯标准库（csv/sqlite3/statistics），matplotlib 不可用时保存绘图数据 CSV 并在结果中声明图未渲染。**禁止** pip install（评测环境不可控）；禁止把路径写死成 `/input` 与 `/app` 之外的任何位置。

### 阶段 3：执行与日志（只许一个脚本，最多修三轮）

```bash
cd /app/work && python3 analysis.py 2>&1 | tee /app/work/run.log; echo "exit=$?"
```

失败时按 `templates/failure_playbook.md` 对号入座（缺列→回阶段 1 改口径；编码错误→显式 encoding 链；空查询→核对表名/连接键/WHERE；内存/超时→分块或 SQL 下推）。**同一处失败修两次还失败，必须改变策略类别**（换实现路径），不得第三次微调同一行。禁止为让脚本"跑完"而删除断言或捕获后忽略异常。

### 阶段 4：复核与契约收口

1. 脚本必须已打印 `SELF-CHECK PASSED`；没有就回到阶段 2 补齐复核块。
2. 把计算结果逐字段填入 result.json；图用绝对路径；`anomalies` 如实记录缺失、异常、空查询、未渲染的图、无法解释的差异。
3. 逐条过 `checklists/final_checklist.md`（契约字段齐全且非占位、每个数字能指回 run.log 的一行、图的轴/单位与数据字典一致、行数账平、provenance 完整、status 与事实相符）。全部打勾后才允许 `status: "success"`；任何一项做不到就 `status: "partial"` 并在 anomalies 说明缺什么。

## 红线

- 不许交付未经复核块确认的数字；不许先填 result.json 后跑脚本；不许用"看起来合理"的值占位；不许静默吞异常；不许删除断言；不许伪造运行日志或环境信息。
