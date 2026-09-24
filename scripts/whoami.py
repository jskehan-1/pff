import os
import requests
from dotenv import load_dotenv

load_dotenv()
api_key = os.getenv("PFF_API_KEY")

resp = requests.get(
    "https://api.pff.com/v1/auth/whoami",
    headers={"Authorization": f"Bearer {api_key}"}
)
print(resp.status_code)
print(resp.json())
