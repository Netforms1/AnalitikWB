from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def main_menu(has_own_shop: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="🔍 Анализ магазина", callback_data="menu:analyze")],
        [InlineKeyboardButton(text="📦 Анализ товара", callback_data="menu:product")],
        [InlineKeyboardButton(text="⭐ Только отзывы", callback_data="menu:reviews")],
        [InlineKeyboardButton(text="💰 Только цены", callback_data="menu:prices")],
    ]
    if has_own_shop:
        rows.append(
            [InlineKeyboardButton(text="📊 Свои продажи (PlatSer)", callback_data="menu:own_sales")]
        )
    rows.extend([
        [InlineKeyboardButton(text="🎯 PlatSer Group: советы", callback_data="menu:brand")],
        [InlineKeyboardButton(text="ℹ️ О боте", callback_data="menu:about")],
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def back_to_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:home")],
        ]
    )


def sales_periods() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🗓 7 дней", callback_data="own_sales:7"),
                InlineKeyboardButton(text="🗓 30 дней", callback_data="own_sales:30"),
            ],
            [
                InlineKeyboardButton(text="🗓 60 дней", callback_data="own_sales:60"),
                InlineKeyboardButton(text="🗓 90 дней", callback_data="own_sales:90"),
            ],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:home")],
        ]
    )


def cancel_input() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✖️ Отмена", callback_data="menu:home")],
        ]
    )


def analysis_modes(supplier_id: int) -> InlineKeyboardMarkup:
    sid = str(supplier_id)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🚀 Полный разбор", callback_data=f"run:full:{sid}")],
            [
                InlineKeyboardButton(text="💰 Цены", callback_data=f"run:prices:{sid}"),
                InlineKeyboardButton(text="⭐ Отзывы", callback_data=f"run:reviews:{sid}"),
            ],
            [
                InlineKeyboardButton(text="📦 Ассортимент", callback_data=f"run:assort:{sid}"),
                InlineKeyboardButton(text="📈 Спрос", callback_data=f"run:demand:{sid}"),
            ],
            [
                InlineKeyboardButton(text="🔁 Воронка", callback_data=f"run:funnel:{sid}"),
                InlineKeyboardButton(text="🖼 Контент", callback_data=f"run:content:{sid}"),
            ],
            [InlineKeyboardButton(text="🎯 Совет под PlatSer", callback_data=f"run:brand:{sid}")],
            [InlineKeyboardButton(text="🔙 Сменить магазин", callback_data="menu:analyze")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:home")],
        ]
    )


def after_report(supplier_id: int) -> InlineKeyboardMarkup:
    sid = str(supplier_id)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔁 Повторить разбор", callback_data=f"run:full:{sid}")],
            [InlineKeyboardButton(text="⚙️ Другой режим", callback_data=f"modes:{sid}")],
            [InlineKeyboardButton(text="🆕 Новый магазин", callback_data="menu:analyze")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:home")],
        ]
    )
