# vivino_proxy_1.py
from flask import Flask
import requests

app = Flask(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Linux; Android 10) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "it-IT,it;q=0.9",
    "Connection": "keep-alive"
}

@app.route('/wine/<int:wine_id>')
def get_wine(wine_id):
    r = requests.get(
        f"https://www.vivino.com/wines/{wine_id}",
        headers=HEADERS, timeout=10
    )
    return (r.text, r.status_code, {"Content-Type": "text/html"})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5055)
