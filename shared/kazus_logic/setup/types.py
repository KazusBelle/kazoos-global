from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, Literal, Optional

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

    def to_dict(self) -> Dict[str, Any]:
        return {
            "formed_at_idx": self.formed_at_idx,
            "formed_at_ts": self.formed_at_ts,
            "top": self.top,
            "bottom": self.bottom,
            "kind": self.kind,
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "Fvg":
        return Fvg(
            formed_at_idx=int(d["formed_at_idx"]),
            formed_at_ts=int(d["formed_at_ts"]),
            top=float(d["top"]),
            bottom=float(d["bottom"]),
            kind=d["kind"],
        )


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

    def to_json(self) -> str:
        return json.dumps({
            "state": self.state,
            "session_id": self.session_id,
            "search_start_ts": self.search_start_ts,
            "swing_low": self.swing_low,
            "swing_low_ts": self.swing_low_ts,
            "first_bear_fvg": self.first_bear_fvg.to_dict() if self.first_bear_fvg else None,
            "first_bull_fvg": self.first_bull_fvg.to_dict() if self.first_bull_fvg else None,
            "inv_fired": self.inv_fired,
            "cre_fired": self.cre_fired,
            "stb_fired": self.stb_fired,
            "inv_at_ts": self.inv_at_ts,
            "cre_at_ts": self.cre_at_ts,
        })

    @staticmethod
    def from_json(raw: Optional[str]) -> Optional["SetupState"]:
        if not raw:
            return None
        try:
            d = json.loads(raw)
        except (TypeError, ValueError):
            return None
        if not isinstance(d, dict):
            return None
        return SetupState(
            state=d.get("state", "NO"),
            session_id=d.get("session_id", ""),
            search_start_ts=int(d.get("search_start_ts", 0)),
            swing_low=d.get("swing_low"),
            swing_low_ts=d.get("swing_low_ts"),
            first_bear_fvg=Fvg.from_dict(d["first_bear_fvg"]) if d.get("first_bear_fvg") else None,
            first_bull_fvg=Fvg.from_dict(d["first_bull_fvg"]) if d.get("first_bull_fvg") else None,
            inv_fired=bool(d.get("inv_fired", False)),
            cre_fired=bool(d.get("cre_fired", False)),
            stb_fired=bool(d.get("stb_fired", False)),
            inv_at_ts=d.get("inv_at_ts"),
            cre_at_ts=d.get("cre_at_ts"),
        )
