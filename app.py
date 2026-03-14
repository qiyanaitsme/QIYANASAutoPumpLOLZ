"""Telegram bot for automatic thread bumping on Lolz.live."""

import asyncio
import logging
import sys
from datetime import datetime
from typing import Sequence

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, StateFilter
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.exceptions import TelegramRetryAfter, TelegramBadRequest

from config_manager import Config
from api_client import APIClient, BumpResult
from database import Database, Thread


# Constants
NOTIFICATION_DELAY_SECONDS = 0.8
BUTTON_TEXT_MAX_LENGTH = 30
AUTO_BUMP_RETRY_DELAY_SECONDS = 60


# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("bot.log", encoding="utf-8")
    ]
)
logger = logging.getLogger(__name__)


class BotStates(StatesGroup):
    """FSM states for bot."""
    waiting_for_thread_ids = State()


class AutoBumpBot:
    """Main bot class with clean separation of concerns."""
    
    __slots__ = (
        "_config", "_bot", "_dp", "_router", "_api", "_db",
        "_is_running", "_total_bumps", "_successful_bumps", "_start_time"
    )
    
    def __init__(self, config: Config) -> None:
        self._config = config
        
        # Initialize bot with default properties (aiogram 3.15.0 best practice)
        self._bot = Bot(
            token=config.bot.api_token,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML)
        )
        
        self._dp = Dispatcher(storage=MemoryStorage())
        self._router = Router(name="main_router")
        
        self._api = APIClient(
            config.api.base_url,
            config.api.auth_token,
            config.api.batch_size
        )
        self._db = Database(config.database.path)
        
        # Statistics
        self._is_running = False
        self._total_bumps = 0
        self._successful_bumps = 0
        self._start_time: datetime | None = None
        
        # Register handlers
        self._register_handlers()
        
        # Include router in dispatcher
        self._dp.include_router(self._router)
    
    def _register_handlers(self) -> None:
        """Register all bot message and callback handlers."""
        # Message handlers
        self._router.message.register(
            self._handle_start_command,
            Command("start")
        )
        self._router.message.register(
            self._handle_thread_ids_input,
            StateFilter(BotStates.waiting_for_thread_ids)
        )
        
        # Callback query handlers
        self._router.callback_query.register(
            self._handle_add_thread_callback,
            F.data == "add_thread"
        )
        self._router.callback_query.register(
            self._handle_list_threads_callback,
            F.data == "list_threads"
        )
        self._router.callback_query.register(
            self._handle_delete_menu_callback,
            F.data == "delete_menu"
        )
        self._router.callback_query.register(
            self._handle_delete_thread_callback,
            F.data.startswith("delete_")
        )
        self._router.callback_query.register(
            self._handle_bump_now_callback,
            F.data == "bump_now"
        )
        self._router.callback_query.register(
            self._handle_stats_callback,
            F.data == "stats"
        )
    
    def _create_main_menu_keyboard(self) -> InlineKeyboardMarkup:
        """Create main menu inline keyboard."""
        return InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="➕ Add topics", callback_data="add_thread"),
                InlineKeyboardButton(text="📋 List of topics", callback_data="list_threads")
            ],
            [
                InlineKeyboardButton(text="🗑️ Delete topic", callback_data="delete_menu"),
                InlineKeyboardButton(text="🚀 Bump topics", callback_data="bump_now")
            ],
            [
                InlineKeyboardButton(text="📊 Statistics", callback_data="stats"),
                InlineKeyboardButton(text="👤 Author", url=self._config.bot.author_url)
            ]
        ])
    
    async def _send_main_menu(self, chat_id: int, text: str = "Choose an action:") -> None:
        """Send main menu to user with photo for consistent width."""
        await self._bot.send_photo(
            chat_id,
            photo=self._config.bot.img_url,
            caption=text,
            reply_markup=self._create_main_menu_keyboard()
        )
    
    @staticmethod
    def _truncate_text_safely(text: str, max_length: int) -> str:
        """Safely truncate text without breaking unicode characters."""
        if len(text) <= max_length:
            return text
        
        truncated = text[:max_length]
        try:
            truncated.encode('utf-8')
            return truncated
        except UnicodeEncodeError:
            return AutoBumpBot._truncate_text_safely(text[:max_length - 1], max_length - 1)
    
    @staticmethod
    def _calculate_uptime(start_time: datetime) -> str:
        """Calculate uptime from start time."""
        delta = datetime.now() - start_time
        total_seconds = int(delta.total_seconds())
        
        days = total_seconds // 86400
        hours = (total_seconds % 86400) // 3600
        minutes = (total_seconds % 3600) // 60
        
        if days > 0:
            return f"{days}д {hours}ч {minutes}м"
        elif hours > 0:
            return f"{hours}ч {minutes}м"
        else:
            return f"{minutes}м"
    
    async def _handle_start_command(self, message: Message) -> None:
        """Handle /start command."""
        try:
            await message.answer_photo(
                photo=self._config.bot.img_url,
                caption=(
                    "🤖 <b>QIYANA AUTO-BUMP BOT</b>\n\n"
                    "Бот для автоматического поднятия тем на Lolz.live\n\n"
                    f"⏰ Автоподнятие каждые <b>{self._config.scheduling.bump_interval_hours}ч</b>"
                ),
                reply_markup=self._create_main_menu_keyboard()
            )
        except TelegramBadRequest as e:
            logger.error(f"Failed to send start message: {e}")
            await message.answer("❌ Ошибка отправки сообщения. Попробуйте /start снова.")
    
    async def _handle_add_thread_callback(self, callback: CallbackQuery, state: FSMContext) -> None:
        """Handle add thread button callback."""
        await callback.answer()
        
        if not callback.message:
            return
        
        await callback.message.answer(
            "📝 Введите ID тем через запятую для добавления:\n"
            "Пример: <code>12345, 67890, 11111</code>"
        )
        await state.set_state(BotStates.waiting_for_thread_ids)
    
    async def _handle_thread_ids_input(self, message: Message, state: FSMContext) -> None:
        """Handle thread IDs input from user."""
        try:
            thread_ids = [tid.strip() for tid in message.text.split(",") if tid.strip()]
            
            if not thread_ids:
                await message.answer("❌ Не указаны ID тем")
                return
            
            # Validate format
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
                f"⏳ Добавляю {len(valid_ids)} тем в базу данных..."
            )
            
            # Add threads to database directly (without fetching titles)
            added: list[str] = []
            
            for thread_id in valid_ids:
                # Use thread ID as title initially (will be fetched on first bump/list)
                success = await self._db.add_thread(thread_id, f"Thread {thread_id}")
                
                if success:
                    added.append(f"✅ {thread_id}")
                else:
                    errors.append(f"⚠️ {thread_id} - уже добавлена")
            
            # Build result message
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
    
    async def _handle_list_threads_callback(self, callback: CallbackQuery) -> None:
        """Handle list threads button callback."""
        await callback.answer()
        
        if not callback.message:
            return
        
        try:
            threads = await self._db.get_all_threads()
            
            if not threads:
                await callback.message.answer("📭 Список тем пуст\n\nДобавьте темы через кнопку ➕")
                await self._send_main_menu(callback.message.chat.id)
                return
            
            # Fetch real titles from API using batch
            status_msg = await callback.message.answer(
                f"⏳ Загружаю информацию о {len(threads)} темах..."
            )
            
            thread_ids = [t.id for t in threads]
            try:
                threads_info = await self._api.get_threads_info_batch(thread_ids)
            except Exception as e:
                logger.error(f"Error fetching thread titles: {e}", exc_info=True)
                threads_info = [None] * len(thread_ids)
            
            # Build formatted list with thread info
            text_parts = [f"📋 <b>Список тем ({len(threads)}):</b>\n\n"]
            
            for thread, thread_info in zip(threads, threads_info):
                # Use fetched title or fallback to database title
                title = thread_info.title if thread_info else thread.title
                
                # Format last bump status
                status = "🆕 Новая"
                if thread.last_bumped:
                    try:
                        dt = datetime.fromisoformat(thread.last_bumped.replace("Z", "+00:00"))
                        status = f"✅ {dt.strftime('%d.%m.%Y %H:%M')}"
                    except ValueError:
                        status = f"✅ {thread.last_bumped[:16]}"
                
                # Format thread entry
                text_parts.append(
                    f"<b>ID:</b> {thread.id}\n"
                    f"<b>Название:</b> {title}\n"
                    f"<b>Статус:</b> {status}\n\n"
                )
            
            await status_msg.edit_text("".join(text_parts))
            await self._send_main_menu(callback.message.chat.id)
        
        except Exception as e:
            logger.error(f"Error listing threads: {e}", exc_info=True)
            await callback.message.answer(f"❌ Ошибка: {str(e)}")
    
    async def _handle_delete_menu_callback(self, callback: CallbackQuery) -> None:
        """Handle delete menu button callback."""
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
                    callback_data=f"delete_{thread.id}"
                )]
                for thread in threads
            ]
            
            keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
            
            await callback.message.answer(
                "🗑️ Выберите тему для удаления:",
                reply_markup=keyboard
            )
        
        except Exception as e:
            logger.error(f"Error showing delete menu: {e}", exc_info=True)
            await callback.message.answer(f"❌ Ошибка: {str(e)}")
    
    async def _handle_delete_thread_callback(self, callback: CallbackQuery) -> None:
        """Handle delete specific thread callback."""
        if not callback.message:
            await callback.answer("❌ Ошибка: сообщение не найдено", show_alert=True)
            return
        
        try:
            thread_id = callback.data.split("_", 1)[1]
            success = await self._db.delete_thread(thread_id)
            
            if success:
                await callback.answer(f"✅ Тема {thread_id} удалена", show_alert=True)
                await callback.message.answer(f"✅ Тема {thread_id} успешно удалена из базы данных")
                await self._send_main_menu(callback.message.chat.id)
            else:
                await callback.answer(f"❌ Тема {thread_id} не найдена", show_alert=True)
        
        except Exception as e:
            logger.error(f"Error deleting thread: {e}", exc_info=True)
            await callback.answer(f"❌ Ошибка: {str(e)}", show_alert=True)
    
    async def _handle_bump_now_callback(self, callback: CallbackQuery) -> None:
        """Handle bump now button callback."""
        await callback.answer()
        
        if not callback.message:
            return
        
        try:
            threads = await self._db.get_threads_to_bump(0)
            
            if not threads:
                await callback.message.answer("📭 Нет тем для поднятия\n\nДобавьте темы через кнопку ➕")
                await self._send_main_menu(callback.message.chat.id)
                return
            
            status_msg = await callback.message.answer(
                f"🚀 Начинаю поднятие {len(threads)} тем...\n\n"
                "Используется batch API (до 10 тем за запрос)\n"
                "Подождите, это может занять некоторое время..."
            )
            
            results = await self._execute_bump_with_notifications(
                threads,
                chat_id=callback.message.chat.id
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
    
    async def _handle_stats_callback(self, callback: CallbackQuery) -> None:
        """Handle statistics button callback."""
        await callback.answer()
        
        if not callback.message:
            return
        
        try:
            threads = await self._db.get_all_threads()
            threads_ready = await self._db.get_threads_to_bump(
                self._config.scheduling.bump_interval_hours
            )
            
            uptime = "0 минут"
            if self._start_time:
                uptime = self._calculate_uptime(self._start_time)
            
            success_rate = 0.0
            if self._total_bumps > 0:
                success_rate = (self._successful_bumps / self._total_bumps) * 100
            
            text = (
                "📊 <b>Статистика бота</b>\n\n"
                f"📋 <b>Темы:</b>\n"
                f"• Всего в списке: {len(threads)}\n"
                f"• Готовы к поднятию: {len(threads_ready)}\n"
                f"• Ожидают времени: {len(threads) - len(threads_ready)}\n\n"
                f"🚀 <b>Поднятия:</b>\n"
                f"• Всего попыток: {self._total_bumps}\n"
                f"• Успешных: {self._successful_bumps}\n"
                f"• Успешность: {success_rate:.1f}%\n\n"
                f"⚙️ <b>Настройки:</b>\n"
                f"• Интервал: {self._config.scheduling.bump_interval_hours}ч\n"
                f"• Batch size: {self._config.api.batch_size}\n\n"
                f"⏱️ <b>Работа:</b>\n"
                f"• Время работы: {uptime}"
            )
            
            await callback.message.answer(text)
            await self._send_main_menu(callback.message.chat.id)
        
        except Exception as e:
            logger.error(f"Error showing stats: {e}", exc_info=True)
            await callback.message.answer(f"❌ Ошибка: {str(e)}")
    
    async def _execute_bump_with_notifications(
        self,
        threads: Sequence[Thread],
        chat_id: int | None = None
    ) -> list[BumpResult]:
        """Execute thread bumping with optional notifications."""
        if not threads:
            return []
        
        thread_ids = [thread.id for thread in threads]
        
        logger.info(f"Starting bump for {len(thread_ids)} threads: {thread_ids}")
        
        # Use batch API to bump all threads
        results = await self._api.bump_threads_batch(thread_ids)
        
        # Log detailed results
        for result in results:
            if result.success:
                logger.info(
                    f"✅ BUMP SUCCESS | Thread: {result.thread_id} | "
                    f"Status: {result.status.value} | Message: {result.message}"
                )
            else:
                logger.error(
                    f"❌ BUMP FAILED | Thread: {result.thread_id} | "
                    f"Status: {result.status.value} | Message: {result.message}"
                )
        
        # Update statistics and database
        for result in results:
            self._total_bumps += 1
            if result.success:
                self._successful_bumps += 1
                await self._db.update_last_bumped(result.thread_id)
                logger.info(f"Database updated for thread {result.thread_id}")
        
        # Send notifications if requested
        if chat_id:
            await self._send_bump_notifications(chat_id, results)
        
        # Log summary
        success_count = sum(1 for r in results if r.success)
        logger.info(
            f"Batch bump completed: {success_count}/{len(results)} successful | "
            f"Failed: {len(results) - success_count}"
        )
        
        return results
    
    async def _send_bump_notifications(self, chat_id: int, results: list[BumpResult]) -> None:
        """Send bump result notifications to user with rate limit handling."""
        for i, result in enumerate(results, 1):
            try:
                await self._bot.send_message(
                    chat_id,
                    f"[{i}/{len(results)}] {result.message}"
                )
                
                # Delay between notifications to avoid rate limits
                if i < len(results):
                    await asyncio.sleep(NOTIFICATION_DELAY_SECONDS)
                    
            except TelegramRetryAfter as e:
                # Handle rate limit - wait and retry
                logger.warning(f"Rate limit hit, waiting {e.retry_after} seconds")
                await asyncio.sleep(e.retry_after)
                
                # Retry sending this message
                try:
                    await self._bot.send_message(
                        chat_id,
                        f"[{i}/{len(results)}] {result.message}"
                    )
                except Exception as retry_error:
                    logger.error(f"Failed to send notification after retry: {retry_error}")
                    
            except Exception as e:
                logger.error(f"Failed to send notification: {e}")
    
    async def _auto_bump_scheduler_loop(self) -> None:
        """Automatic bump scheduler loop."""
        logger.info(
            f"Auto-bump scheduler started (interval: {self._config.scheduling.bump_interval_hours}h)"
        )
        
        while self._is_running:
            try:
                sleep_seconds = self._config.scheduling.bump_interval_hours * 3600
                logger.info(f"Next scheduled bump in {self._config.scheduling.bump_interval_hours} hours")
                
                await asyncio.sleep(sleep_seconds)
                
                if not self._is_running:
                    break
                
                logger.info("Starting scheduled bump...")
                
                threads = await self._db.get_threads_to_bump(
                    self._config.scheduling.bump_interval_hours
                )
                
                if threads:
                    logger.info(f"Found {len(threads)} threads to bump")
                    results = await self._execute_bump_with_notifications(threads)
                    success_count = sum(1 for r in results if r.success)
                    logger.info(f"Scheduled bump completed: {success_count}/{len(threads)} successful")
                else:
                    logger.info("No threads ready for scheduled bump")
                
            except asyncio.CancelledError:
                logger.info("Auto-bump loop cancelled")
                break
            except Exception as e:
                logger.error(f"Error in auto_bump_loop: {e}", exc_info=True)
                await asyncio.sleep(AUTO_BUMP_RETRY_DELAY_SECONDS)
    
    async def start(self) -> None:
        """Start bot and all services."""
        try:
            # Initialize services
            await self._db.connect()
            logger.info("Database connected")
            
            await self._api.start()
            logger.info("API client started")
            
            self._start_time = datetime.now()
            self._is_running = True
            
            # Start auto-bump loop if enabled
            if self._config.scheduling.enable_auto_bump:
                asyncio.create_task(self._auto_bump_scheduler_loop())
            
            logger.info("Bot started successfully!")
            await self._dp.start_polling(self._bot)
            
        except Exception as e:
            logger.error(f"Error starting bot: {e}", exc_info=True)
            # Cleanup on startup failure
            await self._cleanup_resources()
            raise
    
    async def stop(self) -> None:
        """Stop bot and cleanup resources."""
        logger.info("Stopping bot...")
        self._is_running = False
        await self._cleanup_resources()
        logger.info("Bot stopped")
    
    async def _cleanup_resources(self) -> None:
        """Cleanup all resources safely."""
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
    """Main entry point."""
    try:
        config = Config.load()
        logger.info("Configuration loaded successfully")
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
