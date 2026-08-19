import requests

HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json",
    "Content-Type": "application/json",
    "Referer": "https://www.tefas.gov.tr/",
}

payload = {"fonKodu": "IVY"}

# Try some endpoints
endpoints = [
    "https://www.tefas.gov.tr/api/funds/fonVarlikDagilimi",
    "https://www.tefas.gov.tr/api/funds/fonPortfoyDagilimi",
    "https://www.tefas.gov.tr/api/funds/fonTarihselVeriler",
]

for url in endpoints:
    try:
        r = requests.post(url, json=payload, headers=HEADERS)
        print(f"\n--- {url} ---")
        print(r.text[:500])
    except Exception as e:
        print(f"Error {url}: {e}")

