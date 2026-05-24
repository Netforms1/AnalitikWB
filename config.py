from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    telegram_token: str
    openai_api_key: str
    openai_model: str
    wb_top_products: int
    wb_reviews_per_product: int
    wb_dest: int
    log_level: str


def load_settings() -> Settings:
    telegram_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    openai_api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not telegram_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN не задан в .env")
    if not openai_api_key:
        raise RuntimeError("OPENAI_API_KEY не задан в .env")
    return Settings(
        telegram_token=telegram_token,
        openai_api_key=openai_api_key,
        openai_model=os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip(),
        wb_top_products=_int("WB_TOP_PRODUCTS", 30),
        wb_reviews_per_product=_int("WB_REVIEWS_PER_PRODUCT", 30),
        wb_dest=_int("WB_DEST", -1257786),
        log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper(),
    )


BRAND_CONTEXT = (
    "Анализ выполняется в интересах бренда PlatSer Group — российский производитель "
    "и продавец кормов и товаров для животных. Стратегическая цель — стать брендом "
    "№1 в России в категории зоотоваров. Все выводы и рекомендации должны учитывать "
    "этот контекст: позиционирование, конкуренция с лидерами категории, "
    "потенциал масштабирования, удержание клиентов и репутация бренда."
)
