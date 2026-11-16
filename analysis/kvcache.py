#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Read the KV-cache probe of a journey run: block tables, sharing, preemption.

harness/kvprobe/sitecustomize.py writes one JSON line per engine step with the
scheduler's view of the KV cache right after `schedule()`: every running
request's block table, the blocks it was just given, its prefix-cache hit,
preemptions, finished requests, evictions, and the pool's free count. This
module names the requests (engine request IDs -> the client's names), prints
the story step by step, and draws it.

    kvcache.py --run cpu-int8-default-prefix          # step log + figures
    kvcache.py --probe X.jsonl --client Y.json        # any probe file
"""

from __future__ import annotations

import argparse
import json
import os

R = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS = os.path.join(R, "results", "journey")
FIGURES = os.path.join(R, "figures", "journey")

COLORS = {"warmup": "#555555", "absorb": "#7a7a7a", "P1": "#4394e5", "P2": "#EE0000", "P3": "#5ba84f",
          "P4": "#f6cd62", "Q1": "#4394e5", "Q2": "#EE0000", "Q3": "#5ba84f", "Q4": "#f6cd62",
          "R": "#4394e5", "A": "#4394e5", "B": "#EE0000", "C": "#5ba84f", "D": "#f6cd62"}


def load(probe: str, client: str | None) -> tuple[list[dict], dict]:
    rows = [json.loads(line) for line in open(probe)]
    rows = [r for r in rows if "step" in r]
    ids = {}
    if client and os.path.exists(client):
        for r in json.load(open(client))["requests"]:
            if r.get("request_id"):
                ids[r["request_id"]] = r["name"]
    names: dict[str, str] = {}

    def name(rid: str) -> str:
        if rid not in names:
            names[rid] = next((n for cid, n in ids.items() if rid.startswith(cid + "-") or rid == cid), "warmup")
        return names[rid]

    for r in rows:
        for n in r["new"]:
            n["name"] = name(n["rid"])
        for c in r["cached"]:
            c["name"] = name(c["rid"])
        r["tables_n"] = {name(k): v for k, v in r["tables"].items()}
        r["reqs_n"] = {name(k): v for k, v in r["reqs"].items()}
        r["preempted_n"] = [name(x) for x in r["preempted"]]
        r["finished_n"] = [name(x) for x in r["finished"]]
        r["waiting_n"] = [name(x) for x in r["waiting"]]
        r["sched_n"] = {name(k): v for k, v in r["sched"].items()}
    return rows, names


def story(rows: list[dict], block_size: int | None = None) -> list[str]:
    """One line per step where something happened to the KV cache."""
    out = []
    t0 = rows[0]["t"] if rows else 0
    for r in rows:
        ev = []
        for n in r["new"]:
            ev.append(f"{n['name']} admitted: prefix hit {n['computed']} tok, blocks {n['blocks']}")
        for c in r["cached"]:
            if c["resumed"]:
                ev.append(f"{c['name']} RESUMED: computed {c['computed']}, blocks +{c['new_blocks']}")
            elif c["new_blocks"]:
                ev.append(f"{c['name']} +block {c['new_blocks']}")
        if r["preempted_n"]:
            ev.append("PREEMPTED " + ", ".join(r["preempted_n"]))
        if r["finished_n"]:
            ev.append("finished (blocks freed) " + ", ".join(r["finished_n"]))
        if r["evicted"]:
            ev.append(f"evicted from prefix cache: blocks {r['evicted']}")
        if ev:
            shared = f" shared {r['shared']}" if r["shared"] else ""
            out.append(f"step {r['step']:4d} +{r['t'] - t0:7.3f}s free {r['free_blocks']:4d}/{r['num_blocks']}"
                       f" hashed {r['cached_hashes']:4d}{shared} | " + " | ".join(ev))
    return out


def _style():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams.update({
        "figure.facecolor": "#151515", "axes.facecolor": "#1a1a1a", "savefig.facecolor": "#151515",
        "text.color": "#d2d2d2", "axes.labelcolor": "#d2d2d2", "xtick.color": "#a0a0a0",
        "ytick.color": "#d2d2d2", "axes.edgecolor": "#444444", "grid.color": "#2c2c2c",
        "font.size": 9, "axes.titlesize": 10.5, "axes.titleweight": "bold",
        "font.family": ["Red Hat Text", "DejaVu Sans"],
    })
    return plt


def draw_blocks(rows: list[dict], out: str, title: str, max_block: int | None = None) -> None:
    """Physical block x engine step, coloured by the request(s) holding the block."""
    plt = _style()
    from matplotlib.patches import Patch
    used = sorted({b for r in rows for t in r["tables_n"].values() for b in t})
    if not used:
        return
    top = max_block if max_block is not None else max(used)
    fig_h = min(7.5, 1.6 + 0.16 * (top + 1)) if top < 40 else 6.2
    fig, ax = plt.subplots(figsize=(11.5, fig_h))
    seen = set()
    for i, r in enumerate(rows):
        holders: dict[int, list[str]] = {}
        for nm, blocks in r["tables_n"].items():
            for b in blocks:
                holders.setdefault(b, []).append(nm)
        for b, hs in holders.items():
            if b > top:
                continue
            if len(hs) == 1:
                ax.add_patch(plt.Rectangle((i - 0.5, b - 0.5), 1, 1, color=COLORS.get(hs[0], "#888"), lw=0))
                seen.add(hs[0])
            else:  # shared: stripes of each holder
                h = 1 / len(hs)
                for k, nm in enumerate(sorted(hs)):
                    ax.add_patch(plt.Rectangle((i - 0.5, b - 0.5 + k * h), 1, h, color=COLORS.get(nm, "#888"), lw=0))
                    seen.add(nm)
                ax.add_patch(plt.Rectangle((i - 0.5, b - 0.5), 1, 1, fill=False, ec="#ffffff", lw=0.6))
        for nm in r["preempted_n"]:
            ax.axvline(i, color="#ffffff", lw=0.8, ls="--")
            ax.text(i, top + 0.6, f" {nm} preempted", fontsize=7, color="#ffffff", va="bottom", rotation=90)
        for b in r["evicted"]:
            if b <= top:
                ax.plot(i, b, marker="x", color="#ffffff", ms=4, mew=1)
    ax.set_xlim(-0.5, len(rows) - 0.5)
    ax.set_ylim(-0.5, top + 0.5)
    ax.set_xlabel("engine step (steps that scheduled work)")
    ax.set_ylabel("physical KV block id")
    ax.set_title(title, loc="left")
    ax.grid(lw=0.4)
    order = [k for k in COLORS if k in seen]
    hs = [Patch(color=COLORS[k], label=k) for k in order]
    hs.append(Patch(fill=False, ec="#ffffff", label="shared by >1 request (ref_cnt > 1)"))
    fig.legend(handles=hs, loc="lower center", ncol=8, fontsize=7.5, frameon=False)
    fig.tight_layout(rect=(0, 0.07, 1, 1))
    os.makedirs(os.path.dirname(out), exist_ok=True)
    fig.savefig(out, dpi=160)
    plt.close(fig)


def draw_pressure(rows: list[dict], out: str, title: str) -> None:
    """Free blocks and each request's computed tokens over the steps."""
    plt = _style()
    fig, (ax, ax2) = plt.subplots(2, 1, figsize=(11.5, 5.4), sharex=True, gridspec_kw={"height_ratios": [1, 2]})
    xs = list(range(len(rows)))
    ax.step(xs, [r["free_blocks"] for r in rows], where="mid", color="#F4B400")
    ax.set_ylabel("free blocks")
    ax.set_ylim(0, None)
    ax.grid(lw=0.4)
    ax.set_title(title, loc="left")
    names = sorted({n for r in rows for n in r["reqs_n"] if n.startswith("Q")})
    for k, nm in enumerate(names):
        ys, alive = [], False
        for r in rows:
            if nm in r["reqs_n"]:
                ys.append(r["reqs_n"][nm][2])
                alive = True
            elif alive and nm in r["waiting_n"]:
                ys.append(0)  # preempted: its KV is gone, it waits to be re-run
            else:
                ys.append(float("nan"))
        # identical requests overlap: draw earlier ones wider so both stay visible
        ax2.plot(xs, ys, color=COLORS.get(nm, "#888"), lw=3.2 - 0.7 * k, label=f"{nm}: tokens whose KV is in the cache")
    for i, r in enumerate(rows):
        for nm in r["preempted_n"]:
            for a in (ax, ax2):
                a.axvline(i, color="#ffffff", lw=0.7, ls="--")
            ax2.text(i, 0.5, f" {nm} preempted: blocks freed, KV dropped", rotation=90, fontsize=7,
                     color="#ffffff", transform=ax2.get_xaxis_transform(), va="center")
        for c in r["cached"]:
            if c["resumed"]:
                ax2.annotate(f"{c['name']} resumes: prefix hit {c['computed']} of {r['reqs_n'].get(c['name'], [0, 0])[1]} tokens,\n"
                             f"the rest is recomputed in this step", (i, c["computed"]),
                             xytext=(i - 62, 35), fontsize=7, color="#ffffff",
                             arrowprops=dict(arrowstyle="->", color="#ffffff", lw=0.6))
    ax2.set_ylim(0, None)
    ax2.set_ylabel("tokens with KV in the cache")
    ax2.set_xlabel("engine step (steps that scheduled work)")
    ax2.grid(lw=0.4)
    ax2.legend(fontsize=7.5, frameon=False, loc="center left")
    fig.tight_layout()
    fig.savefig(out, dpi=160)
    plt.close(fig)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run")
    ap.add_argument("--probe")
    ap.add_argument("--client")
    ap.add_argument("--out-prefix", help="figure path prefix (default figures/journey/k-<run>)")
    args = ap.parse_args()
    probe = args.probe or os.path.join(RESULTS, f"{args.run}.kvprobe.jsonl")
    client = args.client or (os.path.join(RESULTS, f"{args.run}.client.json") if args.run else None)
    rows, _ = load(probe, client)
    for line in story(rows):
        print(line)
    tag = args.run or os.path.basename(probe).split(".")[0]
    pre = args.out_prefix or os.path.join(FIGURES, f"k-{tag}")
    if "pressure" in tag:
        draw_pressure(rows, pre + "-pressure.png", f"{tag}: free blocks and each request's KV, step by step")
        draw_blocks(rows, pre + "-blocks.png", f"{tag}: which request holds each physical block")
    else:
        live = [r for r in rows if r["tables_n"]]
        draw_blocks(live, pre + "-blocks.png", f"{tag}: which request holds each physical block")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
