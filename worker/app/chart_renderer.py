"""Headless chart renderer: drives Playwright + chromium to take a PNG of
the same CandleChart shown in the UI, plus an optional SetupOverlay.

Used by the Stage-5 setup-alert pipeline. Stage 4 introduces this module
and the underlying browser plumbing; the actual swap of chart_image.py
over to this renderer happens in Stage 5 (see workplan).

The headless URL is /chart-export?... served from the frontend container.
Auth: the worker mints a short-lived HS256 JWT for the configured render
username (same JWT_SECRET as the backend), then seeds localStorage with
it before navigation so the SPA's API client picks it up.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlencode

from jose import jwt
from playwright.async_api import (
    Browser,
    BrowserContext,
    Playwright,
    async_playwright,
)

from .settings import WorkerSettings

logger = logging.getLogger("kazus.worker.chart_renderer")


@dataclass(frozen=True)
class SetupOverlay:
    state: str               # "INV" | "CRE" | "STB"
    fvg_top: float
    fvg_bottom: float
    fvg_ts: int              # ms
    fvg_end_ts: int          # ms
    swing_low_ts: Optional[int] = None     # ms
    swing_low_price: Optional[float] = None


class ChartRenderer:
    """Lazy-init singleton wrapping a headless chromium instance.

    The browser is launched on first `render()` and kept alive across
    calls. Subsequent renders reuse a single browser via fresh contexts
    so localStorage state doesn't leak between symbols.
    """

    def __init__(self, settings: WorkerSettings) -> None:
        self._settings = settings
        self._lock = asyncio.Lock()
        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None

    async def _ensure_browser(self) -> Browser:
        if self._browser is not None and self._browser.is_connected():
            return self._browser
        async with self._lock:
            if self._browser is not None and self._browser.is_connected():
                return self._browser
            if self._playwright is None:
                self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                ],
            )
            logger.info("chart_renderer: chromium launched")
            return self._browser

    def _mint_token(self) -> str:
        s = self._settings
        if not s.jwt_secret:
            raise RuntimeError("jwt_secret not configured for chart renderer")
        now = datetime.now(timezone.utc)
        payload = {
            "sub": s.chart_render_username,
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(minutes=s.chart_render_token_ttl_min)).timestamp()),
        }
        return jwt.encode(payload, s.jwt_secret, algorithm=s.jwt_algorithm)

    def _build_url(
        self,
        symbol: str,
        tf: str,
        *,
        theme: str,
        width: int,
        height: int,
        overlay: Optional[SetupOverlay],
        fvg_limit: int,
    ) -> str:
        params: dict[str, str] = {
            "symbol": symbol,
            "tf": tf,
            "theme": theme,
            "w": str(width),
            "h": str(height),
            "fvg_limit": str(fvg_limit),
        }
        if overlay is not None:
            params.update(
                {
                    "state": overlay.state,
                    "fvg_top": repr(overlay.fvg_top),
                    "fvg_bottom": repr(overlay.fvg_bottom),
                    "fvg_ts": str(overlay.fvg_ts),
                    "fvg_end_ts": str(overlay.fvg_end_ts),
                }
            )
            if overlay.swing_low_ts is not None and overlay.swing_low_price is not None:
                params["swing_low_ts"] = str(overlay.swing_low_ts)
                params["swing_low_price"] = repr(overlay.swing_low_price)
        base = self._settings.chart_render_base_url.rstrip("/")
        return f"{base}/chart-export?{urlencode(params)}"

    async def render(
        self,
        symbol: str,
        tf: str,
        *,
        theme: str = "dark",
        width: int = 1200,
        height: int = 675,
        overlay: Optional[SetupOverlay] = None,
        fvg_limit: int = 6,
        timeout_ms: int = 20_000,
    ) -> bytes:
        """Render a single chart as a PNG. Returns the image bytes.

        Raises RuntimeError if the chart fails to become ready within
        ``timeout_ms``. Callers should fall back to a text-only alert or
        the legacy chart_image.py renderer on failure.
        """
        token = self._mint_token()
        target_url = self._build_url(
            symbol=symbol,
            tf=tf,
            theme=theme,
            width=width,
            height=height,
            overlay=overlay,
            fvg_limit=fvg_limit,
        )
        base = self._settings.chart_render_base_url.rstrip("/")
        browser = await self._ensure_browser()
        context: Optional[BrowserContext] = None
        try:
            context = await browser.new_context(
                viewport={"width": width, "height": height},
                device_scale_factor=2,
            )
            page = await context.new_page()
            # Need a same-origin navigation to set localStorage. The root URL
            # is fast (cached SPA shell); we don't wait for the dashboard to
            # finish loading — only for the page itself.
            await page.goto(f"{base}/", wait_until="domcontentloaded", timeout=timeout_ms)
            await page.evaluate(
                "(t) => { try { localStorage.setItem('kazus_token', t); } catch (e) {} }",
                token,
            )
            await page.goto(target_url, wait_until="load", timeout=timeout_ms)
            await page.wait_for_function(
                "window.__chartReady === true", timeout=timeout_ms
            )
            # Screenshot the export-root element so we get exactly the
            # configured w/h regardless of how the SPA shell laid out.
            element = await page.query_selector("#chart-export-root")
            if element is None:
                png = await page.screenshot(type="png", full_page=False)
            else:
                png = await element.screenshot(type="png")
            return png
        except Exception as exc:
            logger.warning(
                "chart_renderer: failed symbol=%s tf=%s url=%s err=%s",
                symbol, tf, target_url, exc,
            )
            # Best-effort: capture the page text to help debug auth/route issues.
            if context is not None:
                try:
                    pages = context.pages
                    if pages:
                        err_attr = await pages[0].evaluate(
                            "() => window.__chartError || null"
                        )
                        if err_attr:
                            logger.warning("chart_renderer: page __chartError=%s", err_attr)
                except Exception:  # noqa: BLE001
                    pass
            raise
        finally:
            if context is not None:
                try:
                    await context.close()
                except Exception:  # noqa: BLE001
                    pass

    async def close(self) -> None:
        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception:  # noqa: BLE001
                pass
            self._browser = None
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:  # noqa: BLE001
                pass
            self._playwright = None


__all__ = ["ChartRenderer", "SetupOverlay"]
