"""Deterministic catalog and business rules for the simulated recommendation workflow."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Product:
    sku: str
    name: str
    category: str
    price: float
    score: float  # deterministic "model" relevance score
    in_stock: bool = True


CATALOG: tuple[Product, ...] = (
    Product("E-100", "Noise-Cancelling Headphones", "electronics", 199.0, 0.93),
    Product("E-101", "Wireless Earbuds", "electronics", 89.0, 0.88),
    Product("E-102", "Bluetooth Speaker", "electronics", 59.0, 0.81),
    Product("E-103", "Smart Watch", "electronics", 249.0, 0.90),
    Product("E-104", "Phone Charger", "electronics", 19.0, 0.60),
    Product("B-200", "Distributed Systems Primer", "books", 45.0, 0.91),
    Product("B-201", "Observability Engineering", "books", 55.0, 0.95),
    Product("B-202", "Site Reliability Handbook", "books", 40.0, 0.89),
    Product("B-203", "Out-of-Print Classic", "books", 30.0, 0.99, in_stock=False),
    Product("H-300", "Espresso Machine", "home", 129.0, 0.87),
    Product("H-301", "Air Fryer", "home", 99.0, 0.92),
    Product("H-302", "Desk Lamp", "home", 35.0, 0.70),
)

CATEGORIES = frozenset(p.category for p in CATALOG)


def candidates(category: str, budget: float) -> list[Product]:
    """Catalog lookup: in-stock products in the category within budget (not yet ranked)."""
    return [p for p in CATALOG if p.category == category and p.in_stock and p.price <= budget]


def rank(items: list[Product]) -> list[Product]:
    """Serving-pipeline ranking: score descending, SKU ascending as the tie-breaker."""
    return sorted(items, key=lambda p: (-p.score, p.sku))


def expected_recommendation(category: str, budget: float) -> Optional[Product]:
    """Independent golden reference: the highest-scoring in-stock product within budget.

    Implemented separately from the serving pipeline on purpose, as a stand-in for ground
    truth (a rules engine, shadow model, or sampled human-labelled outcomes).
    """
    best: Optional[Product] = None
    for p in CATALOG:
        if p.category != category or not p.in_stock or p.price > budget:
            continue
        if best is None or p.score > best.score or (p.score == best.score and p.sku < best.sku):
            best = p
    return best
