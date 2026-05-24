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

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

SELLER_CATALOG_URL = "https://catalog.wb.ru/sellers/v2/catalog"
CARD_DETAIL_URL = "https://card.wb.ru/cards/v2/detail"
SELLER_INFO_URL = "https://www.wildberries.ru/webapi/seller/data/short/{supplier_id}"


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
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
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
                    if resp.status == 404:
                        return {}
                    resp.raise_for_status()
                    return await resp.json(content_type=None)
        return {}

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
        while len(products) < limit and page <= 10:
            params = {
                "appType": 1,
                "curr": "rub",
                "dest": self._dest,
                "sort": "popular",
                "spp": 30,
                "supplier": supplier_id,
                "page": page,
            }
            data = await self._get_json(SELLER_CATALOG_URL, params=params)
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
        for chunk_start in range(0, len(nm_ids), 100):
            chunk = nm_ids[chunk_start : chunk_start + 100]
            params = {
                "appType": 1,
                "curr": "rub",
                "dest": self._dest,
                "spp": 30,
                "nm": ";".join(str(i) for i in chunk),
            }
            data = await self._get_json(CARD_DETAIL_URL, params=params)
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


async def gather_with_concurrency(n: int, *coros):
    sem = asyncio.Semaphore(n)

    async def _wrap(coro):
        async with sem:
            await asyncio.sleep(random.uniform(0, 0.05))
            return await coro

    return await asyncio.gather(*(_wrap(c) for c in coros))
