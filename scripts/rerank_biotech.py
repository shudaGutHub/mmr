"""Apply AV industry map + user must-drop, rewrite screen JSON from existing Alpaca ranks."""
from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

SRC = Path(r"c:\Users\salee\BITBUCKET\mmr\data\symbol_agent_factory_screen.json")
OUTS = [
    SRC,
    Path(r"C:\Users\salee\.cursor\projects\c-Users-salee-BITBUCKET-mmr\canvases\symbol-agent-factory.screen.json"),
]
ET = ZoneInfo("America/New_York")

# Alpha Vantage COMPANY_OVERVIEW (MCP, 2026-09-08) + user must-drop list
AV = {
    "AMGN": {"name": "Amgen Inc", "sector": "HEALTHCARE", "industry": "DRUG MANUFACTURERS - GENERAL", "drop": "user+description biopharmaceutical"},
    "ALNY": {"name": "Alnylam Pharmaceuticals Inc", "sector": "HEALTHCARE", "industry": "BIOTECHNOLOGY", "drop": "AV Industry BIOTECHNOLOGY"},
    "GILD": {"name": "Gilead Sciences Inc", "sector": "HEALTHCARE", "industry": "DRUG MANUFACTURERS - GENERAL", "drop": "user+description biopharmaceutical"},
    "VRTX": {"name": "Vertex Pharmaceuticals Inc", "sector": "HEALTHCARE", "industry": "BIOTECHNOLOGY", "drop": "AV Industry BIOTECHNOLOGY"},
    "REGN": {"name": "Regeneron Pharmaceuticals Inc", "sector": "HEALTHCARE", "industry": "BIOTECHNOLOGY", "drop": "AV Industry BIOTECHNOLOGY"},
    "BIIB": {"name": "Biogen Inc", "sector": "HEALTHCARE", "industry": "DRUG MANUFACTURERS - GENERAL", "drop": "user+description biotechnology company"},
    "NTRA": {"name": "Natera Inc", "sector": "HEALTHCARE", "industry": "DIAGNOSTICS & RESEARCH", "drop": None},
    "GH": {"name": "Guardant Health Inc", "sector": "HEALTHCARE", "industry": "DIAGNOSTICS & RESEARCH", "drop": None},
    "TWST": {"name": "Twist Bioscience Corp", "sector": "HEALTHCARE", "industry": "BIOTECHNOLOGY", "drop": "AV Industry BIOTECHNOLOGY"},
    "BLTE": {"name": "Belite Bio Inc ADR", "sector": "HEALTHCARE", "industry": "BIOTECHNOLOGY", "drop": "AV Industry BIOTECHNOLOGY"},
    "ILMN": {"name": "Illumina Inc", "sector": "HEALTHCARE", "industry": "DIAGNOSTICS & RESEARCH", "drop": None},
    "ISRG": {"name": "Intuitive Surgical Inc", "sector": "HEALTHCARE", "industry": "MEDICAL INSTRUMENTS & SUPPLIES", "drop": None},
    "PODD": {"name": "Insulet Corporation", "sector": "HEALTHCARE", "industry": "MEDICAL DEVICES", "drop": None},
    "ARGX": {"name": "argenx NV ADR", "sector": "HEALTHCARE", "industry": "BIOTECHNOLOGY", "drop": "AV Industry BIOTECHNOLOGY"},
    "INSM": {"name": "Insmed Inc", "sector": "HEALTHCARE", "industry": "BIOTECHNOLOGY", "drop": "AV Industry BIOTECHNOLOGY"},
    "RVMD": {"name": "Revolution Medicines Inc", "sector": "HEALTHCARE", "industry": "BIOTECHNOLOGY", "drop": "AV Industry BIOTECHNOLOGY"},
    "ONC": {"name": "BeiGene, Ltd.", "sector": "HEALTHCARE", "industry": "BIOTECHNOLOGY", "drop": "AV Industry BIOTECHNOLOGY"},
}

MUST_DROP = {"AMGN", "GILD", "VRTX", "REGN", "ALNY", "BIIB"}


def percentile_ranks(values: list[float]) -> list[float]:
    n = len(values)
    order = sorted(range(n), key=lambda i: values[i])
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2.0
        pct = (avg + 1.0) / n
        for k in range(i, j + 1):
            ranks[order[k]] = pct
        i = j + 1
    return ranks


def mix_family(row: dict) -> str:
    gap = abs(row.get("gap_pct") or 0)
    atr = row.get("atr_pct") or 0
    sess = row.get("session_tr_pct") or 0
    if gap >= 0.02:
        return "fade"
    if sess >= 0.04 and atr >= 0.003:
        return "breakout"
    if sess < 0.015:
        return "range"
    return "trend"


def main() -> None:
    payload = json.loads(SRC.read_text(encoding="utf-8"))
    survivors = payload["survivors"]
    existing_bio = {r["symbol"]: r for r in payload.get("biotech_dropped", [])}

    kept, dropped = [], []
    for row in survivors:
        sym = row["symbol"]
        av = AV.get(sym, {})
        if av:
            row["name"] = av.get("name") or row.get("name")
            row["industry"] = av.get("industry") or row.get("industry")
            row["sector"] = av.get("sector") or row.get("sector")
            row["industry_source"] = "alphavantage_overview"
        reason = None
        if av.get("drop"):
            reason = av["drop"]
        elif sym in MUST_DROP:
            reason = "user must-drop biotech"
        elif "BIOTECH" in str(row.get("industry") or "").upper():
            reason = "AV Industry BIOTECHNOLOGY"
        if reason:
            row["excluded_reason"] = reason
            dropped.append(row)
        else:
            row["mix_family"] = mix_family(row)
            kept.append(row)

    # Re-score among remaining survivors only (full-universe re-rank needs
    # bars for below-cut names; those stay queued as replacement candidates).
    atrs = [r["atr_pct"] for r in kept]
    dvols = [r["dollar_volume"] for r in kept]
    for rec, ar, dr in zip(kept, percentile_ranks(atrs), percentile_ranks(dvols)):
        rec["atr_pct_rank"] = ar
        rec["dollar_volume_rank"] = dr
        rec["combo_score"] = 0.5 * ar + 0.5 * dr
    kept.sort(key=lambda r: r["combo_score"], reverse=True)

    for r in payload.get("biotech_dropped", []):
        if r["symbol"] not in {d["symbol"] for d in dropped}:
            dropped.append(r)

    payload["survivors"] = kept
    payload["biotech_dropped"] = [
        {
            "symbol": r["symbol"],
            "name": r.get("name"),
            "industry": r.get("industry"),
            "sector": r.get("sector"),
            "sic": r.get("sic"),
            "excluded_reason": r.get("excluded_reason"),
        }
        for r in sorted(dropped, key=lambda x: x["symbol"])
    ]
    payload["screen"]["biotech_dropped"] = len(dropped)
    payload["screen"]["survivors"] = len(kept)
    payload["screen"]["industry_source"] = (
        "Alpha Vantage COMPANY_OVERVIEW via MCP for healthcare names; "
        "user must-drop VRTX/REGN/AMGN/GILD/ALNY/BIIB; devices/diagnostics kept"
    )
    payload["screen"]["note"] = (
        f"Re-filtered the Alpaca IEX top-quartile after AV industry. "
        f"Dropped {len(dropped)} biotech from the prior 78-name cut "
        f"({', '.join(sorted(r['symbol'] for r in dropped))}). "
        f"Replacement names from below the original cut are not re-scored here "
        f"(full-universe bars were not persisted). Agent count is the cleaned cut."
    )
    payload["counts"]["after_biotech_drop"] = payload["screen"].get("ranked_pool", 311) - (len(dropped) - 2)
    payload["counts"]["top_25_agents"] = len(kept)
    payload["as_of"] = datetime.now(tz=ET).isoformat(timespec="seconds")

    text = json.dumps(payload, indent=2)
    for p in OUTS:
        p.write_text(text, encoding="utf-8")
    print("agents", len(kept))
    print("dropped", " ".join(sorted(r["symbol"] for r in dropped)))
    print("survivors", " ".join(r["symbol"] for r in kept))


if __name__ == "__main__":
    main()
