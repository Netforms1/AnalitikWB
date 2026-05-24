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
    analysis_menu,
    back_to_menu,
    cancel_input,
    connect_menu,
    sales_periods,
)
from wb_api import (
    WBApiError,
    WBClient,
    WBCommonClient,
    parse_wb_token,
    token_scopes,
)

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
    waiting_token = State()


DIV = "━━━━━━━━━━━━━━━━━━━"


CONNECT_INSTRUCTIONS = (
    f"🔑 <b>ПОДКЛЮЧЕНИЕ WB API</b>\n{DIV}\n\n"
    "Нужен <b>JWT-токен</b> из кабинета WB. Без него я не смогу собрать "
    "продажи, воронку и контент — только публичные карточки.\n\n"
    "📋 <b>Как создать токен:</b>\n"
    "1. Открой <a href=\"https://seller.wildberries.ru/supplier-settings/access-to-api\">"
    "Настройки → Доступ к API</a> в WB-кабинете.\n"
    "2. Нажми <b>«Создать новый токен»</b>.\n"
    "3. <b>Название:</b> <code>AnalitikWB</code> (любое).\n"
    "4. <b>Срок жизни:</b> 180 дней.\n"
    "5. <b>ВАЖНО — отметь категории доступа:</b>\n"
    "   ✅ <b>Контент</b> — карточки, фото, описания\n"
    "   ✅ <b>Аналитика</b> — воронка показы→корзина→заказ→выкуп\n"
    "   ✅ <b>Статистика</b> — продажи, заказы, остатки\n"
    "   ✅ <b>Тарифы</b> — комиссии WB и логистика\n"
    "   ✅ <b>Вопросы и отзывы</b> — расширенные отзывы (если нужны)\n"
    "6. <b>Только чтение:</b> можно оставить включённым — бот ничего не пишет.\n"
    "7. Сгенерируй токен и <b>скопируй</b> его (длинная строка с двумя точками).\n\n"
    "📨 <b>Отправь токен следующим сообщением в этот чат.</b>\n\n"
    "<i>Безопасность: токен хранится только в памяти бота и пропадёт после "
    "перезапуска. Сообщение с токеном я удалю сразу после проверки.</i>"
)


def _welcome_unauth() -> str:
    return (
        "👋 <b>Привет!</b> Это бот аналитики <b>PlatSer Group</b>.\n\n"
        "Я разбираю магазин Wildberries по API:\n"
        "📊 продажи, выручка, средний чек, возвраты\n"
        "🔁 воронка показы→корзина→заказ→выкуп\n"
        "🖼 качество карточек (фото, видео, описание)\n"
        "💰 комиссии WB и реальная маржа\n"
        "📦 остатки и оборачиваемость\n"
        "⭐ отзывы и вопросы клиентов\n"
        "🎯 стратегические советы под бренд №1 в зоотоварах РФ\n\n"
        "<i>Сначала подключи свой WB API токен 👇</i>"
    )


def _welcome_authed(seller: dict[str, Any], scopes: list[str]) -> str:
    name = html.escape(seller.get("name") or seller.get("trademark") or "—")
    sid = seller.get("sid") or seller.get("supplier_id") or "—"
    scopes_str = ", ".join(scopes) if scopes else "—"
    return (
        f"✅ <b>Магазин подключён</b>\n{DIV}\n\n"
        f"🏪 <b>{name}</b>\n"
        f"🆔 supplier_id: <code>{sid}</code>\n"
        f"🔐 Скоупы токена: <i>{html.escape(scopes_str)}</i>\n\n"
        "<i>Выбери разбор 👇</i>"
    )


ABOUT = (
    f"ℹ️ <b>AnalitikWB — PlatSer Group</b>\n{DIV}\n\n"
    "Бот тянет данные через 5 разных WB API и просит ChatGPT-5 сделать "
    "развёрнутый отчёт под цель <b>PlatSer Group — №1 в зоотоварах РФ</b>.\n\n"
    "Никаких ID вводить не нужно — бот сам вытаскивает supplier_id "
    "из JWT-токена.\n\n"
    "<blockquote>Безопасность: токен хранится только в памяти процесса "
    "и пропадает при перезапуске. Сообщение с токеном удаляется из чата "
    "сразу после проверки.</blockquote>"
)


# ───────────────────────── helpers ─────────────────────────

async def _whoami(token: str) -> tuple[dict[str, Any], list[str]] | None:
    """Разбираем JWT и пробуем подтвердить через /seller-info.

    Возвращаем (seller_info, scopes) либо None если токен явно битый.
    """
    try:
        payload = parse_wb_token(token)
    except WBApiError as exc:
        log.info("token parse failed: %s", exc)
        return None
    scopes = token_scopes(payload)
    sid = payload.get("sid") or payload.get("supplier_id")
    if not sid:
        return None
    seller: dict[str, Any] = {"sid": sid}
    # пробуем дёрнуть seller-info, но это не критично
    try:
        async with WBCommonClient(token) as client:
            info = await client.get_seller_info()
        if isinstance(info, dict):
            seller.update({k: v for k, v in info.items() if v})
    except Exception as exc:  # noqa: BLE001
        log.info("seller-info call failed: %s", exc)
    return seller, scopes


async def _delete_silently(message: Message) -> None:
    try:
        await message.delete()
    except Exception as exc:  # noqa: BLE001
        log.debug("delete failed: %s", exc)


async def _ensure_token(state: FSMContext) -> dict[str, Any] | None:
    data = await state.get_data()
    if data.get("wb_token"):
        return data
    if settings.wb_default_token:
        info = await _whoami(settings.wb_default_token)
        if info:
            seller, scopes = info
            await state.update_data(
                wb_token=settings.wb_default_token,
                seller=seller,
                scopes=scopes,
            )
            return await state.get_data()
    return None


# ───────────────────────── handlers ─────────────────────────

@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    data = await _ensure_token(state)
    if data:
        await message.answer(
            _welcome_authed(data["seller"], data["scopes"]),
            reply_markup=analysis_menu(),
            disable_web_page_preview=True,
        )
    else:
        await state.clear()
        await message.answer(_welcome_unauth(), reply_markup=connect_menu(), disable_web_page_preview=True)


@dp.callback_query(F.data == "menu:home")
async def cb_home(query: CallbackQuery, state: FSMContext) -> None:
    data = await _ensure_token(state)
    if data:
        await query.message.edit_text(
            _welcome_authed(data["seller"], data["scopes"]),
            reply_markup=analysis_menu(),
            disable_web_page_preview=True,
        )
    else:
        await query.message.edit_text(
            _welcome_unauth(), reply_markup=connect_menu(), disable_web_page_preview=True
        )
    await query.answer()


@dp.callback_query(F.data == "menu:about")
async def cb_about(query: CallbackQuery) -> None:
    await query.message.edit_text(ABOUT, reply_markup=back_to_menu(), disable_web_page_preview=True)
    await query.answer()


@dp.callback_query(F.data == "menu:connect")
async def cb_connect(query: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Flow.waiting_token)
    await query.message.edit_text(
        CONNECT_INSTRUCTIONS,
        reply_markup=cancel_input(),
        disable_web_page_preview=True,
    )
    await query.answer()


@dp.callback_query(F.data == "menu:disconnect")
async def cb_disconnect(query: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await query.message.edit_text(
        f"🔌 <b>Токен удалён.</b>\n{DIV}\n\n"
        "Чтобы продолжить анализ — подключи токен заново.",
        reply_markup=connect_menu(),
    )
    await query.answer()


@dp.message(Flow.waiting_token)
async def on_token(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    # сразу удаляем сообщение с токеном — оно не должно висеть в чате
    await _delete_silently(message)

    if not raw or "." not in raw:
        await message.answer(
            "⚠️ Это не похоже на JWT. WB-токен — длинная строка с двумя точками "
            "(<code>xxx.yyy.zzz</code>). Создай его в кабинете WB и пришли сюда.",
            reply_markup=cancel_input(),
            disable_web_page_preview=True,
        )
        return

    status = await message.answer("⏳ Проверяю токен и подтягиваю магазин…")
    info = await _whoami(raw)
    if not info:
        await status.edit_text(
            "❌ <b>Токен невалиден</b> или в нём нет supplier_id.\n\n"
            "Проверь что:\n"
            "▪️ скопировал токен целиком (без пробелов)\n"
            "▪️ срок токена не истёк\n"
            "▪️ это токен <i>селлера</i>, а не покупателя",
            reply_markup=connect_menu(),
        )
        return

    seller, scopes = info
    await state.clear()
    await state.update_data(wb_token=raw, seller=seller, scopes=scopes)
    await status.edit_text(
        _welcome_authed(seller, scopes),
        reply_markup=analysis_menu(),
        disable_web_page_preview=True,
    )


@dp.callback_query(F.data == "menu:own_sales")
async def cb_own_sales_menu(query: CallbackQuery, state: FSMContext) -> None:
    data = await _ensure_token(state)
    if not data:
        await query.answer("Сначала подключи токен", show_alert=True)
        return
    await query.message.edit_text(
        f"📊 <b>Свои продажи</b>\n{DIV}\n\n<i>Выбери период анализа 👇</i>",
        reply_markup=sales_periods(),
    )
    await query.answer()


@dp.callback_query(F.data.startswith("own_sales:"))
async def cb_own_sales_run(query: CallbackQuery, state: FSMContext) -> None:
    days = int(query.data.split(":")[1])
    data = await _ensure_token(state)
    if not data:
        await query.answer("Сначала подключи токен", show_alert=True)
        return
    await query.answer(f"Тяну продажи за {days} дн…")
    await _run_own_sales(query.message.chat.id, days, data["wb_token"])


@dp.callback_query(F.data.startswith("run:"))
async def cb_run(query: CallbackQuery, state: FSMContext) -> None:
    mode = query.data.split(":")[1]
    data = await _ensure_token(state)
    if not data:
        await query.answer("Сначала подключи токен", show_alert=True)
        return
    await query.answer("Запускаю анализ…")
    await _run_analysis(query.message.chat.id, mode, data)


# ───────────────────────── analysis ─────────────────────────

async def _run_analysis(chat_id: int, mode: str, data: dict[str, Any]) -> None:
    seller = data["seller"]
    supplier_id = int(seller.get("sid") or seller.get("supplier_id") or 0)
    token = data["wb_token"]
    seller_name = seller.get("name") or seller.get("trademark") or f"id {supplier_id}"
    status = await bot.send_message(
        chat_id,
        f"⏳ Собираю данные по магазину <b>{html.escape(seller_name)}</b> "
        f"(<code>{supplier_id}</code>) через WB API…",
    )
    try:
        async with WBClient(dest=settings.wb_dest) as client:
            report = await analyze_shop(
                client,
                supplier_id=supplier_id,
                top_products=settings.wb_top_products,
                reviews_per_product=settings.wb_reviews_per_product,
                wb_token=token,
                stats_days=settings.wb_stats_days,
            )
        if not report.products and not report.sales and not report.funnel:
            await status.edit_text(
                "🚫 <b>Не удалось собрать данные.</b>\n\n"
                "▪️ Проверь что у токена есть скоупы Статистика / Аналитика / Контент / Тарифы\n"
                "▪️ Если магазин новый — данных могло ещё не накопиться\n"
                "▪️ WB временно недоступен — попробуй через минуту"
            )
            await bot.send_message(chat_id, "🏠", reply_markup=analysis_menu())
            return

        await status.edit_text(
            f"🤖 <i>Данные собраны. Отправляю в ChatGPT…</i>"
        )
        payload = report_to_prompt_dict(report)
        text = await gpt.analyze(payload, mode=mode)
    except Exception as exc:  # noqa: BLE001
        log.exception("analysis failed")
        await status.edit_text(
            f"❌ <b>Ошибка анализа:</b>\n<code>{html.escape(str(exc))[:500]}</code>"
        )
        await bot.send_message(chat_id, "🏠", reply_markup=analysis_menu())
        return

    header = _build_header(report, mode)
    await status.edit_text(header)
    for chunk in _split_for_telegram(text):
        await bot.send_message(chat_id, chunk, disable_web_page_preview=True)
    await bot.send_message(
        chat_id,
        "✅ <b>Готово.</b> <i>Что дальше?</i>",
        reply_markup=after_report(),
    )


async def _run_own_sales(chat_id: int, days: int, token: str) -> None:
    status = await bot.send_message(
        chat_id, f"⏳ Тяну продажи за <b>{days}</b> дн. из WB Statistics API…"
    )
    try:
        summary = await fetch_sales_summary(token, days)
    except Exception as exc:  # noqa: BLE001
        log.exception("own sales fetch failed")
        await status.edit_text(
            f"❌ <b>Ошибка:</b>\n<code>{html.escape(str(exc))[:500]}</code>"
        )
        await bot.send_message(chat_id, "🏠", reply_markup=analysis_menu())
        return
    if summary is None or (summary.sales_count == 0 and summary.orders_count == 0):
        await status.edit_text(
            "🤷 Нет продаж/заказов за выбранный период (или у токена нет скоупа Статистика)."
        )
        await bot.send_message(chat_id, "🏠", reply_markup=analysis_menu())
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
    payload = {"sales": sales_summary_to_dict(summary)}
    try:
        text = await gpt.analyze(payload, mode="sales")
    except Exception as exc:  # noqa: BLE001
        log.exception("gpt sales failed")
        await bot.send_message(
            chat_id, f"❌ <b>GPT:</b>\n<code>{html.escape(str(exc))[:500]}</code>"
        )
        await bot.send_message(chat_id, "🏠", reply_markup=analysis_menu())
        return
    for chunk in _split_for_telegram(text):
        await bot.send_message(chat_id, chunk, disable_web_page_preview=True)
    await bot.send_message(
        chat_id, "✅ <b>Готово.</b>", reply_markup=analysis_menu()
    )


def _build_header(report: Any, mode: str) -> str:
    titles = {
        "full": "🚀 ПОЛНЫЙ РАЗБОР МАГАЗИНА",
        "prices": "💰 АНАЛИЗ ЦЕН",
        "reviews": "⭐ АНАЛИЗ ОТЗЫВОВ",
        "assort": "📦 АНАЛИЗ АССОРТИМЕНТА",
        "demand": "📈 АНАЛИЗ СПРОСА",
        "funnel": "🔁 АНАЛИЗ ВОРОНКИ",
        "content": "🖼 КАЧЕСТВО КАРТОЧЕК",
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
    )
    if report.price_stats.get("avg"):
        base += (
            f"💰 Цена ср/мед: <b>{report.price_stats.get('avg', 0)} ₽</b> / "
            f"<b>{report.price_stats.get('median', 0)} ₽</b>\n"
        )
    if report.rating_stats.get("avg"):
        base += (
            f"⭐ Рейтинг ср: <b>{report.rating_stats.get('avg', 0)}</b> · "
            f"🚫 OOS: <b>{report.out_of_stock_products}</b>\n"
        )
    if report.sales:
        s = report.sales
        base += (
            f"📊 Продажи {s.period_days} дн: <b>{s.gross_revenue:,.0f} ₽</b> "
            f"(<b>{s.sales_count} шт</b>), возвратов <b>{s.returns_rate * 100:.1f}%</b>\n"
        )
    if report.funnel and report.funnel.opens:
        f = report.funnel
        base += (
            f"🔁 Воронка: <b>{f.opens:,}</b> показов → "
            f"CR <b>{f.cr_card_to_cart * 100:.1f}%</b> → "
            f"выкуп <b>{f.cr_order_to_buyout * 100:.1f}%</b>\n"
        )
    if report.content:
        c = report.content
        base += (
            f"🖼 Контент: <b>{c.avg_photos}</b> фото · "
            f"видео <b>{c.cards_with_video_share * 100:.0f}%</b> · "
            f"описание <b>{c.avg_description_len}</b> симв.\n"
        )
    if report.stocks and report.stocks.total_units:
        st = report.stocks
        dos = f" · {st.days_of_supply} дн оборот" if st.days_of_supply else ""
        base += f"📦 Остатки: <b>{st.total_units:,}</b> шт{dos}\n"
    if report.questions and report.questions.total:
        q = report.questions
        base += (
            f"❓ Вопросов: <b>{q.total}</b> · без ответа <b>{q.unanswered}</b>\n"
        )
    if report.margin and report.margin.avg_commission_pct:
        m = report.margin
        base += f"💵 Комиссия WB: <b>{m.avg_commission_pct}%</b>\n"
    return base


_OPEN_TAG_RE = re.compile(r"<(b|strong|i|em|u|ins|s|strike|del|code|pre|blockquote|tg-spoiler)\b[^>]*>")
_CLOSE_TAG_RE = re.compile(r"</(b|strong|i|em|u|ins|s|strike|del|code|pre|blockquote|tg-spoiler)>")


def _unclosed_tags(chunk: str) -> list[str]:
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
