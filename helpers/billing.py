"""
Gemini usage -> cost calculation, and the write path from g.gemini_usage
(populated by helpers/gemini_client.call_gemini(), see there) into
GeminiUsageLog rows. Called once per job from helpers/jobs.log_job() — the
one chokepoint every client task's /process route already runs through, so
no client task.py needs to know billing exists at all.

Pricing/exchange-rate lookups always resolve to whichever row's
effective_from is the latest one <= the call's own timestamp (see
get_active_pricing()/get_active_exchange_rate()) — NOT simply "the most
recently created row" — so a price/rate entered today with a PAST
effective_from can correct historical cost, while one with a FUTURE
effective_from doesn't apply retroactively. Cost is computed and stored on
GeminiUsageLog at write time; it is never recomputed later from current
prices, so past bills stay stable even as pricing/rates change going
forward.
"""

import datetime
from decimal import Decimal

from sqlalchemy import func

from database.models import ExchangeRate, GeminiUsageLog, ModelPricing


def get_active_pricing(session, model_name: str, at: datetime.datetime) -> ModelPricing | None:
    return (
        session.query(ModelPricing)
        .filter(ModelPricing.model_name == model_name, ModelPricing.effective_from <= at)
        .order_by(ModelPricing.effective_from.desc())
        .first()
    )


def get_active_exchange_rate(session, at: datetime.datetime) -> ExchangeRate | None:
    return (
        session.query(ExchangeRate)
        .filter(ExchangeRate.effective_from <= at)
        .order_by(ExchangeRate.effective_from.desc())
        .first()
    )


def compute_cost(session, model_name: str, prompt_tokens: int, completion_tokens: int,
                  at: datetime.datetime) -> dict:
    """Returns the full cost breakdown for one call's token counts, using
    whichever pricing/exchange-rate rows are active at `at`. Returns all
    zeros (rather than raising) when no pricing/rate row exists yet at all
    — a brand-new install with nothing seeded should show $0/₹0, not crash
    every job's logging step."""
    pricing = get_active_pricing(session, model_name, at)
    rate_row = get_active_exchange_rate(session, at)

    input_price = pricing.input_price_usd_per_million if pricing else Decimal(0)
    output_price = pricing.output_price_usd_per_million if pricing else Decimal(0)
    exchange_rate = rate_row.usd_to_inr if rate_row else Decimal(0)

    input_cost_usd = (Decimal(prompt_tokens) / Decimal(1_000_000)) * Decimal(input_price)
    output_cost_usd = (Decimal(completion_tokens) / Decimal(1_000_000)) * Decimal(output_price)
    total_cost_usd = input_cost_usd + output_cost_usd
    total_cost_inr = total_cost_usd * Decimal(exchange_rate)

    return {
        "input_cost_usd": input_cost_usd,
        "output_cost_usd": output_cost_usd,
        "total_cost_usd": total_cost_usd,
        "exchange_rate_used": exchange_rate,
        "total_cost_inr": total_cost_inr,
    }


def record_usage_for_job(session, usage_entries: list[dict], job_id: int,
                          user_id: int, client_id: int, task_id: int,
                          at: datetime.datetime | None = None) -> None:
    """usage_entries: g.gemini_usage as populated by call_gemini() during
    this request — [{model, call_label, prompt_tokens, completion_tokens,
    total_tokens}, ...]. Writes one GeminiUsageLog row per entry. Does NOT
    commit — the caller (log_job()) commits once for the whole job,
    JobHistory row included, so a usage-logging failure can never leave a
    JobHistory row committed with its cost silently missing."""
    if not usage_entries:
        return
    at = at or datetime.datetime.utcnow()
    for entry in usage_entries:
        cost = compute_cost(session, entry["model"], entry["prompt_tokens"],
                             entry["completion_tokens"], at)
        session.add(GeminiUsageLog(
            job_id=job_id,
            user_id=user_id,
            client_id=client_id,
            task_id=task_id,
            model_name=entry["model"],
            call_label=entry.get("call_label"),
            prompt_tokens=entry["prompt_tokens"],
            completion_tokens=entry["completion_tokens"],
            total_tokens=entry["total_tokens"],
            input_cost_usd=cost["input_cost_usd"],
            output_cost_usd=cost["output_cost_usd"],
            total_cost_usd=cost["total_cost_usd"],
            exchange_rate_used=cost["exchange_rate_used"],
            total_cost_inr=cost["total_cost_inr"],
            timestamp=at,
        ))


def list_model_pricing(session=None) -> list[dict]:
    from database.db import SessionLocal
    own_session = session is None
    session = session or SessionLocal()
    try:
        rows = session.query(ModelPricing).order_by(
            ModelPricing.model_name, ModelPricing.effective_from.desc()
        ).all()
        return [{
            "id": p.id, "model_name": p.model_name,
            "input_price_usd_per_million": float(p.input_price_usd_per_million),
            "output_price_usd_per_million": float(p.output_price_usd_per_million),
            "effective_from": p.effective_from.isoformat() + "Z",
        } for p in rows]
    finally:
        if own_session:
            session.close()


def create_model_pricing(model_name: str, input_price: float, output_price: float,
                          effective_from: datetime.datetime, created_by: int) -> dict:
    from database.db import SessionLocal
    model_name = (model_name or "").strip()
    if not model_name:
        raise ValueError("model_name is required")
    if input_price < 0 or output_price < 0:
        raise ValueError("prices cannot be negative")
    session = SessionLocal()
    try:
        row = ModelPricing(
            model_name=model_name,
            input_price_usd_per_million=input_price,
            output_price_usd_per_million=output_price,
            effective_from=effective_from,
            created_by=created_by,
            is_placeholder=False,
        )
        session.add(row)
        session.commit()
        return {"id": row.id, "model_name": row.model_name}
    finally:
        session.close()


def list_exchange_rates(session=None) -> list[dict]:
    from database.db import SessionLocal
    own_session = session is None
    session = session or SessionLocal()
    try:
        rows = session.query(ExchangeRate).order_by(ExchangeRate.effective_from.desc()).all()
        return [{
            "id": r.id, "usd_to_inr": float(r.usd_to_inr),
            "effective_from": r.effective_from.isoformat() + "Z",
        } for r in rows]
    finally:
        if own_session:
            session.close()


def create_exchange_rate(usd_to_inr: float, effective_from: datetime.datetime, created_by: int) -> dict:
    from database.db import SessionLocal
    if usd_to_inr <= 0:
        raise ValueError("usd_to_inr must be positive")
    session = SessionLocal()
    try:
        row = ExchangeRate(
            usd_to_inr=usd_to_inr,
            effective_from=effective_from,
            created_by=created_by,
            is_placeholder=False,
        )
        session.add(row)
        session.commit()
        return {"id": row.id}
    finally:
        session.close()
