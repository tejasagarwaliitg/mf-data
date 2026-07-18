import os, sys, time, logging, threading, webbrowser
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

def _windows_startup_install():
    if sys.platform != "win32":
        print("Windows only. On macOS, add to Login Items in System Settings.")
        return
    import shutil
    startup = os.path.join(os.environ.get("APPDATA", ""), r"Microsoft\Windows\Start Menu\Programs\Startup")
    if not os.path.isdir(startup):
        print("Startup folder not found:", startup)
        return
    src = os.path.join(os.path.dirname(os.path.abspath(__file__)), "start.bat")
    shutil.copy2(src, os.path.join(startup, "Balaji MF.bat"))
    print("Installed to Windows Startup. Balaji MF will start on boot.")


def _windows_startup_remove():
    if sys.platform != "win32":
        return
    startup = os.path.join(os.environ.get("APPDATA", ""), r"Microsoft\Windows\Start Menu\Programs\Startup")
    dst = os.path.join(startup, "Balaji MF.bat")
    try:
        os.remove(dst)
        print("Removed from Windows Startup")
    except FileNotFoundError:
        print("Not installed in Startup")


def _startup_cache_check():
    """If cached fund data is from a previous trading day, refresh now."""
    now = datetime.now(IST)
    if now.weekday() >= 5:
        return  # weekend, skip
    any_stale = False
    for slug, entry in list(scraper._FUND_DATA_CACHE.items()):
        ts = entry.get("ts", 0)
        cached_dt = datetime.fromtimestamp(ts)
        if cached_dt.date() != now.date():
            any_stale = True
            break
    if any_stale:
        logger.info("Stale cache detected from %s, refreshing...", cached_dt.date())
        threading.Thread(target=prewarm_cache, daemon=True).start()


if __name__ == "__main__":
    if "--install-startup" in sys.argv:
        _windows_startup_install()
        sys.exit(0)
    if "--remove-startup" in sys.argv:
        _windows_startup_remove()
        sys.exit(0)
    logger.info("Starting Balaji Stocks | Mutual Funds (localhost:5050)...")
    scraper._FUND_INDEX = scraper._load_fund_index_from_disk()
    _startup_cache_check()
    threading.Thread(target=_daily_refresh_loop, daemon=True).start()
    if "--startup" not in sys.argv:
        threading.Timer(2, lambda: webbrowser.open('http://localhost:5050')).start()
    app.run(host="0.0.0.0", port=5050, debug=False)
