from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any

import numpy as np
import pandas as pd

from breakout_lab import simulate_breakout


def reference_quality_params() -> dict[str, Any]:
    """Fixed reference profile based on the V4.8 diagnosis.

    The point of V4.9 is to test a handful of explicit hypotheses, not to search a large
    parameter grid again. These values therefore stay fixed except for the one change named
    by each hypothesis variant.
    """
    return {
        "min_breakout_score": 75.0,
        "min_trend_score": 65.0,
        "min_volume_ratio": 1.0,
        "min_momentum_score": 55.0,
        "rsi_min": 45.0,
        "rsi_max": 75.0,
        "min_return_24h": 1.0,
        "high_distance_min": -1.0,
        "high_distance_max": 1.5,
        "regime_policy": "no_bear",
        "stop_loss_pct": 3.0,
        "take_profit_pct": 8.0,
        "trailing_stop_pct": 4.0,
        "trailing_activation_pct": 8.0,
        "exit_breakout_score": 40.0,
        "exit_trend_score": 35.0,
        "max_holding_hours": 120,
        "cooldown_loss_hours": 72,
        "cooldown_after_exit_hours": 12,
        "confirmation_scans": 1,
        "min_edge_score": 80.0,
        "signal_exit_min_hold_hours": 12,
    }


def hypothesis_variants() -> list[dict[str, Any]]:
    base = reference_quality_params()
    variants: list[tuple[str, str, dict[str, Any]]] = [
        ("Referenz", "Unveränderte V4.8-Quality-Regeln", {}),
        ("RSI ≤ 72", "Prüft die Hypothese: späte/überhitzte Einstiege vermeiden", {"rsi_max": 72.0}),
        ("Breakout ≥ 85", "Prüft die Hypothese: nur deutlichere Ausbrüche handeln", {"min_breakout_score": 85.0}),
        ("2 Bestätigungen", "Prüft die Hypothese: Breakout muss zwei Scans in Folge bestehen", {"confirmation_scans": 2}),
        ("RSI ≤ 72 + Breakout ≥ 85", "Kombiniert die beiden auffälligsten Diagnose-Hypothesen", {"rsi_max": 72.0, "min_breakout_score": 85.0}),
        ("Volumen ≥ 2×", "Prüft die Hypothese: nur Breakouts mit kräftigem Volumenschub", {"min_volume_ratio": 2.0}),
    ]
    out = []
    for name, thesis, change in variants:
        p = dict(base)
        p.update(change)
        out.append({"name": name, "thesis": thesis, "params": p})
    return out


def _pf_cap(v: float) -> float:
    if math.isinf(float(v)):
        return 3.0
    return max(0.0, min(3.0, float(v)))


def _compound(returns: list[float]) -> float:
    if not returns:
        return 0.0
    return (float(np.prod(1.0 + np.asarray(returns, dtype=float) / 100.0)) - 1.0) * 100.0


def _dev_status(profitable: int, combined: float, worst: float, mean_pf: float, max_dd: float, trades: int) -> str:
    if profitable >= 4 and combined > 0 and worst > -2.0 and mean_pf >= 1.20 and max_dd <= 8.0 and trades >= 8:
        return "STARK"
    if profitable >= 3 and mean_pf >= 1.10 and max_dd <= 10.0 and trades >= 6:
        return "INTERESSANT"
    return "SCHWACH"


def run_hypothesis_test(
    snapshots: list[dict], *, start_capital: float, allocation_pct: float, fee_pct: float,
    max_positions: int, daily_loss_limit_pct: float, n_dev_blocks: int = 4,
    confirmation_fraction: float = 0.20, progress_cb=None,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any], dict[str, Any]]:
    """Compare fixed hypothesis variants without re-optimizing a large search space.

    Ranking uses only the older development blocks. The newest slice is calculated afterwards
    as a *historical confirmation slice*. It is intentionally not described as a pristine
    final holdout because earlier bot versions may already have exposed overlapping dates.
    """
    if len(snapshots) < 100:
        raise RuntimeError("Zu wenige Snapshots für den Hypothesentest.")

    split = max(60, min(len(snapshots) - 30, int(len(snapshots) * (1.0 - float(confirmation_fraction)))))
    dev = snapshots[:split]
    confirmation = snapshots[split:]
    idx_parts = [p for p in np.array_split(np.arange(len(dev)), int(n_dev_blocks)) if len(p) >= 10]
    blocks = [[dev[int(i)] for i in part] for part in idx_parts]
    if len(blocks) != int(n_dev_blocks):
        raise RuntimeError("Zu wenige Daten für vier Entwicklungsblöcke.")

    common = dict(
        start_capital=float(start_capital), allocation_pct=float(allocation_pct), fee_pct=float(fee_pct),
        max_positions=int(max_positions), daily_loss_limit_pct=float(daily_loss_limit_pct), collect_details=True,
    )

    variants = hypothesis_variants()
    rows: list[dict[str, Any]] = []
    detailed: dict[str, Any] = {}

    for vi, variant in enumerate(variants):
        block_results = [simulate_breakout(block, params=variant["params"], **common) for block in blocks]
        returns = [float(r.return_pct) for r in block_results]
        pfs = [_pf_cap(r.profit_factor) for r in block_results]
        dds = [float(r.max_drawdown_pct) for r in block_results]
        trades = int(sum(r.completed_trades for r in block_results))
        profitable = int(sum(x > 0 for x in returns))
        combined = _compound(returns)
        worst = float(min(returns))
        avg_ret = float(np.mean(returns))
        mean_pf = float(np.mean(pfs))
        max_dd = float(max(dds))
        std_ret = float(np.std(returns))

        # Predeclared ranking: consistency first, then downside, then return. Confirmation is excluded.
        score = (
            profitable * 20.0 + combined * 1.2 + worst * 1.5 + mean_pf * 3.0
            - max_dd * 0.8 - std_ret * 0.7 - max(0, 6 - trades) * 1.5
        )
        status = _dev_status(profitable, combined, worst, mean_pf, max_dd, trades)
        row = {
            "Variante": variant["name"], "Hypothese": variant["thesis"],
            "Status Entwicklung": status, "Positive Blöcke": f"{profitable}/{len(blocks)}",
            "Entwicklung kombiniert %": round(combined, 2), "Schlechtester Block %": round(worst, 2),
            "Ø PF": round(mean_pf, 2), "Max DD %": round(max_dd, 2), "Trades": trades,
            "Stabilitäts-Score": round(score, 2),
        }
        for bi, ret in enumerate(returns, start=1):
            row[f"Block {bi} %"] = round(ret, 2)
        rows.append(row)
        detailed[variant["name"]] = {"variant": variant, "block_results": block_results}
        if progress_cb:
            progress_cb((vi + 1) / len(variants) * 0.70)

    dev_df = pd.DataFrame(rows).sort_values(
        ["Stabilitäts-Score", "Entwicklung kombiniert %"], ascending=[False, False]
    ).reset_index(drop=True)
    dev_df.insert(0, "Rang", range(1, len(dev_df) + 1))

    # Only after development ranking: calculate confirmation for all variants, without reordering.
    confirm_rows = []
    for ci, row in dev_df.iterrows():
        name = str(row["Variante"])
        variant = detailed[name]["variant"]
        cr = simulate_breakout(confirmation, params=variant["params"], **common)
        detailed[name]["confirmation_result"] = cr
        confirm_rows.append({
            "Rang Entwicklung": int(row["Rang"]), "Variante": name,
            "Bestätigung %": round(float(cr.return_pct), 2),
            "Bestätigung DD %": round(float(cr.max_drawdown_pct), 2),
            "Bestätigung PF": round(_pf_cap(cr.profit_factor), 2),
            "Bestätigung Trades": int(cr.completed_trades),
            "Trefferquote %": round(float(cr.win_rate_pct), 1),
            "Gebühren €": round(float(cr.total_fees), 2),
        })
        if progress_cb:
            progress_cb(0.70 + (ci + 1) / len(dev_df) * 0.30)

    confirm_df = pd.DataFrame(confirm_rows)
    winner_name = str(dev_df.iloc[0]["Variante"])
    ref_row = dev_df[dev_df["Variante"] == "Referenz"].iloc[0]
    win_row = dev_df.iloc[0]
    ref_detail = detailed["Referenz"]
    win_detail = detailed[winner_name]

    # Identify the reference's worst development block and compare every hypothesis on that same block.
    ref_returns = [float(r.return_pct) for r in ref_detail["block_results"]]
    ref_worst_idx = int(np.argmin(ref_returns))
    rescue_rows = []
    for _, row in dev_df.iterrows():
        name = str(row["Variante"])
        same_block_ret = float(detailed[name]["block_results"][ref_worst_idx].return_pct)
        rescue_rows.append({
            "Variante": name,
            f"Referenz-Problemblock {ref_worst_idx+1} %": round(same_block_ret, 2),
            "Verbesserung ggü. Referenz %-Pkt": round(same_block_ret - ref_returns[ref_worst_idx], 2),
        })
    rescue_df = pd.DataFrame(rescue_rows).sort_values("Verbesserung ggü. Referenz %-Pkt", ascending=False)

    winner_confirm = win_detail["confirmation_result"]
    overall = "SCHWACH"
    if str(win_row["Status Entwicklung"]) == "STARK" and winner_confirm.return_pct > 0 and _pf_cap(winner_confirm.profit_factor) >= 1.2:
        overall = "PAPER-KANDIDAT"
    elif str(win_row["Status Entwicklung"]) in {"STARK", "INTERESSANT"} and winner_confirm.return_pct > -2:
        overall = "WEITER TESTEN"

    summary = {
        "winner": winner_name,
        "winner_status": str(win_row["Status Entwicklung"]),
        "overall": overall,
        "positive_blocks": str(win_row["Positive Blöcke"]),
        "dev_combined_pct": float(win_row["Entwicklung kombiniert %"]),
        "dev_worst_pct": float(win_row["Schlechtester Block %"]),
        "dev_pf": float(win_row["Ø PF"]),
        "dev_max_dd_pct": float(win_row["Max DD %"]),
        "confirmation_pct": float(winner_confirm.return_pct),
        "confirmation_pf": _pf_cap(winner_confirm.profit_factor),
        "confirmation_dd_pct": float(winner_confirm.max_drawdown_pct),
        "confirmation_trades": int(winner_confirm.completed_trades),
        "reference_worst_block": ref_worst_idx + 1,
        "reference_worst_return_pct": ref_returns[ref_worst_idx],
        "winner_params": dict(win_detail["variant"]["params"]),
        "winner_thesis": str(win_detail["variant"]["thesis"]),
        "dev_from": pd.Timestamp(dev[0]["timestamp"]),
        "dev_to": pd.Timestamp(dev[-1]["timestamp"]),
        "confirmation_from": pd.Timestamp(confirmation[0]["timestamp"]),
        "confirmation_to": pd.Timestamp(confirmation[-1]["timestamp"]),
        "note": "Der Bestätigungsabschnitt wurde im V4.9-Lauf nicht zum Ranking benutzt, kann aber zeitlich mit bereits angesehenen V4.7/V4.8-Daten überlappen und ist deshalb kein vollständig neuer Final-Holdout.",
    }
    extra = {"rescue": rescue_df, "detailed": detailed}
    return dev_df, confirm_df, summary, extra
