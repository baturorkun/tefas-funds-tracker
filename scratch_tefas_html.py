import requests
import json
import re

url = "https://www.tefas.gov.tr/FonAnaliz.aspx?FonKod=MAC"
headers = {"User-Agent": "Mozilla/5.0"}
resp = requests.get(url, headers=headers)
html = resp.text

# Try to find ChartData or Varlık Dağılımı
if "Varlık Dağılımı" in html:
    print("Found 'Varlık Dağılımı' in HTML")

# Search for any JSON looking objects that might contain the pie chart data
# e.g., 'chartData' or something similar
matches = re.findall(r'\[\{"name":.*?\}\]', html)
for m in matches:
    if "Kıymetli Madenler" in m or "Hisse Senedi" in m or "Ters Repo" in m or "Takasbank" in m:
        print("Found possible allocation data:")
        print(m[:200])

