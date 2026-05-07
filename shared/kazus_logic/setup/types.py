from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional

SetupKind = Literal["NO", "INV", "CRE", "STB"]
FvgKind = Literal["bullish", "bearish"]


@dataclass(frozen=True)
class Fvg:
    """A 3-bar fair-value-gap formed inside an OTE session.

    `formed_at_idx` indexes into the session-local bar array (0 = entry bar).
    `top`/`bottom` are the gap edges with top >= bottom regardless of kind.
    """
    formed_at_idx: int
    formed_at_ts: int
    top: float
    bottom: float
    kind: FvgKind


@dataclass(frozen=True)
class SetupEvent:
    """A single fired event the worker should turn into a Telegram alert.

    `event_id` is stable across worker cycles so AlertState.sent_event_ids
    can dedupe — the same event observed twice produces the same id.
    """
    kind: Literal["INV", "CRE", "STB"]
    event_id: str
    trigger_ts: int
    fvg: Fvg
    swing_low: float


@dataclass
class SetupState:
    """Persistent state for one (symbol, htf, ltf) tuple. Worker stores
    one of these per row, feeds it into detect_setup() each cycle, and
    persists the returned state for the next cycle.

    Reset rules:
      - new session_id (HTF OTE changed or price re-entered OTE) → wipe.
      - last LTF bar low < swing_low → wipe but keep session_id.
    """
    state: SetupKind = "NO"
    session_id: str = ""
    # Inclusive lower bound on FVG-formation ts for the current search arc.
    # Equals the OTE entry ts initially; advances to the breaking-bar ts
    # when a swing_low break triggers a reset within the same session.
    search_start_ts: int = 0
    swing_low: Optional[float] = None
    swing_low_ts: Optional[int] = None
    first_bear_fvg: Optional[Fvg] = None
    first_bull_fvg: Optional[Fvg] = None
    inv_fired: bool = False
    cre_fired: bool = False
    stb_fired: bool = False
    inv_at_ts: Optional[int] = None
    cre_at_ts: Optional[int] = None
