"""车道跑批器：按垂域配置把 episode 灌进图，断点续跑、逐条容错、自动记账。

用法（the graph host）:
    set -a; . /home/ubuntu/lexkg-data/pilot.env; set +a
    python scripts/kg_lane_run.py --vertical legal \
        --episodes /home/ubuntu/lexkg-data/pilot_episodes.jsonl \
        --state /home/ubuntu/lexkg-data/lane_legal_state.json \
        --limit 3 --rebuild

`--rebuild` 先删该垂域的图再建（对比试点图必须从零起，否则旧节点混入）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fd_text_core.kg_lane import config as cfg_mod
from fd_text_core.kg_lane import cost as cost_mod
from fd_text_core.kg_lane import lane as lane_mod


def load_state(path: str) -> dict:
    p = Path(path)
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return {"done": [], "failed": []}


def save_state(path: str, state: dict) -> None:
    Path(path).write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def load_episodes(path: str) -> list[dict]:
    return [
        json.loads(line)
        for line in Path(path).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


async def drop_graph(graphiti, group_id: str) -> None:
    """删掉整张图（FalkorDB 的图名 = group_id）。"""
    driver = graphiti.driver
    clone = getattr(driver, "clone", None)
    if clone is not None:
        driver = clone(database=group_id)
    try:
        await driver.execute_query(f"GRAPH.DELETE {group_id}")
    except Exception as exc:  # noqa: BLE001 - 图不存在时删除会报错，属正常
        print(f"drop graph {group_id}: {exc!r} (ignored)")


def snapshot(uri: str, *, username: str = "", password: str = "") -> str:
    """对图实例做一次 BGSAVE（零持久化实例唯一的数据保全手段）。

    为什么必须有：the graph host 的 FalkorDB 是 `save` 空、AOF 关的零持久化实例
    （2026-10-21 复验报告记录）。2026-10-22 整机用户态卡死时，内存里
    152 篇散文（约 4 小时）没有任何落盘副本——只能在跑批过程中周期性快照。
    快照失败只告警不中断（保全尽力而为，跑批不该为它停）。
    """
    from falkordb import FalkorDB  # 懒加载：单测环境无图库依赖

    host, port = lane_mod.parse_uri(uri)
    creds = {}
    if username:
        creds["username"] = username
    if password:
        creds["password"] = password
    try:
        return str(FalkorDB(host=host, port=port, **creds)
                   .connection.execute_command("BGSAVE"))
    except Exception as exc:  # noqa: BLE001
        return f"BGSAVE failed: {exc!r}"


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--vertical", required=True)
    ap.add_argument("--episodes", required=True)
    ap.add_argument("--state", required=True)
    ap.add_argument("--config", default="")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--rebuild", action="store_true")
    ap.add_argument("--aggregate-only", action="store_true")
    ap.add_argument("--bgsave-every", type=int, default=20,
                    help="每 N 条成功 episode 做一次 BGSAVE（0=关闭）")
    ap.add_argument("--bgsave-uri", default="",
                    help="图实例地址，默认取 LEXKG_FALKORDB_URI")
    ap.add_argument("--episode-concurrency", type=int, default=1,
                    help="同时处理的 episode 篇数（默认 1=逐篇串行）。"
                         "LEXKG_SEMAPHORE_LIMIT 只并行单篇内部调用，不并行篇与篇")
    args = ap.parse_args()

    cfg_path = args.config or (
        Path(__file__).resolve().parents[1]
        / "fd_text_core/kg_lane/verticals"
        / f"{args.vertical}.kg.json"
    )
    cfg = cfg_mod.load(cfg_path)
    settings = lane_mod.settings_from_env()

    ledger = cost_mod.CostLedger(settings.cost_ledger_path).load_existing()
    graphiti = lane_mod.build_graphiti(settings, ledger=ledger, config=cfg)
    kg = lane_mod.KGLane(config=cfg, settings=settings, graphiti=graphiti, ledger=ledger)

    if args.aggregate_only:
        await graphiti.close()
        print(json.dumps(kg.summarize_costs(), ensure_ascii=False, indent=1))
        return 0

    if args.rebuild:
        await drop_graph(graphiti, cfg.group_id or cfg.name)

    episodes = load_episodes(args.episodes)
    if args.limit:
        episodes = episodes[: args.limit]
    kg.corpus_index = lane_mod.build_corpus_index(episodes)
    state = load_state(args.state)
    done = set(state["done"])
    pending = [e for e in episodes if e["episode_id"] not in done]
    print(f"vertical={cfg.name} group={cfg.group_id} total={len(episodes)} "
          f"done={len(done)} pending={len(pending)}", flush=True)

    from datetime import datetime

    snap_uri = args.bgsave_uri or settings.falkor_uri
    conc = max(1, args.episode_concurrency)
    sem = asyncio.Semaphore(conc)
    gate = asyncio.Lock()          # 只保护状态写入与计数，不覆盖 await 图写入
    done_n = 0
    ok_since_snap = 0
    t0 = time.time()

    async def run_one(ep: dict) -> None:
        """处理单篇：失败只记账不抛出（与串行路径同口径）。"""
        nonlocal done_n, ok_since_snap
        ref = ep.get("valid_at") or ep.get("provenance", {}).get("as_of")
        try:
            ref_dt = datetime.fromisoformat(ref) if ref else datetime.now().astimezone()
        except ValueError:
            ref_dt = datetime.now().astimezone()
        async with sem:
            snap_now = False
            try:
                await kg.add_episode(
                    name=ep["episode_id"],
                    body=ep["content"],
                    reference_time=ref_dt,
                    source_description=f"corpus:{cfg.corpus.domain}:{ep.get('kind', '')}",
                )
                async with gate:
                    state["done"].append(ep["episode_id"])
                    save_state(args.state, state)
                    done_n += 1
                    ok_since_snap += 1
                    n, avg = done_n, (time.time() - t0) / done_n
                    if args.bgsave_every and ok_since_snap >= args.bgsave_every:
                        ok_since_snap = 0
                        snap_now = True
                print(f"[{n}/{len(pending)}] OK {ep['episode_id']} "
                      f"({len(ep['content'])} ch) {avg:.1f}s/ep avg", flush=True)
            except Exception as exc:  # noqa: BLE001 - 逐条容错，失败记录后继续
                async with gate:
                    state["failed"].append(
                        {"id": ep["episode_id"], "error": str(exc)[:300]})
                    save_state(args.state, state)
                print(f"FAIL {ep['episode_id']}: {exc!r}", flush=True)
            if snap_now:  # 快照在锁外做（BGSAVE 是重 IO，不该挡住别的篇）
                print(f"  BGSAVE @ {len(state['done'])} done: "
                      f"{snapshot(snap_uri, username=settings.falkor_username, password=settings.falkor_password)}",
                      flush=True)

    if conc == 1:
        for ep in pending:
            await run_one(ep)
    else:
        print(f"episode concurrency = {conc}", flush=True)
        await asyncio.gather(*(run_one(ep) for ep in pending))

    if args.bgsave_every and ok_since_snap:
        print(f"  BGSAVE(final) @ {len(state['done'])} done: "
              f"{snapshot(snap_uri, username=settings.falkor_username, password=settings.falkor_password)}",
              flush=True)

    await graphiti.close()
    print(f"finished done={len(state['done'])} failed={len(state['failed'])} "
          f"wall={time.time() - t0:.0f}s")
    print(json.dumps(kg.summarize_costs(), ensure_ascii=False, indent=1))
    return 0 if not state["failed"] else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
