#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import asyncio
import json
import logging
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Any
from aiohttp_socks import ProxyConnector
from aiohttp import ClientSession, ClientTimeout

import aiohttp
from aiogram import Bot, Dispatcher, types
from aiogram.contrib.middlewares.logging import LoggingMiddleware
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, ParseMode
from aiogram.utils import executor

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# Состояния для FSM
class ConfigStates(StatesGroup):
    waiting_for_api_url = State()
    waiting_for_api_key = State()

class Database:
    """Класс для работы с базой данных"""
    
    def __init__(self, db_path: str = "bot_data.db"):
        self.db_path = db_path
        self.init_db()
    
    def init_db(self):
        """Инициализация базы данных"""
        with sqlite3.connect(self.db_path) as conn:
            # Таблица для кэша услуг
            conn.execute("""
                CREATE TABLE IF NOT EXISTS services (
                    service_id TEXT PRIMARY KEY,
                    name TEXT,
                    category TEXT,
                    rate TEXT,
                    min TEXT,
                    max TEXT,
                    type TEXT,
                    description TEXT,
                    dripfeed TEXT,
                    last_updated TIMESTAMP
                )
            """)
            
            # Таблица для конфигурации бота
            conn.execute("""
                CREATE TABLE IF NOT EXISTS bot_config (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            """)
            
            # Таблица для администраторов
            conn.execute("""
                CREATE TABLE IF NOT EXISTS admins (
                    chat_id TEXT PRIMARY KEY
                )
            """)
            
            conn.commit()
    
    def save_services(self, services: List[Dict]):
        """Сохранение услуг в кэш"""
        with sqlite3.connect(self.db_path) as conn:
            for service in services:
                conn.execute("""
                    INSERT OR REPLACE INTO services 
                    (service_id, name, category, rate, min, max, type, description, dripfeed, last_updated)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    str(service.get('service', '')),
                    service.get('name', ''),
                    service.get('category', ''),
                    str(service.get('rate', '0')),
                    str(service.get('min', '0')),
                    str(service.get('max', '0')),
                    service.get('type', ''),
                    service.get('desc', ''),
                    str(service.get('dripfeed', '0')),
                    datetime.now().isoformat()
                ))
            conn.commit()
    
    def load_services(self) -> Dict[str, Dict]:
        """Загрузка услуг из кэша"""
        services = {}
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute("SELECT * FROM services")
            for row in cursor:
                services[row['service_id']] = dict(row)
        return services
    
    def save_config(self, key: str, value: str):
        """Сохранение конфигурации"""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT OR REPLACE INTO bot_config (key, value)
                VALUES (?, ?)
            """, (key, value))
            conn.commit()
    
    def load_config(self, key: str) -> Optional[str]:
        """Загрузка конфигурации"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("SELECT value FROM bot_config WHERE key = ?", (key,))
            row = cursor.fetchone()
            return row[0] if row else None
    
    def add_admin(self, chat_id: int):
        """Добавление администратора"""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("INSERT OR IGNORE INTO admins (chat_id) VALUES (?)", (str(chat_id),))
            conn.commit()
    
    def get_admins(self) -> List[str]:
        """Получение списка администраторов"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("SELECT chat_id FROM admins")
            return [row[0] for row in cursor.fetchall()]

class ProxyManager:
    """Класс для управления прокси"""
    
    def __init__(self, config_file: str = "proxy_config.json"):
        self.config_file = config_file
        self.proxy_url = None
        self.proxy_dict = None
        self.load_config()
    
    def load_config(self):
        """Загрузка конфигурации прокси из файла"""
        if Path(self.config_file).exists():
            try:
                with open(self.config_file, 'r', encoding='utf-8') as f:
                    config = json.load(f)
                    self.proxy_url = config.get('proxy_url', '')
                    if self.proxy_url:
                        self.parse_proxy_url()
                        logger.info(f"Прокси загружен: {self.proxy_url}")
                    else:
                        logger.info("Прокси не настроен")
            except Exception as e:
                logger.error(f"Ошибка загрузки прокси: {e}")
                self.proxy_url = None
        else:
            self.create_default_config()
    
    def parse_proxy_url(self):
        """Парсинг URL прокси для разных типов"""
        if not self.proxy_url:
            self.proxy_dict = None
            return
        
        # Поддержка форматов:
        # http://user:pass@host:port
        # https://user:pass@host:port
        # socks5://user:pass@host:port
        # http://host:port
        # socks5://host:port
        self.proxy_dict = {"proxy": self.proxy_url}
    
    def create_default_config(self):
        """Создание файла конфигурации по умолчанию"""
        default_config = {
            "proxy_url": "",
            "description": "Форматы прокси:\n- HTTP: http://username:password@host:port или http://host:port\n- HTTPS: https://username:password@host:port\n- SOCKS5: socks5://username:password@host:port",
            "examples": [
                "http://user:pass@192.168.1.1:8080",
                "socks5://user:pass@127.0.0.1:9050",
                "http://proxy.example.com:3128"
            ]
        }
        with open(self.config_file, 'w', encoding='utf-8') as f:
            json.dump(default_config, f, indent=4, ensure_ascii=False)
        logger.info(f"Создан файл конфигурации прокси: {self.config_file}")
        self.proxy_url = None
        self.proxy_dict = None
    
    def get_proxy_connector(self):
        """Получение коннектора для aiohttp с поддержкой прокси"""
        if not self.proxy_url:
            return None
        
        try:
            # Для SOCKS5 прокси используем aiohttp_socs
            if self.proxy_url.startswith('socks5://'):
                from aiohttp_socks import ProxyConnector
                return ProxyConnector.from_url(self.proxy_url)
            else:
                # Для HTTP/HTTPS прокси используем обычный коннектор с proxy параметром
                return None
        except Exception as e:
            logger.error(f"Ошибка создания прокси коннектора: {e}")
            return None
    
    def get_proxy_dict(self):
        """Получение словаря прокси для aiohttp ClientSession"""
        if not self.proxy_url:
            return None
        
        # Для HTTP/HTTPS прокси возвращаем словарь
        if not self.proxy_url.startswith('socks5://'):
            return {"proxy": self.proxy_url}
        return None

class ServiceMonitor:
    """Монитор для отслеживания изменений в услугах"""
    
    def __init__(self, bot_token: str, db: Database):
        self.bot_token = bot_token
        self.db = db
        self.proxy_manager = ProxyManager()
        self.is_running = True
        self.bot = None
    
    async def init_bot(self):
        """Инициализация бота"""
        self.bot = Bot(token=self.bot_token)
    
    async def fetch_services(self, api_url: str, api_key: str) -> Optional[List[Dict]]:
        """Получение услуг из API через прокси"""
        full_url = f"{api_url}?action=services&key={api_key}"
        
        connector = self.proxy_manager.get_proxy_connector()
        proxy_dict = self.proxy_manager.get_proxy_dict()
        
        timeout = ClientTimeout(total=30)
        
        try:
            if connector:
                # Используем SOCKS5 прокси
                async with ClientSession(connector=connector, timeout=timeout) as session:
                    async with session.get(full_url) as response:
                        if response.status == 200:
                            data = await response.json()
                            logger.info(f"Успешно получено {len(data)} услуг через SOCKS5 прокси")
                            return data
                        else:
                            logger.error(f"Ошибка API: {response.status}")
                            return None
            elif proxy_dict:
                # Используем HTTP/HTTPS прокси
                async with ClientSession(timeout=timeout) as session:
                    async with session.get(full_url, **proxy_dict) as response:
                        if response.status == 200:
                            data = await response.json()
                            logger.info(f"Успешно получено {len(data)} услуг через HTTP прокси")
                            return data
                        else:
                            logger.error(f"Ошибка API: {response.status}")
                            return None
            else:
                # Без прокси
                async with ClientSession(timeout=timeout) as session:
                    async with session.get(full_url) as response:
                        if response.status == 200:
                            data = await response.json()
                            logger.info(f"Успешно получено {len(data)} услуг без прокси")
                            return data
                        else:
                            logger.error(f"Ошибка API: {response.status}")
                            return None
        except Exception as e:
            logger.error(f"Ошибка при запросе к API: {e}")
            return None
    
    def compare_services(self, old_services: Dict[str, Dict], new_services: List[Dict]) -> List[Dict]:
        """Сравнение старого и нового списка услуг для обнаружения изменений"""
        changes = []
        new_services_dict = {str(s.get('service', '')): s for s in new_services}
        
        # Проверка на добавленные и измененные услуги
        for service_id, new_service in new_services_dict.items():
            if service_id not in old_services:
                # Добавлена новая услуга
                changes.append({
                    'type': 'added',
                    'service': new_service
                })
                logger.info(f"Обнаружена новая услуга: {new_service.get('name')}")
            else:
                old_service = old_services[service_id]
                changes_in_service = []
                
                # Проверка изменений
                if old_service.get('name') != new_service.get('name'):
                    changes_in_service.append({
                        'field': 'name',
                        'old': old_service.get('name'),
                        'new': new_service.get('name')
                    })
                
                if old_service.get('rate') != str(new_service.get('rate', '0')):
                    changes_in_service.append({
                        'field': 'rate',
                        'old': old_service.get('rate'),
                        'new': str(new_service.get('rate', '0'))
                    })
                
                if old_service.get('description', '') != new_service.get('desc', ''):
                    changes_in_service.append({
                        'field': 'description',
                        'old': old_service.get('description', ''),
                        'new': new_service.get('desc', '')
                    })
                
                if changes_in_service:
                    changes.append({
                        'type': 'modified',
                        'service': new_service,
                        'changes': changes_in_service
                    })
                    logger.info(f"Обнаружены изменения в услуге: {new_service.get('name')}")
        
        # Проверка на удаленные услуги
        for service_id, old_service in old_services.items():
            if service_id not in new_services_dict:
                changes.append({
                    'type': 'removed',
                    'service': old_service
                })
                logger.info(f"Обнаружено удаление услуги: {old_service.get('name')}")
        
        return changes
    
    async def send_notification(self, chat_id: int, change: Dict):
        """Отправка уведомления об изменении в Telegram"""
        try:
            if change['type'] == 'added':
                message = (
                    f"🆕 <b>Добавлена новая услуга!</b>\n\n"
                    f"📝 <b>Название:</b> {change['service'].get('name')}\n"
                    f"🆔 <b>ID:</b> {change['service'].get('service')}\n"
                    f"💰 <b>Цена:</b> {change['service'].get('rate')} руб.\n"
                    f"📊 <b>Категория:</b> {change['service'].get('category')}\n"
                    f"📈 <b>Мин/Макс:</b> {change['service'].get('min')} / {change['service'].get('max')}\n"
                    f"📝 <b>Описание:</b> {change['service'].get('desc', 'Нет описания')}"
                )
                
            elif change['type'] == 'removed':
                message = (
                    f"❌ <b>Удалена услуга!</b>\n\n"
                    f"📝 <b>Название:</b> {change['service'].get('name')}\n"
                    f"🆔 <b>ID:</b> {change['service'].get('service_id')}\n"
                    f"💰 <b>Цена:</b> {change['service'].get('rate')} руб."
                )
                
            elif change['type'] == 'modified':
                message = f"✏️ <b>Изменения в услуге!</b>\n\n"
                message += f"📝 <b>Название:</b> {change['service'].get('name')}\n"
                message += f"🆔 <b>ID:</b> {change['service'].get('service')}\n\n"
                
                for ch in change['changes']:
                    if ch['field'] == 'name':
                        message += f"📛 <b>Изменение названия:</b>\n"
                        message += f"   <s>{ch['old']}</s>\n"
                        message += f"   ➡️ <b>{ch['new']}</b>\n\n"
                    elif ch['field'] == 'rate':
                        old_rate = float(ch['old']) if ch['old'].replace('.', '').isdigit() else ch['old']
                        new_rate = float(ch['new']) if ch['new'].replace('.', '').isdigit() else ch['new']
                        message += f"💰 <b>Изменение цены:</b>\n"
                        message += f"   <s>{old_rate}</s> руб.\n"
                        message += f"   ➡️ <b>{new_rate}</b> руб.\n\n"
                    elif ch['field'] == 'description':
                        message += f"📝 <b>Изменение описания:</b>\n"
                        message += f"   <s>{ch['old'][:50]}...</s>\n"
                        message += f"   ➡️ <b>{ch['new'][:50]}...</b>\n\n"
            
            await self.bot.send_message(chat_id, message, parse_mode=ParseMode.HTML)
            logger.info(f"Уведомление отправлено в чат {chat_id}")
            
        except Exception as e:
            logger.error(f"Ошибка при отправке уведомления: {e}")
    
    async def check_services(self):
        """Проверка услуг и отправка уведомлений об изменениях"""
        api_url = self.db.load_config('api_url')
        api_key = self.db.load_config('api_key')
        
        if not api_url or not api_key:
            logger.warning("API URL или ключ не настроены")
            return
        
        logger.info("Начинаю проверку услуг...")
        
        # Получаем текущие услуги из API
        new_services = await self.fetch_services(api_url, api_key)
        
        if new_services is None:
            logger.error("Не удалось получить услуги из API")
            return
        
        # Загружаем кэшированные услуги
        old_services = self.db.load_services()
        
        # Сравниваем и находим изменения
        changes = self.compare_services(old_services, new_services)
        
        # Если есть изменения, сохраняем новые данные в кэш
        if changes:
            self.db.save_services(new_services)
            logger.info(f"Обнаружено {len(changes)} изменений")
            
            # Отправляем уведомления всем администраторам
            admins = self.db.get_admins()
            for change in changes:
                for admin_id in admins:
                    await self.send_notification(int(admin_id), change)
        else:
            logger.info("Изменений не обнаружено")
    
    async def start_monitoring(self):
        """Запуск мониторинга услуг"""
        await self.init_bot()
        logger.info("Мониторинг услуг запущен")
        
        while self.is_running:
            try:
                await self.check_services()
                # Ждем 10 минут перед следующей проверкой
                for _ in range(600):
                    if not self.is_running:
                        break
                    await asyncio.sleep(1)
            except Exception as e:
                logger.error(f"Ошибка в цикле мониторинга: {e}")
                await asyncio.sleep(60)
    
    def stop_monitoring(self):
        """Остановка мониторинга"""
        self.is_running = False
        logger.info("Мониторинг услуг остановлен")

class TelegramBot:
    """Основной класс Telegram бота"""
    
    def __init__(self, token: str):
        self.token = token
        self.db = Database()
        self.bot = Bot(token=token)
        self.dp = Dispatcher(self.bot)
        self.monitor = None
        self.monitor_task = None
        self.setup_handlers()
    
    def setup_handlers(self):
        """Настройка обработчиков команд"""
        
        # Клавиатура главного меню
        main_keyboard = ReplyKeyboardMarkup(resize_keyboard=True)
        main_keyboard.add(KeyboardButton("🔧 Настройки"), KeyboardButton("📊 Статус"))
        main_keyboard.add(KeyboardButton("🔄 Проверить сейчас"), KeyboardButton("ℹ️ Помощь"))
        
        @self.dp.message_handler(commands=['start'])
        async def cmd_start(message: types.Message):
            # Добавляем пользователя как администратора при первом запуске
            self.db.add_admin(message.chat.id)
            
            welcome_text = (
                "🤖 <b>Добро пожаловать в бот мониторинга услуг!</b>\n\n"
                "Этот бот отслеживает изменения в API услуг и уведомляет вас о:\n"
                "• Добавлении новых услуг\n"
                "• Удалении услуг\n"
                "• Изменении цен\n"
                "• Изменении названий\n"
                "• Изменении описаний\n\n"
                "📌 <b>Для начала работы:</b>\n"
                "1. Нажмите кнопку '🔧 Настройки'\n"
                "2. Введите URL API\n"
                "3. Введите API ключ\n\n"
                "⚙️ <b>Прокси настраивается в файле proxy_config.json</b>\n\n"
                "Бот будет автоматически проверять услуги каждые 10 минут"
            )
            await message.answer(welcome_text, parse_mode=ParseMode.HTML, reply_markup=main_keyboard)
        
        @self.dp.message_handler(lambda message: message.text == "🔧 Настройки")
        async def settings_menu(message: types.Message):
            keyboard = ReplyKeyboardMarkup(resize_keyboard=True)
            keyboard.add(KeyboardButton("📡 Настроить API URL"), KeyboardButton("🔑 Настроить API Key"))
            keyboard.add(KeyboardButton("📊 Текущие настройки"), KeyboardButton("🔙 Назад"))
            
            await message.answer("⚙️ <b>Настройки бота:</b>\n\nВыберите действие:", 
                               parse_mode=ParseMode.HTML, reply_markup=keyboard)
        
        @self.dp.message_handler(lambda message: message.text == "📡 Настроить API URL")
        async def setup_api_url(message: types.Message, state: FSMContext):
            await message.answer("📡 <b>Введите URL API:</b>\n\nПример: https://stream-promotion.ru/api/v2\n\n"
                               "Бот автоматически добавит ?action=services&key=ВАШ_КЛЮЧ", 
                               parse_mode=ParseMode.HTML)
            await ConfigStates.waiting_for_api_url.set()
        
        @self.dp.message_handler(state=ConfigStates.waiting_for_api_url)
        async def save_api_url(message: types.Message, state: FSMContext):
            api_url = message.text.strip()
            if not api_url.startswith(('http://', 'https://')):
                api_url = 'https://' + api_url
            
            self.db.save_config('api_url', api_url)
            await message.answer("✅ API URL успешно сохранен!", parse_mode=ParseMode.HTML)
            await state.finish()
        
        @self.dp.message_handler(lambda message: message.text == "🔑 Настроить API Key")
        async def setup_api_key(message: types.Message, state: FSMContext):
            await message.answer("🔑 <b>Введите API ключ:</b>", parse_mode=ParseMode.HTML)
            await ConfigStates.waiting_for_api_key.set()
        
        @self.dp.message_handler(state=ConfigStates.waiting_for_api_key)
        async def save_api_key(message: types.Message, state: FSMContext):
            api_key = message.text.strip()
            self.db.save_config('api_key', api_key)
            await message.answer("✅ API ключ успешно сохранен!", parse_mode=ParseMode.HTML)
            await state.finish()
        
        @self.dp.message_handler(lambda message: message.text == "📊 Текущие настройки")
        async def show_settings(message: types.Message):
            api_url = self.db.load_config('api_url') or "Не настроен"
            api_key = self.db.load_config('api_key') or "Не настроен"
            api_key_masked = api_key[:8] + "..." if len(api_key) > 8 else api_key
            
            # Информация о прокси
            proxy_manager = ProxyManager()
            proxy_status = "✅ Активен" if proxy_manager.proxy_url else "❌ Не настроен"
            proxy_url = proxy_manager.proxy_url or "Не настроен"
            
            settings_text = (
                "📊 <b>Текущие настройки:</b>\n\n"
                f"📡 API URL: {api_url}\n"
                f"🔑 API Key: {api_key_masked}\n"
                f"🌐 Прокси: {proxy_status}\n"
                f"🔗 Адрес прокси: {proxy_url}\n\n"
                f"⏱ Интервал проверки: 10 минут\n"
                f"💾 База данных: bot_data.db\n\n"
                f"📁 Файл конфигурации прокси: proxy_config.json"
            )
            await message.answer(settings_text, parse_mode=ParseMode.HTML)
        
        @self.dp.message_handler(lambda message: message.text == "🔄 Проверить сейчас")
        async def check_now(message: types.Message):
            status_msg = await message.answer("🔄 Проверка услуг... Пожалуйста, подождите.")
            
            if self.monitor:
                try:
                    await self.monitor.check_services()
                    await status_msg.edit_text("✅ Проверка завершена! Изменения отправлены администраторам (если были).")
                except Exception as e:
                    await status_msg.edit_text(f"❌ Ошибка при проверке: {str(e)}")
            else:
                await status_msg.edit_text("❌ Монитор не запущен. Обратитесь к администратору.")
        
        @self.dp.message_handler(lambda message: message.text == "📊 Статус")
        async def show_status(message: types.Message):
            services_count = len(self.db.load_services())
            last_services = self.db.load_services()
            last_update = "Неизвестно"
            
            if last_services:
                last_update = max([s.get('last_updated', 'Никогда') for s in last_services.values()])
            
            status_text = (
                "📊 <b>Статус мониторинга:</b>\n\n"
                f"📦 Кешировано услуг: {services_count}\n"
                f"🕐 Последнее обновление: {last_update}\n"
                f"⏱ Интервал проверки: 10 минут\n"
                f"🔄 Мониторинг: {'Активен' if self.monitor and self.monitor.is_running else 'Остановлен'}\n\n"
                f"👥 Администраторов: {len(self.db.get_admins())}"
            )
            await message.answer(status_text, parse_mode=ParseMode.HTML)
        
        @self.dp.message_handler(lambda message: message.text == "ℹ️ Помощь")
        async def help_command(message: types.Message):
            help_text = (
                "ℹ️ <b>Помощь по боту</b>\n\n"
                "<b>Команды и кнопки:</b>\n"
                "• 🔧 Настройки - настройка API URL и ключа\n"
                "• 📊 Статус - просмотр статуса мониторинга\n"
                "• 🔄 Проверить сейчас - ручная проверка услуг\n\n"
                "<b>Настройка прокси:</b>\n"
                "1. Отредактируйте файл proxy_config.json\n"
                "2. Укажите proxy_url в формате:\n"
                "   • HTTP: http://user:pass@host:port\n"
                "   • SOCKS5: socks5://user:pass@host:port\n"
                "3. Перезапустите бота\n\n"
                "<b>Установка на сервер:</b>\n"
                "Следуйте инструкции в README.md"
            )
            await message.answer(help_text, parse_mode=ParseMode.HTML)
        
        @self.dp.message_handler(lambda message: message.text == "🔙 Назад")
        async def back_to_main(message: types.Message):
            main_keyboard = ReplyKeyboardMarkup(resize_keyboard=True)
            main_keyboard.add(KeyboardButton("🔧 Настройки"), KeyboardButton("📊 Статус"))
            main_keyboard.add(KeyboardButton("🔄 Проверить сейчас"), KeyboardButton("ℹ️ Помощь"))
            await message.answer("Главное меню:", reply_markup=main_keyboard)
    
    async def start_monitoring(self):
        """Запуск мониторинга в фоне"""
        self.monitor = ServiceMonitor(self.token, self.db)
        self.monitor_task = asyncio.create_task(self.monitor.start_monitoring())
    
    def run(self):
        """Запуск бота"""
        loop = asyncio.get_event_loop()
        
        # Запускаем мониторинг
        loop.create_task(self.start_monitoring())
        
        # Запускаем бота
        executor.start_polling(self.dp, skip_updates=True)

def main():
    """Главная функция"""
    # Загрузка токена бота из файла или переменной окружения
    config_file = Path("bot_config.json")
    token = None
    
    if config_file.exists():
        with open(config_file, 'r') as f:
            config = json.load(f)
            token = config.get('bot_token')
    
    if not token:
        print("=" * 60)
        print("Telegram Bot Monitor - Настройка")
        print("=" * 60)
        token = input("Введите токен Telegram бота: ").strip()
        
        # Сохраняем токен
        with open(config_file, 'w') as f:
            json.dump({'bot_token': token}, f, indent=4)
        print(f"✅ Токен сохранен в {config_file}")
    
    print("\n" + "=" * 60)
    print("Запуск бота...")
    print("=" * 60)
    print(f"📁 Файл конфигурации прокси: proxy_config.json")
    print(f"💾 База данных: bot_data.db")
    print(f"📝 Лог-файл: bot.log")
    print("=" * 60 + "\n")
    
    bot = TelegramBot(token)
    
    try:
        bot.run()
    except KeyboardInterrupt:
        print("\n\n⏹ Бот остановлен пользователем")
    except Exception as e:
        print(f"\n❌ Ошибка: {e}")
        sys.exit(1)

if __name__ == '__main__':
    main()
