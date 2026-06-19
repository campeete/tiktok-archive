"""
Browser-based page fetcher using Playwright with a persistent profile.

For TikTok /photo/ URLs, an anonymous browser session gets the same
anti-bot shell page that plain requests does. A logged-in session is
required for TikTok to serve real post data. This module uses Playwright's
launch_persistent_context to maintain cookies/localStorage across runs.

Workflow:
1. User runs `tiktok-archive auth-tiktok` once. A non-headless Chromium
   opens the TikTok login page; user logs in normally; profile saves.
2. Subsequent fetches reuse the saved profile in headless mode.
3. If TikTok logs the session out (rare), photo fetches start failing
   again with a clear "session expired" message and the user re-runs
   auth-tiktok.

Plain requests still gets tried first by the photo.py caller. The browser
fallback only fires when the rehydration JSON is missing the post item.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

logger = logging.getLogger(__name__)


class BrowserFetchUnavailable(Exception):
    """Playwright is not installed or the browser is not available."""


class BrowserFetchError(Exception):
    """The fetch ran but didn't get usable content."""


# Connection params. Tuned conservatively: TikTok's JS shell takes a
# couple of seconds to populate the rehydration script, so we wait for
# the script tag to appear rather than for networkidle (which on TikTok
# is essentially never).
_NAVIGATION_TIMEOUT_MS = 30_000
_SCRIPT_WAIT_TIMEOUT_MS = 15_000

# A realistic user agent. Playwright's default is recognizable as
# automation; we override it with a current Chrome on macOS UA string.
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)


def _have_saved_profile() -> bool:
    """Has the user run `tiktok-archive auth-tiktok`?

    We check for a non-trivial profile dir. Playwright creates files in
    user_data_dir even on first launch, so checking existence isn't
    enough — we look for the Cookies file specifically, which only
    exists after a real session.
    """
    from media_archive.core import config
    profile_dir = Path(config.BROWSER_PROFILE_DIR)
    if not profile_dir.is_dir():
        return False
    # Chromium stores cookies at Default/Cookies (a SQLite file).
    cookies_db = profile_dir / "Default" / "Cookies"
    return cookies_db.is_file() and cookies_db.stat().st_size > 0


def fetch_with_browser(url: str, *, headless: bool = True) -> str:
    """Fetch a TikTok page via a real headless browser.

    Uses a persistent profile (cookies + localStorage) at
    config.BROWSER_PROFILE_DIR if one has been created via
    `tiktok-archive auth-tiktok`. Without auth, TikTok serves the
    anti-bot shell for /photo/ URLs and post data is missing.

    Returns the rendered HTML once the rehydration script tag has appeared.
    Raises BrowserFetchUnavailable if Playwright is not installed.
    Raises BrowserFetchError if the page loads but the script tag never
    materializes (e.g. captcha challenge, expired session).
    """
    from media_archive.core import config

    try:
        # Imported lazily so the rest of the package keeps working when
        # Playwright is not installed.
        from playwright.sync_api import (  # type: ignore[import-not-found]
            TimeoutError as PlaywrightTimeoutError,
            sync_playwright,
        )
    except ImportError as e:
        raise BrowserFetchUnavailable(
            "Playwright is not installed. Run: pip install playwright "
            "&& playwright install chromium"
        ) from e

    profile_dir = Path(config.BROWSER_PROFILE_DIR)
    has_auth = _have_saved_profile()
    if not has_auth:
        logger.warning(
            "No saved TikTok session at %s. Photo posts will likely fail "
            "with a captcha shell. Run: tiktok-archive auth-tiktok",
            profile_dir,
        )

    started = time.monotonic()
    try:
        with sync_playwright() as p:
            # Use launch_persistent_context for a stateful browser.
            # First-launch with no profile still works (acts like a fresh
            # context); the auth-tiktok command is what populates it.
            profile_dir.mkdir(parents=True, exist_ok=True)
            try:
                context = p.chromium.launch_persistent_context(
                    user_data_dir=str(profile_dir),
                    headless=headless,
                    user_agent=_USER_AGENT,
                    viewport={"width": 1280, "height": 800},
                    locale="en-US",
                )
            except Exception as e:
                raise BrowserFetchUnavailable(
                    f"Could not launch Chromium: {e}. "
                    "Try: playwright install chromium"
                ) from e

            try:
                page = context.pages[0] if context.pages else context.new_page()
                page.set_default_navigation_timeout(_NAVIGATION_TIMEOUT_MS)

                logger.debug("Navigating to %s (auth=%s)", url, has_auth)
                page.goto(url, wait_until="domcontentloaded")

                # Wait for the rehydration script tag to appear. This is
                # the actual signal that the post data is present —
                # not networkidle, not load, just "is the data there yet".
                try:
                    page.wait_for_selector(
                        'script#__UNIVERSAL_DATA_FOR_REHYDRATION__',
                        timeout=_SCRIPT_WAIT_TIMEOUT_MS,
                        state="attached",
                    )
                except PlaywrightTimeoutError as e:
                    html = page.content()
                    if '__UNIVERSAL_DATA_FOR_REHYDRATION__' not in html:
                        raise BrowserFetchError(
                            f"Rehydration script never appeared (likely captcha "
                            f"or expired session). Page title: {page.title()!r}. "
                            f"Try: tiktok-archive auth-tiktok"
                        ) from e
                    return html

                html = page.content()
                elapsed = time.monotonic() - started
                logger.info(
                    "Browser fetched %s in %.1fs (%d chars, auth=%s)",
                    url, elapsed, len(html), has_auth,
                )
                return html
            finally:
                context.close()
    except (BrowserFetchUnavailable, BrowserFetchError):
        raise
    except Exception as e:
        raise BrowserFetchError(f"Browser fetch failed: {e}") from e


def launch_auth_session(login_url: str = "https://www.tiktok.com/login") -> bool:
    """Launch a non-headless Chromium so the user can manually log into
    TikTok. The session persists in BROWSER_PROFILE_DIR so subsequent
    headless fetches reuse it.

    Returns True on a successful auth-and-save, False on failure or abort.
    Blocks waiting for the user to press Enter in the terminal once
    they've completed login. Does not validate the session beyond the
    user's word — the next real fetch will tell us if it actually worked.
    """
    from media_archive.core import config

    try:
        from playwright.sync_api import sync_playwright  # type: ignore[import-not-found]
    except ImportError:
        print(
            "Playwright is not installed. Run:\n"
            "  pip install -e \".[browser]\" && playwright install chromium"
        )
        return False

    profile_dir = Path(config.BROWSER_PROFILE_DIR)
    profile_dir.mkdir(parents=True, exist_ok=True)
    print(f"\nLaunching Chromium with profile dir: {profile_dir}")
    print(
        "\nA browser window will open. Log into TikTok normally — "
        "username/password, SMS code, whatever it asks for.\n"
        "When you can see your TikTok feed, come back to this terminal "
        "and press Enter to save the session.\n"
        "Press Ctrl+C to abort.\n"
    )

    try:
        with sync_playwright() as p:
            try:
                context = p.chromium.launch_persistent_context(
                    user_data_dir=str(profile_dir),
                    headless=False,
                    user_agent=_USER_AGENT,
                    viewport={"width": 1280, "height": 800},
                    locale="en-US",
                )
            except Exception as e:
                print(f"Could not launch Chromium: {e}")
                print("Try: playwright install chromium")
                return False

            try:
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(login_url, wait_until="domcontentloaded")

                try:
                    input("Press Enter once you're logged in (or Ctrl+C to abort)...")
                except (KeyboardInterrupt, EOFError):
                    print("\nAborted.")
                    return False
            finally:
                context.close()
    except Exception as e:
        print(f"Auth session failed: {e}")
        return False

    print(f"\nSession saved to {profile_dir}")
    print("Photo-post fetches will now use this saved session.")
    return True



def is_browser_available() -> bool:
    """Quick check: is Playwright importable AND chromium installed?

    Used by the `check` CLI command to give a green/yellow indicator.
    Does not actually launch the browser — just verifies the binary
    is present on disk.
    """
    try:
        from playwright.sync_api import sync_playwright  # type: ignore[import-not-found]
    except ImportError:
        return False
    try:
        with sync_playwright() as p:
            # executable_path() returns the path to the chromium binary
            # if installed, or raises if not. We don't actually launch.
            path = p.chromium.executable_path
            import os
            return bool(path) and os.path.exists(path)
    except Exception:
        return False
