"""Telegram bot for automatic thread bumping on Lolz.live — tick-based scheduler,
batch API, forum-backed thread listing."""

import asyncio
import logging
import sys
from datetime import datetime
from typing import Any, Awaitable, Callable, Sequence

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, StateFilter
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.types import TelegramObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.exceptions import TelegramRetryAfter, TelegramBadRequest

from config_manager import Config
from api_client import APIClient, BumpResult
from database import Database, BumpStats, Thread


NOTIFICATION_DELAY_SECONDS = 0.8
BUTTON_TEXT_MAX_LENGTH = 30
MY_THREADS_PAGE_SIZE = 10
AUTO_BUMP_RETRY_DELAY_SECONDS = 60


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("bot.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)


def _format_interval(minutes: float) -> str:
    """Human-friendly interval: minutes below an hour, trimmed decimals above."""
    if minutes < 60:
        if minutes == int(minutes):
            return f"{int(minutes)} мин"
        return f"{minutes:.1f} мин"
    hours = minutes / 60
    text = f"{hours:.2f}".rstrip("0").rstrip(".")
    return f"{text}ч"


class BotStates(StatesGroup):
    waiting_for_thread_ids = State()
    waiting_for_interval = State()
    waiting_for_batch_size = State()


class AuthMiddleware(BaseMiddleware):
    def __init__(self, admin_user_id: int):
        super().__init__()
        self._admin_user_id = admin_user_id

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        from_user = getattr(event, "from_user", None)
        if from_user is None:
            cq = getattr(event, "callback_query", None)
            if cq is not None:
                from_user = cq.from_user
            else:
                msg = getattr(event, "message", None)
                if msg is not None:
                    from_user = msg.from_user

        if from_user is None:
            return await handler(event, data)

        if from_user.id != self._admin_user_id:
            chat_id = None
            msg = getattr(event, "message", None)
            if msg is not None:
                chat_id = msg.chat.id
            else:
                cq = getattr(event, "callback_query", None)
                if cq is not None and cq.message is not None:
                    chat_id = cq.message.chat.id

            if chat_id:
                try:
                    bot: Bot = data["bot"]
                    await bot.send_message(chat_id, "⛔ Доступ запрещён.")
                except Exception:
                    pass
            return None

        return await handler(event, data)


class AutoBumpBot:
    __slots__ = (
        "_config", "_bot", "_dp", "_router", "_api", "_db",
        "_is_running", "_start_time", "_auto_bump_task",
    )

    def __init__(self, config: Config) -> None:
        self._config = config

        self._bot = Bot(
            token=config.bot.api_token,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )

        self._dp = Dispatcher(storage=MemoryStorage())
        self._router = Router(name="main_router")

        self._api = APIClient(
            config.api.base_url,
            config.api.auth_token,
            config.api.batch_size,
            config.scheduling.bump_delay_seconds,
        )
        self._db = Database(config.database.path)

        self._is_running = False
        self._start_time: datetime | None = None
        self._auto_bump_task: asyncio.Task | None = None

        self._register_handlers()
        self._dp.include_router(self._router)
        self._dp.update.middleware(AuthMiddleware(config.admin_user_id))

    def _register_handlers(self) -> None:
        # Commands
        self._router.message.register(
            self._handle_start_command, Command("start")
        )
        # FSM state handlers
        self._router.message.register(
            self._handle_thread_ids_input,
            StateFilter(BotStates.waiting_for_thread_ids),
        )
        self._router.message.register(
            self._handle_interval_input,
            StateFilter(BotStates.waiting_for_interval),
        )
        self._router.message.register(
            self._handle_batch_size_input,
            StateFilter(BotStates.waiting_for_batch_size),
        )
        # Callbacks
        self._router.callback_query.register(self._handle_add_thread_callback, F.data == "add_thread")
        self._router.callback_query.register(self._handle_list_threads_callback, F.data == "list_threads")
        self._router.callback_query.register(self._handle_delete_menu_callback, F.data == "delete_menu")
        self._router.callback_query.register(self._handle_delete_thread_callback, F.data.startswith("delete_"))
        self._router.callback_query.register(self._handle_bump_now_callback, F.data == "bump_now")
        self._router.callback_query.register(self._handle_stats_callback, F.data == "stats")
        self._router.callback_query.register(self._handle_refresh_titles_callback, F.data == "refresh_titles")
        self._router.callback_query.register(self._handle_my_threads_callback, F.data == "my_threads")
        self._router.callback_query.register(self._handle_my_threads_page_callback, F.data.startswith("mypage_"))
        self._router.callback_query.register(self._handle_my_add_callback, F.data.startswith("myadd_"))
        self._router.callback_query.register(self._handle_noop_callback, F.data == "noop")
        self._router.callback_query.register(self._handle_settings_callback, F.data == "settings")
        self._router.callback_query.register(self._handle_set_interval_callback, F.data == "set_interval")
        self._router.callback_query.register(self._handle_set_batch_size_callback, F.data == "set_batch_size")
        self._router.callback_query.register(self._handle_toggle_auto_bump_callback, F.data == "toggle_auto_bump")
        self._router.callback_query.register(self._handle_back_to_menu_callback, F.data == "back_to_menu")

    # ─── Keyboards ─────────────────────────────────────────────

    def _main_menu_kb(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="➕ Add topics", callback_data="add_thread"),
                InlineKeyboardButton(text="📥 My topics", callback_data="my_threads"),
            ],
            [
                InlineKeyboardButton(text="📋 List of topics", callback_data="list_threads"),
                InlineKeyboardButton(text="🗑️ Delete topic", callback_data="delete_menu"),
            ],
            [
                InlineKeyboardButton(text="🚀 Bump topics", callback_data="bump_now"),
                InlineKeyboardButton(text="🔄 Refresh", callback_data="refresh_titles"),
            ],
            [
                InlineKeyboardButton(text="📊 Statistics", callback_data="stats"),
                InlineKeyboardButton(text="🛠️ Settings", callback_data="settings"),
            ],
            [
                InlineKeyboardButton(text="👤 Author", url=self._config.bot.author_url),
            ],
        ])

    def _settings_kb(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⏰ Set Interval", callback_data="set_interval")],
            [InlineKeyboardButton(text="📦 Set Batch Size", callback_data="set_batch_size")],
            [InlineKeyboardButton(text="🔄 Toggle Auto-Bump", callback_data="toggle_auto_bump")],
            [InlineKeyboardButton(text="↩️ Back to Menu", callback_data="back_to_menu")],
        ])

    async def _send_main_menu(self, chat_id: int, text: str = "Choose an action:") -> None:
        markup = self._main_menu_kb()
        img_url = self._config.bot.img_url
        if img_url:
            try:
                await self._bot.send_photo(chat_id, photo=img_url, caption=text, reply_markup=markup)
                return
            except TelegramBadRequest as e:
                logger.warning(f"send_photo failed, falling back to text message: {e}")
        await self._bot.send_message(chat_id, text, reply_markup=markup)

    # ─── Utilities ──────────────────────────────────────────────

    @staticmethod
    def _truncate_text_safely(text: str, max_length: int) -> str:
        if len(text) <= max_length:
            return text
        truncated = text[:max_length]
        try:
            truncated.encode("utf-8")
            return truncated
        except UnicodeEncodeError:
            return AutoBumpBot._truncate_text_safely(text[: max_length - 1], max_length - 1)

    @staticmethod
    def _calculate_uptime(start_time: datetime) -> str:
        delta = datetime.now() - start_time
        total_seconds = int(delta.total_seconds())
        days = total_seconds // 86400
        hours = (total_seconds % 86400) // 3600
        minutes = (total_seconds % 3600) // 60
        if days > 0:
            return f"{days}d {hours}h {minutes}m"
        elif hours > 0:
            return f"{hours}h {minutes}m"
        else:
            return f"{minutes}m"

    async def _get_interval_minutes(self) -> float:
        val = await self._db.get_setting("bump_interval_minutes", "60")
        return float(val)

    async def _get_batch_size(self) -> int:
        val = await self._db.get_setting("batch_size", "10")
        return int(val)

    async def _is_auto_bump_enabled(self) -> bool:
        val = await self._db.get_setting("enable_auto_bump", "true")
        return val.lower() == "true"

    # ─── Start ─────────────────────────────────────────────────

    async def _handle_start_command(self, message: Message) -> None:
        try:
            interval = await self._get_interval_minutes()
            await self._send_main_menu(
                message.chat.id,
                text=(
                    "🤖 <b>QIYANA AUTO-BUMP BOT</b>\n\n"
                    "Бот для автоматического поднятия тем на Lolz.live\n\n"
                    f"⏰ Автоподнятие каждые <b>{_format_interval(interval)}</b>"
                ),
            )
        except TelegramBadRequest as e:
            logger.error(f"Failed to send start message: {e}")
            await message.answer("❌ Ошибка отправки сообщения. Попробуйте /start снова.")

    # ─── Add Thread ─────────────────────────────────────────────

    async def _handle_add_thread_callback(self, callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        if not callback.message:
            return
        await callback.message.answer(
            "📝 Введите ID тем через запятую для добавления:\n"
            "Пример: <code>12345, 67890, 11111</code>\n\n"
            "Или добавьте свои темы с форума через кнопку 📥 <b>My topics</b>"
        )
        await state.set_state(BotStates.waiting_for_thread_ids)

    async def _handle_thread_ids_input(self, message: Message, state: FSMContext) -> None:
        try:
            thread_ids = [tid.strip() for tid in message.text.split(",") if tid.strip()]

            if not thread_ids:
                await message.answer("❌ Не указаны ID тем")
                return

            valid_ids: list[str] = []
            errors: list[str] = []

            for thread_id in thread_ids:
                if not thread_id.isdigit():
                    errors.append(f"❌ {thread_id} - неверный формат")
                else:
                    valid_ids.append(thread_id)

            if not valid_ids:
                await message.answer("❌ Нет валидных ID тем")
                return

            status_msg = await message.answer(
                f"⏳ Добавляю {len(valid_ids)} тем и загружаю названия..."
            )

            # Fetch real titles via batch API
            titles_map: dict[str, str] = {}
            try:
                threads_info = await self._api.get_threads_info_batch(valid_ids)
                for tid, info in zip(valid_ids, threads_info):
                    if info and info.title:
                        titles_map[tid] = info.title
            except Exception as e:
                logger.error(f"Error fetching titles during add: {e}")
                for tid in valid_ids:
                    titles_map.setdefault(tid, f"Thread {tid}")

            added: list[str] = []
            for thread_id in valid_ids:
                title = titles_map.get(thread_id, f"Thread {thread_id}")
                success = await self._db.add_thread(thread_id, title)
                if success:
                    added.append(f"✅ {thread_id} — {title}")
                else:
                    errors.append(f"⚠️ {thread_id} - уже добавлена")

            result_parts = ["📊 Результат добавления:\n"]
            if added:
                result_parts.append("\n✅ <b>Добавлено:</b>\n")
                result_parts.append("\n".join(added))
                result_parts.append("\n")
            if errors:
                result_parts.append("\n❌ <b>Ошибки:</b>\n")
                result_parts.append("\n".join(errors))

            await status_msg.edit_text("".join(result_parts))
            await self._send_main_menu(message.chat.id)

        except Exception as e:
            logger.error(f"Error in thread IDs input handler: {e}", exc_info=True)
            await message.answer(f"❌ Критическая ошибка: {str(e)}")
        finally:
            await state.clear()

    # ─── List Threads ───────────────────────────────────────────

    async def _handle_list_threads_callback(self, callback: CallbackQuery) -> None:
        await callback.answer()
        if not callback.message:
            return
        try:
            threads = await self._db.get_all_threads()
            if not threads:
                await callback.message.answer("📭 Список тем пуст\n\nДобавьте темы через кнопку ➕ или 📥")
                await self._send_main_menu(callback.message.chat.id)
                return

            text_parts = [f"📋 <b>Список тем ({len(threads)}):</b>\n\n"]

            for thread in threads:
                status = "🆕 Новая"
                if thread.last_bumped:
                    try:
                        dt = datetime.fromisoformat(thread.last_bumped.replace("Z", "+00:00"))
                        status = f"✅ {dt.strftime('%d.%m.%Y %H:%M')}"
                    except ValueError:
                        status = f"✅ {thread.last_bumped[:16]}"

                text_parts.append(
                    f"<b>ID:</b> {thread.id}\n"
                    f"<b>Название:</b> {thread.title}\n"
                    f"<b>Статус:</b> {status}\n\n"
                )

            await callback.message.answer("".join(text_parts))
            await self._send_main_menu(callback.message.chat.id)

        except Exception as e:
            logger.error(f"Error listing threads: {e}", exc_info=True)
            await callback.message.answer(f"❌ Ошибка: {str(e)}")

    # ─── Refresh (titles sync) ──────────────────────────────────

    async def _handle_refresh_titles_callback(self, callback: CallbackQuery) -> None:
        await callback.answer()
        if not callback.message:
            return
        try:
            threads = await self._db.get_all_threads()
            if not threads:
                await callback.message.answer("📭 Список тем пуст")
                await self._send_main_menu(callback.message.chat.id)
                return

            status_msg = await callback.message.answer(
                f"🔄 Обновляю названия {len(threads)} тем..."
            )

            thread_ids = [t.id for t in threads]
            threads_info = await self._api.get_threads_info_batch(thread_ids)

            updated = 0
            for info, thread in zip(threads_info, threads):
                if info and info.title and info.title != thread.title:
                    await self._db.update_thread_title(thread.id, info.title)
                    updated += 1

            await status_msg.edit_text(
                f"🔄 <b>Названия обновлены!</b>\n\n"
                f"📝 Всего тем: {len(threads)}\n"
                f"✅ Обновлено: {updated}\n"
                f"⏭️ Без изменений: {len(threads) - updated}"
            )
            await self._send_main_menu(callback.message.chat.id)

        except Exception as e:
            logger.error(f"Error refreshing titles: {e}", exc_info=True)
            await callback.message.answer(f"❌ Ошибка обновления: {str(e)}")

    # ─── My Topics (forum listing) ─────────────────────────────

    async def _handle_my_threads_callback(self, callback: CallbackQuery) -> None:
        await callback.answer()
        if not callback.message:
            return
        await self._show_my_threads(callback.message.chat.id, page=1)

    async def _handle_my_threads_page_callback(self, callback: CallbackQuery) -> None:
        await callback.answer()
        if not callback.message:
            return
        try:
            page = int(callback.data.split("_", 1)[1])
        except ValueError:
            page = 1
        await self._show_my_threads(callback.message.chat.id, page=max(1, page),
                                    message_to_edit=callback.message)

    async def _show_my_threads(
        self, chat_id: int, page: int = 1, message_to_edit: Message | None = None
    ) -> None:
        try:
            page_data = await self._api.get_my_threads(page=page, limit=MY_THREADS_PAGE_SIZE)
        except Exception as e:
            logger.error(f"Error fetching my threads: {e}", exc_info=True)
            text = f"❌ Не удалось получить список тем с форума: {e}"
            if message_to_edit:
                try:
                    await message_to_edit.edit_text(text)
                except TelegramBadRequest:
                    pass
            else:
                await self._bot.send_message(chat_id, text)
            return

        total_pages = max(1, -(-page_data.total // MY_THREADS_PAGE_SIZE))

        if not page_data.threads:
            text = "📭 Ваши темы на форуме не найдены (или токен не даёт их видеть)"
            markup = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="↩️ Back to Menu", callback_data="back_to_menu")],
            ])
        else:
            buttons: list[list[InlineKeyboardButton]] = []
            for t in page_data.threads:
                title = self._truncate_text_safely(t.title or f"Thread {t.thread_id}", 60)
                buttons.append([
                    InlineKeyboardButton(text=title, callback_data=f"myadd_{t.thread_id}")
                ])

            nav: list[InlineKeyboardButton] = []
            if page > 1:
                nav.append(InlineKeyboardButton(text="◀️", callback_data=f"mypage_{page - 1}"))
            nav.append(InlineKeyboardButton(text=f"📄 {page}/{total_pages}", callback_data="noop"))
            if page < total_pages:
                nav.append(InlineKeyboardButton(text="▶️", callback_data=f"mypage_{page + 1}"))
            buttons.append(nav)
            buttons.append([InlineKeyboardButton(text="↩️ Back to Menu", callback_data="back_to_menu")])

            text = (
                f"📥 <b>Мои темы</b> — страница {page}/{total_pages}"
                f" (всего {page_data.total})\n\n"
                "Нажмите на тему, чтобы добавить её в список бампа."
            )
            markup = InlineKeyboardMarkup(inline_keyboard=buttons)

        if message_to_edit:
            try:
                await message_to_edit.edit_text(text, reply_markup=markup)
            except TelegramBadRequest:
                pass
        else:
            await self._bot.send_message(chat_id, text, reply_markup=markup)

    async def _handle_my_add_callback(self, callback: CallbackQuery) -> None:
        if not callback.data:
            return
        thread_id = callback.data.split("_", 1)[1]
        try:
            info_list = await self._api.get_threads_info_batch([thread_id])
            info = info_list[0] if info_list else None
        except Exception as e:
            logger.error(f"Error fetching thread {thread_id} for add: {e}")
            await callback.answer(f"❌ Ошибка получения темы: {e}", show_alert=True)
            return

        title = (info.title if info and info.title else f"Thread {thread_id}")
        added = await self._db.add_thread(thread_id, title)
        if added:
            logger.info(f"Thread {thread_id} added from my-topics listing")
            await callback.answer(f"✅ Добавлена: {title}")
        else:
            await callback.answer("⚠️ Тема уже в списке", show_alert=True)

    async def _handle_noop_callback(self, callback: CallbackQuery) -> None:
        await callback.answer()

    # ─── Delete Thread ──────────────────────────────────────────

    async def _handle_delete_menu_callback(self, callback: CallbackQuery) -> None:
        await callback.answer()
        if not callback.message:
            return
        try:
            threads = await self._db.get_all_threads()
            if not threads:
                await callback.message.answer("📭 Список тем пуст")
                await self._send_main_menu(callback.message.chat.id)
                return

            buttons = [
                [InlineKeyboardButton(
                    text=f"{thread.id} - {self._truncate_text_safely(thread.title, BUTTON_TEXT_MAX_LENGTH)}",
                    callback_data=f"delete_{thread.id}",
                )]
                for thread in threads
            ]
            await callback.message.answer(
                "🗑️ Выберите тему для удаления:",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
            )
        except Exception as e:
            logger.error(f"Error showing delete menu: {e}", exc_info=True)
            await callback.message.answer(f"❌ Ошибка: {str(e)}")

    async def _handle_delete_thread_callback(self, callback: CallbackQuery) -> None:
        if not callback.message:
            await callback.answer("❌ Ошибка: сообщение не найдено", show_alert=True)
            return
        try:
            thread_id = callback.data.split("_", 1)[1]
            success = await self._db.delete_thread(thread_id)
            if success:
                await callback.answer(f"✅ Тема {thread_id} удалена", show_alert=True)
                await callback.message.answer(f"✅ Тема {thread_id} успешно удалена из базы данных")
            else:
                await callback.answer(f"❌ Тема {thread_id} не найдена", show_alert=True)
            await self._send_main_menu(callback.message.chat.id)
        except Exception as e:
            logger.error(f"Error deleting thread: {e}", exc_info=True)
            await callback.answer(f"❌ Ошибка: {str(e)}", show_alert=True)

    # ─── Bump Now ───────────────────────────────────────────────

    async def _handle_bump_now_callback(self, callback: CallbackQuery) -> None:
        await callback.answer()
        if not callback.message:
            return
        try:
            threads = await self._db.get_all_threads()
            if not threads:
                await callback.message.answer("📭 Нет тем для поднятия\n\nДобавьте темы через кнопку ➕ или 📥")
                await self._send_main_menu(callback.message.chat.id)
                return

            status_msg = await callback.message.answer(
                f"🚀 Начинаю поднятие {len(threads)} тем...\n\n"
                "Используется batch API (до 10 тем за запрос)\n"
                "Подождите, это может занять некоторое время..."
            )

            results = await self._execute_bump_with_notifications(
                threads, chat_id=callback.message.chat.id
            )

            success_count = sum(1 for r in results if r.success)
            await status_msg.edit_text(
                f"📊 <b>Поднятие завершено!</b>\n\n"
                f"✅ Успешно: {success_count}\n"
                f"❌ Ошибок: {len(threads) - success_count}\n"
                f"📝 Всего тем: {len(threads)}"
            )
            await self._send_main_menu(callback.message.chat.id)

        except Exception as e:
            logger.error(f"Error bumping threads: {e}", exc_info=True)
            await callback.message.answer(f"❌ Ошибка: {str(e)}")

    # ─── Statistics ─────────────────────────────────────────────

    async def _handle_stats_callback(self, callback: CallbackQuery) -> None:
        await callback.answer()
        if not callback.message:
            return
        try:
            threads = await self._db.get_all_threads()
            interval = await self._get_interval_minutes()
            threads_ready = await self._db.get_threads_to_bump(interval)
            stats = await self._db.get_bump_stats()
            batch_size = await self._get_batch_size()
            auto_enabled = await self._is_auto_bump_enabled()

            uptime = "0 минут"
            if self._start_time:
                uptime = self._calculate_uptime(self._start_time)

            success_rate = 0.0
            if stats.total_bumps > 0:
                success_rate = (stats.successful_bumps / stats.total_bumps) * 100

            text = (
                "📊 <b>Статистика бота</b>\n\n"
                f"📋 <b>Темы:</b>\n"
                f"• Всего в списке: {len(threads)}\n"
                f"• Готовы к поднятию: {len(threads_ready)}\n"
                f"• Ожидают времени: {len(threads) - len(threads_ready)}\n\n"
                f"🚀 <b>Поднятия:</b>\n"
                f"• Всего попыток: {stats.total_bumps}\n"
                f"• Успешных: {stats.successful_bumps}\n"
                f"• Успешность: {success_rate:.1f}%\n"
                f"• Последний бамп: {stats.last_bump_time or '—'}\n\n"
                f"⚙️ <b>Настройки:</b>\n"
                f"• Интервал: {_format_interval(interval)}\n"
                f"• Batch size: {batch_size}\n"
                f"• Автобамп: {'✅ Вкл' if auto_enabled else '❌ Выкл'}\n\n"
                f"⏱️ <b>Работа:</b>\n"
                f"• Время работы: {uptime}"
            )

            await callback.message.answer(text)
            await self._send_main_menu(callback.message.chat.id)

        except Exception as e:
            logger.error(f"Error showing stats: {e}", exc_info=True)
            await callback.message.answer(f"❌ Ошибка: {str(e)}")

    # ─── Settings ───────────────────────────────────────────────

    async def _handle_settings_callback(self, callback: CallbackQuery) -> None:
        await callback.answer()
        if not callback.message:
            return
        interval = await self._get_interval_minutes()
        batch_size = await self._get_batch_size()
        auto_enabled = await self._is_auto_bump_enabled()

        text = (
            "🛠️ <b>Текущие настройки</b>\n\n"
            f"⏰ Интервал бампа: <b>{_format_interval(interval)}</b>\n"
            f"📦 Batch size: <b>{batch_size}</b>\n"
            f"🔄 Автобамп: <b>{'✅ Вкл' if auto_enabled else '❌ Выкл'}</b>\n"
        )
        await callback.message.answer(text, reply_markup=self._settings_kb())

    async def _handle_set_interval_callback(self, callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        if not callback.message:
            return
        interval = await self._get_interval_minutes()
        await callback.message.answer(
            f"Текущий интервал: <b>{_format_interval(interval)}</b>\n\n"
            "Введите новый интервал в минутах (например: <code>5</code> или <code>720</code>):"
        )
        await state.set_state(BotStates.waiting_for_interval)

    async def _handle_interval_input(self, message: Message, state: FSMContext) -> None:
        try:
            new_interval = float(message.text.strip())
            if new_interval <= 0:
                await message.answer("❌ Интервал должен быть положительным числом")
                return

            await self._db.set_setting("bump_interval_minutes", str(new_interval))
            logger.info(f"Bump interval changed to {new_interval} min by user {message.from_user.id}")

            # Tick-based scheduler picks up the new interval on the next tick — no restart needed
            await message.answer(f"✅ Интервал изменён на <b>{_format_interval(new_interval)}</b>")
            await self._send_main_menu(message.chat.id)

        except ValueError:
            await message.answer("❌ Введите число минут (например: <code>5</code>)")
        except Exception as e:
            logger.error(f"Error changing interval: {e}")
            await message.answer(f"❌ Ошибка: {str(e)}")
        finally:
            await state.clear()

    async def _handle_set_batch_size_callback(self, callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        if not callback.message:
            return
        batch_size = await self._get_batch_size()
        await callback.message.answer(
            f"Текущий batch size: <b>{batch_size}</b>\n\n"
            "Введите новый размер (от 1 до 10):"
        )
        await state.set_state(BotStates.waiting_for_batch_size)

    async def _handle_batch_size_input(self, message: Message, state: FSMContext) -> None:
        try:
            new_size = int(message.text.strip())
            if new_size < 1 or new_size > 10:
                await message.answer("❌ Batch size должен быть от 1 до 10")
                return

            await self._db.set_setting("batch_size", str(new_size))
            self._api.set_batch_size(new_size)
            logger.info(f"Batch size changed to {new_size} by user {message.from_user.id}")

            await message.answer(f"✅ Batch size изменён на <b>{new_size}</b>")
            await self._send_main_menu(message.chat.id)

        except ValueError:
            await message.answer("❌ Введите целое число (от 1 до 10)")
        except Exception as e:
            logger.error(f"Error changing batch size: {e}")
            await message.answer(f"❌ Ошибка: {str(e)}")
        finally:
            await state.clear()

    async def _handle_toggle_auto_bump_callback(self, callback: CallbackQuery) -> None:
        await callback.answer()
        if not callback.message:
            return

        current = await self._is_auto_bump_enabled()
        new_state = not current
        await self._db.set_setting("enable_auto_bump", str(new_state).lower())

        if new_state:
            self._restart_auto_bump()
            status = "✅ Включён"
        else:
            self._stop_auto_bump()
            status = "❌ Выключен"

        logger.info(f"Auto-bump toggled to {new_state} by user {callback.from_user.id}")
        await callback.message.answer(f"🔄 Автобамп: <b>{status}</b>")
        await self._send_main_menu(callback.message.chat.id)

    # ─── Back to Menu ───────────────────────────────────────────

    async def _handle_back_to_menu_callback(self, callback: CallbackQuery) -> None:
        await callback.answer()
        if not callback.message:
            return
        await self._send_main_menu(callback.message.chat.id)

    # ─── Bump Execution ─────────────────────────────────────────

    async def _execute_bump_with_notifications(
        self, threads: Sequence[Thread], chat_id: int | None = None
    ) -> list[BumpResult]:
        if not threads:
            return []

        thread_ids = [thread.id for thread in threads]
        logger.info(f"Starting bump for {len(thread_ids)} threads: {thread_ids}")

        results = await self._api.bump_threads_batch(thread_ids)

        success_count = 0
        for result in results:
            if result.success:
                success_count += 1
                logger.info(
                    f"✅ BUMP SUCCESS | Thread: {result.thread_id} | "
                    f"Status: {result.status.value} | Message: {result.message}"
                )
                await self._db.update_last_bumped(result.thread_id)
            else:
                logger.error(
                    f"❌ BUMP FAILED | Thread: {result.thread_id} | "
                    f"Status: {result.status.value} | Message: {result.message}"
                )

        total = len(results)
        await self._db.increment_bump_stats(success_count, total)

        if chat_id:
            await self._send_bump_notifications(chat_id, results)

        logger.info(
            f"Batch bump completed: {success_count}/{len(results)} successful | "
            f"Failed: {len(results) - success_count}"
        )

        return results

    async def _send_bump_notifications(self, chat_id: int, results: list[BumpResult]) -> None:
        for i, result in enumerate(results, 1):
            try:
                await self._bot.send_message(chat_id, f"[{i}/{len(results)}] {result.message}")
                if i < len(results):
                    await asyncio.sleep(NOTIFICATION_DELAY_SECONDS)
            except TelegramRetryAfter as e:
                logger.warning(f"Rate limit hit, waiting {e.retry_after} seconds")
                await asyncio.sleep(e.retry_after)
                try:
                    await self._bot.send_message(chat_id, f"[{i}/{len(results)}] {result.message}")
                except Exception as retry_error:
                    logger.error(f"Failed to send notification after retry: {retry_error}")
            except Exception as e:
                logger.error(f"Failed to send notification: {e}")

    # ─── Auto-Bump Scheduler (tick-based) ──────────────────────

    def _restart_auto_bump(self) -> None:
        self._stop_auto_bump()
        self._auto_bump_task = asyncio.create_task(self._auto_bump_scheduler_loop())

    def _stop_auto_bump(self) -> None:
        if self._auto_bump_task and not self._auto_bump_task.done():
            self._auto_bump_task.cancel()
        self._auto_bump_task = None

    async def _auto_bump_scheduler_loop(self) -> None:
        """Tick every SCHEDULER_TICK_SECONDS: read settings from DB, bump due threads.

        Reads interval/auto-bump from the DB on every tick, so settings changes
        apply immediately without restarting the loop and without losing phase.
        """
        tick_seconds = self._config.scheduling.scheduler_tick_seconds
        logger.info(f"Auto-bump scheduler started (tick every {tick_seconds:.0f}s)")
        while self._is_running:
            try:
                await asyncio.sleep(tick_seconds)
                if not self._is_running:
                    break

                if not await self._is_auto_bump_enabled():
                    continue

                interval = await self._get_interval_minutes()
                threads = await self._db.get_threads_to_bump(interval)
                if not threads:
                    continue

                logger.info(f"Auto-bump: {len(threads)} threads are due")
                results = await self._execute_bump_with_notifications(threads, chat_id=None)
                success_count = sum(1 for r in results if r.success)
                logger.info(
                    f"Scheduled bump completed: {success_count}/{len(results)} successful"
                )

            except asyncio.CancelledError:
                logger.info("Auto-bump loop cancelled")
                break
            except Exception as e:
                logger.error(f"Error in auto_bump_loop: {e}", exc_info=True)
                await asyncio.sleep(AUTO_BUMP_RETRY_DELAY_SECONDS)

    # ─── Lifecycle ──────────────────────────────────────────────

    async def start(self) -> None:
        try:
            await self._db.connect()
            logger.info("Database connected")

            await self._db.seed_defaults(self._config)
            logger.info("Database seeded with default settings")

            await self._api.start()
            logger.info("API client started")

            # Validate the Lolz API token early so misconfiguration is visible immediately
            me = await self._api.get_me()
            if me:
                logger.info(
                    f"API token OK | user_id={me.get('user_id', '?')} username={me.get('username', '?')}"
                )
            else:
                logger.warning(
                    "API token validation failed — bumps will likely fail until API_AUTH_TOKEN is fixed"
                )

            self._start_time = datetime.now()
            self._is_running = True

            if await self._is_auto_bump_enabled():
                self._auto_bump_task = asyncio.create_task(self._auto_bump_scheduler_loop())

            logger.info("Bot started successfully!")
            await self._dp.start_polling(self._bot)

        except Exception as e:
            logger.error(f"Error starting bot: {e}", exc_info=True)
            await self._cleanup_resources()
            raise

    async def stop(self) -> None:
        logger.info("Stopping bot...")
        self._is_running = False
        self._stop_auto_bump()
        await self._cleanup_resources()
        logger.info("Bot stopped")

    async def _cleanup_resources(self) -> None:
        try:
            await self._api.close()
        except Exception as e:
            logger.error(f"Error closing API client: {e}")
        try:
            await self._db.close()
        except Exception as e:
            logger.error(f"Error closing database: {e}")
        try:
            await self._bot.session.close()
        except Exception as e:
            logger.error(f"Error closing bot session: {e}")


async def main() -> None:
    try:
        config = Config.load()
        logger.info("Configuration loaded from .env successfully")
    except (FileNotFoundError, ValueError) as e:
        logger.error(f"Configuration error: {e}")
        sys.exit(1)

    bot = AutoBumpBot(config)

    try:
        await bot.start()
    except KeyboardInterrupt:
        logger.info("Bot interrupted by user")
    except Exception as e:
        logger.error(f"Bot crashed: {e}", exc_info=True)
        sys.exit(1)
    finally:
        await bot.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Application terminated")
