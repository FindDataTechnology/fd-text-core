# fd-text-core

> **Part of the [Wire line (寻数·柏讯)](https://www.finddatatech.cloud/products/wire) of [FindData](https://www.finddatatech.cloud)** — the text engine: corpus ingest, chunked retrieval and domain verticals (news, papers) served as a retrieval API.

通用文本处理引擎（纯基础设施，无产品面）。OpenSpec change：`text-engine-foundation`。

## 它解决什么

采集层已有能力，但**文本中间层缺失**：清洗、结构化、切块、向量化、索引这些逻辑散在多个仓各自实现。本引擎把它们收敛为一层，并让**垂域变成配置**——接入一个新文本垂域只交付四件东西：

1. 域解析器（干净文本 → 结构树 + 元数据）
2. 切块策略
3. facet 词表
4. 检索预设包

守门不变量：**接入垂域不得修改引擎核心模块**（CI 有路径越界检查）。

## 分层

| 层 | 职责 |
|---|---|
| L1 摄取 | 格式适配 → `corpus_documents`（hash 去重 / 版本退役）→ zstd 冷存 |
| L2 结构化 | 域解析器接口 + 无损切块（拼接可还原原文、offset 连续） |
| L3 索引 | 单一嵌入模型（Qwen3-Embedding-**8B**，MRL 截断至 1024 维——8B 原生 4096 维超 pgvector HNSW 的 2000 上限，故必须截断）+ pgvector（HNSW 索引在 alembic 迁移链内）+ 写入即挂钩的嵌入管线 + 余弦自检门 |
| L4 查询 | tsvector ⊕ 向量 RRF 混合、facet 过滤、provenance、单 MCP 服务（域命名空间工具） |

## 快速开始

```bash
# 依赖与测试
pip install -e ".[dev]"
pytest -q

# 迁移（FDTEXT_DATABASE_URL 必填，不回落默认值）
python -m alembic -c fd_text_core/alembic.ini upgrade head

# 质量门（只读，CI 必跑）
python scripts/conformance_gate.py --changed-base origin/main

# 垂域验收唯一合法口径：strict（任一 SKIPPED 即失败）
# 需在网内执行并注入 FDTEXT_DATABASE_URL 等连接，否则 DB 类检查会如实报 SKIPPED
python scripts/conformance_gate.py --strict --vertical-commits <sha>

# 起服务
python -m fd_text_core.server        # MCP 端点 /mcp
```

## 关键约定

- **schema 唯一事实源是 alembic 迁移链**，库外手工建表由质量门判为漂移违规
- **切块不变量**：拼接全部 chunk 必须逐字符还原正文（因此窗口是铺砌，不是重叠）
- **provenance 三元组**：`source` / `stable_id` / `as_of`，每条结果必带
- **诚实降级**：上游失联返回结构化错误并说明缺哪个依赖，不拿半份结果冒充完整
- **版权红线（论文垂域）**：全文仅内部索引处理，对外只出元数据 + 切块引用 + 原文链接，见 `fd_text_core/verticals/papers/LICENSE-NOTE.md`

## 部署

见 `deploy/README.md`（台账先行：PORTS.md NodePort 31887、MCP-REGISTRY.md §1.0）。
