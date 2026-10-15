"""Compare an exporter's cash receipts when the order size changes."""

import bisect
import calendar
import csv
import math
from datetime import date, datetime
from pathlib import Path
from statistics import mean

ROOT = Path(__file__).resolve().parent
EXPECTED_USD = 100_000
HEDGES = (0, 0.5, 1)
ORDERS = {"full": 1, "half": 0.5, "cancelled": 0}


def settle(actual_usd, hedge_usd, forward, spot, cost_bps=10):
    values = (actual_usd, hedge_usd, forward, spot, cost_bps)
    if not all(math.isfinite(x) for x in values):
        raise ValueError("Inputs must be finite")
    if min(actual_usd, hedge_usd, cost_bps) < 0 or min(forward, spot) <= 0:
        raise ValueError("Amounts and fees must be non-negative; rates positive")
    forward_gbp = hedge_usd / forward
    # A negative spot leg means buying missing dollars to fulfil the contract.
    spot_gbp = (actual_usd - hedge_usd) / spot
    cost_gbp = cost_bps / 10_000 * (abs(forward_gbp) + abs(spot_gbp))
    return {
        "forward_gbp": forward_gbp,
        "spot_gbp": spot_gbp,
        "cost_gbp": cost_gbp,
        "cash_gbp": forward_gbp + spot_gbp - cost_gbp,
    }


def stress_summary(forward, cost_bps=10, shock=0.10):
    if not math.isfinite(shock) or not 0 <= shock < 1:
        raise ValueError("Shock must be finite, at least zero and below one")
    summary = []
    for floor in (EXPECTED_USD, EXPECTED_USD / 2, 0):
        receipts = [EXPECTED_USD * x for x in ORDERS.values()
                    if EXPECTED_USD * x >= floor]
        for hedge in HEDGES:
            dollars = EXPECTED_USD * hedge
            cases = []
            for actual in receipts:
                for spot in (forward * (1 - shock), forward, forward * (1 + shock)):
                    cash = settle(actual, dollars, forward, spot, cost_bps)["cash_gbp"]
                    cases.append((cash, actual, spot))
            # These are assumed future rates; no realised spot rate enters this table.
            cash, actual, spot = min(cases, key=lambda case: case[0])
            summary.append({"receipt_floor_usd": floor, "hedge_usd": dollars,
                            "min_cash_gbp": cash, "worst_receipt_usd": actual,
                            "worst_spot": spot, "max_topup_usd": max(0, dollars - floor)})
    return summary


def add_months(day, months):
    year, month = divmod(day.year * 12 + day.month - 1 + months, 12)
    month += 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def settlement_date(target, dates, shift=0):
    index = bisect.bisect_left(dates, target)
    if index == len(dates) or not 0 <= index + shift < len(dates):
        raise ValueError("Settlement date is outside the available data")
    return dates[index + shift]


def load_rates(path=ROOT / "rates.csv"):
    lines = Path(path).read_text(encoding="utf-8-sig").splitlines()
    header = next(i for i, line in enumerate(lines) if line.startswith("DATE,"))
    rates = []
    for row in csv.DictReader(lines[header:]):
        spot = float(row["XUDLUSS"])
        forward = float(row["XUDLDS3"]) if row["XUDLDS3"] else None
        if not math.isfinite(spot) or spot <= 0:
            raise ValueError("Invalid spot rate")
        if forward is not None and (not math.isfinite(forward) or forward <= 0):
            raise ValueError("Invalid forward rate")
        rates.append({"date": datetime.strptime(row["DATE"], "%d %b %Y").date(),
                      "spot": spot, "forward": forward})
    dates = [r["date"] for r in rates]
    if dates != sorted(set(dates)):
        raise ValueError("Dates must be unique and increasing")
    return rates


def make_cases(rates):
    cases = []
    for offset in range(42):
        month = add_months(date(2018, 1, 1), offset)
        candidates = [r for r in rates if r["date"].year == month.year
                      and r["date"].month == month.month and r["forward"] is not None]
        if not candidates:
            raise ValueError(f"Missing starting rates for {month:%Y-%m}")
        first = candidates[0]
        start = first["date"]
        cases.append({"start": start, "target": add_months(start, 3),
                      "forward": first["forward"],
                      "split": "development" if start.year < 2020 else "check"})
    return cases


def run(rates):
    dates = [r["date"] for r in rates]
    spots = {r["date"]: r["spot"] for r in rates}
    results = []
    for case in make_cases(rates):
        for shift in (-2, -1, 0, 1, 2):
            maturity = settlement_date(case["target"], dates, shift)
            spot = spots[maturity]
            for cost_bps in (0, 10, 50):
                for order, fraction in ORDERS.items():
                    actual = EXPECTED_USD * fraction
                    unhedged = settle(actual, 0, case["forward"], spot, cost_bps)
                    # Rebase the cash plan to the actual order, not the expected order.
                    plan = actual / case["forward"] * (1 - cost_bps / 10_000)
                    for hedge in HEDGES:
                        dollars = EXPECTED_USD * hedge
                        cash = settle(actual, dollars, case["forward"], spot, cost_bps)
                        results.append({
                            **case, "maturity": maturity, "date_shift": shift,
                            "spot": spot, "cost_bps": cost_bps, "order": order,
                            "actual_usd": actual, "hedge_ratio": hedge,
                            "hedge_usd": dollars, **cash, "plan_gbp": plan,
                            "gap_gbp": cash["cash_gbp"] - plan,
                            "hedge_effect_gbp": cash["cash_gbp"] - unhedged["cash_gbp"],
                        })
    return results


def save_results(rows):
    with (ROOT / "results.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=rows[0].keys())
        writer.writeheader()
        for row in rows:
            writer.writerow({k: round(v, 6) if isinstance(v, float) else v
                             for k, v in row.items()})


def show_summary(rows):
    for split in ("development", "check"):
        print(f"\n{split.upper()}: 10 bp assumed cost; unshifted date")
        print("Order       Hedge   Mean gap GBP   Min gap GBP   Max gap GBP")
        for order in ORDERS:
            for hedge in HEDGES:
                gaps = [r["gap_gbp"] for r in rows if r["split"] == split
                        and r["cost_bps"] == 10 and r["date_shift"] == 0
                        and r["order"] == order and r["hedge_ratio"] == hedge]
                print(f"{order:11} {hedge:5.0%} {mean(gaps):14,.0f}"
                      f" {min(gaps):13,.0f} {max(gaps):13,.0f}")


def show_stress(start, forward, cost_bps=10, shock=0.10):
    print(f"\nBEFORE SIGNING: {start}, forward {forward:.4f} USD/GBP")
    print(f"Assumed spot: forward -{shock:.0%}, unchanged, +{shock:.0%}; cost {cost_bps} bp")
    print("Receipt floor USD   Hedge USD   Lowest cash GBP   Most USD to buy")
    for row in stress_summary(forward, cost_bps, shock):
        print(f"{row['receipt_floor_usd']:17,.0f} {row['hedge_usd']:11,.0f}"
              f" {row['min_cash_gbp']:17,.0f} {row['max_topup_usd']:17,.0f}")
    print("Scenario minima, not guaranteed cash floors. Orders and future rates are assumed.")


def plot_results(rows, first):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.ticker import StrMethodFormatter

    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 11, "text.parse_math": False,
                         "text.color": "#263442", "axes.labelcolor": "#263442",
                         "xtick.color": "#52606d", "ytick.color": "#52606d",
                         "axes.edgecolor": "#c6cdd3", "axes.spines.top": False,
                         "axes.spines.right": False, "axes.axisbelow": True})
    colors = ("#7b858f", "#147d83", "#253f59")
    labels = ("No hedge", "Sell $50k forward", "Sell $100k forward")
    stress = stress_summary(first["forward"])
    fig, axes = plt.subplots(1, 3, figsize=(12, 5.8), sharex=True, sharey=True)
    titles = ("Full order only\nReceipt: $100k",
              "Order may halve\nReceipt: $50k or $100k",
              "Order may cancel\nReceipt: $0, $50k or $100k")
    for ax, floor, title in zip(axes, (EXPECTED_USD, EXPECTED_USD / 2, 0), titles):
        selected = [r for r in stress if r["receipt_floor_usd"] == floor]
        for i, row in enumerate(selected):
            cash = row["min_cash_gbp"]
            ax.barh(i, cash, color=colors[i], height=0.48)
            ax.annotate(f"£{cash:,.0f}" if cash >= 0 else f"−£{abs(cash):,.0f}",
                        (cash, i), xytext=(5 if cash >= 0 else -5, 0),
                        textcoords="offset points", va="center",
                        ha="left" if cash >= 0 else "right", fontsize=10)
        ax.axvline(0, color="#52606d", linewidth=0.8)
        ax.set_title(title, fontsize=11, loc="left", pad=17, linespacing=1.6)
        ax.set_xlim(-30_000, 100_000)
        ax.set_xticks((0, 40_000, 80_000), ("0", "40k", "80k"))
        ax.grid(axis="x", color="#e9edf0", linewidth=0.7)
        ax.set_yticks(range(3), labels)
        ax.tick_params(axis="y", length=0, pad=9)
        ax.spines["left"].set_visible(False)
    axes[0].invert_yaxis()
    fig.suptitle("How much to hedge when the order size is uncertain?",
                 x=0.04, y=0.96, ha="left", fontsize=17, fontweight="bold")
    fig.text(0.04, 0.885, "Lowest GBP cash in each set of assumed receipt and exchange-rate scenarios",
             fontsize=11, color="#52606d")
    fig.text(0.57, 0.205, "Cash after currency settlement (£)", ha="center", fontsize=10)
    fig.text(0.04, 0.065,
             f"Bank of England forward: {first['forward']:.4f} USD/GBP, {first['start']:%d %b %Y}. "
             "Assumed future spot: forward −10%, unchanged or +10%.\n"
             "Assumed fees: 0.1% on each currency leg. Scenario minima, not guaranteed cash floors. "
             "Orders are hypothetical.", fontsize=9, color="#52606d", linespacing=1.65)
    fig.subplots_adjust(bottom=0.29, top=0.72, left=0.18, right=0.99, wspace=0.20)
    fig.savefig(ROOT / "hedge_choices.png", dpi=180)
    plt.close(fig)

    base = [r for r in rows if r["cost_bps"] == 10 and r["date_shift"] == 0]
    fig, ax = plt.subplots(figsize=(12, 5.8))
    for hedge, color, label, marker in zip(HEDGES, colors, labels, ("x", "s", "o")):
        selected = [r for r in base if r["order"] == "cancelled" and r["hedge_ratio"] == hedge]
        ax.plot([r["start"] for r in selected], [r["cash_gbp"] for r in selected],
                color=color, label=label, marker=marker, markersize=4, linewidth=1.4,
                linestyle="--" if hedge == 0 else "-")
    starts = sorted({r["start"] for r in base})
    ticks = [starts[0], date(2019, 1, 1), date(2020, 1, 1), date(2021, 1, 1), starts[-1]]
    ax.set_xticks(ticks, [starts[0].strftime("%d %b %Y"), "Jan 2019", "Jan 2020",
                         "Jan 2021", starts[-1].strftime("%d %b %Y")])
    ax.margins(x=0.015)
    ax.set_ylabel("GBP cash from currency settlement (£)", labelpad=10)
    ax.set_xlabel("Contract start date", labelpad=12)
    ax.yaxis.set_major_formatter(StrMethodFormatter("{x:,.0f}"))
    ax.grid(axis="y", color="#e9edf0", linewidth=0.7)
    ax.legend(loc="lower left", bbox_to_anchor=(0, 1.015), ncol=3, frameon=False,
              borderaxespad=0, columnspacing=2)
    fig.suptitle("The order is cancelled. The forward contract still settles.",
                 x=0.04, y=0.96, ha="left", fontsize=17, fontweight="bold")
    fig.text(0.04, 0.885, "42 historical start dates · Three-month contracts · No sales receipts",
             fontsize=11, color="#52606d")
    fig.text(0.04, 0.06,
             "Each point is a separate hypothetical order, not cumulative account profit. "
             "The three-month windows overlap.\n"
             "Source: Bank of England reference rates. Assumed fees: 0.1% per currency leg. "
             "Business costs are excluded.", fontsize=9, color="#52606d", linespacing=1.65)
    fig.subplots_adjust(bottom=0.27, top=0.76, left=0.10, right=0.94)
    fig.savefig(ROOT / "cancelled_orders.png", dpi=180)
    plt.close(fig)


if __name__ == "__main__":
    rates = load_rates()
    first = make_cases(rates)[0]
    show_stress(first["start"], first["forward"])
    rows = run(rates)
    save_results(rows)
    show_summary(rows)
    plot_results(rows, first)
    print(f"\nSaved {len(rows):,} rows, hedge_choices.png and cancelled_orders.png.")
