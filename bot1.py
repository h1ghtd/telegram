import asyncio
import json
import logging
from datetime import datetime
from typing import Dict, List, Optional, Any
from collections import defaultdict

import aiohttp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes

from config import (
    TELEGRAM_TOKEN, ADMIN_IDS, PROXY_URL, PROXY_ENABLED,
    API_URL_BASE, API_KEY, MONITOR_INTERVAL, CACHE_COMPARE_INTERVAL,
    validate_config
)

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Хранилище данных
class ServiceCache:
    def __init__(self):
        self.services: Dict[str, Dict] = {}  # service_id -> service_data
        self.last_update: Optional[datetime] = None
        self.comparison_cache: Dict[str, Dict] = {}  # Кэш для сравнения (каждые 30 минут)
        self.last_comparison_update: Optional[datetime] = None
    
    def update(self, services: List[Dict]):
        """Обновление основного кэша"""
        new_services = {}
        for service in services:
            service_id = str(service.get('service'))
            if service_id:
                new_services[service_id] = service
        
        self.services = new_services
        self.last_update = datetime.now()
    
    def update_comparison_cache(self):
        """Обновление кэша для сравнения (берется текущий кэш)"""
        self.comparison_cache = self.services.copy()
        self.last_comparison_update = datetime.now()
        logger.info("Comparison cache updated with {} services".format(len(self.comparison_cache)))
    
    def compare_and_get_changes(self, new_services: List[Dict]) -> List[Dict]:
        """Сравнивает новые данные с кэшем и возвращает изменения"""
        changes = []
        new_services_dict = {}
        
        for service in new_services:
            service_id = str(service.get('service'))
            if service_id:
                new_services_dict[service_id] = service
        
        # Проверяем добавленные и изменённые услуги
        for service_id, new_data in new_services_dict.items():
            if service_id not in self.comparison_cache:
                # Новая услуга
                changes.append({
                    'type': 'added',
                    'service_id': service_id,
                    'data': new_data
                })
            else:
                old_data = self.comparison_cache[service_id]
                field_changes = self._get_field_changes(old_data, new_data)
                if field_changes:
                    changes.append({
                        'type': 'modified',
                        'service_id': service_id,
                        'changes': field_changes,
                        'old_data': old_data,
                        'new_data': new_data
                    })
        
        # Проверяем удалённые услуги
        for service_id in self.comparison_cache:
            if service_id not in new_services_dict:
                changes.append({
                    'type': 'deleted',
                    'service_id': service_id,
                    'data': self.comparison_cache[service_id]
                })
        
        return changes
    
    def _get_field_changes(self, old: Dict, new: Dict) -> Dict:
        """Определяет изменения в полях: name, rate, desc"""
        changes = {}
        
        # Поля для отслеживания
        tracked_fields = ['name', 'rate', 'desc']
        
        for field in tracked_fields:
            old_val = old.get(field, '')
            new_val = new.get(field, '')
            
            # Преобразуем None в пустую строку
            old_val = old_val if old_val is not None else ''
            new_val = new_val if new_val is not None else ''
            
            if str(old_val) != str(new_val):
                changes[field] = {
                    'old': old_val,
                    'new': new_val
                }
        
        return changes

# Глобальный экземпляр кэша
service_cache = ServiceCache()

# Настройки для сессии
class SessionManager:
    def __init__(self):
        self.session = None
        self.proxy = PROXY_URL if PROXY_ENABLED else None
    
    async def get_session(self):
        if self.session is None:
            connector = None
            if self.proxy:
                # Для различных типов прокси
                if self.proxy.startswith('socks5'):
                    try:
                        from aiohttp_socks import ProxyConnector
                        connector = ProxyConnector.from_url(self.proxy)
                    except ImportError:
                        logger.warning("aiohttp-socks not installed, falling back to HTTP proxy")
                        connector = aiohttp.TCPConnector()
            self.session = aiohttp.ClientSession(connector=connector)
        return self.session
    
    async def close(self):
        if self.session:
            await self.session.close()
            self.session = None

session_manager = SessionManager()

async def fetch_services() -> Optional[List[Dict]]:
    """Запрос услуг через API"""
    if not API_URL_BASE or not API_KEY:
        logger.error("API_URL_BASE or API_KEY not configured")
        return None
    
    url = f"{API_URL_BASE}?action=services&key={API_KEY}"
    
    try:
        session = await session_manager.get_session()
        async with session.get(url, timeout=30) as response:
            if response.status == 200:
                data = await response.json()
                logger.info(f"Fetched {len(data) if isinstance(data, list) else 0} services from API")
                return data
            else:
                logger.error(f"API error: {response.status}")
                return None
    except Exception as e:
        logger.error(f"Error fetching services: {e}")
        return None

async def monitor_services():
    """Мониторинг услуг"""
    logger.info("Starting service monitoring...")
    
    while True:
        try:
            # Получаем свежие данные
            services = await fetch_services()
            
            if services is None:
                logger.warning("Failed to fetch services")
                await asyncio.sleep(MONITOR_INTERVAL)
                continue
            
            # Проверяем, нужно ли обновить кэш для сравнения
            need_comparison_update = (
                service_cache.last_comparison_update is None or
                (datetime.now() - service_cache.last_comparison_update).total_seconds() >= CACHE_COMPARE_INTERVAL
            )
            
            # Если кэш сравнения пуст, сначала заполняем его
            if not service_cache.comparison_cache:
                service_cache.update(services)
                service_cache.update_comparison_cache()
                logger.info("Initial cache filled")
            else:
                # Сравниваем с кэшем
                changes = service_cache.compare_and_get_changes(services)
                
                if changes:
                    logger.info(f"Found {len(changes)} changes")
                    await send_changes_to_admins(changes)
                
                # Обновляем основной кэш
                service_cache.update(services)
                
                # Если пришло время, обновляем кэш для сравнения
                if need_comparison_update:
                    service_cache.update_comparison_cache()
                    logger.info("Comparison cache refreshed")
            
        except Exception as e:
            logger.error(f"Monitor error: {e}")
        
        await asyncio.sleep(MONITOR_INTERVAL)

async def send_changes_to_admins(changes: List[Dict]):
    """Отправка уведомлений администраторам"""
    for admin_id in ADMIN_IDS:
        for change in changes:
            message = format_change_message(change)
            if message:
                await send_telegram_message(admin_id, message)

def format_change_message(change: Dict) -> str:
    """Форматирование сообщения об изменении"""
    change_type = change.get('type')
    service_id = change.get('service_id')
    
    if change_type == 'added':
        data = change.get('data', {})
        return (
            f"🟢 **НОВАЯ УСЛУГА**\n\n"
            f"ID: `{service_id}`\n"
            f"Название: {data.get('name', 'N/A')}\n"
            f"Цена: {data.get('rate', 'N/A')} ₽\n"
            f"Описание: {data.get('desc', 'Нет описания')[:100]}"
        )
    
    elif change_type == 'deleted':
        data = change.get('data', {})
        return (
            f"🔴 **УДАЛЕНА УСЛУГА**\n\n"
            f"ID: `{service_id}`\n"
            f"Название: {data.get('name', 'N/A')}\n"
            f"Цена была: {data.get('rate', 'N/A')} ₽"
        )
    
    elif change_type == 'modified':
        changes = change.get('changes', {})
        old_data = change.get('old_data', {})
        new_data = change.get('new_data', {})
        
        message_parts = [f"🟡 **ИЗМЕНЕНИЕ УСЛУГИ**\n\nID: `{service_id}`\n"]
        
        if 'name' in changes:
            message_parts.append(f"📝 Название:\n~~{changes['name']['old']}~~\n→ {changes['name']['new']}")
        
        if 'rate' in changes:
            message_parts.append(f"💰 Цена:\n~~{changes['rate']['old']}~~ ₽\n→ {changes['rate']['new']} ₽")
        
        if 'desc' in changes:
            old_desc = changes['desc']['old'][:100] if changes['desc']['old'] else 'Нет описания'
            new_desc = changes['desc']['new'][:100] if changes['desc']['new'] else 'Нет описания'
            message_parts.append(f"📄 Описание:\n~~{old_desc}~~\n→ {new_desc}")
        
        return '\n\n'.join(message_parts)
    
    return None

async def send_telegram_message(chat_id: int, text: str):
    """Отправка сообщения через прокси"""
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "Markdown"
        }
        
        session = await session_manager.get_session()
        async with session.post(url, json=payload, timeout=30) as response:
            if response.status != 200:
                logger.error(f"Failed to send message: {await response.text()}")
    except Exception as e:
        logger.error(f"Error sending message: {e}")

# Команды бота
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start"""
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ У вас нет доступа к этому боту")
        return
    
    await update.message.reply_text(
        "🤖 **Бот мониторинга услуг**\n\n"
        "Бот отслеживает изменения услуг:\n"
        "• Добавление новых услуг\n"
        "• Удаление услуг\n"
        "• Изменение цены (rate)\n"
        "• Изменение названия (name)\n"
        "• Изменение описания (desc)\n\n"
        "**Команды:**\n"
        "/status - Статус мониторинга\n"
        "/config - Текущая конфигурация\n"
        "/services - Список услуг\n"
        "/seturl <url> - Установить API URL\n"
        "/setkey <key> - Установить API Key\n"
        "/check - Проверить API сейчас\n"
        "/help - Помощь",
        parse_mode='Markdown'
    )

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Статус мониторинга"""
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ Нет доступа")
        return
    
    status_text = f"""
📊 **Статус мониторинга**

• API URL: `{API_URL_BASE if API_URL_BASE else '❌ Не настроен'}`
• API Key: `{'✅ Установлен' if API_KEY else '❌ Не настроен'}`
• Прокси: `{'✅ Включен' if PROXY_ENABLED else '❌ Выключен'}`
• Прокси URL: `{PROXY_URL if PROXY_URL else 'Не указан'}`

• Услуг в кэше: `{len(service_cache.services)}`
• Последнее обновление: `{service_cache.last_update if service_cache.last_update else 'Никогда'}`

• Интервал мониторинга: `{MONITOR_INTERVAL // 60} минут`
• Интервал сравнения с кэшем: `{CACHE_COMPARE_INTERVAL // 60} минут`
"""
    await update.message.reply_text(status_text, parse_mode='Markdown')

async def show_services(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать список услуг"""
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ Нет доступа")
        return
    
    if not service_cache.services:
        await update.message.reply_text("📭 Кэш услуг пуст. Данные ещё не загружены.")
        return
    
    # Показываем первые 10 услуг
    services_list = list(service_cache.services.values())[:10]
    message = "📋 **Услуги (первые 10):**\n\n"
    
    for service in services_list:
        message += f"**ID:** `{service.get('service')}`\n"
        message += f"**Название:** {service.get('name', 'N/A')[:50]}\n"
        message += f"**Цена:** {service.get('rate', 'N/A')} ₽\n"
        message += f"**Описание:** {service.get('desc', 'Нет')[:50]}\n"
        message += "---\n"
    
    if len(service_cache.services) > 10:
        message += f"\n*Всего услуг: {len(service_cache.services)}*"
    
    await update.message.reply_text(message, parse_mode='Markdown')

async def check_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Принудительная проверка API"""
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ Нет доступа")
        return
    
    await update.message.reply_text("🔄 Выполняю проверку API...")
    
    services = await fetch_services()
    
    if services is None:
        await update.message.reply_text("❌ Ошибка при обращении к API")
        return
    
    # Сравниваем с кэшем (не обновляя его)
    changes = service_cache.compare_and_get_changes(services)
    
    if changes:
        await update.message.reply_text(f"✅ Найдено {len(changes)} изменений. Отправляю уведомления...")
        await send_changes_to_admins(changes)
        # Обновляем кэш после ручной проверки
        service_cache.update(services)
    else:
        await update.message.reply_text("✅ Изменений не обнаружено")
    
    await update.message.reply_text(f"📊 Всего услуг: {len(services)}")

async def set_api_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Установка API URL"""
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ Нет доступа")
        return
    
    if not context.args:
        await update.message.reply_text("❌ Использование: /seturl <url>")
        return
    
    new_url = context.args[0]
    # Сохраняем в .env файл
    try:
        import os
        with open('.env', 'r') as f:
            lines = f.readlines()
        
        with open('.env', 'w') as f:
            url_set = False
            for line in lines:
                if line.startswith('API_URL_BASE='):
                    f.write(f'API_URL_BASE={new_url}\n')
                    url_set = True
                else:
                    f.write(line)
            if not url_set:
                f.write(f'API_URL_BASE={new_url}\n')
        
        os.environ['API_URL_BASE'] = new_url
        # Обновляем глобальную переменную (нужно перезагрузить модуль)
        await update.message.reply_text(f"✅ API URL установлен: {new_url}\n\n⚠️ Для применения изменений в мониторинге перезапустите бота")
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка сохранения: {e}")

async def set_api_key(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Установка API Key"""
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ Нет доступа")
        return
    
    if not context.args:
        await update.message.reply_text("❌ Использование: /setkey <key>")
        return
    
    new_key = context.args[0]
    try:
        import os
        with open('.env', 'r') as f:
            lines = f.readlines()
        
        with open('.env', 'w') as f:
            key_set = False
            for line in lines:
                if line.startswith('API_KEY='):
                    f.write(f'API_KEY={new_key}\n')
                    key_set = True
                else:
                    f.write(line)
            if not key_set:
                f.write(f'API_KEY={new_key}\n')
        
        os.environ['API_KEY'] = new_key
        await update.message.reply_text(f"✅ API Key установлен\n\n⚠️ Для применения изменений в мониторинге перезапустите бота")
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка сохранения: {e}")

async def show_config(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать текущую конфигурацию"""
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ Нет доступа")
        return
    
    config_text = f"""
⚙️ **Текущая конфигурация**

• API URL: `{API_URL_BASE if API_URL_BASE else '❌ Не настроен'}`
• API Key: `{'✅ Установлен' if API_KEY else '❌ Не настроен'}`

Для изменения используйте:
• `/seturl <url>` - установить API URL
• `/setkey <key>` - установить API Key
"""
    await update.message.reply_text(config_text, parse_mode='Markdown')

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Помощь"""
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ Нет доступа")
        return
    
    help_text = """
📚 **Доступные команды:**

/start - Начало работы
/status - Статус мониторинга
/services - Список услуг (первые 10)
/config - Текущая конфигурация
/seturl <url> - Установить API URL
/setkey <key> - Установить API Key
/check - Принудительная проверка API
/help - Эта справка

**Форматы сообщений:**
• 🟢 Новая услуга - зелёный индикатор
• 🔴 Удаление - красный индикатор  
• 🟡 Изменение - жёлтый индикатор

Изменения цен отображаются с зачёркиванием старой цены и стрелкой → к новой.
"""
    await update.message.reply_text(help_text, parse_mode='Markdown')

async def main():
    """Главная функция"""
    # Валидация конфигурации
    try:
        validate_config()
    except ValueError as e:
        logger.error(f"Configuration error: {e}")
        logger.error("Please set TELEGRAM_TOKEN and ADMIN_IDS in .env file")
        return
    
    # Создаём приложение
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    
    # Добавляем обработчики
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("services", show_services))
    application.add_handler(CommandHandler("config", show_config))
    application.add_handler(CommandHandler("seturl", set_api_url))
    application.add_handler(CommandHandler("setkey", set_api_key))
    application.add_handler(CommandHandler("check", check_now))
    application.add_handler(CommandHandler("help", help_command))
    
    # Запускаем мониторинг в фоне
    asyncio.create_task(monitor_services())
    
    # Запускаем бота
    logger.info("Starting bot...")
    await application.initialize()
    await application.start()
    
    # Используем polling с прокси если нужно
    if PROXY_ENABLED and PROXY_URL:
        logger.info(f"Using proxy: {PROXY_URL}")
        await application.updater.start_polling()
    else:
        await application.updater.start_polling()
    
    logger.info("Bot is running!")
    
    # Ждём завершения
    try:
        await asyncio.Event().wait()
    except KeyboardInterrupt:
        logger.info("Shutting down...")
        await session_manager.close()
        await application.stop()

if __name__ == "__main__":
    asyncio.run(main())
