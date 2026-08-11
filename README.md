# TEFAS Portfolio Tracker

> **Turkey only** — This app is built exclusively for the Turkish mutual fund system. It pulls data from [TEFAS](https://www.tefas.gov.tr) (Türkiye Elektronik Fon Alım Satım Platformu), the official platform where all Turkish mutual funds are traded. It will not work with funds from other countries.

A daily portfolio profit/loss reporter for Turkish mutual funds. Fetches live fund prices from TEFAS, generates a PDF report, and sends it via email — fully automated with Docker and a cron scheduler.

## Features

- Fetches live fund prices from [tefas.gov.tr](https://www.tefas.gov.tr)
- Calculates P&L per transaction and per fund
- Shows today's and yesterday's daily return (%)
- Shows each fund's allocation weight in the portfolio (%)
- Highlights daily gainers/losers ≥1% in bold green/red
- Generates a formatted PDF report with:
  - Portfolio summary (total invested, current value, P&L, daily returns)
  - Bank totals (invested amount, current value, P&L, and portfolio share)
  - Fund summary table with daily return and portfolio weight
  - Transaction detail table
  - 30-day portfolio performance chart
- Sends the PDF via email (SMTP)
- Persists daily history in `reports/history.tsv`
- Persists TEFAS fund-level stats in `reports/fund_stats.tsv`
- Docker + Ofelia scheduler for fully automated daily runs (weekdays at 11:00)

## Setup

### 1. Clone and configure

```bash
git clone https://github.com/yourusername/tefas.git
cd tefas
cp fon.dat.sample fon.dat
```

Edit `fon.dat` with your holdings:

```
Date        Code    Amount  Bank
2026-01-10  TLY     24      Garanti
2026-03-17  VPS     39412   YapiKredi
```

### 2. Create `.env`

```env
SMTP_HOST=smtp.example.com
SMTP_PORT=587
SMTP_USER=you@example.com
SMTP_PASS=yourpassword
REPORT_TO=you@example.com
```

### 3. Run manually

```bash
pip install requests reportlab python-dotenv matplotlib
python main.py

# Force re-run if today's report already exists
python main.py --force

# Run for a specific past date
python main.py --date 2026-05-07

# Run for a specific date and force re-generation
python main.py --date 2026-05-07 --force
```

> **Weekend guard** — The script skips automatically on Saturdays and Sundays since TEFAS is closed. Use `--force` to override (e.g. for backfilling a past weekend date).

### 4. Run with Docker

```bash
docker compose --profile job run --rm tefas
```

### 5. Automated daily schedule (Docker)

```bash
docker compose up -d scheduler
```

This starts the Ofelia scheduler which runs the report every weekday at **11:00**.

## Output

Reports are saved to the `reports/` directory:

```
reports/
  rapor_2026-05-08.pdf
  history.tsv
  fund_stats.tsv
```

`fund_stats.tsv` columns are:

```
date    fund_code    participant_count    fund_size_tl    outstanding_shares    market_share_pct
```

## Requirements

- Python 3.13+
- `requests`, `reportlab`, `python-dotenv`, `matplotlib`
- SMTP credentials for email delivery
- Docker + Docker Compose (optional, for scheduled runs)
