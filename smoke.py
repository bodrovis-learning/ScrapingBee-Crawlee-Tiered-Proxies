
"""Minimal Requests smoke test before involving Crawlee.

Usage:
    uv run python smoke.py --mode direct
    uv run python smoke.py --mode classic
    uv run python smoke.py --mode premium
    uv run python smoke.py --mode stealth
"""

import argparse
import os

import requests
from dotenv import load_dotenv

from proxy_config import scrapingbee_proxy_url


def main() -> None:
    parser = argparse.ArgumentParser(
        description="One-request network sanity check"
    )
    parser.add_argument(
        "--mode",
        choices=("direct", "classic", "premium", "stealth"),
        default="direct",
    )
    parser.add_argument(
        "--url",
        default="https://books.toscrape.com/",
    )
    args = parser.parse_args()
    load_dotenv()

    kwargs: dict = {"timeout": 90}

    if args.mode != "direct":
        api_key = os.getenv("SCRAPINGBEE_API_KEY", "")

        if not api_key.strip():
            parser.error("Set SCRAPINGBEE_API_KEY in .env")

        proxy = scrapingbee_proxy_url(
            api_key,
            premium=args.mode == "premium",
            stealth=args.mode == "stealth",
            render_js=args.mode in ("premium", "stealth"),
        )

        kwargs["proxies"] = {
            "http": proxy,
            "https": proxy,
        }

        # Required by ScrapingBee Proxy Mode.
        # Only for testing public, unauthenticated pages.
        kwargs["verify"] = False

        print("WARNING: TLS certificate verification disabled")

    try:
        response = requests.get(args.url, **kwargs)

        print(f"URL: {args.url}")
        print(f"Mode: {args.mode}")
        print(f"Status: {response.status_code}")
        print(f"Bytes: {len(response.content)}")

        if args.mode != "direct":
            print(
                "ScrapingBee reported cost: "
                f"{response.headers.get('Spb-cost', 'not exposed')}"
            )

    except requests.RequestException as exc:
        # Exceptions may contain credential-bearing proxy URLs.
        print(
            f"Request failed: {type(exc).__name__} "
            "(details hidden to protect API key)"
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
