
"""ScrapingBee Proxy Mode URL helpers (no network calls)."""

from urllib.parse import quote

# ScrapingBee's HTTP proxy front-end.
PROXY_HOST = "proxy.scrapingbee.com"
PROXY_PORT = 8886

# Tier index -> proxy configuration.
# Credits per successful request: 0, 1, 25, 75.
TIER_DESCRIPTIONS = {
    0: "direct",
    1: "classic-no-js",
    2: "premium-with-js",
    3: "stealth-with-js",
}


def scrapingbee_proxy_url(
    api_key: str,
    *,
    premium: bool = False,
    stealth: bool = False,
    render_js: bool = False,
) -> str:
    """Construct an authenticated ScrapingBee proxy URL.

    The URL contains a secret. Never log or store it.
    """
    if not api_key or not api_key.strip():
        raise ValueError("SCRAPINGBEE_API_KEY is required")

    if premium and stealth:
        raise ValueError("Premium and stealth proxies are mutually exclusive")

    if stealth and not render_js:
        raise ValueError("Stealth proxies require JavaScript rendering")

    params = f"render_js={render_js}"

    if premium:
        params += "&premium_proxy=True"

    if stealth:
        params += "&stealth_proxy=True"

    username = quote(api_key.strip(), safe="")
    password = quote(params, safe="")

    return f"http://{username}:{password}@{PROXY_HOST}:{PROXY_PORT}"


def proxy_tiers(api_key: str) -> list[list[str | None]]:
    """Return proxy tiers ordered from cheapest to most expensive."""
    return [
        [None],
        [scrapingbee_proxy_url(api_key)],
        [scrapingbee_proxy_url(
            api_key, premium=True, render_js=True
        )],
        [scrapingbee_proxy_url(
            api_key, stealth=True, render_js=True
        )],
    ]
