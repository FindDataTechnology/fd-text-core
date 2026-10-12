# 垂域注册表说明

本目录是**垂域（vertical）配置的唯一落点**。守门不变量：

> 接入一个新文本垂域 = 交付四件配置，**引擎核心模块零改动**。

引擎核心模块指 `fd_text_core/` 包根下的：
`config.py` / `db.py` / `provenance.py` / `ingest.py` / `chunking.py` /
`coldstore.py` / `embedding.py` / `embed_worker.py` / `querylib.py` / `server.py`。
CI 的路径越界检查会拦截任何触碰这些文件的垂域改动。

## 四件配置

| 配置 | 落点 | 说明 |
|---|---|---|
| 域解析器 | `verticals/<domain>/parser.py` | 干净文本 → 结构树 + 元数据 |
| 切块策略 | `verticals/<domain>/__init__.py` 里的 `strategy=` | 可插拔；产物必须过无损还原校验 |
| facet 词表 | `verticals/<domain>/__init__.py` 里的 `FACETS` | 词表外的过滤值一律拒绝并回显可用值 |
| 检索预设包 | `verticals/<domain>/__init__.py` 里的 `PRESETS` | 声明式参数封装，不含 SQL，不绕过 facet 校验 |

外加两个声明位：`language`（决定 CJK 分词是否启用）与 `tsv_config`。

## 中文分词

引擎提供**原生 CJK 分词**（unigram + bigram + 繁简归一），零第三方扩展依赖、
可私有化。中文垂域在注册时声明 `language="zh"`，由引擎写入侧与查询侧同构分词
保证召回。

`zhparser` 保留为可选升级路径（当前 PG 实例不可得该扩展：实例无此扩展且宿主无
管理通道）。若将来装上，切换只是垂域配置动作——`tsv_config` 改指 zhparser，
引擎 schema 与迁移链零变更。

## 存储侧

新垂域还需要 `corpus_chunks` 的一个 LIST 分区（见
`fd_text_core/migrations/versions/0002_screenplay_partition.py` 的写法）。加分区
属运维/迁移动作，不是引擎代码改动。

## 接入后

- 工具面自动挂载 `<domain>_*` 命名空间工具（单 MCP 服务，不新增 Deployment）
- 向量索引：分区继承母表 `ix_chunks_embedding_hnsw`，无需单独建索引
  （母表索引在分区 ATTACH 时自动派生，迁移里只建母表索引，见 0004 注释）
- 验收走统一模板：语料入库含 provenance / 标注查询集命中达标 / 工具面可用 /
  质量门 `--strict` 全绿
