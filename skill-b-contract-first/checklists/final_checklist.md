# 交付前契约清单（final_checklist）——逐项打勾后才允许 status=success

## A. 契约完整性
- [ ] result.json 含 `case_id`，且与 case.json 逐字一致
- [ ] output_contract 的**每一个**字段存在且非 null、非占位符
- [ ] 字段类型与契约语义一致（计数是整数、比率是浮点、路径是字符串）

## B. 数字可复核
- [ ] 脚本打印了 `SELF-CHECK PASSED`，且复核 claims 数 ≥ 契约中的数字个数
- [ ] result.json 里每个数字能在 run.log 中找到对应 `[stat]`/`[sql]` 行
- [ ] 行数账平：原始行数 = 使用行数 + 各规则排除行数（clean 段打印可佐证）
- [ ] 缺失值没有被当作 0 或删得无记录；重复行策略已声明且执行

## C. 图与查询
- [ ] 每张图的标题、轴标签、单位逐字来自数据字典（不是自创措辞）
- [ ] 图文件真实存在且非空（字节数 > 0）；未渲染的图已在 anomalies 声明
- [ ] 每条 SQL 落盘 CSV 且打印了行数；空结果集已核实并在 anomalies 注明

## D. 可复跑
- [ ] provenance 含：Python 版本、平台、依赖可用性、全部输入文件 sha256
- [ ] 涉及随机性时种子已记录（本案例若无随机步骤，写明"无随机步骤"）
- [ ] run.log 完整（从断言门到 SELF-CHECK PASSED），退出码为 0

## E. 诚实性
- [ ] anomalies 覆盖：缺失、异常值、空查询、口径二义性、未完成的契约项
- [ ] status 与事实相符：任何一项做不到即 partial，并在 anomalies 写明缺什么
- [ ] 没有凭记忆写的列名、没有"看起来合理"的数、没有被吞掉的异常
