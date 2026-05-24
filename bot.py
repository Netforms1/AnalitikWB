from __future__ import annotations

import asyncio
import html
import logging
from typing import Any

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Message

from analyzer import (
    analyze_shop,
    fetch_sales_summary,
    report_to_prompt_dict,
    sales_summary_to_dict,
)
from config import load_settings
from gpt import GPTAnalyzer
from keyboards import (
    after_report,
    analysis_modes,
    back_to_menu,
    cancel_input,
    main_menu,
    sales_periods,
)
from wb_api import WBClient

log = logging.getLogger(__name__)

settings = load_settings()
logging.basicConfig(
    level=getattr(logging, settings.log_level, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

bot = Bot(
    token=settings.telegram_token,
    default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN),
)
dp = Dispatcher(storage=MemoryStorage())
gpt = GPTAnalyzer(api_key=settings.openai_api_key, model=settings.openai_model)


class Flow(StatesGroup):
    waiting_supplier_id = State()


def _has_own_shop() -> bool:
    return bool(settings.wb_supplier_token and settings.wb_own_supplier_id)


WELCOME = (
    "👋 *Привет!* Я бот аналитики *PlatSer Group*.\n\n"
    "Я разбираю любой магазин Wildberries и выдаю заключение через ChatGPT:\n"
    "• 📈 спрос и потенциал\n"
    "• 💰 ценовая политика (можно ли поднять цену)\n"
    "• 📦 ассортимент и слабые карточки\n"
    "• ⭐ боли клиентов из отзывов\n"
    "• 🎯 рекомендации под цель бренда №1 в зоотоварах РФ\n\n"
    "Выбирай действие 👇"
)

ABOUT = (
    "ℹ️ *AnalitikWB — PlatSer Group*\n\n"
    "Бот собирает публичные данные по продавцу на Wildberries "
    "(карточки, цены, остатки, отзывы) и просит ChatGPT составить "
    "развёрнутый отчёт со стратегическими выводами под бренд *PlatSer Group* — "
    "корма и товары для животных, цель: №1 в России.\n\n"
    "Нужен ID продавца WB (supplier_id). Найти его можно в URL магазина: "
    "`wildberries.ru/seller/<ID>`."
)


@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(WELCOME, reply_markup=main_menu(has_own_shop=_has_own_shop()))


@dp.callback_query(F.data == "menu:home")
async def cb_home(query: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await query.message.edit_text(WELCOME, reply_markup=main_menu(has_own_shop=_has_own_shop()))
    await query.answer()


@dp.callback_query(F.data == "menu:about")
async def cb_about(query: CallbackQuery) -> None:
    await query.message.edit_text(ABOUT, reply_markup=back_to_menu())
    await query.answer()


@dp.callback_query(F.data.in_({"menu:analyze", "menu:product", "menu:reviews", "menu:prices", "menu:brand"}))
async def cb_ask_supplier(query: CallbackQuery, state: FSMContext) -> None:
    mode_map = {
        "menu:analyze": ("full", "🔍 Полный анализ магазина"),
        "menu:product": ("assort", "📦 Анализ ассортимента"),
        "menu:reviews": ("reviews", "⭐ Анализ отзывов"),
        "menu:prices": ("prices", "💰 Анализ цен"),
        "menu:brand": ("brand", "🎯 Совет под PlatSer Group"),
    }
    mode, title = mode_map[query.data]
    await state.set_state(Flow.waiting_supplier_id)
    await state.update_data(pending_mode=mode)
    await query.message.edit_text(
        f"{title}\n\n"
        "Пришли *ID продавца WB* (только число).\n\n"
        "Подсказка: ID есть в URL магазина — `wildberries.ru/seller/123456`.",
        reply_markup=cancel_input(),
    )
    await query.answer()


@dp.message(Flow.waiting_supplier_id)
async def on_supplier_id(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    digits = "".join(ch for ch in raw if ch.isdigit())
    if not digits or len(digits) < 3:
        await message.answer(
            "⚠️ Это не похоже на ID продавца. Пришли только число, например `123456`.",
            reply_markup=cancel_input(),
        )
        return
    supplier_id = int(digits)
    data = await state.get_data()
    pending_mode = data.get("pending_mode", "full")
    await state.clear()
    await message.answer(
        f"✅ ID принят: `{supplier_id}`\n\nВыбери режим разбора 👇",
        reply_markup=analysis_modes(supplier_id),
    )
    if pending_mode != "full":
        await _run_analysis(message.chat.id, supplier_id, pending_mode)


@dp.callback_query(F.data == "menu:own_sales")
async def cb_own_sales_menu(query: CallbackQuery) -> None:
    if not _has_own_shop():
        await query.answer("Не настроен WB_SUPPLIER_TOKEN / WB_OWN_SUPPLIER_ID", show_alert=True)
        return
    await query.message.edit_text(
        "📊 *Свои продажи PlatSer Group*\n\nВыбери период анализа 👇",
        reply_markup=sales_periods(),
    )
    await query.answer()


@dp.callback_query(F.data.startswith("own_sales:"))
async def cb_own_sales_run(query: CallbackQuery) -> None:
    days = int(query.data.split(":")[1])
    await query.answer(f"Тяну продажи за {days} дн…")
    await _run_own_sales(query.message.chat.id, days)


@dp.callback_query(F.data.startswith("modes:"))
async def cb_modes(query: CallbackQuery) -> None:
    supplier_id = int(query.data.split(":")[1])
    await query.message.edit_text(
        f"⚙️ Выбери режим разбора для магазина `{supplier_id}` 👇",
        reply_markup=analysis_modes(supplier_id),
    )
    await query.answer()


@dp.callback_query(F.data.startswith("run:"))
async def cb_run(query: CallbackQuery) -> None:
    _, mode, sid = query.data.split(":")
    supplier_id = int(sid)
    await query.answer("Запускаю анализ…")
    await _run_analysis(query.message.chat.id, supplier_id, mode)


async def _run_analysis(chat_id: int, supplier_id: int, mode: str) -> None:
    status = await bot.send_message(
        chat_id,
        f"⏳ Собираю данные по продавцу `{supplier_id}`…",
    )
    try:
        async with WBClient(dest=settings.wb_dest) as client:
            report = await analyze_shop(
                client,
                supplier_id=supplier_id,
                top_products=settings.wb_top_products,
                reviews_per_product=settings.wb_reviews_per_product,
                sales_token=settings.wb_supplier_token or None,
                own_supplier_id=settings.wb_own_supplier_id,
                stats_days=settings.wb_stats_days,
            )
        if not report.products:
            await status.edit_text(
                "🚫 Не удалось получить товары продавца.\n\n"
                "Возможные причины:\n"
                "• WB временно блокирует запросы (403) — попробуй ещё раз через 1–2 мин\n"
                "• Неверный ID продавца — проверь URL `wildberries.ru/seller/<ID>`\n"
                "• У продавца сейчас нет активных карточек в каталоге\n"
                "• С данного IP идёт блокировка (VPN/зарубежный сервер)"
            )
            await bot.send_message(chat_id, "🏠", reply_markup=main_menu(has_own_shop=_has_own_shop()))
            return

        await status.edit_text(
            f"📦 Найдено товаров: *{len(report.products)}*\n"
            f"⭐ Среднее ratings топа: *{report.rating_stats.get('avg', 0)}*\n"
            f"💬 Отзывов в выборке: *{report.reviews.total}*\n\n"
            "🤖 Отправляю в ChatGPT…"
        )
        payload = report_to_prompt_dict(report)
        text = await gpt.analyze(payload, mode=mode)
    except Exception as exc:  # noqa: BLE001
        log.exception("analysis failed")
        await status.edit_text(
            f"❌ Ошибка анализа: `{html.escape(str(exc))[:300]}`"
        )
        await bot.send_message(chat_id, "🏠", reply_markup=main_menu(has_own_shop=_has_own_shop()))
        return

    header = _build_header(report, mode)
    await status.edit_text(header)
    for chunk in _split_for_telegram(text):
        await bot.send_message(chat_id, chunk, disable_web_page_preview=True)
    await bot.send_message(
        chat_id,
        "✅ Готово. Что дальше?",
        reply_markup=after_report(supplier_id),
    )


async def _run_own_sales(chat_id: int, days: int) -> None:
    if not _has_own_shop():
        await bot.send_message(chat_id, "🚫 WB_SUPPLIER_TOKEN не задан в .env")
        return
    status = await bot.send_message(
        chat_id, f"⏳ Тяну продажи за *{days}* дн. из WB Statistics API…"
    )
    try:
        summary = await fetch_sales_summary(settings.wb_supplier_token, days)
    except Exception as exc:  # noqa: BLE001
        log.exception("own sales fetch failed")
        await status.edit_text(f"❌ Ошибка: `{html.escape(str(exc))[:300]}`")
        await bot.send_message(chat_id, "🏠", reply_markup=main_menu(_has_own_shop()))
        return
    if summary is None or (summary.sales_count == 0 and summary.orders_count == 0):
        await status.edit_text(
            "🤷 Нет продаж/заказов за выбранный период (или токен невалиден)."
        )
        await bot.send_message(chat_id, "🏠", reply_markup=main_menu(_has_own_shop()))
        return

    await status.edit_text(
        f"💰 Выручка: *{summary.gross_revenue:,.0f} ₽*\n"
        f"💵 К выплате: *{summary.net_payout:,.0f} ₽*\n"
        f"🧾 Продаж: *{summary.sales_count}* | Возвратов: *{summary.returns_count}* "
        f"({summary.returns_rate * 100:.1f}%)\n"
        f"📥 Заказов: *{summary.orders_count}* | Отмен: *{summary.cancelled_orders}* "
        f"({summary.cancel_rate * 100:.1f}%)\n"
        f"🧮 Конверсия заказ→выкуп: *{summary.conversion_orders_to_sales * 100:.1f}%*\n"
        f"💳 Средний чек: *{summary.avg_check:,.0f} ₽*\n\n"
        "🤖 Отправляю в ChatGPT…"
    )
    payload = {
        "supplier_id": settings.wb_own_supplier_id,
        "seller": {"trademark": "PlatSer Group"},
        "sales": sales_summary_to_dict(summary),
    }
    try:
        text = await gpt.analyze(payload, mode="sales")
    except Exception as exc:  # noqa: BLE001
        log.exception("gpt sales failed")
        await bot.send_message(chat_id, f"❌ GPT: `{html.escape(str(exc))[:300]}`")
        await bot.send_message(chat_id, "🏠", reply_markup=main_menu(_has_own_shop()))
        return
    for chunk in _split_for_telegram(text):
        await bot.send_message(chat_id, chunk, disable_web_page_preview=True)
    await bot.send_message(
        chat_id, "✅ Готово.", reply_markup=main_menu(_has_own_shop())
    )


def _build_header(report: Any, mode: str) -> str:
    titles = {
        "full": "🚀 Полный разбор магазина",
        "prices": "💰 Анализ цен",
        "reviews": "⭐ Анализ отзывов",
        "assort": "📦 Анализ ассортимента",
        "demand": "📈 Анализ спроса",
        "brand": "🎯 Стратегия PlatSer Group",
    }
    title = titles.get(mode, "📊 Отчёт")
    seller = report.seller_trademark or report.seller_name or f"id {report.supplier_id}"
    base = (
        f"{title}\n\n"
        f"🏪 *{seller}* (id `{report.supplier_id}`)\n"
        f"📦 Товаров: *{len(report.products)}* "
        f"| 💬 Отзывы выборки: *{report.reviews.total}*\n"
        f"💰 Цена ср/мед: *{report.price_stats.get('avg', 0)} ₽* / "
        f"*{report.price_stats.get('median', 0)} ₽*\n"
        f"⭐ Рейтинг ср: *{report.rating_stats.get('avg', 0)}* "
        f"| 🚫 OOS-карточек: *{report.out_of_stock_products}*\n"
    )
    if report.sales:
        s = report.sales
        base += (
            f"📊 Продажи {s.period_days} дн: *{s.gross_revenue:,.0f} ₽* "
            f"({s.sales_count} шт), возвратов *{s.returns_rate * 100:.1f}%*\n"
        )
    return base


def _split_for_telegram(text: str, limit: int = 3800) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    buf: list[str] = []
    size = 0
    for line in text.splitlines(keepends=True):
        if size + len(line) > limit and buf:
            chunks.append("".join(buf))
            buf, size = [], 0
        buf.append(line)
        size += len(line)
    if buf:
        chunks.append("".join(buf))
    return chunks


async def main() -> None:
    log.info("Bot starting…")
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
