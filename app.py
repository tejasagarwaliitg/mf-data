import os, time, logging, threading, webbrowser
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import numpy as np
from flask import Flask
from flask.json.provider import DefaultJSONProvider

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.config['TEMPLATES_AUTO_RELOAD'] = True

from webscraper_bp import webscraper_bp, refresh_scraper_caches
import scraper
app.register_blueprint(webscraper_bp, url_prefix='')

def _clean(obj):
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_clean(v) for v in obj]
    if isinstance(obj, (float, np.floating)) and (np.isnan(obj) or np.isinf(obj)):
        return None
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.floating):
        return float(obj)
    return obj

class SafeJSONProvider(DefaultJSONProvider):
    def dumps(self, obj, **kwargs):
        return super().dumps(_clean(obj), **kwargs)

app.json = SafeJSONProvider(app)
IST = ZoneInfo("Asia/Kolkata")

def prewarm_cache():
    logger.info("Cache refresh started at %s", datetime.now(IST))
    try:
        scraper._IS_PREWARM = True
        scraper._PE_ALLOW_NETWORK = True
        refresh_scraper_caches()
        logger.info("Cache refresh completed at %s", datetime.now(IST))
    finally:
        scraper._IS_PREWARM = False

def _daily_refresh_loop():
    while True:
        now = datetime.now(IST)
        target = now.replace(hour=16, minute=0, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        wait = (target - now).total_seconds()
        logger.info("Next scheduled refresh at %s", target.isoformat())
        time.sleep(wait)
        logger.info("Running scheduled daily refresh...")
        try:
            prewarm_cache()
        except Exception as e:
            logger.error("Scheduled refresh error: %s", e)

if __name__ == "__main__":
    logger.info("Starting Balaji Stocks | Mutual Funds (localhost:5050)...")
    threading.Thread(target=_daily_refresh_loop, daemon=True).start()
    threading.Timer(2, lambda: webbrowser.open('http://localhost:5050')).start()
    app.run(host="0.0.0.0", port=5050, debug=False)
