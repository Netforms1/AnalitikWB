from __future__ import annotations

import asyncio
import logging
import statistics
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from wb_api import (
    WBAnalyticsClient,
    WBClient,
    WBCommonClient,
    WBContentClient,
    WBSellerStatsClient,
    gather_with_concurrency,
)

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
class ContentQuality:
    cards_total: int = 0
    avg_photos: float = 0.0
    cards_with_video: int = 0
    cards_with_video_share: float = 0.0
    avg_description_len: int = 0
    short_descriptions: int = 0
    cards_missing_chars: int = 0
    avg_characteristics: float = 0.0
    weak_cards: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class FunnelStats:
    period_days: int
    opens: int = 0
    add_to_cart: int = 0
    orders: int = 0
    buyouts: int = 0
    orders_sum: float = 0.0
    buyouts_sum: float = 0.0
    cr_card_to_cart: float = 0.0
    cr_cart_to_order: float = 0.0
    cr_order_to_buyout: float = 0.0
    cr_card_to_order: float = 0.0
    top_funnel_sku: list[dict[str, Any]] = field(default_factory=list)
    weak_funnel_sku: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class MarginEstimate:
    avg_commission_pct: float = 0.0
    by_subject: list[dict[str, Any]] = field(default_factory=list)
    box_logistics_base_rub: float | None = None
    box_logistics_per_liter_rub: float | None = None
    note: str = ""


@dataclass
class StocksSummary:
    total_units: int = 0
    warehouses: list[tuple[str, int]] = field(default_factory=list)
    zero_stock_skus: int = 0
    low_stock_skus: int = 0
    days_of_supply: float | None = None


@dataclass
class QuestionsSummary:
    total: int = 0
    unanswered: int = 0
    answer_rate: float = 0.0
    sample: list[str] = field(default_factory=list)


@dataclass
class ShopReport:
    supplier_id: str
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
    content: ContentQuality | None = None
    funnel: FunnelStats | None = None
    margin: MarginEstimate | None = None
    stocks: StocksSummary | None = None
    questions: QuestionsSummary | None = None


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


def aggregate_content(cards: list[dict[str, Any]]) -> ContentQuality:
    if not cards:
        return ContentQuality()
    photos: list[int] = []
    descs: list[int] = []
    chars: list[int] = []
    with_video = 0
    short = 0
    missing_chars = 0
    weak: list[dict[str, Any]] = []
    for c in cards:
        photo_count = len(c.get("photos") or [])
        photos.append(photo_count)
        if c.get("video"):
            with_video += 1
        desc = (c.get("description") or "").strip()
        descs.append(len(desc))
        if len(desc) < 500:
            short += 1
        ch_count = len(c.get("characteristics") or [])
        chars.append(ch_count)
        if ch_count < 5:
            missing_chars += 1
        if photo_count < 3 or len(desc) < 500 or ch_count < 5 or not c.get("video"):
            weak.append(
                {
                    "nm_id": c.get("nmID"),
                    "title": (c.get("title") or "")[:80],
                    "photos": photo_count,
                    "video": bool(c.get("video")),
                    "desc_len": len(desc),
                    "chars": ch_count,
                }
            )
    n = len(cards)
    return ContentQuality(
        cards_total=n,
        avg_photos=round(statistics.fmean(photos), 1),
        cards_with_video=with_video,
        cards_with_video_share=round(with_video / n, 2),
        avg_description_len=int(statistics.fmean(descs)),
        short_descriptions=short,
        cards_missing_chars=missing_chars,
        avg_characteristics=round(statistics.fmean(chars), 1),
        weak_cards=weak[:15],
    )


def aggregate_funnel(cards: list[dict[str, Any]], days: int) -> FunnelStats:
    if not cards:
        return FunnelStats(period_days=days)
    opens = adds = orders = buyouts = 0
    orders_sum = buyouts_sum = 0.0
    detailed: list[dict[str, Any]] = []
    for c in cards:
        stat = ((c.get("statistics") or {}).get("selectedPeriod")) or {}
        if not stat:
            continue
        o = int(stat.get("openCardCount") or 0)
        a = int(stat.get("addToCartCount") or 0)
        ord_ = int(stat.get("ordersCount") or 0)
        b = int(stat.get("buyoutsCount") or 0)
        opens += o
        adds += a
        orders += ord_
        buyouts += b
        orders_sum += float(stat.get("ordersSumRub") or 0)
        buyouts_sum += float(stat.get("buyoutsSumRub") or 0)
        detailed.append(
            {
                "nm_id": c.get("nmID"),
                "name": (c.get("vendorCode") or c.get("brandName") or "")[:60],
                "opens": o,
                "carts": a,
                "orders": ord_,
                "buyouts": b,
                "cr_card_to_cart": round(a / o, 3) if o else 0.0,
                "cr_order_to_buyout": round(b / ord_, 3) if ord_ else 0.0,
                "orders_sum_rub": round(float(stat.get("ordersSumRub") or 0), 2),
            }
        )

    def _div(num: float, den: float) -> float:
        return round(num / den, 3) if den else 0.0

    detailed.sort(key=lambda x: x["orders_sum_rub"], reverse=True)
    weak = sorted(
        [d for d in detailed if d["opens"] >= 100],
        key=lambda d: d["cr_card_to_cart"],
    )[:10]
    return FunnelStats(
        period_days=days,
        opens=opens,
        add_to_cart=adds,
        orders=orders,
        buyouts=buyouts,
        orders_sum=round(orders_sum, 2),
        buyouts_sum=round(buyouts_sum, 2),
        cr_card_to_cart=_div(adds, opens),
        cr_cart_to_order=_div(orders, adds),
        cr_order_to_buyout=_div(buyouts, orders),
        cr_card_to_order=_div(orders, opens),
        top_funnel_sku=detailed[:10],
        weak_funnel_sku=weak,
    )


def aggregate_margin(
    commissions: list[dict[str, Any]],
    box_tariffs: dict[str, Any],
    product_subjects: list[str],
) -> MarginEstimate:
    if not commissions:
        return MarginEstimate(note="Нет данных по комиссиям WB")
    relevant = []
    subj_set = {s.lower() for s in product_subjects if s}
    rows = []
    for c in commissions:
        subj = str(c.get("subjectName") or c.get("subject") or "").lower()
        comm = c.get("kgvpMarketplace") or c.get("paidStorageKgvp") or 0
        try:
            comm_pct = float(comm)
        except (TypeError, ValueError):
            continue
        if not subj_set or subj in subj_set or any(s in subj for s in subj_set):
            relevant.append(comm_pct)
            rows.append({"subject": subj, "commission_pct": comm_pct})
    avg = round(statistics.fmean(relevant), 2) if relevant else 0.0
    base = None
    per_l = None
    try:
        warehouse_list = box_tariffs.get("warehouseList") or []
        if warehouse_list:
            base_vals = [
                float(w.get("boxDeliveryBase") or w.get("boxDeliveryAndStorageExpr") or 0)
                for w in warehouse_list
            ]
            liter_vals = [
                float(w.get("boxDeliveryLiter") or 0) for w in warehouse_list
            ]
            base = round(statistics.fmean([v for v in base_vals if v]), 2) if any(base_vals) else None
            per_l = round(statistics.fmean([v for v in liter_vals if v]), 2) if any(liter_vals) else None
    except Exception as exc:  # noqa: BLE001
        log.debug("box tariffs parse failed: %s", exc)
    return MarginEstimate(
        avg_commission_pct=avg,
        by_subject=rows[:15],
        box_logistics_base_rub=base,
        box_logistics_per_liter_rub=per_l,
    )


def aggregate_stocks(
    stocks: list[dict[str, Any]],
    avg_daily_sales: float | None = None,
) -> StocksSummary:
    if not stocks:
        return StocksSummary()
    by_wh: Counter[str] = Counter()
    by_sku: Counter[int] = Counter()
    total = 0
    for s in stocks:
        qty = int(s.get("quantity") or 0)
        total += qty
        wh = s.get("warehouseName") or ""
        if wh:
            by_wh[str(wh)] += qty
        nm = int(s.get("nmId") or 0)
        if nm:
            by_sku[nm] += qty
    zero = sum(1 for v in by_sku.values() if v == 0)
    low = sum(1 for v in by_sku.values() if 0 < v <= 5)
    days_of_supply = (
        round(total / avg_daily_sales, 1)
        if avg_daily_sales and avg_daily_sales > 0
        else None
    )
    return StocksSummary(
        total_units=total,
        warehouses=by_wh.most_common(8),
        zero_stock_skus=zero,
        low_stock_skus=low,
        days_of_supply=days_of_supply,
    )


def aggregate_questions(items: list[dict[str, Any]]) -> QuestionsSummary:
    if not items:
        return QuestionsSummary()
    total = len(items)
    answered = sum(1 for q in items if (q.get("answer") or {}).get("text"))
    unanswered = total - answered
    sample = [
        (q.get("text") or "").strip()[:240]
        for q in items[:8]
        if (q.get("text") or "").strip()
    ]
    return QuestionsSummary(
        total=total,
        unanswered=unanswered,
        answer_rate=round(answered / total, 2) if total else 0.0,
        sample=sample,
    )


async def fetch_content_quality(
    token: str, limit: int = 100
) -> ContentQuality | None:
    try:
        async with WBContentClient(token) as client:
            cards = await client.get_cards(limit=limit)
    except Exception as exc:  # noqa: BLE001
        log.warning("content fetch failed: %s", exc)
        return None
    return aggregate_content(cards)


async def fetch_funnel(
    token: str, days: int = 30
) -> FunnelStats | None:
    try:
        async with WBAnalyticsClient(token) as client:
            cards = await client.get_nm_funnel(days_back=days)
    except Exception as exc:  # noqa: BLE001
        log.warning("funnel fetch failed: %s", exc)
        return None
    return aggregate_funnel(cards, days)


async def fetch_margin(
    token: str, product_subjects: list[str]
) -> MarginEstimate | None:
    try:
        async with WBCommonClient(token) as client:
            commissions = await client.get_commissions()
            try:
                box = await client.get_box_tariffs()
            except Exception:  # noqa: BLE001
                box = {}
    except Exception as exc:  # noqa: BLE001
        log.warning("margin fetch failed: %s", exc)
        return None
    return aggregate_margin(commissions, box, product_subjects)


async def fetch_stocks_summary(
    token: str, avg_daily_sales: float | None = None
) -> StocksSummary | None:
    try:
        async with WBSellerStatsClient(token) as client:
            stocks = await client.get_stocks(days_back=1)
    except Exception as exc:  # noqa: BLE001
        log.warning("stocks fetch failed: %s", exc)
        return None
    return aggregate_stocks(stocks or [], avg_daily_sales)


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
    top_products: int,
    reviews_per_product: int,
    wb_token: str,
    stats_days: int = 30,
    supplier_display: str = "",
    seller_name: str = "",
    seller_trademark: str = "",
) -> ShopReport:
    """Полный разбор магазина по токену WB. supplier_id для отображения только.

    SKU магазина берём из Content API (нужен скоуп Контент). Цены/рейтинги/
    отзывы — публичные card.wb.ru / feedbacks.wb.ru по nmID/imtID.
    """
    # 1) Свои карточки из Content API
    own_cards: list[dict[str, Any]] = []
    try:
        async with WBContentClient(wb_token) as content_client:
            own_cards = await content_client.get_cards(limit=top_products)
    except Exception as exc:  # noqa: BLE001
        log.warning("content cards fetch failed: %s", exc)

    nm_ids: list[int] = []
    for c in own_cards:
        nm = c.get("nmID") or c.get("nmId") or c.get("id")
        try:
            if nm:
                nm_ids.append(int(nm))
        except (TypeError, ValueError):
            continue

    # 2) Публичные детали по nmID (цена, рейтинг, feedbacks, остатки на витрине)
    public_cards: list[dict[str, Any]] = []
    if nm_ids:
        try:
            public_cards = await client.get_cards_details(nm_ids[:top_products])
        except Exception as exc:  # noqa: BLE001
            log.warning("public cards fetch failed: %s", exc)
    products = [_build_product_summary(c) for c in public_cards]

    imt_ids = list({p.imt_id for p in products if p.imt_id})[:top_products]

    # 3) Отзывы и вопросы (публично, по imtID)
    all_reviews: list[dict[str, Any]] = []
    questions_summary: QuestionsSummary | None = None
    if imt_ids:
        try:
            review_results = await gather_with_concurrency(
                6, *(client.get_reviews(imt, take=reviews_per_product) for imt in imt_ids)
            )
            all_reviews = [item for batch in review_results for item in batch]
        except Exception as exc:  # noqa: BLE001
            log.warning("reviews fetch failed: %s", exc)
        try:
            q_results = await gather_with_concurrency(
                4, *(client.get_questions(imt) for imt in imt_ids[:15])
            )
            all_questions = [q for batch in q_results for q in batch]
            questions_summary = aggregate_questions(all_questions)
        except Exception as exc:  # noqa: BLE001
            log.warning("questions fetch failed: %s", exc)

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

    # 4) Авторизованные API параллельно
    product_subjects: list[str] = []
    seen: set[str] = set()
    for c in own_cards:
        s = str(c.get("subjectName") or c.get("subject") or "").strip()
        if s and s not in seen:
            seen.add(s)
            product_subjects.append(s)
    for p in products:
        if p.category and p.category not in seen:
            seen.add(p.category)
            product_subjects.append(p.category)

    # Если имя/бренд магазина не передали — пробуем взять самый частый
    # бренд из своих карточек.
    if not seller_trademark and own_cards:
        brands = Counter(
            str(c.get("brand") or "").strip() for c in own_cards if c.get("brand")
        )
        if brands:
            seller_trademark = brands.most_common(1)[0][0]

    content_summary = aggregate_content(own_cards) if own_cards else None
    (
        sales_summary,
        funnel_summary,
        margin_summary,
    ) = await asyncio.gather(
        fetch_sales_summary(wb_token, stats_days),
        fetch_funnel(wb_token, days=stats_days),
        fetch_margin(wb_token, product_subjects),
    )
    avg_daily = (
        (sales_summary.sales_count / sales_summary.period_days)
        if sales_summary and sales_summary.period_days
        else None
    )
    stocks_summary = await fetch_stocks_summary(wb_token, avg_daily)

    return ShopReport(
        supplier_id=str(supplier_display or ""),
        seller_name=seller_name,
        seller_trademark=seller_trademark,
        seller_rating=None,
        seller_sale_item_qty=None,
        products=products,
        reviews=_aggregate_reviews(all_reviews),
        categories=Counter(p.category for p in products if p.category),
        price_stats=price_stats,
        rating_stats=rating_stats,
        feedbacks_total=sum(p.feedbacks for p in products),
        in_stock_total=sum(p.in_stock for p in products),
        out_of_stock_products=sum(1 for p in products if p.in_stock == 0),
        sales=sales_summary,
        content=content_summary,
        funnel=funnel_summary,
        margin=margin_summary,
        stocks=stocks_summary,
        questions=questions_summary,
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
        "content_quality": (
            {
                "cards_total": report.content.cards_total,
                "avg_photos": report.content.avg_photos,
                "cards_with_video": report.content.cards_with_video,
                "cards_with_video_share": report.content.cards_with_video_share,
                "avg_description_len": report.content.avg_description_len,
                "short_descriptions": report.content.short_descriptions,
                "cards_missing_chars": report.content.cards_missing_chars,
                "avg_characteristics": report.content.avg_characteristics,
                "weak_cards": report.content.weak_cards,
            }
            if report.content
            else None
        ),
        "funnel": (
            {
                "period_days": report.funnel.period_days,
                "opens": report.funnel.opens,
                "add_to_cart": report.funnel.add_to_cart,
                "orders": report.funnel.orders,
                "buyouts": report.funnel.buyouts,
                "orders_sum_rub": report.funnel.orders_sum,
                "buyouts_sum_rub": report.funnel.buyouts_sum,
                "cr_card_to_cart": report.funnel.cr_card_to_cart,
                "cr_cart_to_order": report.funnel.cr_cart_to_order,
                "cr_order_to_buyout": report.funnel.cr_order_to_buyout,
                "cr_card_to_order": report.funnel.cr_card_to_order,
                "top_funnel_sku": report.funnel.top_funnel_sku,
                "weak_funnel_sku": report.funnel.weak_funnel_sku,
            }
            if report.funnel
            else None
        ),
        "margin": (
            {
                "avg_commission_pct": report.margin.avg_commission_pct,
                "by_subject": report.margin.by_subject,
                "box_logistics_base_rub": report.margin.box_logistics_base_rub,
                "box_logistics_per_liter_rub": report.margin.box_logistics_per_liter_rub,
                "note": report.margin.note,
            }
            if report.margin
            else None
        ),
        "stocks": (
            {
                "total_units": report.stocks.total_units,
                "warehouses": report.stocks.warehouses,
                "zero_stock_skus": report.stocks.zero_stock_skus,
                "low_stock_skus": report.stocks.low_stock_skus,
                "days_of_supply": report.stocks.days_of_supply,
            }
            if report.stocks
            else None
        ),
        "questions": (
            {
                "total": report.questions.total,
                "unanswered": report.questions.unanswered,
                "answer_rate": report.questions.answer_rate,
                "sample": report.questions.sample,
            }
            if report.questions
            else None
        ),
    }
