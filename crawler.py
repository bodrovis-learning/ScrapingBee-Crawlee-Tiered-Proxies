
"""Small Crawlee / ScrapingBee proxy tier lab.

Examples:
    uv run python crawler.py --mode tiered --difficulty medium
    uv run python crawler.py --mode tiered --site imdb_top --retry-blocked
    uv run python crawler.py --mode direct --difficulty easy --no-llm

A targeted --retry-blocked run can escalate proxy tiers based on local
Ollama classification. Navigation attempts and final outcomes are
recorded separately.
"""

import argparse
import asyncio
from collections import Counter
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from time import perf_counter

from crawlee import ConcurrencySettings
from crawlee.crawlers import (
    BasicCrawlingContext,
    BeautifulSoupCrawler,
    BeautifulSoupCrawlingContext,
    HttpCrawlingContext,
)
from crawlee.errors import SessionError
from crawlee.http_clients import ImpitHttpClient
from crawlee.proxy_configuration import ProxyConfiguration
from dotenv import load_dotenv

from proxy_config import (
    TIER_DESCRIPTIONS,
    proxy_tiers,
    scrapingbee_proxy_url,
)

ROOT = Path(__file__).resolve().parent


class AttemptLimitReached(Exception):
    """Stop a request after its network attempt budget is exhausted."""


def load_sites(
    path: Path,
    difficulty: str,
    include_disabled: bool,
    site_id: str | None,
) -> list[dict]:
    entries = json.loads(path.read_text(encoding="utf-8"))

    if site_id is not None:
        selected = [
            entry for entry in entries
            if entry["id"] == site_id
        ]

        if not selected:
            raise SystemExit(f"Unknown site ID: {site_id}")

        if not selected[0].get("enabled", False) and not include_disabled:
            raise SystemExit(
                "Site is disabled; pass --include-disabled to run it"
            )

        return selected

    return [
        entry
        for entry in entries
        if (difficulty == "all" or entry["difficulty"] == difficulty)
        and (include_disabled or entry.get("enabled", False))
    ]


def is_challenge_page(title: str, body_start: str) -> bool:
    """Cheap diagnostic heuristic, not a comprehensive detector."""
    lower_title = title.casefold()
    lower_body = body_start.casefold()

    if any(
        term in lower_title
        for term in (
            "just a moment",
            "access denied",
            "captcha",
            "robot check",
        )
    ):
        return True

    return lower_body.startswith((
        "please verify you are a human",
        "unusual traffic from your computer network",
    ))


def get_proxy_details(
    context: BasicCrawlingContext,
    mode: str,
) -> tuple[int | None, str]:
    """Report proxy tier without exposing authenticated proxy URLs."""

    if mode == "direct":
        tier = 0
    elif mode == "classic":
        tier = 1
    elif context.proxy_info is not None:
        tier = context.proxy_info.proxy_tier
    else:
        # Crawlee may return no ProxyInfo for the direct tier ([None]).
        tier = context.request.last_proxy_tier

        if tier is None:
            tier = 0

    return tier, TIER_DESCRIPTIONS.get(tier, "unknown")


def prepare_page_text(
    soup,
    site_id: str,
    limit: int = 8000,
) -> str:
    """Prefer relevant page content over navigation menus."""

    selectors = {
        "github_trending": "article.Box-row",
        "ebay_search": "li.s-item",
    }

    selector = selectors.get(site_id)
    elements = soup.select(selector) if selector else []

    # eBay fallback if the main selector does not match.
    if site_id == "ebay_search" and not elements:
        links = soup.select('a[href*="/itm/"]')
        elements = [
            link.find_parent("li") or link.parent
            for link in links
        ]

    if elements:
        snippets = []
        seen = set()
        remaining = limit

        for element in elements:
            if element is None:
                continue

            snippet = element.get_text(" ", strip=True)

            if len(snippet) < 30 or snippet in seen:
                continue

            seen.add(snippet)

            snippets.append(
                snippet[:min(600, remaining)]
            )

            remaining -= len(snippets[-1]) + 1

            if remaining <= 0:
                break

        if snippets:
            return "\n".join(snippets)[:limit]

    # Generic fallback: sample the beginning, middle and end.
    text = soup.get_text(" ", strip=True)

    if len(text) <= limit:
        return text

    part = limit // 3
    middle = len(text) // 2 - part // 2

    return (
        f"START:\n{text[:part]}\n"
        f"MIDDLE:\n{text[middle:middle + part]}\n"
        f"END:\n{text[-part:]}"
    )[:limit]


async def crawl(
    mode: str,
    difficulty: str,
    include_disabled: bool,
    site_id: str | None,
    max_concurrency: int,
    max_attempts: int,
    use_llm: bool,
    retry_blocked: bool,
) -> None:
    load_dotenv(ROOT / ".env")

    # page_quality reads OLLAMA_MODEL at import time.
    if use_llm:
        from page_quality import assess_page

    sites = load_sites(
        ROOT / "sites.json",
        difficulty,
        include_disabled,
        site_id,
    )

    if not sites:
        raise SystemExit(
            "No enabled sites selected. Try --include-disabled."
        )

    lookup = {
        entry["url"]: entry
        for entry in sites
    }

    proxy_config = None

    if mode != "direct":
        api_key = os.getenv("SCRAPINGBEE_API_KEY", "")

        if not api_key.strip():
            raise SystemExit(
                "Set SCRAPINGBEE_API_KEY in .env"
            )

        if mode == "tiered":
            # 0: Direct
            # 1: Classic without JS
            # 2: Premium with JS
            # 3: Stealth with JS
            proxy_config = ProxyConfiguration(
                tiered_proxy_urls=proxy_tiers(api_key)
            )
        else:
            proxy_config = ProxyConfiguration(
                proxy_urls=[
                    scrapingbee_proxy_url(api_key)
                ]
            )

    # ScrapingBee Proxy Mode requires verify=False.
    # In tiered mode, this also affects direct requests.
    # Do not use this setup for private or sensitive data.
    crawler = BeautifulSoupCrawler(
        proxy_configuration=proxy_config,
        http_client=ImpitHttpClient(
            verify=(mode == "direct")
        ),
        concurrency_settings=ConcurrencySettings(
            desired_concurrency=max_concurrency,
            max_concurrency=max_concurrency,
            max_tasks_per_minute=180,
        ),
        navigation_timeout=timedelta(seconds=50),
        request_handler_timeout=timedelta(minutes=3),
        max_request_retries=1,
        max_session_rotations=3,
        max_requests_per_crawl=len(sites),
        respect_robots_txt_file=False,
    )

    success_rows: list[dict] = []
    failed_rows: list[dict] = []

    attempts: list[dict] = []
    latest_attempt: dict[str, dict] = {}

    # Only one local LLM inference at a time.
    llm_semaphore = asyncio.Semaphore(1)

    @crawler.pre_navigation_hook
    async def record_attempt(
        context: BasicCrawlingContext,
    ) -> None:
        """Record navigation attempts and enforce the attempt limit."""

        request = context.request

        attempt_number = (
            int(request.user_data.get("lab_attempt", 0)) + 1
        )

        # Limit network starts independently of Crawlee's
        # retry and session rotation counters.
        if attempt_number > max_attempts:
            request.no_retry = True

            raise AttemptLimitReached(
                f"Exceeded {max_attempts} network attempts"
            )

        request.user_data["lab_attempt"] = attempt_number

        tier, proxy_type = get_proxy_details(context, mode)

        attempt = {
            "id": lookup.get(request.url, {}).get(
                "id", "unknown"
            ),
            "url": request.url,
            "attempt": attempt_number,
            "proxy_tier": tier,
            "proxy_type": proxy_type,
            "http_status": None,
            "quality": None,
            "quality_reason": None,
            "outcome": "started",
            "error_type": None,
            "time_utc": datetime.now(
                timezone.utc
            ).isoformat(),
        }

        attempts.append(attempt)
        latest_attempt[request.url] = attempt

    @crawler.post_navigation_hook
    async def record_response(
        context: HttpCrawlingContext,
    ) -> None:
        """Capture HTTP status before further response processing."""

        attempt = latest_attempt.get(
            context.request.url
        )

        if attempt is not None:
            attempt["http_status"] = (
                context.http_response.status_code
            )
            attempt["outcome"] = "received"

    @crawler.router.default_handler
    async def handle(
        context: BeautifulSoupCrawlingContext,
    ) -> None:
        entry = lookup[context.request.url]
        attempt = latest_attempt.get(context.request.url)

        title = (
            context.soup.title.get_text(" ", strip=True)
            if context.soup.title
            else ""
        )

        page_text = context.soup.get_text(" ", strip=True)

        heuristic_challenge = is_challenge_page(
            title,
            page_text[:120],
        )

        tier, proxy_type = get_proxy_details(context, mode)

        quality = {
            "verdict": (
                "skipped" if not use_llm else "unavailable"
            ),
            "reason": (
                "LLM analysis disabled" if not use_llm else ""
            ),
        }

        llm_duration = 0.0

        if use_llm:
            expected = entry.get(
                "expected_content",
                "Main content relevant to the requested URL",
            )

            llm_text = prepare_page_text(
                context.soup,
                entry["id"],
            )

            context.log.info(
                "LLM input: %s; %d characters",
                entry["id"],
                len(llm_text),
            )

            async with llm_semaphore:
                llm_start = perf_counter()

                try:
                    quality = await asyncio.to_thread(
                        assess_page,
                        entry["url"],
                        title,
                        llm_text,
                        expected,
                    )
                except Exception as exc:
                    # Do not print exception messages.
                    # They could contain sensitive URLs.
                    quality = {
                        "verdict": "unavailable",
                        "reason": (
                            "LLM analysis failed: "
                            f"{type(exc).__name__}"
                        ),
                    }

                llm_duration = round(
                    perf_counter() - llm_start,
                    3,
                )

        # Update the current attempt with Ollama's assessment.
        if attempt is not None:
            attempt["quality"] = quality["verdict"]
            attempt["quality_reason"] = quality["reason"]

        # Optional escalation on detected anti-bot challenges.
        if retry_blocked and quality["verdict"] == "blocked":
            if attempt is not None:
                attempt["outcome"] = "rejected_blocked"
                attempt["error_type"] = "SessionError"

            context.log.warning(
                "Blocked: %s (HTTP %s; tier=%s). Rotating session.",
                entry["id"],
                context.http_response.status_code,
                tier,
            )

            raise SessionError(
                "Local model detected an anti-bot challenge"
            )

        row = {
            "id": entry["id"],
            "difficulty": entry["difficulty"],
            "url": entry["url"],
            "status": "fetched",
            "http_status": (
                context.http_response.status_code
            ),
            "title": title[:180],
            "bytes": len(
                await context.http_response.read()
            ),
            "proxy_tier": tier,
            "proxy_type": proxy_type,
            "heuristic_challenge": heuristic_challenge,
            "quality": quality["verdict"],
            "quality_reason": quality["reason"],
            "llm_duration_seconds": llm_duration,
            "time_utc": datetime.now(
                timezone.utc
            ).isoformat(),
        }

        await context.push_data(row)
        success_rows.append(row)

        if attempt is not None:
            attempt["outcome"] = "accepted"

        context.log.info(
            "Completed %s (HTTP %s; tier=%s; proxy=%s; quality=%s)",
            entry["id"],
            row["http_status"],
            tier,
            proxy_type,
            row["quality"],
        )

        if heuristic_challenge:
            context.log.warning(
                "Possible challenge page on %s",
                entry["id"],
            )

    @crawler.error_handler
    async def on_error(
        context: BasicCrawlingContext,
        error: Exception,
    ) -> None:
        """Record errors before Crawlee handles retries."""

        if isinstance(error, AttemptLimitReached):
            # No new network request was started.
            # Preserve the previous attempt.
            context.request.no_retry = True
            return

        attempt = latest_attempt.get(
            context.request.url
        )

        if attempt is not None:
            attempt["error_type"] = type(error).__name__

            if attempt["outcome"] in ("started", "received"):
                attempt["outcome"] = "error"

    @crawler.failed_request_handler
    async def on_failed(
        context: BasicCrawlingContext,
        error: Exception,
    ) -> None:
        """Record a request that has exhausted its retry budget."""

        entry = lookup.get(
            context.request.url,
            {
                "id": "unknown",
                "difficulty": "unknown",
            },
        )

        last_attempt = latest_attempt.get(
            context.request.url
        )

        if last_attempt is not None:
            if last_attempt["outcome"] in (
                "started",
                "received",
                "error",
            ):
                last_attempt["outcome"] = "failed"

            tier = last_attempt["proxy_tier"]
            proxy_type = last_attempt["proxy_type"]
        else:
            tier = None
            proxy_type = "unknown"

        failed_rows.append({
            "id": entry["id"],
            "difficulty": entry["difficulty"],
            "url": context.request.url,
            "status": "failed",
            "error_type": type(error).__name__,
            "proxy_tier": tier,
            "proxy_type": proxy_type,
            "quality": (
                last_attempt["quality"]
                if last_attempt
                else "not_evaluated"
            ),
        })

        context.log.warning(
            "Failed: %s (%s)",
            entry["id"],
            type(error).__name__,
        )

    # Create an output directory for this run.
    run_dir = (
        ROOT
        / "runs"
        / datetime.now(timezone.utc).strftime(
            "%Y%m%d-%H%M%S-%f"
        )
    )

    run_dir.mkdir(
        parents=True,
        exist_ok=False,
    )

    start = perf_counter()

    await crawler.run([
        entry["url"]
        for entry in sites
    ])

    elapsed = round(
        perf_counter() - start,
        3,
    )

    def write_json(filename: str, data) -> None:
        (run_dir / filename).write_text(
            json.dumps(
                data,
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    write_json(
        "results.json",
        success_rows + failed_rows,
    )

    write_json("attempts.json", attempts)

    summary = {
        "mode": mode,
        "difficulty": (
            difficulty if site_id is None else "single-site"
        ),
        "site": site_id,
        "llm_enabled": use_llm,
        "retry_blocked": retry_blocked,
        "max_attempts_per_site": max_attempts,
        "requested_urls": len(sites),
        "successful": len(success_rows),
        "useful": (
            sum(
                row["quality"] == "useful"
                for row in success_rows
            )
            if use_llm
            else None
        ),
        "failed": len(failed_rows),
        "not_recorded": max(
            0,
            len(sites) - len(success_rows) - len(failed_rows),
        ),
        "duration_seconds": elapsed,
        "navigation_attempts_observed": len(attempts),
        "attempts_by_tier": dict(
            Counter(
                str(attempt["proxy_tier"])
                for attempt in attempts
            )
        ),
        "attempt_outcomes": dict(
            Counter(
                attempt["outcome"]
                for attempt in attempts
            )
        ),
        "success_by_tier": dict(
            Counter(
                str(row["proxy_tier"])
                for row in success_rows
            )
        ),
        "quality_by_verdict": dict(
            Counter(
                row["quality"]
                for row in success_rows
            )
        ),
        "notes": (
            "A successful fetch is not necessarily a useful page. "
            "Ollama-triggered retries are allowed only for a targeted "
            "--site run. Attempts count network starts, not ScrapingBee "
            "credits. TLS verification is disabled in proxy modes. "
            "Total duration includes Ollama inference and retries."
        ),
    }

    write_json("summary.json", summary)

    print(f"Results: {run_dir}")
    print(json.dumps(
        summary,
        indent=2,
        ensure_ascii=False,
    ))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Crawlee tiered-proxy experiment"
    )

    parser.add_argument(
        "--mode",
        choices=("direct", "classic", "tiered"),
        default="direct",
    )

    parser.add_argument(
        "--difficulty",
        choices=("easy", "medium", "hard", "all"),
        default="easy",
    )

    parser.add_argument(
        "--site",
        help="Crawl one exact ID from sites.json (ignores --difficulty)",
    )

    parser.add_argument(
        "--include-disabled",
        action="store_true",
    )

    parser.add_argument(
        "--max-concurrency",
        type=int,
        default=2,
    )

    parser.add_argument(
        "--max-attempts",
        type=int,
        default=4,
        help="Limit network attempts per URL, including session rotations",
    )

    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="Disable local Ollama analysis",
    )

    parser.add_argument(
        "--retry-blocked",
        action="store_true",
        help="Rotate sessions on LLM-detected blocks (single site only)",
    )

    args = parser.parse_args()

    if not 1 <= args.max_concurrency <= 5:
        parser.error(
            "--max-concurrency must be between 1 and 5"
        )

    if not 1 <= args.max_attempts <= 8:
        parser.error(
            "--max-attempts must be between 1 and 8"
        )

    if args.retry_blocked and (
        args.no_llm
        or args.mode != "tiered"
        or not args.site
    ):
        parser.error(
            "--retry-blocked requires --mode tiered, "
            "--site ID, and Ollama"
        )

    asyncio.run(
        crawl(
            args.mode,
            args.difficulty,
            args.include_disabled,
            args.site,
            args.max_concurrency,
            args.max_attempts,
            use_llm=not args.no_llm,
            retry_blocked=args.retry_blocked,
        )
    )


if __name__ == "__main__":
    main()
