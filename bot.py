import asyncio
import os
import sqlite3
import time
from dotenv import load_dotenv
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / "pomnibot.env")
bot = Bot(token=os.getenv("BOT_TOKEN"))
dp = Dispatcher()

HELP_TEXT = (
    "Команды:\n"
    "/remind 10 купить хлеб - напомнить через 10 минут\n"
    "/list - мои активные напоминания"
)

db = sqlite3.connect("reminders.db")
db.execute(
    """CREATE TABLE IF NOT EXISTS reminders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER NOT NULL,
        text TEXT NOT NULL,
        remind_at INTEGER NOT NULL,
        done INTEGER NOT NULL DEFAULT 0
    )"""
)
try:
    # для старой базы, где ещё нет колонки done
    db.execute("ALTER TABLE reminders ADD COLUMN done INTEGER NOT NULL DEFAULT 0")
except sqlite3.OperationalError:
    pass  # колонка уже есть
db.commit()


class Snooze(StatesGroup):
    waiting_minutes = State()


def snooze_keyboard(rid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="5 мин", callback_data=f"snooze:{rid}:5"),
                InlineKeyboardButton(text="10 мин", callback_data=f"snooze:{rid}:10"),
                InlineKeyboardButton(text="30 мин", callback_data=f"snooze:{rid}:30"),
                InlineKeyboardButton(text="1 час", callback_data=f"snooze:{rid}:60"),
            ],
            [
                InlineKeyboardButton(text="✏️ Своё время", callback_data=f"custom:{rid}"),
                InlineKeyboardButton(text="✅ Готово", callback_data=f"done:{rid}"),
            ],
        ]
    )


def get_reminder(rid: int, chat_id: int):
    # проверяем и id, и чат, чтобы нельзя было трогать чужие напоминания
    return db.execute(
        "SELECT text FROM reminders WHERE id = ? AND chat_id = ?", (rid, chat_id)
    ).fetchone()


def set_snooze(rid: int, minutes: int):
    db.execute(
        "UPDATE reminders SET remind_at = ?, done = 0 WHERE id = ?",
        (int(time.time()) + minutes * 60, rid),
    )
    db.commit()


@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer("Я бот-напоминалка.\n" + HELP_TEXT)


@dp.message(Command("remind"))
async def cmd_remind(message: Message, command: CommandObject):
    if not command.args:
        await message.answer(HELP_TEXT)
        return

    parts = command.args.split(maxsplit=1)
    if len(parts) < 2 or not parts[0].isdigit():
        await message.answer(HELP_TEXT)
        return

    minutes = int(parts[0])
    text = parts[1]

    if minutes < 1 or minutes > 1440:
        await message.answer("Укажите время от 1 до 1440 минут.")
        return

    db.execute(
        "INSERT INTO reminders (chat_id, text, remind_at) VALUES (?, ?, ?)",
        (message.chat.id, text, int(time.time()) + minutes * 60),
    )
    db.commit()
    await message.answer(f"Хорошо, напомню через {minutes} мин: {text}")


@dp.message(Command("list"))
async def cmd_list(message: Message):
    rows = db.execute(
        "SELECT text, remind_at FROM reminders "
        "WHERE chat_id = ? AND done = 0 ORDER BY remind_at",
        (message.chat.id,),
    ).fetchall()

    if not rows:
        await message.answer("Активных напоминаний нет.")
        return

    now = int(time.time())
    lines = []
    for text, remind_at in rows:
        left = max(1, (remind_at - now + 59) // 60)
        lines.append(f"• {text} (через {left} мин)")
    await message.answer("\n".join(lines))


# ---------- кнопки ----------

@dp.callback_query(F.data.startswith("snooze:"))
async def cb_snooze(callback: CallbackQuery):
    _, rid, minutes = callback.data.split(":")
    row = get_reminder(int(rid), callback.message.chat.id)
    if not row:
        await callback.answer("Напоминание не найдено", show_alert=True)
        return

    set_snooze(int(rid), int(minutes))
    await callback.message.edit_text(f"😴 Отложено на {minutes} мин: {row[0]}")
    await callback.answer()


@dp.callback_query(F.data.startswith("custom:"))
async def cb_custom(callback: CallbackQuery, state: FSMContext):
    rid = int(callback.data.split(":")[1])
    await state.set_state(Snooze.waiting_minutes)
    await state.update_data(rid=rid)
    await callback.message.answer(
        "На сколько минут отложить? Напишите число от 1 до 1440."
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("done:"))
async def cb_done(callback: CallbackQuery):
    rid = int(callback.data.split(":")[1])
    row = get_reminder(rid, callback.message.chat.id)
    if row:
        db.execute("DELETE FROM reminders WHERE id = ?", (rid,))
        db.commit()
        await callback.message.edit_text(f"✅ Готово: {row[0]}")
    await callback.answer()


# этот обработчик должен стоять ПОСЛЕ команд, чтобы /list и /remind работали всегда
@dp.message(Snooze.waiting_minutes)
async def got_minutes(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    if not raw.isdigit() or not (1 <= int(raw) <= 1440):
        await message.answer("Нужно число от 1 до 1440. Попробуйте ещё раз.")
        return

    minutes = int(raw)
    data = await state.get_data()
    rid = data["rid"]
    row = get_reminder(rid, message.chat.id)
    await state.clear()

    if not row:
        await message.answer("Напоминание не найдено.")
        return

    set_snooze(rid, minutes)
    await message.answer(f"😴 Отложено на {minutes} мин: {row[0]}")


# ---------- фоновая проверка ----------

async def checker():
    while True:
        now = int(time.time())
        rows = db.execute(
            "SELECT id, chat_id, text FROM reminders WHERE done = 0 AND remind_at <= ?",
            (now,),
        ).fetchall()
        for rid, chat_id, text in rows:
            try:
                await bot.send_message(
                    chat_id,
                    f"⏰ Напоминание: {text}",
                    reply_markup=snooze_keyboard(rid),
                )
            except Exception as e:
                print("Не удалось отправить:", e)
            # помечаем как отправленное; кнопка «Отложить» снова сделает его активным
            db.execute("UPDATE reminders SET done = 1 WHERE id = ?", (rid,))
        db.commit()
        await asyncio.sleep(10)


async def main():
    asyncio.create_task(checker())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main());
