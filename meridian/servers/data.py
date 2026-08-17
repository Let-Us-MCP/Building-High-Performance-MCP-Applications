"""Deterministic fake data for Meridian.

Everything is generated from a fixed seed, so a measurement taken on your
machine is comparable to the one printed in the book. No network, no database,
no clock dependence in the data itself. The only nondeterminism left in the
system is the one we actually want to measure: time.
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass, field

SEED = 20260728

INDUSTRIES = [
    "commercial real estate", "logistics", "specialty chemicals",
    "regional healthcare", "agricultural equipment", "renewable energy",
    "food distribution", "precision manufacturing",
]

REGIONS = ["us-east", "us-west", "eu-central", "eu-west", "apac-south"]

FILING_TYPES = ["10-K", "10-Q", "8-K", "S-1", "DEF 14A"]


@dataclass
class Account:
    account_id: str
    name: str
    industry: str
    region: str
    revenue_musd: float
    leverage: float
    years_trading: int
    prior_defaults: int
    tier: str

    def to_json(self) -> dict:
        return {
            "accountId": self.account_id,
            "name": self.name,
            "industry": self.industry,
            "region": self.region,
            "revenueMusd": round(self.revenue_musd, 1),
            "leverage": round(self.leverage, 2),
            "yearsTrading": self.years_trading,
            "priorDefaults": self.prior_defaults,
            "tier": self.tier,
        }


@dataclass
class Transaction:
    txn_id: str
    account_id: str
    amount_usd: float
    counterparty: str
    corridor: str
    flagged: bool
    reason: str = ""

    def to_json(self) -> dict:
        out = {
            "txnId": self.txn_id,
            "accountId": self.account_id,
            "amountUsd": round(self.amount_usd, 2),
            "counterparty": self.counterparty,
            "corridor": self.corridor,
            "flagged": self.flagged,
        }
        if self.reason:
            out["reason"] = self.reason
        return out


def _rng(salt: str = "") -> random.Random:
    return random.Random(SEED + int(hashlib.sha256(salt.encode()).hexdigest()[:8], 16))


def build_accounts(count: int = 240) -> dict[str, Account]:
    rng = _rng("accounts")
    prefixes = ["Northwind", "Meridian", "Halcyon", "Brightwater", "Ironvale",
                "Cordelia", "Sundance", "Keystone", "Ravensmoor", "Ostara",
                "Belvedere", "Ashcroft", "Waypoint", "Thistledown", "Calderon"]
    suffixes = ["Holdings", "Industries", "Partners", "Group", "Logistics",
                "Capital", "Works", "Systems", "Trading", "Collective"]
    out: dict[str, Account] = {}
    for i in range(count):
        acct_id = f"ACC-{1000 + i}"
        revenue = rng.lognormvariate(3.4, 1.05)
        leverage = max(0.2, rng.gauss(2.1, 1.1))
        years = rng.randint(1, 42)
        defaults = 0 if rng.random() < 0.86 else rng.randint(1, 3)
        score = leverage * 1.4 - min(years, 20) * 0.06 + defaults * 1.3
        tier = "A" if score < 1.6 else "B" if score < 2.8 else "C" if score < 4.2 else "D"
        out[acct_id] = Account(
            account_id=acct_id,
            name=f"{rng.choice(prefixes)} {rng.choice(suffixes)}",
            industry=rng.choice(INDUSTRIES),
            region=rng.choice(REGIONS),
            revenue_musd=revenue,
            leverage=leverage,
            years_trading=years,
            prior_defaults=defaults,
            tier=tier,
        )
    return out


def build_transactions(accounts: dict[str, Account], per_account: int = 6
                       ) -> dict[str, list[Transaction]]:
    rng = _rng("transactions")
    corridors = ["US-EU", "US-APAC", "EU-EU", "US-US", "EU-MENA", "APAC-APAC"]
    counterparties = ["Adalwolf GmbH", "Pinecrest LLC", "Baltic Freight AB",
                      "Zenith Pacific", "Corvid Metals", "Alderway Foods",
                      "Tessera Chemical", "Ferrolane BV"]
    reasons = [
        "amount just below the reporting threshold",
        "new counterparty in a high-risk corridor",
        "velocity spike against a 90-day baseline",
        "round-number transfer to a shell-like entity",
    ]
    out: dict[str, list[Transaction]] = {}
    for acct_id in accounts:
        items = []
        for j in range(per_account):
            flagged = rng.random() < 0.13
            amount = rng.choice([9_850.0, 250_000.0, rng.uniform(500, 480_000)])
            items.append(Transaction(
                txn_id=f"TXN-{acct_id[4:]}-{j:02d}",
                account_id=acct_id,
                amount_usd=amount,
                counterparty=rng.choice(counterparties),
                corridor=rng.choice(corridors),
                flagged=flagged,
                reason=rng.choice(reasons) if flagged else "",
            ))
        out[acct_id] = items
    return out


def build_filings(count: int = 120) -> dict[str, dict]:
    rng = _rng("filings")
    out = {}
    for i in range(count):
        fid = f"FIL-{2000 + i}"
        out[fid] = {
            "filingId": fid,
            "type": rng.choice(FILING_TYPES),
            "fiscalYear": rng.choice([2023, 2024, 2025, 2026]),
            "pages": rng.randint(18, 340),
            "summary": (
                "Management discussion notes margin compression in the "
                f"{rng.choice(INDUSTRIES)} segment, partially offset by "
                "pricing actions taken in the second half."
            ),
        }
    return out


ACCOUNTS = build_accounts()
TRANSACTIONS = build_transactions(ACCOUNTS)
FILINGS = build_filings()

ACCOUNT_IDS = sorted(ACCOUNTS)


def risk_score(account: Account) -> dict:
    """A deliberately boring scoring function.

    Boring is the point. The book measures protocol overhead, and a scoring
    function with interesting performance characteristics of its own would
    contaminate every measurement downstream of it.
    """
    base = 42.0
    base += min(account.leverage, 6.0) * 7.4
    base -= min(account.years_trading, 25) * 0.9
    base += account.prior_defaults * 11.0
    base -= min(account.revenue_musd, 400.0) * 0.02
    score = max(1.0, min(99.0, base))
    band = ("low" if score < 35 else "moderate" if score < 55
            else "elevated" if score < 75 else "high")
    return {
        "accountId": account.account_id,
        "score": round(score, 1),
        "band": band,
        "drivers": [
            {"factor": "leverage", "contribution": round(min(account.leverage, 6.0) * 7.4, 1)},
            {"factor": "tenure", "contribution": round(-min(account.years_trading, 25) * 0.9, 1)},
            {"factor": "priorDefaults", "contribution": round(account.prior_defaults * 11.0, 1)},
            {"factor": "scale", "contribution": round(-min(account.revenue_musd, 400.0) * 0.02, 1)},
        ],
    }
