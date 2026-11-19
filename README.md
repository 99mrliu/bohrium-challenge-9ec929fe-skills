# Bohrium Playground challenge-9ec929fe — 科研代码生成与执行（数据分析）

Three **independent, decoupled** Agent Skill bundles for the fixed data-analysis cases
(CSV snapshots + SQLite databases; `/input` → `/app/output/result.json` under output_contract).

| Bundle | 方向 | 核心思想 |
|---|---|---|
| `skill-a-datapipe-engine/` | 确定性执行引擎 | Agent 不临时写核心代码：调用纯标准库引擎 casekit.py（profile→clean→stats/sql/figure→verify→finalize），只写 rules.json 与 claims.json |
| `skill-b-contract-first/` | 契约先行代码生成协议 | 纯方法论（无引擎）：契约→口径表→带断言门与双算法复核块的单脚本→契约清单收口 |
| `skill-c-falsify-audit/` | 证伪审计双通道 | 作者通道自由分析 + 独立审计器 ledger.py 交付前独立重算每个数字，FAIL 即禁止 success |

Each bundle is self-contained: its own SKILL.md, README.md, resources; no shared files.
See each bundle's README.md for usage, method notes and a minimal example.
