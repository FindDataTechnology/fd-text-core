"""arXiv 采集器：解析与限速合规（不联网，用固定 XML 夹具）。"""
from __future__ import annotations

import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

import fd_text_core.verticals.papers.harvest as H  # noqa: E402

NS_R = 'xmlns="http://arxiv.org/OAI/arXivRaw/"'


def _wrap(inner: str) -> bytes:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">
  <ListRecords>
    <record>
      <header><identifier>oai:arXiv.org:2601.00001</identifier>
        <datestamp>2026-01-01</datestamp></header>
      <metadata><arXivRaw {NS_R}>{inner}</arXivRaw></metadata>
    </record>
  </ListRecords>
</OAI-PMH>""".encode()


def test_parse_flat_and_nested_authors():
    assert H.parse_authors(ET.fromstring(f"<authors {NS_R}>Ada Lovelace and Alan Turing</authors>")) == [
        "Ada Lovelace", "Alan Turing"
    ]
    assert H.parse_authors(ET.fromstring(f"<authors {NS_R}>A. B, C. D; E. F</authors>")) == ["A. B", "C. D", "E. F"]
    nested = H.parse_authors(
        ET.fromstring(f"<authors {NS_R}><author><forenames>John</forenames><keyname>Smith</keyname></author></authors>")
    )
    assert nested == ["John Smith"]
    assert H.parse_authors(None) == []
    # 未闭合括注不应污染结果
    assert H.parse_authors(ET.fromstring(f"<authors {NS_R}>Lucy Zhang (CITA, U of T</authors>")) == ["Lucy Zhang"]


def test_harvest_parses_record_fields():
    xml = _wrap(
        "<id>2601.00001</id><title>Test <math>paper</math> Title</title>"
        f"<authors {NS_R}>Jane Doe and John Roe</authors>"
        "<categories>q-fin.CP cs.CE</categories>"
        "<abstract>We study attention mechanisms in markets.</abstract>"
    )
    t = H.Throttler(sleep=lambda s: None)

    def fetcher(url, timeout=60):
        return xml

    got = list(H.harvest_metadata(max_records=1, throttler=t, fetcher=fetcher,
                                 from_date="2026-10-09", until_date="2026-10-09"))
    assert got, "应至少产出一批"
    papers, token = got[0]
    assert len(papers) == 1
    p = papers[0]
    assert p.arxiv_id == "2601.00001"
    assert "Test" in p.title and "paper" in p.title  # itertext 需含数学环境内文本
    assert p.authors == ["Jane Doe", "John Roe"]
    assert p.categories == ["q-fin.CP", "cs.CE"]
    assert "attention" in p.abstract
    assert p.source_url == "https://arxiv.org/abs/2601.00001"
    assert p.fulltext_url == "https://arxiv.org/e-print/2601.00001"


def test_resumption_token_drives_next_request():
    xml_page1 = _wrap("<id>2601.00002</id><title>T2</title>").replace(
        b"</ListRecords>",
        b"<resumptionToken>tok123</resumptionToken></ListRecords>",
    )
    xml_page2 = _wrap("<id>2601.00003</id><title>T3</title>")
    urls_seen = []

    def fetcher(url, timeout=60):
        urls_seen.append(url)
        return xml_page1 if "tok123" not in url else xml_page2

    t = H.Throttler(sleep=lambda s: None)
    pages = list(H.harvest_metadata(max_records=2, throttler=t, fetcher=fetcher,
                                    from_date="2026-10-09", until_date="2026-10-09"))
    assert len(pages) == 2
    assert "resumptionToken=tok123" in urls_seen[1]
    assert pages[1][0][0].arxiv_id == "2601.00003"


def test_oai_error_is_raised():
    xml = (
        b'<?xml version="1.0"?><OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">'
        b'<error code="badArgument">nope</error></OAI-PMH>'
    )
    with pytest.raises(H.HarvestError, match="badArgument"):
        list(H.harvest_metadata(max_records=1, throttler=H.Throttler(sleep=lambda s: None),
                                fetcher=lambda u, timeout=60: xml,
                                from_date="2026-10-09", until_date="2026-10-09"))


def test_throttler_enforces_three_second_spacing():
    """ToU 硬限额：相邻请求开始时刻间隔 >= 3s（请求自身耗时会吃掉间隔，故按起点计算）。"""
    now = {"t": 1000.0}

    def fake_monotonic():
        return now["t"]

    def fake_sleep(s):
        now["t"] += s

    real = time.monotonic
    H.time.monotonic = fake_monotonic
    try:
        t = H.Throttler(sleep=fake_sleep)
        starts = []
        for _ in range(4):
            t.wait()
            starts.append(now["t"])
            now["t"] += 1.2  # 模拟每次请求耗时
    finally:
        H.time.monotonic = real
    gaps = [round(starts[i + 1] - starts[i], 3) for i in range(len(starts) - 1)]
    assert all(g >= 3.0 for g in gaps), f"间隔不足: {gaps}"


def test_first_request_requires_date_window():
    """裸 set= 查询实测被服务端挂死：首请求必须带日期窗，缺即报错。"""
    with pytest.raises(H.HarvestError, match="日期窗"):
        list(H.harvest_metadata(max_records=1, throttler=H.Throttler(sleep=lambda s: None),
                                fetcher=lambda u, timeout=60: b"<x/>"))


def test_first_request_url_carries_window_and_set():
    urls = []

    def fetcher(url, timeout=60):
        urls.append(url)
        return _wrap("<id>2601.00009</id><title>T</title>")

    list(H.harvest_metadata(set_spec="cs", max_records=1, throttler=H.Throttler(sleep=lambda s: None),
                            fetcher=fetcher, from_date="2026-10-09", until_date="2026-10-09"))
    assert "set=cs" in urls[0] and "from=2026-10-09" in urls[0] and "until=2026-10-09" in urls[0]


def test_no_records_match_yields_empty_batch_not_error():
    """noRecordsMatch 是「这天没东西」：按空批返回，不中断逐日续采。"""
    xml = (
        b'<?xml version="1.0"?><OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">'
        b"<error code='noRecordsMatch'>empty</error></OAI-PMH>"
    )
    got = list(H.harvest_metadata(max_records=1, throttler=H.Throttler(sleep=lambda s: None),
                                  fetcher=lambda u, timeout=60: xml,
                                  from_date="2026-10-10", until_date="2026-10-10"))
    assert got == [([], None)]
