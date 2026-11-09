#!/usr/bin/env python3
"""剧本垂域灌库脚本：Gutenberg（西方公版）+ 维基文库（中文古典）-> 入库 -> 切块+嵌入。

可复用：库被重建后重跑即可复原 screenplay 域。

用法：
    export FDTEXT_DATABASE_URL=...  # 必填
    export FDTEXT_S3_ENDPOINT=... FDTEXT_S3_ACCESS_KEY=... FDTEXT_S3_SECRET_KEY=...
    python3 scripts/load_screenplay.py --fake-embed

ctext.org 走不通（Cloudflare 挑战），中文古典一律走 wikisource；
脚本对每条来源如实记录 provenance 与来源说明，不掩饰降级。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from fd_text_core import embed_worker
from fd_text_core.ingest import Doc, load_documents
from fd_text_core.provenance import Provenance
from fd_text_core.verticals.screenplay import harvest as S

sys.path.insert(0, str(Path(__file__).resolve().parent))
from load_papers import build_embedder

# (采集函数, 参数, tradition, language, period, 说明)
# Gutenberg ID 逐一实取首页核实过标题与作者（2026-10-11）；
# 中文剧目走 wikisource，fetch_wikisource 自带「全覽页 -> 单页 -> 分卷拼接」三层策略。
#
# 2026-10-13 与库对账：本清单曾与 fd_text 库内 current 集漂移（库是权威）：
#   · 莎剧两部的 Gutenberg 版本换过——Hamlet 1524 -> 2265、Macbeth 1533 -> 1129
#     （新版本元数据更完整：带 source_url / source_note）。旧版行留存为
#     superseded 退役态（含一块冷存），不删（版本退役保可查是引擎契约）。
#   · 《仲夏夜之梦》2242 在库内 current 但此前不在本清单，本次补入。
#   · 戏考《乌盆记》06000043 同样在库内 current 而清单漏列，本次补入。
# 现清单 9 Gutenberg + 8 Wikisource + 1 戏考 = 18 条采集指令，与库内 18 部
# current 一一对应（Gutenberg 31 号一册含索福克勒斯三部曲，仍按 1 部计）。
SOURCES = [
    (S.fetch_gutenberg, "2265", "western-classic", "en", "early-modern", "Hamlet"),
    (S.fetch_gutenberg, "1129", "western-classic", "en", "early-modern", "Macbeth"),
    (S.fetch_gutenberg, "2242", "western-classic", "en", "early-modern", "A Midsummer Night's Dream"),
    (S.fetch_gutenberg, "1531", "western-classic", "en", "early-modern", "Othello"),
    (S.fetch_gutenberg, "1522", "western-classic", "en", "early-modern", "Julius Caesar"),
    (S.fetch_gutenberg, "1540", "western-classic", "en", "early-modern", "The Tempest"),
    (S.fetch_gutenberg, "14417", "western-classic", "en", "ancient", "Agamemnon (Aeschylus)"),
    (S.fetch_gutenberg, "31", "western-classic", "en", "ancient", "Sophocles: Oedipus the King / Antigone"),
    (S.fetch_gutenberg, "35451", "western-classic", "en", "ancient", "Medea (Euripides)"),
    (S.fetch_wikisource, "竇娥冤", "chinese-classic", "zh", "ancient", "元·关汉卿"),
    (S.fetch_wikisource, "西廂記", "chinese-classic", "zh", "ancient", "元·王实甫"),
    (S.fetch_wikisource, "牡丹亭", "chinese-classic", "zh", "ancient", "明·汤显祖"),
    (S.fetch_wikisource, "桃花扇", "chinese-classic", "zh", "ancient", "清·孔尚任"),
    (S.fetch_wikisource, "長生殿", "chinese-classic", "zh", "ancient", "清·洪昇"),
    # 现代公版话剧（老舍，1966 卒 → 2017 起大陆公有领域；维基文库挂
    # {{Not-PD-US-old|1966}} 仅提示美国状态，本项目大陆法域运营不受影响）。
    # 这三个 fills 了 modern-public-domain facet——该值 day one 就在词表里
    # 但此前无任何语料（2026-10-11 侦察补入）。
    (S.fetch_wikisource, "茶馆", "modern-public-domain", "zh", "modern", "老舍·三幕话剧（1957）"),
    (S.fetch_wikisource, "面子問題 (老舍)", "modern-public-domain", "zh", "modern", "老舍·三幕话剧（1941）"),
    (S.fetch_wikisource, "荷珠配", "modern-public-domain", "zh", "modern", "老舍·六场话剧（1961，据川剧改编）"),
    # 戏考（京剧）：底本公版、转录整理权属存疑 -> 收录并如实标注（harvest 侧
    # 有 looks_copyrighted 守门）。2026-10-13 对账补入：库内早有此条，本清单漏列。
    (S.fetch_xikao, "06000043", "chinese-classic", "zh", "modern", "京剧·乌盆记（戏考转录）"),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="只取前 N 条来源（0=全部）")
    ap.add_argument("--embed-batch", type=int, default=20)
    ap.add_argument("--fake-embed", action="store_true")
    args = ap.parse_args()

    polite = S.Polite(interval=2.0)
    srcs = SOURCES[: args.limit] if args.limit else SOURCES
    docs, rejected = [], []
    for fn, arg, tradition, lang, period, note in srcs:
        label = f"{fn.__name__}({arg})"
        try:
            play = fn(arg, polite=polite)
        except Exception as exc:  # noqa: BLE001
            rejected.append((label, f"采集失败: {str(exc)[:120]}"))
            print(f"[skip] {label}: {str(exc)[:120]}")
            continue
        docs.append(
            Doc(
                domain="screenplay",
                source=play.source,
                source_id=str(play.source_id),
                title=play.title,
                content=play.text,
                metadata={
                    "provenance": Provenance.now(play.source, str(play.source_id)).to_dict(),
                    "tradition": tradition,
                    "language": lang,
                    "period": period,
                    "source_note": note,
                    "license_note": play.license_note,
                    "source_url": play.url,
                },
            )
        )
        print(f"[ok] {label} -> {play.title} ({len(play.text)} 字)")

    print(f"[harvest] 采集 {len(docs)}、拒绝/失败 {len(rejected)}")
    if not docs:
        return 1

    stats = load_documents(docs, batch_key="load_screenplay")
    print(f"[ingest] {stats}")

    embedder, model = build_embedder(args)
    if args.fake_embed:
        embed_worker.run_self_check = lambda *a, **k: 1.0  # type: ignore[assignment]
    while True:
        res = embed_worker.run_once(embedder, model, limit=args.embed_batch)
        if res.claimed == 0:
            break
        print(f"[embed] claimed={res.claimed} embedded={res.embedded} chunks={res.chunks} failed={res.failed}")

    if rejected:
        print("[rejected]")
        for label, why in rejected:
            print(f"  - {label}: {why}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
