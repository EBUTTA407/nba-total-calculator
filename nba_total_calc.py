
#!/usr/bin/env python3
from __future__ import annotations
from dataclasses import dataclass, asdict
from datetime import datetime
import csv
import math
import os

# ---------- helpers ----------
def parse_mmss(s: str) -> float:
    """Return minutes as float from 'mm:ss'."""
    parts = s.strip().split(":")
    if len(parts) != 2:
        raise ValueError("Time must be mm:ss (e.g., 7:10).")
    mm = int(parts[0])
    ss = int(parts[1])
    if mm < 0 or ss < 0 or ss >= 60:
        raise ValueError("Invalid mm:ss.")
    return mm + ss / 60.0

def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))

def auto_alpha(elapsed_min: float) -> float:
    # cautious early, trusts live more later
    return clamp(0.35 + 0.012 * elapsed_min, 0.35, 0.90)

def dec_from_american(a: float) -> float:
    if a == 0:
        raise ValueError("American odds cannot be 0.")
    if a < 0:
        return 1.0 + (100.0 / abs(a))
    return 1.0 + (a / 100.0)

def fmt(x: float, d: int = 1) -> str:
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
        return "—"
    return f"{x:.{d}f}"

# ---------- core ----------
@dataclass
class CalcResult:
    timestamp: str
    quarter: int
    time_left: str
    total_points: float
    live_total: float
    pregame_total: float
    elapsed_min: float
    alpha_used: float
    pace_ppm: float
    pace_proj: float
    blended_proj: float
    edge_vs_live: float
    needed_ppm_for_live: float
    lean: str
    flags: str  # semicolon-separated

def compute_projection(
    quarter: int,
    time_left_mmss: str,
    total_points: float,
    live_total: float,
    pregame_total: float,
    alpha: float | None = None,
    edge_threshold: float = 4.0,
    bonus: bool = False,
    ft_parade: bool = False,
    bonus_boost_ppm: float = 0.25,
    fta_total: float | None = None,
    three_pt_pct: float | None = None,
    ot_on: bool = False,
    ot_prob_pct: float = 6.0,
    ot_expected_points: float = 10.0,
) -> CalcResult:
    # elapsed minutes
    tleft = parse_mmss(time_left_mmss)
    elapsed = (quarter - 1) * 12.0 + (12.0 - tleft)
    if elapsed <= 0 or elapsed > 48:
        raise ValueError("Elapsed time out of bounds. Check quarter/time left.")

    alpha_used = auto_alpha(elapsed) if alpha is None else clamp(alpha, 0.05, 0.95)

    pace_ppm = total_points / elapsed
    pace_proj = pace_ppm * 48.0

    baseline_rate = pregame_total / 48.0

    boost = 0.0
    if bonus:
        boost += bonus_boost_ppm
    if ft_parade:
        boost += 0.35

    blended_rate = alpha_used * (pace_ppm + boost) + (1 - alpha_used) * baseline_rate
    blended_proj = total_points + blended_rate * (48.0 - elapsed)

    if ot_on:
        ot_adj = (ot_prob_pct / 100.0) * ot_expected_points
        blended_proj += ot_adj

    needed_ppm = (live_total - total_points) / (48.0 - elapsed)
    edge = blended_proj - live_total

    # flags
    flags = []
    if bonus and quarter <= 3 and tleft > 6:
        flags.append("EARLY_BONUS_HIGH_RISK")
    elif bonus:
        flags.append("BONUS/WHISTLES_ON")
    if ft_parade:
        flags.append("FT_PARADE")

    if fta_total is not None:
        fta_per_min = fta_total / elapsed
        if fta_per_min >= 1.10:
            flags.append(f"HIGH_FT_RATE({fta_per_min:.2f}/min)")
        elif fta_per_min >= 0.85:
            flags.append(f"ELEVATED_FT_RATE({fta_per_min:.2f}/min)")
        else:
            flags.append(f"FT_RATE_OK({fta_per_min:.2f}/min)")

    if three_pt_pct is not None:
        if three_pt_pct >= 0.42:
            flags.append(f"3P_HOT({three_pt_pct*100:.1f}%)")
        elif three_pt_pct <= 0.31:
            flags.append(f"3P_COLD({three_pt_pct*100:.1f}%)")
        else:
            flags.append(f"3P_NORMAL({three_pt_pct*100:.1f}%)")

    if ot_on:
        flags.append(f"OT_ON({ot_prob_pct:.1f}% -> +{(ot_prob_pct/100)*ot_expected_points:.1f} pts)")

    # lean
    lean = "PASS / WAIT"
    if edge >= edge_threshold:
        lean = "LEAN: OVER"
    elif edge <= -edge_threshold:
        lean = "LEAN: UNDER"

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    return CalcResult(
        timestamp=ts,
        quarter=quarter,
        time_left=time_left_mmss,
        total_points=total_points,
        live_total=live_total,
        pregame_total=pregame_total,
        elapsed_min=elapsed,
        alpha_used=alpha_used,
        pace_ppm=pace_ppm,
        pace_proj=pace_proj,
        blended_proj=blended_proj,
        edge_vs_live=edge,
        needed_ppm_for_live=needed_ppm,
        lean=lean,
        flags=";".join(flags) if flags else "NONE",
    )

# ---------- hedge ----------
@dataclass
class HedgeResult:
    suggestion: str
    equalized_hedge_stake: float
    middle_note: str
    worst_case_profit: float
    best_case_profit: float

def hedge_equalize(
    my_side: str,           # "UNDER" or "OVER"
    my_line: float,
    my_stake: float,
    my_odds_american: float,
    hedge_line: float,
    hedge_odds_american: float,
    live_total: float,
    flags: list[str],
) -> HedgeResult:
    my_side = my_side.upper().strip()
    if my_side not in ("UNDER", "OVER"):
        raise ValueError("my_side must be UNDER or OVER.")

    dec0 = dec_from_american(my_odds_american)
    dech = dec_from_american(hedge_odds_american)

    # equalize payout sizes (simple variance reducer)
    hedge_stake = my_stake * dec0 / dech

    # CLV heuristic
    clv = (my_line - live_total) if my_side == "UNDER" else (live_total - my_line)
    bad = sum("HIGH_RISK" in f or "FT_PARADE" in f or "HIGH_FT_RATE" in f for f in flags)
    warn = sum(("BONUS" in f or "ELEVATED_FT_RATE" in f or "3P_" in f or "OT_ON" in f) for f in flags)

    if clv >= 6 and (bad >= 1 or warn >= 2):
        suggestion = "Consider SMALL hedge"
    elif clv >= 6:
        suggestion = "Hold (good CLV)"
    elif clv >= 3 and bad >= 1:
        suggestion = "Watch closely (whistle risk)"
    else:
        suggestion = "No hedge signal"

    # middle detection
    if my_side == "UNDER":
        if hedge_line < my_line:
            middle_note = f"Middle range (both win): {hedge_line:.1f} to {my_line:.1f} (excluding pushes)."
        else:
            middle_note = "No classic middle (hedge line not below your under line)."
    else:
        if hedge_line > my_line:
            middle_note = f"Middle range (both win): {my_line:.1f} to {hedge_line:.1f} (excluding pushes)."
        else:
            middle_note = "No classic middle (hedge line not above your over line)."

    win0 = my_stake * (dec0 - 1)
    lose0 = -my_stake
    winh = hedge_stake * (dech - 1)
    loseh = -hedge_stake

    worst = min(win0 + loseh, lose0 + winh)
    best = win0 + winh

    return HedgeResult(
        suggestion=suggestion,
        equalized_hedge_stake=hedge_stake,
        middle_note=middle_note,
        worst_case_profit=worst,
        best_case_profit=best,
    )

# ---------- CSV snapshots ----------
CSV_FILE = "nba_total_snapshots.csv"

def save_snapshot(result: CalcResult) -> None:
    new_file = not os.path.exists(CSV_FILE)
    with open(CSV_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(asdict(result).keys()))
        if new_file:
            w.writeheader()
        w.writerow(asdict(result))

def print_result(r: CalcResult) -> None:
    print("\n=== NBA LIVE TOTAL READ ===")
    print(f"Time: {r.timestamp}")
    print(f"Q{r.quarter}  {r.time_left} left | Total pts: {r.total_points:.0f}")
    print(f"Live total: {fmt(r.live_total,1)} | Pregame: {fmt(r.pregame_total,1)}")
    print(f"Elapsed: {fmt(r.elapsed_min,2)} min | Alpha: {fmt(r.alpha_used,2)}")
    print(f"Pace: {fmt(r.pace_ppm,2)} pts/min | Needed for live: {fmt(r.needed_ppm_for_live,2)} pts/min")
    print(f"Pace proj: {fmt(r.pace_proj,1)} | Blended proj: {fmt(r.blended_proj,1)}")
    print(f"Edge vs live: {fmt(r.edge_vs_live,1)}  =>  {r.lean}")
    print(f"Flags: {r.flags}")

# ---------- CLI loop ----------
def main():
    print("NBA Live Total Calculator (Terminal)")
    print("Type 'q' at any prompt to quit. Snapshots save to nba_total_snapshots.csv\n")

    pregame = 228.5
    thr = 4.0

    while True:
        try:
            s = input("Quarter (1-4): ").strip()
            if s.lower() == "q":
                break
            quarter = int(s)

            s = input("Time left (mm:ss): ").strip()
            if s.lower() == "q":
                break
            tleft = s

            s = input("Total points (both teams): ").strip()
            if s.lower() == "q":
                break
            pts = float(s)

            s = input("Live total (market): ").strip()
            if s.lower() == "q":
                break
            live = float(s)

            s = input(f"Pregame total [{pregame}]: ").strip()
            if s.lower() == "q":
                break
            if s != "":
                pregame = float(s)

            s = input(f"Edge threshold pts [{thr}]: ").strip()
            if s.lower() == "q":
                break
            if s != "":
                thr = float(s)

            s = input("Alpha (blank=auto): ").strip()
            if s.lower() == "q":
                break
            alpha = None if s == "" else float(s)

            bonus = input("Bonus/whistles on? (y/n): ").strip().lower() == "y"
            ftparade = input("FT parade? (y/n): ").strip().lower() == "y"

            s = input("FTA total so far (optional blank): ").strip()
            if s.lower() == "q":
                break
            fta = None if s == "" else float(s)

            s = input("3P% so far (optional blank, e.g. 0.41): ").strip()
            if s.lower() == "q":
                break
            tpct = None if s == "" else float(s)

            ot_on = input("Include OT adjustment? (y/n): ").strip().lower() == "y"
            otp = 6.0
            otpts = 10.0
            if ot_on:
                s = input("OT probability % [6]: ").strip()
                if s.lower() == "q":
                    break
                if s != "":
                    otp = float(s)

                s = input("Expected OT points [10]: ").strip()
                if s.lower() == "q":
                    break
                if s != "":
                    otpts = float(s)

            r = compute_projection(
                quarter=quarter,
                time_left_mmss=tleft,
                total_points=pts,
                live_total=live,
                pregame_total=pregame,
                alpha=alpha,
                edge_threshold=thr,
                bonus=bonus,
                ft_parade=ftparade,
                fta_total=fta,
                three_pt_pct=tpct,
                ot_on=ot_on,
                ot_prob_pct=otp,
                ot_expected_points=otpts,
            )
            print_result(r)

            if input("\nRun hedge calc? (y/n): ").strip().lower() == "y":
                my_side = input("My bet side (UNDER/OVER): ").strip().upper()
                my_line = float(input("My bet line: ").strip())
                my_stake = float(input("My stake $: ").strip())
                my_odds = float(input("My odds (American, e.g. -110): ").strip())

                s = input(f"Hedge line (blank=use live {live}): ").strip()
                hedge_line = live if s == "" else float(s)
                hedge_odds = float(input("Hedge odds (American): ").strip())

                flags_list = [] if r.flags == "NONE" else r.flags.split(";")
                h = hedge_equalize(
                    my_side=my_side,
                    my_line=my_line,
                    my_stake=my_stake,
                    my_odds_american=my_odds,
                    hedge_line=hedge_line,
                    hedge_odds_american=hedge_odds,
                    live_total=live,
                    flags=flags_list,
                )
                print("\n--- HEDGE OUTPUT ---")
                print(f"Suggestion: {h.suggestion}")
                print(f"Equalized hedge stake: ${fmt(h.equalized_hedge_stake,2)}")
                print(h.middle_note)
                print(f"Worst-case profit (approx): ${fmt(h.worst_case_profit,2)}")
                print(f"Best-case (middle hits): ${fmt(h.best_case_profit,2)}")

            if input("\nSave snapshot to CSV? (y/n): ").strip().lower() == "y":
                save_snapshot(r)
                print("Saved -> nba_total_snapshots.csv")

            print("\n--------------------------------\n")

        except Exception as e:
            print(f"\nError: {e}\nTry again.\n")

if __name__ == "__main__":
    main()


