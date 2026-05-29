# Burst Detection — measurement contract (companion)

**Companion to:** [`docs/2026-05-23-architecture-freeze.md`](2026-05-23-architecture-freeze.md) (§4 Operational Narrative, Execution Validation measurement layer), [`docs/lip-execution-validation.md`](lip-execution-validation.md), [`docs/lip-metric-registry.md`](lip-metric-registry.md) §A.5, [`docs/lip-governance.md`](lip-governance.md), [`docs/lip-semantic-vocabulary-boundaries.md`](lip-semantic-vocabulary-boundaries.md).

**Status: IMPLEMENTED (2026-05-29, PHASE 3A).** `detect_bursts()` in [`shared/kazus_logic/liquidity/realtime/burst.py`](../shared/kazus_logic/liquidity/realtime/burst.py), emitted per tick from `engine._sample_all`, persisted append-only to the `liquidity_bursts` table ([`LiquidityBurst`](../shared/kazus_db/models.py)).

---

## 1. Architectural decision

Burst Detection is **completion of an already-declared, already-implemented primitive**, not a new capability. The burst — *a temporally clustered run of same-side taker prints* — has been the unit of the Execution Validation layer since it shipped (`exec_impact.detect_and_measure_bursts`). What PHASE 3A adds is the **standalone burst record**: every settled burst is now emitted as a first-class measurement (`burst_start_ts · burst_end_ts · burst_duration_ms · burst_trade_count · burst_notional · burst_side`), independent of the impact computation (which only emitted bursts above a notional floor with usable book snapshots).

**Single source of truth.** The burst-grouping rule lives once, in `burst.iter_settled_bursts()`, and is consumed by **both** the new burst-record emitter **and** `exec_impact`. The two cannot drift; this is what makes acceptance criterion #10 (PHASE 3B compatibility) true by construction — 3B measures exactly the bursts 3A records.

**Sensor reality.** The spec named `aggTrade`, but `@aggTrade` is silently unavailable on Binance Futures from this network perimeter (commit `5c4acbc`); production runs on the non-aggregated **`<s>@trade`** stream. The contract is therefore stated against the `@trade` sensor — see §6.

## 2. Semantic contract (load-bearing)

Burst Detection measures: **temporally clustered same-side taker activity as observed by the Binance `@trade` sensor.**

It does **not** measure, infer, or assert: market intent, hidden liquidity, informed trading, manipulation, future behaviour, or flow toxicity. A burst is a *mesoscopic observation of temporal clustering* — nothing more. `burst_side` is the observed taker side of the matched prints; it is **not** an intent, opinion, or forecast.

**Forbidden vocabulary** (per [semantic-vocabulary-boundaries](lip-semantic-vocabulary-boundaries.md)): aggression · informed flow · smart money · manipulation · spoofing · hidden intent · alpha · signal · prediction · expected move · future direction. These appear in code/docs only inside explicit "this is NOT…" negations.

**Ontology invariant** (per [ontology-boundaries](lip-ontology-boundaries.md)): emits bounded observational classifications under current instrumentation constraints; does not establish authoritative market ontology.

## 3. Formula specification (as implemented)

A burst is built from an ascending-ts sequence of `@trade` prints (`iter_settled_bursts`):

- **Open** with the first print.
- **Extend** while the next print is the **same side** AND arrives within `BURST_GAP_MS = 250` ms of the previous print (sliding window — each new print re-extends).
- **Close** on an opposite-side print OR a gap `> 250` ms.
- **Settle** only once `SETTLE_MS = 500` ms have elapsed since the last print, with a tail-of-tape grace (`BURST_GAP_MS + SETTLE_MS = 750` ms) so a burst at the edge of the visible tape is held one more cycle rather than closed prematurely.

Worked example (spec): prints at `t = 0, 120, 240, 460` ms → **one** burst, because every consecutive gap stays ≤ 250 ms.

Outputs per settled burst:

| Output | Definition |
|---|---|
| `burst_start_ts` | exchange ts of the first print |
| `burst_end_ts` | exchange ts of the last print |
| `burst_duration_ms` | `burst_end_ts − burst_start_ts` |
| `burst_trade_count` | number of `@trade` prints in the burst (sensor events — see §6) |
| `burst_notional` | Σ `qty × price` over the prints |
| `burst_side` | observed taker side: `buy` \| `sell` |

## 4. Refusal-first states

When data is insufficient to form a trustworthy burst, the detector emits an **explicit refusal marker** (burst_* fields NULL) rather than fabricating a burst. One marker is written on **transition into** a non-OK status (not every cycle — bounded, append-only-friendly); resumption to OK is implicit when real burst rows resume.

| State | Cause | Detection |
|---|---|---|
| `UNKNOWN` | startup warmup / partial tape visibility / nothing observed | no trades yet, or `now − tape_started_ts < BURST_WARMUP_MS = 2000` (earlier prints may be unseen) |
| `INSUFFICIENT` | insufficient observations | warmed up, no discontinuity, but the current tape window holds no prints |
| `DROPPED` | WS reconnect / tape gap | engine sets `tape_gap_ts` on `conn_id` bump; the detector refuses any burst spanning the gap and steps its cursor past it |

**Scope note (no hidden reconstruction, acceptance #6):** `DROPPED` covers only *detectable* discontinuities (WS reconnect / tape gap). Per-trade "missing sequence" detection is **not** claimed — the `Trade` struct does not store per-print sequence IDs, and none were added.

## 5. Replay & append-only contract

- **Replay-deterministic:** burst boundaries, `burst_trade_count`, `burst_notional`, and all timestamps are pure deterministic functions of the `@trade` sequence (`iter_settled_bursts` has no wall clock, no randomness, no interpolation). Reprocessing the same prints yields identical bursts.
- **Forward-only / append-only:** `burst_cursor_ts` advances monotonically; prints are never re-evaluated; rows are only appended to `liquidity_bursts`, never mutated. Independent of `exec_cursor_ts` (separate cursor, same shared boundaries).
- **Persistence authority:** like the rest of the realtime tier, the trade tape is in-memory only; the persisted `liquidity_bursts` row is authoritative for replay. Bursts before subscription / across a gap are not reconstructed.
- **Timestamp discipline:** existing dual-domain model only — `burst_*` use the exchange-timestamp domain; `local_recv_ts` is the existing local-receive domain (detection time). No new time domain added.

## 6. `burst_trade_count` honesty note

`burst_trade_count` counts `@trade` **sensor events**, not underlying executions and not taker orders. On the non-aggregated `@trade` stream each message is a single match print, but one taker order can still print across several price levels (→ multiple prints), so the count is **not** a count of taker orders. It is sensitive to how the venue emits prints. The platform does not attempt to reconstruct the number of underlying executions ("market as seen by a specific sensor").

## 7. Governance classification (deliverable)

Per [lip-governance §2](lip-governance.md): **Class B + Class E**. B — new measurement computation (the standalone burst record); E — new persisted table `liquidity_bursts`. Permitted during the Operational Observation Period under the **Class E carve-out for additive, append-only fields with no behaviour dependence**, and explicitly operator-authorized (PHASE 3A):

- changes **no** existing emit's value/distribution/timing — the `exec_impact` refactor is behaviour-preserving (its 14 tests are unchanged and green; it now imports the shared grouping instead of an inline copy);
- nothing downstream consumes burst records yet (3B will read the same in-memory boundaries via the shared iterator, not the table);
- additive schema via `create_all` (no migration tool), append-only, no row mutation.

Audit entry: [lip-governance §14](lip-governance.md) `2026-05-29-02`. **What it is NOT:** not an execution/signal/alpha engine, not a manipulation/intent classifier. Increases measurability/falsifiability without increasing inference or autonomy (§11 invariant holds).

## 8. Open items (uncalibrated)

- `BURST_GAP_MS = 250`, `SETTLE_MS = 500`, `BURST_WARMUP_MS = 2000` are interim, uncalibrated (Class C). They are inherited/shared with the Execution Validation layer where applicable.
- Burst records are **persisted but not surfaced in any UI** (frozen DISC surface). Exposing them is out of scope (no Phase 3C authorized).
