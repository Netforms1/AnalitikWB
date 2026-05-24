from __future__ import annotations

import asyncio
import html
import logging
import re
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
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher(storage=MemoryStorage())
gpt = GPTAnalyzer(api_key=settings.openai_api_key, model=settings.openai_model)


class Flow(StatesGroup):
    waiting_supplier_id = State()


def _has_own_shop() -> bool:
    return bool(settings.wb_supplier_token and settings.wb_own_supplier_id)


DIV = "━━━━━━━━━━━━━━━━━━━"

WELCOME = (
    "👋 <b>Привет!</b> Это бот аналитики <b>PlatSer Group</b>.\n\n"
    "Я разбираю любой магазин Wildberries и выдаю заключение через ChatGPT:\n"
    "📈 спрос и потенциал\n"
    "💰 ценовая политика — можно ли поднять цену\n"
    "📦 ассортимент и слабые карточки\n"
    "⭐ боли клиентов из отзывов\n"
    "🎯 рекомендации под цель «№1 в зоотоварах РФ»\n\n"
    "<i>Выбирай действие 👇</i>"
)

ABOUT = (
    f"ℹ️ <b>AnalitikWB — PlatSer Group</b>\n{DIV}\n\n"
    "Бот собирает публичные данные по продавцу на Wildberries "
    "(карточки, цены, остатки, отзывы) и просит <b>ChatGPT</b> составить "
    "развёрнутый отчёт со стратегическими выводами под бренд "
    "<b>PlatSer Group</b> — корма и товары для животных, цель: №1 в России.\n\n"
    "<blockquote>Нужен ID продавца WB (supplier_id). Найти его можно в URL "
    "магазина: <code>wildberries.ru/seller/&lt;ID&gt;</code>.</blockquote>"
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
        "menu:analyze": ("full", "🔍 <b>Полный анализ магазина</b>"),
        "menu:product": ("assort", "📦 <b>Анализ ассортимента</b>"),
        "menu:reviews": ("reviews", "⭐ <b>Анализ отзывов</b>"),
        "menu:prices": ("prices", "💰 <b>Анализ цен</b>"),
        "menu:brand": ("brand", "🎯 <b>Совет под PlatSer Group</b>"),
    }
    mode, title = mode_map[query.data]
    await state.set_state(Flow.waiting_supplier_id)
    await state.update_data(pending_mode=mode)
    await query.message.edit_text(
        f"{title}\n{DIV}\n\n"
        "Пришли <b>ID продавца WB</b> (только число).\n\n"
        "<i>Подсказка:</i> ID есть в URL магазина — "
        "<code>wildberries.ru/seller/123456</code>.",
        reply_markup=cancel_input(),
    )
    await query.answer()


@dp.message(Flow.waiting_supplier_id)
async def on_supplier_id(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    digits = "".join(ch for ch in raw if ch.isdigit())
    if not digits or len(digits) < 3:
        await message.answer(
            "⚠️ Это не похоже на ID продавца. Пришли только число, "
            "например <code>123456</code>.",
            reply_markup=cancel_input(),
        )
        return
    supplier_id = int(digits)
    data = await state.get_data()
    pending_mode = data.get("pending_mode", "full")
    await state.clear()
    await message.answer(
        f"✅ ID принят: <code>{supplier_id}</code>\n\n"
        "<i>Выбери режим разбора 👇</i>",
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
        f"📊 <b>Свои продажи PlatSer Group</b>\n{DIV}\n\n"
        "<i>Выбери период анализа 👇</i>",
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
        f"⚙️ <b>Режим разбора</b>\n"
        f"Магазин: <code>{supplier_id}</code>\n\n"
        "<i>Выбери что разобрать 👇</i>",
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
        f"⏳ Собираю данные по продавцу <code>{supplier_id}</code>…",
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
                "🚫 <b>Не удалось получить товары продавца.</b>\n\n"
                "<i>Возможные причины:</i>\n"
                "▪️ WB временно блокирует запросы (403) — попробуй через 1–2 мин\n"
                "▪️ Неверный ID продавца — проверь <code>wildberries.ru/seller/&lt;ID&gt;</code>\n"
                "▪️ У продавца сейчас нет активных карточек\n"
                "▪️ С данного IP идёт блокировка (VPN/зарубежный сервер)"
            )
            await bot.send_message(chat_id, "🏠", reply_markup=main_menu(has_own_shop=_has_own_shop()))
            return

        await status.edit_text(
            f"📦 Товаров: <b>{len(report.products)}</b>\n"
            f"⭐ Средний рейтинг: <b>{report.rating_stats.get('avg', 0)}</b>\n"
            f"💬 Отзывов в выборке: <b>{report.reviews.total}</b>\n\n"
            "🤖 <i>Отправляю в ChatGPT…</i>"
        )
        payload = report_to_prompt_dict(report)
        text = await gpt.analyze(payload, mode=mode)
    except Exception as exc:  # noqa: BLE001
        log.exception("analysis failed")
        await status.edit_text(
            f"❌ <b>Ошибка анализа:</b>\n<code>{html.escape(str(exc))[:500]}</code>"
        )
        await bot.send_message(chat_id, "🏠", reply_markup=main_menu(has_own_shop=_has_own_shop()))
        return

    header = _build_header(report, mode)
    await status.edit_text(header)
    for chunk in _split_for_telegram(text):
        await bot.send_message(chat_id, chunk, disable_web_page_preview=True)
    await bot.send_message(
        chat_id,
        "✅ <b>Готово.</b> <i>Что дальше?</i>",
        reply_markup=after_report(supplier_id),
    )


async def _run_own_sales(chat_id: int, days: int) -> None:
    if not _has_own_shop():
        await bot.send_message(chat_id, "🚫 WB_SUPPLIER_TOKEN не задан в .env")
        return
    status = await bot.send_message(
        chat_id, f"⏳ Тяну продажи за <b>{days}</b> дн. из WB Statistics API…"
    )
    try:
        summary = await fetch_sales_summary(settings.wb_supplier_token, days)
    except Exception as exc:  # noqa: BLE001
        log.exception("own sales fetch failed")
        await status.edit_text(
            f"❌ <b>Ошибка:</b>\n<code>{html.escape(str(exc))[:500]}</code>"
        )
        await bot.send_message(chat_id, "🏠", reply_markup=main_menu(_has_own_shop()))
        return
    if summary is None or (summary.sales_count == 0 and summary.orders_count == 0):
        await status.edit_text(
            "🤷 Нет продаж/заказов за выбранный период (или токен невалиден)."
        )
        await bot.send_message(chat_id, "🏠", reply_markup=main_menu(_has_own_shop()))
        return

    await status.edit_text(
        f"📊 <b>СВОДКА ЗА {days} ДН.</b>\n{DIV}\n\n"
        f"💰 Выручка: <b>{summary.gross_revenue:,.0f} ₽</b>\n"
        f"💵 К выплате: <b>{summary.net_payout:,.0f} ₽</b>\n"
        f"🧾 Продаж: <b>{summary.sales_count}</b> · "
        f"Возвратов: <b>{summary.returns_count}</b> "
        f"(<i>{summary.returns_rate * 100:.1f}%</i>)\n"
        f"📥 Заказов: <b>{summary.orders_count}</b> · "
        f"Отмен: <b>{summary.cancelled_orders}</b> "
        f"(<i>{summary.cancel_rate * 100:.1f}%</i>)\n"
        f"🧮 Конверсия заказ→выкуп: <b>{summary.conversion_orders_to_sales * 100:.1f}%</b>\n"
        f"💳 Средний чек: <b>{summary.avg_check:,.0f} ₽</b>\n\n"
        "🤖 <i>Отправляю в ChatGPT…</i>"
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
        await bot.send_message(
            chat_id, f"❌ <b>GPT:</b>\n<code>{html.escape(str(exc))[:500]}</code>"
        )
        await bot.send_message(chat_id, "🏠", reply_markup=main_menu(_has_own_shop()))
        return
    for chunk in _split_for_telegram(text):
        await bot.send_message(chat_id, chunk, disable_web_page_preview=True)
    await bot.send_message(
        chat_id, "✅ <b>Готово.</b>", reply_markup=main_menu(_has_own_shop())
    )


def _build_header(report: Any, mode: str) -> str:
    titles = {
        "full": "🚀 ПОЛНЫЙ РАЗБОР МАГАЗИНА",
        "prices": "💰 АНАЛИЗ ЦЕН",
        "reviews": "⭐ АНАЛИЗ ОТЗЫВОВ",
        "assort": "📦 АНАЛИЗ АССОРТИМЕНТА",
        "demand": "📈 АНАЛИЗ СПРОСА",
        "brand": "🎯 СТРАТЕГИЯ PLATSER GROUP",
    }
    title = titles.get(mode, "📊 ОТЧЁТ")
    seller_raw = report.seller_trademark or report.seller_name or f"id {report.supplier_id}"
    seller = html.escape(seller_raw)
    base = (
        f"<b>{title}</b>\n{DIV}\n\n"
        f"🏪 <b>{seller}</b> · id <code>{report.supplier_id}</code>\n"
        f"📦 Товаров: <b>{len(report.products)}</b> · "
        f"💬 отзывов: <b>{report.reviews.total}</b>\n"
        f"💰 Цена ср/мед: <b>{report.price_stats.get('avg', 0)} ₽</b> / "
        f"<b>{report.price_stats.get('median', 0)} ₽</b>\n"
        f"⭐ Рейтинг ср: <b>{report.rating_stats.get('avg', 0)}</b> · "
        f"🚫 OOS: <b>{report.out_of_stock_products}</b>\n"
    )
    if report.sales:
        s = report.sales
        base += (
            f"📊 Продажи {s.period_days} дн: <b>{s.gross_revenue:,.0f} ₽</b> "
            f"(<b>{s.sales_count} шт</b>), возвратов <b>{s.returns_rate * 100:.1f}%</b>\n"
        )
    return base


_OPEN_TAG_RE = re.compile(r"<(b|strong|i|em|u|ins|s|strike|del|code|pre|blockquote|tg-spoiler)\b[^>]*>")
_CLOSE_TAG_RE = re.compile(r"</(b|strong|i|em|u|ins|s|strike|del|code|pre|blockquote|tg-spoiler)>")


def _unclosed_tags(chunk: str) -> list[str]:
    """Возвращает список тегов, открытых в chunk и не закрытых."""
    stack: list[str] = []
    pos = 0
    while pos < len(chunk):
        open_m = _OPEN_TAG_RE.search(chunk, pos)
        close_m = _CLOSE_TAG_RE.search(chunk, pos)
        if open_m and (not close_m or open_m.start() < close_m.start()):
            stack.append(open_m.group(1))
            pos = open_m.end()
        elif close_m:
            tag = close_m.group(1)
            for i in range(len(stack) - 1, -1, -1):
                if stack[i] == tag:
                    del stack[i]
                    break
            pos = close_m.end()
        else:
            break
    return stack


def _split_for_telegram(text: str, limit: int = 3800) -> list[str]:
    """Режем длинный HTML на куски, не разрывая теги.

    Бьём по пустым строкам (границам параграфов). Если в куске остались
    открытые теги — закрываем их в конце и открываем заново в следующем.
    """
    if len(text) <= limit:
        return [text]
    paragraphs = text.split("\n\n")
    raw_chunks: list[str] = []
    buf: list[str] = []
    size = 0
    for p in paragraphs:
        addition = ("\n\n" if buf else "") + p
        if size + len(addition) > limit and buf:
            raw_chunks.append("\n\n".join(buf))
            buf, size = [p], len(p)
        else:
            buf.append(p)
            size += len(addition)
    if buf:
        raw_chunks.append("\n\n".join(buf))

    # Закрываем/реоткрываем теги между кусками
    fixed: list[str] = []
    carry: list[str] = []
    for chunk in raw_chunks:
        prefix = "".join(f"<{t}>" for t in carry)
        full = prefix + chunk
        unclosed = _unclosed_tags(full)
        suffix = "".join(f"</{t}>" for t in reversed(unclosed))
        fixed.append(full + suffix)
        carry = unclosed
    return fixed


async def main() -> None:
    log.info("Bot starting…")
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
