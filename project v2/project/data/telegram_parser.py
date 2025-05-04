from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes
)
from datetime import datetime, timedelta
from .text_processor import TextProcessor
from .db_session import create_session
from project.models.TelegramModel import TelegramChat
import logging
import random
import string
from typing import Dict, Optional
from project.models.users import User


class TelegramBot:
    def __init__(self, token: str):
        self.token = token
        self.processor = TextProcessor()
        self.active_verifications: Dict[int, dict] = {}  # {user_id: {code: str, expires_at: datetime}}
        logging.basicConfig(
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            level=logging.INFO
        )
        self.logger = logging.getLogger(__name__)

    def clean_expired_codes(self):
        """Очищает просроченные коды верификации"""
        now = datetime.now()
        expired_users = [
            user_id for user_id, data in self.active_verifications.items()
            if data["expires_at"] < now
        ]
        for user_id in expired_users:
            del self.active_verifications[user_id]
            self.logger.debug(f"Cleared expired code for user {user_id}")

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /start"""
        chat = update.effective_chat
        user = update.effective_user

        if chat.type == "private":
            await update.message.reply_text(
                "🔐 Для привязки Telegram к сайту:\n"
                "1. На сайте нажми 'Привязать Telegram'\n"
                "2. Полученный код введи здесь командой /verify КОД\n\n"
            )
            return

        db = create_session()
        chat_in_db = db.query(TelegramChat).filter_by(chat_id=str(chat.id)).first()

        if chat_in_db:
            await update.message.reply_text(
                f"Этот чат уже зарегистрирован!\n"
                f"ID чата: {chat.id}\n"
                f"Статус: {'Активен' if chat_in_db.is_active else 'Неактивен'}"
            )
        else:
            await self._register_new_chat(chat, user, update)

    async def verify(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик верификации кода"""
        if update.message.chat.type != "private":
            return

        self.clean_expired_codes()
        user_id = update.message.from_user.id
        args = context.args

        if not args or len(args[0]) != 6:
            await update.message.reply_text("❌ Код должен состоять из 6 символов. Пример: /verify A1B2C3")
            return

        code = args[0].upper()
        db = create_session()

        try:
            # Ищем пользователя с таким кодом
            user = db.query(User).filter(
                User.telegram_verify_code == code,
                User.telegram_code_expires > datetime.now()
            ).first()

            if user:
                if user.telegram_id and user.telegram_id != user_id:
                    await update.message.reply_text(
                        "❌ Этот код уже используется другим аккаунтом Telegram"
                    )
                    return

                user.telegram_id = user_id
                user.is_telegram_verified = True
                user.telegram_verify_code = None
                user.telegram_code_expires = None
                db.commit()

                await update.message.reply_text(
                    "✅ Ваш Telegram успешно привязан к аккаунту!\n\n"
                    f"Привязанный аккаунт: {user.email or user.name}"
                )
            else:
                await update.message.reply_text("❌ Неверный или просроченный код")

        except Exception as e:
            db.rollback()
            self.logger.error(f"Ошибка при верификации: {e}")
            await update.message.reply_text("⚠ Произошла ошибка при обработке запроса")

        finally:
            db.close()

    async def get_verification_code(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Генерирует и возвращает код верификации (только для ЛС)"""
        if update.message.chat.type != "private":
            return

        self.clean_expired_codes()  # Очищаем просроченные коды

        user_id = update.message.from_user.id

        # Если у пользователя уже есть активный код
        if user_id in self.active_verifications:
            code_data = self.active_verifications[user_id]
            await update.message.reply_text(
                f"У вас уже есть активный код: {code_data['code']}\n"
                f"⏳ Действует до: {code_data['expires_at'].strftime('%H:%M:%S')}\n\n"
                f"Используйте его на сайте или дождитесь истечения срока."
            )
            return

        code = self.generate_verification_code()
        expires_at = datetime.now() + timedelta(minutes=3)

        self.active_verifications[user_id] = {
            "code": code,
            "expires_at": expires_at
        }

        self.logger.info(f"Generated verification code {code} for user {user_id}")

        await update.message.reply_text(
            f"🔑 Ваш код верификации: {code}\n"
            f"⏳ Действует до: {expires_at.strftime('%H:%M:%S')}\n\n"
            f"Введите его на сайте для привязки аккаунта."
        )

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик обычных сообщений"""
        chat_id = str(update.effective_chat.id)
        db = create_session()

        chat = db.query(TelegramChat).filter_by(chat_id=chat_id).first()
        if not chat or not chat.is_active:
            return

        try:
            new_entry = self.processor.process_text(
                text=update.message.text,
                source="telegram",
                chat_id=chat_id,
                author=str(update.message.from_user.id))
            db.add(new_entry)
            db.commit()
        except Exception as e:
            db.rollback()
            self.logger.error(f"Ошибка сохранения сообщения: {e}")

    async def get_chat_id(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Команда для получения ID чата"""
        chat = update.effective_chat
        await update.message.reply_text(
            f"ID этого чата: {chat.id}\n"
            f"Тип чата: {'группа' if chat.type in ['group', 'supergroup'] else 'личный'}\n"
            f"Название: {getattr(chat, 'title', 'нет')}"
        )

    async def _register_new_chat(self, chat, user, update):
        """Регистрация нового чата в БД"""
        db = create_session()

        try:
            if chat.type == "private":
                title_parts = []
                if user.first_name:
                    title_parts.append(user.first_name)
                if user.last_name:
                    title_parts.append(user.last_name)
                title = ' '.join(title_parts) if title_parts else f"Пользователь {chat.id}"

                if hasattr(user, 'username') and user.username:
                    title += f" (@{user.username})"
            else:
                title = chat.title if hasattr(chat, 'title') and chat.title else f'Чат {chat.id}'

            new_chat = TelegramChat(
                chat_id=str(chat.id),
                title=title,
                user_id=user.id,
                is_active=False,
                chat_type=chat.type,
                created_at=datetime.now()
            )

            db.add(new_chat)
            db.commit()

            response = (
                f"🎉 Чат успешно зарегистрирован!\n"
                f"ID: {chat.id}\n"
                f"Тип: {'Личные сообщения' if chat.type == 'private' else 'Группа/Канал'}\n"
                f"Название: {title}\n\n"
                f"Для активации парсинга перейдите в панель управления."
            )

        except Exception as e:
            db.rollback()
            response = f"❌ Ошибка регистрации чата: {str(e)}"
            self.logger.error(f"Chat registration error: {str(e)}", exc_info=True)

        await update.message.reply_text(response)

    async def on_chat_join(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик добавления бота в чат"""
        chat = update.effective_chat
        user = update.effective_user
        db = create_session()

        if not db.query(TelegramChat).filter_by(chat_id=str(chat.id)).first():
            title = chat.title if chat.type in ['group', 'supergroup', 'channel'] else user.full_name

            new_chat = TelegramChat(
                chat_id=str(chat.id),
                title=title,
                user_id=str(user.id),
                is_active=False,
                chat_type=chat.type,
                created_at=datetime.now()
            )

            db.add(new_chat)
            db.commit()

            response_msg = (
                f"Чат {'группы' if chat.type != 'private' else 'ЛС'} "
                f"добавлен: {title}\n"
                f"Активируйте парсинг в панели управления."
            )

            await update.message.reply_text(response_msg)

    def run(self):
        """Запуск бота"""
        app = Application.builder().token(self.token).build()

        # Добавляем обработчики команд
        handlers = [
            CommandHandler("start", self.start),
            CommandHandler("chat_id", self.get_chat_id),
            CommandHandler("verify", self.verify),
            MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message),
            MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, self.on_chat_join)
        ]

        for handler in handlers:
            app.add_handler(handler)

        self.logger.info("Бот запущен и готов к работе!")
        app.run_polling()