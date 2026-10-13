"""引擎级检索评测：数据集契约 + 参数化跑分 + 结果趋势。

设计与依据见 `openspec/changes/text-retrieval-eval/design.md`。要点：

- **数据集进包**（`eval/datasets/<domain>/annotated_queries.json`）而非 tests：
  Dockerfile 只拷 `fd_text_core/ scripts/ deploy/`，tests 不进镜像，放 tests 下
  则容器内与质量门都读不到（一手核实的打包事实，见 design D1）。
- **只消费检索公开契约**：跑分只调 `querylib.hybrid_search`，不自己拼 SQL、
  不复制 RRF 逻辑。度量与被度量必须走同一条路径（design D5）。
- **结果入库带双指纹**：只存分数无法回答「这次变差是检索退化还是我改了标注集/
  换了模型」，故记数据集指纹与配置指纹（design D3）。
"""
from __future__ import annotations

from .dataset import (
    DatasetError,
    compute_fingerprint,
    list_datasets,
    load_dataset,
)
from .runner import (
    CHANNELS,
    EvalReport,
    compare_with_previous,
    compute_config_fingerprint,
    run_eval,
    run_eval_all,
)
from .store import check_expects_exist, latest_previous, store_run

__all__ = [
    "CHANNELS",
    "DatasetError",
    "EvalReport",
    "check_expects_exist",
    "compare_with_previous",
    "compute_config_fingerprint",
    "compute_fingerprint",
    "latest_previous",
    "list_datasets",
    "load_dataset",
    "run_eval",
    "run_eval_all",
    "store_run",
]
