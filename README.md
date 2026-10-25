# Crawlee + ScrapingBee: tiered-proxy lab

A deliberately small Python experiment for an article about **tiered proxies and concurrency in Crawlee**. No recursion, no browser installation, and only a few initial URLs. The requested website names are **candidate difficulty labels**, not verified real-world block ratings.

## Setup (uv)

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```powershell
uv sync
Copy-Item .env.example .env
```

Add your `SCRAPINGBEE_API_KEY` to `.env` (it is gitignored). `pyproject.toml` declares `requests`, `crawlee[beautifulsoup]`, and `python-dotenv`; `uv sync` installs them.

## First runs

```powershell
# Direct request smoke test, no API key required
uv run python smoke.py --mode direct

# One ScrapingBee classic-proxy request
uv run python smoke.py --mode classic

# 3 easy sites with no proxy (baseline)
uv run python crawler.py --mode direct --difficulty easy

# Same sites, Crawlee tiered proxy selection
uv run python crawler.py --mode tiered --difficulty easy

# Then try medium sites (3 URLs)
uv run python crawler.py --mode tiered --difficulty medium
```

Everything under `--difficulty hard` is **disabled** until explicitly selected with `--include-disabled`. Review each site's terms and access policies before enabling it. Crawlee is configured to respect robots.txt. Do not use this lab to access authenticated or personal data.

## How the three Crawlee tiers map to ScrapingBee

| Crawlee tier | Proxy mode parameters | Standard HTML API credits per successful request |
| --- | --- | ---: |
| 0 | `render_js=False` | 1 |
| 1 | `render_js=False&premium_proxy=True` | 10 |
| 2 | `render_js=True&premium_proxy=True` | 25 |

Google and some specialized targets have **different billing rules**. `proxy_tier` is the *tier used on the successful request*, not a complete record of retries or API costs. Actual credits should be checked in the ScrapingBee dashboard. Failure handling depends on the HTTP status and whether a challenge is recognized; a 200 OK challenge page can be misclassified as success.

ScrapingBee's Proxy Mode is an **API front-end**, not direct access to independent raw IP pools. Multiple Crawlee tiers represent different *ScrapingBee request configurations*. JavaScript rendering on tier 2 happens at ScrapingBee, not in BeautifulSoupCrawler. Crawlee's own proxy selection can be compared later with ScrapingBee's `mode=auto`, but do **not** combine them in the first experiment.

## TLS caveat

ScrapingBee Proxy Mode [currently documents](https://www.scrapingbee.com/documentation/proxy-mode/) that callers must turn off SSL certificate verification. `smoke.py` sets `verify=False` only when in proxy mode; `crawler.py` uses `ImpitHttpClient(verify=False)` only when in classic/tiered mode. **Do not copy this TLS bypass into production or use it for passwords, user sessions, or confidential data.** Direct-mode requests retain normal TLS verification.

## Files

- `sites.json`: 9 candidate URLs, difficulty, enabled flag and notes.
- `proxy_config.py`: API-key-safe proxy URL construction and tiers.
- `smoke.py`: one-request `requests` diagnostic.
- `crawler.py`: small concurrent `BeautifulSoupCrawler` with retries, session rotation, tier selection and per-run result files.
- `runs/<timestamp>/results.json`: successful/failed URLs, HTML titles, status, and observed tiers (never raw proxy URLs).
- `runs/<timestamp>/summary.json`: counts and elapsed time (not actual billing totals).

No network calls occur unless you explicitly execute the scripts. There is no automated result collection yet and no claim that the targets have been successfully scraped.

## Testing the helpers

```powershell
uv run python -m unittest discover -s tests
```

## Useful docs

- [Crawlee proxy management](https://crawlee.dev/python/docs/guides/proxy-management)
- [Crawlee scaling](https://crawlee.dev/python/docs/guides/scaling-crawlers)
- [ScrapingBee Proxy Mode](https://www.scrapingbee.com/documentation/proxy-mode/)
- [ScrapingBee HTML API costs](https://www.scrapingbee.com/documentation/)
