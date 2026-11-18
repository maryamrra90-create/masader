"""Per-purpose AI budgets (NFR6, architecture E.4 and H.3).

Every model call attempt (first try, retry, fallback, JSON repair) reserves its worst-case cost before it
is sent, in the same transaction that writes its ai_calls row with outcome 'started'. The budgets table's
CHECK (reserved + spent <= cap) refuses an overspend even under concurrent reservations (measured in E1).
After the call, `settle` releases the reservation and books the charge.
"""
from __future__ import annotations

from .. import config
from ..settings import settings
from ..store.db import pool
from .errors import BudgetExhausted

CHARS_PER_TOKEN = 2.0     # conservative (Arabic text tokenises denser than English); over-reserving is safe


def estimate_tokens(text: str) -> int:
    """Upper-bound token estimate from characters. The architecture planned messages.count_tokens; a
    conservative estimate gives the same guarantee (reservation >= real cost) without an extra round trip."""
    return int(len(text) / CHARS_PER_TOKEN) + 16


def price(model: str) -> tuple[float, float]:
    base = model.removeprefix("sim:")              # simulated calls are priced as the model they stand in for
    pin, pout = config.routing()["prices_usd_per_mtok"][base]
    return float(pin), float(pout)


def call_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    pin, pout = price(model)
    return round((input_tokens * pin + output_tokens * pout) / 1e6, 5)


def worst_case(model: str, input_tokens: int, max_tokens: int) -> float:
    """Counted input with margin at the cache-write price, plus max_tokens (which bounds thinking and
    visible output together) at the output price."""
    r = config.routing()
    pin, pout = price(model)
    tin = input_tokens * r["input_margin"]
    return round((tin * pin * r["cache_write_factor"] + max_tokens * pout) / 1e6, 5)


def reserve(report_id: str | None, purpose: str, role: str, model: str, prompt_version: str, amount: float) -> int:
    with pool().connection() as c, c.transaction():
        ok = c.execute("UPDATE budgets SET reserved_usd = reserved_usd + %s WHERE purpose=%s AND period_start=%s "
                       "AND reserved_usd + spent_usd + %s <= cap_usd RETURNING 1",
                       (amount, purpose, settings.budget_period, amount)).fetchone()
        if not ok:
            raise BudgetExhausted(f"{role}: next {model} call may cost up to US${amount:.5f}, "
                                  f"more than is left of the {purpose} budget")
        row = c.execute("INSERT INTO ai_calls (report_id, purpose, role, model, prompt_version, reserved_usd, outcome) "
                        "VALUES (%s,%s,%s,%s,%s,%s,'started') RETURNING id",
                        (report_id, purpose, role, model, prompt_version, amount)).fetchone()
    return row["id"]


def settle(call_id: int, tin: int, tout: int, cost: float, ms: int | None, outcome: str) -> None:
    """Completed and invalid outputs are booked at their real cost (never above the reservation); a fast
    HTTP error costs nothing; a timed-out, cancelled or lost call keeps its whole reservation as spent,
    because the provider may still bill it (reconciled later against the provider's usage report)."""
    with pool().connection() as c, c.transaction():
        row = c.execute("UPDATE ai_calls SET input_tokens=%s, output_tokens=%s, cost_usd=%s, latency_ms=%s, outcome=%s "
                        "WHERE id=%s AND outcome='started' RETURNING purpose, reserved_usd",
                        (tin, tout, cost, ms, outcome, call_id)).fetchone()
        if row is None:                                   # already settled by the stage or the reaper
            return
        reserved = row["reserved_usd"]
        charge = reserved if outcome in ("timeout", "cancelled", "lost") else min(cost, reserved)
        if outcome in ("error", "refusal"):
            charge = 0 if outcome == "error" else min(cost, reserved)
        c.execute("UPDATE budgets SET reserved_usd = reserved_usd - %s, spent_usd = spent_usd + %s "
                  "WHERE purpose=%s AND period_start=%s", (reserved, charge, row["purpose"], settings.budget_period))


def remaining(purpose: str) -> float:
    with pool().connection() as c:
        row = c.execute("SELECT cap_usd - reserved_usd - spent_usd AS left FROM budgets WHERE purpose=%s AND period_start=%s",
                        (purpose, settings.budget_period)).fetchone()
    return float(row["left"]) if row else 0.0
