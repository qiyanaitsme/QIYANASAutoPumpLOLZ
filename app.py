import asyncio
import logging
import sys
from datetime import datetime
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, StateFilter
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

from config_manager import Config
from api_client import APIClient
from database import Database


logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('bot.log', encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)


class BotStates(StatesGroup):
    waiting_for_threads = State()


class AutoBumpBot:
    
    def __init__(self):
        self.config = Config.load()
        self.bot = Bot(token=self.config.bot_token)
        self.storage = MemoryStorage()
        self.dp = Dispatcher(storage=self.storage)
        self.api = APIClient(self.config.api_base_url, self.config.api_token)
        self.db = Database(self.config.db_path)
        self.is_running = False
        self.total_bumps = 0
        self.successful_bumps = 0
        self.start_time = None
        
        self.dp.message.register(self.cmd_start, Command("start"))
        self.dp.callback_query.register(self.cb_add_thread, F.data == "add_thread")
        self.dp.callback_query.register(self.cb_list_threads, F.data == "list_threads")
        self.dp.callback_query.register(self.cb_delete_menu, F.data == "delete_menu")
        self.dp.callback_query.register(self.cb_delete_thread, F.data.startswith("delete_"))
        self.dp.callback_query.register(self.cb_bump_now, F.data == "bump_now")
        self.dp.callback_query.register(self.cb_stats, F.data == "stats")
        self.dp.message.register(self.msg_add_threads, StateFilter(BotStates.waiting_for_threads))
    
    def get_main_keyboard(self) -> InlineKeyboardMarkup:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="➕ Добавить темы", callback_data="add_thread"),
                InlineKeyboardButton(text="📋 Список тем", callback_data="list_threads")
            ],
            [
                InlineKeyboardButton(text="🗑️ Удалить тему", callback_data="delete_menu"),
                InlineKeyboardButton(text="🚀 Поднять темы", callback_data="bump_now")
            ],
            [
                InlineKeyboardButton(text="📊 Статистика", callback_data="stats"),
                InlineKeyboardButton(text="👤 Автор", url="https://lolz.live/kqlol/")
            ]
        ])
        return keyboard
    
    async def send_main_menu(self, chat_id: int, text: str = None):
        if text is None:
            text = "Выберите действие:"
        
        await self.bot.send_message(
            chat_id,
            text,
            reply_markup=self.get_main_keyboard()
        )
    
    async def cmd_start(self, message: Message):
        await message.answer_photo(
            photo="https://wallpapers-clan.com/wp-content/uploads/2024/04/dark-anime-girl-with-red-eyes-desktop-wallpaper-preview.jpg",
            caption=(
                "🤖 <b>QIYANA AUTO-BUMP BOT</b>\n\n"
                "Бот для автоматического поднятия тем на Lolz.live\n\n"
                f"⏰ Автоподнятие каждые <b>{self.config.bump_interval_hours}ч</b>"
            ),
            parse_mode="HTML"
        )
        await self.send_main_menu(message.chat.id)
    
    async def cb_add_thread(self, callback: CallbackQuery, state: FSMContext):
        await callback.answer()
        await callback.message.answer(
            "📝 Введите ID тем через запятую для добавления:\n"
            "Пример: <code>12345, 67890, 11111</code>",
            parse_mode="HTML"
        )
        await state.set_state(BotStates.waiting_for_threads)
    
    async def msg_add_threads(self, message: Message, state: FSMContext):
        try:
            thread_ids = [tid.strip() for tid in message.text.split(',') if tid.strip()]
            
            if not thread_ids:
                await message.answer("❌ Не указаны ID тем")
                return
            
            status_msg = await message.answer(f"⏳ Добавляю {len(thread_ids)} тем...")
            
            added = []
            errors = []
            
            for thread_id in thread_ids:
                if not thread_id.isdigit():
                    errors.append(f"❌ {thread_id} - неверный формат")
                    continue
                
                thread_info = await self.api.get_thread_info(thread_id)
                
                if not thread_info:
                    errors.append(f"❌ {thread_id} - не найдена")
                    continue
                
                success = await self.db.add_thread(thread_id, thread_info.title)
                
                if success:
                    added.append(f"✅ {thread_id} - {thread_info.title}")
                else:
                    errors.append(f"⚠️ {thread_id} - уже добавлена")
            
            result = f"📊 Результат добавления:\n\n"
            
            if added:
                result += "✅ <b>Добавлено:</b>\n" + "\n".join(added) + "\n\n"
            
            if errors:
                result += "❌ <b>Ошибки:</b>\n" + "\n".join(errors)
            
            await status_msg.edit_text(result, parse_mode="HTML")
            await self.send_main_menu(message.chat.id)
            
        except Exception as e:
            logger.error(f"Error adding threads: {e}", exc_info=True)
            await message.answer(f"❌ Ошибка: {str(e)}")
        finally:
            await state.clear()
    
    async def cb_list_threads(self, callback: CallbackQuery):
        await callback.answer()
        
        try:
            threads = await self.db.get_all_threads()
            
            if not threads:
                await callback.message.answer("📭 Список тем пуст\n\nДобавьте темы через кнопку ➕")
                await self.send_main_menu(callback.message.chat.id)
                return
            
            text = f"📋 <b>Список тем ({len(threads)}):</b>\n\n"
            
            for thread in threads:
                status = "🆕 Новая"
                if thread.last_bumped:
                    try:
                        dt = datetime.fromisoformat(thread.last_bumped.replace('Z', '+00:00'))
                        status = f"✅ {dt.strftime('%d.%m.%Y %H:%M')}"
                    except:
                        status = f"✅ {thread.last_bumped[:16]}"
                
                text += f"<b>{thread.id}</b> - {thread.title}\n{status}\n\n"
            
            await callback.message.answer(text, parse_mode="HTML")
            await self.send_main_menu(callback.message.chat.id)
        
        except Exception as e:
            logger.error(f"Error listing threads: {e}", exc_info=True)
            await callback.message.answer(f"❌ Ошибка: {str(e)}")
    
    async def cb_delete_menu(self, callback: CallbackQuery):
        await callback.answer()
        
        try:
            threads = await self.db.get_all_threads()
            
            if not threads:
                await callback.message.answer("📭 Список тем пуст")
                await self.send_main_menu(callback.message.chat.id)
                return
            
            buttons = []
            for thread in threads:
                button_text = f"{thread.id} - {thread.title[:30]}"
                buttons.append([
                    InlineKeyboardButton(
                        text=button_text,
                        callback_data=f"delete_{thread.id}"
                    )
                ])
            
            keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
            
            await callback.message.answer(
                "🗑️ Выберите тему для удаления:",
                reply_markup=keyboard
            )
        
        except Exception as e:
            logger.error(f"Error showing delete menu: {e}", exc_info=True)
            await callback.message.answer(f"❌ Ошибка: {str(e)}")
    
    async def cb_delete_thread(self, callback: CallbackQuery):
        try:
            thread_id = callback.data.split('_')[1]
            success = await self.db.delete_thread(thread_id)
            
            if success:
                await callback.answer(f"✅ Тема {thread_id} удалена", show_alert=True)
                await callback.message.answer(f"✅ Тема {thread_id} успешно удалена из базы данных")
                await self.send_main_menu(callback.message.chat.id)
            else:
                await callback.answer(f"❌ Тема {thread_id} не найдена", show_alert=True)
        
        except Exception as e:
            logger.error(f"Error deleting thread: {e}", exc_info=True)
            await callback.answer(f"❌ Ошибка: {str(e)}", show_alert=True)
    
    async def cb_bump_now(self, callback: CallbackQuery):
        await callback.answer()
        
        try:
            threads = await self.db.get_threads_to_bump(0)
            
            if not threads:
                await callback.message.answer("📭 Нет тем для поднятия\n\nДобавьте темы через кнопку ➕")
                await self.send_main_menu(callback.message.chat.id)
                return
            
            status_msg = await callback.message.answer(
                f"🚀 Начинаю поднятие {len(threads)} тем...\n\n"
                "Подождите, это может занять некоторое время..."
            )
            
            results = await self.bump_threads(threads, send_to_chat=callback.message.chat.id)
            
            success_count = sum(1 for r in results if r.success)
            
            await status_msg.edit_text(
                f"📊 <b>Поднятие завершено!</b>\n\n"
                f"✅ Успешно: {success_count}\n"
                f"❌ Ошибок: {len(threads) - success_count}\n"
                f"📝 Всего тем: {len(threads)}",
                parse_mode="HTML"
            )
            
            await self.send_main_menu(callback.message.chat.id)
        
        except Exception as e:
            logger.error(f"Error bumping threads: {e}", exc_info=True)
            await callback.message.answer(f"❌ Ошибка: {str(e)}")
    
    async def cb_stats(self, callback: CallbackQuery):
        await callback.answer()
        
        try:
            threads = await self.db.get_all_threads()
            threads_ready = await self.db.get_threads_to_bump(self.config.bump_interval_hours)
            
            uptime = "0 минут"
            if self.start_time:
                delta = datetime.now() - self.start_time
                hours = delta.seconds // 3600
                minutes = (delta.seconds % 3600) // 60
                if hours > 0:
                    uptime = f"{hours}ч {minutes}м"
                else:
                    uptime = f"{minutes}м"
            
            success_rate = 0
            if self.total_bumps > 0:
                success_rate = (self.successful_bumps / self.total_bumps) * 100
            
            text = (
                "📊 <b>Статистика бота</b>\n\n"
                f"📋 <b>Темы:</b>\n"
                f"• Всего в списке: {len(threads)}\n"
                f"• Готовы к поднятию: {len(threads_ready)}\n"
                f"• Ожидают времени: {len(threads) - len(threads_ready)}\n\n"
                f"🚀 <b>Поднятия:</b>\n"
                f"• Всего попыток: {self.total_bumps}\n"
                f"• Успешных: {self.successful_bumps}\n"
                f"• Успешность: {success_rate:.1f}%\n\n"
                f"⚙️ <b>Настройки:</b>\n"
                f"• Интервал: {self.config.bump_interval_hours}ч\n"
                f"• Задержка между темами: 2с\n\n"
                f"⏱️ <b>Работа:</b>\n"
                f"• Время работы: {uptime}"
            )
            
            await callback.message.answer(text, parse_mode="HTML")
            await self.send_main_menu(callback.message.chat.id)
        
        except Exception as e:
            logger.error(f"Error showing stats: {e}", exc_info=True)
            await callback.message.answer(f"❌ Ошибка: {str(e)}")
    
    async def bump_threads(self, threads, send_to_chat=None):
        results = []
        
        for i, thread in enumerate(threads, 1):
            result = await self.api.bump_thread(thread.id)
            results.append(result)
            
            self.total_bumps += 1
            if result.success:
                self.successful_bumps += 1
                await self.db.update_last_bumped(thread.id)
            
            if send_to_chat:
                await self.bot.send_message(
                    send_to_chat,
                    f"[{i}/{len(threads)}] {result.message}"
                )
            
            logger.info(f"Bump result: {result.message}")
            
            await asyncio.sleep(2)
        
        return results
    
    async def auto_bump_loop(self):
        logger.info(f"Auto-bump scheduler started (interval: {self.config.bump_interval_hours}h)")
        logger.info("Waiting for manual first bump or timer...")
        
        while self.is_running:
            try:
                sleep_seconds = self.config.bump_interval_hours * 3600
                logger.info(f"Next scheduled bump in {self.config.bump_interval_hours} hours")
                await asyncio.sleep(sleep_seconds)
                
                if not self.is_running:
                    break
                
                logger.info("Starting scheduled bump...")
                
                threads = await self.db.get_threads_to_bump(self.config.bump_interval_hours)
                
                if threads:
                    logger.info(f"Found {len(threads)} threads to bump")
                    results = await self.bump_threads(threads)
                    success_count = sum(1 for r in results if r.success)
                    logger.info(f"Scheduled bump completed: {success_count}/{len(threads)} successful")
                else:
                    logger.info("No threads ready for scheduled bump")
                
            except Exception as e:
                logger.error(f"Error in auto_bump_loop: {e}", exc_info=True)
                await asyncio.sleep(60)
    
    async def start(self):
        try:
            await self.db.init()
            logger.info("Database initialized")
            
            await self.api.start()
            logger.info("API client started")
            
            self.start_time = datetime.now()
            
            self.is_running = True
            asyncio.create_task(self.auto_bump_loop())
            
            logger.info("Bot started successfully!")
            await self.dp.start_polling(self.bot)
            
        except Exception as e:
            logger.error(f"Error starting bot: {e}", exc_info=True)
            raise
    
    async def stop(self):
        logger.info("Stopping bot...")
        self.is_running = False
        await self.api.close()
        await self.bot.session.close()
        logger.info("Bot stopped")


async def main():
    bot = AutoBumpBot()
    try:
        await bot.start()
    except KeyboardInterrupt:
        logger.info("Bot interrupted by user")
    except Exception as e:
        logger.error(f"Bot crashed: {e}", exc_info=True)
    finally:
        await bot.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Application terminated")
