import requests
import json
import re
import copy

RAIDSID = "s%3A6687024977739776%3AopXbRND5Y8KWEZ6BtfUUkQ.1C9bmPhC1tG0A24NV1YdfZC4QhuN0sTOJH0q2uGnKYc"

session = requests.Session()
session.cookies.set("raidsid", RAIDSID, domain="www.raidbots.com")
session.headers.update({
    "Content-Type": "application/json",
    "Referer":      "https://www.raidbots.com/simbot/droptimizer",
    "Origin":       "https://www.raidbots.com",
    "User-Agent":   "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36 OPR/127.0.0.0",
})
 
payload = json.load(open(r"c:\Users\Xiant\Documents\Projects\auto_sim\payload_debug.json"))
payload.pop("droptimizerItems", None)
payload.pop("armory", None)
payload.pop("character", None)
 
# Also strip equipped from droptimizer
payload["droptimizer"].pop("equipped", None)
 
print("droptimizer keys:", list(payload["droptimizer"].keys()))
print(f"Payload size: {len(json.dumps(payload))} bytes")
resp = session.post("https://www.raidbots.com/sim", json=payload, timeout=60)
print("Status:", resp.status_code)
print("Body:", resp.text[:300])