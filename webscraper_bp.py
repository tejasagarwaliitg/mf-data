from flask import Blueprint, request, render_template, jsonify
import scraper

webscraper_bp = Blueprint('webscraper', __name__)

_BM_CACHE = {}

def refresh_scraper_caches():
    old_fund_slugs = list(scraper._FUND_DATA_CACHE.keys())
    nav_slugs = list(scraper._NAV_CACHE.keys())
    _BM_CACHE.clear()
    scraper._PE_CACHE.clear()
    scraper._FUND_INDEX = None

    all_slugs = list(dict.fromkeys(old_fund_slugs + nav_slugs))

    was_allow = scraper._PE_ALLOW_NETWORK
    was_prewarm = scraper._IS_PREWARM
    scraper._PE_ALLOW_NETWORK = True
    scraper._IS_PREWARM = True
    try:
        for slug in all_slugs:
            try:
                scraper.fetch_fund_data(slug)
            except Exception:
                pass
    finally:
        scraper._PE_ALLOW_NETWORK = was_allow
        scraper._IS_PREWARM = was_prewarm

    scraper._save_fund_data_cache()
    scraper._save_nav_cache()

BENCHMARK_KEYS = ["returns_6m", "returns_1y", "returns_3y", "returns_5y", "std_dev", "sharpe", "max_drawdown", "rolling_1y", "rolling_3y", "rolling_5y"]

def _get_benchmark_metrics(bm_name):
    if not bm_name:
        return None
    if bm_name not in _BM_CACHE:
        bm_nav = scraper._fetch_benchmark_daily(bm_name)
        if bm_nav and len(bm_nav) > 60:
            _BM_CACHE[bm_name] = scraper._compute_metrics(bm_nav)
        else:
            _BM_CACHE[bm_name] = None
    return _BM_CACHE.get(bm_name)

def _attach_benchmark(data):
    if data.get("benchmark_name") and data.get("benchmark_chart"):
        return  # Already computed in parallel during fetch_fund_data
    bm_name = scraper._get_benchmark_for_category(data.get("category"))
    bm_metrics = _get_benchmark_metrics(bm_name)
    if not bm_metrics:
        return
    data["benchmark_name"] = bm_name
    data["benchmark_chart"] = bm_metrics.get("nav_chart")
    for key in BENCHMARK_KEYS:
        if key in bm_metrics:
            data["benchmark_" + key] = bm_metrics[key]

@webscraper_bp.route("/")
def index():
    return render_template("scraper_index.html")

@webscraper_bp.route("/api/ping")
def api_ping():
    return jsonify({"ok": True})

@webscraper_bp.route("/api/search")
def api_search():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"results": []})
    try:
        results = scraper.search_fund(q)
        return jsonify({"results": results})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@webscraper_bp.route("/api/fund/<slug>")
def api_fund(slug):
    try:
        data = scraper.fetch_fund_data(slug)
        if not data:
            return jsonify({"error": "Not found"}), 404
        _attach_benchmark(data)
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@webscraper_bp.route("/api/compare")
def api_compare():
    slugs_str = request.args.get("slugs", "").strip()
    slugs = [s.strip() for s in slugs_str.split(",") if s.strip()]
    if len(slugs) < 2:
        return jsonify({"error": "Select at least 2 funds"}), 400
    try:
        funds = scraper.fetch_funds_compare(slugs)
        overlap, overlap_combined = scraper.compute_overlap_matrix(funds)
        jaccard = scraper.compute_jaccard_matrix(funds)
        correlation_1y = scraper.compute_correlation_matrix(funds, 1)
        correlation_3y = scraper.compute_correlation_matrix(funds, 3)
        correlation_5y = scraper.compute_correlation_matrix(funds, 5)
        dominant_cat = scraper._get_dominant_category(funds)
        if dominant_cat:
            benchmark_name = scraper._get_benchmark_for_category(dominant_cat)
            bm_metrics = _get_benchmark_metrics(benchmark_name)
            if bm_metrics:
                bm_entry = {"name": benchmark_name, "slug": "__benchmark__", "plan": "Index", "category": benchmark_name, "nav": None, "aum": None, "expense_ratio": None, "pe_ratio": None, "pb_ratio": None, "launch_date": None, "holdings": [], "source": "yfinance"}
                bm_entry.update(bm_metrics)
                funds["__benchmark__"] = bm_entry
        return jsonify({"funds": funds, "overlap": overlap, "overlap_combined": overlap_combined, "jaccard": jaccard, "correlation_1y": correlation_1y, "correlation_3y": correlation_3y, "correlation_5y": correlation_5y})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
