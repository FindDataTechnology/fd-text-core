# fd-text-core 部署

照 fd-legal-search-mcp 的新仓范式（2026-10-11 立项）。

## 出片记录

| 日期 | 镜像 tag | 真源 | 通道 | 工具面 |
|---|---|---|---|---|
| 2026-10-11 | `sha-3a4ec40` | `d118d8b` | `law-ai-official/fd-text-core-images` | 10（papers+screenplay） |
| **2026-10-19** | **`sha-3283213`** | **`39b1188`** | **`testaction001/build-pool`** | **15（+gov）** |

### 2026-10-19 出片与验收（change `fix-text-core-gov-surface`）

上一版落后 20 个提交；本次带入 gov 垂域（工具面 10→15）、原串精确命中优先（`d7553b1`）、
rrf 向量侧权重 2.0（`b519ddb`）、摄取契约修复（`f27cdb9`）与检索评测层。

**出片通道选择**：`law-ai-official`（原通道账号）私有额度仅剩 17 min，冷缓存构建必超；
改用通用池 `testaction001/build-pool`（剩 1810 min）。源仓与目标仓均 private，未触泄漏门。

**五项验收（全部实跑通过，2026-10-19）**

| 项 | 结果 |
|---|---|
| 工具面 10→15 | 网络实探 `/mcp` `tools/list` = **15**，含五个 `gov_*`；papers/screenplay 十个仍在 |
| selfcheck 三方一致 | 运行时 15 == manifest 15 == 生产实探 15，双向断言通过 |
| 三域真实检索 | gov「财政部 通知」→ 财政部普惠金融文；papers「transformer attention」→ arXiv 两篇；screenplay「窦娥冤」→ 竇娥冤 / 语义「复仇与正义」→ Medea。三例均带 provenance 三元组，无降级 |
| 摄取路径冒烟 | pod 内 `load_gov.py --limit 5` → `inserted=0 skipped=5 superseded=0`（幂等），验证新 `iter_structured(s3,bucket,domain)` 签名与 0007 删约束后的库匹配 |
| 锚点余弦 | **0.999935** ≥ 0.999 门槛（销掉 10-13 SiliconFlow 402 遗留的「充值后重跑」待办） |

**出片前网内 strict**：`conformance_gate.py --changed-base d118d8b --allow-core-change --strict --eval-all`
→ **10 PASS / 0 FAIL / 0 SKIPPED / 退出码 0**。输出留档于
`openspec/changes/fix-text-core-gov-surface/reports/gate-strict-2026-10-19.log`。

**回滚**：把 `deploy/k8s/fd-text-core.yaml` 的 image 改回 `sha-3a4ec40` 并推 gitee main 即可。
库无需回滚——本次不改 schema、不写数据。

## 前置台账（已先行）

- `fd-infra-deploy/PORTS.md` §1.2 已登 **NodePort 31887**（Fd-prod 平台线，紧邻 lex 块 31882-31886）
- 根 `MCP-REGISTRY.md` §1.0 已登「仅台账、未注册 gateway」行
- the relay node `/etc/tcr-relay/repos.conf` 已加 `finddata/fd-text-core`（2026-10-11）

v1 **不注册公网 gateway 入口**——引擎只对集群内/网内 NodePort 直连开放（change 验收口径）。

## 发布通道

git push → GitHub Actions（`.github/workflows/image.yml`）→ hkccr → the relay node tcr-relay（5 分钟同步）→ ccr。
详参根仓 `docs/IMAGE-RELEASE.md` 的六步新仓清单。

## 尚未完成的一步（需用户出手）

本仓目前**只有本地 git 仓库**，尚无远端。落地需：

1. 在 Gitee 建私有仓 `FindDataTechnology/fd-text-core`（ArgoCD 的 source）+ GitHub 同名镜像仓（GHA 构建）
2. 为本仓配 GitHub secrets：`TCR_USERNAME` / `TCR_PASSWORD`
3. TCR 两侧（hkccr 与 ccr）创建命名空间 `finddata`（已存在则跳过）
4. 推远端后 ArgoCD `repo-fd-text-core` 凭据 secret + application 应用：

```bash
kubectl --context cheap -n argocd create secret generic repo-fd-text-core \
  --from-literal=url=https://gitee.com/FindDataTechnology/fd-text-core.git \
  --from-literal=username=<gitee-user> --from-literal=password=<gitee-token> \
  --from-literal=type=git
kubectl --context cheap -n argocd label secret repo-fd-text-core \
  argocd.argoproj.io/secret.type=repository
kubectl --context cheap -n argocd apply -f deploy/argocd/application.yaml
```

## 运行所需 Secret（集群内，不进 git）

> 2026-11-12：`FDTEXT_DATABASE_URL` 已指向集群内专用实例
> `fd-text-pg.fd-prod.svc.cluster.local:5432`（清单在
> `fd-infra-deploy/all-services/prod/fd-text-pg.yaml`），角色是库属主 `fd_text_app`。
>
> 2026-11-13：图库（KG 车道）也已迁进集群，`LEXKG_FALKORDB_URI` 指向
> `redis://falkordb.fd-prod.svc.cluster.local:6379`（清单在
> `fd-infra-deploy/all-services/prod/falkordb.yaml`）。笔记本上的旧实例
> 旧的 Redis 地址只作为回滚窗口保留，不再是消费目标。
> **`LEXKG_FALKORDB_USERNAME` 必须为空或不存在**：实例只设了 `requirepass`，
> 填了非空用户名会让客户端走两参 `AUTH`，直接 WRONGPASS。

```bash
kubectl --context cheap-ts -n fd-prod create secret generic fd-text-core-env \
  --from-literal=FDTEXT_DATABASE_URL='postgresql://fd_text_app:<pwd>@fd-text-pg.fd-prod.svc.cluster.local:5432/fd_text' \
  --from-literal=FDTEXT_S3_ENDPOINT='http://<object-store-host>:9000' \
  --from-literal=FDTEXT_S3_BUCKET='fd-text' \
  --from-literal=FDTEXT_S3_ACCESS_KEY='<key>' \
  --from-literal=FDTEXT_S3_SECRET_KEY='<secret>' \
  --from-literal=FDTEXT_EMBEDDING_API_BASE='https://api.siliconflow.cn/v1' \
  --from-literal=FDTEXT_EMBEDDING_API_KEY='<siliconflow-key>' \
  --from-literal=FDTEXT_EMBEDDING_MODEL='Qwen/Qwen3-Embedding-0.6B' \
  --from-literal=LEXKG_FALKORDB_URI='redis://falkordb.fd-prod.svc.cluster.local:6379' \
  --from-literal=LEXKG_FALKORDB_PASSWORD='<pwd>' \
  --from-literal=LEXKG_EMBED_BASE_URL='<embed-base>' \
  --from-literal=LEXKG_EMBED_API_KEY='<embed-key>' \
  --from-literal=LEXKG_EMBED_MODEL='<embed-model>' \
  --from-literal=LEXKG_EMBED_DIM='1024'
```

`LEXKG_FALKORDB_USERNAME` 不要给（见上）。`LEXKG_EMBED_PROVIDER_ONLY` 视部署口径可选。

凭据当前记在根仓 `INFRA-NOTES.md`「fd-text-core 专用 PG」段与 RustFS 段落。

## 验收

集群内 `initialize` / `tools/list` / `papers_search` / `papers_read` 四连实弹；
公网 gateway 上应查无此条目（负例同属验收项）。

## 外部依赖与私有化可用性

papers 垂域的引用图能力（`openalex_references` / `openalex_citations`）依赖
**OpenAlex**（`api.openalex.org`）。私有化交付时：

- **需外部网络出口**；完全隔离环境内该能力**不可用**，须显式标注而不是静默返回空。
- 授权 CC0，无需 key；礼貌池只要求 `User-Agent` 带联系邮箱（`mailto`）。
- **失败口径**（空结果必须带原因，禁静默空）：
  - `quota` — 429 配额/限流
  - `upstream` — 5xx 上游故障
  - `unreachable` — 网络不可达/超时
  - `not_indexed` — 上游可达但查无此 work（DOI/ID 未收录）
- 该能力不属 lex 交付包，也不进 `lex_packer` 登记表。
