import asyncio
import logging
import html
import time
from datetime import datetime, timedelta
from typing import Dict, Set

from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import Message, ChatPermissions
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramAPIError
from pydantic_settings import BaseSettings, SettingsConfigDict

# ==========================================
# 1. КОНФИГУРАЦИЯ
# ==========================================
class Settings(BaseSettings):
    BOT_TOKEN: str
    CHANNEL_ID: str
    WARNINGS_LIMIT: int = 3
    MUTE_MINUTES: int = 5

    model_config = SettingsConfigDict(env_file='.env', env_file_encoding='utf-8')

settings = Settings()

# ==========================================
# 2. IN-MEMORY ХРАНИЛИЩА И КЭШ
# ==========================================
# Структура: {chat_id: {user_id: count}}
warnings_count: Dict[int, Dict[int, int]] = {}
# Структура: {chat_id: {user_id: warning_message_id}}
last_warning_msgs: Dict[int, Dict[int, int]] = {}

class AdminCache:
    """Легковесный кэш для администраторов, чтобы не дергать API на каждом сообщении"""
    def __init__(self, ttl_seconds: int = 300):
        self._cache: Dict[int, Dict[str, any]] = {}
        self.ttl = ttl_seconds

    def get_admins(self, chat_id: int) -> Set[int] | None:
        cached = self._cache.get(chat_id)
        if cached and (time.time() - cached['timestamp'] < self.ttl):
            return cached['admins']
        return None

    def set_admins(self, chat_id: int, admin_ids: Set[int]):
        self._cache[chat_id] = {
            'timestamp': time.time(),
            'admins': admin_ids
        }

admin_cache = AdminCache(ttl_seconds=300)  # Кэшируем на 5 минут

# ==========================================
# 3. ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ==========================================
async def is_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    """Проверяет, является ли пользователь администратором группы."""
    admins = admin_cache.get_admins(chat_id)
    
    if admins is None:
        try:
            chat_admins = await bot.get_chat_administrators(chat_id)
            admins = {admin.user.id for admin in chat_admins}
            admin_cache.set_admins(chat_id, admins)
        except TelegramAPIError as e:
            logging.error(f"Не удалось получить список админов чата {chat_id}: {e}")
            return False
            
    return user_id in admins

async def is_subscribed(bot: Bot, channel_id: str, user_id: int) -> bool:
    """Проверяет подписку пользователя на канал."""
    try:
        member = await bot.get_chat_member(chat_id=channel_id, user_id=user_id)
        return member.status not in ("left", "kicked")
    except TelegramAPIError as e:
        logging.error(f"Ошибка проверки подписки {user_id} на {channel_id}: {e}")
        # Если бот не в канале или канал не существует - пропускаем пользователя, чтобы не блокировать чат
        return True 

def clear_user_state(chat_id: int, user_id: int):
    """Очищает данные пользователя из in-memory структур."""
    if chat_id in warnings_count and user_id in warnings_count[chat_id]:
        del warnings_count[chat_id][user_id]
    if chat_id in last_warning_msgs and user_id in last_warning_msgs[chat_id]:
        del last_warning_msgs[chat_id][user_id]

# ==========================================
# 4. БИЗНЕС-ЛОГИКА (РОУТЕР)
# ==========================================
router = Router()

@router.message(F.chat.type.in_({"group", "supergroup"}))
async def handle_group_message(message: Message, bot: Bot):
    user = message.from_user
    chat_id = message.chat.id
    user_id = user.id

    if user.is_bot:
        return

    # Игнорируем админов
    if await is_admin(bot, chat_id, user_id):
        return

    # Инициализация хранилищ для чата, если их еще нет
    warnings_count.setdefault(chat_id, {})
    last_warning_msgs.setdefault(chat_id, {})

    # Проверка подписки
    subscribed = await is_subscribed(bot, settings.CHANNEL_ID, user_id)

    if subscribed:
        # Если подписался и написал сообщение — подчищаем старые предупреждения (Умный анти-спам)
        old_warn_msg_id = last_warning_msgs[chat_id].get(user_id)
        if old_warn_msg_id:
            try:
                await bot.delete_message(chat_id, old_warn_msg_id)
            except TelegramBadRequest:
                pass  # Сообщение уже удалено пользователем или другим админом
            
        clear_user_state(chat_id, user_id)
        return

    # --- ЛОГИКА ДЛЯ НЕПОДПИСАННЫХ ---
    
    # 1. Удаляем сообщение нарушителя
    try:
        await message.delete()
    except TelegramBadRequest:
        logging.warning(f"Нет прав для удаления сообщения в {chat_id}")
        return # Если не можем удалять — выходим, чтобы не спамить предупреждениями

    # 2. Удаляем предыдущее предупреждение от бота (Умный анти-спам)
    old_warn_msg_id = last_warning_msgs[chat_id].get(user_id)
    if old_warn_msg_id:
        try:
            await bot.delete_message(chat_id, old_warn_msg_id)
        except TelegramBadRequest:
            pass 

    # 3. Увеличиваем счетчик нарушений
    current_warnings = warnings_count[chat_id].get(user_id, 0) + 1
    warnings_count[chat_id][user_id] = current_warnings

    safe_name = html.escape(user.full_name)
    user_mention = f'<a href="tg://user?id={user_id}">{safe_name}</a>'

    # 4. Проверка на мут
    if current_warnings >= settings.WARNINGS_LIMIT:
        mute_until = datetime.now() + timedelta(minutes=settings.MUTE_MINUTES)
        try:
            await bot.restrict_chat_member(
                chat_id=chat_id,
                user_id=user_id,
                permissions=ChatPermissions(can_send_messages=False),
                until_date=mute_until
            )
            await message.answer(
                f"🛑 {user_mention} подпишись на канал @FurriStik чтобы писать в чат"
            )
        except TelegramBadRequest as e:
            logging.error(f"Не удалось выдать мут: {e}")
        finally:
            clear_user_state(chat_id, user_id)
    
    # 5. Выдача предупреждения
    else:
        try:
            warn_msg = await message.answer(
                f"⚠️ {user_mention}, чтобы писать в чат, подпишись на канал: t.me/FurriStik\n"
            )
            # Сохраняем ID нового предупреждения
            last_warning_msgs[chat_id][user_id] = warn_msg.message_id
        except TelegramAPIError as e:
            logging.error(f"Не удалось отправить предупреждение: {e}")

# ==========================================
# 5. ТОЧКА ВХОДА
# ==========================================
async def main():
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    bot = Bot(
        token=settings.BOT_TOKEN, 
        default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )
    dp = Dispatcher()
    dp.include_router(router)

    # Пропускаем накопившиеся апдейты
    await bot.delete_webhook(drop_pending_updates=True)
    
    logging.info("Бот успешно запущен!")
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Бот остановлен.")
      
