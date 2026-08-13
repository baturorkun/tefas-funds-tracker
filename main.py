import requests
import math
import os
import shutil
from dotenv import load_dotenv
load_dotenv()
from datetime import datetime, timedelta
from collections import defaultdict
from xml.sax.saxutils import escape
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

TEFAS_URL = "https://www.tefas.gov.tr/api/funds/fonFiyatBilgiGetir"
TEFAS_INFO_URL = "https://www.tefas.gov.tr/api/funds/fonBilgiGetir"
HEADERS = {
    "Content-Type": "application/json",
    "Referer": "https://www.tefas.gov.tr/",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
}


def backup_portfolio(filename="fon.dat", backup_filename="fon.dat.backup"):
    """Create an atomic backup of the portfolio file before report generation."""
    temp_backup = f"{backup_filename}.tmp"
    try:
        shutil.copy2(filename, temp_backup)
        os.replace(temp_backup, backup_filename)
    except Exception:
        if os.path.exists(temp_backup):
            os.remove(temp_backup)
        raise


def read_portfolio(filename="fon.dat", as_of=None, include_sold=False):
    """
    as_of: YYYY-MM-DD string. Purchases first appear on the day after buy_date.
    Holdings with sold_date < as_of are excluded (sold at end of sold_date, so
    they appear on sold_date but not after).
    If as_of is None, all unsold holdings are returned. Set include_sold=True
    to return the full transaction history up to as_of.
    """
    holdings = []
    with open(filename, "r", encoding="utf-8") as f:
        f.readline()  # skip header
        for line in f:
            line = line.strip()
            if not line:
                continue
            parts = line.split()
            if len(parts) >= 3:
                sold_date = parts[4].strip() if len(parts) > 4 else ""
                buy_date = parts[0].strip()
                if as_of is not None and buy_date >= as_of:
                    continue
                if sold_date and not include_sold:
                    if as_of is None or sold_date < as_of:
                        continue
                holdings.append({
                    "alim_tarihi": buy_date,
                    "fon_kodu": parts[1].strip(),
                    "pay_adeti": int(parts[2].strip()),
                    "banka": parts[3].strip() if len(parts) > 3 else "",
                    "sold_date": sold_date,
                })
    return holdings


def get_fund_prices(fund_code, periyod=12):
    """Returns {date_str(YYYY-MM-DD): price, ...} and fund_name."""
    payload = {"fonKodu": fund_code, "dil": "TR", "periyod": periyod}
    try:
        resp = requests.post(TEFAS_URL, json=payload, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        result_list = data.get("resultList", [])
        prices = {}
        fund_name = fund_code
        for item in result_list:
            fund_name = item.get("fonUnvan", fund_code)
            tarih = item.get("tarih")   # YYYY-MM-DD
            fiyat = item.get("fiyat")
            if tarih and fiyat is not None:
                prices[tarih] = float(fiyat)
        return prices, fund_name
    except Exception as e:
        print(f"  Error ({fund_code}): {e}")
        return {}, fund_code


def get_fund_stats(fund_code):
    """Returns current TEFAS fund-level stats for a fund code."""
    payload = {"fonKodu": fund_code, "dil": "TR"}
    try:
        resp = requests.post(TEFAS_INFO_URL, json=payload, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        result_list = data.get("resultList") or []
        if not result_list:
            return None
        item = result_list[0]
        return {
            "code": item.get("fonKodu", fund_code),
            "name": item.get("fonUnvan", fund_code),
            "participant_count": int(item.get("yatirimciSayi") or 0),
            "fund_size": float(item.get("portBuyukluk") or 0.0),
            "outstanding_shares": int(item.get("payAdet") or 0),
            "market_share_pct": float(item.get("pazarPayi") or 0.0),
        }
    except Exception as e:
        print(f"  Stats error ({fund_code}): {e}")
        return None


def find_closest_price(prices, target_date_str):
    """
    Given prices {YYYY-MM-DD: float} and a target date (DD.MM.YYYY),
    return the price on that date or the nearest previous available date.
    """
    try:
        target = datetime.strptime(target_date_str, "%Y-%m-%d")
    except ValueError:
        return None, None

    # Walk backwards up to 7 days to find a trading day price
    for delta in range(8):
        candidate = (target - timedelta(days=delta)).strftime("%Y-%m-%d")
        if candidate in prices:
            return prices[candidate], candidate
    return None, None


def find_closest_price_forward(prices, target_date_str):
    """
    Like find_closest_price but walks FORWARD up to 7 days (TEFAS convention
    for monthly-return reference: if the target falls on a weekend/holiday,
    use the next available trading day).
    """
    try:
        target = datetime.strptime(target_date_str, "%Y-%m-%d")
    except ValueError:
        return None, None
    for delta in range(8):
        candidate = (target + timedelta(days=delta)).strftime("%Y-%m-%d")
        if candidate in prices:
            return prices[candidate], candidate
    return None, None


def fund_monthly_return(prices):
    """
    Returns (pct, today_price, price_1m_ago) for the ~30-day window.
    Uses forward-looking date resolution to match TEFAS website methodology.
    pct is None when insufficient data.
    """
    if not prices:
        return None, None, None
    sorted_dates = sorted(prices.keys())
    today_date = datetime.strptime(sorted_dates[-1], "%Y-%m-%d")
    target_1m = today_date - timedelta(days=30)
    price_1m, _ = find_closest_price_forward(prices, target_1m.strftime("%Y-%m-%d"))
    today_price = prices[sorted_dates[-1]]
    if price_1m is None or price_1m == 0:
        return None, today_price, None
    pct = (today_price - price_1m) / price_1m * 100
    return pct, today_price, price_1m


def fund_3month_return(prices):
    """
    Returns (pct, today_price, price_3m_ago) for the ~90-day window.
    Uses forward-looking date resolution to match TEFAS website methodology.
    pct is None when insufficient data.
    """
    if not prices:
        return None, None, None
    sorted_dates = sorted(prices.keys())
    today_date = datetime.strptime(sorted_dates[-1], "%Y-%m-%d")
    target_3m = today_date - timedelta(days=90)
    price_3m, _ = find_closest_price_forward(prices, target_3m.strftime("%Y-%m-%d"))
    today_price = prices[sorted_dates[-1]]
    if price_3m is None or price_3m == 0:
        return None, today_price, None
    pct = (today_price - price_3m) / price_3m * 100
    return pct, today_price, price_3m


def fund_6month_return(prices):
    """
    Returns (pct, today_price, price_6m_ago) for the ~180-day window.
    Uses forward-looking date resolution to match TEFAS website methodology.
    pct is None when insufficient data.
    """
    if not prices:
        return None, None, None
    sorted_dates = sorted(prices.keys())
    today_date = datetime.strptime(sorted_dates[-1], "%Y-%m-%d")
    target_6m = today_date - timedelta(days=180)
    price_6m, _ = find_closest_price_forward(prices, target_6m.strftime("%Y-%m-%d"))
    today_price = prices[sorted_dates[-1]]
    if price_6m is None or price_6m == 0:
        return None, today_price, None
    pct = (today_price - price_6m) / price_6m * 100
    return pct, today_price, price_6m


def calculate_xirr(cash_flows):
    """Calculate annualized money-weighted return for dated cash flows."""
    if not cash_flows or not any(v < 0 for _, v in cash_flows) or not any(v > 0 for _, v in cash_flows):
        return None

    dated = [(datetime.strptime(d, "%Y-%m-%d"), v) for d, v in cash_flows]
    origin = min(d for d, _ in dated)
    years = [((d - origin).days / 365.0, v) for d, v in dated]
    if max(t for t, _ in years) <= 0:
        return None

    # Solve in log(1 + rate) space. This remains stable for very high annualized
    # returns caused by young positions.
    def npv(log_rate):
        return sum(value * math.exp(-log_rate * year) for year, value in years)

    low, high = -50.0, 50.0
    low_npv, high_npv = npv(low), npv(high)
    if low_npv * high_npv > 0:
        return None
    for _ in range(200):
        mid = (low + high) / 2
        mid_npv = npv(mid)
        if abs(mid_npv) < 0.00000001:
            break
        if mid_npv > 0:
            low = mid
        else:
            high = mid
    try:
        return math.expm1((low + high) / 2)
    except OverflowError:
        return None


def calculate_fund_xirrs(holdings, fund_data, as_of):
    """Return per-fund and whole-portfolio XIRR for currently open positions."""
    flows_by_fund = defaultdict(list)
    current_by_fund = defaultdict(float)
    for holding in holdings:
        code = holding["fon_kodu"]
        prices, _ = fund_data.get(code, ({}, code))
        buy_price, _ = find_closest_price(prices, holding["alim_tarihi"])
        current_price, _ = find_closest_price(prices, as_of)
        if buy_price is None or current_price is None:
            continue
        shares = holding["pay_adeti"]
        flows_by_fund[code].append((holding["alim_tarihi"], -(buy_price * shares)))
        current_by_fund[code] += current_price * shares

    fund_xirr = {}
    portfolio_flows = []
    for code, flows in flows_by_fund.items():
        terminal = (as_of, current_by_fund[code])
        fund_xirr[code] = calculate_xirr(flows + [terminal])
        portfolio_flows.extend(flows)
        portfolio_flows.append(terminal)
    return fund_xirr, calculate_xirr(portfolio_flows)


def calculate_bank_totals(holdings, fund_data, as_of):
    """Return invested and current portfolio values grouped by bank."""
    bank_totals = defaultdict(lambda: {"cost": 0.0, "current": 0.0})
    for holding in holdings:
        code = holding["fon_kodu"]
        prices, _ = fund_data.get(code, ({}, code))
        buy_price, _ = find_closest_price(prices, holding["alim_tarihi"])
        current_price, _ = find_closest_price(prices, as_of)
        if buy_price is None or current_price is None:
            continue

        bank = holding.get("banka", "").strip() or "Unspecified"
        shares = holding["pay_adeti"]
        bank_totals[bank]["cost"] += buy_price * shares
        bank_totals[bank]["current"] += current_price * shares

    return bank_totals


def calculate_realized_pnl(holdings, fund_data, as_of):
    """Return realized P&L and closed-position details up to as_of.

    The closest TEFAS price on or before sold_date is used as the sale price.
    """
    realized_total = 0.0
    closed_positions = []
    for holding in holdings:
        sold_date = holding.get("sold_date", "")
        # A holding remains in the open-position report through sold_date and
        # becomes realized on the following report date.
        if not sold_date or sold_date >= as_of:
            continue

        code = holding["fon_kodu"]
        prices, _ = fund_data.get(code, ({}, code))
        buy_price, used_buy_date = find_closest_price(prices, holding["alim_tarihi"])
        sale_price, used_sale_date = find_closest_price(prices, sold_date)
        if buy_price is None or sale_price is None:
            continue

        shares = holding["pay_adeti"]
        cost = buy_price * shares
        proceeds = sale_price * shares
        pnl = proceeds - cost
        realized_total += pnl
        closed_positions.append({
            "bank": holding.get("banka", "").strip() or "Unspecified",
            "code": code,
            "buy_date": holding["alim_tarihi"],
            "used_buy_date": used_buy_date,
            "sold_date": sold_date,
            "used_sale_date": used_sale_date,
            "shares": shares,
            "cost": cost,
            "proceeds": proceeds,
            "pnl": pnl,
            "pnl_pct": (pnl / cost * 100) if cost else 0.0,
        })

    return realized_total, closed_positions


def personal_30d_return(xirr):
    """Convert an annual XIRR rate to its compounded 30-day equivalent."""
    if xirr is None or xirr <= -1:
        return None
    return math.expm1(math.log1p(xirr) * 30 / 365)


def format_personal_30d(xirr):
    rate = personal_30d_return(xirr)
    if rate is None:
        return "—"
    return f"{rate * 100:+.2f}%"


def build_fund_rankings(fund_summary, fund_data, top_n=5):
    horizons = [
        ("1M", fund_monthly_return),
        ("3M", fund_3month_return),
        ("6M", fund_6month_return),
    ]
    rankings = {}
    for term, return_fn in horizons:
        ranked = []
        for code, s in fund_summary.items():
            prices, _ = fund_data.get(code, ({}, code))
            ret_pct, _, _ = return_fn(prices)
            if ret_pct is None:
                continue
            ranked.append({
                "code": code,
                "name": s.get("name", code),
                "ret_pct": ret_pct,
                "current": s.get("current", 0.0),
            })
        ranked_desc = sorted(ranked, key=lambda x: x["ret_pct"], reverse=True)
        ranked_asc = sorted(ranked, key=lambda x: x["ret_pct"])
        rankings[term] = {
            "top": ranked_desc[:top_n],
            "bottom": ranked_asc[:top_n],
        }
    return rankings


def print_fund_performance_rankings(fund_summary, fund_data, top_n=5):
    """
    Prints console tables with top/bottom performers for 1M, 3M and 6M returns.
    """
    rankings = build_fund_rankings(fund_summary, fund_data, top_n=top_n)

    # ── Personal P&L rankings (since buy) ─────────────────────────────────────
    personal = []
    for code, s in fund_summary.items():
        cost = s.get("cost", 0.0)
        current = s.get("current", 0.0)
        if cost <= 0:
            continue
        pnl_pct = (current - cost) / cost * 100
        personal.append({
            "code": code, "name": s.get("name", code),
            "ret_pct": pnl_pct, "pnl": current - cost, "current": current,
        })
    personal_sorted = sorted(personal, key=lambda x: x["ret_pct"], reverse=True)
    row2 = "{:<6} {:<48} {:>10} {:>14} {:>14}"
    print("\n" + "=" * 115)
    print("MY TOP 5 (since buy — personal P&L %)")
    print(row2.format("Fund", "Fund Name", "P&L %", "P&L (TL)", "Cur. Value"))
    print("-" * 105)
    for r in personal_sorted[:top_n]:
        print(row2.format(r["code"], r["name"][:48],
                          f"{r['ret_pct']:+.2f}%", f"{r['pnl']:+,.0f}", f"{r['current']:,.0f}"))
    print("\n" + "=" * 115)
    print("MY BOTTOM 5 (since buy — personal P&L %)")
    print(row2.format("Fund", "Fund Name", "P&L %", "P&L (TL)", "Cur. Value"))
    print("-" * 105)
    for r in personal_sorted[-top_n:][::-1]:
        print(row2.format(r["code"], r["name"][:48],
                          f"{r['ret_pct']:+.2f}%", f"{r['pnl']:+,.0f}", f"{r['current']:,.0f}"))
    print("=" * 115)

    row = "{:<6} {:<48} {:>10} {:>14}"

    print("\n" + "=" * 115)
    print("TOP 5 PERFORMERS")
    for term in ("1M", "3M", "6M"):
        print(f"\n{term} TOP 5")
        print(row.format("Fund", "Fund Name", "Return", "Cur. Value"))
        print("-" * 90)
        top_rows = rankings[term]["top"]
        if not top_rows:
            print("No data available for this term.")
            continue
        for r in top_rows:
            print(row.format(
                r["code"],
                r["name"][:48],
                f"{r['ret_pct']:+.2f}%",
                f"{r['current']:,.0f}",
            ))

    print("\n" + "=" * 115)
    print("BOTTOM 5 PERFORMERS")
    for term in ("1M", "3M", "6M"):
        print(f"\n{term} BOTTOM 5")
        print(row.format("Fund", "Fund Name", "Return", "Cur. Value"))
        print("-" * 90)
        bottom_rows = rankings[term]["bottom"]
        if not bottom_rows:
            print("No data available for this term.")
            continue
        for r in bottom_rows:
            print(row.format(
                r["code"],
                r["name"][:48],
                f"{r['ret_pct']:+.2f}%",
                f"{r['current']:,.0f}",
            ))
    print("=" * 115)


def save_history(date_str, total_cost, total_current, total_pnl, total_pnl_pct,
                 daily_gain=None, daily_pct=None):
    import os
    path = os.path.join("reports", "history.tsv")
    lines = []
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    lines = [l for l in lines if l.strip() and not l.startswith(date_str)]
    daily_gain_str = f"{daily_gain:.2f}" if daily_gain is not None else ""
    daily_pct_str = f"{daily_pct:.6f}" if daily_pct is not None else ""
    lines.append(
        f"{date_str}\t{total_cost:.0f}\t{total_current:.0f}\t"
        f"{total_pnl:.0f}\t{total_pnl_pct:.2f}\t{daily_gain_str}\t{daily_pct_str}\n"
    )
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(lines)


def read_history():
    import os
    path = os.path.join("reports", "history.tsv")
    if not os.path.exists(path):
        return []
    entries = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            parts = line.strip().split("\t")
            if len(parts) >= 5:
                entry = {
                    "date": parts[0],
                    "cost": float(parts[1]),
                    "current": float(parts[2]),
                    "pnl": float(parts[3]),
                    "pnl_pct": float(parts[4]),
                }
                if len(parts) >= 7 and parts[5] and parts[6]:
                    entry["daily_gain"] = float(parts[5])
                    entry["daily_pct"] = float(parts[6])
                entries.append(entry)
    entries.sort(key=lambda x: x["date"])
    return entries


def read_fund_stats_history():
    import os
    path = os.path.join("reports", "fund_stats.tsv")
    if not os.path.exists(path):
        return []
    entries = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            parts = line.strip().split("\t")
            if len(parts) < 6:
                continue
            try:
                entries.append({
                    "date": parts[0],
                    "code": parts[1],
                    "participant_count": int(float(parts[2])),
                    "fund_size": float(parts[3]),
                    "outstanding_shares": int(float(parts[4])),
                    "market_share_pct": float(parts[5]),
                })
            except ValueError:
                continue
    entries.sort(key=lambda x: (x["date"], x["code"]))
    return entries


def save_fund_stats(rows):
    import os
    path = os.path.join("reports", "fund_stats.tsv")
    os.makedirs("reports", exist_ok=True)
    existing = read_fund_stats_history()
    replace_keys = {(r["date"], r["code"]) for r in rows}
    kept = [r for r in existing if (r["date"], r["code"]) not in replace_keys]
    all_rows = kept + rows
    all_rows.sort(key=lambda x: (x["date"], x["code"]))
    with open(path, "w", encoding="utf-8") as f:
        for r in all_rows:
            f.write(
                f"{r['date']}\t{r['code']}\t{r['participant_count']}\t"
                f"{r['fund_size']:.2f}\t{r['outstanding_shares']}\t"
                f"{r['market_share_pct']:g}\n"
            )


def print_fund_stats_changes(rows, previous_entries):
    if not rows:
        return

    previous_by_code = {}
    for r in rows:
        candidates = [
            e for e in previous_entries
            if e["code"] == r["code"] and e["date"] < r["date"]
        ]
        if candidates:
            previous_by_code[(r["date"], r["code"])] = sorted(candidates, key=lambda x: x["date"])[-1]

    print("\n" + "=" * 115)
    print("FUND STATS")
    col = "{:<6} {:>12} {:>12} {:>16} {:>14} {:>12} {:>10}"
    print(col.format("Fund", "Investors", "Inv. Δ", "Fund Size", "Size Δ", "Mkt Share", "Mkt Δ"))
    print("-" * 95)
    for r in sorted(rows, key=lambda x: x["code"]):
        prev = previous_by_code.get((r["date"], r["code"]))
        investor_delta = r["participant_count"] - prev["participant_count"] if prev else None
        size_delta = r["fund_size"] - prev["fund_size"] if prev else None
        market_delta = r["market_share_pct"] - prev["market_share_pct"] if prev else None
        print(col.format(
            r["code"],
            f"{r['participant_count']:,}",
            f"{investor_delta:+,}" if investor_delta is not None else "?",
            f"{r['fund_size']:,.0f}",
            f"{size_delta:+,.0f}" if size_delta is not None else "?",
            f"{r['market_share_pct']:.2f}%",
            f"{market_delta:+.2f}" if market_delta is not None else "?",
        ))
    print("=" * 115)


def compute_holding_period_return(holdings, fund_data, start_date, end_date):
    """Return gain and percentage from positions held at the end of a period.

    Closed positions that disappear after ``start_date`` are intentionally not
    treated as a loss of capital. New positions use their buy value as the
    period starting value, so purchases and sales cannot masquerade as return.
    """
    start_value = 0.0
    end_value = 0.0
    for holding in holdings:
        buy_date = holding["alim_tarihi"]
        sold_date = holding.get("sold_date", "")
        if buy_date > end_date or (sold_date and sold_date <= start_date):
            continue
        prices, _ = fund_data.get(holding["fon_kodu"], ({}, holding["fon_kodu"]))
        if not prices:
            continue
        position_start = max(start_date, buy_date)
        position_end = min(end_date, sold_date) if sold_date else end_date
        start_price, _ = find_closest_price(prices, position_start)
        end_price, _ = find_closest_price(prices, position_end)
        if start_price is None or end_price is None:
            continue
        shares = holding["pay_adeti"]
        start_value += start_price * shares
        end_value += end_price * shares

    gain = end_value - start_value
    pct = (gain / start_value * 100) if start_value else 0.0
    return gain, pct


def compute_daily_return_series(entries):
    """
    Compute cash-flow-adjusted daily returns from history-like entries.
    Each entry must have: date, cost, current.
    Returns list of dicts for days where previous day exists:
    {date, daily_gain, daily_pct, new_capital}
    """
    if not entries or len(entries) < 2:
        return []

    ordered = sorted(entries, key=lambda x: x["date"])
    series = []
    for i in range(1, len(ordered)):
        prev = ordered[i - 1]
        cur = ordered[i]
        new_capital = cur["cost"] - prev["cost"]
        if "daily_gain" in cur and "daily_pct" in cur:
            daily_gain = cur["daily_gain"]
            daily_pct = cur["daily_pct"]
        else:
            # Compatibility with history rows written before position-based
            # daily returns were stored.
            daily_gain = (cur["current"] - prev["current"]) - new_capital
            daily_pct = (daily_gain / prev["current"] * 100) if prev["current"] else 0.0
        series.append({
            "date": cur["date"],
            "daily_gain": daily_gain,
            "daily_pct": daily_pct,
            "new_capital": new_capital,
        })
    return series


def generate_history_chart(history, days=30):
    import os
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    if len(history) < 2:
        return None

    recent = history[-days:] if len(history) > days else history

    labels   = [e["date"][5:] for e in recent]   # MM-DD
    xs       = list(range(len(recent)))

    # Daily return bars should be cash-flow-adjusted (so red/green days are visible)
    daily_series = compute_daily_return_series(recent)
    daily_by_date = {d["date"]: d for d in daily_series}
    daily_pcts = [daily_by_date.get(e["date"], {"daily_pct": 0.0})["daily_pct"] for e in recent]

    # Build cash-flow-adjusted portfolio value line: start at first day's value,
    # then add only organic daily_gain (excluding new capital injections)
    adj_val = recent[0]["current"]
    adj_values = [adj_val]
    for d in daily_series:
        adj_val += d["daily_gain"]
        adj_values.append(adj_val)
    adj_currents = [v / 1000 for v in adj_values]

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(11, 4.5), sharex=True,
                                    gridspec_kw={"height_ratios": [2, 1]})
    fig.patch.set_facecolor("white")

    ax1.plot(xs, adj_currents, color="#1f4e79", linewidth=1.8, zorder=3)
    ax1.fill_between(xs, adj_currents, min(adj_currents) * 0.998, alpha=0.12, color="#1f4e79")
    ax1.set_ylabel("Portfolio Value (TL x1000)")
    ax1.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"{x:,.0f}K"))
    ax1.grid(True, alpha=0.3, linestyle="--")
    ax1.set_title("Portfolio Performance — Last 30 Days", fontsize=10, pad=6)

    bar_colors = ["#1a7a1a" if p >= 0 else "#c00000" for p in daily_pcts]
    ax2.bar(xs, daily_pcts, color=bar_colors, width=0.7, zorder=3)
    ax2.axhline(0, color="black", linewidth=0.6)
    ax2.set_ylabel("Daily Return %")
    ax2.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"{x:+.1f}%"))
    ax2.grid(True, alpha=0.3, linestyle="--")

    # Show at most ~8 evenly spaced date labels to avoid crowding
    n = len(xs)
    step = max(1, n // 8)
    tick_pos = xs[::step]
    tick_lbl = labels[::step]
    ax2.set_xticks(tick_pos)
    ax2.set_xticklabels(tick_lbl, rotation=30, ha="right", fontsize=8)

    plt.tight_layout(pad=1.2)
    chart_path = os.path.join("reports", "_chart_tmp.png")
    plt.savefig(chart_path, dpi=150, bbox_inches="tight", facecolor="white")
    plt.close()
    return chart_path


def main():
    import os
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="Re-generate today's report even if it already exists")
    parser.add_argument("--date", help="Run for a specific date (YYYY-MM-DD) instead of today")
    args = parser.parse_args()

    if args.date:
        try:
            today = datetime.strptime(args.date, "%Y-%m-%d")
        except ValueError:
            print(f"Invalid date format: {args.date!r} — expected YYYY-MM-DD")
            return
    else:
        today = datetime.now()
    today_str = today.strftime("%Y-%m-%d")

    if today.weekday() >= 5 and not args.force:  # 5=Saturday, 6=Sunday
        day_name = "Saturday" if today.weekday() == 5 else "Sunday"
        print(f"{today_str} is a {day_name} — TEFAS is closed. Skipping. (use --force to override)")
        return

    # Skip if today's report already exists in history AND PDF was generated
    existing = read_history()
    pdf_path = os.path.join("reports", f"rapor_{today_str}.pdf")
    if any(e["date"] == today_str for e in existing) and os.path.exists(pdf_path):
        if not args.force:
            print(f"Report for {today_str} already exists. Skipping. (use --force to regenerate)")
            return
        print(f"--force: regenerating report for {today_str}...")

    backup_portfolio("fon.dat", "fon.dat.backup")
    print("Portfolio backup created: fon.dat.backup")
    holdings = read_portfolio("fon.dat", as_of=today_str)
    all_holdings = read_portfolio("fon.dat", as_of=today_str, include_sold=True)

    print(f"\nTEFAS Portfolio P&L Report — {today_str}")
    print("=" * 115)

    # Fetch sold funds too so their realized P&L can be calculated.
    active_fund_codes = sorted(set(h["fon_kodu"] for h in holdings))
    fund_codes = sorted(set(h["fon_kodu"] for h in all_holdings))

    # Fetch prices
    print("Fetching fund prices...\n")
    fund_data = {}  # code -> (prices_dict, fund_name)
    price_errors = []
    for code in fund_codes:
        print(f"  [{code}] ", end="", flush=True)
        prices, name = get_fund_prices(code)
        fund_data[code] = (prices, name)
        if prices:
            latest_date = max(prices.keys())
            latest_price = prices[latest_date]
            print(f"{name[:50]:<52} — {len(prices)} days, latest: {latest_date} = {latest_price:.6f}")
            if latest_price == 0:
                price_errors.append(f"[{code}] {name[:50]} — latest price is 0 on {latest_date}")
        else:
            print("NO DATA")
            price_errors.append(f"[{code}] — no price data returned")

    if price_errors:
        print("\n⛔ Aborting: uncertain prices detected — report not generated.")
        for err in price_errors:
            print(f"  ✗ {err}")
        return

    latest_data_dates = [
        max(prices.keys()) for prices, _ in fund_data.values()
        if prices
    ]
    latest_data_date = max(latest_data_dates) if latest_data_dates else today_str
    if not args.date or today_str == latest_data_date:
        print("\nFetching fund stats...\n")
        previous_fund_stats = read_fund_stats_history()
        fund_stats_rows = []
        for code in active_fund_codes:
            print(f"  [{code}] stats ", end="", flush=True)
            stats = get_fund_stats(code)
            if not stats:
                print("NO DATA")
                continue
            prices, _ = fund_data.get(code, ({}, code))
            stats["date"] = max(prices.keys()) if prices else latest_data_date
            fund_stats_rows.append(stats)
            print(
                f"investors: {stats['participant_count']:,}, "
                f"size: {stats['fund_size']:,.0f} TL, "
                f"market share: {stats['market_share_pct']:.2f}%"
            )
        if fund_stats_rows:
            save_fund_stats(fund_stats_rows)
            print_fund_stats_changes(fund_stats_rows, previous_fund_stats)
            print("Fund stats saved: reports/fund_stats.tsv")
    else:
        print(
            f"\nSkipping fund stats for historical report date {today_str}; "
            f"TEFAS latest stats date appears to be {latest_data_date}."
        )

    # ── Benchmark fund ─────────────────────────────────────────────────────────
    benchmark_code = os.environ.get("BENCHMARK_FUND", "").strip().upper()
    bench_prices = {}
    bench_name = ""
    use_benchmark = False
    if benchmark_code:
        if benchmark_code in fund_data:
            bench_prices, bench_name = fund_data[benchmark_code]
            use_benchmark = True
        else:
            print(f"  [{benchmark_code}] (benchmark) ", end="", flush=True)
            bench_prices, bench_name = get_fund_prices(benchmark_code)
            if bench_prices:
                latest_date = max(bench_prices.keys())
                print(f"{bench_name[:50]:<52} — {len(bench_prices)} days, latest: {latest_date} = {bench_prices[latest_date]:.6f}")
                use_benchmark = True
            else:
                print("NO DATA")

    print()

    # ── Per-transaction table ──────────────────────────────────────────────────
    col = "{:<10} {:<5} {:<13} {:>10} {:>13} {:>13} {:>14} {:>14} {:>14} {:>8}"
    header = col.format(
        "Bank", "Fund", "Buy Date", "Shares",
        "Buy Price", "Today Price",
        "Buy Value", "Cur. Value", "P&L", "P&L %"
    )
    print(header)
    print("-" * 115)

    total_cost = 0.0
    total_current = 0.0
    total_bench_current = 0.0
    today_capital = 0.0
    missing_prices = []
    bench_rows = []

    # For per-fund summary
    fund_summary = defaultdict(lambda: {"cost": 0.0, "current": 0.0, "shares": 0, "name": "", "weighted_days": 0.0, "buy_count": 0})

    for h in holdings:
        code = h["fon_kodu"]
        buy_date = h["alim_tarihi"]
        shares = h["pay_adeti"]
        bank = h["banka"]

        prices, fund_name = fund_data.get(code, ({}, code))

        # Buy price: exact date or nearest previous trading day
        buy_price, used_buy_date = find_closest_price(prices, buy_date)

        # Current price: price on the target date (or nearest previous trading day)
        current_price, latest_date = find_closest_price(prices, today_str)

        if buy_price is not None and current_price is not None:
            cost = buy_price * shares
            current_val = current_price * shares
            pnl = current_val - cost
            pnl_pct = (pnl / cost) * 100 if cost else 0.0

            total_cost += cost
            total_current += current_val
            if buy_date == today_str:
                today_capital += cost
            # Benchmark
            bench_cur = None
            if use_benchmark:
                bench_bp, _ = find_closest_price(bench_prices, buy_date)
                bench_tp, _ = find_closest_price(bench_prices, today_str)
                if bench_bp and bench_tp and bench_bp > 0:
                    bench_cur = (cost / bench_bp) * bench_tp
                    total_bench_current += bench_cur
            bench_rows.append((bank, code, buy_date, cost, bench_cur, current_val))
            fund_summary[code]["cost"] += cost
            fund_summary[code]["current"] += current_val
            fund_summary[code]["shares"] += shares
            fund_summary[code]["name"] = fund_name
            try:
                _days_held = (datetime.strptime(today_str, "%Y-%m-%d") - datetime.strptime(buy_date, "%Y-%m-%d")).days
            except ValueError:
                _days_held = 0
            fund_summary[code]["weighted_days"] += cost * _days_held
            fund_summary[code]["buy_count"] += 1

            note = f" *{used_buy_date}" if used_buy_date != buy_date else ""
            print(col.format(
                bank, code, buy_date, f"{shares:,}",
                f"{buy_price:.6f}", f"{current_price:.6f}",
                f"{cost:,.0f}", f"{current_val:,.0f}",
                f"{pnl:+,.0f}", f"{pnl_pct:+.2f}%"
            ) + note)
        else:
            missing = []
            if buy_price is None:
                missing.append(f"buy price not found ({buy_date})")
            if current_price is None:
                missing.append("no current price")
            missing_prices.append(f"{code} — {', '.join(missing)}")
            print(col.format(bank, code, buy_date, f"{shares:,}", "?", "?", "?", "?", "?", "?")
                  + f"  ⚠ {', '.join(missing)}")

    # ── Totals ─────────────────────────────────────────────────────────────────
    print("-" * 115)
    total_pnl = total_current - total_cost
    total_pnl_pct = (total_pnl / total_cost * 100) if total_cost else 0.0
    realized_pnl, closed_positions = calculate_realized_pnl(
        all_holdings, fund_data, today_str
    )
    combined_pnl = total_pnl + realized_pnl
    combined_capital = total_cost - realized_pnl
    combined_pnl_pct = (
        combined_pnl / combined_capital * 100 if combined_capital else 0.0
    )
    print(col.format(
        "TOTAL", "", "", "",
        "", "",
        f"{total_cost:,.0f}", f"{total_current:,.0f}",
        f"{total_pnl:+,.0f}", f"{total_pnl_pct:+.2f}%"
    ))
    # ── Benchmark comparison table ─────────────────────────────────────────────────────
    if use_benchmark and any(r[4] is not None for r in bench_rows):
        bench_pnl_total = total_bench_current - total_cost
        bench_pnl_pct_total = (bench_pnl_total / total_cost * 100) if total_cost else 0.0
        total_vs_bench = total_current - total_bench_current
        vs_bench_pct_total = (total_vs_bench / total_bench_current * 100) if total_bench_current else 0.0
        print(f"\nBENCHMARK — [{benchmark_code}] {bench_name[:55]}")
        bcol = "{:<10} {:<5} {:<13} {:>14} {:>14} {:>14} {:>9}"
        print(bcol.format("Bank", "Fund", "Buy Date", "Buy Value", "Bench Val", "vs Bench", "vs Bench%"))
        print("-" * 93)
        for bank_, code_, bdate_, cost_, bench_cur_, cur_val_ in bench_rows:
            if bench_cur_ is not None:
                vs = cur_val_ - bench_cur_
                vs_pct = (vs / bench_cur_ * 100) if bench_cur_ else 0.0
                print(bcol.format(bank_, code_, bdate_, f"{cost_:,.0f}", f"{bench_cur_:,.0f}", f"{vs:+,.0f}", f"{vs_pct:+.2f}%"))
            else:
                print(bcol.format(bank_, code_, bdate_, f"{cost_:,.0f}", "?", "?", "?"))
        print("-" * 93)
        print(bcol.format("TOTAL", "", "", f"{total_cost:,.0f}", f"{total_bench_current:,.0f}", f"{total_vs_bench:+,.0f}", f"{vs_bench_pct_total:+.2f}%"))
    # ── Per-fund summary ───────────────────────────────────────────────────────
    # Aggregate benchmark values per fund
    fund_bench: dict = {}
    if use_benchmark:
        for _, _fc, _, _, _bc, _ in bench_rows:
            if _bc is not None:
                fund_bench[_fc] = fund_bench.get(_fc, 0.0) + _bc
    fund_xirr, portfolio_xirr = calculate_fund_xirrs(holdings, fund_data, today_str)
    _bench_cols = use_benchmark and bool(fund_bench)
    sep_w = 187 if _bench_cols else 151
    print("\n" + "=" * sep_w)
    print("FUND SUMMARY\n")
    if _bench_cols:
        col2 = "{:<5} {:<50} {:>8} {:>10} {:>10} {:>10} {:>14} {:>14} {:>14} {:>14} {:>10} {:>9} {:>8} {:>14}"
        print(col2.format("Fund", "Fund Name", "Today %", "1M %", "3M %", "Shares", "Buy Value", "Cur. Value", "P&L", "Bench Val", "vs Bench", "vs Bnch%", "P&L %", "Personal 30D %"))
    else:
        col2 = "{:<5} {:<50} {:>8} {:>10} {:>10} {:>10} {:>14} {:>14} {:>14} {:>8} {:>14}"
        print(col2.format("Fund", "Fund Name", "Today %", "1M %", "3M %", "Shares", "Buy Value", "Cur. Value", "P&L", "P&L %", "Personal 30D %"))
    print("-" * sep_w)
    # Collect 1M and 3M data for weighted averages
    _1m_weight_sum = 0.0
    _1m_weighted_pct = 0.0
    _3m_weight_sum = 0.0
    _3m_weighted_pct = 0.0
    for code in sorted(
        fund_summary.keys(),
        key=lambda c: fund_xirr.get(c) if fund_xirr.get(c) is not None else float("-inf"),
        reverse=True,
    ):
        s = fund_summary[code]
        pnl = s["current"] - s["cost"]
        pct = (pnl / s["cost"] * 100) if s["cost"] else 0.0
        prices, _ = fund_data.get(code, ({}, code))
        # today daily %
        sorted_p = sorted(prices.keys())
        if len(sorted_p) >= 2:
            today_p = prices[sorted_p[-1]]
            prev_p  = prices[sorted_p[-2]]
            daily_pct_str = f"{((today_p - prev_p) / prev_p * 100):+.2f}%" if prev_p else "—"
        else:
            daily_pct_str = "—"
        # 1-month %
        m_pct, _, _ = fund_monthly_return(prices)
        m_pct_str = f"{m_pct:+.2f}%" if m_pct is not None else "—"
        if m_pct is not None and s["current"] > 0:
            _1m_weight_sum += s["current"]
            _1m_weighted_pct += m_pct * s["current"]
        # 3-month %
        m3_pct, _, _ = fund_3month_return(prices)
        m3_pct_str = f"{m3_pct:+.2f}%" if m3_pct is not None else "—"
        if m3_pct is not None and s["current"] > 0:
            _3m_weight_sum += s["current"]
            _3m_weighted_pct += m3_pct * s["current"]
        if _bench_cols:
            b_cur = fund_bench.get(code, 0.0)
            vs_b = s["current"] - b_cur if b_cur else None
            vs_b_pct = (vs_b / b_cur * 100) if b_cur else None
            print(col2.format(
                code, s["name"][:50],
                daily_pct_str, m_pct_str, m3_pct_str,
                f"{s['shares']:,}",
                f"{s['cost']:,.0f}", f"{s['current']:,.0f}",
                f"{pnl:+,.0f}",
                f"{b_cur:,.0f}" if b_cur else "?",
                f"{vs_b:+,.0f}" if vs_b is not None else "?",
                f"{vs_b_pct:+.2f}%" if vs_b_pct is not None else "?",
                f"{pct:+.2f}%", format_personal_30d(fund_xirr.get(code))
            ))
        else:
            print(col2.format(
                code, s["name"][:50],
                daily_pct_str, m_pct_str, m3_pct_str,
                f"{s['shares']:,}",
                f"{s['cost']:,.0f}", f"{s['current']:,.0f}",
                f"{pnl:+,.0f}", f"{pct:+.2f}%", format_personal_30d(fund_xirr.get(code))
            ))
    print("-" * sep_w)
    avg_1m_str = f"{(_1m_weighted_pct / _1m_weight_sum):+.2f}%" if _1m_weight_sum else "—"
    avg_3m_str = f"{(_3m_weighted_pct / _3m_weight_sum):+.2f}%" if _3m_weight_sum else "—"
    fund_count = len(fund_summary)
    if _bench_cols:
        bench_vs = total_current - total_bench_current
        bench_vs_pct = (bench_vs / total_bench_current * 100) if total_bench_current else 0.0
        print(col2.format(f"TOTAL ({fund_count})", "", "", avg_1m_str, avg_3m_str, "", f"{total_cost:,.0f}", f"{total_current:,.0f}", f"{total_pnl:+,.0f}", f"{total_bench_current:,.0f}", f"{bench_vs:+,.0f}", f"{bench_vs_pct:+.2f}%", f"{total_pnl_pct:+.2f}%", format_personal_30d(portfolio_xirr)))
    else:
        print(col2.format(f"TOTAL ({fund_count})", "", "", avg_1m_str, avg_3m_str, "", f"{total_cost:,.0f}", f"{total_current:,.0f}", f"{total_pnl:+,.0f}", f"{total_pnl_pct:+.2f}%", format_personal_30d(portfolio_xirr)))

    print(f"\n  Total Invested : {total_cost:>14,.0f} TL")
    print(f"  Current Value  : {total_current:>14,.0f} TL")
    print(f"  Open P&L       : {total_pnl:>+14,.0f} TL  ({total_pnl_pct:+.2f}%)")
    print(f"  Realized P&L   : {realized_pnl:>+14,.0f} TL")
    print(f"  Combined P&L   : {combined_pnl:>+14,.0f} TL  ({combined_pnl_pct:+.2f}%)")
    if use_benchmark and total_bench_current > 0:
        bench_pnl = total_bench_current - total_cost
        bench_pnl_pct = (bench_pnl / total_cost * 100) if total_cost else 0.0
        vs_bench = total_current - total_bench_current
        vs_bench_pct = (vs_bench / total_bench_current * 100) if total_bench_current else 0.0
        print(f"  Benchmark [{benchmark_code}]  : {total_bench_current:>14,.0f} TL  ({bench_pnl_pct:+.2f}%)")
        print(f"  vs Benchmark (open positions): {vs_bench:>+14,.0f} TL  ({vs_bench_pct:+.2f}%)")
    # ── Daily returns (today and yesterday) ────────────────────────────────────
    history = [e for e in existing if e["date"] < today_str]  # exclude today
    today_daily_gain = None
    today_daily_pct = None
    if history:
        prev = history[-1]
        today_daily_gain, today_daily_pct = compute_holding_period_return(
            all_holdings, fund_data, prev["date"], today_str
        )
        print(f"\n  Today's Daily Return: {today_daily_pct:>+7.2f}%  ({prev['date']} \u2192 {today_str})")
        if len(history) >= 2:
            prev2 = history[-2]
            if "daily_pct" in prev:
                yest_daily_pct = prev["daily_pct"]
            else:
                yest_new_capital = prev["cost"] - prev2["cost"]
                yest_gain = (prev["current"] - prev2["current"]) - yest_new_capital
                yest_daily_pct = (yest_gain / prev2["current"] * 100) if prev2["current"] else 0.0
            print(f"  Yesterday's Return: {yest_daily_pct:>+7.2f}%  ({prev2['date']} → {prev['date']})")

    # Show recent daily returns so negative days are clearly visible
    today_history_entry = {
        "date": today_str,
        "cost": total_cost,
        "current": total_current,
        "pnl": total_pnl,
        "pnl_pct": total_pnl_pct,
    }
    if today_daily_gain is not None:
        today_history_entry["daily_gain"] = today_daily_gain
        today_history_entry["daily_pct"] = today_daily_pct
    history_with_today = history + [today_history_entry]
    recent_daily = compute_daily_return_series(history_with_today)[-7:]
    if recent_daily:
        print("\n  Recent Daily Returns (cash-flow adjusted):")
        for d in recent_daily:
            print(f"    {d['date']}: {d['daily_pct']:+.2f}%  ({d['daily_gain']:+,.0f} TL)")
    print()

    if missing_prices:
        print("* Rows with missing prices:")
        for m in missing_prices:
            print(f"  - {m}")
    print()

    save_history(
        today_str, total_cost, total_current, total_pnl, total_pnl_pct,
        today_daily_gain, today_daily_pct,
    )
    print_fund_performance_rankings(fund_summary, fund_data, top_n=3)
    write_pdf_report(
        holdings, fund_data, fund_summary,
        total_cost, total_current, total_pnl, total_pnl_pct,
        missing_prices, today_str, today_capital,
        benchmark_code=benchmark_code, bench_name=bench_name,
        total_bench_current=total_bench_current,
        fund_bench=fund_bench,
        fund_xirr=fund_xirr,
        portfolio_xirr=portfolio_xirr,
        realized_pnl=realized_pnl,
        closed_positions=closed_positions,
    )


def write_pdf_report(holdings, fund_data, fund_summary,
                     total_cost, total_current, total_pnl, total_pnl_pct,
                     missing_prices, today_str, today_capital=0.0,
                     benchmark_code="", bench_name="", total_bench_current=0.0,
                     fund_bench=None, fund_xirr=None, portfolio_xirr=None,
                     realized_pnl=0.0, closed_positions=None):
    # ── Register fonts ─────────────────────────────────────────────────────────
    font_paths = [
        ("/System/Library/Fonts/Supplemental/Arial.ttf",
         "/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
        ("/Library/Fonts/Arial.ttf", "/Library/Fonts/Arial Bold.ttf"),
    ]
    font_reg = False
    for regular, bold in font_paths:
        try:
            pdfmetrics.registerFont(TTFont("Arial", regular))
            pdfmetrics.registerFont(TTFont("Arial-Bold", bold))
            font_reg = True
            break
        except Exception:
            continue
    FONT = "Arial" if font_reg else "Helvetica"
    FONT_B = "Arial-Bold" if font_reg else "Helvetica-Bold"

    # ── Styles ─────────────────────────────────────────────────────────────────
    def style(name, size, bold=False, color=colors.black, align="LEFT"):
        return ParagraphStyle(
            name,
            fontName=FONT_B if bold else FONT,
            fontSize=size,
            textColor=color,
            alignment={"LEFT": 0, "CENTER": 1, "RIGHT": 2}[align],
            leading=size * 1.3,
        )

    title_style   = style("title",   16, bold=True, align="CENTER")
    heading_style = style("heading",  9, bold=True)
    normal_style  = style("normal",   8)
    small_style   = style("small",    7)

    def th(text): return Paragraph(f"<b>{escape(str(text))}</b>", style("th", 7, bold=True, color=colors.white))
    def td(text, bold=False, align="LEFT"):
        return Paragraph(escape(str(text)), style("td", 7, bold=bold, align=align))
    def tdr(text, bold=False):
        return td(text, bold=bold, align="RIGHT")

    HEADER_BG  = colors.HexColor("#1f4e79")
    ALT_BG     = colors.HexColor("#dce6f1")
    TOTAL_BG   = colors.HexColor("#bdd7ee")
    GREEN_HEX = "#1a7a1a"
    RED_HEX   = "#c00000"

    def pnl_para(text, val, bold=False, align="RIGHT", size=7):
        hex_color = GREEN_HEX if val >= 0 else RED_HEX
        return Paragraph(f'<font color="{hex_color}">{text}</font>',
                         style("pnl", size, bold=bold, align=align))

    # ── Document ───────────────────────────────────────────────────────────────
    import os
    os.makedirs("reports", exist_ok=True)
    filename = os.path.join("reports", f"rapor_{today_str}.pdf")
    doc = SimpleDocTemplate(filename, pagesize=landscape(A4),
                            leftMargin=1.2*cm, rightMargin=1.2*cm,
                            topMargin=1.5*cm, bottomMargin=1.5*cm)
    story = []

    weighted_holding_days = sum(
        summary.get("weighted_days", 0.0) for summary in fund_summary.values()
    )
    avg_holding_days = round(weighted_holding_days / total_cost) if total_cost else None

    # Title
    story.append(Paragraph("TEFAS Portfolio P&amp;L Report", title_style))
    story.append(Spacer(1, 0.3*cm))
    story.append(Paragraph(f"Date: {today_str}", style("sub", 9, align="CENTER")))
    story.append(Spacer(1, 0.5*cm))

    # ── Read history for daily changes ─────────────────────────────────────────
    history = read_history()
    today_entry = next((e for e in history if e["date"] == today_str), None)
    prev_entries = [e for e in history if e["date"] < today_str]

    # ── Summary table ──────────────────────────────────────────────────────────
    story.append(Paragraph("Summary", heading_style))
    story.append(Spacer(1, 0.2*cm))
    open_pnl_str = f"{total_pnl:+,.0f} TL  ({total_pnl_pct:+.2f}%)"
    combined_pnl = total_pnl + realized_pnl
    combined_capital = total_cost - realized_pnl
    combined_pnl_pct = (
        combined_pnl / combined_capital * 100 if combined_capital else 0.0
    )
    summary_data = [
        [th(""), th("Value")],
        [td("Avg. Holding Period"),
         td(f"{avg_holding_days} days (weighted by invested amount)")
         if avg_holding_days is not None else td("—")],
        [td("Total Invested"),  tdr(f"{total_cost:,.0f} TL")],
        [td("Current Value"),   tdr(f"{total_current:,.0f} TL")],
        [td("Open Positions P&L"), pnl_para(open_pnl_str, total_pnl)],
        [td("Realized P&L (closed)"),
         pnl_para(f"{realized_pnl:+,.0f} TL", realized_pnl)],
        [td("Combined P&L"),
         pnl_para(f"{combined_pnl:+,.0f} TL  ({combined_pnl_pct:+.2f}%)",
                  combined_pnl, bold=True)],
    ]
    today_return_row = None
    cost_basis_rows = []
    if today_entry and prev_entries:
        prev = prev_entries[-1]
        new_capital = today_entry["cost"] - prev["cost"]
        if "daily_gain" in today_entry:
            today_gain = today_entry["daily_gain"]
            today_daily_pct = today_entry["daily_pct"]
        else:
            today_gain = (today_entry["current"] - prev["current"]) - new_capital
            today_daily_pct = (today_gain / prev["current"] * 100) if prev["current"] else 0.0
        today_daily_str = f"{today_gain:+,.0f} TL  ({today_daily_pct:+.2f}%)  (vs {prev['date']})"
        today_return_row = len(summary_data)
        summary_data.append([
            td("Today's Daily Return", bold=True),
            pnl_para(today_daily_str, today_daily_pct, bold=True, size=8),
        ])
        if abs(new_capital) > 0.01:
            cost_basis_rows.append([td("Cost Basis Change"), tdr(f"{new_capital:+,.0f} TL")])
        if len(prev_entries) >= 2:
            prev2 = prev_entries[-2]
            yest_new_capital = prev["cost"] - prev2["cost"]
            if "daily_gain" in prev:
                yest_gain = prev["daily_gain"]
                yest_daily_pct = prev["daily_pct"]
            else:
                yest_gain = (prev["current"] - prev2["current"]) - yest_new_capital
                yest_daily_pct = (yest_gain / prev2["current"] * 100) if prev2["current"] else 0.0
            yest_daily_str = f"{yest_gain:+,.0f} TL  ({yest_daily_pct:+.2f}%)  (vs {prev2['date']})"
            summary_data.append([td("Yesterday's Return"), pnl_para(yest_daily_str, yest_daily_pct)])
            if abs(yest_new_capital) > 0.01:
                cost_basis_rows.append([td("Previous Cost Basis Change"), tdr(f"{yest_new_capital:+,.0f} TL")])
        summary_data.extend(cost_basis_rows)

    # Benchmark belongs at the end: it is a reference, not a portfolio total.
    if benchmark_code and total_bench_current > 0:
        bench_pnl = total_bench_current - total_cost
        bench_pnl_pct = (bench_pnl / total_cost * 100) if total_cost else 0.0
        vs_bench = total_current - total_bench_current
        vs_bench_pct = (vs_bench / total_bench_current * 100) if total_bench_current else 0.0
        summary_data.append([td(f"Benchmark [{benchmark_code}]"), tdr(f"{total_bench_current:,.0f} TL  ({bench_pnl_pct:+.2f}%)")])
        vs_bench_str = f"{vs_bench:+,.0f} TL  ({vs_bench_pct:+.2f}%)"
        summary_data.append([td("vs Benchmark (open positions)"), pnl_para(vs_bench_str, vs_bench)])

    summary_style = [
        ("BACKGROUND",  (0,0), (-1,0), HEADER_BG),
        ("TEXTCOLOR",   (0,0), (-1,0), colors.white),
        ("ROWBACKGROUNDS", (0,1), (-1,-1), [colors.white, ALT_BG]),
        ("BOX",         (0,0), (-1,-1), 0.5, colors.grey),
        ("INNERGRID",   (0,0), (-1,-1), 0.3, colors.lightgrey),
        ("VALIGN",      (0,0), (-1,-1), "MIDDLE"),
        ("TOPPADDING",  (0,0), (-1,-1), 3),
        ("BOTTOMPADDING",(0,0), (-1,-1), 3),
    ]
    if today_return_row is not None:
        TODAY_BG = colors.HexColor("#fff2cc")
        TODAY_BORDER = colors.HexColor("#d6a800")
        summary_style.extend([
            ("BACKGROUND", (0,today_return_row), (-1,today_return_row), TODAY_BG),
            ("BOX", (0,today_return_row), (-1,today_return_row), 1.2, TODAY_BORDER),
            ("TOPPADDING", (0,today_return_row), (-1,today_return_row), 5),
            ("BOTTOMPADDING", (0,today_return_row), (-1,today_return_row), 5),
        ])
    summary_table = Table(summary_data, colWidths=[5*cm, 7*cm])
    summary_table.setStyle(TableStyle(summary_style))
    story.append(summary_table)
    story.append(Spacer(1, 0.6*cm))

    # ── Bank totals table ──
    bank_totals = calculate_bank_totals(holdings, fund_data, today_str)
    if bank_totals:
        story.append(Paragraph("Bank Totals", heading_style))
        story.append(Spacer(1, 0.2*cm))
        bank_rows = [[th("Bank"), th("Invested (TL)"), th("Current Value (TL)"),
                      th("P&L (TL)"), th("Portfolio %")]]
        for bank, values in sorted(
            bank_totals.items(), key=lambda item: item[1]["current"], reverse=True
        ):
            bank_pnl = values["current"] - values["cost"]
            allocation = (values["current"] / total_current * 100) if total_current else 0.0
            bank_rows.append([
                td(bank, bold=True),
                tdr(f'{values["cost"]:,.0f}'),
                tdr(f'{values["current"]:,.0f}', bold=True),
                pnl_para(f"{bank_pnl:+,.0f}", bank_pnl),
                tdr(f"{allocation:.1f}%"),
            ])
        bank_rows.append([
            td(f"TOTAL ({len(bank_totals)})", bold=True),
            tdr(f"{total_cost:,.0f}", bold=True),
            tdr(f"{total_current:,.0f}", bold=True),
            pnl_para(f"{total_pnl:+,.0f}", total_pnl, bold=True),
            tdr("100.0%", bold=True),
        ])
        bank_table = Table(
            bank_rows,
            colWidths=[4.0*cm, 3.2*cm, 3.7*cm, 3.2*cm, 2.8*cm],
            repeatRows=1,
        )
        bank_table.setStyle(TableStyle([
            ("BACKGROUND", (0,0), (-1,0), HEADER_BG),
            ("TEXTCOLOR", (0,0), (-1,0), colors.white),
            ("ROWBACKGROUNDS", (0,1), (-1,-2), [colors.white, ALT_BG]),
            ("BACKGROUND", (0,-1), (-1,-1), TOTAL_BG),
            ("BOX", (0,0), (-1,-1), 0.5, colors.grey),
            ("INNERGRID", (0,0), (-1,-1), 0.3, colors.lightgrey),
            ("VALIGN", (0,0), (-1,-1), "MIDDLE"),
            ("TOPPADDING", (0,0), (-1,-1), 3),
            ("BOTTOMPADDING", (0,0), (-1,-1), 3),
            ("ALIGN", (1,0), (-1,-1), "RIGHT"),
        ]))
        story.append(bank_table)
        story.append(Spacer(1, 0.6*cm))

    # ── Fund summary table ─────────────────────────────────────────────────────
    story.append(Paragraph("Fund Summary", heading_style))
    story.append(Spacer(1, 0.2*cm))
    _pdf_bench = fund_bench if fund_bench else {}
    _pdf_xirr = fund_xirr if fund_xirr else {}
    if _pdf_bench:
        fund_header = [th("Fund"), th("Fund Name"), th("Today %"), th("1M %"), th("3M %"), th("Days"), th("Shares"),
                       th("Buy Value"), th("Cur. Value"), th("P&L"), th("P&L %"),
                       th("Bench Val"), th("vs Bench"), th("vs Bnch%"), th("Personal 30D %")]
    else:
        fund_header = [th("Fund"), th("Fund Name"), th("Today %"), th("1M %"), th("3M %"), th("Days"), th("Shares"), th("Portfolio %"),
                       th("Buy Value"), th("Cur. Value"),
                       th("P&L"), th("P&L %"), th("Personal 30D %")]
    fund_rows = [fund_header]
    _pdf_1m_weight_sum = 0.0
    _pdf_1m_weighted_pct = 0.0
    _pdf_3m_weight_sum = 0.0
    _pdf_3m_weighted_pct = 0.0
    for code in sorted(
        fund_summary.keys(),
        key=lambda c: _pdf_xirr.get(c) if _pdf_xirr.get(c) is not None else float("-inf"),
        reverse=True,
    ):
        s = fund_summary[code]
        pnl = s["current"] - s["cost"]
        pct = (pnl / s["cost"] * 100) if s["cost"] else 0.0
        # Daily return for this fund
        prices, _ = fund_data.get(code, ({}, code))
        sorted_dates = sorted(prices.keys())
        if len(sorted_dates) >= 2:
            today_p = prices[sorted_dates[-1]]
            prev_p  = prices[sorted_dates[-2]]
            daily_pct = ((today_p - prev_p) / prev_p) * 100 if prev_p else 0.0
            daily_cell = pnl_para(f"{daily_pct:+.2f}%", daily_pct, bold=abs(daily_pct) >= 1.0)
        else:
            daily_pct = 0.0
            daily_cell = td("—", align="RIGHT")
        # 1-month return for this fund
        m_pct, _, _ = fund_monthly_return(prices)
        if m_pct is not None:
            m_cell = pnl_para(f"{m_pct:+.2f}%", m_pct, bold=abs(m_pct) >= 2.0)
            if s["current"] > 0:
                _pdf_1m_weight_sum += s["current"]
                _pdf_1m_weighted_pct += m_pct * s["current"]
        else:
            m_cell = td("—", align="RIGHT")
        # 3-month return for this fund
        m3_pct, _, _ = fund_3month_return(prices)
        if m3_pct is not None:
            m3_cell = pnl_para(f"{m3_pct:+.2f}%", m3_pct, bold=abs(m3_pct) >= 2.0)
            if s["current"] > 0:
                _pdf_3m_weight_sum += s["current"]
                _pdf_3m_weighted_pct += m3_pct * s["current"]
        else:
            m3_cell = td("—", align="RIGHT")
        alloc_pct = (s["current"] / total_current * 100) if total_current else 0.0
        if _pdf_bench:
            b_cur = _pdf_bench.get(code, 0.0)
            vs_b = s["current"] - b_cur if b_cur else None
            vs_b_pct = (vs_b / b_cur * 100) if b_cur else None
            fund_rows.append([
                td(code, bold=True),
                td(s["name"]),
                daily_cell,
                m_cell,
                m3_cell,
                tdr(f"{int(s['weighted_days']/s['cost'])}{'*' if s['buy_count'] > 1 else ''}") if s["cost"] else td("—", align="RIGHT"),
                tdr(f"{s['shares']:,}"),
                tdr(f"{s['cost']:,.0f}"),
                tdr(f"{s['current']:,.0f}"),
                pnl_para(f"{pnl:+,.0f}", pnl),
                pnl_para(f"{pct:+.2f}%", pnl, bold=True),
                tdr(f"{b_cur:,.0f}") if b_cur else td("?"),
                pnl_para(f"{vs_b:+,.0f}", vs_b) if vs_b is not None else td("?"),
                pnl_para(f"{vs_b_pct:+.2f}%", vs_b_pct) if vs_b_pct is not None else td("?"),
                pnl_para(format_personal_30d(_pdf_xirr.get(code)), _pdf_xirr[code]) if _pdf_xirr.get(code) is not None else td("—", align="RIGHT"),
            ])
        else:
            fund_rows.append([
                td(code, bold=True),
                td(s["name"]),
                daily_cell,
                m_cell,
                m3_cell,
                tdr(f"{int(s['weighted_days']/s['cost'])}{'*' if s['buy_count'] > 1 else ''}") if s["cost"] else td("—", align="RIGHT"),
                tdr(f"{s['shares']:,}"),
                tdr(f"{alloc_pct:.1f}%"),
                tdr(f"{s['cost']:,.0f}"),
                tdr(f"{s['current']:,.0f}"),
                pnl_para(f"{pnl:+,.0f}", pnl),
                pnl_para(f"{pct:+.2f}%", pnl, bold=True),
                pnl_para(format_personal_30d(_pdf_xirr.get(code)), _pdf_xirr[code]) if _pdf_xirr.get(code) is not None else td("—", align="RIGHT"),
            ])
    avg_1m_pdf = (_pdf_1m_weighted_pct / _pdf_1m_weight_sum) if _pdf_1m_weight_sum else None
    avg_1m_cell = pnl_para(f"{avg_1m_pdf:+.2f}%", avg_1m_pdf, bold=True) if avg_1m_pdf is not None else td("—", align="RIGHT")
    avg_3m_pdf = (_pdf_3m_weighted_pct / _pdf_3m_weight_sum) if _pdf_3m_weight_sum else None
    avg_3m_cell = pnl_para(f"{avg_3m_pdf:+.2f}%", avg_3m_pdf, bold=True) if avg_3m_pdf is not None else td("—", align="RIGHT")
    fund_count = len(fund_summary)
    if _pdf_bench:
        bench_vs = total_current - total_bench_current
        bench_vs_pct = (bench_vs / total_bench_current * 100) if total_bench_current else 0.0
        fund_rows.append([
            td(f"TOTAL ({fund_count})", bold=True), td(""), td(""), avg_1m_cell, avg_3m_cell, td(""), td(""),
            tdr(f"{total_cost:,.0f}", bold=True),
            tdr(f"{total_current:,.0f}", bold=True),
            pnl_para(f"{total_pnl:+,.0f}", total_pnl, bold=True),
            pnl_para(f"{total_pnl_pct:+.2f}%", total_pnl, bold=True),
            tdr(f"{total_bench_current:,.0f}", bold=True),
            pnl_para(f"{bench_vs:+,.0f}", bench_vs, bold=True),
            pnl_para(f"{bench_vs_pct:+.2f}%", bench_vs, bold=True),
            pnl_para(format_personal_30d(portfolio_xirr), portfolio_xirr, bold=True) if portfolio_xirr is not None else td("—", align="RIGHT"),
        ])
        fund_table = Table(fund_rows, colWidths=[1.2*cm, 3.0*cm, 1.5*cm, 1.5*cm, 1.5*cm, 1.2*cm, 1.5*cm, 2.0*cm, 2.0*cm, 1.8*cm, 1.7*cm, 2.3*cm, 2.1*cm, 1.5*cm, 1.5*cm])
        pnl_pct_col = 10
    else:
        fund_rows.append([
            td(f"TOTAL ({fund_count})", bold=True), td(""), td(""), avg_1m_cell, avg_3m_cell, td(""), td(""), tdr("100.0%", bold=True),
            tdr(f"{total_cost:,.0f}", bold=True),
            tdr(f"{total_current:,.0f}", bold=True),
            pnl_para(f"{total_pnl:+,.0f}", total_pnl, bold=True),
            pnl_para(f"{total_pnl_pct:+.2f}%", total_pnl, bold=True),
            pnl_para(format_personal_30d(portfolio_xirr), portfolio_xirr, bold=True) if portfolio_xirr is not None else td("—", align="RIGHT"),
        ])
        fund_table = Table(fund_rows, colWidths=[1.3*cm, 4.7*cm, 1.7*cm, 1.7*cm, 1.7*cm, 1.2*cm, 1.8*cm, 1.6*cm, 2.5*cm, 2.5*cm, 2.3*cm, 1.7*cm, 1.5*cm])
        pnl_pct_col = 11
    bg_colors = []
    for i in range(1, len(fund_rows)-1):
        bg = colors.white if i % 2 == 1 else ALT_BG
        bg_colors.append(("BACKGROUND", (0,i), (-1,i), bg))
    fund_table.setStyle(TableStyle([
        ("BACKGROUND",   (0,0), (-1,0), HEADER_BG),
        ("TEXTCOLOR",    (0,0), (-1,0), colors.white),
        ("BACKGROUND",   (0,-1), (-1,-1), TOTAL_BG),
        ("BOX",          (0,0), (-1,-1), 0.5, colors.grey),
        ("INNERGRID",    (0,0), (-1,-1), 0.3, colors.lightgrey),
        ("VALIGN",       (0,0), (-1,-1), "MIDDLE"),
        ("TOPPADDING",   (0,0), (-1,-1), 3),
        ("BOTTOMPADDING",(0,0), (-1,-1), 3),
        ("ALIGN",        (2,0), (-1,-1), "RIGHT"),
        ("LINEBEFORE",   (pnl_pct_col,0), (pnl_pct_col,-1), 1.2, colors.grey),
        ("LINEAFTER",    (pnl_pct_col,0), (pnl_pct_col,-1), 1.2, colors.grey),
    ] + bg_colors))
    story.append(fund_table)
    story.append(Spacer(1, 0.1*cm))
    story.append(Paragraph(
        "Personal 30D: 30-day equivalent personal return based on every purchase date and amount.",
        style("xirr_note", 6, color=colors.grey),
    ))
    story.append(Spacer(1, 0.6*cm))

    # ── Top/Bottom Performers tables ───────────────────────────────────────────
    rankings = build_fund_rankings(fund_summary, fund_data, top_n=5)

    def make_side_by_side_table(rows_list):
        header = [
            th(rows_list[0][0]), th("Fund"), th("Return"), th("Cur. Value"),
            th(rows_list[1][0]), th("Fund"), th("Return"), th("Cur. Value"),
            th(rows_list[2][0]), th("Fund"), th("Return"), th("Cur. Value"),
        ]
        rows = [header]
        max_rows = 5
        for idx in range(max_rows):
            row_cells = []
            for _, term_rows in rows_list:
                if idx < len(term_rows):
                    r = term_rows[idx]
                    row_cells.extend([
                        td(r["code"], bold=True),
                        td(r["name"][:28]),
                        pnl_para(f"{r['ret_pct']:+.2f}%", r['ret_pct']),
                        tdr(f"{r['current']:,.0f}"),
                    ])
                else:
                    row_cells.extend([td(""), td(""), td(""), td("")])
            rows.append(row_cells)

        tbl = Table(rows, colWidths=[1.2*cm, 3.8*cm, 1.8*cm, 1.8*cm,
                                     1.2*cm, 3.8*cm, 1.8*cm, 1.8*cm,
                                     1.2*cm, 3.8*cm, 1.8*cm, 1.8*cm], repeatRows=1)
        tbl.setStyle(TableStyle([
            ("BACKGROUND", (0,0), (-1,0), HEADER_BG),
            ("TEXTCOLOR", (0,0), (-1,0), colors.white),
            ("BOX", (0,0), (-1,-1), 0.4, colors.black),
            ("INNERGRID", (0,0), (-1,-1), 0.3, colors.black),
            ("VALIGN", (0,0), (-1,-1), "MIDDLE"),
            ("ALIGN", (2,0), (-1,-1), "RIGHT"),
            ("ALIGN", (6,0), (6,-1), "RIGHT"),
            ("ALIGN", (10,0), (10,-1), "RIGHT"),
            ("LINEAFTER", (3,0), (3,-1), 1, colors.black),
            ("LINEAFTER", (7,0), (7,-1), 1, colors.black),
        ]))
        return tbl

    # ── Personal P&L top/bottom table ─────────────────────────────────────────
    personal = []
    for code, s in fund_summary.items():
        cost = s.get("cost", 0.0)
        current = s.get("current", 0.0)
        if cost <= 0:
            continue
        pnl_pct = (current - cost) / cost * 100
        personal.append({"code": code, "name": s.get("name", code),
                         "ret_pct": pnl_pct, "pnl": current - cost, "current": current})
    personal_sorted = sorted(personal, key=lambda x: x["ret_pct"], reverse=True)
    top5_personal    = personal_sorted[:5]
    bottom5_personal = personal_sorted[-5:][::-1]

    def make_personal_table(top_rows, bottom_rows):
        header = [
            th("My Top"), th("Fund"), th("P&L %"), th("P&L (TL)"), th("Cur. Value"),
            th("My Bottom"), th("Fund"), th("P&L %"), th("P&L (TL)"), th("Cur. Value"),
        ]
        rows = [header]
        for idx in range(max(len(top_rows), len(bottom_rows))):
            row_cells = []
            for side in (top_rows, bottom_rows):
                if idx < len(side):
                    r = side[idx]
                    row_cells.extend([
                        td(r["code"], bold=True),
                        td(r["name"][:28]),
                        pnl_para(f"{r['ret_pct']:+.2f}%", r["ret_pct"]),
                        pnl_para(f"{r['pnl']:+,.0f}", r["pnl"]),
                        tdr(f"{r['current']:,.0f}"),
                    ])
                else:
                    row_cells.extend([td(""), td(""), td(""), td(""), td("")])
            rows.append(row_cells)
        tbl = Table(rows, colWidths=[1.2*cm, 3.8*cm, 1.8*cm, 2.2*cm, 2.0*cm,
                                     1.2*cm, 3.8*cm, 1.8*cm, 2.2*cm, 2.0*cm], repeatRows=1)
        tbl.setStyle(TableStyle([
            ("BACKGROUND", (0,0), (-1,0), HEADER_BG),
            ("TEXTCOLOR",  (0,0), (-1,0), colors.white),
            ("BOX",        (0,0), (-1,-1), 0.4, colors.black),
            ("INNERGRID",  (0,0), (-1,-1), 0.3, colors.black),
            ("VALIGN",     (0,0), (-1,-1), "MIDDLE"),
            ("ALIGN",      (2,0), (-1,-1), "RIGHT"),
            ("LINEAFTER",  (4,0), (4,-1), 1, colors.black),
        ]))
        return tbl

    story.append(Paragraph("My Best / Worst (since buy — personal P&amp;L %)", heading_style))
    story.append(Spacer(1, 0.2*cm))
    story.append(make_personal_table(top5_personal, bottom5_personal))
    story.append(Spacer(1, 0.5*cm))
    story.append(Table([[td("")]], colWidths=[25.5*cm], style=TableStyle([
        ("LINEBELOW", (0,0), (-1,-1), 1, colors.black),
    ])))
    story.append(Spacer(1, 0.4*cm))

    story.append(Paragraph("Top 5 Performers", heading_style))
    story.append(Spacer(1, 0.2*cm))
    story.append(make_side_by_side_table([
        ("1M", rankings["1M"]["top"]),
        ("3M", rankings["3M"]["top"]),
        ("6M", rankings["6M"]["top"]),
    ]))
    story.append(Spacer(1, 0.4*cm))
    story.append(Table([[td("")]], colWidths=[25.5*cm], style=TableStyle([
        ("LINEBELOW", (0,0), (-1,-1), 1, colors.black),
    ])))
    story.append(Spacer(1, 0.4*cm))
    story.append(Paragraph("Bottom 5 Performers", heading_style))
    story.append(Spacer(1, 0.2*cm))
    story.append(make_side_by_side_table([
        ("1M", rankings["1M"]["bottom"]),
        ("3M", rankings["3M"]["bottom"]),
        ("6M", rankings["6M"]["bottom"]),
    ]))
    story.append(Spacer(1, 0.5*cm))

    # ── History chart ──────────────────────────────────────────────────────────
    chart_path = generate_history_chart(history)
    if chart_path:
        from reportlab.platypus import Image as RLImage
        story.append(Paragraph("Portfolio History — Last 30 Days", heading_style))
        story.append(Spacer(1, 0.2*cm))
        story.append(RLImage(chart_path, width=24*cm, height=9*cm))
        story.append(Spacer(1, 0.6*cm))

    # ── Closed positions / realized P&L ──
    closed_positions = closed_positions or []
    closed_section = []
    if closed_positions:
        closed_section.append(Paragraph("Closed Positions — Realized P&amp;L", heading_style))
        closed_section.append(Spacer(1, 0.2*cm))
        closed_rows = [[
            th("Bank"), th("Fund"), th("Buy Date"), th("Sold Date"), th("Shares"),
            th("Buy Value"), th("Sale Value"), th("Realized P&L"), th("P&L %"),
        ]]
        for position in sorted(
            closed_positions, key=lambda row: (row["sold_date"], row["code"]), reverse=True
        ):
            closed_rows.append([
                td(position["bank"]), td(position["code"], bold=True),
                td(position["buy_date"]), td(position["sold_date"]),
                tdr(f'{position["shares"]:,}'),
                tdr(f'{position["cost"]:,.0f}'),
                tdr(f'{position["proceeds"]:,.0f}'),
                pnl_para(f'{position["pnl"]:+,.0f}', position["pnl"]),
                pnl_para(f'{position["pnl_pct"]:+.2f}%', position["pnl"]),
            ])
        realized_cost = sum(row["cost"] for row in closed_positions)
        realized_proceeds = sum(row["proceeds"] for row in closed_positions)
        closed_rows.append([
            td(f"TOTAL ({len(closed_positions)})", bold=True), td(""), td(""), td(""), td(""),
            tdr(f"{realized_cost:,.0f}", bold=True),
            tdr(f"{realized_proceeds:,.0f}", bold=True),
            pnl_para(f"{realized_pnl:+,.0f}", realized_pnl, bold=True), td(""),
        ])
        closed_table = Table(
            closed_rows,
            colWidths=[2.0*cm, 1.3*cm, 2.1*cm, 2.1*cm, 1.8*cm,
                       2.6*cm, 2.6*cm, 2.8*cm, 1.8*cm],
            repeatRows=1,
        )
        closed_table.setStyle(TableStyle([
            ("BACKGROUND", (0,0), (-1,0), HEADER_BG),
            ("TEXTCOLOR", (0,0), (-1,0), colors.white),
            ("ROWBACKGROUNDS", (0,1), (-1,-2), [colors.white, ALT_BG]),
            ("BACKGROUND", (0,-1), (-1,-1), TOTAL_BG),
            ("BOX", (0,0), (-1,-1), 0.5, colors.grey),
            ("INNERGRID", (0,0), (-1,-1), 0.3, colors.lightgrey),
            ("VALIGN", (0,0), (-1,-1), "MIDDLE"),
            ("TOPPADDING", (0,0), (-1,-1), 2),
            ("BOTTOMPADDING", (0,0), (-1,-1), 2),
            ("ALIGN", (4,0), (-1,-1), "RIGHT"),
        ]))
        closed_section.append(closed_table)
        closed_section.append(Spacer(1, 0.1*cm))
        closed_section.append(Paragraph(
            "Sale values use the closest TEFAS price on or before the sold date.",
            style("sale_note", 6, color=colors.grey),
        ))
        closed_section.append(Spacer(1, 0.6*cm))

    # ── Transaction detail table ───────────────────────────────────────────────
    story.append(Paragraph("Transaction Detail", heading_style))
    story.append(Spacer(1, 0.2*cm))
    tx_header = [th("Bank"), th("Fund"), th("Buy Date"), th("Shares"),
                 th("Buy Price"), th("Today Price"),
                 th("Buy Value (TL)"), th("Cur. Value (TL)"),
                 th("P&L (TL)"), th("P&L %"), th("Sold")]
    tx_rows = [tx_header]
    for h in holdings:
        code = h["fon_kodu"]
        buy_date = h["alim_tarihi"]
        shares = h["pay_adeti"]
        bank = h["banka"]
        prices, _ = fund_data.get(code, ({}, code))
        buy_price, used_buy_date = find_closest_price(prices, buy_date)
        current_price = prices[max(prices.keys())] if prices else None
        if buy_price is not None and current_price is not None:
            cost = buy_price * shares
            current_val = current_price * shares
            pnl = current_val - cost
            pnl_pct = (pnl / cost) * 100 if cost else 0.0
            expected = buy_date
            note_parts = []
            if used_buy_date != expected:
                note_parts.append(f"*{used_buy_date}")
            if h.get("sold_date"):
                note_parts.append(h["sold_date"])
            note = "  ".join(note_parts)
            tx_rows.append([
                td(bank), td(code, bold=True), td(buy_date),
                tdr(f"{shares:,}"),
                tdr(f"{buy_price:.4f}"), tdr(f"{current_price:.4f}"),
                tdr(f"{cost:,.0f}"), tdr(f"{current_val:,.0f}"),
                pnl_para(f"{pnl:+,.0f}", pnl),
                pnl_para(f"{pnl_pct:+.2f}%", pnl),
                td(note, align="CENTER"),
            ])
        else:
            tx_rows.append([td(bank), td(code, bold=True), td(buy_date),
                            tdr(f"{shares:,}"),
                            td("?"), td("?"), td("?"), td("?"), td("?"), td("?"),
                            td("⚠ no price")])
    tx_count = len(holdings)
    tx_rows.append([
        td(f"TOTAL ({tx_count})", bold=True), td(""), td(""), td(""), td(""), td(""),
        tdr(f"{total_cost:,.0f}", bold=True),
        tdr(f"{total_current:,.0f}", bold=True),
        pnl_para(f"{total_pnl:+,.0f}", total_pnl, bold=True),
        pnl_para(f"{total_pnl_pct:+.2f}%", total_pnl, bold=True),
        td(""),
    ])
    tx_bg = []
    for i in range(1, len(tx_rows)-1):
        bg = colors.white if i % 2 == 1 else ALT_BG
        tx_bg.append(("BACKGROUND", (0,i), (-1,i), bg))
    tx_table = Table(tx_rows, colWidths=[2*cm, 1.2*cm, 2.2*cm, 1.8*cm,
                                         2.3*cm, 2.3*cm, 2.9*cm, 2.9*cm, 2.8*cm, 1.8*cm, 1.8*cm],
                    repeatRows=1)
    tx_table.setStyle(TableStyle([
        ("BACKGROUND",   (0,0), (-1,0), HEADER_BG),
        ("TEXTCOLOR",    (0,0), (-1,0), colors.white),
        ("BACKGROUND",   (0,-1), (-1,-1), TOTAL_BG),
        ("BOX",          (0,0), (-1,-1), 0.5, colors.grey),
        ("INNERGRID",    (0,0), (-1,-1), 0.3, colors.lightgrey),
        ("VALIGN",       (0,0), (-1,-1), "MIDDLE"),
        ("TOPPADDING",   (0,0), (-1,-1), 2),
        ("BOTTOMPADDING",(0,0), (-1,-1), 2),
        ("ALIGN",        (3,0), (-1,-1), "RIGHT"),
    ] + tx_bg))
    story.append(tx_table)
    if closed_section:
        story.append(Spacer(1, 0.6*cm))
        story.extend(closed_section)
    story.append(Spacer(1, 0.4*cm))
    story.append(Paragraph(
        f"Generated: {today_str} — Data source: tefas.gov.tr",
        style("footer", 7, color=colors.grey, align="CENTER")
    ))

    doc.build(story)
    print(f"PDF report generated: {filename}")
    send_email(
        filename, today_str, total_cost, total_current, total_pnl, total_pnl_pct,
        today_capital, realized_pnl,
    )


def send_email(pdf_path, date_str, total_cost, total_current, total_pnl, total_pnl_pct,
               today_capital=0.0, realized_pnl=0.0):
    import os
    import smtplib
    from email.message import EmailMessage

    smtp_host = os.environ.get("SMTP_HOST", "smtp.bilkent.edu.tr")
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    smtp_user = os.environ.get("SMTP_USER", "")
    smtp_pass = os.environ.get("SMTP_PASS", "")
    to_addr   = os.environ.get("REPORT_TO", "batur@bilkent.edu.tr")
    combined_pnl = total_pnl + realized_pnl
    combined_capital = total_cost - realized_pnl
    combined_pnl_pct = (
        combined_pnl / combined_capital * 100 if combined_capital else 0.0
    )

    if not smtp_user or not smtp_pass:
        print("Email not sent: SMTP_USER or SMTP_PASS not set.")
        return

    msg = EmailMessage()
    msg["Subject"] = f"TEFAS Portfolio Report — {date_str}"
    msg["From"]    = smtp_user
    msg["To"]      = to_addr

    history = read_history()
    prev_entries = [e for e in history if e["date"] < date_str]
    today_entry = next((e for e in history if e["date"] == date_str), None)
    day_change_str = ""
    if prev_entries:
        prev = prev_entries[-1]
        if today_entry and "daily_gain" in today_entry:
            day_delta = today_entry["daily_gain"]
            day_pct = today_entry["daily_pct"]
        else:
            new_capital = total_cost - prev["cost"]
            day_delta = (total_current - prev["current"]) - new_capital
            day_pct = (day_delta / prev["current"]) * 100 if prev["current"] else 0
        arrow = "\u2191" if day_delta >= 0 else "\u2193"
        day_change_str = (
            f"\nToday's Daily Return ({prev['date']} \u2192 {date_str}): "
            f"{arrow} {day_delta:+,.0f} TL ({day_pct:+.2f}%)\n"
        )
        if len(prev_entries) >= 2:
            prev2 = prev_entries[-2]
            if "daily_gain" in prev:
                yest_delta = prev["daily_gain"]
                yest_pct = prev["daily_pct"]
            else:
                yest_new_capital = prev["cost"] - prev2["cost"]
                yest_delta = (prev["current"] - prev2["current"]) - yest_new_capital
                yest_pct = (yest_delta / prev2["current"]) * 100 if prev2["current"] else 0
            yest_arrow = "↑" if yest_delta >= 0 else "↓"
            day_change_str += (
                f"Yesterday's Return ({prev2['date']} → {prev['date']}): "
                f"{yest_arrow} {yest_delta:+,.0f} TL ({yest_pct:+.2f}%)\n"
            )

    msg.set_content(
        f"Hi,\n\n"
        f"Please find attached the TEFAS portfolio report for {date_str}.\n\n"
        f"Open Positions P&L: {total_pnl:+,.0f} TL ({total_pnl_pct:+.2f}%)\n"
        f"Realized P&L: {realized_pnl:+,.0f} TL\n"
        f"Combined P&L: {combined_pnl:+,.0f} TL ({combined_pnl_pct:+.2f}%)"
        f"{day_change_str}\n"
    )

    with open(pdf_path, "rb") as f:
        msg.add_attachment(
            f.read(),
            maintype="application",
            subtype="pdf",
            filename=os.path.basename(pdf_path),
        )

    try:
        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.send_message(msg)
        print(f"Email sent: {to_addr}")
    except Exception as e:
        print(f"Email failed: {e}")


if __name__ == "__main__":
    main()
