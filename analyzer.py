from __future__ import annotations

import logging
import statistics
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from wb_api import WBClient, WBSellerStatsClient, gather_with_concurrency

log = logging.getLogger(__name__)


@dataclass
class ProductSummary:
    nm_id: int
    imt_id: int | None
    name: str
    brand: str
    category: str
    price: float
    sale_price: float
    rating: float
    feedbacks: int
    in_stock: int
    sizes: int
    promo_text: str | None = None

    @property
    def discount_pct(self) -> float:
        if self.price <= 0:
            return 0.0
        return round((1 - self.sale_price / self.price) * 100, 1)


@dataclass
class ReviewsSummary:
    total: int = 0
    avg_rating: float = 0.0
    pos_share: float = 0.0
    neg_share: float = 0.0
    top_negative_terms: list[tuple[str, int]] = field(default_factory=list)
    sample_positive: list[str] = field(default_factory=list)
    sample_negative: list[str] = field(default_factory=list)


@dataclass
class SalesSummary:
    period_days: int
    sales_count: int = 0
    returns_count: int = 0
    gross_revenue: float = 0.0
    net_payout: float = 0.0
    avg_check: float = 0.0
    returns_rate: float = 0.0
    orders_count: int = 0
    cancelled_orders: int = 0
    cancel_rate: float = 0.0
    conversion_orders_to_sales: float = 0.0
    daily_sales: list[dict[str, Any]] = field(default_factory=list)
    top_sku_by_revenue: list[dict[str, Any]] = field(default_factory=list)
    top_sku_by_units: list[dict[str, Any]] = field(default_factory=list)
    top_warehouses: list[tuple[str, int]] = field(default_factory=list)
    top_regions: list[tuple[str, int]] = field(default_factory=list)


@dataclass
class ShopReport:
    supplier_id: int
    seller_name: str
    seller_trademark: str
    seller_rating: float | None
    seller_sale_item_qty: int | None
    products: list[ProductSummary]
    reviews: ReviewsSummary
    categories: Counter
    price_stats: dict[str, float]
    rating_stats: dict[str, float]
    feedbacks_total: int
    in_stock_total: int
    out_of_stock_products: int
    sales: SalesSummary | None = None


_NEG_STOPWORDS = {
    "и", "в", "на", "не", "что", "это", "как", "у", "с", "по", "за", "к", "от", "для",
    "но", "а", "или", "же", "бы", "то", "вот", "там", "тут", "только", "уже", "ещё",
    "так", "тоже", "был", "была", "было", "были", "очень", "если", "при", "из", "до",
    "после", "его", "её", "их", "мне", "меня", "нам", "вам", "они", "мы", "вы", "он",
    "она", "оно", "о", "об", "будет", "есть", "нет", "да", "ну", "вообще", "когда",
    "потом", "пока", "также", "товар", "заказ", "вб", "озон", "wildberries",
}


def _extract_terms(texts: list[str], min_len: int = 4, top_k: int = 12) -> list[tuple[str, int]]:
    counter: Counter[str] = Counter()
    for text in texts:
        if not text:
            continue
        for raw in text.lower().split():
            token = "".join(ch for ch in raw if ch.isalpha())
            if len(token) < min_len or token in _NEG_STOPWORDS:
                continue
            counter[token] += 1
    return counter.most_common(top_k)


def _build_product_summary(card: dict[str, Any]) -> ProductSummary:
    sizes = card.get("sizes") or []
    stocks_qty = 0
    for size in sizes:
        for stock in size.get("stocks") or []:
            stocks_qty += int(stock.get("qty") or 0)
    price = 0.0
    sale_price = 0.0
    if sizes:
        first_price = (sizes[0].get("price") or {})
        price = (first_price.get("basic") or first_price.get("total") or 0) / 100
        sale_price = (first_price.get("total") or first_price.get("product") or 0) / 100
    return ProductSummary(
        nm_id=int(card.get("id") or 0),
        imt_id=int(card.get("root") or 0) or None,
        name=str(card.get("name") or ""),
        brand=str(card.get("brand") or ""),
        category=str(card.get("entity") or card.get("subjectName") or ""),
        price=round(price, 2),
        sale_price=round(sale_price, 2),
        rating=float(card.get("reviewRating") or card.get("rating") or 0.0),
        feedbacks=int(card.get("feedbacks") or 0),
        in_stock=stocks_qty,
        sizes=len(sizes),
        promo_text=(card.get("promoTextCard") or card.get("promoTextCat") or None),
    )


def _aggregate_reviews(all_reviews: list[dict[str, Any]]) -> ReviewsSummary:
    if not all_reviews:
        return ReviewsSummary()
    ratings = [int(r.get("productValuation") or 0) for r in all_reviews if r.get("productValuation")]
    pos_texts: list[str] = []
    neg_texts: list[str] = []
    for r in all_reviews:
        text = (r.get("text") or "").strip()
        cons = (r.get("cons") or "").strip()
        pros = (r.get("pros") or "").strip()
        full = " ".join(t for t in (text, pros, cons) if t)
        val = int(r.get("productValuation") or 0)
        if val >= 4:
            pos_texts.append(full)
        elif 0 < val <= 3:
            neg_texts.append(" ".join(t for t in (text, cons) if t))
    total = len(all_reviews)
    pos = sum(1 for x in ratings if x >= 4)
    neg = sum(1 for x in ratings if 0 < x <= 3)
    return ReviewsSummary(
        total=total,
        avg_rating=round(statistics.fmean(ratings), 2) if ratings else 0.0,
        pos_share=round(pos / total, 2) if total else 0.0,
        neg_share=round(neg / total, 2) if total else 0.0,
        top_negative_terms=_extract_terms(neg_texts),
        sample_positive=[t[:280] for t in pos_texts[:5] if t],
        sample_negative=[t[:280] for t in neg_texts[:8] if t],
    )


def aggregate_sales(
    sales: list[dict[str, Any]],
    orders: list[dict[str, Any]],
    period_days: int,
) -> SalesSummary:
    summary = SalesSummary(period_days=period_days)
    revenue_by_sku: Counter[int] = Counter()
    units_by_sku: Counter[int] = Counter()
    sku_names: dict[int, str] = {}
    warehouses: Counter[str] = Counter()
    regions: Counter[str] = Counter()
    daily: dict[str, dict[str, float]] = {}

    for s in sales:
        sale_id = str(s.get("saleID") or "")
        for_pay = float(s.get("forPay") or 0)
        price_with_disc = float(s.get("priceWithDisc") or s.get("finishedPrice") or 0)
        nm_id = int(s.get("nmId") or 0)
        is_return = sale_id.startswith("R") or bool(s.get("IsStorno"))
        date = (s.get("date") or "")[:10]
        if is_return:
            summary.returns_count += 1
            summary.gross_revenue -= price_with_disc
            summary.net_payout -= for_pay
            if nm_id:
                revenue_by_sku[nm_id] -= price_with_disc
                units_by_sku[nm_id] -= 1
        else:
            summary.sales_count += 1
            summary.gross_revenue += price_with_disc
            summary.net_payout += for_pay
            if nm_id:
                revenue_by_sku[nm_id] += price_with_disc
                units_by_sku[nm_id] += 1
            wh = s.get("warehouseName")
            if wh:
                warehouses[str(wh)] += 1
            region = s.get("regionName") or s.get("oblastOkrugName")
            if region:
                regions[str(region)] += 1
        if nm_id and nm_id not in sku_names:
            name = s.get("subject") or s.get("supplierArticle") or ""
            if name:
                sku_names[nm_id] = str(name)
        if date and not is_return:
            bucket = daily.setdefault(date, {"sales": 0, "revenue": 0.0})
            bucket["sales"] += 1
            bucket["revenue"] += price_with_disc

    for o in orders:
        summary.orders_count += 1
        if o.get("isCancel"):
            summary.cancelled_orders += 1

    total_sales = summary.sales_count + summary.returns_count
    summary.returns_rate = (
        round(summary.returns_count / total_sales, 3) if total_sales else 0.0
    )
    summary.cancel_rate = (
        round(summary.cancelled_orders / summary.orders_count, 3)
        if summary.orders_count
        else 0.0
    )
    summary.conversion_orders_to_sales = (
        round(summary.sales_count / summary.orders_count, 3)
        if summary.orders_count
        else 0.0
    )
    summary.avg_check = (
        round(summary.gross_revenue / summary.sales_count, 2)
        if summary.sales_count
        else 0.0
    )
    summary.gross_revenue = round(summary.gross_revenue, 2)
    summary.net_payout = round(summary.net_payout, 2)

    summary.top_sku_by_revenue = [
        {
            "nm_id": nm,
            "name": sku_names.get(nm, ""),
            "revenue_rub": round(rev, 2),
            "units": units_by_sku.get(nm, 0),
        }
        for nm, rev in revenue_by_sku.most_common(10)
    ]
    summary.top_sku_by_units = [
        {
            "nm_id": nm,
            "name": sku_names.get(nm, ""),
            "units": units,
            "revenue_rub": round(revenue_by_sku.get(nm, 0.0), 2),
        }
        for nm, units in units_by_sku.most_common(10)
    ]
    summary.top_warehouses = warehouses.most_common(5)
    summary.top_regions = regions.most_common(5)
    summary.daily_sales = [
        {
            "date": d,
            "sales": int(v["sales"]),
            "revenue_rub": round(v["revenue"], 2),
        }
        for d, v in sorted(daily.items())
    ]
    return summary


async def fetch_sales_summary(
    token: str, days: int
) -> SalesSummary | None:
    try:
        async with WBSellerStatsClient(token) as client:
            sales = await client.get_sales(days_back=days)
            orders = await client.get_orders(days_back=days)
    except Exception as exc:  # noqa: BLE001
        log.warning("sales fetch failed: %s", exc)
        return None
    return aggregate_sales(sales or [], orders or [], period_days=days)


async def analyze_shop(
    client: WBClient,
    supplier_id: int,
    top_products: int,
    reviews_per_product: int,
    sales_token: str | None = None,
    own_supplier_id: int | None = None,
    stats_days: int = 30,
) -> ShopReport:
    seller_info_task = client.get_seller_info(supplier_id)
    products_raw = await client.get_seller_products(supplier_id, limit=top_products)
    if not products_raw:
        seller_info = await seller_info_task
        return ShopReport(
            supplier_id=supplier_id,
            seller_name=str(seller_info.get("name") or ""),
            seller_trademark=str(seller_info.get("trademark") or ""),
            seller_rating=seller_info.get("valuation"),
            seller_sale_item_qty=seller_info.get("saleItemQuantity"),
            products=[],
            reviews=ReviewsSummary(),
            categories=Counter(),
            price_stats={},
            rating_stats={},
            feedbacks_total=0,
            in_stock_total=0,
            out_of_stock_products=0,
        )

    nm_ids = [int(p["id"]) for p in products_raw if p.get("id")]
    cards = await client.get_cards_details(nm_ids)
    products = [_build_product_summary(c) for c in cards]

    imt_ids = list({p.imt_id for p in products if p.imt_id})[:top_products]
    review_results = await gather_with_concurrency(
        6, *(client.get_reviews(imt, take=reviews_per_product) for imt in imt_ids)
    )
    all_reviews: list[dict[str, Any]] = [item for batch in review_results for item in batch]

    sale_prices = [p.sale_price for p in products if p.sale_price > 0]
    ratings = [p.rating for p in products if p.rating > 0]
    price_stats = {
        "min": round(min(sale_prices), 2) if sale_prices else 0.0,
        "max": round(max(sale_prices), 2) if sale_prices else 0.0,
        "avg": round(statistics.fmean(sale_prices), 2) if sale_prices else 0.0,
        "median": round(statistics.median(sale_prices), 2) if sale_prices else 0.0,
    }
    rating_stats = {
        "avg": round(statistics.fmean(ratings), 2) if ratings else 0.0,
        "min": round(min(ratings), 2) if ratings else 0.0,
        "max": round(max(ratings), 2) if ratings else 0.0,
    }

    seller_info = await seller_info_task
    sales_summary: SalesSummary | None = None
    if sales_token and own_supplier_id and own_supplier_id == supplier_id:
        sales_summary = await fetch_sales_summary(sales_token, stats_days)
    return ShopReport(
        supplier_id=supplier_id,
        seller_name=str(seller_info.get("name") or ""),
        seller_trademark=str(seller_info.get("trademark") or ""),
        seller_rating=seller_info.get("valuation"),
        seller_sale_item_qty=seller_info.get("saleItemQuantity"),
        products=products,
        reviews=_aggregate_reviews(all_reviews),
        categories=Counter(p.category for p in products if p.category),
        price_stats=price_stats,
        rating_stats=rating_stats,
        feedbacks_total=sum(p.feedbacks for p in products),
        in_stock_total=sum(p.in_stock for p in products),
        out_of_stock_products=sum(1 for p in products if p.in_stock == 0),
        sales=sales_summary,
    )


def sales_summary_to_dict(s: SalesSummary) -> dict[str, Any]:
    return {
        "period_days": s.period_days,
        "sales_count": s.sales_count,
        "returns_count": s.returns_count,
        "returns_rate": s.returns_rate,
        "orders_count": s.orders_count,
        "cancelled_orders": s.cancelled_orders,
        "cancel_rate": s.cancel_rate,
        "conversion_orders_to_sales": s.conversion_orders_to_sales,
        "gross_revenue_rub": s.gross_revenue,
        "net_payout_rub": s.net_payout,
        "avg_check_rub": s.avg_check,
        "top_sku_by_revenue": s.top_sku_by_revenue,
        "top_sku_by_units": s.top_sku_by_units,
        "top_warehouses": s.top_warehouses,
        "top_regions": s.top_regions,
        "daily_sales": s.daily_sales,
    }


def report_to_prompt_dict(report: ShopReport) -> dict[str, Any]:
    """Сводим отчёт в компактный dict для отправки в GPT."""
    top_products = sorted(report.products, key=lambda p: p.feedbacks, reverse=True)[:15]
    return {
        "supplier_id": report.supplier_id,
        "seller": {
            "name": report.seller_name,
            "trademark": report.seller_trademark,
            "rating": report.seller_rating,
            "items_sold_qty": report.seller_sale_item_qty,
        },
        "totals": {
            "products_analyzed": len(report.products),
            "feedbacks_total": report.feedbacks_total,
            "in_stock_total_units": report.in_stock_total,
            "out_of_stock_products": report.out_of_stock_products,
        },
        "categories_top": report.categories.most_common(10),
        "price_stats_rub": report.price_stats,
        "rating_stats": report.rating_stats,
        "reviews": {
            "total_collected": report.reviews.total,
            "avg_rating": report.reviews.avg_rating,
            "positive_share": report.reviews.pos_share,
            "negative_share": report.reviews.neg_share,
            "top_negative_terms": report.reviews.top_negative_terms,
            "sample_positive": report.reviews.sample_positive,
            "sample_negative": report.reviews.sample_negative,
        },
        "top_products": [
            {
                "nm_id": p.nm_id,
                "name": p.name,
                "brand": p.brand,
                "category": p.category,
                "price_rub": p.price,
                "sale_price_rub": p.sale_price,
                "discount_pct": p.discount_pct,
                "rating": p.rating,
                "feedbacks": p.feedbacks,
                "in_stock_units": p.in_stock,
                "sizes": p.sizes,
                "promo": p.promo_text,
            }
            for p in top_products
        ],
        "sales": sales_summary_to_dict(report.sales) if report.sales else None,
    }
