"""Fill README.md, docs/PITCH.md, docs/LOOM.md and docs/curve.svg from the benchmark result files,
so every number in the docs is one the benchmark printed.   python scripts/render_docs.py
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bench"))
from run import table  # noqa: E402

res = json.loads((ROOT / "bench/results/learning.json").read_text())
a = res["aggregate"]
b, f, c0, cn = a["baselines"], a["final"], a["curve"][0], a["curve"][-1]
m = lambda d, k: d[k]["mean"]
n = lambda x: f"{round(x):,}"
load_path = ROOT / "bench/results/load.json"
load_md = ""
if load_path.exists():
    L = json.loads(load_path.read_text())
    rows = "\n".join(f"| {l['concurrency']} | {l['rps']} | {l['p50_ms']} | {l['p95_ms']} | {l['p99_ms']} | {l['writes']} | {l['errors']} |" for l in L["levels"])
    srows = "\n".join(f"| {s['tools']:,} | {s['edges']:,} | {s['ingest_s']} | {s['rps']} | {s['p50_ms']} | {s['p95_ms']} | {s['errors']} |" for s in L["scale"])
    load_md = (
        f"### Under load\n\n`bench/load.py` on {L['machine']}; {L['seconds_per_level']:.0f} s per level; one resolve in five also writes "
        "an outcome back (edge reweighting while reads continue).\n\n"
        "| concurrent agents | resolves / s | p50 ms | p95 ms | p99 ms | outcome writes | errors |\n|---|---|---|---|---|---|---|\n" + rows +
        "\n\nCatalog scaled by cloning every server, 8 concurrent agents, reads only:\n\n"
        "| tools | edges | ingest s | resolves / s | p50 ms | p95 ms | errors |\n|---|---|---|---|---|---|---|\n" + srows +
        f"\n\nNo errors at any level. With the word-edge cache off (every hop read from HydraDB every time) the same mix at 8 agents "
        f"runs at {L['uncached']['rps']} resolves/s, p50 {L['uncached']['p50_ms']} ms. The HydraDB node, its indexer and the single-process "
        "Python load generator share the same two cores here and the node is CPU-bound, so these are floors for this box, not the engine's ceiling. "
        "Outcome writes are serialised per Hydrant process."
    )
v = {
    "PRELOAD": n(m(b["preload_all"], "tokens")), "N_TOOLS": n(m(b["preload_all"], "tools")), "TASKS": str(res["tasks"]),
    "SEEDS": str(res["seeds"]), "SEEN": str(cn["seen"]), "R0": f"{m(c0, 'complete'):.1f}", "RN": f"{m(cn, 'complete'):.1f}",
    "R0_TOK": n(m(c0, "tokens")), "RN_TOK": n(m(cn, "tokens")), "FLAT10": f"{m(b['flat_top10'], 'complete'):.1f}",
    "FLAT10_TOK": n(m(b["flat_top10"], "tokens")), "FLAT20": f"{m(b['flat_top20'], 'complete'):.1f}",
    "FLAT20_TOK": n(m(b["flat_top20"], "tokens")), "NO_TRAV": f"{m(f['no_traversal'], 'complete'):.1f}",
    "TRAV_LOSS": f"{m(cn, 'complete') - m(f['no_traversal'], 'complete'):.1f}",
    "TRAV_LOSS_MULTI": f"{m(cn, 'complete_multi') - m(f['no_traversal'], 'complete_multi'):.1f}",
    "SMALLER": f"{100 - 100 * m(cn, 'tokens') / m(b['preload_all'], 'tokens'):.1f}",
    "SAVED": n(m(b["preload_all"], "tokens") - m(cn, "tokens")),
    "CALLS": f"{m(cn, 'calls'):.2f}", "CALLS_NOENT": f"{m(f['no_entity_memory'], 'calls'):.2f}",
    "CURVE": " → ".join(f"{m(c, 'complete'):.0f}" for c in a["curve"]) + " (% after each batch)",
    "TABLE": table(a), "LOAD": load_md,
}
for src, dst in (("README.md", "README.md"), ("PITCH.md", "docs/PITCH.md"), ("LOOM.md", "docs/LOOM.md")):
    text = (ROOT / "docs/tmpl" / src).read_text()
    for k, val in v.items():
        text = text.replace("{{" + k + "}}", val)
    assert "{{" not in text, f"unfilled placeholder in {src}"
    (ROOT / dst).write_text(text)

# curve.svg
W, H, ml, mr, mt, mb = 760, 360, 52, 170, 20, 46
curve = a["curve"]
maxx = curve[-1]["seen"]
lo = 10 * int(min(m(c0, "complete") - c0["complete"]["sd"], m(b["flat_top10"], "complete")) // 10) - 10
X = lambda x: ml + x / maxx * (W - ml - mr)
Y = lambda y: mt + (1 - (y - lo) / (100 - lo)) * (H - mt - mb)
o = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" font-family="system-ui, sans-serif"><rect width="{W}" height="{H}" fill="#fff"/>']
for y in range(lo, 101, 10):
    o.append(f'<line x1="{ml}" x2="{W - mr}" y1="{Y(y):.1f}" y2="{Y(y):.1f}" stroke="#E1E8ED"/><text x="{ml - 8}" y="{Y(y) + 4:.1f}" text-anchor="end" font-size="12" fill="#5C7080">{y}%</text>')
for x in range(0, maxx + 1, 100):
    o.append(f'<text x="{X(x):.1f}" y="{H - mb + 18}" text-anchor="middle" font-size="12" fill="#5C7080">{x}</text>')
o.append(f'<text x="{(ml + W - mr) / 2}" y="{H - 8}" text-anchor="middle" font-size="13" fill="#5C7080">tasks of usage the graph has learned from</text>')
for k, label in (("flat_top10", "flat index, top 10"), ("flat_top20", "flat index, top 20")):
    y = Y(m(b[k], "complete"))
    o.append(f'<line x1="{ml}" x2="{W - mr}" y1="{y:.1f}" y2="{y:.1f}" stroke="#8FA1AE" stroke-dasharray="4 4"/><text x="{W - mr + 8}" y="{y + 4:.1f}" font-size="12.5" fill="#5C7080">{label}: {m(b[k], "complete"):.0f}%</text>')
up = [f'{X(c["seen"]):.1f},{Y(m(c, "complete") + c["complete"]["sd"]):.1f}' for c in curve]
dn = [f'{X(c["seen"]):.1f},{Y(m(c, "complete") - c["complete"]["sd"]):.1f}' for c in reversed(curve)]
o.append(f'<polygon points="{" ".join(up + dn)}" fill="#D4372C" opacity=".13"/>')
o.append('<polyline points="' + " ".join(f'{X(c["seen"]):.1f},{Y(m(c, "complete")):.1f}' for c in curve) + '" fill="none" stroke="#D4372C" stroke-width="2.6"/>')
for c in curve:
    o.append(f'<circle cx="{X(c["seen"]):.1f}" cy="{Y(m(c, "complete")):.1f}" r="3.6" fill="#D4372C"/>')
o.append(f'<text x="{X(maxx) + 9:.1f}" y="{Y(m(cn, "complete")) - 4:.1f}" font-size="13.5" font-weight="600" fill="#D4372C">Hydrant: {m(cn, "complete"):.0f}%</text>')
o.append(f'<text x="{X(maxx) + 9:.1f}" y="{Y(m(cn, "complete")) + 12:.1f}" font-size="11.5" fill="#D4372C">held-out, mean of {res["seeds"]} splits</text>')
o.append(f'<text x="{X(0) + 8:.1f}" y="{Y(m(c0, "complete")) + 18:.1f}" font-size="12.5" fill="#D4372C">{m(c0, "complete"):.0f}% before any usage</text>')
o.append(f'<text x="{ml}" y="{mt - 4}" font-size="12.5" fill="#10283A">Tasks whose context held every tool they needed</text></svg>')
(ROOT / "docs/curve.svg").write_text("\n".join(o))
print("rendered README.md, docs/PITCH.md, docs/LOOM.md, docs/curve.svg")
