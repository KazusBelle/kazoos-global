"""
Matplotlib-based PNG renderer for setup alerts.

Draws confirmation-timeframe candles (H1 for Global, M15 for Local) with:
  - FVG boxes (bullish green / bearish red, semi-transparent)
  - OTE zone shading (0.618 .. 0.79 from the engine)
  - The trigger candle highlighted (last bar) and its body-vs-FVG line marked
"""

from __future__ import annotations

import io
import logging
from datetime import datetime, timezone
from typing import List, Optional

import matplotlib

matplotlib.use("Agg")  # no display, must be set before pyplot import.
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle

from kazus_logic.engine import Bar, ZoneResult


logger = logging.getLogger("kazus.worker.chart_image")


# 3-bar FVG scan + mitigation check, inlined here from the old compute.py.
# chart_image.py is the only remaining consumer and is slated for removal in
# the Playwright render-export migration; not worth re-exposing as shared API.
def _scan_fvgs(bars: List[Bar]):
    out = []
    for i in range(2, len(bars)):
        if bars[i].low > bars[i - 2].high:
            out.append((i, bars[i].low, bars[i - 2].high, "bullish"))
        if bars[i].high < bars[i - 2].low:
            out.append((i, bars[i - 2].low, bars[i].high, "bearish"))
    return out


def _is_fvg_mitigated(bars: List[Bar], fvg_idx: int, fvg_top: float, fvg_bottom: float) -> bool:
    for j in range(fvg_idx + 1, len(bars)):
        if bars[j].low <= fvg_top and bars[j].high >= fvg_bottom:
            return True
    return False


def render_setup_png(
    symbol: str,
    timeframe: str,                 # "D1" or "H1" (snapshot tf)
    confirmation_tf: str,           # "H1" or "M15" (bars tf)
    bars: List[Bar],
    result: ZoneResult,
    setup_kind: str,                # "STB" | "Inversion" | "Created"
    fvg_top: Optional[float],
    fvg_bottom: Optional[float],
    max_bars: int = 80,
) -> Optional[bytes]:
    """
    Render the last `max_bars` bars with FVG / OTE / trigger overlay.
    Returns PNG bytes, or None on failure.
    """
    if not bars or len(bars) < 3:
        return None

    try:
        plot_bars = bars[-max_bars:]
        offset = len(bars) - len(plot_bars)
        n = len(plot_bars)

        fig, ax = plt.subplots(figsize=(11, 6), dpi=120)
        fig.patch.set_facecolor("#0f1115")
        ax.set_facecolor("#15181f")

        bull = "#26a69a"
        bear = "#ef5350"
        wick = "#9aa0a6"

        # --- candles ---
        for i, b in enumerate(plot_bars):
            color = bull if b.close >= b.open else bear
            ax.vlines(i, b.low, b.high, color=wick, linewidth=0.7, zorder=2)
            body_low = min(b.open, b.close)
            body_high = max(b.open, b.close)
            height = max(body_high - body_low, (b.high - b.low) * 0.001 or 1e-9)
            ax.add_patch(
                Rectangle(
                    (i - 0.35, body_low),
                    0.7,
                    height,
                    facecolor=color,
                    edgecolor=color,
                    linewidth=0.5,
                    zorder=3,
                )
            )

        # --- FVG boxes (in the all-bars space, then translated by offset) ---
        full_fvgs = _scan_fvgs(bars)
        for (fvg_i, top, bottom, kind) in full_fvgs:
            local_i = fvg_i - offset
            if local_i < 0 or local_i >= n:
                continue
            mitigated = _is_fvg_mitigated(bars, fvg_i, top, bottom)
            face = "#26a69a" if kind == "bullish" else "#ef5350"
            alpha = 0.10 if mitigated else 0.22
            ax.add_patch(
                Rectangle(
                    (local_i - 0.5, bottom),
                    (n - 1) - (local_i - 0.5) + 0.5,
                    top - bottom,
                    facecolor=face,
                    edgecolor="none",
                    alpha=alpha,
                    zorder=1,
                )
            )

        # --- OTE zone ---
        if result.ote_low_price is not None and result.ote_high_price is not None:
            ax.axhspan(
                result.ote_low_price,
                result.ote_high_price,
                facecolor="#facc15",
                alpha=0.10,
                zorder=0,
            )
            ax.axhline(
                result.ote_low_price,
                color="#facc15",
                linewidth=0.6,
                linestyle="--",
                alpha=0.6,
                zorder=0,
            )
            ax.axhline(
                result.ote_high_price,
                color="#facc15",
                linewidth=0.6,
                linestyle="--",
                alpha=0.6,
                zorder=0,
            )

        # --- trigger FVG outline (highlight the alerted zone) ---
        if fvg_top is not None and fvg_bottom is not None:
            ax.axhline(
                fvg_top,
                color="#E3822D",
                linewidth=1.0,
                alpha=0.9,
                zorder=4,
            )
            ax.axhline(
                fvg_bottom,
                color="#E3822D",
                linewidth=1.0,
                alpha=0.9,
                zorder=4,
            )

        # --- trigger candle marker ---
        trigger_x = n - 1
        trigger = plot_bars[-1]
        ax.scatter(
            [trigger_x],
            [max(trigger.open, trigger.close)],
            marker="v" if trigger.close < trigger.open else "^",
            color="#E3822D",
            s=80,
            zorder=5,
        )

        # --- styling ---
        ax.set_xlim(-1, n)
        for spine in ax.spines.values():
            spine.set_color("#2a2d35")
        ax.tick_params(colors="#9aa0a6", labelsize=8)
        ax.grid(True, color="#1f2128", linewidth=0.5, zorder=0)

        title = (
            f"{symbol}  [{timeframe}]  setup={setup_kind}  "
            f"dir={result.direction}  ret="
            + (f"{result.retracement * 100:.1f}%" if result.retracement is not None else "—")
            + f"  ({confirmation_tf} bars)"
        )
        ax.set_title(
            title,
            color="#E3822D",
            fontsize=11,
            loc="left",
            pad=10,
        )

        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        ax.text(
            1.0,
            1.01,
            timestamp,
            transform=ax.transAxes,
            ha="right",
            va="bottom",
            color="#6b7280",
            fontsize=8,
        )

        buf = io.BytesIO()
        fig.savefig(buf, format="png", facecolor=fig.get_facecolor())
        plt.close(fig)
        return buf.getvalue()

    except Exception as exc:
        logger.warning("render_setup_png failed for %s/%s: %s", symbol, timeframe, exc)
        try:
            plt.close("all")
        except Exception:
            pass
        return None


def render_htf_png(
    symbol: str,
    timeframe: str,                 # "D1" or "H1" (HTF where the setup was found)
    bars: List[Bar],
    result: ZoneResult,
    setup_kind: str,
    fvg_top: Optional[float],
    fvg_bottom: Optional[float],
    max_bars: int = 120,
) -> Optional[bytes]:
    """
    HTF-context PNG: candles, active fib (1.0 / 0.0 anchors), OTE zone,
    trigger-FVG outline highlighted with a dashed box, marker on the last
    closed bar. Trimmed chrome — only the elements that define the setup,
    no full-FVG zoo.
    """
    if not bars or len(bars) < 3:
        return None

    try:
        plot_bars = bars[-max_bars:]
        n = len(plot_bars)

        fig, ax = plt.subplots(figsize=(11, 6), dpi=120)
        fig.patch.set_facecolor("#0f1115")
        ax.set_facecolor("#15181f")

        bull = "#26a69a"
        bear = "#ef5350"
        wick = "#9aa0a6"

        for i, b in enumerate(plot_bars):
            color = bull if b.close >= b.open else bear
            ax.vlines(i, b.low, b.high, color=wick, linewidth=0.7, zorder=2)
            body_low = min(b.open, b.close)
            body_high = max(b.open, b.close)
            height = max(body_high - body_low, (b.high - b.low) * 0.001 or 1e-9)
            ax.add_patch(
                Rectangle(
                    (i - 0.35, body_low),
                    0.7,
                    height,
                    facecolor=color,
                    edgecolor=color,
                    linewidth=0.5,
                    zorder=3,
                )
            )

        # --- active fib anchors (1.0 = swing, 0.0 = fib) -------------------
        if result.fib_high is not None:
            ax.axhline(
                result.fib_high,
                color="#9aa0a6",
                linewidth=0.8,
                linestyle=":",
                alpha=0.7,
                zorder=1,
            )
        if result.fib_low is not None:
            ax.axhline(
                result.fib_low,
                color="#9aa0a6",
                linewidth=0.8,
                linestyle=":",
                alpha=0.7,
                zorder=1,
            )

        # --- OTE zone ------------------------------------------------------
        if result.ote_low_price is not None and result.ote_high_price is not None:
            ax.axhspan(
                result.ote_low_price,
                result.ote_high_price,
                facecolor="#facc15",
                alpha=0.14,
                zorder=0,
            )

        # --- trigger FVG outline (highlight the alerted zone) -------------
        if fvg_top is not None and fvg_bottom is not None:
            ax.add_patch(
                Rectangle(
                    (-0.5, fvg_bottom),
                    n,
                    fvg_top - fvg_bottom,
                    facecolor="#E3822D",
                    edgecolor="#E3822D",
                    alpha=0.18,
                    linewidth=1.4,
                    linestyle="--",
                    zorder=4,
                )
            )

        # --- last-bar marker ----------------------------------------------
        last_x = n - 1
        last = plot_bars[-1]
        ax.scatter(
            [last_x],
            [max(last.open, last.close)],
            marker="v" if last.close < last.open else "^",
            color="#E3822D",
            s=80,
            zorder=5,
        )

        ax.set_xlim(-1, n)
        for spine in ax.spines.values():
            spine.set_color("#2a2d35")
        ax.tick_params(colors="#9aa0a6", labelsize=8)
        ax.grid(True, color="#1f2128", linewidth=0.5, zorder=0)

        title = (
            f"{symbol}  [{timeframe}]  HTF context  "
            f"setup={setup_kind}  dir={result.direction}"
        )
        ax.set_title(
            title,
            color="#E3822D",
            fontsize=11,
            loc="left",
            pad=10,
        )

        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        ax.text(
            1.0,
            1.01,
            timestamp,
            transform=ax.transAxes,
            ha="right",
            va="bottom",
            color="#6b7280",
            fontsize=8,
        )

        buf = io.BytesIO()
        fig.savefig(buf, format="png", facecolor=fig.get_facecolor())
        plt.close(fig)
        return buf.getvalue()

    except Exception as exc:
        logger.warning("render_htf_png failed for %s/%s: %s", symbol, timeframe, exc)
        try:
            plt.close("all")
        except Exception:
            pass
        return None
