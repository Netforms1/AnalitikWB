from __future__ import annotations

import asyncio
import logging
import random
from typing import Any

import aiohttp
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

log = logging.getLogger(__name__)

# WB периодически меняет версии путей. Пробуем по очереди, пока не ответит 200.
SELLER_CATALOG_URLS = [
    "https://catalog.wb.ru/sellers/v4/catalog",
    "https://catalog.wb.ru/sellers/v2/catalog",
    "https://catalog.wb.ru/sellers/catalog",
]
CARD_DETAIL_URLS = [
    "https://card.wb.ru/cards/v4/detail",
    "https://card.wb.ru/cards/v2/detail",
    "https://card.wb.ru/cards/v1/detail",
]
SELLER_INFO_URL = "https://www.wildberries.ru/webapi/seller/data/short/{supplier_id}"

WB_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Origin": "https://www.wildberries.ru",
    "Referer": "https://www.wildberries.ru/",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "cross-site",
    "Connection": "keep-alive",
}

STATS_API_BASE = "https://statistics-api.wildberries.ru"
STATS_SALES_URL = f"{STATS_API_BASE}/api/v1/supplier/sales"
STATS_ORDERS_URL = f"{STATS_API_BASE}/api/v1/supplier/orders"
STATS_STOCKS_URL = f"{STATS_API_BASE}/api/v1/supplier/stocks"


class WBApiError(RuntimeError):
    pass


class WBClient:
    """Асинхронный клиент к публичным эндпоинтам Wildberries."""

    def __init__(self, dest: int = -1257786, timeout: float = 20.0) -> None:
        self._dest = dest
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> "WBClient":
        self._session = aiohttp.ClientSession(
            timeout=self._timeout,
            headers=WB_BROWSER_HEADERS,
        )
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def _get_json(self, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        assert self._session is not None, "WBClient должен использоваться как async context manager"
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(4),
            wait=wait_exponential(multiplier=1, min=1, max=10),
            retry=retry_if_exception_type((aiohttp.ClientError, asyncio.TimeoutError)),
            reraise=True,
        ):
            with attempt:
                async with self._session.get(url, params=params) as resp:
                    if resp.status >= 500:
                        raise aiohttp.ClientError(f"WB {resp.status} on {url}")
                    if resp.status in (403, 404):
                        return {}
                    resp.raise_for_status()
                    return await resp.json(content_type=None)
        return {}

    async def _get_json_fallback(
        self, urls: list[str], params: dict[str, Any] | None = None
    ) -> tuple[dict[str, Any], str | None]:
        """Пробуем url'ы по очереди. Возвращаем (data, рабочий_url)."""
        last_status = 0
        for url in urls:
            try:
                data = await self._get_json(url, params=params)
            except aiohttp.ClientResponseError as exc:
                last_status = exc.status
                log.debug("WB %s on %s, пробую следующий", exc.status, url)
                continue
            if data:
                return data, url
        if last_status:
            log.warning("Все WB url'ы вернули ошибку, последний статус %s", last_status)
        return {}, None

    async def get_seller_info(self, supplier_id: int) -> dict[str, Any]:
        try:
            data = await self._get_json(SELLER_INFO_URL.format(supplier_id=supplier_id))
        except Exception as exc:  # noqa: BLE001
            log.warning("seller info failed: %s", exc)
            return {}
        return data or {}

    async def get_seller_products(self, supplier_id: int, limit: int = 30) -> list[dict[str, Any]]:
        """Топ товаров продавца по популярности."""
        products: list[dict[str, Any]] = []
        page = 1
        working_url: str | None = None
        while len(products) < limit and page <= 10:
            params = {
                "ab_testid": "false",
                "appType": 1,
                "curr": "rub",
                "dest": self._dest,
                "hide_dtype": 10,
                "lang": "ru",
                "sort": "popular",
                "spp": 30,
                "suppressSpellcheck": "false",
                "supplier": supplier_id,
                "page": page,
            }
            if working_url:
                data = await self._get_json(working_url, params=params)
            else:
                data, working_url = await self._get_json_fallback(
                    SELLER_CATALOG_URLS, params=params
                )
            batch = (((data or {}).get("data") or {}).get("products")) or []
            if not batch:
                break
            products.extend(batch)
            page += 1
        return products[:limit]

    async def get_cards_details(self, nm_ids: list[int]) -> list[dict[str, Any]]:
        if not nm_ids:
            return []
        out: list[dict[str, Any]] = []
        working_url: str | None = None
        for chunk_start in range(0, len(nm_ids), 100):
            chunk = nm_ids[chunk_start : chunk_start + 100]
            params = {
                "appType": 1,
                "curr": "rub",
                "dest": self._dest,
                "hide_dtype": 10,
                "lang": "ru",
                "spp": 30,
                "nm": ";".join(str(i) for i in chunk),
            }
            if working_url:
                data = await self._get_json(working_url, params=params)
            else:
                data, working_url = await self._get_json_fallback(
                    CARD_DETAIL_URLS, params=params
                )
            out.extend((((data or {}).get("data") or {}).get("products")) or [])
        return out

    async def get_reviews(self, imt_id: int, take: int = 30) -> list[dict[str, Any]]:
        """Отзывы по imtId. Сервер шардится на feedbacks1/2."""
        assert self._session is not None
        for host in ("feedbacks1.wb.ru", "feedbacks2.wb.ru"):
            url = f"https://{host}/feedbacks/v1/{imt_id}"
            try:
                async with self._session.get(url) as resp:
                    if resp.status != 200:
                        continue
                    data = await resp.json(content_type=None)
            except Exception as exc:  # noqa: BLE001
                log.debug("reviews host %s failed: %s", host, exc)
                continue
            feedbacks = (data or {}).get("feedbacks") or []
            if feedbacks:
                feedbacks.sort(key=lambda f: f.get("createdDate", ""), reverse=True)
                return feedbacks[:take]
        return []


class WBSellerStatsClient:
    """Клиент к WB Statistics API (продажи, заказы, остатки). Нужен JWT-токен."""

    def __init__(self, token: str, timeout: float = 60.0) -> None:
        if not token:
            raise WBApiError("WB_SUPPLIER_TOKEN пуст — Statistics API недоступен")
        self._token = token
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> "WBSellerStatsClient":
        self._session = aiohttp.ClientSession(
            timeout=self._timeout,
            headers={
                "Authorization": self._token,
                "User-Agent": WB_BROWSER_HEADERS["User-Agent"],
                "Accept": "application/json",
            },
        )
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def _get_json(self, url: str, params: dict[str, Any]) -> Any:
        assert self._session is not None
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(4),
            wait=wait_exponential(multiplier=1, min=2, max=20),
            retry=retry_if_exception_type((aiohttp.ClientError, asyncio.TimeoutError)),
            reraise=True,
        ):
            with attempt:
                async with self._session.get(url, params=params) as resp:
                    if resp.status == 401:
                        raise WBApiError(
                            "WB Statistics API: 401 — проверь WB_SUPPLIER_TOKEN"
                        )
                    if resp.status == 429:
                        await asyncio.sleep(20)
                        raise aiohttp.ClientError("WB 429 rate limit")
                    if resp.status >= 500:
                        raise aiohttp.ClientError(f"WB stats {resp.status}")
                    resp.raise_for_status()
                    return await resp.json(content_type=None)
        return None

    @staticmethod
    def _date_from(days_back: int) -> str:
        from datetime import datetime, timedelta, timezone

        dt = datetime.now(tz=timezone.utc) - timedelta(days=days_back)
        return dt.strftime("%Y-%m-%dT%H:%M:%S")

    async def get_sales(self, days_back: int = 30) -> list[dict[str, Any]]:
        params = {"dateFrom": self._date_from(days_back), "flag": 0}
        data = await self._get_json(STATS_SALES_URL, params=params)
        return data or []

    async def get_orders(self, days_back: int = 30) -> list[dict[str, Any]]:
        params = {"dateFrom": self._date_from(days_back), "flag": 0}
        data = await self._get_json(STATS_ORDERS_URL, params=params)
        return data or []

    async def get_stocks(self, days_back: int = 7) -> list[dict[str, Any]]:
        params = {"dateFrom": self._date_from(days_back)}
        data = await self._get_json(STATS_STOCKS_URL, params=params)
        return data or []


async def gather_with_concurrency(n: int, *coros):
    sem = asyncio.Semaphore(n)

    async def _wrap(coro):
        async with sem:
            await asyncio.sleep(random.uniform(0, 0.05))
            return await coro

    return await asyncio.gather(*(_wrap(c) for c in coros))
