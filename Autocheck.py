import requests
s = requests.Session()
s.cookies.set("raidsid", "s%3A6687024977739776%3AopXbRND5Y8KWEZ6BtfUUkQ.1C9bmPhC1tG0A24NV1YdfZC4QhuN0sTOJH0q2uGnKYc", domain="www.raidbots.com")
s.headers["User-Agent"] = "Mozilla/5.0"
r = s.get("https://www.raidbots.com/api/user")
print("Status:", r.status_code)
print("Body:", r.text[:300])