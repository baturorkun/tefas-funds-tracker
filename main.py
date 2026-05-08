import requests
from dotenv import load_dotenv
load_dotenv()
from datetime import datetime, timedelta
from collections import defaultdict
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

TEFAS_URL = "https://www.tefas.gov.tr/api/funds/fonFiyatBilgiGetir"
HEADERS = {
    "Content-Type": "application/json",
    "Referer": "https://www.tefas.gov.tr/",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
}


def read_portfolio(filename="fon.dat"):
    holdings = []
    with open(filename, "r", encoding="utf-8") as f:
        f.readline()  # skip header
        for line in f:
            line = line.strip()
            if not line:
                continue
            parts = line.split("\t")
            if len(parts) >= 3 and parts[2].strip():
                holdings.append({
                    "alim_tarihi": parts[0].strip(),
                    "fon_kodu": parts[1].strip(),
                    "pay_adeti": int(parts[2].strip()),
                    "banka": parts[3].strip() if len(parts) > 3 else "",
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


def save_history(date_str, total_cost, total_current, total_pnl, total_pnl_pct):
    import os
    path = os.path.join("reports", "history.tsv")
    lines = []
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    lines = [l for l in lines if l.strip() and not l.startswith(date_str)]
    lines.append(f"{date_str}\t{total_cost:.2f}\t{total_current:.2f}\t{total_pnl:.2f}\t{total_pnl_pct:.4f}\n")
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
                entries.append({
                    "date": parts[0],
                    "cost": float(parts[1]),
                    "current": float(parts[2]),
                    "pnl": float(parts[3]),
                    "pnl_pct": float(parts[4]),
                })
    entries.sort(key=lambda x: x["date"])
    return entries


def generate_history_chart(history, days=30):
    import os
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates

    if len(history) < 2:
        return None

    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    recent = [e for e in history if e["date"] >= cutoff]
    if len(recent) < 2:
        recent = history

    dates = [datetime.strptime(e["date"], "%Y-%m-%d") for e in recent]
    currents = [e["current"] / 1000 for e in recent]
    pnl_pcts = [e["pnl_pct"] for e in recent]

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(11, 4.5), sharex=True,
                                    gridspec_kw={"height_ratios": [2, 1]})
    fig.patch.set_facecolor("white")

    ax1.plot(dates, currents, color="#1f4e79", linewidth=1.8, zorder=3)
    ax1.fill_between(dates, currents, min(currents) * 0.998, alpha=0.12, color="#1f4e79")
    ax1.set_ylabel("Portfolio Value (TL x1000)")
    ax1.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"{x:,.0f}K"))
    ax1.grid(True, alpha=0.3, linestyle="--")
    ax1.set_title("Portfolio Performance — Last 30 Days", fontsize=10, pad=6)

    bar_colors = ["#1a7a1a" if p >= 0 else "#c00000" for p in pnl_pcts]
    ax2.bar(dates, pnl_pcts, color=bar_colors, width=0.7, zorder=3)
    ax2.axhline(0, color="black", linewidth=0.6)
    ax2.set_ylabel("Total Return %")
    ax2.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"{x:+.1f}%"))
    ax2.grid(True, alpha=0.3, linestyle="--")
    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%d %b"))
    fig.autofmt_xdate(rotation=30, ha="right")

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
    args = parser.parse_args()

    holdings = read_portfolio("fon.dat")
    today_str = datetime.now().strftime("%Y-%m-%d")

    # Skip if today's report already exists in history AND PDF was generated
    existing = read_history()
    pdf_path = os.path.join("reports", f"rapor_{today_str}.pdf")
    if any(e["date"] == today_str for e in existing) and os.path.exists(pdf_path):
        if not args.force:
            print(f"Report for {today_str} already exists. Skipping. (use --force to regenerate)")
            return
        print(f"--force: regenerating report for {today_str}...")

    print(f"\nTEFAS Portfolio P&L Report — {today_str}")
    print("=" * 115)

    # Unique fund codes
    fund_codes = sorted(set(h["fon_kodu"] for h in holdings))

    # Fetch prices
    print("Fetching fund prices...\n")
    fund_data = {}  # code -> (prices_dict, fund_name)
    for code in fund_codes:
        print(f"  [{code}] ", end="", flush=True)
        prices, name = get_fund_prices(code)
        fund_data[code] = (prices, name)
        if prices:
            latest_date = max(prices.keys())
            print(f"{name[:50]:<52} — {len(prices)} days, latest: {latest_date} = {prices[latest_date]:.6f}")
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
    missing_prices = []

    # For per-fund summary
    fund_summary = defaultdict(lambda: {"cost": 0.0, "current": 0.0, "shares": 0, "name": ""})

    for h in holdings:
        code = h["fon_kodu"]
        buy_date = h["alim_tarihi"]
        shares = h["pay_adeti"]
        bank = h["banka"]

        prices, fund_name = fund_data.get(code, ({}, code))

        # Buy price: exact date or nearest previous trading day
        buy_price, used_buy_date = find_closest_price(prices, buy_date)

        # Current price: latest available
        current_price, latest_date = (None, None)
        if prices:
            latest_date = max(prices.keys())
            current_price = prices[latest_date]

        if buy_price is not None and current_price is not None:
            cost = buy_price * shares
            current_val = current_price * shares
            pnl = current_val - cost
            pnl_pct = (pnl / cost) * 100 if cost else 0.0

            total_cost += cost
            total_current += current_val
            fund_summary[code]["cost"] += cost
            fund_summary[code]["current"] += current_val
            fund_summary[code]["shares"] += shares
            fund_summary[code]["name"] = fund_name

            note = f" *{used_buy_date}" if used_buy_date != buy_date else ""
            print(col.format(
                bank, code, buy_date, f"{shares:,}",
                f"{buy_price:.6f}", f"{current_price:.6f}",
                f"{cost:,.2f}", f"{current_val:,.2f}",
                f"{pnl:+,.2f}", f"{pnl_pct:+.2f}%"
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
    print(col.format(
        "TOTAL", "", "", "",
        "", "",
        f"{total_cost:,.2f}", f"{total_current:,.2f}",
        f"{total_pnl:+,.2f}", f"{total_pnl_pct:+.2f}%"
    ))

    # ── Per-fund summary ───────────────────────────────────────────────────────
    print("\n" + "=" * 115)
    print("FUND SUMMARY\n")
    col2 = "{:<5} {:<54} {:>10} {:>14} {:>14} {:>14} {:>8}"
    print(col2.format("Fund", "Fund Name", "Shares", "Buy Value", "Cur. Value", "P&L", "P&L %"))
    print("-" * 115)
    for code in sorted(fund_summary.keys(), key=lambda c: fund_summary[c]["cost"], reverse=True):
        s = fund_summary[code]
        pnl = s["current"] - s["cost"]
        pct = (pnl / s["cost"] * 100) if s["cost"] else 0.0
        print(col2.format(
            code, s["name"][:54],
            f"{s['shares']:,}",
            f"{s['cost']:,.2f}", f"{s['current']:,.2f}",
            f"{pnl:+,.2f}", f"{pct:+.2f}%"
        ))
    print("-" * 115)
    print(col2.format("TOTAL", "", "", f"{total_cost:,.2f}", f"{total_current:,.2f}", f"{total_pnl:+,.2f}", f"{total_pnl_pct:+.2f}%"))

    print(f"\n  Total Invested : {total_cost:>14,.2f} TL")
    print(f"  Current Value  : {total_current:>14,.2f} TL")
    print(f"  Total P&L      : {total_pnl:>+14,.2f} TL  ({total_pnl_pct:+.2f}%)")

    # ── Daily returns (today and yesterday) ────────────────────────────────────
    history = [e for e in existing if e["date"] < today_str]  # exclude today
    if history:
        prev = history[-1]
        today_daily_pct = ((total_current - prev["current"]) / prev["current"] * 100) if prev["current"] else 0.0
        print(f"\n  Today's Daily Return: {today_daily_pct:>+10.4f}%  ({prev['date']} → {today_str})")
        if len(history) >= 2:
            prev2 = history[-2]
            yest_daily_pct = ((prev["current"] - prev2["current"]) / prev2["current"] * 100) if prev2["current"] else 0.0
            print(f"  Yesterday's Return: {yest_daily_pct:>+10.4f}%  ({prev2['date']} → {prev['date']})")
    print()

    if missing_prices:
        print("* Rows with missing prices:")
        for m in missing_prices:
            print(f"  - {m}")
    print()

    save_history(today_str, total_cost, total_current, total_pnl, total_pnl_pct)
    write_pdf_report(
        holdings, fund_data, fund_summary,
        total_cost, total_current, total_pnl, total_pnl_pct,
        missing_prices, today_str,
    )


def write_pdf_report(holdings, fund_data, fund_summary,
                     total_cost, total_current, total_pnl, total_pnl_pct,
                     missing_prices, today_str):
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

    def th(text): return Paragraph(f"<b>{text}</b>", style("th", 7, bold=True, color=colors.white))
    def td(text, bold=False, align="LEFT"):
        return Paragraph(str(text), style("td", 7, bold=bold, align=align))
    def tdr(text, bold=False):
        return td(text, bold=bold, align="RIGHT")

    HEADER_BG  = colors.HexColor("#1f4e79")
    ALT_BG     = colors.HexColor("#dce6f1")
    TOTAL_BG   = colors.HexColor("#bdd7ee")
    GREEN_HEX = "#1a7a1a"
    RED_HEX   = "#c00000"

    def pnl_para(text, val, bold=False, align="RIGHT"):
        hex_color = GREEN_HEX if val >= 0 else RED_HEX
        return Paragraph(f'<font color="{hex_color}">{text}</font>',
                         style("pnl", 7, bold=bold, align=align))

    # ── Document ───────────────────────────────────────────────────────────────
    import os
    os.makedirs("reports", exist_ok=True)
    today_file = datetime.now().strftime("%Y-%m-%d")
    filename = os.path.join("reports", f"rapor_{today_file}.pdf")
    doc = SimpleDocTemplate(filename, pagesize=landscape(A4),
                            leftMargin=1.2*cm, rightMargin=1.2*cm,
                            topMargin=1.5*cm, bottomMargin=1.5*cm)
    story = []

    buy_dates = [datetime.strptime(h["alim_tarihi"], "%Y-%m-%d") for h in holdings]
    oldest_date = min(buy_dates).strftime("%Y-%m-%d")
    days_held = (datetime.strptime(today_str, "%Y-%m-%d") - min(buy_dates)).days

    # Title
    story.append(Paragraph("TEFAS Portfolio P&L Report", title_style))
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
    pnl_str = f"{total_pnl:+,.2f} TL  ({total_pnl_pct:+.2f}%)"
    summary_data = [
        [th(""), th("Value")],
        [td("First Buy Date"), td(f"{oldest_date}  ({days_held} days ago)")],
        [td("Total Invested"),  tdr(f"{total_cost:,.2f} TL")],
        [td("Current Value"),   tdr(f"{total_current:,.2f} TL")],
        [td("Total P&L"), pnl_para(pnl_str, total_pnl, bold=True)],
    ]
    if today_entry and prev_entries:
        prev = prev_entries[-1]
        today_daily_pct = ((today_entry["current"] - prev["current"]) / prev["current"] * 100) if prev["current"] else 0.0
        today_daily_str = f"{today_daily_pct:+.4f}%  (vs {prev['date']})"
        summary_data.append([td("Today's Daily Return"), pnl_para(today_daily_str, today_daily_pct, bold=True)])
        if len(prev_entries) >= 2:
            prev2 = prev_entries[-2]
            yest_daily_pct = ((prev["current"] - prev2["current"]) / prev2["current"] * 100) if prev2["current"] else 0.0
            yest_daily_str = f"{yest_daily_pct:+.4f}%  (vs {prev2['date']})"
            summary_data.append([td("Yesterday's Return"), pnl_para(yest_daily_str, yest_daily_pct)])
    summary_table = Table(summary_data, colWidths=[5*cm, 7*cm])
    summary_table.setStyle(TableStyle([
        ("BACKGROUND",  (0,0), (-1,0), HEADER_BG),
        ("TEXTCOLOR",   (0,0), (-1,0), colors.white),
        ("ROWBACKGROUNDS", (0,1), (-1,-1), [colors.white, ALT_BG]),
        ("BOX",         (0,0), (-1,-1), 0.5, colors.grey),
        ("INNERGRID",   (0,0), (-1,-1), 0.3, colors.lightgrey),
        ("VALIGN",      (0,0), (-1,-1), "MIDDLE"),
        ("TOPPADDING",  (0,0), (-1,-1), 3),
        ("BOTTOMPADDING",(0,0), (-1,-1), 3),
    ]))
    story.append(summary_table)
    story.append(Spacer(1, 0.6*cm))

    # ── Fund summary table ─────────────────────────────────────────────────────
    story.append(Paragraph("Fund Summary", heading_style))
    story.append(Spacer(1, 0.2*cm))
    fund_header = [th("Fund"), th("Fund Name"), th("Today %"), th("Shares"), th("Portfolio %"),
                   th("Buy Value (TL)"), th("Cur. Value (TL)"),
                   th("P&L (TL)"), th("P&L %")]
    fund_rows = [fund_header]
    for code in sorted(fund_summary.keys(), key=lambda c: fund_summary[c]["cost"], reverse=True):
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
        alloc_pct = (s["current"] / total_current * 100) if total_current else 0.0
        fund_rows.append([
            td(code, bold=True),
            td(s["name"]),
            daily_cell,
            tdr(f"{s['shares']:,}"),
            tdr(f"{alloc_pct:.1f}%"),
            tdr(f"{s['cost']:,.2f}"),
            tdr(f"{s['current']:,.2f}"),
            pnl_para(f"{pnl:+,.2f}", pnl),
            pnl_para(f"{pct:+.2f}%", pnl),
        ])
    fund_rows.append([
        td("TOTAL", bold=True), td(""), td(""), td(""), tdr("100.0%", bold=True),
        tdr(f"{total_cost:,.2f}", bold=True),
        tdr(f"{total_current:,.2f}", bold=True),
        pnl_para(f"{total_pnl:+,.2f}", total_pnl, bold=True),
        pnl_para(f"{total_pnl_pct:+.2f}%", total_pnl, bold=True),
    ])
    fund_table = Table(fund_rows, colWidths=[1.3*cm, 6.2*cm, 1.8*cm, 1.8*cm, 1.7*cm, 3.0*cm, 3.0*cm, 2.8*cm, 1.8*cm])
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
    ] + bg_colors))
    story.append(fund_table)
    story.append(Spacer(1, 0.6*cm))

    # ── History chart ──────────────────────────────────────────────────────────
    chart_path = generate_history_chart(history)
    if chart_path:
        from reportlab.platypus import Image as RLImage
        story.append(Paragraph("Portfolio History — Last 30 Days", heading_style))
        story.append(Spacer(1, 0.2*cm))
        story.append(RLImage(chart_path, width=24*cm, height=9*cm))
        story.append(Spacer(1, 0.6*cm))

    # ── Transaction detail table ───────────────────────────────────────────────
    story.append(Paragraph("Transaction Detail", heading_style))
    story.append(Spacer(1, 0.2*cm))
    tx_header = [th("Bank"), th("Fund"), th("Buy Date"), th("Shares"),
                 th("Buy Price"), th("Today Price"),
                 th("Buy Value (TL)"), th("Cur. Value (TL)"),
                 th("P&L (TL)"), th("P&L %"), th("Note")]
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
            note = f"*{used_buy_date}" if used_buy_date != expected else ""
            tx_rows.append([
                td(bank), td(code, bold=True), td(buy_date),
                tdr(f"{shares:,}"),
                tdr(f"{buy_price:.4f}"), tdr(f"{current_price:.4f}"),
                tdr(f"{cost:,.2f}"), tdr(f"{current_val:,.2f}"),
                pnl_para(f"{pnl:+,.2f}", pnl),
                pnl_para(f"{pnl_pct:+.2f}%", pnl),
                td(note, align="CENTER"),
            ])
        else:
            tx_rows.append([td(bank), td(code, bold=True), td(buy_date),
                            tdr(f"{shares:,}"),
                            td("?"), td("?"), td("?"), td("?"), td("?"), td("?"),
                            td("⚠ no price")])
    tx_rows.append([
        td("TOTAL", bold=True), td(""), td(""), td(""), td(""), td(""),
        tdr(f"{total_cost:,.2f}", bold=True),
        tdr(f"{total_current:,.2f}", bold=True),
        pnl_para(f"{total_pnl:+,.2f}", total_pnl, bold=True),
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
    story.append(Spacer(1, 0.4*cm))
    story.append(Paragraph(
        f"Generated: {today_str} — Data source: tefas.gov.tr",
        style("footer", 7, color=colors.grey, align="CENTER")
    ))

    doc.build(story)
    print(f"PDF report generated: {filename}")
    send_email(filename, today_file, total_pnl, total_pnl_pct)


def send_email(pdf_path, date_str, total_pnl, total_pnl_pct):
    import os
    import smtplib
    from email.message import EmailMessage

    smtp_host = os.environ.get("SMTP_HOST", "smtp.bilkent.edu.tr")
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    smtp_user = os.environ.get("SMTP_USER", "")
    smtp_pass = os.environ.get("SMTP_PASS", "")
    to_addr   = os.environ.get("REPORT_TO", "batur@bilkent.edu.tr")

    if not smtp_user or not smtp_pass:
        print("Email not sent: SMTP_USER or SMTP_PASS not set.")
        return

    msg = EmailMessage()
    msg["Subject"] = f"TEFAS Portfolio Report — {date_str}"
    msg["From"]    = smtp_user
    msg["To"]      = to_addr

    history = read_history()
    day_change_str = ""
    if len(history) >= 2:
        today_entry = next((e for e in history if e["date"] == date_str), None)
        if today_entry:
            prev_entries = [e for e in history if e["date"] < date_str]
            if prev_entries:
                prev = prev_entries[-1]
                day_delta = today_entry["current"] - prev["current"]
                day_pct = (day_delta / prev["current"]) * 100 if prev["current"] else 0
                arrow = "↑" if day_delta >= 0 else "↓"
                day_change_str = (
                    f"\nToday's Daily Return ({prev['date']} → {date_str}): "
                    f"{arrow} {day_delta:+,.2f} TL ({day_pct:+.2f}%)\n"
                )
                if len(prev_entries) >= 2:
                    prev2 = prev_entries[-2]
                    yest_delta = prev["current"] - prev2["current"]
                    yest_pct = (yest_delta / prev2["current"]) * 100 if prev2["current"] else 0
                    yest_arrow = "↑" if yest_delta >= 0 else "↓"
                    day_change_str += (
                        f"Yesterday's Return ({prev2['date']} → {prev['date']}): "
                        f"{yest_arrow} {yest_delta:+,.2f} TL ({yest_pct:+.2f}%)\n"
                    )

    msg.set_content(
        f"Hi,\n\n"
        f"Please find attached the TEFAS portfolio report for {date_str}.\n\n"
        f"Total P&L: {total_pnl:+,.2f} TL ({total_pnl_pct:+.2f}%)"
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
