"""Setup-alert sender.

Renders an HTF (context) chart + an LTF chart with the setup overlay via
the headless Playwright runner, then posts both as a Telegram media-group
with a minimal caption. Replaces the matplotlib-based path that lived in
chart_image.py.

Caption is deliberately terse — symbol, timeframe, setup kind, price —
because the two attached PNGs carry the FVG / swing-low / OTE detail.
"""

from __future__ import annotations

import logging
from typing import Optional, Tuple

from kazus_logic.compute import (
    ALERT_TF_D1,
    ALERT_TF_H1,
    ALERT_TF_H1_M5,
    SymbolSnapshot,
)
from kazus_logic.setup import SetupEvent

from .chart_renderer import ChartRenderer, OverlayFvg, SetupOverlay
from .telegram import (
    send_telegram,
    send_telegram_media_group,
    send_telegram_photo,
)

logger = logging.getLogger("kazus.worker.telegram_alerts")


# (HTF chart interval, LTF chart interval) per alert timeframe.
# HTF carries the OTE/fib context, LTF carries the FVG + swing-low overlay.
_TF_CHART_PAIR: dict[str, Tuple[str, str]] = {
    ALERT_TF_D1: ("1d", "1h"),
    ALERT_TF_H1: ("1h", "15m"),
    ALERT_TF_H1_M5: ("1h", "5m"),
}


def _format_price(price: Optional[float]) -> str:
    if price is None:
        return "—"
    return f"{price:g}"


def _build_caption(symbol: str, timeframe: str, event: SetupEvent, price: Optional[float]) -> str:
    return (
        f"🎯 <b>{event.kind}</b> #{symbol} [{timeframe}]\n"
        f"Price: {_format_price(price)}"
    )


async def send_setup_alert(
    settings,
    renderer: ChartRenderer,
    snap: SymbolSnapshot,
    timeframe: str,
    event: SetupEvent,
) -> Tuple[bool, str]:
    """Render HTF + LTF charts and post as a media-group.

    Returns ``(sent, caption)``. ``sent`` is True only when Telegram
    acknowledged the message. ``caption`` is the message text persisted to
    the AlertEvent feed regardless of which delivery path took it.

    On any chart-render failure the function still tries to send whatever
    images succeeded, falling back to a text-only message if both renders
    fail. Telegram-side failures are non-fatal — the caller decides whether
    to retry on the next cycle (see runner._collect_new_events).
    """
    htf_tf, ltf_tf = _TF_CHART_PAIR[timeframe]
    symbol = snap.symbol
    caption = _build_caption(symbol, timeframe, event, snap.price)

    # Pull swing_low_ts off the persisted state — SetupEvent only carries
    # the price. Missing ts is fine: CandleChart spans the dashed line
    # across the full plot when ts isn't provided.
    state = snap.setup_states.get(timeframe)
    swing_low_ts = state.swing_low_ts if state is not None else None

    # FVG visually extends from formation to the most-recent LTF bar
    # (matches backend/app/api/chart.py's FvgBox convention).
    ltf_bars = snap.confirmation_bars.get(timeframe, [])
    fvg_end_ts = ltf_bars[-1].ts if ltf_bars else event.fvg.formed_at_ts

    # Only the FVGs that actually compose the setup are drawn. STB is the
    # union of an inversion (bear FVG) and a creation (bull FVG), so it
    # carries both; INV/CRE carry the single FVG on the event. The frontend
    # colours each by `kind`, so no per-state colour is needed here.
    def _overlay_fvg(fvg) -> OverlayFvg:
        return OverlayFvg(
            top=fvg.top,
            bottom=fvg.bottom,
            ts=fvg.formed_at_ts,
            end_ts=fvg_end_ts,
            kind=fvg.kind,
        )

    setup_fvgs: list[OverlayFvg] = []
    if event.kind == "STB" and state is not None:
        for f in (state.first_bear_fvg, state.first_bull_fvg):
            if f is not None:
                setup_fvgs.append(_overlay_fvg(f))
    if not setup_fvgs:
        setup_fvgs.append(_overlay_fvg(event.fvg))

    overlay = SetupOverlay(
        state=event.kind,
        fvgs=tuple(setup_fvgs),
        swing_low_ts=swing_low_ts,
        swing_low_price=event.swing_low,
    )

    htf_png: Optional[bytes] = None
    ltf_png: Optional[bytes] = None
    try:
        htf_png = await renderer.render(
            symbol, htf_tf, theme="light", fvg_nearest_pair=True
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("htf chart render failed (%s %s): %s", symbol, htf_tf, exc)
    try:
        ltf_png = await renderer.render(
            symbol, ltf_tf, overlay=overlay, theme="light",
            zoom=getattr(settings, "chart_render_zoom", 1.0),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("ltf chart render failed (%s %s): %s", symbol, ltf_tf, exc)

    photos = [p for p in (htf_png, ltf_png) if p is not None]
    sent = False
    if len(photos) >= 2:
        sent = await send_telegram_media_group(
            settings.telegram_bot_token, settings.telegram_chat_id, photos, caption
        )
    elif len(photos) == 1:
        sent = await send_telegram_photo(
            settings.telegram_bot_token, settings.telegram_chat_id, photos[0], caption
        )
    if not sent:
        sent = await send_telegram(
            settings.telegram_bot_token, settings.telegram_chat_id, caption
        )
    return sent, caption


__all__ = ["send_setup_alert"]
