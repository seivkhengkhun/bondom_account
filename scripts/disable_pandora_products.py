"""Disable every Pandora-linked storefront product before catalog review.

This operation is idempotent and does not delete products, supplier mappings,
orders, inventory, or pricing settings.
"""

import asyncio
import sys
from pathlib import Path

from sqlalchemy import select, update

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from shared.database import AsyncSessionLocal, engine
from shared.models import Product, SupplierProduct


async def main() -> None:
    async with AsyncSessionLocal() as session:
        supplier_product_ids = select(SupplierProduct.product_id).where(
            SupplierProduct.supplier == "pandora"
        )
        result = await session.execute(
            update(Product)
            .where(Product.id.in_(supplier_product_ids))
            .values(is_active=False)
        )
        await session.commit()
        print(f"Disabled Pandora storefront products: {result.rowcount}")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
