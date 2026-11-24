"""守护：声明了 facet 词表的垂域必须有 facet 投影。

为什么需要它（change: unify-facet-projection-at-source）：facet 投影是「一个域的
facet 规则住一个地方」的落点。若某域声明了词表却没投影，加载器就无 facet 可算，
按它过滤**静默返回空集**——这比报错更坏（用户看到「没有这类文档」而不是「配置漏了」）。
实测形态：govcn 14831 篇入库后 facet 全空、period=2024 命中 0。
"""
from __future__ import annotations

import pytest

from fd_text_core.verticals import registry

#: facet 值由各自**专用 loader 在采集时写入**的域（人工策展表，不可从记录派生）：
#: papers 的 cat 来自 arXiv 元数据的 categories、screenplay 的 tradition/language
#: 来自 load_screenplay 里的策展指令表。它们不需要投影；新域若也属这一类，须在此
#: 显式登记——加进来就是一次「我确认这个域的 facet 不靠派生」的决定，而非默认放行。
AUTHORED_AT_INGEST = {"papers", "screenplay"}


@pytest.mark.parametrize("domain", registry.list_verticals())
def test_domain_with_facets_declares_projector(domain):
    vert = registry.get(domain)
    if not vert.facets:
        pytest.skip(f"{domain}: 未声明 facet 词表，无需投影")
    if domain in AUTHORED_AT_INGEST:
        pytest.skip(f"{domain}: facet 由专用 loader 采集时写入（策展数据），不派生")
    assert vert.facet_projector is not None, (
        f"{domain} 声明了 facet 词表 {sorted(vert.facets)} 却没有 facet_projector；"
        "加载器将无 facet 可算，按它过滤会静默返回空集"
    )
