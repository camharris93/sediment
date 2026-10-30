#!/usr/bin/env python
"""saasco — a seeded, deterministic synthetic SaaS dataset with PLANTED stories.

    python datasets/saasco/generate.py                       # dev variant -> datasets/saasco/data
    python datasets/saasco/generate.py --variant holdout     # -> datasets/saasco_holdout/data
    python datasets/saasco/generate.py --no-truths --out /tmp/x

Pure Python (`random.Random(seed)`, no numpy). Same seed + variant -> byte-identical
CSVs on every platform. After writing the CSVs it runs every answer-key
`fixture_sql` against a scratch DuckDB built from them and writes the OBSERVED
values back into the answer key, so the key states what IS in the data, not what
the generator meant to put there.

Design rules (docs/investigate.md §7):
  * every table has a single surrogate key; join keys share names;
  * no column names or values give a story away (no churn_type, promo_flag, ...);
  * stories are only reachable through behaviour — dates, prices, invoice
    statuses, usage — on top of background noise (size-band hazard, a December
    dip, random support load, ~1% null industries, plan upgrades).

The "decoy" attribute (industry=healthcare on dev, region=APAC on holdout) has NO
causal effect: it is assigned AFTER the simulation from its own RNG stream, and
that stream's sub-seed is searched (deterministically) until sampling luck alone
produces a one-quarter churn blip. The chosen sub-seed is printed and recorded.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import math
import random
import re
import sys
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]

START = date(2024, 1, 1)
END = date(2026, 6, 30)
N_MONTHS = 30
TABLES = ["accounts", "subscriptions", "invoices", "usage_weekly", "support_tickets"]

# Staging column types — MUST match the casts in dbt_project/macros/saasco.sql.
# Truths are computed on a scratch copy of staging built with exactly these.
STAGING_TYPES: dict[str, list[tuple[str, str]]] = {
    "accounts": [("account_id", "INTEGER"), ("signup_date", "DATE"), ("company_size_band", "VARCHAR"),
                 ("industry", "VARCHAR"), ("region", "VARCHAR"), ("acquisition_channel", "VARCHAR"),
                 ("sales_assisted", "BOOLEAN")],
    "subscriptions": [("subscription_id", "INTEGER"), ("account_id", "INTEGER"), ("plan_tier", "VARCHAR"),
                      ("billing_interval", "VARCHAR"), ("list_price_monthly", "DOUBLE"), ("seats", "INTEGER"),
                      ("start_date", "DATE"), ("end_date", "DATE"), ("status", "VARCHAR"),
                      ("cancel_reason", "VARCHAR")],
    "invoices": [("invoice_id", "INTEGER"), ("subscription_id", "INTEGER"), ("account_id", "INTEGER"),
                 ("invoice_period", "VARCHAR"), ("invoice_date", "DATE"), ("amount", "DOUBLE"),
                 ("status", "VARCHAR"), ("attempt_number", "INTEGER")],
    "usage_weekly": [("usage_id", "INTEGER"), ("account_id", "INTEGER"), ("week_start", "DATE"),
                     ("active_users", "INTEGER"), ("key_actions", "INTEGER")],
    "support_tickets": [("ticket_id", "INTEGER"), ("account_id", "INTEGER"), ("created_at", "TIMESTAMP"),
                        ("category", "VARCHAR"), ("priority", "VARCHAR"), ("first_response_hours", "DOUBLE"),
                        ("csat", "INTEGER")],
}

BANDS = ["1-10", "11-50", "51-200", "201+"]
CHANNELS = ["organic", "paid_search", "paid_social", "partner", "outbound"]
REGIONS = ["NA", "EU", "APAC", "LATAM"]
INDUSTRIES = ["software", "retail", "finance", "education", "manufacturing", "media", "nonprofit"]
TIERS = ["starter", "growth", "pro"]


# ─────────────────────────────────────────────────────────────────────────────
# Variant knobs. Everything a story depends on lives here; the simulation below
# is shared. Holdout uses the same schema with different magnitudes and dates,
# moves S3/S6/D1 to other segments, and has NO activation effect (S4 absent).
# ─────────────────────────────────────────────────────────────────────────────

VARIANTS: dict[str, dict] = {
    "dev": {
        "seed": 20261030,
        "dataset": "saasco",
        "base_signups": 82,
        # S1 — annual promo cohort, hidden until renewal
        "promo_months": [(2025, 4), (2025, 5), (2025, 6)],
        "promo_extra_annual": 70,
        "p_annual": 0.22,
        "p_low_usage_annual": 0.20, "p_low_usage_promo": 0.55,
        "p_renew_churn_low": 0.62, "p_renew_churn_high": 0.08,
        # S2 — starter price step, grandfathered; new post-change cohort churns early
        "price_change": date(2025, 7, 1), "starter_price_old": 29, "starter_price_new": 49,
        "price_early_mult": 1.45,
        # S3 — involuntary churn masked as voluntary
        "fail_region": "EU", "fail_from": date(2026, 1, 1),
        "fail_event_p": 0.10, "fail_event_churn_p": 0.85, "fail_first_attempt_p": 0.15,
        # S4 — activation
        "activation_mult": 3.0, "p_low_activation": 0.25,
        # S5 — sales assistance helps within band, looks harmful pooled
        "sa_mult": 0.62, "p_sa": {"1-10": 0.85, "11-50": 0.15, "51-200": 0.15, "201+": 0.15},
        # S6 — channel mix shift into a constant-but-high-churn channel
        "mix_channel": "paid_social", "mix_from": (2025, 10),
        "mix_share_before": 0.10, "mix_ramp": [0.18, 0.26, 0.32], "mix_share_after": 0.35,
        "channel_mult": {"organic": 0.85, "paid_search": 1.0, "paid_social": 2.3,
                         "partner": 0.8, "outbound": 1.0},
        "region_weights": {"NA": 0.45, "EU": 0.30, "APAC": 0.15, "LATAM": 0.10},
        # D1 — decoy (post-hoc, no effect)
        "decoy": {"attr": "industry", "value": "healthcare", "share": 0.014,
                  "quarter": (2026, 1)},
    },
    "holdout": {
        "seed": 77015,
        "dataset": "saasco_holdout",
        "base_signups": 85,
        "promo_months": [(2025, 4), (2025, 5), (2025, 6)],
        "promo_extra_annual": 80,
        "p_annual": 0.25,
        "p_low_usage_annual": 0.22, "p_low_usage_promo": 0.60,
        "p_renew_churn_low": 0.60, "p_renew_churn_high": 0.10,
        "price_change": date(2025, 3, 1), "starter_price_old": 29, "starter_price_new": 45,
        "price_early_mult": 1.8,
        "fail_region": "LATAM", "fail_from": date(2026, 1, 1),
        "fail_event_p": 0.095, "fail_event_churn_p": 0.80, "fail_first_attempt_p": 0.14,
        "activation_mult": 1.0, "p_low_activation": 0.25,          # S4 ABSENT
        "sa_mult": 0.65, "p_sa": {"1-10": 0.85, "11-50": 0.15, "51-200": 0.15, "201+": 0.15},
        "mix_channel": "partner", "mix_from": (2025, 9),
        "mix_share_before": 0.11, "mix_ramp": [0.16, 0.21, 0.26], "mix_share_after": 0.30,
        "channel_mult": {"organic": 0.85, "paid_search": 1.05, "paid_social": 1.1,
                         "partner": 2.1, "outbound": 0.95},
        # LATAM is causal (pre-sim); NA/EU/APAC split happens post-hoc (APAC = decoy)
        "region_weights": {"LATAM": 0.20, "NON": 0.80},
        "post_hoc_regions": {"NA": 0.55, "EU": 0.43},
        "decoy": {"attr": "region", "value": "APAC", "share": 0.022, "quarter": (2026, 1)},
    },
}

BASE_HAZARD = {"1-10": 0.060, "11-50": 0.028, "51-200": 0.015, "201+": 0.008}
TIER_MULT = {"starter": 1.15, "growth": 1.0, "pro": 0.8}
TIER_PRICE = {"growth": 59, "pro": 99}
BAND_WEIGHTS = {"1-10": 0.40, "11-50": 0.30, "51-200": 0.20, "201+": 0.10}
SEATS = {"1-10": (3, 8), "11-50": (5, 20), "51-200": (15, 50), "201+": (30, 120)}
TIER_BY_BAND = {"1-10": (0.65, 0.30, 0.05), "11-50": (0.45, 0.40, 0.15),
                "51-200": (0.25, 0.45, 0.30), "201+": (0.10, 0.40, 0.50)}
BASE_FIRST_ATTEMPT_FAIL = 0.05
BASE_INVOLUNTARY_P = 0.003
UPGRADE_P = 0.004
ANNUAL_DISCOUNT = 0.85


# ─────────────────────────────────────────────────────────────────────────────
# Small deterministic helpers (only Random.random/randint/getrandbits are used —
# stable across CPython 3.10-3.13).
# ─────────────────────────────────────────────────────────────────────────────

def month_start(i: int) -> date:
    return date(START.year + (START.month - 1 + i) // 12, (START.month - 1 + i) % 12 + 1, 1)


def add_months(d: date, n: int) -> date:
    y, m = d.year + (d.month - 1 + n) // 12, (d.month - 1 + n) % 12 + 1
    return date(y, m, min(d.day, 28))


def pick(rng: random.Random, weights: dict) -> str:
    total = sum(weights.values())
    r = rng.random() * total
    acc = 0.0
    for k, w in weights.items():
        acc += w
        if r < acc:
            return k
    return list(weights)[-1]


def poisson(rng: random.Random, lam: float) -> int:
    if lam <= 0:
        return 0
    lim, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= lim:
            return k
        k += 1


def monday_on_or_after(d: date) -> date:
    return d + timedelta(days=(7 - d.weekday()) % 7)


# ─────────────────────────────────────────────────────────────────────────────
# Simulation
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Sub:
    subscription_id: int
    account_id: int
    plan_tier: str
    billing_interval: str
    list_price_monthly: int
    seats: int
    start_date: date
    end_date: date | None = None
    status: str = "active"
    cancel_reason: str | None = None


@dataclass
class Account:
    account_id: int
    signup_date: date
    company_size_band: str
    region: str | None
    acquisition_channel: str
    sales_assisted: bool
    industry: str | None = None
    # latent (never written)
    low_activation: bool = False
    low_usage: bool = False
    engagement: float = 0.6
    churn_kind: str | None = None          # voluntary | involuntary | renewal
    subs: list[Sub] = field(default_factory=list)
    fail_months: list[date] = field(default_factory=list)

    @property
    def end_date(self) -> date | None:
        return None if any(s.end_date is None for s in self.subs) else max(s.end_date for s in self.subs)


class Sim:
    def __init__(self, v: dict):
        self.v = v
        self.rng = random.Random(v["seed"])
        self.accounts: list[Account] = []
        self.invoices: list[dict] = []
        self.usage: list[dict] = []
        self.tickets: list[dict] = []
        self._sub_id = 0

    # ── signups ──────────────────────────────────────────────────────────
    def channel_weights(self, ym: tuple[int, int]) -> dict:
        v = self.v
        y0, m0 = v["mix_from"]
        k = (ym[0] - y0) * 12 + (ym[1] - m0)
        if k < 0:
            share = v["mix_share_before"]
        elif k < len(v["mix_ramp"]):
            share = v["mix_ramp"][k]
        else:
            share = v["mix_share_after"]
        base = {"organic": 0.35, "paid_search": 0.25, "paid_social": 0.12, "partner": 0.14,
                "outbound": 0.14}
        others = {c: w for c, w in base.items() if c != v["mix_channel"]}
        tot = sum(others.values())
        out = {c: (1 - share) * w / tot for c, w in others.items()}
        out[v["mix_channel"]] = share
        return {c: out[c] for c in CHANNELS}

    def make_accounts(self) -> None:
        v = self.v
        aid = 0
        for i in range(N_MONTHS):
            ms = month_start(i)
            ym = (ms.year, ms.month)
            n = round(v["base_signups"] * (1 + 0.012 * i) * (0.85 if ms.month == 12 else 1.0))
            extra = v["promo_extra_annual"] if ym in v["promo_months"] else 0
            for j in range(n + extra):
                aid += 1
                promo = j >= n
                arng = random.Random(v["seed"] * 1_000_003 + aid)
                band = pick(arng, BAND_WEIGHTS)
                region = pick(arng, v["region_weights"])
                acct = Account(
                    account_id=aid,
                    signup_date=ms + timedelta(days=arng.randint(0, 27)),
                    company_size_band=band,
                    region=None if region == "NON" else region,
                    acquisition_channel=pick(arng, self.channel_weights(ym)),
                    sales_assisted=arng.random() < v["p_sa"][band],
                )
                if v["decoy"]["attr"] != "industry":   # industry has no effect; draw it now
                    acct.industry = (None if arng.random() < 0.01
                                     else pick(arng, {k: 1 for k in INDUSTRIES + ["healthcare"]}))
                annual = promo or arng.random() < v["p_annual"]
                acct.low_activation = arng.random() < v["p_low_activation"]
                p_low = v["p_low_usage_promo"] if promo else v["p_low_usage_annual"]
                acct.low_usage = annual and arng.random() < p_low
                acct.engagement = (0.12 + 0.18 * arng.random()) if acct.low_usage \
                    else (0.40 + 0.50 * arng.random())
                tp = TIER_BY_BAND[band]
                tier = pick(arng, {"starter": tp[0], "growth": tp[1], "pro": tp[2]})
                lo, hi = SEATS[band]
                seats = arng.randint(lo, hi)
                acct.subs.append(self.new_sub(acct, tier, "annual" if annual else "monthly",
                                              seats, acct.signup_date))
                self.simulate(acct, arng)
                self.accounts.append(acct)

    def price(self, tier: str, start: date) -> int:
        if tier == "starter":
            return (self.v["starter_price_new"] if start >= self.v["price_change"]
                    else self.v["starter_price_old"])
        return TIER_PRICE[tier]

    def new_sub(self, acct: Account, tier: str, interval: str, seats: int, start: date) -> Sub:
        self._sub_id += 1
        return Sub(self._sub_id, acct.account_id, tier, interval, self.price(tier, start), seats, start)

    # ── invoices ─────────────────────────────────────────────────────────
    def invoice(self, sub: Sub, d: date, rng: random.Random, *, fail_all: int = 0,
                p_first_fail: float = BASE_FIRST_ATTEMPT_FAIL) -> None:
        amount = sub.list_price_monthly * sub.seats
        if sub.billing_interval == "annual":
            amount = round(amount * 12 * ANNUAL_DISCOUNT, 2)
        period = f"{d.year:04d}-{d.month:02d}"
        offsets = [0, 3, 7, 14]
        if fail_all:
            n_fail, paid = fail_all, False
        else:
            n_fail = 0
            if rng.random() < p_first_fail:
                n_fail = 1 if rng.random() < 0.8 else 2
            paid = True
        for a in range(n_fail):
            self.invoices.append(dict(subscription_id=sub.subscription_id, account_id=sub.account_id,
                                      invoice_period=period, invoice_date=d + timedelta(days=offsets[a]),
                                      amount=amount, status="failed", attempt_number=a + 1))
        if paid:
            status = "refunded" if rng.random() < 0.004 else "paid"
            self.invoices.append(dict(subscription_id=sub.subscription_id, account_id=sub.account_id,
                                      invoice_period=period, invoice_date=d + timedelta(days=offsets[n_fail]),
                                      amount=amount, status=status, attempt_number=n_fail + 1))

    def in_fail_regime(self, acct: Account, d: date) -> bool:
        # Card-on-file failures hit tenured accounts (expired/replaced cards), not
        # brand-new signups whose card was just verified.
        return (acct.region == self.v["fail_region"] and d >= self.v["fail_from"]
                and (d - acct.signup_date).days >= 100)

    def voluntary_reason(self, rng: random.Random, sub: Sub) -> str | None:
        w = {"customer_request": 0.35, "too_expensive": 0.25, "missing_feature": 0.20,
             "other": 0.10, "none": 0.10}
        if sub.plan_tier == "starter" and sub.start_date >= self.v["price_change"]:
            w["too_expensive"] = 0.40
        r = pick(rng, w)
        return None if r == "none" else r

    def end(self, acct: Account, sub: Sub, d: date, kind: str, reason: str | None) -> None:
        sub.end_date, sub.status, sub.cancel_reason = d, "canceled", reason
        acct.churn_kind = kind

    def involuntary(self, acct: Account, sub: Sub, d: date, rng: random.Random) -> bool:
        """A payment-failure event at billing date d. Returns True if it churned."""
        v = self.v
        p_event = v["fail_event_p"] if self.in_fail_regime(acct, d) else BASE_INVOLUNTARY_P
        if rng.random() >= p_event:
            return False
        acct.fail_months.append(d)
        n_fail = 2 + (rng.random() < 0.5) + (rng.random() < 0.3)
        churn_p = v["fail_event_churn_p"] if self.in_fail_regime(acct, d) else 0.9
        end_d = d + timedelta(days=21)
        if rng.random() < churn_p and end_d <= END:
            self.invoice(sub, d, rng, fail_all=n_fail)
            self.end(acct, sub, end_d, "involuntary",
                     "customer_request" if rng.random() < 0.5 else None)
            return True
        # recovered: the failed attempts, then a paid retry
        self.invoice(sub, d, rng, fail_all=n_fail)
        self.invoices.append(dict(subscription_id=sub.subscription_id, account_id=sub.account_id,
                                  invoice_period=f"{d.year:04d}-{d.month:02d}",
                                  invoice_date=d + timedelta(days=18), amount=self.invoices[-1]["amount"],
                                  status="paid", attempt_number=n_fail + 1))
        return False

    # ── churn simulation ─────────────────────────────────────────────────
    def hazard(self, acct: Account, sub: Sub, d: date, k: int) -> float:
        v = self.v
        h = BASE_HAZARD[acct.company_size_band] * TIER_MULT[sub.plan_tier]
        h *= v["channel_mult"][acct.acquisition_channel]
        if acct.sales_assisted:
            h *= v["sa_mult"]
        if d.month == 12:
            h *= 0.7
        tenure = (d.year - acct.signup_date.year) * 12 + d.month - acct.signup_date.month + 1
        if acct.low_activation and 2 <= tenure <= 6:
            h *= v["activation_mult"]
        if sub.plan_tier == "starter" and sub.start_date >= v["price_change"] and k <= 3:
            h *= v["price_early_mult"]
        return min(h, 0.9)

    def simulate(self, acct: Account, rng: random.Random) -> None:
        v = self.v
        sub = acct.subs[0]
        def p_ff(d: date) -> float:
            return v["fail_first_attempt_p"] if self.in_fail_regime(acct, d) else BASE_FIRST_ATTEMPT_FAIL
        self.invoice(sub, sub.start_date, rng, p_first_fail=p_ff(sub.start_date))
        if sub.billing_interval == "annual":
            k = 1
            while True:
                d = add_months(sub.start_date, 12 * k)
                if d > END:
                    return
                p = v["p_renew_churn_low"] if acct.low_usage else v["p_renew_churn_high"]
                if acct.sales_assisted:
                    p *= v["sa_mult"]
                if rng.random() < p:
                    self.end(acct, sub, d, "renewal", self.voluntary_reason(rng, sub))
                    return
                if self.involuntary(acct, sub, d, rng):
                    return
                if not acct.fail_months or acct.fail_months[-1] != d:
                    self.invoice(sub, d, rng, p_first_fail=p_ff(d))
                k += 1
        k = 1
        while True:
            d = add_months(sub.start_date, k)
            if d > END:
                return
            if rng.random() < self.hazard(acct, sub, d, k):
                self.end(acct, sub, d, "voluntary", self.voluntary_reason(rng, sub))
                return
            if sub.plan_tier != "pro" and rng.random() < UPGRADE_P:
                overlap = rng.randint(0, 4)
                new_tier = TIERS[TIERS.index(sub.plan_tier) + 1]
                sub.end_date, sub.status, sub.cancel_reason = d, "canceled", None
                sub = self.new_sub(acct, new_tier, "monthly", sub.seats, d - timedelta(days=overlap))
                acct.subs.append(sub)
                self.invoice(sub, sub.start_date, rng, p_first_fail=p_ff(sub.start_date))
                k = 1
                continue
            if self.involuntary(acct, sub, d, rng):
                return
            if not acct.fail_months or acct.fail_months[-1] != d:
                self.invoice(sub, d, rng, p_first_fail=p_ff(d))
            k += 1

    # ── usage + support (after the timeline is known) ────────────────────
    def behaviour(self) -> None:
        for acct in self.accounts:
            rng = random.Random(self.v["seed"] * 7_919 + acct.account_id)
            end = acct.end_date
            last = min(end, END) if end else END
            seats = max(s.seats for s in acct.subs)
            w0 = monday_on_or_after(acct.signup_date)
            week = w0
            i = 0
            decline_from = None
            if end and acct.churn_kind == "renewal":
                decline_from = end - timedelta(weeks=13 + rng.randint(0, 9))
            elif end and acct.churn_kind == "voluntary":
                decline_from = end - timedelta(weeks=6)
            while week <= last:
                e = acct.engagement
                if acct.low_activation and i >= 8:
                    e *= 0.6
                if decline_from and week >= decline_from:
                    span = max((end - decline_from).days, 1)
                    frac = min((week - decline_from).days / span, 1.0)
                    e *= 1 - 0.8 * frac
                au = round(seats * e * (0.8 + 0.4 * rng.random()))
                au = max(0, min(seats, au))
                if i < 8:
                    au = min(au, rng.randint(0, 2)) if acct.low_activation else max(au, 3)
                ka = sum(1 for _ in range(au * 4) if rng.random() < 0.75) * 3 + rng.randint(0, 3)
                self.usage.append(dict(account_id=acct.account_id, week_start=week,
                                       active_users=au, key_actions=ka))
                week += timedelta(weeks=1)
                i += 1
            # support tickets: background load + billing tickets after failures
            m = date(acct.signup_date.year, acct.signup_date.month, 1)
            while m <= last:
                lam = 0.12 + 0.05 * BANDS.index(acct.company_size_band)
                n = poisson(rng, lam)
                if any(f.year == m.year and f.month == m.month for f in acct.fail_months) \
                        and rng.random() < 0.45:
                    self._ticket(acct, m, rng, "billing")
                if end and acct.churn_kind == "voluntary" and (end.year, end.month) == (m.year, m.month) \
                        and rng.random() < 0.3:
                    self._ticket(acct, m, rng, None, unhappy=True)
                for _ in range(n):
                    self._ticket(acct, m, rng, None)
                m = add_months(m, 1)
                m = date(m.year, m.month, 1)

    def _ticket(self, acct: Account, m: date, rng: random.Random, category: str | None,
                unhappy: bool = False) -> None:
        cat = category or pick(rng, {"bug": 0.30, "how_to": 0.35, "feature_request": 0.20, "billing": 0.15})
        hrs = round(math.exp(1.4 + 0.8 * (sum(rng.random() for _ in range(6)) - 3) / 0.707), 1)
        csat = None
        if rng.random() > 0.45:
            base = 2.6 if unhappy else 4.0
            csat = max(1, min(5, round(base + (rng.random() - 0.5) * 2.4)))
        day = m + timedelta(days=rng.randint(0, 27))
        if day > END:
            return
        self.tickets.append(dict(account_id=acct.account_id,
                                 created_at=f"{day.isoformat()} {rng.randint(7, 19):02d}:{rng.randint(0, 59):02d}:00",
                                 category=cat,
                                 priority=pick(rng, {"low": 0.35, "medium": 0.4, "high": 0.2, "urgent": 0.05}),
                                 first_response_hours=hrs, csat=csat))

    # ── decoy: post-hoc assignment with a searched sub-seed ──────────────
    def assign_post_hoc(self) -> dict:
        v = self.v
        dec = v["decoy"]
        stats = quarter_stats(self.accounts)
        target = dec["quarter"]
        # Only accounts whose decoy attribute is still unassigned are eligible (on
        # holdout the causal LATAM segment was fixed before the simulation).
        eligible = [a for a in self.accounts if getattr(a, dec["attr"]) is None]
        want = dec["share"] * len(self.accounts)
        for k in range(20_000):
            rng = random.Random(v["seed"] * 31 + k)
            flags = [rng.random() < dec["share"] * len(self.accounts) / len(eligible) for _ in eligible]
            group = [a for a, f in zip(eligible, flags) if f]
            if not (0.9 * want <= len(group) <= 1.15 * want):
                continue
            g = quarter_stats(group)
            ratios = {}
            ok = True
            for q, (n_all, c_all) in stats.items():
                n, c = g.get(q, (0, 0))
                if n < 8 or c_all == 0:
                    continue
                ratios[q] = (c / n) / (c_all / n_all)
            if target not in ratios or g[target][0] < 20:
                continue
            if not (2.2 <= ratios[target] <= 3.2):
                continue
            if any(r > 1.5 for q, r in ratios.items() if q != target):
                ok = False
            if ok:
                break
        else:
            raise SystemExit("decoy search failed — widen the criteria")
        rest = random.Random(v["seed"] * 37 + k)
        for a, f in zip(eligible, flags):
            if dec["attr"] == "industry":
                a.industry = dec["value"] if f else (None if rest.random() < 0.01
                                                     else pick(rest, {i: 1 for i in INDUSTRIES}))
            else:
                a.region = dec["value"] if f else pick(rest, v["post_hoc_regions"])
        return {"decoy_subseed": k, "decoy_n": sum(flags),
                "decoy_ratio_target_q": round(ratios[target], 3),
                "decoy_ratios": {f"{q[0]}Q{q[1]}": round(r, 2) for q, r in sorted(ratios.items())}}


def quarter_stats(accounts: list[Account]) -> dict[tuple[int, int], tuple[int, int]]:
    """(year, quarter) -> (accounts active at quarter start, of those churned in quarter)."""
    out: dict[tuple[int, int], tuple[int, int]] = {}
    for y in (2024, 2025, 2026):
        for q in (1, 2, 3, 4):
            qs = date(y, 3 * (q - 1) + 1, 1)
            if qs <= START or qs > END:
                continue
            qe = add_months(qs, 3) - timedelta(days=1)
            n = c = 0
            for a in accounts:
                if a.signup_date >= qs:
                    continue
                e = a.end_date
                if e is not None and e < qs:
                    continue
                n += 1
                if e is not None and e <= qe:
                    c += 1
            out[(y, q)] = (n, c)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Output
# ─────────────────────────────────────────────────────────────────────────────

def _fmt(x) -> str:
    if x is None:
        return ""
    if isinstance(x, bool):
        return "true" if x else "false"
    if isinstance(x, float):
        return f"{x:.2f}"
    return str(x)


def to_csv(header: list[str], rows: list[list]) -> bytes:
    buf = io.StringIO(newline="")
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(header)
    for r in rows:
        w.writerow([_fmt(x) for x in r])
    return buf.getvalue().encode("utf-8")


def build_tables(sim: Sim) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    out["accounts"] = to_csv(
        ["account_id", "signup_date", "company_size_band", "industry", "region",
         "acquisition_channel", "sales_assisted"],
        [[a.account_id, a.signup_date, a.company_size_band, a.industry, a.region,
          a.acquisition_channel, a.sales_assisted] for a in sim.accounts])
    subs = sorted((s for a in sim.accounts for s in a.subs), key=lambda s: s.subscription_id)
    out["subscriptions"] = to_csv(
        ["subscription_id", "account_id", "plan_tier", "billing_interval", "list_price_monthly",
         "seats", "start_date", "end_date", "status", "cancel_reason"],
        [[s.subscription_id, s.account_id, s.plan_tier, s.billing_interval, s.list_price_monthly,
          s.seats, s.start_date, s.end_date, s.status, s.cancel_reason] for s in subs])
    # The extract is taken on END: retries scheduled after it don't exist yet.
    inv = sorted((r for r in sim.invoices if r["invoice_date"] <= END),
                 key=lambda r: (r["invoice_date"], r["subscription_id"], r["attempt_number"]))
    out["invoices"] = to_csv(
        ["invoice_id", "subscription_id", "account_id", "invoice_period", "invoice_date", "amount",
         "status", "attempt_number"],
        [[i + 1, r["subscription_id"], r["account_id"], r["invoice_period"], r["invoice_date"],
          float(r["amount"]), r["status"], r["attempt_number"]] for i, r in enumerate(inv)])
    us = sorted(sim.usage, key=lambda r: (r["week_start"], r["account_id"]))
    out["usage_weekly"] = to_csv(
        ["usage_id", "account_id", "week_start", "active_users", "key_actions"],
        [[i + 1, r["account_id"], r["week_start"], r["active_users"], r["key_actions"]]
         for i, r in enumerate(us)])
    tk = sorted(sim.tickets, key=lambda r: (r["created_at"], r["account_id"]))
    out["support_tickets"] = to_csv(
        ["ticket_id", "account_id", "created_at", "category", "priority", "first_response_hours", "csat"],
        [[i + 1, r["account_id"], r["created_at"], r["category"], r["priority"],
          float(r["first_response_hours"]), r["csat"]] for i, r in enumerate(tk)])
    return out


def generate(variant: str) -> tuple[dict[str, bytes], dict]:
    v = VARIANTS[variant]
    sim = Sim(v)
    sim.make_accounts()
    meta = sim.assign_post_hoc()
    sim.behaviour()
    return build_tables(sim), meta


# ─────────────────────────────────────────────────────────────────────────────
# Truths: run answer-key fixture_sql on a scratch DuckDB built from the CSVs
# ─────────────────────────────────────────────────────────────────────────────

def answer_key_path(variant: str) -> Path:
    name = "saasco_answer_key.yaml" if variant == "dev" else "saasco_holdout_answer_key.yaml"
    return REPO_ROOT / "evals" / "investigations" / name


def compute_truths(variant: str, data_dir: Path) -> dict[str, float]:
    import duckdb
    import yaml

    key_path = answer_key_path(variant)
    key = yaml.safe_load(key_path.read_text(encoding="utf-8"))
    schema = f"{VARIANTS[variant]['dataset']}_staging"
    con = duckdb.connect(":memory:")
    con.execute(f'CREATE SCHEMA "{schema}"')
    for t in TABLES:
        p = str(data_dir / f"{t}.csv").replace("\\", "/")
        cols = ", ".join(f'CAST(NULLIF("{c}", \'\') AS {ty}) AS "{c}"' for c, ty in STAGING_TYPES[t])
        con.execute(f'CREATE TABLE "{schema}"."stg_{t}" AS SELECT {cols} '
                    f"FROM read_csv('{p}', header=true, all_varchar=true)")
    observed: dict[str, float] = {}
    for story in key["stories"]:
        for t in story.get("truths", []) or []:
            row = con.execute(t["fixture_sql"]).fetchone()
            observed[t["id"]] = round(float(row[0]), 4)
    con.close()
    return observed


def write_observed(variant: str, observed: dict[str, float]) -> None:
    """Rewrite only the `observed:` line under each truth id, keeping the key's
    comments and layout intact."""
    path = answer_key_path(variant)
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    current = None
    for i, line in enumerate(lines):
        m = re.match(r"\s*- id: (\S+)", line)
        if m:
            current = m.group(1)
        m = re.match(r"(\s*observed:)\s*.*?(\r?\n)?$", line)
        if m and current in observed:
            lines[i] = f"{m.group(1)} {observed[current]}{m.group(2) or ''}"
    path.write_text("".join(lines), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--variant", choices=sorted(VARIANTS), default="dev")
    ap.add_argument("--out", help="output dir (default: datasets/<dataset>/data)")
    ap.add_argument("--no-truths", action="store_true", help="skip writing observed truths")
    args = ap.parse_args(argv)

    v = VARIANTS[args.variant]
    out = Path(args.out) if args.out else REPO_ROOT / "datasets" / v["dataset"] / "data"
    out.mkdir(parents=True, exist_ok=True)
    tables, meta = generate(args.variant)
    for t, data in tables.items():
        (out / f"{t}.csv").write_bytes(data)
        n_rows = data.count(b"\n") - 1
        print(f"  [ok] {t}.csv  {n_rows:>8,} rows  sha256={hashlib.sha256(data).hexdigest()[:12]}")
    print(f"  decoy: {meta}")
    if not args.no_truths:
        obs = compute_truths(args.variant, out)
        write_observed(args.variant, obs)
        print(f"  truths written to {answer_key_path(args.variant).relative_to(REPO_ROOT)}:")
        for k, val in obs.items():
            print(f"    {k:8} {val}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
