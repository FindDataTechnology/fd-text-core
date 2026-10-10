# 论文垂域：许可红线与全文分发禁令

## 我们能做什么

arXiv 条款（Terms of Use for the arXiv APIs + Submission/Distribution policy）：

- 元数据（标题/作者/摘要/分类）为 **CC0**，可自由持有、索引、再分发。
- 论文全文可以**下载、索引、做内部处理**（这是我们引擎做的事）。
- 不得**从自家服务器对外提供论文全文**（no re-serving full text from another
  server）——用户要全文必须指向 arXiv.org。

## 因此本垂域的硬规则

1. 全文只进冷存供引擎内部解析与索引，**任何对外端点不得返回全文文件**。
2. `paper_read` 等工具返回：切块引用（含 `doc_id` / offset / 出处）+ 指向
   arXiv 的链接 + CC0 元数据。不允许出现"下载全文"工具。
3. 仅对明确 CC 授权的论文子集才可能旁路下载成品，且须另行逐个核对；v1 不做。
4. Web 端 crawling 需遵守 arXiv 公布的策略并标明 UA。

## 解析器许可约束（选型不可回退）

| 组件 | 许可 | 结论 |
|---|---|---|
| opendetex / 自写 detex | NCSA | 可用 |
| **GROBID** | Apache-2.0 | **PDF 兜底唯一选择** |
| PyMuPDF | AGPL-3.0 / 商业双许可 | **禁用**——与闭源平台冲突 |
| Marker | 代码 Apache-2.0，但**模型权重非商用**（营收 >$5M 需付费） | **禁用** |

变更这条线前请先在 issue 里说明理由，不要在 PR 里静默改换解析器。
