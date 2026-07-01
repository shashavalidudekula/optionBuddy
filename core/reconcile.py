"""reconcile.py — read-only check that internal positions match the broker's book.

In LIVE mode the bot places real orders, but its notion of "what I hold" lives only
in the paper_positions table. If a live order rejected (margin, freeze qty, circuit)
or partially filled, the two drift apart silently — and the bot would later try to
square off something that isn't there, or leave a real position unmanaged. This
module compares the two and surfaces any divergence so a human can step in.

It is strictly READ-ONLY: it never places, modifies or cancels an order. The actual
Dhan position fetch (`Broker.fetch_positions`) is documented-but-unverified — confirm
the field shape with a live read before trusting the numbers (see GO_LIVE_CRITERIA.md).

`reconcile_positions` and `net_by_code` are pure (resolver injected) and unit-tested;
`run_reconciliation` wires them to the live DB + broker.
"""
from config.logger import get_logger

log = get_logger("reconcile")


def net_by_code(positions: list[dict], resolver) -> dict[str, int]:
    """Collapse internal open positions into net signed qty per scrip code.

    `resolver(position) -> "SEG:securityId" | None` maps a position to the same
    scrip code the broker reports under. Unresolvable positions are skipped (and
    logged) rather than silently dropped — an unresolved leg is itself a risk.
    """
    out: dict[str, int] = {}
    for p in positions:
        code = None
        try:
            code = resolver(p)
        except Exception as e:  # noqa: BLE001
            log.debug("Reconcile resolve failed (%s): %s", p.get("instrument"), e)
        if not code:
            log.warning("Reconcile: could not resolve scrip for %s — excluded", p.get("instrument"))
            continue
        qty = int(p.get("remaining_qty")
                  or (int(p.get("lots") or 0) * int(p.get("lot_size") or 0))
                  or 0)
        if qty <= 0:
            continue
        signed = qty if str(p.get("action", "")).upper() == "BUY" else -qty
        out[code] = out.get(code, 0) + signed
    return out


def reconcile_positions(internal: dict[str, int], broker: dict[str, int],
                        qty_tol: int = 0) -> list[dict]:
    """Compare two {code: net_qty} maps. Return one divergence dict per code that
    differs by more than `qty_tol` (covers extras on either side).

    Each divergence: {code, internal, broker, diff} where diff = broker − internal.
    Empty list == fully reconciled.
    """
    divergences: list[dict] = []
    for code in sorted(set(internal) | set(broker)):
        i = int(internal.get(code, 0))
        b = int(broker.get(code, 0))
        if abs(i - b) > qty_tol:
            divergences.append({"code": code, "internal": i, "broker": b, "diff": b - i})
    return divergences


def run_reconciliation(broker, qty_tol: int = 0) -> list[dict] | None:
    """Reconcile live internal positions against the broker's book.

    Returns: None when reconciliation can't run this cycle (paper broker / fetch
    failed — so the caller skips, never false-alarms); otherwise the (possibly
    empty) list of divergences.
    """
    broker_pos = broker.fetch_positions()
    if broker_pos is None:
        return None  # unsupported or transient fetch failure — skip silently

    from data.advisory_store import get_open_paper_positions
    from core.market_data_provider import resolve_scrip_for_call

    internal = net_by_code(get_open_paper_positions(), resolve_scrip_for_call)
    return reconcile_positions(internal, broker_pos, qty_tol=qty_tol)
