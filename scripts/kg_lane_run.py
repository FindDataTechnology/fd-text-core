"""车道跑批器：按垂域配置把 episode 灌进图，断点续跑、逐条容错、自动记账。

用法（zihan）:
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

from fd_text_core.kg_lane import config as cfg_mod  # noqa: E402
from fd_text_core.kg_lane import cost as cost_mod  # noqa: E402
from fd_text_core.kg_lane import lane as lane_mod  # noqa: E402


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

    为什么必须有：zihan 的 FalkorDB 是 `save` 空、AOF 关的零持久化实例
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
    ok_since_snap = 0
    t0 = time.time()
    for i, ep in enumerate(pending, 1):
        ref = ep.get("valid_at") or ep.get("provenance", {}).get("as_of")
        try:
            ref_dt = datetime.fromisoformat(ref) if ref else datetime.now().astimezone()
        except ValueError:
            ref_dt = datetime.now().astimezone()
        try:
            await kg.add_episode(
                name=ep["episode_id"],
                body=ep["content"],
                reference_time=ref_dt,
                source_description=f"corpus:{cfg.corpus.domain}:{ep.get('kind', '')}",
            )
            state["done"].append(ep["episode_id"])
            save_state(args.state, state)
            ok_since_snap += 1
            print(f"[{i}/{len(pending)}] OK {ep['episode_id']} "
                  f"({len(ep['content'])} ch) {(time.time() - t0) / i:.1f}s/ep avg", flush=True)
        except Exception as exc:  # noqa: BLE001 - 逐条容错，失败记录后继续
            state["failed"].append({"id": ep["episode_id"], "error": str(exc)[:300]})
            save_state(args.state, state)
            print(f"[{i}/{len(pending)}] FAIL {ep['episode_id']}: {exc!r}", flush=True)
        if args.bgsave_every and ok_since_snap >= args.bgsave_every:
            ok_since_snap = 0
            print(f"  BGSAVE @ {len(state['done'])} done: "
                  f"{snapshot(snap_uri, username=settings.falkor_username, password=settings.falkor_password)}",
                  flush=True)

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
