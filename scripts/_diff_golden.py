"""Diff two golden-eval JSON reports case by case and emit a Markdown table."""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
A = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE.parent / "tmp_golden_hash.json"
B = Path(sys.argv[2]) if len(sys.argv) > 2 else HERE.parent / "tmp_golden_provider.json"
OUT = Path(sys.argv[3]) if len(sys.argv) > 3 else HERE.parent / "tmp_golden_diff.md"


def load(path: Path) -> dict[str, dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return {(r["repo"], c["case_key"]): c for r in data for c in r["per_case"]}


a, b = load(A), load(B)
rows = ["| 语料 | 用例 | hash rank | hash cite | qwen rank | qwen cite | 变化 |",
        "| --- | --- | --- | --- | --- | --- | --- |"]
changed = 0
for key in a:
    if key not in b:
        continue
    x, y = a[key], b[key]
    rx, ry = x["rank"], y["rank"]
    cx, cy = x["citation_ok"], y["citation_ok"]
    tags = []
    if (rx is None) != (ry is None):
        tags.append("召回 " + ("命中→丢失" if ry is None else "丢失→命中"))
    if cx != cy:
        tags.append("引用 " + ("✓→✗" if cy is False else "✗→✓"))
    if rx != ry and rx is not None and ry is not None:
        tags.append(f"位次 {rx}→{ry}")
    if tags:
        changed += 1
    rows.append(f"| {key[0]} | {key[1]} | {rx if rx else '-'} | "
                f"{'✓' if cx else '✗'} | {ry if ry else '-'} | {'✓' if cy else '✗'} | "
                f"{'；'.join(tags) or '—'} |")


def agg(source: dict) -> tuple[int, int, int]:
    tot = len(source)
    rec = sum(1 for v in source.values() if v["retrieved"])
    cit = sum(1 for v in source.values() if v["citation_ok"])
    return tot, rec, cit


lines = list(rows)
lines.append("")
lines.append(f"共 {len(a)} 题，发生变化 {changed} 题。")
for name, src in (("hash", a), ("qwen", b)):
    tot, rec, cit = agg(src)
    lines.append(f"- {name}: recall@5 = {rec}/{tot} = {rec/tot:.4f}；citation = {cit}/{tot} = {cit/tot:.4f}")
OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
print(f"written {OUT}")
