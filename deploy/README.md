# fd-text-core 部署

照 fd-legal-search-mcp 的新仓范式（2026-10-11 立项）。

## 前置台账（已先行）

- `fd-infra-deploy/PORTS.md` §1.2 已登 **NodePort 31887**（Fd-prod 平台线，紧邻 lex 块 31882-31886）
- 根 `MCP-REGISTRY.md` §1.0 已登「仅台账、未注册 gateway」行
- cheap-3 `/etc/tcr-relay/repos.conf` 已加 `finddata/fd-text-core`（2026-10-11）

v1 **不注册公网 gateway 入口**——引擎只对集群内/网内 NodePort 直连开放（change 验收口径）。

## 发布通道

git push → GitHub Actions（`.github/workflows/image.yml`）→ hkccr → cheap-3 tcr-relay（5 分钟同步）→ ccr。
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

```bash
kubectl --context cheap -n fd-prod create secret generic fd-text-core-env \
  --from-literal=FDTEXT_DATABASE_URL='postgresql://admin:<pwd>@100.64.0.10:5432/fd_text' \
  --from-literal=FDTEXT_S3_ENDPOINT='http://100.64.0.11:9000' \
  --from-literal=FDTEXT_S3_BUCKET='fd-text' \
  --from-literal=FDTEXT_S3_ACCESS_KEY='<key>' \
  --from-literal=FDTEXT_S3_SECRET_KEY='<secret>' \
  --from-literal=FDTEXT_EMBEDDING_API_BASE='https://api.siliconflow.cn/v1' \
  --from-literal=FDTEXT_EMBEDDING_API_KEY='<siliconflow-key>' \
  --from-literal=FDTEXT_EMBEDDING_MODEL='Qwen/Qwen3-Embedding-0.6B'
```

凭据当前记在根仓 `INFRA-NOTES.md`「fd-text-core 专用 PG」段与 RustFS 段落。

## 验收

集群内 `initialize` / `tools/list` / `papers_search` / `papers_read` 四连实弹；
公网 gateway 上应查无此条目（负例同属验收项）。
