from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def connect_menu() -> InlineKeyboardMarkup:
    """Меню до подключения токена."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔑 Подключить WB API", callback_data="menu:connect")],
            [InlineKeyboardButton(text="ℹ️ О боте", callback_data="menu:about")],
        ]
    )


def analysis_menu() -> InlineKeyboardMarkup:
    """Главное меню анализа (когда токен подключён)."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🚀 Полный разбор", callback_data="run:full")],
            [
                InlineKeyboardButton(text="💰 Цены", callback_data="run:prices"),
                InlineKeyboardButton(text="⭐ Отзывы", callback_data="run:reviews"),
            ],
            [
                InlineKeyboardButton(text="📦 Ассортимент", callback_data="run:assort"),
                InlineKeyboardButton(text="📈 Спрос", callback_data="run:demand"),
            ],
            [
                InlineKeyboardButton(text="🔁 Воронка", callback_data="run:funnel"),
                InlineKeyboardButton(text="🖼 Контент", callback_data="run:content"),
            ],
            [InlineKeyboardButton(text="📊 Продажи (период)", callback_data="menu:own_sales")],
            [InlineKeyboardButton(text="🎯 Стратегия PlatSer", callback_data="run:brand")],
            [
                InlineKeyboardButton(text="🔌 Сменить токен", callback_data="menu:connect"),
                InlineKeyboardButton(text="ℹ️ О боте", callback_data="menu:about"),
            ],
            [InlineKeyboardButton(text="🗑 Удалить токен", callback_data="menu:disconnect")],
        ]
    )


def back_to_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:home")],
        ]
    )


def cancel_input() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✖️ Отмена", callback_data="menu:home")],
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


def after_report() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔁 Другой разбор", callback_data="menu:home")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:home")],
        ]
    )
