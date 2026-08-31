"""
Populates the `products` table with the concrete 10-item catalog from
LOW_LEVEL_DESIGN.md §11.4. Idempotent: skips SKUs that already exist.
"""

from app.db import SessionLocal, init_db
from app.models import Product

CATALOG = [
    # (sku, name, price INR, stock, category, description)
    ("SHOE-001", "Nike Revolution 6", 1899, 20, "footwear", "Lightweight running shoe"),
    ("SHOE-002", "Adidas Duramo SL", 2499, 15, "footwear", "Everyday road running shoe"),
    ("SHOE-003", "Puma Softride", 3299, 10, "footwear", "Cushioned running shoe"),
    ("APP-001", "Cotton Crew T-Shirt", 499, 50, "apparel", "100% cotton crew neck tee"),
    ("APP-002", "Slim Fit Chinos", 1299, 25, "apparel", "Classic slim fit chino"),
    ("ELEC-001", "Wireless Earbuds", 1999, 30, "electronics", "Bluetooth true wireless earbuds"),
    ("ELEC-002", "USB-C Fast Charger", 799, 40, "electronics", "65W GaN fast charger"),
    ("HOME-001", "Ceramic Coffee Mug", 349, 60, "home", "Handmade ceramic mug"),
    ("HOME-002", "Desk Lamp LED", 1199, 18, "home", "Adjustable LED desk lamp"),
    ("ACC-001", "Leather Wallet", 899, 22, "accessories", "Genuine leather bifold wallet"),
]


def seed() -> None:
    init_db()
    db = SessionLocal()
    try:
        existing = {p.sku for p in db.query(Product).all()}
        created = 0
        for sku, name, price, stock, category, description in CATALOG:
            if sku in existing:
                continue
            db.add(
                Product(
                    sku=sku,
                    name=name,
                    description=description,
                    price=float(price),
                    currency="INR",
                    stock=stock,
                    category=category,
                )
            )
            created += 1
        db.commit()
        print(f"Seeded {created} new products ({len(existing)} already present).")
    finally:
        db.close()


if __name__ == "__main__":
    seed()
