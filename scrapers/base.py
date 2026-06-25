from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class ScrapedProduct:
    external_id: str
    chain: str
    category_slug: str | None
    title: str
    price: float
    old_price: float | None
    discount_pct: float | None
    image_url: str | None
    url: str | None
    unit: str | None
    in_stock: bool
    promo_end_date: str | None


@dataclass
class ScrapedCategory:
    chain: str
    slug: str
    title: str
    parent_slug: str | None


@dataclass
class StoreInfo:
    id: str
    chain: str
    name: str
    city: str | None = None
    address: str | None = None
    lat: float | None = None
    lng: float | None = None


class BaseScraper(ABC):
    @abstractmethod
    def chain_name(self) -> str: ...

    @abstractmethod
    def scrape_categories(self) -> list[ScrapedCategory]: ...

    @abstractmethod
    def get_stores(self) -> list[StoreInfo]: ...

    @abstractmethod
    def scrape_products(self, store: StoreInfo) -> list[ScrapedProduct]: ...
