"""Admin-controlled print config: print areas and prices per product."""
import re
from typing import Dict, List

from pydantic import BaseModel, Field, model_validator

import cache
from core import db

PRODUCTS = ("tee", "hoodie", "tote", "tee3d", "sleeveless", "sweatshirt", "croptop")
CURRENCIES = ("INR", "USD", "GBP", "EUR", "AED")
_APPAREL = ["XS", "S", "M", "L", "XL", "XXL"]
SIZES = {p: (["ONE SIZE"] if p == "tote" else _APPAREL) for p in PRODUCTS}
LABELS = {"tee": "TEE", "hoodie": "HOODIE", "tote": "TOTE", "tee3d": "3D TEE", "sleeveless": "SLEEVELESS", "sweatshirt": "SWEATSHIRT", "croptop": "CROP TOP", "catalog": "CATALOG"}


class PrintArea(BaseModel):
    x: float = Field(ge=0, le=0.9)
    y: float = Field(ge=0, le=0.9)
    w: float = Field(ge=0.1, le=1)
    h: float = Field(ge=0.1, le=1)

    @model_validator(mode="after")
    def fits(self):
        if self.x + self.w > 1.0001 or self.y + self.h > 1.0001:
            raise ValueError("Print area must stay inside the product")
        return self


_HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")


class ProductConfig(BaseModel):
    front: PrintArea
    back: PrintArea
    prices: Dict[str, float]
    colors: List[str] = []  # garment colors offered for THIS product (empty = filled with the defaults)
    sizes: List[str] = []  # sizes offered for THIS product

    @model_validator(mode="after")
    def clean_options(self):
        cols, seen = [], set()
        for c in self.colors:
            if not _HEX.match(c):
                raise ValueError(f"Bad color {c}")
            if c.upper() not in seen:
                seen.add(c.upper())
                cols.append(c.upper())
        sizes, seen = [], set()
        for z in self.sizes:
            z = z.strip().upper()
            if not z or len(z) > 12:
                raise ValueError("Sizes must be 1 to 12 characters")
            if z not in seen:
                seen.add(z)
                sizes.append(z)
        if len(sizes) > 12 or len(cols) > 24:
            raise ValueError("Too many sizes or colors")
        self.colors, self.sizes = cols, sizes
        return self

    @model_validator(mode="after")
    def all_prices(self):
        for c in CURRENCIES:
            p = self.prices.get(c)
            if p is None or not (1 <= p <= 1_000_000):
                raise ValueError(f"Price for {c} must be between 1 and 1,000,000")
        self.prices = {c: round(float(self.prices[c]), 2) for c in CURRENCIES}
        return self


class PrintConfig(BaseModel):
    products: Dict[str, ProductConfig]
    max_layers: int = Field(ge=1, le=20)
    max_scale: float = Field(ge=0.5, le=4)
    colors: List[str] = Field(min_length=1)

    @model_validator(mode="after")
    def all_products(self):
        if set(self.products) != set(PRODUCTS):
            raise ValueError("Config must include every product: " + ", ".join(PRODUCTS))
        for name, p in self.products.items():  # each product keeps its own colors and sizes; blanks take the defaults
            if not p.colors:
                p.colors = [c.upper() for c in self.colors]
            if not p.sizes:
                p.sizes = list(SIZES[name])
        return self


DEFAULT_CONFIG = PrintConfig(
    products={
        "tee": ProductConfig(
            front=PrintArea(x=0.3, y=0.24, w=0.4, h=0.46),
            back=PrintArea(x=0.28, y=0.18, w=0.44, h=0.56),
            prices={"INR": 799, "USD": 19, "GBP": 16, "EUR": 18, "AED": 70},
        ),
        "hoodie": ProductConfig(
            front=PrintArea(x=0.34, y=0.3, w=0.32, h=0.26),
            back=PrintArea(x=0.3, y=0.3, w=0.4, h=0.36),
            prices={"INR": 1799, "USD": 45, "GBP": 38, "EUR": 42, "AED": 165},
        ),
        "tote": ProductConfig(
            front=PrintArea(x=0.2, y=0.42, w=0.6, h=0.44),
            back=PrintArea(x=0.2, y=0.42, w=0.6, h=0.44),
            prices={"INR": 499, "USD": 14, "GBP": 12, "EUR": 13, "AED": 51},
        ),
        "tee3d": ProductConfig(
            front=PrintArea(x=0.3, y=0.22, w=0.4, h=0.36),
            back=PrintArea(x=0.28, y=0.18, w=0.44, h=0.44),
            prices={"INR": 899, "USD": 22, "GBP": 18, "EUR": 20, "AED": 81},
        ),
        "sleeveless": ProductConfig(
            front=PrintArea(x=0.2, y=0.26, w=0.6, h=0.36),
            back=PrintArea(x=0.2, y=0.2, w=0.6, h=0.44),
            prices={"INR": 699, "USD": 17, "GBP": 14, "EUR": 16, "AED": 62},
        ),
        "sweatshirt": ProductConfig(
            front=PrintArea(x=0.32, y=0.24, w=0.36, h=0.3),
            back=PrintArea(x=0.3, y=0.22, w=0.4, h=0.38),
            prices={"INR": 1599, "USD": 39, "GBP": 33, "EUR": 37, "AED": 143},
        ),
        "croptop": ProductConfig(
            front=PrintArea(x=0.28, y=0.3, w=0.44, h=0.4),
            back=PrintArea(x=0.26, y=0.26, w=0.48, h=0.46),
            prices={"INR": 699, "USD": 17, "GBP": 14, "EUR": 16, "AED": 62},
        ),
    },
    max_layers=6,
    max_scale=2.5,
    colors=["#FFFFFF", "#111111", "#C8102E", "#1F3A93", "#2E7D32", "#F2C94C", "#9E9E9E", "#FF4400", "#E8D5B7", "#6A1B9A"],
)


async def load_config() -> PrintConfig:
    hit = await cache.cache_get("print_config")
    if hit:
        return PrintConfig(**hit)
    cfg = await _load_config()
    await cache.cache_set("print_config", cfg.model_dump(), ttl=60)
    return cfg


async def _load_config() -> PrintConfig:
    doc = await db.settings.find_one({"key": "print_config"}, {"_id": 0, "key": 0})
    if not doc:
        return DEFAULT_CONFIG
    try:
        # Products added later get their defaults; the admin's saved settings for existing ones are kept.
        defaults = DEFAULT_CONFIG.model_dump()["products"]
        prods = {**defaults, **(doc.get("products") or {})}
        for k, v in prods.items():  # currencies added after the admin saved (e.g. AED) take their defaults
            v["prices"] = {**defaults.get(k, {}).get("prices", {}), **v.get("prices", {})}
        return PrintConfig(**{**doc, "products": {k: prods[k] for k in PRODUCTS}})
    except Exception:
        return DEFAULT_CONFIG  # older single-product format
