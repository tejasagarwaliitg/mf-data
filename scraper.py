import re
import json
import csv
import io
import os
import time as time_module
import math
from datetime import datetime, time, date, timedelta, timezone
import requests
import concurrent.futures
from urllib.parse import quote
from collections import defaultdict
from bs4 import BeautifulSoup



ON_VERCEL = os.environ.get("VERCEL") is not None
ON_RENDER = os.environ.get("RENDER") is not None
ON_HF = os.environ.get("SPACE_ID") is not None
_VERCEL_TIMEOUT = 2 if ON_VERCEL else (6 if ON_RENDER else 20)
_MFAPI_TIMEOUT = 4 if ON_VERCEL else 20

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

GROWW_BASE = "https://groww.in"

_CACHE_DIR = "/tmp/.cache" if (ON_RENDER or ON_HF or os.environ.get("VERCEL")) else os.path.join(os.path.dirname(__file__), ".cache")
os.makedirs(_CACHE_DIR, exist_ok=True)

_FUND_DATA_CACHE = {}  # slug -> {"data": dict, "ts": float(epoch)}
_PE_CACHE_FILE = os.path.join(_CACHE_DIR, "stock_pe_cache.json")

_PE_CACHE = {}
_FUND_INDEX = None  # lazy-loaded list of {name, slug}
_NSE_MAP = None  # lazy-loaded company_name -> NSE symbol
_STOCK_PE_CACHE = {}  # company_name -> {"pe": float|None, "pb": float|None, "ts": "YYYY-MM-DDTHH:MM:SS"}

_CATEGORY_PE_CACHE = {}  # category_name -> {"pes": [float], "avg": float|None}
_CATEGORY_PE_FILE = os.path.join(_CACHE_DIR, "category_pe_cache.json")

_NAV_CACHE = {}  # slug -> [[date_str, nav_float], ...] sorted chronologically
_NAV_CACHE_FILE = os.path.join(_CACHE_DIR, "nav_cache.json")
_FUND_DATA_CACHE_FILE = os.path.join(_CACHE_DIR, "fund_data_cache.json")
_FUND_INDEX_FILE = os.path.join(_CACHE_DIR, "fund_index.json")


def _load_nav_cache():
    global _NAV_CACHE
    try:
        with open(_NAV_CACHE_FILE) as f:
            raw = json.load(f)
        if raw:
            _NAV_CACHE.clear()
            _NAV_CACHE.update(raw)
    except (FileNotFoundError, json.JSONDecodeError):
        pass


def _nav_cache_to_nav_data(entries):
    result = []
    for date_str, nav in entries:
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d")
            result.append((dt, float(nav)))
        except (ValueError, TypeError):
            continue
    result.sort(key=lambda x: x[0])
    return result


def _merge_nav_into_cache(slug, new_nav_data):
    """Merge mfapi NAV data into cache: add new entries, trim oldest to match original length."""
    existing = _NAV_CACHE.get(slug, [])
    if not existing:
        _NAV_CACHE[slug] = [[d.strftime("%Y-%m-%d"), round(float(n), 2)] for d, n in new_nav_data]
        return
    existing_dates = {e[0] for e in existing}
    new_entries = []
    for d, n in new_nav_data:
        ds = d.strftime("%Y-%m-%d")
        if ds not in existing_dates:
            new_entries.append([ds, round(float(n), 2)])
    if not new_entries:
        return
    merged = existing + new_entries
    merged.sort(key=lambda x: x[0])
    over = len(merged) - len(existing)
    if over > 0:
        merged = merged[over:]
    _NAV_CACHE[slug] = merged
    _save_nav_cache()


def _save_nav_cache():
    try:
        with open(_NAV_CACHE_FILE, "w") as f:
            json.dump(_NAV_CACHE, f, indent=2)
    except Exception:
        pass


def _load_fund_data_cache():
    global _FUND_DATA_CACHE
    try:
        with open(_FUND_DATA_CACHE_FILE) as f:
            raw = json.load(f)
        if raw:
            _FUND_DATA_CACHE.clear()
            _FUND_DATA_CACHE.update(raw)
    except (FileNotFoundError, json.JSONDecodeError):
        pass


def _save_fund_data_cache():
    try:
        with open(_FUND_DATA_CACHE_FILE, "w") as f:
            json.dump(_FUND_DATA_CACHE, f, indent=2)
    except Exception:
        pass


_load_nav_cache()
_load_fund_data_cache()


def _load_category_pe_cache():
    global _CATEGORY_PE_CACHE
    loaded = False
    for path in [_CATEGORY_PE_FILE, os.path.join(os.path.dirname(__file__), "category_pe_cache.json")]:
        try:
            with open(path) as f:
                raw = json.load(f)
            if raw:
                _CATEGORY_PE_CACHE.clear()
                _CATEGORY_PE_CACHE.update(raw)
                loaded = True
                break
        except (FileNotFoundError, json.JSONDecodeError):
            pass
    if not loaded:
        _CATEGORY_PE_CACHE.clear()


def _save_category_pe_cache():
    try:
        with open(_CATEGORY_PE_FILE, "w") as f:
            json.dump(_CATEGORY_PE_CACHE, f, indent=2)
    except Exception:
        pass


_load_category_pe_cache()


def _cache_entry(value):
    return {"pe": value, "ts": datetime.now().isoformat(timespec="seconds")}

def _cache_entry_pb(pb_value):
    """Create a cache entry with PB stored alongside PE."""
    return {"pe": None, "pb": pb_value, "ts": datetime.now().isoformat(timespec="seconds")}


def _is_cache_stale(entry):
    """Refresh if cached before 9:30 AM today and now is past 9:30 AM.
    On Vercel/Render, never mark stale since live refresh isn't available."""
    if ON_VERCEL or ON_RENDER:
        return False
    if entry is None:
        return True
    ts_str = entry.get("ts") if isinstance(entry, dict) else None
    if not ts_str:
        return True
    try:
        cached_dt = datetime.fromisoformat(ts_str)
    except (ValueError, TypeError):
        return True
    today = date.today()
    if cached_dt.date() != today:
        # Entry from a previous day — refresh it
        return True
    market_open = time(9, 30)
    if cached_dt.time() < market_open and datetime.now().time() > market_open:
        return True
    return False


def _load_pe_cache():
    global _STOCK_PE_CACHE
    _STOCK_PE_CACHE.clear()
    for path in [_PE_CACHE_FILE, os.path.join(os.path.dirname(__file__), "stock_pe_cache.json")]:
        try:
            with open(path) as f:
                raw = json.load(f)
            for k, v in raw.items():
                if isinstance(v, dict):
                    if "pe" not in v:
                        v["pe"] = None
                    _STOCK_PE_CACHE[k] = v
                else:
                    _STOCK_PE_CACHE[k] = _cache_entry(v)
            if _STOCK_PE_CACHE:
                break
        except (FileNotFoundError, json.JSONDecodeError):
            pass


def _save_pe_cache():
    try:
        with open(_PE_CACHE_FILE, "w") as f:
            json.dump(_STOCK_PE_CACHE, f, indent=2)
    except Exception:
        pass


_load_pe_cache()


_NSE_MAP_LOADED = False

def _load_nse_map():
    global _NSE_MAP, _NSE_MAP_LOADED
    if _NSE_MAP_LOADED:
        return _NSE_MAP
    nse_cache_file = os.path.join(_CACHE_DIR, "nse_map.json")
    # Try loading from disk cache first (24h TTL)
    try:
        if os.path.exists(nse_cache_file):
            with open(nse_cache_file) as f:
                cached = json.load(f)
            ts = cached.get("ts", 0)
            if time_module.time() - ts < 86400:
                _NSE_MAP = cached.get("map", {})
                _NSE_MAP_LOADED = True
                return _NSE_MAP
    except Exception:
        pass
    # Download fresh
    try:
        _NSE_MAP = {}
        resp = requests.get(
            "https://archives.nseindia.com/content/equities/EQUITY_L.csv",
            headers=HEADERS, timeout=10
        )
        reader = csv.DictReader(io.StringIO(resp.text))
        for row in reader:
            name = row["NAME OF COMPANY"].strip().upper()
            name = _normalize_name(name)
            _NSE_MAP[name] = row["SYMBOL"]
        # Save to disk cache
        try:
            with open(nse_cache_file, "w") as f:
                json.dump({"map": _NSE_MAP, "ts": time_module.time()}, f)
        except Exception:
            pass
    except Exception as e:
        print(f"Warning: failed to load NSE map: {e}")
        if not isinstance(_NSE_MAP, dict):
            _NSE_MAP = {}
    _NSE_MAP_LOADED = True
    return _NSE_MAP


def _normalize_name(name):
    name = name.upper()
    name = name.replace(" LIMITED", " ")
    name = name.replace(" LTD", " ")
    name = name.replace(".", "")
    name = name.replace("'", "")
    name = re.sub(r"\bTHE\b", "", name)
    name = re.sub(r"[^A-Z0-9 &/,-]", " ", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name


def _normalize_name_aggressive(name):
    """More aggressive normalization that strips corporate suffixes.
    Used as fallback when basic normalization doesn't match."""
    name = _normalize_name(name)
    for word in [" COMPANY", " CORPORATION", " ENTERPRISES", " HOLDINGS",
                  " INDUSTRIES", " INDIA", " GROUP", " PRIVATE", " LIMITED",
                  " OF", " AND", " THE"]:
        name = name.replace(word, " ")
    name = name.replace(" & ", " ")
    name = re.sub(r"\s+", " ", name).strip()
    return name


def _lookup_pe_cache(company_name):
    """Look up company in _STOCK_PE_CACHE with name normalization fallback."""
    cached = _STOCK_PE_CACHE.get(company_name)
    if cached is not None:
        return cached
    norm = _normalize_name(company_name)
    for key, val in _STOCK_PE_CACHE.items():
        if isinstance(val, dict) and _normalize_name(key) == norm:
            _STOCK_PE_CACHE[company_name] = val
            return val
    agg = _normalize_name_aggressive(company_name)
    if agg != norm:
        for key, val in _STOCK_PE_CACHE.items():
            if isinstance(val, dict) and _normalize_name_aggressive(key) == agg:
                _STOCK_PE_CACHE[company_name] = val
                return val
    return None


def _company_to_symbol(company_name):
    nse_map = _load_nse_map()
    norm = _normalize_name(company_name.upper())
    if norm in nse_map:
        return nse_map[norm]
    # Word-based fallback: check if all words in company_name appear in any NSE name
    words = set(norm.split())
    for nse_name, sym in nse_map.items():
        nse_words = set(nse_name.split())
        if words and words <= nse_words:
            return sym
    return None


def _guess_nse_symbol(company_name):
    """Derive a plausible NSE symbol from a company name when the map lookup fails."""
    name = company_name.upper().strip()
    # Remove common suffixes
    for suffix in [" LTD", " LIMITED", " LTD.", " LIMITED.", " PVT LTD", " PRIVATE LIMITED",
                   " -", " & CO", " EQ", " EQ NEW", " FV RS", " FV RE"]:
        idx = name.find(suffix)
        if idx >= 0:
            name = name[:idx]
    # Remove common words
    for word in ["THE ", "COMPANY ", "CORPORATION ", "CORP ", "INDIA ",
                 "INDUSTRIES ", "TECHNOLOGIES ", "ENTERPRISES ", "HOLDINGS ",
                 "VENTURES ", "TECH ", "AND ", "& ", "SELECT ", "TRUST "]:
        name = name.replace(" " + word.strip(), " ")
    # Handle short generic first words
    parts = name.split()
    short_generic = {"DR", "MR", "MRS", "MS", "SMT", "SHRI", "M/S", "THE"}
    if parts and parts[0] in short_generic:
        parts = parts[1:]
        name = " ".join(parts)
    name = name.strip()
    # Take first word if multiple
    name = name.split()[0] if name.split() else name
    # Remove non-alphanumeric
    name = re.sub(r"[^A-Z0-9]", "", name)
    if 2 <= len(name) <= 15:
        return name
    return None


def _get_stock_pe_screener(company_name, force_refresh=False):
    cached = _STOCK_PE_CACHE.get(company_name)
    if cached is not None and not force_refresh and not _is_cache_stale(cached):
        return cached["pe"]
    symbol = _company_to_symbol(company_name)
    if not symbol:
        _STOCK_PE_CACHE[company_name] = _cache_entry(None)
        return None
    url = f"https://www.screener.in/company/{symbol}/consolidated/"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=_VERCEL_TIMEOUT)
        if resp.status_code != 200:
            _STOCK_PE_CACHE[company_name] = _cache_entry(None)
            return None
        soup = BeautifulSoup(resp.text, "html.parser")

        # Prefer the direct P/E value from screener.in's company ratios
        pe = None
        for li in soup.select(".company-ratios .flex"):
            name_el = li.select_one(".name")
            if name_el and "P/E" in name_el.get_text(strip=True):
                pe_str = li.select_one(".number")
                if pe_str:
                    try:
                        pe = float(pe_str.get_text(strip=True).replace(",", ""))
                    except ValueError:
                        pass
                break
        if pe is not None and pe > 0:
            _STOCK_PE_CACHE[company_name] = _cache_entry(pe)
            # Also try to get P/B from the same page
            for li in soup.select(".company-ratios .flex"):
                name_el = li.select_one(".name")
                if name_el and "P/B" in name_el.get_text(strip=True):
                    pb_str = li.select_one(".number")
                    if pb_str:
                        try:
                            pb_val = float(pb_str.get_text(strip=True).replace(",", ""))
                            _STOCK_PE_CACHE[company_name] = {"pe": pe, "pb": pb_val, "mcap": None, "ts": datetime.now().isoformat(timespec="seconds")}
                        except ValueError:
                            pass
                    break
            if len(_STOCK_PE_CACHE) % 10 == 0:
                _save_pe_cache()
            return pe

        # Fallback: compute PE from price / sum(last 4 quarters EPS)
        price_el = soup.select_one(".company-ratios .flex:nth-child(2) .number")
        if not price_el:
            for li in soup.select(".company-ratios .flex"):
                name_el = li.select_one(".name")
                if name_el and "Current Price" in name_el.get_text(strip=True):
                    price_el = li.select_one(".number")
                    break
        eps_row = None
        quarter_cols = 0
        quarters_section = soup.select_one("#quarters")
        if quarters_section:
            table = quarters_section.select_one("table")
            if table:
                # Count how many header cells look like quarter columns (e.g. "Mar 2025")
                header_tr = table.select_one("thead tr") or table.select_one("tr")
                if header_tr:
                    header_cells = header_tr.select("th, td")
                    quarter_cols = sum(1 for c in header_cells
                                       if re.search(r'\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{4}\b', c.get_text(strip=True)))
                for tr in table.select("tr"):
                    tds = tr.select("td")
                    if tds and "EPS" in tds[0].get_text(strip=True).upper():
                        vals = [td.get_text(strip=True) for td in tds[1:]]
                        eps_row = vals
                        break
        if price_el and eps_row and len(eps_row) >= 4:
            price_str = price_el.get_text(strip=True).replace(",", "")
            price = float(price_str)
            # Only use EPS values from actual quarter columns (ignore TTM/Growth suffix columns)
            last4 = []
            eps_count = quarter_cols if quarter_cols >= 4 else len(eps_row)
            for v in eps_row[:eps_count][:4]:
                try:
                    last4.append(float(v.replace(",", "")))
                except ValueError:
                    continue
            if len(last4) == 4 and price > 0 and sum(last4) > 0:
                pe = round(price / sum(last4), 2)
                _STOCK_PE_CACHE[company_name] = _cache_entry(pe)
                if len(_STOCK_PE_CACHE) % 10 == 0:
                    _save_pe_cache()
                # Also try to get P/B from the same page
                pb_val = None
                bvps_el = None
                for li in soup.select(".company-ratios .flex"):
                    name_el = li.select_one(".name")
                    if name_el and "Book Value" in name_el.get_text(strip=True):
                        bvps_el = li.select_one(".number")
                        break
                if bvps_el and price > 0:
                    try:
                        bvps = float(bvps_el.get_text(strip=True).replace(",", ""))
                        if bvps > 0:
                            pb_val = round(price / bvps, 2)
                    except (ValueError, AttributeError):
                        pass
                _STOCK_PE_CACHE[company_name] = {"pe": pe, "pb": pb_val, "mcap": None, "ts": datetime.now().isoformat(timespec="seconds")}
                if len(_STOCK_PE_CACHE) % 10 == 0:
                    _save_pe_cache()
                return pe
        _STOCK_PE_CACHE[company_name] = _cache_entry(None)
        return None
    except Exception:
        _STOCK_PE_CACHE[company_name] = _cache_entry(None)
        return None



_BENCHMARK_CACHE_FILE = os.path.join(_CACHE_DIR, "benchmark_cache.json")
_BENCHMARK_BUNDLED_FILE = os.path.join(os.path.dirname(__file__), "benchmark_cache.json")
_MCAP_RANKING_FILE = os.path.join(_CACHE_DIR, "mcap_rankings.json")
_MCAP_RANKINGS_BUNDLED = os.path.join(_CACHE_DIR, "mcap_rankings_cache.json")
_PE_ALLOW_NETWORK = True      # Allow PE during user requests (with 10s timeout); PB only during prewarm
_IS_PREWARM = False            # True during the scheduled prewarm run
_MCAP_RANKINGS = {}          # company_name -> "large_cap"|"mid_cap"|"small_cap"
_MCAP_RANKINGS_UPDATED = None  # ISO timestamp string from cache


def _mcap_cache_needs_refresh():
    """Return True if cache should be refreshed (1st of month after 12pm)."""
    global _MCAP_RANKINGS_UPDATED
    if not _MCAP_RANKINGS_UPDATED:
        return True
    try:
        updated_dt = datetime.fromisoformat(_MCAP_RANKINGS_UPDATED)
        now = datetime.now()
        if now.day == 1 and now.hour >= 12:
            return updated_dt.day != 1 or updated_dt.month != now.month or updated_dt.year != now.year
    except Exception:
        return True
    return False


def _load_mcap_rankings():
    global _MCAP_RANKINGS, _MCAP_RANKINGS_UPDATED
    try:
        with open(_MCAP_RANKING_FILE) as f:
            data = json.load(f)
        _MCAP_RANKINGS = data.get("rankings", {})
        _MCAP_RANKINGS_UPDATED = data.get("updated")
        return
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    try:
        with open(_MCAP_RANKINGS_BUNDLED) as f:
            data = json.load(f)
        _MCAP_RANKINGS = data.get("rankings", {})
        _MCAP_RANKINGS_UPDATED = data.get("updated")
    except (FileNotFoundError, json.JSONDecodeError):
        _MCAP_RANKINGS = {}
        _MCAP_RANKINGS_UPDATED = None


def _save_mcap_rankings():
    global _MCAP_RANKINGS_UPDATED
    _MCAP_RANKINGS_UPDATED = datetime.now().isoformat(timespec="seconds")
    data = {
        "rankings": _MCAP_RANKINGS,
        "updated": _MCAP_RANKINGS_UPDATED,
    }
    try:
        with open(_MCAP_RANKING_FILE, "w") as f:
            json.dump(data, f)
    except Exception:
        pass
    try:
        with open(_MCAP_RANKINGS_BUNDLED, "w") as f:
            json.dump(data, f)
    except Exception:
        pass


_NSE_INDEX_API_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.nseindia.com/",
}


def _fetch_nse_index_symbols(index_display_name):
    """Fetch constituent symbol list for an NSE index via the NSE API.
    Returns list of NSE symbols (str) or None on failure."""
    try:
        sess = requests.Session()
        sess.headers.update(_NSE_INDEX_API_HEADERS)
        sess.get("https://www.nseindia.com", headers=_NSE_INDEX_API_HEADERS, timeout=_VERCEL_TIMEOUT)
        time_module.sleep(0.3)
        url = f"https://www.nseindia.com/api/equity-stockIndices?index={quote(index_display_name)}"
        resp = sess.get(url, timeout=_VERCEL_TIMEOUT)
        if resp.status_code != 200:
            return None
        data = resp.json()
        symbols = []
        for item in data.get("data", []):
            sym = (item.get("symbol") or "").strip().upper()
            if sym:
                symbols.append(sym)
        return symbols if symbols else None
    except Exception:
        return None


def _rebuild_mcap_rankings():
    """Fetch NSE index constituents and rebuild _MCAP_RANKINGS.
    Large cap = Nifty 100, Mid cap = Nifty Midcap 150, Small cap = Nifty Smallcap 250."""
    global _MCAP_RANKINGS
    _MCAP_RANKINGS = {}
    _load_nse_map()
    sym_to_company = {v: k for k, v in _NSE_MAP.items()} if _NSE_MAP else {}

    def classify(display_name, category):
        symbols = _fetch_nse_index_symbols(display_name)
        if symbols:
            for sym in symbols:
                company = sym_to_company.get(sym)
                if company and company not in _MCAP_RANKINGS:
                    _MCAP_RANKINGS[company] = category

    classify("NIFTY 100", "large_cap")
    classify("NIFTY MIDCAP 150", "mid_cap")
    classify("NIFTY SMALLCAP 250", "small_cap")

    if _MCAP_RANKINGS:
        _save_mcap_rankings()


def _get_mcap_category(company_name):
    """Classify a company's market cap.
    Primary: pre-built mcap rankings (top 100 large, next 150 mid, rest small).
    Fallback: yfinance mcap with thresholds."""
    if not company_name:
        return "other"
    normalized = _normalize_name(company_name.upper())
    cat = _MCAP_RANKINGS.get(normalized)
    if cat:
        return cat
    aggressive = _normalize_name_aggressive(company_name.upper())
    if aggressive != normalized:
        cat = _MCAP_RANKINGS.get(aggressive)
        if cat:
            _MCAP_RANKINGS[normalized] = cat
            return cat
    if ON_VERCEL or ON_RENDER or not _MCAP_RANKINGS:
        _MCAP_RANKINGS[normalized] = "other"
        return "other"
    mcap = _fetch_stock_mcap(company_name)
    if mcap is not None and mcap > 0:
        cr = mcap / 1e7
        SEBI_AVG_MCAP = _estimate_sebi_thresholds()
        if cr >= SEBI_AVG_MCAP.get("large", 15000):
            cat = "large_cap"
        elif cr >= SEBI_AVG_MCAP.get("mid", 4000):
            cat = "mid_cap"
        else:
            cat = "small_cap"
        _MCAP_RANKINGS[normalized] = cat
        return cat
    return "other"


_SEBI_THRESHOLD_CACHE = {}

def _estimate_sebi_thresholds():
    """Estimate SEBI mcap thresholds from cached Nifty 100 / Midcap 150 data.
    Returns dict with 'large' and 'mid' threshold values in crores."""
    global _SEBI_THRESHOLD_CACHE
    if _SEBI_THRESHOLD_CACHE:
        return _SEBI_THRESHOLD_CACHE
    mcaps = []
    for company in _MCAP_RANKINGS:
        entry = _STOCK_PE_CACHE.get(company)
        if isinstance(entry, dict):
            m = entry.get("mcap")
            if m and m > 0:
                mcaps.append(m / 1e7)
    if len(mcaps) < 50:
        _SEBI_THRESHOLD_CACHE = {"large": 15000, "mid": 4000}
    else:
        mcaps.sort(reverse=True)
        large_threshold = mcaps[99] if len(mcaps) > 100 else mcaps[-1]
        mid_threshold = mcaps[249] if len(mcaps) > 250 else mcaps[-1]
        _SEBI_THRESHOLD_CACHE = {"large": large_threshold, "mid": mid_threshold}
    return _SEBI_THRESHOLD_CACHE


# ponytail: hardcoded overrides for funds renamed on Groww (slug ≠ actual name)
_FUND_NAME_CORRECTIONS = {
    "icici-prudential-top-100-fund": "ICICI Prudential Large & Mid Cap Fund",
}

def _load_fund_index():
    global _FUND_INDEX
    if _FUND_INDEX is not None:
        return _FUND_INDEX
    _FUND_INDEX = _load_fund_index_from_disk()
    if _FUND_INDEX is not None:
        return _FUND_INDEX
    try:
        resp = requests.get("https://groww.in/mf-sitemap.xml", headers=HEADERS, timeout=20)
        import xml.etree.ElementTree as ET
        root = ET.fromstring(resp.content)
        ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
        urls = root.findall(".//sm:loc", ns)
        seen = set()
        index = []
        for u in urls:
            slug_match = re.search(r"/mutual-funds/([a-z0-9-]+)", u.text or "")
            if not slug_match:
                continue
            s = slug_match.group(1)
            if s in seen or s in ("fund-list",):
                continue
            seen.add(s)
            index.append({"slug": s, "name": _slug_to_name(s)})
        for entry in index:
            base = _strip_plan_suffix(entry["slug"])
            if base in _FUND_NAME_CORRECTIONS:
                entry["name"] = _FUND_NAME_CORRECTIONS[base]
            else:
                cached = _FUND_DATA_CACHE.get(entry["slug"])
                if cached and cached.get("data", {}).get("name"):
                    entry["name"] = cached["data"]["name"]
        _FUND_INDEX = _augment_with_plan_variants(index)
        _save_fund_index_to_disk(_FUND_INDEX)
    except Exception as e:
        print(f"Warning: failed to load fund index: {e}")
        _FUND_INDEX = _load_fund_index_from_disk() or []
    return _FUND_INDEX


def _load_fund_index_from_disk():
    try:
        with open(_FUND_INDEX_FILE) as f:
            data = json.load(f)
        stale = data.get("ts", 0) < time_module.time() - 86400 * 7
        if not stale and isinstance(data.get("index"), list):
            return data["index"]
    except Exception:
        pass
    return None


def _save_fund_index_to_disk(index):
    try:
        with open(_FUND_INDEX_FILE, "w") as f:
            json.dump({"ts": time_module.time(), "index": index}, f)
    except Exception as e:
        print(f"Warning: failed to save fund index: {e}")


def _slug_to_name(slug):
    name = slug.replace("-", " ").title()
    name = re.sub(
        r"\s+(Direct Growth|Direct Plan Growth|Regular Growth|Growth|Direct Plan|Direct|Regular)$",
        "",
        name,
    )
    name = re.sub(
        r"\b(Hdfc|Sbi|Mnc|Fof|Etf|Elss|Amc|Nfo|Roe|Roce|Eps|Psu|Nifty|Aaa|Sdl|Ipo|Idfc|Icici)\b",
        lambda m: m.group(1).upper(),
        name,
    )
    return name

def _get_stock_pe_from_groww(search_id):
    if search_id in _PE_CACHE:
        return _PE_CACHE[search_id]
    url = f"{GROWW_BASE}/stocks/{search_id}"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=10)
        if resp.status_code != 200:
            return None
        data = _extract_next_data(resp.text)
        if not data:
            return None
        stock_data = data.get("props", {}).get("pageProps", {}).get("stockData", {})
        pe = stock_data.get("stats", {}).get("peRatio")
        if pe is not None:
            pe = float(pe)
        _PE_CACHE[search_id] = pe
        return pe
    except (requests.RequestException, ValueError, TypeError):
        _PE_CACHE[search_id] = None
        return None


def _is_debt_instrument(name):
    """Heuristic: return True if company name looks like a bond/debt instrument."""
    if not name:
        return True
    name_u = name.upper().strip()
    # Debt indicators in company name
    debt_keywords = [
        "CD ", " BD ", " NCD ", "DEBENTURE", "FVRS", "GOI ",
        "SDL ", "STRPP", "LOA ", "BOND", "SEC ", "IRS ",
        "MRGN MONEY", "TREPS", "CASH", "NET CURRENT",
        "NET RECEIVABLES", "MUTUAL FUND", "ETF",
        "REVERSE REPO", "TBILLS", "T-BILL", "CP ", "SOV",
        "FLOAT", "G SEC", "G SECURITY", "SGL",
    ]
    for kw in debt_keywords:
        if kw in name_u:
            return True
    # Very long names with numbers are typically bonds
    if len(name) > 35 and any(c.isdigit() for c in name):
        return True
    return False


def _get_stock_pe_from_yfinance(symbol=None, company_name=None):
    """Tertiary fallback: fetch trailing P/E from yfinance.

    Tries in order:
    1. {NSE_symbol}.NS (if symbol provided)
    2. yfinance Search on company name → first valid equity ticker
    3. First word of company name + .NS (for Indian stocks)
    4. Company name as-is (yfinance may resolve it)
    """
    if _is_debt_instrument(company_name):
        return None

    try:
        import yfinance as yf
        import requests
    except ImportError:
        return None

    # ponytail: set 3s timeout on yfinance's shared session
    class _TimeoutAdapter(requests.adapters.HTTPAdapter):
        def send(self, req, **kw):
            kw.setdefault("timeout", 3 if not _IS_PREWARM else None)
            return super().send(req, **kw)
    try:
        _dummy = yf.Ticker("RELIANCE.NS")
        _session = _dummy.session
        _session.mount("https://", _TimeoutAdapter())
        _session.mount("http://", _TimeoutAdapter())
    except Exception:
        pass

    def _is_valid_equity(info):
        return info and info.get("quoteType") == "EQUITY"

    def _try_ticker(t):
        try:
            ti = yf.Ticker(t)
            info = ti.info
            if _is_valid_equity(info):
                pe = info.get("trailingPE") or info.get("forwardPE")
                pb = info.get("priceToBook")
                mcap = info.get("marketCap")
                if pe is not None and pe > 0:
                    return round(float(pe), 2), pb, mcap
        except Exception:
            pass
        return None, None, None

    def _store(t, company):
        pe, pb, mcap = _try_ticker(t)
        _STOCK_PE_CACHE[company] = {"pe": pe if pe is not None else None, "pb": pb, "mcap": mcap, "ts": datetime.now().isoformat(timespec="seconds")}
        return pe

    seen = set()
    company = company_name or symbol or ""

    # 1. NSE symbol
    if symbol:
        t = f"{symbol}.NS"
        seen.add(t)
        pe = _store(t, company)
        if pe is not None:
            return pe

    # 2. yfinance Search on company name (try with and without "Ltd"/"Corp" suffixes)
    if company_name:
        search_names = [company_name]
        stem = company_name.upper().strip()
        for suffix in [" LTD", " LIMITED", " LTD.", " PVT LTD", " PRIVATE LIMITED",
                        " CORPORATION", " CORP", " INC", " INCORPORATED", " LLC"]:
            if stem.endswith(suffix):
                search_names.append(stem[:-len(suffix)].strip().title())
                break
        for sn in search_names:
            try:
                search = yf.Search(sn)
                if search.quotes:
                    for q in search.quotes:
                        sym = q.get("symbol", "")
                        qtype = q.get("quoteType", "")
                        exch = q.get("exchange", "")
                        if qtype != "EQUITY":
                            continue
                        # Accept NSE/BSE Indian stocks and US exchange stocks
                        is_indian = sym.endswith(".NS") or sym.endswith(".BO")
                        is_us = exch in ("NMS", "NCM", "NYQ", "NAS", "NYE", "ASE")
                        if not (is_indian or is_us):
                            continue
                        if sym in seen:
                            continue
                        seen.add(sym)
                        pe = _store(sym, company_name)
                        if pe is not None:
                            return pe
            except Exception:
                continue

    # 3. First word + .NS (Indian stocks)
    if company_name:
        first = company_name.upper().strip().split()[0]
        t = f"{first}.NS"
        if t not in seen:
            seen.add(t)
            pe = _store(t, company_name)
            if pe is not None:
                return pe

    # 4. Company name as-is (yfinance may resolve it directly)
    if company_name and company_name not in seen:
        pe = _store(company_name, company_name)
        if pe is not None:
            return pe

    return None


def _compute_weighted_pe(holdings):
    eligible = [
        h for h in holdings
        if h.get("company_name") and h.get("corpus_per") is not None
        and (
            h.get("nature_name") == "EQUITY"
            or not _is_debt_instrument(h["company_name"])
        )
    ]
    if not eligible:
        return None
    eligible.sort(key=lambda h: float(h["corpus_per"]), reverse=True)
    seen = set()
    company_names = []
    for h in eligible:
        name = h["company_name"]
        if name not in seen:
            seen.add(name)
            company_names.append(name)

    pe_map = {}
    for name in company_names:
        cached = _lookup_pe_cache(name)
        if cached is not None:
            if cached.get("pe") is not None and not _is_cache_stale(cached):
                pe_map[name] = cached["pe"]
            # ponytail: skip stocks that already failed (None PE) to avoid retrying 404s
            continue

    _ALLOW_NETWORK = _PE_ALLOW_NETWORK or not ON_RENDER
    if any(name not in pe_map for name in company_names):
        if _ALLOW_NETWORK:
            from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError
            uncached = [n for n in company_names if n not in pe_map and _lookup_pe_cache(n) is None]
            pe_timeout = None if _IS_PREWARM else 12
            with ThreadPoolExecutor(max_workers=15) as ex:
                fut_to_name = {ex.submit(_get_stock_pe_screener, n, True): n for n in uncached}
                try:
                    for fut in as_completed(fut_to_name, timeout=pe_timeout):
                        name = fut_to_name[fut]
                        pe = fut.result()
                        if pe is not None:
                            pe_map[name] = pe
                            _STOCK_PE_CACHE[name] = _cache_entry(pe)
                except TimeoutError:
                    pass
            # yfinance fallback for stocks still missing P/E (both user requests and prewarm)
            yfinance_batch = []
            for name in company_names:
                if pe_map.get(name) is None:
                    symbol = _company_to_symbol(name)
                    yfinance_batch.append((name, symbol))
            if yfinance_batch:
                yf_timeout = None if _IS_PREWARM else 15
                with ThreadPoolExecutor(max_workers=12) as ex:
                    fut_to_name = {
                        ex.submit(_get_stock_pe_from_yfinance, sym, name): name
                        for name, sym in yfinance_batch
                    }
                    try:
                        for fut in as_completed(fut_to_name, timeout=yf_timeout):
                            name = fut_to_name[fut]
                            pe = fut.result()
                            if pe is not None:
                                _STOCK_PE_CACHE[name] = _cache_entry(pe)
                                pe_map[name] = pe
                    except TimeoutError:
                        pass

            if _IS_PREWARM:
                # Fallback to Groww for stocks missing from screener
                groww_fallback = []
                for name in uncached:
                    if pe_map.get(name) is None and any(
                        h.get("stock_search_id") and h["company_name"] == name
                        for h in eligible
                    ):
                        search_id = next(
                            h["stock_search_id"] for h in eligible
                            if h["company_name"] == name and h.get("stock_search_id")
                        )
                        groww_fallback.append((name, search_id))
                if groww_fallback:
                    with ThreadPoolExecutor(max_workers=10) as ex:
                        fut_to_name = {ex.submit(_get_stock_pe_from_groww, sid): name for name, sid in groww_fallback}
                        for fut in as_completed(fut_to_name):
                            name = fut_to_name[fut]
                            pe = fut.result()
                            if pe is not None:
                                _STOCK_PE_CACHE[name] = _cache_entry(pe)
                                pe_map[name] = pe

    # Include all stocks with valid PE (exclude only PE=None or PE=0)
    # Deduplicate by company name to avoid double-counting weight
    weighted_pe_data = []
    total_raw_weight = 0.0
    seen_weight = set()
    for h in eligible:
        name = h["company_name"]
        if name in seen_weight:
            continue
        seen_weight.add(name)
        pe = pe_map.get(name)
        w = float(h["corpus_per"])
        if isinstance(pe, (int, float)) and pe != 0:
            weighted_pe_data.append((w, pe))
            total_raw_weight += w

    if not weighted_pe_data or total_raw_weight == 0:
        _save_pe_cache()
        return None

    # Renormalize weights to 100% and compute harmonic mean
    harmonic_sum = 0.0
    for w, pe in weighted_pe_data:
        norm_w = w / total_raw_weight * 100
        harmonic_sum += norm_w / pe
    _save_pe_cache()
    return round(100 / harmonic_sum, 2)


def _fetch_pb_via_yfinance(name):
    """Fetch P/B for a single stock via yfinance with timeout and cache it."""
    try:
        import yfinance as yf
        symbol = _company_to_symbol(name)
        if not symbol:
            symbol = _guess_nse_symbol(name)
        if symbol:
            import concurrent.futures as _cf
            with _cf.ThreadPoolExecutor(1) as _ex:
                _t = _ex.submit(lambda: yf.Ticker(f"{symbol}.NS").info)
                info = _t.result(timeout=5)
            pb = info.get("priceToBook")
            if pb is not None and pb > 0:
                pb = round(float(pb), 2)
                existing = _STOCK_PE_CACHE.get(name, {})
                existing.setdefault("pe", None)
                existing["pb"] = pb
                existing["ts"] = datetime.now().isoformat(timespec="seconds")
                _STOCK_PE_CACHE[name] = existing
                return pb
    except Exception:
        pass
    return None


def _compute_weighted_pb(holdings, fetch_timeout=10):
    """Compute weighted average P/B from stock PE cache (PB stored alongside PE).
    fetch_timeout: max seconds to spend fetching uncached P/B values via yfinance."""
    eligible = [
        h for h in holdings
        if h.get("company_name") and h.get("corpus_per") is not None
        and (h.get("nature_name") == "EQUITY" or not _is_debt_instrument(h["company_name"]))
    ]
    if not eligible:
        return None
    eligible.sort(key=lambda h: float(h["corpus_per"]), reverse=True)
    seen = set()
    weighted_data = []
    total_w = 0.0
    missing_pb = []
    for h in eligible:
        name = h["company_name"]
        if name in seen:
            continue
        seen.add(name)
        cached = _lookup_pe_cache(name)
        pb = cached.get("pb") if isinstance(cached, dict) else None
        if pb is not None and pb > 0:
            w = float(h["corpus_per"])
            weighted_data.append((w, pb))
            total_w += w
        elif not _is_debt_instrument(name):
            missing_pb.append(name)

    if missing_pb:
        from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError
        fetch_limit = min(len(missing_pb), 50)
        missing_pb = missing_pb[:fetch_limit]
        with ThreadPoolExecutor(max_workers=10) as ex:
            fut_map = {ex.submit(_fetch_pb_via_yfinance, n): n for n in missing_pb}
            try:
                for fut in as_completed(fut_map, timeout=fetch_timeout):
                    pb = fut.result()
                    if pb is not None:
                        name = fut_map[fut]
                        w = sum(float(h["corpus_per"]) for h in eligible if h["company_name"] == name)
                        if w > 0:
                            weighted_data.append((w, pb))
                            total_w += w
                    if len(_STOCK_PE_CACHE) % 10 == 0:
                        _save_pe_cache()
            except TimeoutError:
                pass

    if not weighted_data or total_w == 0:
        _save_pe_cache()
        return None
    avg = sum(w * pb for w, pb in weighted_data) / total_w
    _save_pe_cache()
    return round(avg, 2)


def _plan_type(slug):
    if slug.endswith("-direct-growth"):
        return "Direct"
    if slug.endswith("-regular-growth"):
        return "Regular"
    if slug.endswith("-direct-plan-growth"):
        return "Direct"
    if slug.endswith("-growth"):
        return "Growth"
    if slug.endswith("-direct"):
        return "Direct"
    if slug.endswith("-regular"):
        return "Regular"
    return ""


def _strip_plan_suffix(slug):
    return re.sub(
        r"-(direct-growth|regular-growth|direct-plan-growth|growth|direct|regular)$",
        "",
        slug,
    )


_PLAN_SUFFIXES = [
    "-direct-growth",
    "-regular-growth",
    "-direct-plan-growth",
    "-growth",
]

_PLAN_SWAP = {
    "-direct-growth": "-regular-growth",
    "-regular-growth": "-direct-growth",
    "-direct-plan-growth": "-regular-growth",
    "-direct": "-regular",
    "-regular": "-direct",
    "-growth": "-direct-growth",
}

def _augment_with_plan_variants(index):
    result = []
    seen = set()
    for entry in index:
        slug = entry["slug"]
        if slug in seen:
            continue
        seen.add(slug)
        result.append(entry)
        alt = None
        for suffix, replacement in _PLAN_SWAP.items():
            if slug.endswith(suffix):
                alt = slug[:-len(suffix)] + replacement
                break
        if alt and alt not in seen:
            seen.add(alt)
            result.append({"slug": alt, "name": _slug_to_name(alt)})
    return result


def _get_all_plan_variants(base_slug):
    """Given a fund slug, return list of {slug, plan} for all available plan types."""
    results = []
    for suffix in _PLAN_SUFFIXES:
        s = f"{base_slug}{suffix}"
        url = f"{GROWW_BASE}/mutual-funds/{s}"
        try:
            resp = requests.get(url, headers=HEADERS, timeout=8)
            if resp.status_code == 200:
                plan = _plan_type(s)
                results.append({"slug": s, "plan": plan, "url": url})
        except requests.RequestException:
            continue
    return results


def _slugify(text):
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9\s-]", "", text)
    text = re.sub(r"\s+", "-", text)
    return text.strip("-")


_SEARCH_SYNONYMS = {
    "bluechip": ["large cap", "bluechip"],
    "largecap": ["large cap", "largecap"],
    "midcap": ["mid cap", "midcap"],
    "smallcap": ["small cap", "smallcap"],
}


def _expand_query(query):
    q = query.lower().strip()
    words = q.split()
    expanded = {q}
    # Expand known synonyms
    for i, w in enumerate(words):
        if w in _SEARCH_SYNONYMS:
            for syn in _SEARCH_SYNONYMS[w]:
                alt = " ".join(words[:i] + syn.split() + words[i + 1:])
                expanded.add(alt)
    return expanded


def search_fund(query):
    q = query.lower().strip()
    if not q:
        return []

    queries = _expand_query(query)
    # Also add a variant with "&" replaced by "and"
    queries.add(q.replace("&", "and"))
    # Also try without filler words
    filler = {"the", "a", "an", "and", "or", "of", "in", "to", "for", "&"}
    q_no_filler = " ".join(w for w in q.split() if w not in filler)
    if q_no_filler != q:
        queries.add(q_no_filler)
        queries.add(q_no_filler.replace("&", "and"))
    # Also try without "fund" suffix
    q_no_fund = re.sub(r"\s+fund$", "", q).strip()
    if q_no_fund != q:
        queries.add(q_no_fund)

    q_variants = [qv.lower() for qv in queries]
    q_words = set(q.split())

    index = _load_fund_index()
    scored = []
    seen_slugs = set()

    # Score from index (fast, ~1900 funds)
    if index:
        for entry in index:
            slug_lower = entry["slug"].lower()
            if slug_lower.startswith("best-"):
                continue
            seen_slugs.add(slug_lower)
            name_lower = entry["name"].lower().replace("&", "and")
            slug_words = set(slug_lower.replace("-", " ").split())
            name_words = set(name_lower.split())

            best_score = 0
            for qv in q_variants:
                qv_lower = qv.lower().replace("&", "and")
                qv_words = set(qv_lower.split())

                score = 0
                if name_lower == qv_lower or slug_lower == qv_lower or slug_lower.replace("-", " ") == qv_lower:
                    score = 50
                elif qv_lower in name_lower:
                    score = 30
                elif qv_lower in slug_lower:
                    score = 25
                elif qv_lower in slug_lower.replace("-", " "):
                    score = 20

                if score < 20:
                    if qv_words and qv_words <= slug_words:
                        score = max(score, 15)
                    elif qv_words and qv_words <= name_words:
                        score = max(score, 12)

                if score < 12:
                    common_name = qv_words & name_words
                    common_slug = qv_words & slug_words
                    score += len(common_name) * 3 + len(common_slug) * 2

                best_score = max(best_score, score)

            if best_score > 0:
                stripped_name = re.sub(
                    r"\s+(Direct Growth|Direct Plan Growth|Regular Growth|Growth|Direct Plan|Direct|Regular)$",
                    "", entry["name"]
                )
                scored.append((best_score, entry["slug"], stripped_name))

    # Only run slow HTTP fallback when we have very few results
    if len(scored) < 5:
        fallback = _search_fund_fallback(query, seen_slugs)
        for fb in fallback:
            fb_score = fb.get("score", 12)
            name_no_plan = re.sub(
                r"\s+(Direct Growth|Direct Plan Growth|Regular Growth|Growth|Direct Plan|Direct|Regular)$",
                "", fb["name"]
            )
            scored.append((fb_score, fb["slug"], name_no_plan))

    # Deduplicate by slug: keep entry with better name (more query words in common)
    q_name_words = set(q_no_filler.replace("&", "and").split()) if q_no_filler != q else set(q.split())
    def _name_relevance(name):
        n = name.lower().replace("&", "and").replace("\\u0026", "and")
        return len(q_name_words & set(n.split()))
    best_by_slug = {}
    for score, slug, name in scored:
        slug_lower = slug.lower()
        if slug_lower not in best_by_slug:
            best_by_slug[slug_lower] = (score, slug, name)
        else:
            existing = best_by_slug[slug_lower]
            # Prefer higher score, or better name relevance on near tie
            if score > existing[0] + 2:  # significantly higher score wins
                best_by_slug[slug_lower] = (score, slug, name)
            elif score >= existing[0] - 2:  # within range: better name wins
                if _name_relevance(name) > _name_relevance(existing[2]):
                    # Keep the original (higher) score when replacing for better name
                    best_by_slug[slug_lower] = (max(score, existing[0]), slug, name)
    scored = list(best_by_slug.values())

    scored.sort(key=lambda x: (-x[0], x[1]))

    # Group by base fund and collect all plan variants
    grouped = {}
    for _, slug, name in scored:
        base = _strip_plan_suffix(slug)
        plan = _plan_type(slug)
        if base not in grouped:
            grouped[base] = {"name": name, "plans": {}}
        grouped[base]["plans"][plan] = slug

    # Group by base fund, using only plans already known from the sitemap.
    # No HTTP probing for missing plans — the sitemap covers the vast majority.
    results = []
    groups_list = list(grouped.items())
    for base, g in groups_list[:100]:
        plans_list = sorted([(p, s) for p, s in g["plans"].items() if p], key=lambda x: x[0])
        results.append({
            "name": g["name"],
            "base": base,
            "plans": [{"plan": p, "slug": s} for p, s in plans_list],
        })
    return results


def _search_fund_fallback(query, skip_slugs=None):
    candidates = _generate_slugs(query)
    seen = set(skip_slugs or [])

    # Also derive candidates from mfapi.in scheme names (catches renamed funds)
    try:
        resp = requests.get(
            f"https://api.mfapi.in/mf/search?q={quote(query)}",
            headers=HEADERS, timeout=8
        )
        if resp.status_code == 200:
            for scheme in resp.json()[:3]:
                name = scheme.get("schemeName", "")
                clean = re.sub(r"\s*[-–]\s*(Direct|Regular).*$", "", name, flags=re.I)
                clean = re.sub(r"\s*[-–]\s*Growth.*$", "", clean, flags=re.I).strip()
                if clean:
                    base = _slugify(clean)
                    if base:
                        for s in ["-direct-growth", "-regular-growth", "-growth", ""]:
                            candidates.append(f"{base}{s}")
    except:
        pass

    # Cross-reference sitemap: re-probe slugs whose slug-derived name
    # shares >= 2 non-filler query words (catches old-slug renamed funds).
    # We OVERRIDE seen here because the slug-derived name may be stale
    # and we need the actual page title from the Groww page.
    cross_ref_slugs = []
    filler = {"the", "a", "an", "and", "or", "of", "in", "to", "for", "fund", "plan", "growth", "direct", "regular"}
    q_words = set(w for w in query.lower().split() if w not in filler)
    if len(q_words) >= 2:
        index = _load_fund_index()
        for entry in index:
            slug = entry["slug"]
            if slug.startswith("best-"):
                continue
            name_words = set(entry["name"].lower().split())
            common = q_words & name_words
            if len(common) >= 2:
                cross_ref_slugs.append(slug)

    # Deduplicate preserving order
    candidates = list(dict.fromkeys(candidates))

    def _try(slug):
        if slug in seen:
            return None
        seen.add(slug)
        try:
            r = requests.get(f"{GROWW_BASE}/mutual-funds/{slug}", headers=HEADERS, timeout=5)
            if r.status_code == 200:
                name = _extract_name(r.text) or slug.replace("-", " ").title()
                return {"name": name, "slug": slug, "url": r.url}
        except requests.RequestException:
            pass
        return None

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as ex:
        for res in ex.map(_try, candidates):
            if res:
                results.append(res)

    # For cross-referenced slugs, probe even if already in seen,
    # because we want the actual page title (not slug-derived name).
    # Compute a relevance score from the actual page title vs query.
    q_lower = query.lower().replace("&", "and")
    q_variants = [q_lower]
    no_filler = " ".join(w for w in q_lower.split() if w not in filler)
    if no_filler != q_lower:
        q_variants.append(no_filler)
    # Also use same expansions as the main index scoring
    for v in _expand_query(query):
        v_lower = v.lower()
        if v_lower not in q_variants:
            q_variants.append(v_lower)

    def _score_name(name):
        """Score a fund name against the query (0-30)."""
        n = name.lower().replace("&", "and")
        n_words = set(n.split())
        best = 0
        for qv in q_variants:
            qv_lower = qv.lower()
            qv_words = set(qv_lower.split())
            if qv_lower in n:
                return 30
            if qv_words and qv_words <= n_words:
                best = max(best, 12)
            common = qv_words & n_words
            best = max(best, min(11, len(common) * 2))
        return best

    def _try_cross(slug):
        try:
            r = requests.get(f"{GROWW_BASE}/mutual-funds/{slug}", headers=HEADERS, timeout=5)
            if r.status_code == 200:
                name = _extract_name(r.text) or slug.replace("-", " ").title()
                score = _score_name(name)
                return {"name": name, "slug": slug, "url": r.url, "score": score}
        except requests.RequestException:
            pass
        return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as ex:
        for res in ex.map(_try_cross, cross_ref_slugs):
            if res:
                results.append(res)

    return results


def _generate_slugs(query):
    base = _slugify(query)
    base = re.sub(r"-(growth|direct|plan|regular|bonus|dividend|payout|reinvestment|fund)-*", "", base)
    if not base:
        base = _slugify(query)
    base = base.strip("-")

    suffixes = [
        "-direct-growth",
        "-direct-plan-growth",
        "-regular-growth",
        "-growth",
        "",
    ]
    slugs = []
    for s in suffixes:
        slugs.append(f"{base}{s}")
        slugs.append(f"{base}-fund{s}")
    return slugs


def _extract_name(html):
    m = re.search(r'"fund_name"\s*:\s*"([^"]+)"', html)
    if m:
        name = m.group(1)
        # Decode JSON unicode escapes
        try:
            name = json.loads(f'"{name}"')
        except:
            pass
        return name
    m = re.search(r'<title[^>]*>(.*?)</title>', html, re.DOTALL)
    if m:
        title = m.group(1).split("|")[0].strip()
        title = title.replace(" - NAV, Mutual Fund Performance & Portfolio", "")
        return title
    return None


def _search_mfapi(fund_name, plan_type):
    clean = fund_name.lower().strip()
    for suffix in [" fund", " - direct", " - regular", " - direct plan", " - regular plan",
                   "-direct", "-regular", "-growth", " growth", " plan", "-direct-plan",
                   "-regular-plan"]:
        if clean.endswith(suffix):
            clean = clean[:-len(suffix)]
    clean = re.sub(r'\s*-\s*', ' ', clean).strip()
    tokens = [t for t in clean.split()
              if t not in ("fund", "plan", "regular", "direct", "growth", "option",
                           "a", "an", "the", "new", "old", "bonus", "pension") and len(t) > 1]
    if not tokens:
        tokens = [t for t in clean.split() if len(t) > 2][:3] or clean.split()[:3]

    def _try_search(search_tokens):
        q = quote((" ".join(search_tokens))[:60])
        url = f"https://api.mfapi.in/mf/search?q={q}"
        try:
            resp = requests.get(url, headers=HEADERS, timeout=(8, 10))
            if resp.status_code != 200:
                return None
            results = resp.json()
            is_direct = plan_type == "Direct"
            match_tokens = set(tokens)
            best_code = None
            best_score = (0, 0)
            for r in results:
                name = r.get("schemeName", "")
                if not isinstance(name, str):
                    continue
                name_upper = name.upper()
                if "IDCW" in name_upper:
                    continue
                if "GROWTH" not in name_upper:
                    continue
                has_direct = "DIRECT" in name_upper
                if is_direct != has_direct:
                    continue
                result_tokens = set(name.lower().split())
                overlap = match_tokens & result_tokens
                if len(overlap) >= min(2, len(match_tokens)):
                    score = (len(overlap), -len(result_tokens))
                    if score > best_score:
                        best_code = r["schemeCode"]
                        best_score = score
            return best_code
        except Exception:
            return None

    code = _try_search(tokens[:4])
    if code is not None:
        return code
    if len(tokens) >= 3:
        code = _try_search(tokens[:3])
        if code is not None:
            return code
    if len(tokens) >= 2:
        code = _try_search(tokens[:2])
        if code is not None:
            return code
    return None


MFAPI_CACHE = {}

def _fetch_nav_history(scheme_code):
    cached = MFAPI_CACHE.get(scheme_code)
    if cached is not None:
        return cached
    url = f"https://api.mfapi.in/mf/{scheme_code}"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=(6, 8))
        if resp.status_code != 200:
            return None
        data = resp.json()
        navs = data.get("data", [])
        parsed = []
        for entry in navs:
            try:
                dt = datetime.strptime(entry["date"], "%d-%m-%Y")
                nav = float(entry["nav"])
                parsed.append((dt, nav))
            except (ValueError, KeyError):
                continue
        parsed.sort(key=lambda x: x[0])
        MFAPI_CACHE[scheme_code] = parsed
        return parsed
    except Exception:
        return None


def _cagr(nav_data, years):
    if not nav_data or len(nav_data) < 2:
        return None
    end_date, end_nav = nav_data[-1]
    target_date = end_date - timedelta(days=int(years * 365))
    start = None
    for i in range(len(nav_data) - 1, -1, -1):
        if nav_data[i][0] <= target_date:
            start = nav_data[i]
            break
    if start is None:
        start = nav_data[0]
    days = (end_date - start[0]).days
    if days < 30 or start[1] <= 0 or end_nav <= 0:
        return None
    return (end_nav / start[1]) ** (365 / days) - 1


def _annualized_std(nav_data):
    if not nav_data or len(nav_data) < 25:
        return None
    monthly = []
    groups = defaultdict(list)
    for dt, nav in nav_data:
        groups[(dt.year, dt.month)].append((dt, nav))
    for key in sorted(groups.keys()):
        monthly.append(groups[key][-1])
    if len(monthly) < 6:
        return None
    returns = []
    for i in range(1, len(monthly)):
        if monthly[i - 1][1] > 0:
            r = monthly[i][1] / monthly[i - 1][1] - 1
            returns.append(r)
    if len(returns) < 5:
        return None
    mean = sum(returns) / len(returns)
    variance = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    return math.sqrt(variance) * math.sqrt(12)


def _rolling_returns(nav_data, years):
    if not nav_data or len(nav_data) < 2:
        return None
    window_days = int(years * 365)
    rolls = []
    dates = []
    for i in range(len(nav_data)):
        end_dt, end_nav = nav_data[i]
        target_dt = end_dt - timedelta(days=window_days)
        for j in range(i, -1, -1):
            if nav_data[j][0] <= target_dt:
                start_nav = nav_data[j][1]
                days = (end_dt - nav_data[j][0]).days
                if days >= 30 and start_nav > 0 and end_nav > 0:
                    cagr = (end_nav / start_nav) ** (365 / days) - 1
                    rolls.append(round(cagr * 100, 2))
                    dates.append(end_dt.strftime("%d-%m-%Y"))
                break
    if not rolls:
        return None
    if len(rolls) == 1:
        return {"average": round(rolls[0], 2), "min": round(rolls[0], 2), "max": round(rolls[0], 2), "std": 0, "count": 1}
    avg = sum(rolls) / len(rolls)
    var = sum((r - avg) ** 2 for r in rolls) / (len(rolls) - 1)
    return {
        "average": round(avg, 2),
        "min": round(min(rolls), 2),
        "max": round(max(rolls), 2),
        "std": round(math.sqrt(var), 2),
        "count": len(rolls),
    }


_RFR_CACHE = None
_RFR_CACHE_TS = 0

def _get_risk_free_rate():
    global _RFR_CACHE, _RFR_CACHE_TS
    now = time_module.time()
    if _RFR_CACHE is not None and now - _RFR_CACHE_TS < 3600:
        return _RFR_CACHE
    try:
        resp = requests.get(
            "https://www.investing.com/rates-bonds/india-10-year-bond-yield",
            headers=HEADERS, timeout=10
        )
        m = re.search(r'data-test="instrument-price-last"[^>]*>([\d.]+)', resp.text)
        if m:
            val = float(m.group(1))
            if 0 < val < 20:
                _RFR_CACHE = round(val / 100, 4)
                _RFR_CACHE_TS = now
                return _RFR_CACHE
    except Exception:
        pass
    _RFR_CACHE = 0.07
    _RFR_CACHE_TS = now
    return 0.07


def _slice_nav_data(nav_data, years):
    if not nav_data:
        return None
    end = nav_data[-1][0]
    start = end - timedelta(days=int(years * 365))
    sliced = [(dt, nav) for dt, nav in nav_data if dt >= start]
    if len(sliced) < 20:
        return None
    return sliced


def _compute_max_drawdown(nav_data):
    """Compute maximum drawdown (peak-to-trough decline) from daily NAV."""
    if not nav_data or len(nav_data) < 20:
        return None
    peak = nav_data[0][1]
    max_dd = 0
    for _, nav in nav_data:
        if nav > peak:
            peak = nav
        dd = (peak - nav) / peak
        if dd > max_dd:
            max_dd = dd
    return round(max_dd * 100, 2) if max_dd > 0 else None


def _compute_metrics(nav_data, plan_type="Direct"):
    if not nav_data or len(nav_data) < 30:
        return {}
    rfr = _get_risk_free_rate()
    r6m = _cagr(nav_data, 0.5)
    r1y = _cagr(nav_data, 1)
    r3y = _cagr(nav_data, 3)
    r5y = _cagr(nav_data, 5)
    nav_3y = _slice_nav_data(nav_data, 3) or nav_data
    nav_1y = _slice_nav_data(nav_data, 1) or nav_data
    std_3y = _annualized_std(nav_3y)
    std_1y = _annualized_std(nav_1y)
    std = std_3y if (std_3y is not None and r3y is not None) else std_1y
    sharpe = None
    if std and std > 0:
        recent_return = r3y if r3y is not None else r1y
        if recent_return is not None:
            sharpe = round((recent_return - rfr) / std, 2)
    roll_1y = _rolling_returns(nav_data, 1)
    roll_3y = _rolling_returns(nav_data, 3)
    roll_5y = _rolling_returns(nav_data, 5)
    max_dd = _compute_max_drawdown(nav_data)

    # Build chart data: full daily resolution
    step = max(1, len(nav_data) // 2000)
    chart = []
    for i in range(0, len(nav_data), step):
        d, n = nav_data[i]
        chart.append([d.strftime("%Y-%m-%d"), round(float(n), 2)])
    if len(chart) < 2 or chart[-1][0] != nav_data[-1][0].strftime("%Y-%m-%d"):
        chart.append([nav_data[-1][0].strftime("%Y-%m-%d"), round(float(nav_data[-1][1]), 2)])

    return {
        "returns_6m": round(((1 + r6m) ** 0.5 - 1) * 100, 2) if r6m is not None else None,
        "returns_1y": round(r1y * 100, 2) if r1y is not None else None,
        "returns_3y": round(r3y * 100, 2) if r3y is not None else None,
        "returns_5y": round(r5y * 100, 2) if r5y is not None else None,
        "std_dev": round(std * 100, 2) if std is not None else None,
        "sharpe": sharpe,
        "rolling_1y": roll_1y,
        "rolling_3y": roll_3y,
        "rolling_5y": roll_5y,
        "risk_free_rate": round(rfr * 100, 2),
        "max_drawdown": max_dd,
        "nav_chart": chart,
    }


def _to_float(v):
    if v is None:
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def _fetch_stock_mcap(company_name):
    """Fetch market cap for a stock. Tries yfinance with multiple approaches."""
    # Known ticker rebrandings / name changes
    _TICKER_ALIAS = {
        "ZOMATO": "ETERNAL",
        "HEXAWARE": "HEXT",
        "HEXA": "HEXT",
    }
    cached = _STOCK_PE_CACHE.get(company_name)
    if isinstance(cached, dict):
        mcap = cached.get("mcap")
        if mcap is not None and mcap > 0:
            return float(mcap)
    # Try NSE symbol first, then fallback to name-derived ticker
    sym = _company_to_symbol(company_name)
    if not sym:
        sym = _guess_nse_symbol(company_name)
    if not sym:
        _STOCK_PE_CACHE[company_name] = _cache_entry(None)
        return None
    def _do_fetch(ticker_str):
        import yfinance as yf
        ti = yf.Ticker(ticker_str)
        info = ti.info
        mcap = info.get("marketCap")
        if mcap is not None and mcap > 0:
            return float(mcap)
        price = info.get("currentPrice") or info.get("regularMarketPrice") or info.get("previousClose")
        shares = info.get("sharesOutstanding") or info.get("impliedSharesOutstanding")
        if price and shares:
            mcap = price * shares
            if mcap > 0:
                return float(mcap)
        df = yf.download(ticker_str, period="1d", progress=False)
        if not df.empty:
            close = float(df["Close"].values.flat[0])
            if shares:
                mcap = close * shares
                if mcap > 0:
                    return float(mcap)
        return None
    try:
        mcap = _do_fetch(f"{sym}.NS")
        if mcap:
            return mcap
    except Exception:
        pass
    # Try alias if primary ticker failed
    alias = _TICKER_ALIAS.get(sym)
    if alias:
        try:
            mcap = _do_fetch(f"{alias}.NS")
            if mcap:
                return mcap
        except Exception:
            pass
    # Fallback: search yfinance for the company name
    try:
        import yfinance as yf
        # Strip common suffixes for better search results
        search_name = company_name
        for suffix in [" Ltd", " Limited", " Ltd.", " Limited.", " Pvt Ltd", " Private Limited"]:
            idx = search_name.upper().find(suffix.upper())
            if idx >= 0:
                search_name = search_name[:idx].strip()
                break
        search = yf.Search(search_name, max_results=3)
        for q in (search.quotes or []):
            ticker = q.get("symbol", "")
            if ticker.endswith(".NS"):
                mcap = _do_fetch(ticker)
                if mcap:
                    return mcap
        for q in (search.quotes or []):
            ticker = q.get("symbol", "")
            if ticker.endswith(".BO"):
                mcap = _do_fetch(ticker)
                if mcap:
                    return mcap
    except Exception:
        pass
    _STOCK_PE_CACHE[company_name] = _cache_entry(None)
    return None


def _compute_mcap_breakdown(holdings_raw):
    """Classify holdings into large/mid/small cap using SEBI-based market cap ranking."""
    buckets = {"large_cap": 0.0, "mid_cap": 0.0, "small_cap": 0.0, "other": 0.0}
    per_holding = {}
    for h in holdings_raw:
        name = h.get("company_name")
        pct = h.get("corpus_per")
        if not name or pct is None:
            continue
        cat = _get_mcap_category(name)
        buckets[cat] += float(pct)
        per_holding[name] = cat

    return {k: round(v, 2) for k, v in buckets.items() if v > 0.01}, per_holding


_BENCHMARK_YFINANCE_MAP = {
    "nifty 500 total return": "^NSEI",
    "nifty 500 tri": "^NSEI",
    "nifty 500": "^NSEI",
    "nifty 50 tri": "^NSEI",
    "nifty 50": "^NSEI",
    "sensex": "^BSESN",
    "bse 500": "^BSESN",
    "bse sensex": "^BSESN",
    "nifty smallcap 250": "^NSEI",
    "nifty smallcap 250 tri": "^NSEI",
    "bse 250 smallcap": "^NSEI",
    "nifty midcap 150": "^NSEI",
    "nifty midcap 150 tri": "^NSEI",
    "nifty largemidcap 250": "^NSEI",
    "nifty dividend yield": "^NSEI",
    "nifty midcap 100": "^NSEI",
    "nifty smallcap 100": "^NSEI",
    "nifty 100": "^NSEI",
    "nifty 200": "^NSEI",
    "bse 100": "^BSESN",
    "bse 200": "^BSESN",
}

# ── Display benchmarks for compare view ──

_CATEGORY_BENCHMARK_MAP = {
    "large cap": "Nifty 50 TRI",
    "flexi cap": "Nifty 500 TRI",
    "mid cap": "Nifty Midcap 150 TRI",
    "small cap": "Nifty Smallcap 250 TRI",
    "value": "Nifty 500 TRI",
    "multi cap": "Nifty 500 TRI",
    "elss": "Nifty 500 TRI",
    "focused fund": "Nifty 500 TRI",
    "large & midcap": "Nifty LargeMidcap 250 TRI",
    "aggressive hybrid": "Nifty 500 Hybrid Composite",
    "dividend yield": "Nifty Dividend Yield TRI",
    "value oriented": "Nifty 500 TRI",
}

_BENCHMARK_DISPLAY_SYMBOLS = {
    "Nifty 50 TRI": "^NSEI",
    "Nifty 500 TRI": "^NSEI",
    "Nifty Midcap 150 TRI": "^NSEI",
    "Nifty Smallcap 250 TRI": "^NSEI",
    "Nifty LargeMidcap 250 TRI": "^NSEI",
    "Nifty Dividend Yield TRI": "^NSEI",
    "Nifty 500 Hybrid Composite": "^NSEI",
}


def _get_benchmark_for_category(category):
    if not category:
        return None
    key = category.strip().lower()
    alias = _CATEGORY_PE_ALIAS.get(key)
    if alias:
        return _CATEGORY_BENCHMARK_MAP.get(alias)
    return _CATEGORY_BENCHMARK_MAP.get(key)


def _get_dominant_category(funds_data):
    cats = {}
    for f in funds_data.values():
        c = f.get("category")
        if c:
            c = c.strip().lower()
            cats[c] = cats.get(c, 0) + 1
    if not cats:
        return None
    return max(cats, key=cats.get)


def _fetch_benchmark_daily(benchmark_name):
    """Fetch daily benchmark index data from yfinance.
    Falls back to disk cache if yfinance is unavailable (e.g. Vercel).
    Returns [(datetime, float), ...] or None."""
    if not benchmark_name:
        return None
    nav_data = _load_benchmark_cache(benchmark_name)
    cache_recent = False
    if nav_data and len(nav_data) > 60:
        last_date = nav_data[-1][0]
        days_stale = (datetime.now() - last_date).days
        if days_stale <= 2:
            return nav_data
        cache_recent = True
    sym = _BENCHMARK_DISPLAY_SYMBOLS.get(benchmark_name)
    if not sym:
        return nav_data if cache_recent else None
    if ON_HF or not ON_VERCEL:
        try:
            import yfinance as yf
            import concurrent.futures as _cf
            with _cf.ThreadPoolExecutor() as _ex:
                _fut = _ex.submit(yf.download, sym, period="10y", interval="1d", progress=False)
                bm = _fut.result(timeout=8)
            if not bm.empty:
                prices = bm["Close"] if "Close" in bm.columns else (bm["Adj Close"] if "Adj Close" in bm.columns else None)
                if prices is not None:
                    nav_data = []
                    for dt, val in zip(prices.index, prices.values):
                        if val is not None and not (isinstance(val, float) and math.isnan(val)):
                            nav_data.append((dt.to_pydatetime(), float(val)))
                    if len(nav_data) > 60:
                        _save_benchmark_cache(benchmark_name, nav_data)
                        return nav_data
        except Exception:
            pass
    return nav_data if cache_recent else None


def _load_benchmark_cache(benchmark_name):
    """Load benchmark data from the disk cache file, with bundled fallback."""
    for fpath in (_BENCHMARK_CACHE_FILE, _BENCHMARK_BUNDLED_FILE):
        try:
            if os.path.exists(fpath):
                with open(fpath) as f:
                    data = json.load(f)
                raw = data.get(benchmark_name)
                if raw:
                    return [(datetime.fromisoformat(p[0]), float(p[1])) for p in raw]
        except Exception:
            pass
    return None


def _save_benchmark_cache(benchmark_name, nav_data):
    """Save benchmark data to disk cache file so it's available on Vercel deployment."""
    try:
        existing = {}
        if os.path.exists(_BENCHMARK_CACHE_FILE):
            with open(_BENCHMARK_CACHE_FILE) as f:
                existing = json.load(f)
        existing[benchmark_name] = [(d.isoformat(), float(v)) for d, v in nav_data]
        with open(_BENCHMARK_CACHE_FILE, "w") as f:
            json.dump(existing, f)
    except Exception:
        pass


def _yfinance_benchmark_symbol(benchmark_name):
    if not benchmark_name:
        return "^NSEI"
    key = benchmark_name.lower().strip()
    for pat, sym in _BENCHMARK_YFINANCE_MAP.items():
        if re.search(r'\b' + re.escape(pat) + r'\b', key):
            return sym
    return "^NSEI"


def _fetch_benchmark_monthly(benchmark_name):
    """Fetch benchmark monthly prices. Tries yfinance first, falls back to bundled cache.
    Returns (month_key->price) dict or None."""
    if not benchmark_name:
        return None
    daily = _load_benchmark_cache(benchmark_name)
    if daily and len(daily) >= 60:
        monthly = _fund_monthly_map(daily)
        if monthly and len(monthly) >= 12:
            return monthly
    sym = _yfinance_benchmark_symbol(benchmark_name)
    if ON_HF or not ON_VERCEL:
        try:
            import yfinance as yf
            import concurrent.futures as _cf
            with _cf.ThreadPoolExecutor() as _ex:
                _fut = _ex.submit(yf.download, sym, period="5y", interval="1mo", progress=False)
                bm = _fut.result(timeout=4)
            if not bm.empty:
                bm_prices = bm["Close"] if "Close" in bm.columns else bm["Adj Close"] if "Adj Close" in bm.columns else None
                if bm_prices is not None:
                    bm_monthly = bm_prices.resample("ME").last().dropna()
                    if len(bm_monthly) >= 12:
                        return {d.strftime("%Y-%m"): float(p) for d, p in zip(bm_monthly.index, bm_monthly.values)}
        except Exception:
            pass
    return None


def _fund_monthly_map(fund_nav_data):
    """Convert fund NAV data to {YYYY-MM: price} dict."""
    return {d.strftime("%Y-%m"): float(p) for d, p in fund_nav_data}


def _compute_downside_capture(fund_nav_data, benchmark_name, bm_series=None):
    """Compute downside capture ratio: fund return / benchmark return during down periods."""
    if not fund_nav_data or len(fund_nav_data) < 60:
        return None
    if bm_series is None:
        bm_series = _fetch_benchmark_monthly(benchmark_name)
    if not bm_series or len(bm_series) < 12:
        return None

    fund_series = _fund_monthly_map(fund_nav_data)
    common_months = sorted(set(fund_series.keys()) & set(bm_series.keys()))
    if len(common_months) < 6:
        return None

    fund_ret = 0.0
    bm_ret = 0.0
    count = 0
    for i in range(1, len(common_months)):
        m = common_months[i]
        pm = common_months[i - 1]
        bm_r = (bm_series[m] - bm_series[pm]) / bm_series[pm]
        if bm_r < 0:
            fund_r = (fund_series[m] - fund_series[pm]) / fund_series[pm]
            fund_ret += fund_r
            bm_ret += bm_r
            count += 1
    if count < 3 or bm_ret >= 0:
        return None
    ratio = float(fund_ret / count) / float(bm_ret / count) * 100
    return round(ratio, 2)


def _compute_information_ratio(fund_nav_data, bm_series):
    """Compute 3-year information ratio from NAV history and benchmark monthly returns.

    IR = annualized excess return / annualized tracking error
    Computed over trailing 3-year window.
    """
    if not fund_nav_data or len(fund_nav_data) < 60 or not bm_series or len(bm_series) < 12:
        return None

    # Restrict to trailing 3 years
    end_date = fund_nav_data[-1][0]
    cutoff = end_date - timedelta(days=1095)
    nav_3y = [(d, float(p)) for d, p in fund_nav_data if d >= cutoff]
    if len(nav_3y) < 60:
        nav_3y = fund_nav_data

    fund_series = _fund_monthly_map(nav_3y)
    common = sorted(set(fund_series.keys()) & set(bm_series.keys()))
    if len(common) < 6:
        return None

    excess_returns = []
    for i in range(1, len(common)):
        m = common[i]
        pm = common[i - 1]
        fund_r = (fund_series[m] - fund_series[pm]) / fund_series[pm]
        bm_r = (bm_series[m] - bm_series[pm]) / bm_series[pm]
        excess_returns.append(float(fund_r - bm_r))

    if len(excess_returns) < 5:
        return None

    mean_excess = sum(excess_returns) / len(excess_returns)
    variance = sum((r - mean_excess) ** 2 for r in excess_returns) / (len(excess_returns) - 1)
    tracking_error = math.sqrt(variance) * math.sqrt(12)

    if tracking_error < 1e-10:
        return None

    annualized_excess = mean_excess * 12
    ir = annualized_excess / tracking_error
    return round(ir, 4)


def _fetch_fund_news(fund_name, max_items=5):
    """Fetch latest news about a fund from Google News RSS."""
    from urllib.parse import quote
    try:
        query = quote(f"{fund_name} mutual fund India")
        url = f"https://news.google.com/rss/search?q={query}&hl=en-IN&gl=IN&ceid=IN:en"
        r = requests.get(url, headers=HEADERS, timeout=5)
        if r.status_code != 200:
            return []
        import xml.etree.ElementTree as ET
        root = ET.fromstring(r.text)
        items = []
        for item in root.findall(".//item"):
            title = item.findtext("title", "")
            link = item.findtext("link", "")
            pubdate = item.findtext("pubDate", "")
            source = item.findtext("source", "")
            if title and link:
                items.append({"title": title, "link": link, "date": pubdate[:16] if pubdate else "", "source": source or ""})
                if len(items) >= max_items:
                    break
        return items
    except Exception:
        return []


def fetch_fund_data(slug):
    # In-memory cache: on Render, rely on scheduler (15:36 IST) for refresh
    cached = _FUND_DATA_CACHE.get(slug)
    if cached:
        if ON_RENDER:
            if cached["data"].get("nav_chart") or cached["data"].get("rolling_1y"):
                return cached["data"]
        cached_dt = datetime.fromtimestamp(cached["ts"])
        cached_t = cached_dt.time()
        now_dt = datetime.now()
        now_t = now_dt.time()
        market_open = time(9, 30)
        market_close = time(15, 30)
        should_refetch = (
            cached_dt.date() != now_dt.date() or
            (cached_t < market_open and now_t >= market_open) or
            (cached_t < market_close and now_t >= market_close)
        )
        if not should_refetch:
            return cached["data"]
    url = f"{GROWW_BASE}/mutual-funds/{slug}"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=(6, 8))
    except Exception:
        return cached["data"] if cached else None
    if resp.status_code != 200:
        # Try swapped plan suffix (some plan variants don't exist on Groww)
        for suffix, replacement in _PLAN_SWAP.items():
            if slug.endswith(suffix):
                alt = slug[: -len(suffix)] + replacement
                try:
                    alt_resp = requests.get(f"{GROWW_BASE}/mutual-funds/{alt}", headers=HEADERS, timeout=(6, 8))
                    if alt_resp.status_code == 200:
                        resp = alt_resp
                        slug = alt
                        break
                except Exception:
                    continue
        if resp.status_code != 200:
            return cached["data"] if cached else None
    data = _extract_next_data(resp.text)
    if not data:
        return None

    props = data.get("props", {}).get("pageProps", {})
    scheme_data = props.get("fundDetailsData") or props.get("mfServerSideData") or {}
    seo = props.get("seoDetails", {})

    fund_name = scheme_data.get("fund_name") or seo.get("name") or _extract_name(resp.text) or slug.replace("-", " ").title()
    plan_type = _plan_type(slug)

    nav_data = None
    metrics = {}

    holdings_raw = scheme_data.get("holdings", [])
    portfolio_date = None
    if holdings_raw:
        pd = holdings_raw[0].get("portfolio_date")
        if pd:
            portfolio_date = pd.replace("T", " ").replace("Z", "").split(".")[0]
    holdings = [
        {
            "company": h.get("company_name"),
            "sector": h.get("sector_name"),
            "percent": h.get("corpus_per"),
            "stock_search_id": h.get("stock_search_id"),
        }
        for h in holdings_raw
    ]

    category = scheme_data.get("sub_category") or scheme_data.get("category")

    # ── Determine benchmark from category early (for parallel benchmark fetch) ──
    benchmark_name = _get_benchmark_for_category(category)
    BENCHMARK_KEYS = ["returns_6m", "returns_1y", "returns_3y", "returns_5y", "std_dev", "sharpe", "max_drawdown", "rolling_1y", "rolling_3y", "rolling_5y"]

    # ── Run NAV fetch, PE, PB, and benchmark metrics in parallel (4 workers) ──
    from concurrent.futures import ThreadPoolExecutor, as_completed

    def _do_nav():
        local_nav_data = nav_data
        if local_nav_data is None or len(local_nav_data) <= 30:
            cached_nav = _NAV_CACHE.get(slug)
            nav_stale = True
            if cached_nav and len(cached_nav) > 30:
                local_nav_data = _nav_cache_to_nav_data(cached_nav)
                last_date = local_nav_data[-1][0] if local_nav_data else None
                nav_stale = last_date is None or (datetime.now() - last_date).days > 2
            if nav_stale and (ON_HF or not ON_VERCEL):
                try:
                    scheme_code = _search_mfapi(fund_name, plan_type)

                    if scheme_code:
                        fresh_nav = _fetch_nav_history(scheme_code)
                        if fresh_nav and len(fresh_nav) > 30:
                            _merge_nav_into_cache(slug, fresh_nav)
                            local_nav_data = fresh_nav
                except Exception:
                    pass
        if local_nav_data and len(local_nav_data) > 30:
            local_metrics = _compute_metrics(local_nav_data, plan_type)
        else:
            local_metrics = {}
        return local_nav_data, local_metrics

    def _do_pe():
        local_pe = _scrape_pe_from_groww(scheme_data, resp.text)
        if local_pe is None:
            local_pe = _compute_weighted_pe(holdings_raw)
        return local_pe

    def _do_pb():
        return _compute_weighted_pb(holdings_raw, fetch_timeout=30 if _IS_PREWARM else 5)

    def _do_benchmark():
        if not benchmark_name:
            return {}
        bm_nav = _fetch_benchmark_daily(benchmark_name)
        if bm_nav and len(bm_nav) > 60:
            return _compute_metrics(bm_nav)
        return {}

    with ThreadPoolExecutor(max_workers=4) as ex:
        nav_fut = ex.submit(_do_nav)
        pe_fut = ex.submit(_do_pe)
        pb_fut = ex.submit(_do_pb)
        bm_fut = ex.submit(_do_benchmark)

        nav_data, metrics = nav_fut.result()
        pe_ratio = pe_fut.result()
        pb_ratio = pb_fut.result()
        benchmark_metrics = bm_fut.result()

    # Category PE needs pe_ratio, so run after
    category_pe = _get_category_pe(category)
    if pe_ratio is not None and category_pe is None:
        ck = category.strip().lower() if category else ""
        if ck not in _PREPOPULATING:
            _PREPOPULATING.add(ck)
            import threading
            pt = _plan_type(slug)

            def _fallback_search():
                try:
                    _search_and_record_category_pe(category, fund_name, pt)
                finally:
                    _PREPOPULATING.discard(ck)

            t = threading.Thread(target=_fallback_search, daemon=True)
            t.start()
            t.join(timeout=5)
            category_pe = _get_category_pe(category)

    for h in holdings:
        if h.get("company"):
            cached = _lookup_pe_cache(h["company"])
            if cached is not None and isinstance(cached, dict):
                pe = cached.get("pe")
                h["stock_pe"] = pe if isinstance(pe, (int, float)) else None
                pb = cached.get("pb")
                h["stock_pb"] = pb if isinstance(pb, (int, float)) else None
            else:
                h["stock_pe"] = None
                h["stock_pb"] = None
    result = {
        "name": fund_name,
        "slug": slug,
        "plan": plan_type,
        "nav": _to_float(scheme_data.get("nav")),
        "aum": _to_float(scheme_data.get("aum")),
        "expense_ratio": _to_float(scheme_data.get("expense_ratio")),
        "category": category,
        "risk": scheme_data.get("risk"),
        "pe_ratio": pe_ratio,
        "pb_ratio": pb_ratio,
        "category_pe": category_pe,
        "launch_date": scheme_data.get("launch_date"),
        "holdings": holdings,
        "portfolio_date": portfolio_date,
        "nav_date": scheme_data.get("nav_date"),
        "source": "groww",
    }

    # ── Extract predictive metrics from Groww return_stats ──
    rs = (scheme_data.get("return_stats") or [None])[0]
    if rs:
        result["alpha"] = rs.get("alpha")
        result["sharpe_ratio"] = rs.get("sharpe_ratio")
        result["sortino_ratio"] = rs.get("sortino_ratio")
        result["beta"] = rs.get("beta")
        result["std_dev_groww"] = rs.get("standard_deviation")
        result["rank_3y"] = rs.get("rank3yr")
        result["rank_5y"] = rs.get("rank5yr")
        result["cat_return_3y"] = rs.get("cat_return3y")
        result["cat_return_5y"] = rs.get("cat_return5y")
        result["cat_return_1y"] = rs.get("cat_return1y")
        result["index_return_3y"] = rs.get("index_return3y")
        result["index_return_5y"] = rs.get("index_return5y")
        result["portfolio_turnover"] = scheme_data.get("portfolio_turnover")
        result["groww_rating"] = scheme_data.get("groww_rating")
        result["fund_manager"] = _extract_manager_name(scheme_data)
        # CAGR fallback from Groww (overwritten by mfapi compute if available)
        for gr_key, out_key in [("return1y", "returns_1y"), ("return3y", "returns_3y"),
                                 ("return5y", "returns_5y"), ("return6m", "returns_6m")]:
            val = rs.get(gr_key)
            if val is not None:
                result[out_key] = round(float(val), 2)

    tenure_days = _manager_tenure_days(scheme_data)
    result["manager_avg_tenure_days"] = round(tenure_days) if tenure_days is not None else None
    result["fund_managers"] = _extract_fund_managers(scheme_data)

    result.update(metrics)

    # ── Attach benchmark metrics computed in parallel ──
    if benchmark_name and benchmark_metrics:
        result["benchmark_name"] = benchmark_name
        result["benchmark_chart"] = benchmark_metrics.get("nav_chart")
        for k in BENCHMARK_KEYS:
            if k in benchmark_metrics:
                result["benchmark_" + k] = benchmark_metrics[k]

    bm_name = scheme_data.get("benchmark_name") or benchmark_name
    result["downside_capture_benchmark"] = bm_name
    if nav_data and len(nav_data) > 30:
        benchmark_series = _fetch_benchmark_monthly(bm_name)
        if benchmark_series is None and category:
            cat_bm = _get_benchmark_for_category(category)
            if cat_bm:
                benchmark_series = _fetch_benchmark_monthly(cat_bm)
                if benchmark_series:
                    bm_name = cat_bm
                    result["downside_capture_benchmark"] = cat_bm
        try:
            result["downside_capture"] = _compute_downside_capture(nav_data, bm_name, benchmark_series)
        except Exception:
            result["downside_capture"] = None
        try:
            computed_ir = _compute_information_ratio(nav_data, benchmark_series)
            if computed_ir is not None:
                result["information_ratio"] = computed_ir
            else:
                result.pop("information_ratio", None)
        except Exception:
            result.pop("information_ratio", None)
    else:
        result["downside_capture"] = None
        result.pop("information_ratio", None)

    _FUND_DATA_CACHE[slug] = {"data": result, "ts": time_module.time()}
    _save_fund_data_cache()
    return result


def _extract_manager_name(scheme_data):
    details = scheme_data.get("fund_manager_details")
    if details and isinstance(details, list) and len(details) > 0:
        names = [d.get("person_name", "") for d in details if d.get("person_name")]
        if names:
            return names[0]
    return scheme_data.get("fund_manager")


def _manager_tenure_days(scheme_data):
    """Compute earliest manager start date tenure in days from fund_manager_details."""
    details = scheme_data.get("fund_manager_details")
    if not details or not isinstance(details, list) or len(details) == 0:
        return None
    tenures = []
    for d in details:
        date_from = d.get("date_from")
        if date_from:
            try:
                dt = datetime.fromisoformat(date_from.replace("Z", "+00:00"))
                delta = datetime.now(timezone.utc) - dt
                tenures.append(delta.days)
            except (ValueError, TypeError):
                continue
    if not tenures:
        return None
    return sum(tenures) / len(tenures)


def _extract_fund_managers(scheme_data):
    """Extract detailed info for all fund managers: name, tenure, experience."""
    details = scheme_data.get("fund_manager_details")
    if not details or not isinstance(details, list) or len(details) == 0:
        return None
    managers = []
    for d in details:
        name = d.get("person_name", "").strip()
        if not name:
            continue
        tenure_years = None
        date_from = d.get("date_from")
        if date_from:
            try:
                dt = datetime.fromisoformat(date_from.replace("Z", "+00:00"))
                days = (datetime.now(timezone.utc) - dt).days
                tenure_years = round(days / 365, 1)
            except (ValueError, TypeError):
                pass
        exp_text = d.get("experience", "").strip()
        managers.append({
            "name": name,
            "tenure_years": tenure_years,
            "experience": exp_text,
        })
    return managers if managers else None


def _scrape_pe_from_groww(scheme_data, html):
    raw = scheme_data.get("pe_ratio") or scheme_data.get("pe") or scheme_data.get("p_e_ratio")
    if raw:
        return float(raw)

    patterns = [
        r'P/E\s*[:\-]?\s*(\d+\.?\d*)',
        r'PE\s+Ratio\s*[:\-]?\s*(\d+\.?\d*)',
    ]
    for pat in patterns:
        m = re.search(pat, html, re.IGNORECASE)
        if m:
            return float(m.group(1))

    soup = BeautifulSoup(html, "html.parser")
    labels = soup.find_all(["span", "div", "p", "label"], string=re.compile(r"P/E|PE Ratio", re.I))
    for el in labels:
        parent = el.find_parent(["div", "li", "td"])
        if parent:
            val = parent.find(["span", "div", "p"])
            if val:
                try:
                    return float(val.get_text(strip=True))
                except ValueError:
                    pass
        sibling = el.find_next_sibling(["span", "div", "p"])
        if sibling:
            try:
                return float(sibling.get_text(strip=True))
            except ValueError:
                pass

    return None


def _extract_next_data(html):
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
    if not m:
        return None
    return json.loads(m.group(1))


def _record_category_pe(category, pe):
    if not category or pe is None:
        return
    key = category.strip().lower()
    if key not in _CATEGORY_PE_CACHE:
        _CATEGORY_PE_CACHE[key] = {"pes": [], "avg": None}
    entry = _CATEGORY_PE_CACHE[key]
    if pe not in entry["pes"]:
        entry["pes"].append(pe)
        entry["avg"] = round(sum(entry["pes"]) / len(entry["pes"]), 2)
        _save_category_pe_cache()


def _get_category_pe(category):
    if not category:
        return None
    key = category.strip().lower()
    entry = _CATEGORY_PE_CACHE.get(key)
    if entry and entry["avg"] is not None:
        return {"pe": entry["avg"], "count": len(entry["pes"])}
    alias = _CATEGORY_PE_ALIAS.get(key)
    if alias:
        entry = _CATEGORY_PE_CACHE.get(alias)
        if entry and entry["avg"] is not None:
            return {"pe": entry["avg"], "count": len(entry["pes"])}
    return None


_PREPOPULATING = set()
_ENRICH_IN_FLIGHT = set()
_DDG_SEARCH_URL = "https://html.duckduckgo.com/html/"

# Map category names to existing PE cache entries for categories that share the same PE profile
_CATEGORY_PE_ALIAS = {
    "value oriented": "value",
    "elss": "large cap",
    "multi cap": "flexi cap",
    "focused fund": "flexi cap",
    "large & midcap": "large & midcap",
}


def _search_and_record_category_pe(category_name, fund_name=None, plan_type=None):
    """Search web for a category's average PE and record it in cache."""
    pe = _search_category_pe(category_name, fund_name=fund_name, plan_type=plan_type)
    if pe is not None:
        _record_category_pe(category_name, pe)




# Known-good fund names for each category on rethinkwealth.in
_CATEGORY_SEARCH = {
    "flexi cap": "HDFC Flexi Cap Fund",
    "large cap": "HDFC Large Cap Fund",
    "large & midcap": "HDFC Large and Mid Cap Fund",
    "mid cap": "HDFC Mid Cap Fund",
    "small cap": "HDFC Small Cap Fund",
    "value": "HDFC Value Fund",
    "dividend yield": "HDFC Dividend Yield Fund",
    "aggressive hybrid": "HDFC Hybrid Equity Fund",
    "multi cap": "Quant Multi Cap Fund",
    "elss": "Axis ELSS Tax Saver Fund",
    "focused fund": "HDFC Focused 30 Fund",
}


def _scrape_rethink_pe(slug):
    """Extract category_avg_pe from a rethinkwealth.in fund page."""
    url = f"https://rethinkwealth.in/mutual-fund/{slug}"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=8)
        if resp.status_code != 200:
            return None
        m = re.search(r'category_avg_pe[\\\":]+([\d.]+)', resp.text)
        if m:
            return round(float(m.group(1)), 2)
    except Exception:
        pass
    return None


def _search_category_pe(category_name, fund_name=None, plan_type=None):
    """Search for a category's average PE from rethinkwealth.in."""
    if not fund_name:
        return None
    slug = fund_name.lower().replace(" ", "-")
    suffix = "-growth-option-direct-plan" if (plan_type and "direct" in plan_type.lower()) else "-growth-plan"
    return _scrape_rethink_pe(f"{slug}{suffix}")


def _prebuild_category_pe():
    """Pre-build category PE cache via rethinkwealth for each category."""
    tasks = []
    for cat_name, fund_name in _CATEGORY_SEARCH.items():
        key = cat_name.lower().strip()
        entry = _CATEGORY_PE_CACHE.get(key)
        if entry and len(entry.get("pes", [])) >= 1:
            continue
        tasks.append((cat_name, fund_name, "Direct"))

    from concurrent.futures import ThreadPoolExecutor, as_completed
    with ThreadPoolExecutor(max_workers=10) as ex:
        fut_map = {ex.submit(_search_category_pe, cat, fund, plan): cat for cat, fund, plan in tasks}
        for fut in as_completed(fut_map):
            cat = fut_map[fut]
            try:
                pe = fut.result()
                if pe is not None:
                    _record_category_pe(cat, pe)
            except Exception:
                pass


# Load existing mcap rankings; rebuild in background if stale
_load_mcap_rankings()
if _mcap_cache_needs_refresh():
    import threading
    threading.Thread(target=_rebuild_mcap_rankings, daemon=True).start()

# Pre-build category PE cache in background
import threading
threading.Thread(target=_prebuild_category_pe, daemon=True).start()


def fetch_funds_compare(slugs):
    """Fetch fund data for multiple slugs, serving cached data when available.

    Returns cached data instantly for already-viewed funds; only fetches
    uncached funds from groww (first time after refresh). Preserves slug order.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    results = {}
    to_fetch = []
    for s in slugs:
        cached = _FUND_DATA_CACHE.get(s)
        if cached:
            results[s] = cached["data"]
        else:
            to_fetch.append(s)
    if to_fetch:
        with ThreadPoolExecutor(max_workers=min(len(to_fetch), 10)) as ex:
            fut_to_slug = {ex.submit(fetch_fund_data, s): s for s in to_fetch}
            for fut in as_completed(fut_to_slug):
                slug = fut_to_slug[fut]
                try:
                    data = fut.result()
                    if data:
                        results[slug] = data
                except Exception:
                    continue
    ordered = {}
    for s in slugs:
        if s in results:
            ordered[s] = results[s]
    return ordered


# ponytail: non-stock patterns for portfolio overlap filtering
_NON_STOCK_PATTERNS = (
    "repo", "reverse repo", "cblo", "treps", "cash margin", "net payables",
    "net receivables", "net current assets", "cash and cash equivalent",
    "others cblo", "future",
)


def _is_stock_holding(name):
    if not name:
        return False
    name_lower = name.lower().strip()
    if name_lower.endswith(" future") or name_lower.endswith(" futures"):
        return False
    for pat in _NON_STOCK_PATTERNS:
        if pat in name_lower:
            return False
    return True


def compute_overlap_matrix(funds_data):
    """Compute portfolio overlap metrics between all fund pairs.

    Returns two matrices:
      overlap (Sum-of-Minimums %): sum(min(a%, b%)) for common stocks,
        measuring what fraction of A's portfolio is also in B (and vice versa).
      overlap_combined (Combined Pool %): sum(min) / (total_A + total_B) * 100,
        measuring common holdings as a fraction of the combined pool (advisorkhoj style).

    Non-stock items (Repo, Cash, CBLO, Futures, etc.) are excluded.
    """
    slugs = list(funds_data.keys())
    holdings_map = {}
    totals_map = {}
    for slug in slugs:
        fund = funds_data[slug]
        holdings = {}
        total = 0.0
        for h in fund.get("holdings", []):
            name = h.get("company")
            pct = h.get("percent")
            if name and pct is not None and _is_stock_holding(name):
                pct_f = float(pct)
                holdings[name] = pct_f
                total += pct_f
        holdings_map[slug] = holdings
        totals_map[slug] = total

    overlap = {}
    overlap_combined = {}
    for s1 in slugs:
        overlap[s1] = {}
        overlap_combined[s1] = {}
        for s2 in slugs:
            if s1 == s2:
                overlap[s1][s2] = 100.0
                overlap_combined[s1][s2] = 100.0
                continue
            h1 = holdings_map.get(s1, {})
            h2 = holdings_map.get(s2, {})
            common = set(h1.keys()) & set(h2.keys())
            if not common:
                overlap[s1][s2] = 0.0
                overlap_combined[s1][s2] = 0.0
                continue
            s = sum(min(h1[c], h2[c]) for c in common)
            overlap[s1][s2] = round(s, 2)
            total = totals_map.get(s1, 0) + totals_map.get(s2, 0)
            overlap_combined[s1][s2] = round(s / total * 100, 2) if total else 0.0
    return overlap, overlap_combined


def compute_jaccard_matrix(funds_data):
    """Compute NxN Jaccard index matrix as percentage.

    For each pair of funds, Jaccard = |A ∩ B| / |A ∪ B| * 100
    (count of common stocks divided by total unique stocks).
    Returns {slug: {slug: float}}.
    """
    slugs = list(funds_data.keys())
    holdings_map = {}
    for slug in slugs:
        fund = funds_data[slug]
        names = set()
        for h in fund.get("holdings", []):
            name = h.get("company")
            if name:
                names.add(name)
        holdings_map[slug] = names

    matrix = {}
    for s1 in slugs:
        matrix[s1] = {}
        for s2 in slugs:
            if s1 == s2:
                matrix[s1][s2] = 100.0
                continue
            h1 = holdings_map.get(s1, set())
            h2 = holdings_map.get(s2, set())
            if not h1 or not h2:
                matrix[s1][s2] = 0.0
                continue
            intersection = h1 & h2
            union = h1 | h2
            if not union:
                matrix[s1][s2] = 0.0
                continue
            matrix[s1][s2] = round(len(intersection) / len(union) * 100, 2)
    return matrix


def compute_correlation_matrix(funds_data, years):
    """Compute NxN Pearson correlation matrix of daily returns.

    years: slice NAV data to last N years. Returns {slug: {slug: float}}.
    """
    import statistics
    from math import sqrt

    slugs = list(funds_data.keys())
    cutoff = None
    if years and years > 0:
        from datetime import datetime, timedelta
        # find latest date across all funds
        latest = None
        for slug in slugs:
            nav = funds_data[slug].get("nav_chart")
            if nav and len(nav) >= 2:
                d = nav[-1][0]
                if latest is None or d > latest:
                    latest = d
        if latest:
            cutoff = datetime.strptime(latest, "%Y-%m-%d") - timedelta(days=int(years * 365))

    returns_map = {}
    for slug in slugs:
        nav_chart = funds_data[slug].get("nav_chart")
        if not nav_chart or len(nav_chart) < 2:
            continue
        sliced = nav_chart
        if cutoff:
            sliced = [d for d in nav_chart if datetime.strptime(d[0], "%Y-%m-%d") >= cutoff]
        if len(sliced) < 2:
            continue
        returns = []
        for i in range(1, len(sliced)):
            prev = sliced[i - 1][1]
            curr = sliced[i][1]
            if prev and curr and prev > 0:
                returns.append((curr - prev) / prev)
        if len(returns) >= 2:
            returns_map[slug] = returns

    matrix = {}
    for s1 in slugs:
        matrix[s1] = {}
        for s2 in slugs:
            if s1 == s2:
                matrix[s1][s2] = 100.0
                continue
            r1 = returns_map.get(s1)
            r2 = returns_map.get(s2)
            if not r1 or not r2:
                matrix[s1][s2] = 0.0
                continue
            n = min(len(r1), len(r2))
            if n < 2:
                matrix[s1][s2] = 0.0
                continue
            r1 = r1[-n:]
            r2 = r2[-n:]
            mean1 = statistics.mean(r1)
            mean2 = statistics.mean(r2)
            num = sum((a - mean1) * (b - mean2) for a, b in zip(r1, r2))
            den = sqrt(sum((a - mean1) ** 2 for a in r1)) * sqrt(sum((b - mean2) ** 2 for b in r2))
            if den == 0:
                matrix[s1][s2] = 0.0
            else:
                matrix[s1][s2] = round(num / den * 100, 2)
    return matrix
