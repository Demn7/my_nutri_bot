import logging
import sqlite3
import aiohttp
import csv
import io
# import matplotlib.pyplot as plt
import datetime
import random
import os  # ДОБАВЛЕНО для Bothost.ru
from flask import Flask
import threading
from datetime import time, timedelta
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, ConversationHandler, filters
from telegram.request import HTTPXRequest

# ИМПОРТ НОВОГО МОДУЛЯ ДЛЯ СЧЕТЧИКА
import database

# Минимальный веб-сервер для Render
web_app = Flask('')

@web_app.route('/')
@web_app.route('/health')
def health():
    return "OK", 200

def run_web():
    port = int(os.environ.get("PORT", 8080))
    web_app.run(host='0.0.0.0', port=port)

# Запускаем веб-сервер в отдельном потоке
threading.Thread(target=run_web, daemon=True).start()

# Настройка логирования
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Ключи API - ТЕПЕРЬ ЧЕРЕЗ ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ ДЛЯ BOTHOST.RU
CALORIE_NINJAS_API_KEY = os.getenv('CALORIE_NINJAS_API_KEY', "kq1fOCH5cJ7wk+hwSrsdBA==k5Nqdgg0JB31Essz")
DEEPSEEK_API_KEY = os.getenv('DEEPSEEK_API_KEY', "sk-your-deepseek-api-key-here")
BOT_TOKEN = os.getenv('BOT_TOKEN')   # Токен будет браться из настроек Bothost.ru

if not BOT_TOKEN:
    raise ValueError("❌ Токен бота не найден! Добавьте BOT_TOKEN в переменные окружения на Bothost.ru")

# Состояния для ConversationHandler
GENDER, AGE, HEIGHT, WEIGHT, GOAL, ACTIVITY = range(6)
MEAL_TYPE, PRODUCT_NAME, GRAMS = range(3)
WEIGHT_TRACKING = range(1)


# Клавиатура главного меню
def main_menu_keyboard():
    keyboard = [
        ['🍽 Ввести прием пищи', '📊 Статистика сегодня'],
        ['⚖️ Ввести вес', '📈 График прогресса'],
        ['💡 Рекомендации ИИ', '🎯 Мои цели'],
        ['⚙️ Настройки', '👤 Мой профиль']
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, input_field_placeholder='Выберите действие...')

# Инициализация базы данных (ваша существующая БД + новая для счетчика)
def init_db():
    conn = sqlite3.connect('nutribot.db', check_same_thread=False)
    cur = conn.cursor()

    # Таблица пользователей (ваша существующая)
    cur.execute('''
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        full_name TEXT,
        age INTEGER,
        gender TEXT,
        height INTEGER,
        weight REAL,
        goal TEXT,
        activity_level TEXT,
        daily_calories INTEGER,
        daily_protein INTEGER,
        daily_fat INTEGER,
        daily_carbs INTEGER,
        notification_time TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )
    ''')

    # Таблица дневника питания
    cur.execute('''
    CREATE TABLE IF NOT EXISTS food_diary (
        entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        date TEXT DEFAULT CURRENT_DATE,
        meal_type TEXT,
        product_name TEXT,
        grams REAL,
        calories REAL,
        protein REAL,
        fat REAL,
        carbs REAL,
        FOREIGN KEY (user_id) REFERENCES users (user_id)
    )
    ''')

    # Таблица отслеживания веса
    cur.execute('''
    CREATE TABLE IF NOT EXISTS weight_tracking (
        track_id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        date TEXT DEFAULT CURRENT_DATE,
        weight REAL,
        FOREIGN KEY (user_id) REFERENCES users (user_id)
    )
    ''')

    # Таблица настроек
    cur.execute('''
    CREATE TABLE IF NOT EXISTS user_settings (
        user_id INTEGER PRIMARY KEY,
        notifications_enabled BOOLEAN DEFAULT TRUE,
        notification_time TEXT DEFAULT '09:00',
        FOREIGN KEY (user_id) REFERENCES users (user_id)
    )
    ''')

    # Таблица активности (ваша существующая)
    cur.execute('''
    CREATE TABLE IF NOT EXISTS user_activity (
        activity_id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        date TEXT DEFAULT CURRENT_DATE,
        commands_used INTEGER DEFAULT 0,
        foods_added INTEGER DEFAULT 0,
        weight_entries INTEGER DEFAULT 0,
        last_active TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users (user_id),
        UNIQUE(user_id, date)
    )
    ''')

    conn.commit()
    conn.close()

    # Инициализация новой таблицы для счетчика посещений
    database.init_db()


# Функция для получения соединения с БД
def get_db_connection():
    conn = sqlite3.connect('nutribot.db', check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


# Отслеживание активности (ваша существующая функция)
async def track_activity(user_id: int, action: str):
    """Отслеживание активности пользователя"""
    conn = get_db_connection()
    cur = conn.cursor()
    today = datetime.date.today().isoformat()

    if action == 'food':
        cur.execute('''
            INSERT INTO user_activity (user_id, date, foods_added, commands_used, last_active)
            VALUES (?, ?, 1, 1, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id, date) 
            DO UPDATE SET 
                foods_added = foods_added + 1,
                commands_used = commands_used + 1,
                last_active = CURRENT_TIMESTAMP
        ''', (user_id, today))
    elif action == 'weight':
        cur.execute('''
            INSERT INTO user_activity (user_id, date, weight_entries, commands_used, last_active)
            VALUES (?, ?, 1, 1, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id, date) 
            DO UPDATE SET 
                weight_entries = weight_entries + 1,
                commands_used = commands_used + 1,
                last_active = CURRENT_TIMESTAMP
        ''', (user_id, today))
    else:  # command
        cur.execute('''
            INSERT INTO user_activity (user_id, date, commands_used, last_active)
            VALUES (?, ?, 1, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id, date) 
            DO UPDATE SET 
                commands_used = commands_used + 1,
                last_active = CURRENT_TIMESTAMP
        ''', (user_id, today))

    conn.commit()
    conn.close()


# РАСШИРЕННАЯ БАЗА ПРОДУКТОВ (300+ продуктов)
LOCAL_PRODUCTS = {
    # ХЛЕБ И ВЫПЕЧКА 🍞 (расширено)
    'хлеб': {'name': 'Хлеб пшеничный', 'calories': 265, 'protein_g': 8, 'fat_total_g': 3, 'carbohydrates_total_g': 50},
    'хлеб ржаной': {'name': 'Хлеб ржаной', 'calories': 165, 'protein_g': 6, 'fat_total_g': 1,
                    'carbohydrates_total_g': 34},
    'батон': {'name': 'Батон нарезной', 'calories': 262, 'protein_g': 8, 'fat_total_g': 3, 'carbohydrates_total_g': 50},
    'лаваш': {'name': 'Лаваш', 'calories': 275, 'protein_g': 9, 'fat_total_g': 1, 'carbohydrates_total_g': 56},
    'булка': {'name': 'Булка сдобная', 'calories': 339, 'protein_g': 8, 'fat_total_g': 4, 'carbohydrates_total_g': 70},
    'булочка': {'name': 'Булочка сдобная', 'calories': 339, 'protein_g': 8, 'fat_total_g': 4,
                'carbohydrates_total_g': 70},
    'багет': {'name': 'Багет французский', 'calories': 285, 'protein_g': 11, 'fat_total_g': 2,
              'carbohydrates_total_g': 56},
    'сухари': {'name': 'Сухари пшеничные', 'calories': 331, 'protein_g': 11, 'fat_total_g': 2,
               'carbohydrates_total_g': 72},
    'сухарики': {'name': 'Сухарики', 'calories': 400, 'protein_g': 10, 'fat_total_g': 15, 'carbohydrates_total_g': 65},
    'круассан': {'name': 'Круассан', 'calories': 406, 'protein_g': 8, 'fat_total_g': 21, 'carbohydrates_total_g': 45},
    'тост': {'name': 'Тост из белого хлеба', 'calories': 290, 'protein_g': 9, 'fat_total_g': 4,
             'carbohydrates_total_g': 55},

    # МОЛОЧНЫЕ ПРОДУКТЫ 🥛 (расширено)
    'молоко': {'name': 'Молоко 2.5%', 'calories': 52, 'protein_g': 3, 'fat_total_g': 2.5, 'carbohydrates_total_g': 5},
    'молоко 3.2%': {'name': 'Молоко 3.2%', 'calories': 60, 'protein_g': 3, 'fat_total_g': 3.2,
                    'carbohydrates_total_g': 5},
    'молоко 1.5%': {'name': 'Молоко 1.5%', 'calories': 45, 'protein_g': 3, 'fat_total_g': 1.5,
                    'carbohydrates_total_g': 5},
    'молоко обезжиренное': {'name': 'Молоко обезжиренное', 'calories': 35, 'protein_g': 3, 'fat_total_g': 0.1,
                            'carbohydrates_total_g': 5},
    'кефир': {'name': 'Кефир 2.5%', 'calories': 53, 'protein_g': 3, 'fat_total_g': 2.5, 'carbohydrates_total_g': 4},
    'кефир 3.2%': {'name': 'Кефир 3.2%', 'calories': 56, 'protein_g': 3, 'fat_total_g': 3.2,
                   'carbohydrates_total_g': 4},
    'кефир 1%': {'name': 'Кефир 1%', 'calories': 40, 'protein_g': 3, 'fat_total_g': 1, 'carbohydrates_total_g': 4},
    'творог': {'name': 'Творог 5%', 'calories': 121, 'protein_g': 17, 'fat_total_g': 5, 'carbohydrates_total_g': 2},
    'творог 9%': {'name': 'Творог 9%', 'calories': 159, 'protein_g': 16, 'fat_total_g': 9, 'carbohydrates_total_g': 2},
    'творог обезжиренный': {'name': 'Творог обезжиренный', 'calories': 85, 'protein_g': 18, 'fat_total_g': 0.5,
                            'carbohydrates_total_g': 2},
    'творог 2%': {'name': 'Творог 2%', 'calories': 103, 'protein_g': 18, 'fat_total_g': 2, 'carbohydrates_total_g': 2},
    'сыр': {'name': 'Сыр российский', 'calories': 364, 'protein_g': 23, 'fat_total_g': 29, 'carbohydrates_total_g': 0},
    'сыр голландский': {'name': 'Сыр голландский', 'calories': 352, 'protein_g': 26, 'fat_total_g': 27,
                        'carbohydrates_total_g': 0},
    'сыр чеддер': {'name': 'Сыр чеддер', 'calories': 402, 'protein_g': 25, 'fat_total_g': 33,
                   'carbohydrates_total_g': 1},
    'брынза': {'name': 'Брынза', 'calories': 260, 'protein_g': 22, 'fat_total_g': 19, 'carbohydrates_total_g': 0},
    'сулугуни': {'name': 'Сулугуни', 'calories': 286, 'protein_g': 20, 'fat_total_g': 22, 'carbohydrates_total_g': 0},
    'фета': {'name': 'Сыр фета', 'calories': 264, 'protein_g': 14, 'fat_total_g': 21, 'carbohydrates_total_g': 4},
    'йогурт': {'name': 'Йогурт натуральный', 'calories': 68, 'protein_g': 5, 'fat_total_g': 3,
               'carbohydrates_total_g': 4},
    'йогурт греческий': {'name': 'Йогурт греческий', 'calories': 59, 'protein_g': 10, 'fat_total_g': 0.4,
                         'carbohydrates_total_g': 3.6},
    'йогурт питьевой': {'name': 'Йогурт питьевой', 'calories': 72, 'protein_g': 3, 'fat_total_g': 2,
                        'carbohydrates_total_g': 10},
    'сметана': {'name': 'Сметана 15%', 'calories': 158, 'protein_g': 3, 'fat_total_g': 15, 'carbohydrates_total_g': 3},
    'сметана 20%': {'name': 'Сметана 20%', 'calories': 206, 'protein_g': 3, 'fat_total_g': 20,
                    'carbohydrates_total_g': 3},
    'сметана 10%': {'name': 'Сметана 10%', 'calories': 115, 'protein_g': 3, 'fat_total_g': 10,
                    'carbohydrates_total_g': 3},
    'сливки': {'name': 'Сливки 10%', 'calories': 118, 'protein_g': 3, 'fat_total_g': 10, 'carbohydrates_total_g': 4},
    'сливки 20%': {'name': 'Сливки 20%', 'calories': 205, 'protein_g': 3, 'fat_total_g': 20,
                   'carbohydrates_total_g': 4},
    'сливки 33%': {'name': 'Сливки 33%', 'calories': 322, 'protein_g': 2, 'fat_total_g': 33,
                   'carbohydrates_total_g': 3},
    'ряженка': {'name': 'Ряженка', 'calories': 54, 'protein_g': 3, 'fat_total_g': 2.5, 'carbohydrates_total_g': 4},
    'простокваша': {'name': 'Простокваша', 'calories': 58, 'protein_g': 3, 'fat_total_g': 3.2,
                    'carbohydrates_total_g': 4},

    # МЯСО И ПТИЦА 🍗 (расширено)
    'курица': {'name': 'Куриная грудка', 'calories': 113, 'protein_g': 23, 'fat_total_g': 2,
               'carbohydrates_total_g': 0},
    'куриная грудка': {'name': 'Куриная грудка', 'calories': 113, 'protein_g': 23, 'fat_total_g': 2,
                       'carbohydrates_total_g': 0},
    'куриное бедро': {'name': 'Куриное бедро', 'calories': 209, 'protein_g': 26, 'fat_total_g': 11,
                      'carbohydrates_total_g': 0},
    'куриное филе': {'name': 'Куриное филе', 'calories': 113, 'protein_g': 23, 'fat_total_g': 2,
                     'carbohydrates_total_g': 0},
    'куриные крылышки': {'name': 'Куриные крылышки', 'calories': 222, 'protein_g': 19, 'fat_total_g': 16,
                         'carbohydrates_total_g': 0},
    'индейка': {'name': 'Индейка', 'calories': 135, 'protein_g': 25, 'fat_total_g': 3, 'carbohydrates_total_g': 0},
    'индейка грудка': {'name': 'Индейка грудка', 'calories': 135, 'protein_g': 25, 'fat_total_g': 3,
                       'carbohydrates_total_g': 0},
    'индейка бедро': {'name': 'Индейка бедро', 'calories': 144, 'protein_g': 18, 'fat_total_g': 8,
                      'carbohydrates_total_g': 0},
    'говядина': {'name': 'Говядина', 'calories': 187, 'protein_g': 19, 'fat_total_g': 12, 'carbohydrates_total_g': 0},
    'говяжий фарш': {'name': 'Говяжий фарш', 'calories': 254, 'protein_g': 17, 'fat_total_g': 20,
                     'carbohydrates_total_g': 0},
    'говядина вырезка': {'name': 'Говядина вырезка', 'calories': 175, 'protein_g': 22, 'fat_total_g': 9,
                         'carbohydrates_total_g': 0},
    'свинина': {'name': 'Свинина', 'calories': 242, 'protein_g': 25, 'fat_total_g': 15, 'carbohydrates_total_g': 0},
    'свиная вырезка': {'name': 'Свиная вырезка', 'calories': 143, 'protein_g': 21, 'fat_total_g': 6,
                       'carbohydrates_total_g': 0},
    'свиной фарш': {'name': 'Свиной фарш', 'calories': 263, 'protein_g': 16, 'fat_total_g': 22,
                    'carbohydrates_total_g': 0},
    'баранина': {'name': 'Баранина', 'calories': 294, 'protein_g': 25, 'fat_total_g': 21, 'carbohydrates_total_g': 0},
    'телятина': {'name': 'Телятина', 'calories': 172, 'protein_g': 30, 'fat_total_g': 6, 'carbohydrates_total_g': 0},
    'кролик': {'name': 'Кролик', 'calories': 156, 'protein_g': 21, 'fat_total_g': 8, 'carbohydrates_total_g': 0},

    # КОЛБАСЫ И СОУСКИ 🌭
    'колбаса вареная': {'name': 'Колбаса вареная', 'calories': 257, 'protein_g': 13, 'fat_total_g': 22,
                        'carbohydrates_total_g': 3},
    'колбаса сырокопченая': {'name': 'Колбаса сырокопченая', 'calories': 473, 'protein_g': 24, 'fat_total_g': 41,
                             'carbohydrates_total_g': 0},
    'колбаса полукопченая': {'name': 'Колбаса полукопченая', 'calories': 350, 'protein_g': 16, 'fat_total_g': 30,
                             'carbohydrates_total_g': 3},
    'салями': {'name': 'Салями', 'calories': 407, 'protein_g': 21, 'fat_total_g': 34, 'carbohydrates_total_g': 1},
    'сосиски': {'name': 'Сосиски', 'calories': 233, 'protein_g': 11, 'fat_total_g': 20, 'carbohydrates_total_g': 2},
    'сардельки': {'name': 'Сардельки', 'calories': 270, 'protein_g': 12, 'fat_total_g': 24, 'carbohydrates_total_g': 2},
    'бекон': {'name': 'Бекон', 'calories': 541, 'protein_g': 37, 'fat_total_g': 42, 'carbohydrates_total_g': 1},
    'ветчина': {'name': 'Ветчина', 'calories': 145, 'protein_g': 20, 'fat_total_g': 6, 'carbohydrates_total_g': 1},
    'карбонад': {'name': 'Карбонад', 'calories': 141, 'protein_g': 18, 'fat_total_g': 7, 'carbohydrates_total_g': 1},

    # РЫБА И МОРЕПРОДУКТЫ 🐟 (расширено)
    'лосось': {'name': 'Лосось', 'calories': 208, 'protein_g': 20, 'fat_total_g': 13, 'carbohydrates_total_g': 0},
    'семга': {'name': 'Семга', 'calories': 206, 'protein_g': 22, 'fat_total_g': 12, 'carbohydrates_total_g': 0},
    'форель': {'name': 'Форель', 'calories': 148, 'protein_g': 21, 'fat_total_g': 6.6, 'carbohydrates_total_g': 0},
    'тунец': {'name': 'Тунец', 'calories': 101, 'protein_g': 23, 'fat_total_g': 1, 'carbohydrates_total_g': 0},
    'тунец консервированный': {'name': 'Тунец консервированный', 'calories': 198, 'protein_g': 29, 'fat_total_g': 8,
                               'carbohydrates_total_g': 0},
    'треска': {'name': 'Треска', 'calories': 78, 'protein_g': 18, 'fat_total_g': 1, 'carbohydrates_total_g': 0},
    'минтай': {'name': 'Минтай', 'calories': 72, 'protein_g': 16, 'fat_total_g': 0.9, 'carbohydrates_total_g': 0},
    'хек': {'name': 'Хек', 'calories': 86, 'protein_g': 17, 'fat_total_g': 2.2, 'carbohydrates_total_g': 0},
    'камбала': {'name': 'Камбала', 'calories': 83, 'protein_g': 16, 'fat_total_g': 2.6, 'carbohydrates_total_g': 0},
    'окунь': {'name': 'Окунь', 'calories': 91, 'protein_g': 19, 'fat_total_g': 0.9, 'carbohydrates_total_g': 0},
    'судак': {'name': 'Судак', 'calories': 84, 'protein_g': 19, 'fat_total_g': 0.8, 'carbohydrates_total_g': 0},
    'сельдь': {'name': 'Сельдь', 'calories': 158, 'protein_g': 18, 'fat_total_g': 9, 'carbohydrates_total_g': 0},
    'скумбрия': {'name': 'Скумбрия', 'calories': 262, 'protein_g': 19, 'fat_total_g': 21, 'carbohydrates_total_g': 0},
    'сайра': {'name': 'Сайра', 'calories': 205, 'protein_g': 20, 'fat_total_g': 13, 'carbohydrates_total_g': 0},
    'икра красная': {'name': 'Икра красная', 'calories': 251, 'protein_g': 31, 'fat_total_g': 13,
                     'carbohydrates_total_g': 1},
    'икра черная': {'name': 'Икра черная', 'calories': 264, 'protein_g': 25, 'fat_total_g': 18,
                    'carbohydrates_total_g': 4},
    'креветки': {'name': 'Креветки', 'calories': 99, 'protein_g': 21, 'fat_total_g': 1, 'carbohydrates_total_g': 1},
    'кальмар': {'name': 'Кальмар', 'calories': 92, 'protein_g': 16, 'fat_total_g': 1.4, 'carbohydrates_total_g': 3},
    'мидии': {'name': 'Мидии', 'calories': 77, 'protein_g': 11, 'fat_total_g': 2.2, 'carbohydrates_total_g': 4},
    'осьминог': {'name': 'Осьминог', 'calories': 82, 'protein_g': 15, 'fat_total_g': 1, 'carbohydrates_total_g': 2},
    'краб': {'name': 'Краб', 'calories': 87, 'protein_g': 18, 'fat_total_g': 1.5, 'carbohydrates_total_g': 0},
    'крабовые палочки': {'name': 'Крабовые палочки', 'calories': 88, 'protein_g': 17, 'fat_total_g': 0.5,
                         'carbohydrates_total_g': 0},

    # ЯЙЦА 🥚
    'яйцо': {'name': 'Яйцо куриное', 'calories': 155, 'protein_g': 13, 'fat_total_g': 11, 'carbohydrates_total_g': 1},
    'яйца': {'name': 'Яйцо куриное', 'calories': 155, 'protein_g': 13, 'fat_total_g': 11, 'carbohydrates_total_g': 1},
    'яичный белок': {'name': 'Яичный белок', 'calories': 52, 'protein_g': 11, 'fat_total_g': 0.2,
                     'carbohydrates_total_g': 1},
    'яичный желток': {'name': 'Яичный желток', 'calories': 322, 'protein_g': 16, 'fat_total_g': 27,
                      'carbohydrates_total_g': 3.6},
    'перепелиные яйца': {'name': 'Перепелиные яйца', 'calories': 168, 'protein_g': 14, 'fat_total_g': 13,
                         'carbohydrates_total_g': 0.6},

    # КРУПЫ И ЗЛАКИ 🍚 (расширено)
    'гречка': {'name': 'Гречка', 'calories': 132, 'protein_g': 4.5, 'fat_total_g': 1.3, 'carbohydrates_total_g': 27},
    'гречка отварная': {'name': 'Гречка отварная', 'calories': 101, 'protein_g': 4, 'fat_total_g': 1,
                        'carbohydrates_total_g': 21},
    'рис': {'name': 'Рис белый', 'calories': 130, 'protein_g': 2.7, 'fat_total_g': 0.3, 'carbohydrates_total_g': 28},
    'рис отварной': {'name': 'Рис отварной', 'calories': 116, 'protein_g': 2.2, 'fat_total_g': 0.5,
                     'carbohydrates_total_g': 25},
    'рис бурый': {'name': 'Рис бурый', 'calories': 111, 'protein_g': 2.6, 'fat_total_g': 0.9,
                  'carbohydrates_total_g': 23},
    'овсянка': {'name': 'Овсянка', 'calories': 68, 'protein_g': 2.4, 'fat_total_g': 1.4, 'carbohydrates_total_g': 12},
    'овсяные хлопья': {'name': 'Овсяные хлопья', 'calories': 379, 'protein_g': 13, 'fat_total_g': 6.5,
                       'carbohydrates_total_g': 67},
    'манка': {'name': 'Манка', 'calories': 328, 'protein_g': 10, 'fat_total_g': 1, 'carbohydrates_total_g': 73},
    'перловка': {'name': 'Перловка', 'calories': 123, 'protein_g': 2.3, 'fat_total_g': 0.4,
                 'carbohydrates_total_g': 28},
    'пшено': {'name': 'Пшено', 'calories': 119, 'protein_g': 3.5, 'fat_total_g': 1, 'carbohydrates_total_g': 23},
    'ячневая крупа': {'name': 'Ячневая крупа', 'calories': 313, 'protein_g': 10, 'fat_total_g': 1.3,
                      'carbohydrates_total_g': 65},
    'кукурузная крупа': {'name': 'Кукурузная крупа', 'calories': 337, 'protein_g': 8.3, 'fat_total_g': 1.2,
                         'carbohydrates_total_g': 75},
    'горох': {'name': 'Горох', 'calories': 298, 'protein_g': 21, 'fat_total_g': 2, 'carbohydrates_total_g': 53},
    'чечевица': {'name': 'Чечевица', 'calories': 116, 'protein_g': 9, 'fat_total_g': 0.4, 'carbohydrates_total_g': 20},
    'булгур': {'name': 'Булгур', 'calories': 342, 'protein_g': 12, 'fat_total_g': 1.3, 'carbohydrates_total_g': 76},
    'киноа': {'name': 'Киноа', 'calories': 120, 'protein_g': 4.4, 'fat_total_g': 1.9, 'carbohydrates_total_g': 21},

    # МАКАРОННЫЕ ИЗДЕЛИЯ 🍝
    'макароны': {'name': 'Макароны', 'calories': 131, 'protein_g': 5, 'fat_total_g': 1, 'carbohydrates_total_g': 25},
    'макароны отварные': {'name': 'Макароны отварные', 'calories': 158, 'protein_g': 5.8, 'fat_total_g': 0.9,
                          'carbohydrates_total_g': 30},
    'спагетти': {'name': 'Спагетти', 'calories': 158, 'protein_g': 5.8, 'fat_total_g': 0.9,
                 'carbohydrates_total_g': 30},
    'лапша': {'name': 'Лапша яичная', 'calories': 384, 'protein_g': 12, 'fat_total_g': 4.5,
              'carbohydrates_total_g': 75},
    'лапша гречневая': {'name': 'Лапша гречневая', 'calories': 348, 'protein_g': 14, 'fat_total_g': 0.9,
                        'carbohydrates_total_g': 72},
    'лапша рисовая': {'name': 'Лапша рисовая', 'calories': 364, 'protein_g': 6, 'fat_total_g': 0.6,
                      'carbohydrates_total_g': 82},
    'вермишель': {'name': 'Вермишель', 'calories': 157, 'protein_g': 5.3, 'fat_total_g': 1,
                  'carbohydrates_total_g': 31},

    # ОВОЩИ 🥦 (расширено)
    'картофель': {'name': 'Картофель', 'calories': 77, 'protein_g': 2, 'fat_total_g': 0.1, 'carbohydrates_total_g': 17},
    'картофель отварной': {'name': 'Картофель отварной', 'calories': 82, 'protein_g': 2, 'fat_total_g': 0.4,
                           'carbohydrates_total_g': 18},
    'картофель жареный': {'name': 'Картофель жареный', 'calories': 192, 'protein_g': 2.8, 'fat_total_g': 9.5,
                          'carbohydrates_total_g': 24},
    'картофель пюре': {'name': 'Картофельное пюре', 'calories': 106, 'protein_g': 2.5, 'fat_total_g': 4.2,
                       'carbohydrates_total_g': 14},
    'морковь': {'name': 'Морковь', 'calories': 41, 'protein_g': 0.9, 'fat_total_g': 0.2, 'carbohydrates_total_g': 10},
    'помидор': {'name': 'Помидор', 'calories': 18, 'protein_g': 0.9, 'fat_total_g': 0.2, 'carbohydrates_total_g': 4},
    'помидоры': {'name': 'Помидоры', 'calories': 18, 'protein_g': 0.9, 'fat_total_g': 0.2, 'carbohydrates_total_g': 4},
    'огурец': {'name': 'Огурец', 'calories': 15, 'protein_g': 0.7, 'fat_total_g': 0.1, 'carbohydrates_total_g': 3.6},
    'огурцы': {'name': 'Огурцы', 'calories': 15, 'protein_g': 0.7, 'fat_total_g': 0.1, 'carbohydrates_total_g': 3.6},
    'капуста': {'name': 'Капуста белокачанная', 'calories': 25, 'protein_g': 1.3, 'fat_total_g': 0.1,
                'carbohydrates_total_g': 6},
    'капуста цветная': {'name': 'Капуста цветная', 'calories': 25, 'protein_g': 2, 'fat_total_g': 0.3,
                        'carbohydrates_total_g': 5},
    'брокколи': {'name': 'Брокколи', 'calories': 34, 'protein_g': 2.8, 'fat_total_g': 0.4, 'carbohydrates_total_g': 7},
    'цветная капуста': {'name': 'Цветная капуста', 'calories': 25, 'protein_g': 2, 'fat_total_g': 0.3,
                        'carbohydrates_total_g': 5},
    'брюссельская капуста': {'name': 'Брюссельская капуста', 'calories': 43, 'protein_g': 3.4, 'fat_total_g': 0.3,
                             'carbohydrates_total_g': 9},
    'пекинская капуста': {'name': 'Пекинская капуста', 'calories': 16, 'protein_g': 1.2, 'fat_total_g': 0.2,
                          'carbohydrates_total_g': 3},
    'свекла': {'name': 'Свекла', 'calories': 43, 'protein_g': 1.6, 'fat_total_g': 0.2, 'carbohydrates_total_g': 10},
    'редька': {'name': 'Редька', 'calories': 36, 'protein_g': 1.9, 'fat_total_g': 0.2, 'carbohydrates_total_g': 8},
    'редис': {'name': 'Редис', 'calories': 16, 'protein_g': 0.7, 'fat_total_g': 0.1, 'carbohydrates_total_g': 3.4},
    'лук': {'name': 'Лук репчатый', 'calories': 40, 'protein_g': 1.1, 'fat_total_g': 0.1, 'carbohydrates_total_g': 9},
    'лук зеленый': {'name': 'Лук зеленый', 'calories': 27, 'protein_g': 1.8, 'fat_total_g': 0.2,
                    'carbohydrates_total_g': 5},
    'чеснок': {'name': 'Чеснок', 'calories': 149, 'protein_g': 6.4, 'fat_total_g': 0.5, 'carbohydrates_total_g': 33},
    'перец болгарский': {'name': 'Перец болгарский', 'calories': 27, 'protein_g': 1, 'fat_total_g': 0.3,
                         'carbohydrates_total_g': 6},
    'перец чили': {'name': 'Перец чили', 'calories': 40, 'protein_g': 2, 'fat_total_g': 0.2,
                   'carbohydrates_total_g': 9},
    'кабачок': {'name': 'Кабачок', 'calories': 24, 'protein_g': 0.6, 'fat_total_g': 0.3, 'carbohydrates_total_g': 5},
    'баклажан': {'name': 'Баклажан', 'calories': 24, 'protein_g': 1, 'fat_total_g': 0.2, 'carbohydrates_total_g': 6},
    'тыква': {'name': 'Тыква', 'calories': 26, 'protein_g': 1, 'fat_total_g': 0.1, 'carbohydrates_total_g': 7},
    'горошек зеленый': {'name': 'Горошек зеленый', 'calories': 81, 'protein_g': 5.4, 'fat_total_g': 0.4,
                        'carbohydrates_total_g': 14},
    'кукуруза': {'name': 'Кукуруза', 'calories': 86, 'protein_g': 3.2, 'fat_total_g': 1.2, 'carbohydrates_total_g': 19},
    'кукуруза консервированная': {'name': 'Кукуруза консервированная', 'calories': 58, 'protein_g': 2.2,
                                  'fat_total_g': 0.4, 'carbohydrates_total_g': 13},
    'фасоль': {'name': 'Фасоль', 'calories': 93, 'protein_g': 7, 'fat_total_g': 0.5, 'carbohydrates_total_g': 17},
    'фасоль консервированная': {'name': 'Фасоль консервированная', 'calories': 84, 'protein_g': 5.4, 'fat_total_g': 0.4,
                                'carbohydrates_total_g': 15},
    'горох консервированный': {'name': 'Горох консервированный', 'calories': 69, 'protein_g': 3.6, 'fat_total_g': 0.2,
                               'carbohydrates_total_g': 13},
    'оливки': {'name': 'Оливки', 'calories': 115, 'protein_g': 0.8, 'fat_total_g': 11, 'carbohydrates_total_g': 6},
    'маслины': {'name': 'Маслины', 'calories': 115, 'protein_g': 0.8, 'fat_total_g': 11, 'carbohydrates_total_g': 6},
    'авокадо': {'name': 'Авокадо', 'calories': 160, 'protein_g': 2, 'fat_total_g': 15, 'carbohydrates_total_g': 9},

    # ЗЕЛЕНЬ И САЛАТЫ 🥗
    'салат': {'name': 'Салат листовой', 'calories': 15, 'protein_g': 1.4, 'fat_total_g': 0.2,
              'carbohydrates_total_g': 2.9},
    'салат айсберг': {'name': 'Салат айсберг', 'calories': 14, 'protein_g': 0.9, 'fat_total_g': 0.1,
                      'carbohydrates_total_g': 3},
    'руккола': {'name': 'Руккола', 'calories': 25, 'protein_g': 2.6, 'fat_total_g': 0.7, 'carbohydrates_total_g': 3.7},
    'шпинат': {'name': 'Шпинат', 'calories': 23, 'protein_g': 2.9, 'fat_total_g': 0.4, 'carbohydrates_total_g': 3.6},
    'укроп': {'name': 'Укроп', 'calories': 40, 'protein_g': 2.5, 'fat_total_g': 0.5, 'carbohydrates_total_g': 7},
    'петрушка': {'name': 'Петрушка', 'calories': 36, 'protein_g': 3, 'fat_total_g': 0.8, 'carbohydrates_total_g': 6},
    'базилик': {'name': 'Базилик', 'calories': 27, 'protein_g': 3.2, 'fat_total_g': 0.6, 'carbohydrates_total_g': 4},
    'кинза': {'name': 'Кинза', 'calories': 23, 'protein_g': 2.1, 'fat_total_g': 0.5, 'carbohydrates_total_g': 4},
    'сельдерей': {'name': 'Сельдерей', 'calories': 16, 'protein_g': 0.7, 'fat_total_g': 0.2,
                  'carbohydrates_total_g': 3},
    'сельдерей стебель': {'name': 'Сельдерей стебель', 'calories': 16, 'protein_g': 0.7, 'fat_total_g': 0.2,
                          'carbohydrates_total_g': 3},
    'сельдерей корень': {'name': 'Сельдерей корень', 'calories': 42, 'protein_g': 1.5, 'fat_total_g': 0.3,
                         'carbohydrates_total_g': 9},

    # ФРУКТЫ И ЯГОДЫ 🍎 (расширено)
    'яблоко': {'name': 'Яблоко', 'calories': 52, 'protein_g': 0.3, 'fat_total_g': 0.2, 'carbohydrates_total_g': 14},
    'яблоки': {'name': 'Яблоки', 'calories': 52, 'protein_g': 0.3, 'fat_total_g': 0.2, 'carbohydrates_total_g': 14},
    'банан': {'name': 'Банан', 'calories': 89, 'protein_g': 1.1, 'fat_total_g': 0.3, 'carbohydrates_total_g': 23},
    'бананы': {'name': 'Бананы', 'calories': 89, 'protein_g': 1.1, 'fat_total_g': 0.3, 'carbohydrates_total_g': 23},
    'апельсин': {'name': 'Апельсин', 'calories': 43, 'protein_g': 0.9, 'fat_total_g': 0.1, 'carbohydrates_total_g': 11},
    'апельсины': {'name': 'Апельсины', 'calories': 43, 'protein_g': 0.9, 'fat_total_g': 0.1,
                  'carbohydrates_total_g': 11},
    'мандарин': {'name': 'Мандарин', 'calories': 53, 'protein_g': 0.8, 'fat_total_g': 0.3, 'carbohydrates_total_g': 13},
    'мандарины': {'name': 'Мандарины', 'calories': 53, 'protein_g': 0.8, 'fat_total_g': 0.3,
                  'carbohydrates_total_g': 13},
    'лимон': {'name': 'Лимон', 'calories': 29, 'protein_g': 1.1, 'fat_total_g': 0.3, 'carbohydrates_total_g': 9},
    'лайм': {'name': 'Лайм', 'calories': 30, 'protein_g': 0.7, 'fat_total_g': 0.2, 'carbohydrates_total_g': 11},
    'грейпфрут': {'name': 'Грейпфрут', 'calories': 42, 'protein_g': 0.8, 'fat_total_g': 0.1,
                  'carbohydrates_total_g': 11},
    'персик': {'name': 'Персик', 'calories': 39, 'protein_g': 0.9, 'fat_total_g': 0.3, 'carbohydrates_total_g': 10},
    'нектарин': {'name': 'Нектарин', 'calories': 44, 'protein_g': 1.1, 'fat_total_g': 0.3, 'carbohydrates_total_g': 11},
    'абрикос': {'name': 'Абрикос', 'calories': 48, 'protein_g': 1.4, 'fat_total_g': 0.4, 'carbohydrates_total_g': 11},
    'слива': {'name': 'Слива', 'calories': 46, 'protein_g': 0.7, 'fat_total_g': 0.3, 'carbohydrates_total_g': 11},
    'виноград': {'name': 'Виноград', 'calories': 69, 'protein_g': 0.7, 'fat_total_g': 0.2, 'carbohydrates_total_g': 18},
    'груша': {'name': 'Груша', 'calories': 57, 'protein_g': 0.4, 'fat_total_g': 0.1, 'carbohydrates_total_g': 15},
    'киви': {'name': 'Киви', 'calories': 61, 'protein_g': 1.1, 'fat_total_g': 0.5, 'carbohydrates_total_g': 15},
    'ананас': {'name': 'Ананас', 'calories': 50, 'protein_g': 0.5, 'fat_total_g': 0.1, 'carbohydrates_total_g': 13},
    'манго': {'name': 'Манго', 'calories': 60, 'protein_g': 0.8, 'fat_total_g': 0.4, 'carbohydrates_total_g': 15},
    'папайя': {'name': 'Папайя', 'calories': 43, 'protein_g': 0.5, 'fat_total_g': 0.3, 'carbohydrates_total_g': 11},
    'гранат': {'name': 'Гранат', 'calories': 83, 'protein_g': 1.7, 'fat_total_g': 1.2, 'carbohydrates_total_g': 19},
    'хурма': {'name': 'Хурма', 'calories': 67, 'protein_g': 0.5, 'fat_total_g': 0.4, 'carbohydrates_total_g': 16},
    'инжир': {'name': 'Инжир', 'calories': 74, 'protein_g': 0.8, 'fat_total_g': 0.3, 'carbohydrates_total_g': 19},
    'финик': {'name': 'Финик', 'calories': 282, 'protein_g': 2.5, 'fat_total_g': 0.4, 'carbohydrates_total_g': 75},
    'изюм': {'name': 'Изюм', 'calories': 299, 'protein_g': 3.1, 'fat_total_g': 0.5, 'carbohydrates_total_g': 79},
    'курага': {'name': 'Курага', 'calories': 241, 'protein_g': 3.4, 'fat_total_g': 0.5, 'carbohydrates_total_g': 63},
    'чернослив': {'name': 'Чернослив', 'calories': 240, 'protein_g': 2.2, 'fat_total_g': 0.4,
                  'carbohydrates_total_g': 64},

    # ЯГОДЫ 🍓
    'клубника': {'name': 'Клубника', 'calories': 32, 'protein_g': 0.7, 'fat_total_g': 0.3, 'carbohydrates_total_g': 8},
    'малина': {'name': 'Малина', 'calories': 52, 'protein_g': 1.2, 'fat_total_g': 0.7, 'carbohydrates_total_g': 12},
    'черника': {'name': 'Черника', 'calories': 57, 'protein_g': 0.7, 'fat_total_g': 0.3, 'carbohydrates_total_g': 14},
    'голубика': {'name': 'Голубика', 'calories': 57, 'protein_g': 0.7, 'fat_total_g': 0.3, 'carbohydrates_total_g': 14},
    'ежевика': {'name': 'Ежевика', 'calories': 43, 'protein_g': 1.4, 'fat_total_g': 0.5, 'carbohydrates_total_g': 10},
    'смородина': {'name': 'Смородина', 'calories': 56, 'protein_g': 1.4, 'fat_total_g': 0.2,
                  'carbohydrates_total_g': 13},
    'смородина красная': {'name': 'Смородина красная', 'calories': 56, 'protein_g': 1.4, 'fat_total_g': 0.2,
                          'carbohydrates_total_g': 13},
    'смородина черная': {'name': 'Смородина черная', 'calories': 63, 'protein_g': 1.4, 'fat_total_g': 0.4,
                         'carbohydrates_total_g': 15},
    'крыжовник': {'name': 'Крыжовник', 'calories': 44, 'protein_g': 0.9, 'fat_total_g': 0.6,
                  'carbohydrates_total_g': 10},
    'вишня': {'name': 'Вишня', 'calories': 50, 'protein_g': 1, 'fat_total_g': 0.3, 'carbohydrates_total_g': 12},
    'черешня': {'name': 'Черешня', 'calories': 63, 'protein_g': 1.1, 'fat_total_g': 0.2, 'carbohydrates_total_g': 16},
    'клюква': {'name': 'Клюква', 'calories': 46, 'protein_g': 0.5, 'fat_total_g': 0.1, 'carbohydrates_total_g': 12},
    'брусника': {'name': 'Брусника', 'calories': 46, 'protein_g': 0.7, 'fat_total_g': 0.5, 'carbohydrates_total_g': 10},
    'облепиха': {'name': 'Облепиха', 'calories': 82, 'protein_g': 1.2, 'fat_total_g': 5.4, 'carbohydrates_total_g': 10},
    'арбуз': {'name': 'Арбуз', 'calories': 30, 'protein_g': 0.6, 'fat_total_g': 0.2, 'carbohydrates_total_g': 8},
    'дыня': {'name': 'Дыня', 'calories': 34, 'protein_g': 0.8, 'fat_total_g': 0.2, 'carbohydrates_total_g': 8},

    # ОРЕХИ И СЕМЕНА 🥜 (расширено)
    'орехи': {'name': 'Орехи грецкие', 'calories': 654, 'protein_g': 15, 'fat_total_g': 65,
              'carbohydrates_total_g': 14},
    'грецкие орехи': {'name': 'Орехи грецкие', 'calories': 654, 'protein_g': 15, 'fat_total_g': 65,
                      'carbohydrates_total_g': 14},
    'миндаль': {'name': 'Миндаль', 'calories': 609, 'protein_g': 19, 'fat_total_g': 54, 'carbohydrates_total_g': 13},
    'фундук': {'name': 'Фундук', 'calories': 628, 'protein_g': 15, 'fat_total_g': 61, 'carbohydrates_total_g': 17},
    'кешью': {'name': 'Кешью', 'calories': 553, 'protein_g': 18, 'fat_total_g': 44, 'carbohydrates_total_g': 30},
    'арахис': {'name': 'Арахис', 'calories': 567, 'protein_g': 26, 'fat_total_g': 49, 'carbohydrates_total_g': 16},
    'фисташки': {'name': 'Фисташки', 'calories': 560, 'protein_g': 20, 'fat_total_g': 45, 'carbohydrates_total_g': 27},
    'кедровые орехи': {'name': 'Кедровые орехи', 'calories': 673, 'protein_g': 14, 'fat_total_g': 68,
                       'carbohydrates_total_g': 13},
    'бразильский орех': {'name': 'Бразильский орех', 'calories': 656, 'protein_g': 14, 'fat_total_g': 66,
                         'carbohydrates_total_g': 12},
    'семечки подсолнечника': {'name': 'Семечки подсолнечника', 'calories': 578, 'protein_g': 21, 'fat_total_g': 49,
                              'carbohydrates_total_g': 20},
    'семечки тыквенные': {'name': 'Семечки тыквенные', 'calories': 559, 'protein_g': 30, 'fat_total_g': 49,
                          'carbohydrates_total_g': 11},
    'семена льна': {'name': 'Семена льна', 'calories': 534, 'protein_g': 18, 'fat_total_g': 42,
                    'carbohydrates_total_g': 29},
    'семена чиа': {'name': 'Семена чиа', 'calories': 486, 'protein_g': 17, 'fat_total_g': 31,
                   'carbohydrates_total_g': 42},
    'семена кунжута': {'name': 'Семена кунжута', 'calories': 573, 'protein_g': 18, 'fat_total_g': 50,
                       'carbohydrates_total_g': 23},

    # НАПИТКИ ☕ (расширено)
    'чай': {'name': 'Чай черный', 'calories': 1, 'protein_g': 0, 'fat_total_g': 0, 'carbohydrates_total_g': 0.3},
    'чай черный': {'name': 'Чай черный', 'calories': 1, 'protein_g': 0, 'fat_total_g': 0, 'carbohydrates_total_g': 0.3},
    'чай зеленый': {'name': 'Чай зеленый', 'calories': 1, 'protein_g': 0, 'fat_total_g': 0,
                    'carbohydrates_total_g': 0.3},
    'чай травяной': {'name': 'Чай травяной', 'calories': 1, 'protein_g': 0, 'fat_total_g': 0,
                     'carbohydrates_total_g': 0.3},
    'чай каркаде': {'name': 'Чай каркаде', 'calories': 5, 'protein_g': 0.3, 'fat_total_g': 0,
                    'carbohydrates_total_g': 1},
    'кофе': {'name': 'Кофе', 'calories': 1, 'protein_g': 0.1, 'fat_total_g': 0, 'carbohydrates_total_g': 0},
    'кофе черный': {'name': 'Кофе черный', 'calories': 1, 'protein_g': 0.1, 'fat_total_g': 0,
                    'carbohydrates_total_g': 0},
    'кофе с молоком': {'name': 'Кофе с молоком', 'calories': 40, 'protein_g': 1.5, 'fat_total_g': 1.2,
                       'carbohydrates_total_g': 6},
    'кофе латте': {'name': 'Кофе латте', 'calories': 120, 'protein_g': 6, 'fat_total_g': 5,
                   'carbohydrates_total_g': 12},
    'кофе капучино': {'name': 'Кофе капучино', 'calories': 80, 'protein_g': 4, 'fat_total_g': 3,
                      'carbohydrates_total_g': 8},
    'кофе американо': {'name': 'Кофе американо', 'calories': 1, 'protein_g': 0.1, 'fat_total_g': 0,
                       'carbohydrates_total_g': 0},
    'какао': {'name': 'Какао', 'calories': 228, 'protein_g': 20, 'fat_total_g': 14, 'carbohydrates_total_g': 58},
    'какао с молоком': {'name': 'Какао с молоком', 'calories': 89, 'protein_g': 3, 'fat_total_g': 3,
                        'carbohydrates_total_g': 13},
    'горячий шоколад': {'name': 'Горячий шоколад', 'calories': 77, 'protein_g': 2, 'fat_total_g': 2,
                        'carbohydrates_total_g': 14},

    # СЛАДОСТИ И ДЕСЕРТЫ 🍰 (расширено)
    'сахар': {'name': 'Сахар', 'calories': 387, 'protein_g': 0, 'fat_total_g': 0, 'carbohydrates_total_g': 100},
    'сахарная пудра': {'name': 'Сахарная пудра', 'calories': 389, 'protein_g': 0, 'fat_total_g': 0,
                       'carbohydrates_total_g': 100},
    'мед': {'name': 'Мед', 'calories': 304, 'protein_g': 0.3, 'fat_total_g': 0, 'carbohydrates_total_g': 82},
    'шоколад': {'name': 'Шоколад молочный', 'calories': 546, 'protein_g': 5, 'fat_total_g': 31,
                'carbohydrates_total_g': 61},
    'шоколад молочный': {'name': 'Шоколад молочный', 'calories': 546, 'protein_g': 5, 'fat_total_g': 31,
                         'carbohydrates_total_g': 61},
    'шоколад черный': {'name': 'Шоколад черный', 'calories': 539, 'protein_g': 6.2, 'fat_total_g': 35,
                       'carbohydrates_total_g': 48},
    'шоколад темный': {'name': 'Шоколад темный', 'calories': 539, 'protein_g': 6.2, 'fat_total_g': 35,
                       'carbohydrates_total_g': 48},
    'шоколад белый': {'name': 'Шоколад белый', 'calories': 541, 'protein_g': 4.3, 'fat_total_g': 32,
                      'carbohydrates_total_g': 59},
    'варенье': {'name': 'Варенье', 'calories': 265, 'protein_g': 0.3, 'fat_total_g': 0.1, 'carbohydrates_total_g': 70},
    'джем': {'name': 'Джем', 'calories': 250, 'protein_g': 0.4, 'fat_total_g': 0.1, 'carbohydrates_total_g': 65},
    'пастила': {'name': 'Пастила', 'calories': 320, 'protein_g': 0.5, 'fat_total_g': 0.1, 'carbohydrates_total_g': 80},
    'зефир': {'name': 'Зефир', 'calories': 326, 'protein_g': 0.8, 'fat_total_g': 0.1, 'carbohydrates_total_g': 80},
    'мармелад': {'name': 'Мармелад', 'calories': 290, 'protein_g': 0.1, 'fat_total_g': 0.1,
                 'carbohydrates_total_g': 76},
    'халва': {'name': 'Халва', 'calories': 469, 'protein_g': 12, 'fat_total_g': 22, 'carbohydrates_total_g': 54},
    'пряники': {'name': 'Пряники', 'calories': 350, 'protein_g': 5, 'fat_total_g': 3, 'carbohydrates_total_g': 75},
    'печенье': {'name': 'Печенье', 'calories': 417, 'protein_g': 6.5, 'fat_total_g': 14, 'carbohydrates_total_g': 71},
    'печенье овсяное': {'name': 'Печенье овсяное', 'calories': 437, 'protein_g': 6.5, 'fat_total_g': 17,
                        'carbohydrates_total_g': 66},
    'печенье песочное': {'name': 'Печенье песочное', 'calories': 458, 'protein_g': 6, 'fat_total_g': 20,
                         'carbohydrates_total_g': 65},
    'вафли': {'name': 'Вафли', 'calories': 425, 'protein_g': 8, 'fat_total_g': 14, 'carbohydrates_total_g': 72},
    'торт': {'name': 'Торт', 'calories': 350, 'protein_g': 5, 'fat_total_g': 15, 'carbohydrates_total_g': 50},
    'пирожное': {'name': 'Пирожное', 'calories': 340, 'protein_g': 5.5, 'fat_total_g': 12, 'carbohydrates_total_g': 55},
    'эклер': {'name': 'Эклер', 'calories': 333, 'protein_g': 5, 'fat_total_g': 22, 'carbohydrates_total_g': 28},
    'мороженое': {'name': 'Мороженое', 'calories': 207, 'protein_g': 3.7, 'fat_total_g': 11,
                  'carbohydrates_total_g': 24},
    'мороженое пломбир': {'name': 'Мороженое пломбир', 'calories': 227, 'protein_g': 3.2, 'fat_total_g': 15,
                          'carbohydrates_total_g': 20},
    'мороженое эскимо': {'name': 'Мороженое эскимо', 'calories': 270, 'protein_g': 4, 'fat_total_g': 20,
                         'carbohydrates_total_g': 20},
    'сгущенка': {'name': 'Сгущенное молоко', 'calories': 320, 'protein_g': 7, 'fat_total_g': 9,
                 'carbohydrates_total_g': 56},
    'сгущенное молоко': {'name': 'Сгущенное молоко', 'calories': 320, 'protein_g': 7, 'fat_total_g': 9,
                         'carbohydrates_total_g': 56},

    # МАСЛА И ЖИРЫ 🫒 (расширено)
    'масло': {'name': 'Масло подсолнечное', 'calories': 884, 'protein_g': 0, 'fat_total_g': 100,
              'carbohydrates_total_g': 0},
    'масло подсолнечное': {'name': 'Масло подсолнечное', 'calories': 884, 'protein_g': 0, 'fat_total_g': 100,
                           'carbohydrates_total_g': 0},
    'масло оливковое': {'name': 'Масло оливковое', 'calories': 898, 'protein_g': 0, 'fat_total_g': 100,
                        'carbohydrates_total_g': 0},
    'масло сливочное': {'name': 'Сливочное масло', 'calories': 717, 'protein_g': 1, 'fat_total_g': 81,
                        'carbohydrates_total_g': 1},
    'масло сливочное 82%': {'name': 'Сливочное масло 82%', 'calories': 748, 'protein_g': 0.5, 'fat_total_g': 82,
                            'carbohydrates_total_g': 0.8},
    'масло растительное': {'name': 'Масло растительное', 'calories': 884, 'protein_g': 0, 'fat_total_g': 100,
                           'carbohydrates_total_g': 0},
    'масло кукурузное': {'name': 'Масло кукурузное', 'calories': 899, 'protein_g': 0, 'fat_total_g': 100,
                         'carbohydrates_total_g': 0},
    'масло льняное': {'name': 'Масло льняное', 'calories': 884, 'protein_g': 0, 'fat_total_g': 100,
                      'carbohydrates_total_g': 0},
    'масло кокосовое': {'name': 'Масло кокосовое', 'calories': 862, 'protein_g': 0, 'fat_total_g': 100,
                        'carbohydrates_total_g': 0},
    'маргарин': {'name': 'Маргарин', 'calories': 717, 'protein_g': 0.2, 'fat_total_g': 80, 'carbohydrates_total_g': 1},
    'сало': {'name': 'Сало', 'calories': 797, 'protein_g': 2.4, 'fat_total_g': 89, 'carbohydrates_total_g': 0},
    'жир': {'name': 'Животный жир', 'calories': 897, 'protein_g': 0, 'fat_total_g': 99, 'carbohydrates_total_g': 0},

    # ПЕРВЫЕ БЛЮДА 🍲 (расширено - теперь есть ВСЕ супы!)
    'суп': {'name': 'Суп куриный', 'calories': 45, 'protein_g': 4, 'fat_total_g': 2, 'carbohydrates_total_g': 3},
    'суп куриный': {'name': 'Суп куриный', 'calories': 45, 'protein_g': 4, 'fat_total_g': 2,
                    'carbohydrates_total_g': 3},
    'суп овощной': {'name': 'Суп овощной', 'calories': 40, 'protein_g': 2, 'fat_total_g': 1,
                    'carbohydrates_total_g': 6},
    'суп грибной': {'name': 'Суп грибной', 'calories': 45, 'protein_g': 2, 'fat_total_g': 2,
                    'carbohydrates_total_g': 6},
    'суп гороховый': {'name': 'Суп гороховый', 'calories': 66, 'protein_g': 4, 'fat_total_g': 2,
                      'carbohydrates_total_g': 9},
    'суп с фрикадельками': {'name': 'Суп с фрикадельками', 'calories': 55, 'protein_g': 5, 'fat_total_g': 2,
                            'carbohydrates_total_g': 5},
    'суп вермишелевый': {'name': 'Суп вермишелевый', 'calories': 50, 'protein_g': 3, 'fat_total_g': 1,
                         'carbohydrates_total_g': 8},
    'суп рисовый': {'name': 'Суп рисовый', 'calories': 48, 'protein_g': 3, 'fat_total_g': 1,
                    'carbohydrates_total_g': 8},
    'суп гречневый': {'name': 'Суп гречневый', 'calories': 47, 'protein_g': 3, 'fat_total_g': 1,
                      'carbohydrates_total_g': 7},
    'суп рыбный': {'name': 'Суп рыбный', 'calories': 42, 'protein_g': 5, 'fat_total_g': 1, 'carbohydrates_total_g': 3},
    'уха': {'name': 'Уха', 'calories': 45, 'protein_g': 6, 'fat_total_g': 1, 'carbohydrates_total_g': 3},
    'борщ': {'name': 'Борщ', 'calories': 85, 'protein_g': 3, 'fat_total_g': 4, 'carbohydrates_total_g': 10},
    'щи': {'name': 'Щи', 'calories': 78, 'protein_g': 3, 'fat_total_g': 3, 'carbohydrates_total_g': 9},
    'солянка': {'name': 'Солянка', 'calories': 95, 'protein_g': 5, 'fat_total_g': 6, 'carbohydrates_total_g': 5},
    'рассольник': {'name': 'Рассольник', 'calories': 72, 'protein_g': 3, 'fat_total_g': 3, 'carbohydrates_total_g': 8},
    'харчо': {'name': 'Харчо', 'calories': 88, 'protein_g': 4, 'fat_total_g': 5, 'carbohydrates_total_g': 7},
    'окрошка': {'name': 'Окрошка', 'calories': 65, 'protein_g': 4, 'fat_total_g': 2, 'carbohydrates_total_g': 7},
    'свекольник': {'name': 'Свекольник', 'calories': 50, 'protein_g': 2, 'fat_total_g': 2, 'carbohydrates_total_g': 6},
    'щи из квашеной капусты': {'name': 'Щи из квашеной капусты', 'calories': 80, 'protein_g': 3, 'fat_total_g': 4,
                               'carbohydrates_total_g': 9},
    'суп-пюре': {'name': 'Суп-пюре', 'calories': 75, 'protein_g': 3, 'fat_total_g': 4, 'carbohydrates_total_g': 7},
    'суп-лапша': {'name': 'Суп-лапша', 'calories': 55, 'protein_g': 4, 'fat_total_g': 2, 'carbohydrates_total_g': 6},
    'суп с клецками': {'name': 'Суп с клецками', 'calories': 70, 'protein_g': 3, 'fat_total_g': 3,
                       'carbohydrates_total_g': 8},
    'томатный суп': {'name': 'Томатный суп', 'calories': 60, 'protein_g': 2, 'fat_total_g': 3,
                     'carbohydrates_total_g': 7},
    'крем-суп': {'name': 'Крем-суп', 'calories': 75, 'protein_g': 3, 'fat_total_g': 4, 'carbohydrates_total_g': 7},
    'сырный суп': {'name': 'Сырный суп', 'calories': 95, 'protein_g': 5, 'fat_total_g': 6, 'carbohydrates_total_g': 5},
    'суп с фасолью': {'name': 'Суп с фасолью', 'calories': 70, 'protein_g': 4, 'fat_total_g': 3,
                      'carbohydrates_total_g': 8},
    'суп с чечевицей': {'name': 'Суп с чечевицей', 'calories': 68, 'protein_g': 5, 'fat_total_g': 2,
                        'carbohydrates_total_g': 8},
    'суп харчо': {'name': 'Суп харчо', 'calories': 88, 'protein_g': 4, 'fat_total_g': 5, 'carbohydrates_total_g': 7},
    'суп солянка': {'name': 'Суп солянка', 'calories': 95, 'protein_g': 5, 'fat_total_g': 6,
                    'carbohydrates_total_g': 5},
    'суп рассольник': {'name': 'Суп рассольник', 'calories': 72, 'protein_g': 3, 'fat_total_g': 3,
                       'carbohydrates_total_g': 8},
    'суп борщ': {'name': 'Суп борщ', 'calories': 85, 'protein_g': 3, 'fat_total_g': 4, 'carbohydrates_total_g': 10},
    'суп щи': {'name': 'Суп щи', 'calories': 78, 'protein_g': 3, 'fat_total_g': 3, 'carbohydrates_total_g': 9},

    # ГОТОВЫЕ БЛЮДА 🍛
    'пельмени': {'name': 'Пельмени', 'calories': 275, 'protein_g': 12, 'fat_total_g': 15, 'carbohydrates_total_g': 29},
    'вареники': {'name': 'Вареники', 'calories': 203, 'protein_g': 7.6, 'fat_total_g': 5.7,
                 'carbohydrates_total_g': 32},
    'блины': {'name': 'Блины', 'calories': 233, 'protein_g': 6.1, 'fat_total_g': 8.7, 'carbohydrates_total_g': 32},
    'блины с творогом': {'name': 'Блины с творогом', 'calories': 195, 'protein_g': 10, 'fat_total_g': 8,
                         'carbohydrates_total_g': 22},
    'блины с мясом': {'name': 'Блины с мясом', 'calories': 256, 'protein_g': 13, 'fat_total_g': 14,
                      'carbohydrates_total_g': 22},
    'сырники': {'name': 'Сырники', 'calories': 183, 'protein_g': 12, 'fat_total_g': 8, 'carbohydrates_total_g': 15},
    'омлет': {'name': 'Омлет', 'calories': 154, 'protein_g': 10, 'fat_total_g': 12, 'carbohydrates_total_g': 2},
    'яичница': {'name': 'Яичница', 'calories': 212, 'protein_g': 14, 'fat_total_g': 17, 'carbohydrates_total_g': 1},
    'голубцы': {'name': 'Голубцы', 'calories': 143, 'protein_g': 7, 'fat_total_g': 8, 'carbohydrates_total_g': 11},
    'котлеты': {'name': 'Котлеты', 'calories': 220, 'protein_g': 15, 'fat_total_g': 16, 'carbohydrates_total_g': 4},
    'котлеты куриные': {'name': 'Котлеты куриные', 'calories': 210, 'protein_g': 18, 'fat_total_g': 14,
                        'carbohydrates_total_g': 4},
    'котлеты рыбные': {'name': 'Котлеты рыбные', 'calories': 168, 'protein_g': 16, 'fat_total_g': 10,
                       'carbohydrates_total_g': 4},
    'отбивные': {'name': 'Отбивные', 'calories': 242, 'protein_g': 20, 'fat_total_g': 18, 'carbohydrates_total_g': 1},
    'шашлык': {'name': 'Шашлык', 'calories': 240, 'protein_g': 25, 'fat_total_g': 15, 'carbohydrates_total_g': 2},
    'пицца': {'name': 'Пицца', 'calories': 266, 'protein_g': 11, 'fat_total_g': 9.8, 'carbohydrates_total_g': 33},
    'пирог': {'name': 'Пирог', 'calories': 280, 'protein_g': 6, 'fat_total_g': 12, 'carbohydrates_total_g': 38},
    'пирожок': {'name': 'Пирожок', 'calories': 230, 'protein_g': 5, 'fat_total_g': 10, 'carbohydrates_total_g': 30},
    'пирожок с капустой': {'name': 'Пирожок с капустой', 'calories': 225, 'protein_g': 5, 'fat_total_g': 9,
                           'carbohydrates_total_g': 31},
    'пирожок с мясом': {'name': 'Пирожок с мясом', 'calories': 245, 'protein_g': 9, 'fat_total_g': 12,
                        'carbohydrates_total_g': 26},
    'пирожок с картошкой': {'name': 'Пирожок с картошкой', 'calories': 235, 'protein_g': 5, 'fat_total_g': 10,
                            'carbohydrates_total_g': 32},
    'бутерброд': {'name': 'Бутерброд', 'calories': 250, 'protein_g': 8, 'fat_total_g': 12, 'carbohydrates_total_g': 28},
    'сэндвич': {'name': 'Сэндвич', 'calories': 280, 'protein_g': 12, 'fat_total_g': 15, 'carbohydrates_total_g': 25},
    'бургер': {'name': 'Бургер', 'calories': 295, 'protein_g': 12, 'fat_total_g': 10, 'carbohydrates_total_g': 40},
    'чизбургер': {'name': 'Чизбургер', 'calories': 303, 'protein_g': 15, 'fat_total_g': 13,
                  'carbohydrates_total_g': 30},
    'картофель фри': {'name': 'Картофель фри', 'calories': 312, 'protein_g': 3.4, 'fat_total_g': 15,
                      'carbohydrates_total_g': 41},
    'наггетсы': {'name': 'Наггетсы', 'calories': 296, 'protein_g': 15, 'fat_total_g': 19, 'carbohydrates_total_g': 17},
    'хот-дог': {'name': 'Хот-дог', 'calories': 290, 'protein_g': 10, 'fat_total_g': 17, 'carbohydrates_total_g': 24},

    # СОУСЫ И ПРИПРАВЫ 🍯
    'майонез': {'name': 'Майонез', 'calories': 680, 'protein_g': 0.3, 'fat_total_g': 75, 'carbohydrates_total_g': 2.6},
    'кетчуп': {'name': 'Кетчуп', 'calories': 101, 'protein_g': 1.7, 'fat_total_g': 0.3, 'carbohydrates_total_g': 23},
    'горчица': {'name': 'Горчица', 'calories': 67, 'protein_g': 4.4, 'fat_total_g': 3.3, 'carbohydrates_total_g': 5},
    'аджика': {'name': 'Аджика', 'calories': 80, 'protein_g': 2, 'fat_total_g': 5, 'carbohydrates_total_g': 8},
    'соевый соус': {'name': 'Соевый соус', 'calories': 53, 'protein_g': 6, 'fat_total_g': 0,
                    'carbohydrates_total_g': 10},
    'соус тартар': {'name': 'Соус тартар', 'calories': 311, 'protein_g': 0.9, 'fat_total_g': 33,
                    'carbohydrates_total_g': 3},
    'соус бешамель': {'name': 'Соус бешамель', 'calories': 147, 'protein_g': 4, 'fat_total_g': 11,
                      'carbohydrates_total_g': 9},
    'сметанный соус': {'name': 'Сметанный соус', 'calories': 205, 'protein_g': 3, 'fat_total_g': 20,
                       'carbohydrates_total_g': 5},
    'томатный соус': {'name': 'Томатный соус', 'calories': 74, 'protein_g': 1.5, 'fat_total_g': 3.5,
                      'carbohydrates_total_g': 10},
    'соус барбекю': {'name': 'Соус барбекю', 'calories': 172, 'protein_g': 1.5, 'fat_total_g': 0.7,
                     'carbohydrates_total_g': 40},
    'уксус': {'name': 'Уксус', 'calories': 18, 'protein_g': 0, 'fat_total_g': 0, 'carbohydrates_total_g': 5},
    'соль': {'name': 'Соль', 'calories': 0, 'protein_g': 0, 'fat_total_g': 0, 'carbohydrates_total_g': 0},
    'перец': {'name': 'Перец черный', 'calories': 251, 'protein_g': 10, 'fat_total_g': 3.3,
              'carbohydrates_total_g': 64},

    # АЛКОГОЛЬНЫЕ НАПИТКИ 🍷
    'пиво': {'name': 'Пиво', 'calories': 43, 'protein_g': 0.5, 'fat_total_g': 0, 'carbohydrates_total_g': 3.6},
    'вино': {'name': 'Вино красное', 'calories': 85, 'protein_g': 0.1, 'fat_total_g': 0, 'carbohydrates_total_g': 2.6},
    'вино белое': {'name': 'Вино белое', 'calories': 82, 'protein_g': 0.1, 'fat_total_g': 0,
                   'carbohydrates_total_g': 2.6},
    'водка': {'name': 'Водка', 'calories': 235, 'protein_g': 0, 'fat_total_g': 0, 'carbohydrates_total_g': 0},
    'коньяк': {'name': 'Коньяк', 'calories': 239, 'protein_g': 0, 'fat_total_g': 0, 'carbohydrates_total_g': 0},
    'виски': {'name': 'Виски', 'calories': 250, 'protein_g': 0, 'fat_total_g': 0, 'carbohydrates_total_g': 0},
    'ром': {'name': 'Ром', 'calories': 231, 'protein_g': 0, 'fat_total_g': 0, 'carbohydrates_total_g': 0},
    'джин': {'name': 'Джин', 'calories': 263, 'protein_g': 0, 'fat_total_g': 0, 'carbohydrates_total_g': 0},
    'текила': {'name': 'Текила', 'calories': 231, 'protein_g': 0, 'fat_total_g': 0, 'carbohydrates_total_g': 0},
    'ликер': {'name': 'Ликер', 'calories': 327, 'protein_g': 0, 'fat_total_g': 0, 'carbohydrates_total_g': 53},
    'вермут': {'name': 'Вермут', 'calories': 153, 'protein_g': 0, 'fat_total_g': 0, 'carbohydrates_total_g': 15},
    'шампанское': {'name': 'Шампанское', 'calories': 84, 'protein_g': 0.2, 'fat_total_g': 0,
                   'carbohydrates_total_g': 2},
}


async def search_product_api(product_name: str):
    print(f"🔍 DEBUG: Поиск продукта: '{product_name}'")

    # Добавляем эмодзи в поиск
    emoji_to_product = {
        '🍞': 'хлеб',
        '🥖': 'батон',
        '🍳': 'яйцо',
        '🥚': 'яйцо',
        '🥛': 'молоко',
        '🧀': 'сыр',
        '🍗': 'курица',
        '🥩': 'говядина',
        '🐟': 'лосось',
        '🍚': 'рис',
        '🍜': 'макароны',
        '🥔': 'картофель',
        '🥕': 'морковь',
        '🍅': 'помидор',
        '🥒': 'огурец',
        '🍎': 'яблоко',
        '🍌': 'банан',
        '🍊': 'апельсин',
        '🥜': 'орехи',
        '☕': 'кофе',
        '🍫': 'шоколад',
        '🍲': 'суп',
        '🥗': 'салат',
        '🥣': 'каша',
        '🍪': 'печенье',
        '🍩': 'пончик',
        '🍦': 'мороженое',
        '🥐': 'круассан',
        '🥪': 'сэндвич',
        '🌭': 'хот-дог',
        '🍕': 'пицца',
        '🍔': 'бургер',
        '🍟': 'картофель фри',
        '🥞': 'блины',
        '🧇': 'вафли',
        '🍯': 'мед',
        '🥛': 'молоко',
        '🍵': 'чай',
        '🍷': 'вино',
        '🍺': 'пиво',
        '🥂': 'шампанское',
        '🥃': 'виски',
        '🍸': 'коктейль',
        '🍹': 'тропический коктейль',
        '🍶': 'саке',
        '🍾': 'шампанское',
        '🧃': 'сок',
        '🥤': 'газировка',
        '🧊': 'лед',
        '🍽️': 'еда',
        '🍴': 'приборы',
        '🥄': 'ложка',
        '🍽': 'тарелка',
        '🥡': 'еда на вынос',
        '🧂': 'соль',
        '🥫': 'консервы',
        '🍤': 'креветки',
        '🥮': 'лунный пирог',
        '🍡': 'данго',
        '🥟': 'пельмени',
        '🥠': 'печенье с предсказанием',
        '🥧': 'пирог',
        '🍰': 'торт',
        '🎂': 'торт на день рождения',
        '🧁': 'кекс',
        '🥮': 'лунный пирог',
        '🍦': 'мягкое мороженое',
        '🍨': 'мороженое',
        '🍧': 'щербет',
        '🍡': 'данго',
        '🍢': 'оден',
        '🍣': 'суши',
        '🍤': 'креветки темпура',
        '🍥': 'рыбный пирог',
        '🥮': 'лунный пирог',
        '🍘': 'рисовые крекеры',
        '🍙': 'онигири',
        '🍚': 'рис',
        '🍛': 'карри с рисом',
        '🍜': 'лапша',
        '🍝': 'спагетти',
        '🍠': 'печеный картофель',
        '🍢': 'оден',
        '🍣': 'суши',
        '🍤': 'креветки темпура',
        '🍥': 'рыбный пирог',
        '🍦': 'мягкое мороженое',
        '🍧': 'щербет',
        '🍨': 'мороженое',
        '🍩': 'пончик',
        '🍪': 'печенье',
        '🎂': 'торт на день рождения',
        '🍰': 'торт',
        '🧁': 'кекс',
        '🥧': 'пирог',
        '🍫': 'шоколад',
        '🍬': 'конфета',
        '🍭': 'леденец',
        '🍮': 'крем',
        '🍯': 'горшочек меда',
        '🍼': 'детская бутылочка',
        '🥛': 'стакан молока',
        '☕': 'чашка кофе',
        '🍵': 'чашка чая',
        '🍶': 'саке',
        '🍾': 'бутылка шампанского',
        '🍷': 'бокал вина',
        '🍸': 'коктейль',
        '🍹': 'тропический коктейль',
        '🍺': 'пиво',
        '🍻': 'чокание бокалами',
        '🥂': 'чокание бокалами',
        '🥃': 'стакан',
        '🥤': 'стакан с трубочкой',
        '🧃': 'коробка сока',
        '🧉': 'мате',
        '🧊': 'лед',
        '🥢': 'палочки для еды',
        '🍽️': 'нож и вилка',
        '🍴': 'нож и вилка',
        '🥄': 'ложка',
        '🔪': 'нож',
        '🏺': 'амфора',
    }

    # Если ввели эмодзи - заменяем на текстовый эквивалент
    if product_name in emoji_to_product:
        product_name = emoji_to_product[product_name]
        print(f"🔍 DEBUG: Эмодзи распознано, заменено на: '{product_name}'")

    # Сначала проверяем локальную базу - УЛУЧШЕННЫЙ ПОИСК
    product_lower = product_name.lower().strip()
    print(f"🔍 DEBUG: Поиск в локальной базе: '{product_lower}'")

    # 1. Прямое совпадение
    if product_lower in LOCAL_PRODUCTS:
        print(f"✅ DEBUG: Найден в локальной базе (прямое совпадение): {LOCAL_PRODUCTS[product_lower]['name']}")
        return LOCAL_PRODUCTS[product_lower]

    # 2. Поиск по частичному совпадению
    for key, product in LOCAL_PRODUCTS.items():
        if key in product_lower:
            print(f"✅ DEBUG: Найден по частичному совпадению: {product['name']} (ключ: '{key}' в '{product_lower}')")
            return product

    # 3. Поиск вхождений в ключах
    for key, product in LOCAL_PRODUCTS.items():
        if product_lower in key:
            print(f"✅ DEBUG: Найден по вхождению: {product['name']} ('{product_lower}' в ключе '{key}')")
            return product

    print(f"❌ DEBUG: Продукт '{product_lower}' не найден в локальной базе")

    # Если не нашли в локальной базе, используем общие категории
    category_defaults = {
        'суп': {'name': 'Суп (среднее значение)', 'calories': 60, 'protein_g': 3, 'fat_total_g': 2,
                'carbohydrates_total_g': 8},
        'салат': {'name': 'Салат (среднее значение)', 'calories': 80, 'protein_g': 4, 'fat_total_g': 5,
                  'carbohydrates_total_g': 6},
        'каша': {'name': 'Каша (среднее значение)', 'calories': 120, 'protein_g': 4, 'fat_total_g': 2,
                 'carbohydrates_total_g': 25},
        'сок': {'name': 'Сок (среднее значение)', 'calories': 45, 'protein_g': 0.5, 'fat_total_g': 0.1,
                'carbohydrates_total_g': 11},
        'компот': {'name': 'Компот (среднее значение)', 'calories': 60, 'protein_g': 0.2, 'fat_total_g': 0.1,
                   'carbohydrates_total_g': 15},
        'напиток': {'name': 'Напиток (среднее значение)', 'calories': 30, 'protein_g': 0, 'fat_total_g': 0,
                    'carbohydrates_total_g': 8},
        'десерт': {'name': 'Десерт (среднее значение)', 'calories': 300, 'protein_g': 5, 'fat_total_g': 15,
                   'carbohydrates_total_g': 40},
        'выпечка': {'name': 'Выпечка (среднее значение)', 'calories': 350, 'protein_g': 8, 'fat_total_g': 15,
                    'carbohydrates_total_g': 50},
        'соус': {'name': 'Соус (среднее значение)', 'calories': 200, 'protein_g': 2, 'fat_total_g': 20,
                 'carbohydrates_total_g': 10},
    }

    for category, defaults in category_defaults.items():
        if category in product_lower:
            print(f"✅ DEBUG: Определена категория '{category}' для '{product_lower}'")
            return defaults

    # Общие средние значения для неизвестного продукта
    print(f"⚠️ DEBUG: Продукт '{product_lower}' не найден, используем средние значения")
    return {
        'name': product_name,
        'calories': 100,
        'protein_g': 5,
        'fat_total_g': 3,
        'carbohydrates_total_g': 12
    }


# Генерация рекомендаций через DeepSeek API
async def generate_deepseek_recommendations(user_data: dict, nutrition_data: dict):
    if DEEPSEEEK_API_KEY == "sk-your-deepseek-api-key-here":
        return None

    prompt = f"""
    Ты - профессиональный нутрициолог. Проанализируй данные пользователя и дай персональные рекомендации.

    ДАННЫЕ ПОЛЬЗОВАТЕЛЯ:
    - Возраст: {user_data['age']} лет
    - Пол: {user_data['gender']}
    - Рост: {user_data['height']} см
    - Вес: {user_data['weight']} кг
    - Цель: {user_data['goal']}
    - Уровень активности: {user_data['activity_level']}
    - Дневная норма калорий: {user_data['daily_calories']} ккал

    СЕГОДНЯШНЕЕ ПИТАНИЕ:
    - Калории: {nutrition_data['calories']:.0f} / {user_data['daily_calories']} ккал ({nutrition_data['calories'] / user_data['daily_calories'] * 100:.1f}%)
    - Белки: {nutrition_data['protein']:.1f}г
    - Жиры: {nutrition_data['fat']:.1f}г  
    - Углеводы: {nutrition_data['carbs']:.1f}г

    Дай конкретные, практические рекомендации!
    """

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                    'https://api.deepseek.com/chat/completions',
                    headers={
                        'Authorization': f'Bearer {DEEPSEEK_API_KEY}',
                        'Content-Type': 'application/json'
                    },
                    json={
                        'model': 'deepseek-chat',
                        'messages': [{'role': 'user', 'content': prompt}],
                        'temperature': 0.7,
                        'max_tokens': 800
                    },
                    timeout=30
            ) as response:
                if response.status == 200:
                    result = await response.json()
                    return result['choices'][0]['message']['content']
                else:
                    logger.error(f"DeepSeek API error: {response.status}")
                    return None
    except Exception as e:
        logger.error(f"DeepSeek API error: {e}")
        return None


# ИСПРАВЛЕННАЯ ФУНКЦИЯ - ТЕПЕРЬ ВОЗВРАЩАЕТ ЗНАЧЕНИЕ!
def generate_local_recommendations(user_data, nutrition_data):
    print(f"🔍 DEBUG: Генерация локальных рекомендаций")
    print(f"🔍 DEBUG: user_data: {user_data}")
    print(f"🔍 DEBUG: nutrition_data: {nutrition_data}")

    recommendations = []
    user_goal = user_data['goal']
    user_gender = user_data['gender']
    user_weight = user_data['weight']

    # Проверяем, что есть данные о калориях
    if not nutrition_data['calories'] or nutrition_data['calories'] == 0:
        return "📊 Сегодня еще нет данных о питании. Добавьте приемы пищи для получения рекомендаций."

    calorie_percentage = (nutrition_data['calories'] / user_data['daily_calories']) * 100

    print(f"🔍 DEBUG: Процент калорий: {calorie_percentage:.1f}%")

    # Персональные рекомендации по целям
    if user_goal == 'loss':
        if calorie_percentage < 70:
            recommendations.append("🔥 Для похудения недобор калорий! Добавьте:")
            recommendations.append("   • Белковые продукты: куриная грудка, творог, яйца")
            recommendations.append("   • Овощи: салат из огурцов и помидоров")
        elif calorie_percentage > 110:
            recommendations.append("⚠️ Для похудения превышение калорий!")
            recommendations.append("   • Уменьшите порции углеводов на ужин")
        else:
            recommendations.append("🎯 Идеально для похудения! Продолжайте в том же духе!")

    elif user_goal == 'gain':
        if calorie_percentage < 80:
            recommendations.append("💪 Для набора массы нужно больше калорий!")
            recommendations.append("   • Добавьте сложные углеводы: гречка, рис, овсянка")
        else:
            recommendations.append("🚀 Отлично! Для роста мышц продолжайте!")

    else:  # maintain
        if calorie_percentage < 80:
            recommendations.append("⚖️ Для поддержания веса добавьте сбалансированный перекус:")
            recommendations.append("   • Фрукты с йогуртом")
        elif calorie_percentage > 120:
            recommendations.append("📊 Для поддержания веса немного превышена норма")

    # Рекомендации по белкам
    protein_need = user_weight * (2.0 if user_goal == 'gain' else 1.6)
    current_protein = nutrition_data['protein']

    if current_protein < protein_need * 0.7:
        protein_deficit = protein_need - current_protein
        recommendations.append(f"🥩 Белков не хватает! Нужно еще ~{protein_deficit:.0f}г")

    elif current_protein >= protein_need:
        recommendations.append("💪 Отлично по белкам! Мышцы скажут спасибо!")

    # Общие советы
    general_tips = [
        "🍽 Ешьте каждые 3-4 часа для стабильного метаболизма",
        "🥦 Половина тарелки - овощи в каждый основной прием пищи",
        "💤 Последний прием пищи за 2-3 часа до сна",
        "🚰 Стакан воды за 30 минут до еды улучшает пищеварение",
        "🏃‍♂️ Сочетайте питание с физической активностью",
        "🥑 Добавляйте полезные жиры: орехи, авокадо, оливковое масло",
        "💧 Пейте 1.5-2 литра воды в день",
        "🍎 Перекусывайте фруктами вместо сладостей"
    ]

    recommendations.extend(random.sample(general_tips, 2))

    if not recommendations:
        recommendations.append("✅ Все в норме! Продолжайте в том же духе!")
        recommendations.extend(random.sample(general_tips, 2))

    result = "\n".join([f"{rec}" for rec in recommendations])
    print(f"🔍 DEBUG: Сгенерированные рекомендации: {result}")
    return result  # ВАЖНО: возвращаем результат!


# НОВАЯ КОМАНДА ДЛЯ ПРОСМОТРА СТАТИСТИКИ ПОСЕЩЕНИЙ
async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда для просмотра статистики посещений (только для админа)"""
    ADMIN_ID = 5199340101  # ЗАМЕНИТЕ НА СВОЙ TELEGRAM ID!

    if update.message.from_user.id != ADMIN_ID:
        await update.message.reply_text("❌ У вас нет прав для просмотра этой статистики.")
        return

    stats = database.get_visit_stats()

    message = (
        f"📊 **СТАТИСТИКА ПОСЕЩЕНИЙ**\n\n"
        f"👥 **Всего пользователей:** {stats['total_users']}\n"
        f"👣 **Всего визитов:** {stats['total_visits']}\n"
        f"📅 **Уникальных за сегодня:** {stats['unique_today']}\n"
        f"📆 **Уникальных за неделю:** {stats['unique_week']}\n"
        f"🗓️ **Уникальных за месяц:** {stats['unique_month']}\n\n"
        f"🏆 **ТОП-10 ПО ВИЗИТАМ:**\n"
    )

    for i, (user_id, username, first_name, visits) in enumerate(stats['top_users'], 1):
        name = first_name or username or f"ID:{user_id}"
        message += f"{i}. {name} — {visits} визитов\n"

    await update.message.reply_text(message, parse_mode='Markdown')


# Команда /start (ОБНОВЛЕНА С ДОБАВЛЕНИЕМ СЧЕТЧИКА)
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user

    # НОВОЕ: Обновляем счетчик посещений
    visit_count = database.update_visit_counter(
        user_id=user.id,
        username=user.username,
        first_name=user.first_name,
        last_name=user.last_name
    )

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('SELECT * FROM users WHERE user_id = ?', (user.id,))
    existing_user = cur.fetchone()
    conn.close()

    if existing_user:
        await update.message.reply_text(
            f"🍎 С возвращением, {user.first_name}!\n"
            f"🌟 Это ваш {visit_count}-й визит в бота!",
            reply_markup=main_menu_keyboard()
        )
    else:
        welcome_text = f"""
🍏 Привет, {user.first_name}! 

Я - твой персональный нутрициолог! 🌟
Это твой первый визит в бота!

Помогу тебе:
• 📊 Следить за питанием и калориями
• 🎯 Достигать целей по весу  
• 💪 Контролировать белки, жиры, углеводы
• 🧠 Получать умные рекомендации

Давай создадим твой персональный план! 🚀
        """
        await update.message.reply_text(welcome_text)
        reply_keyboard = [['👨 Мужской', '👩 Женский']]
        await update.message.reply_text(
            'Для начала скажи, какой у тебя пол?',
            reply_markup=ReplyKeyboardMarkup(reply_keyboard, one_time_keyboard=True)
        )
        return GENDER


async def gender(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['gender'] = update.message.text
    await update.message.reply_text('Сколько тебе лет?', reply_markup=ReplyKeyboardRemove())
    return AGE


async def age(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        age_val = int(update.message.text)
        if age_val < 10 or age_val > 100:
            await update.message.reply_text('Пожалуйста, введите реальный возраст (10-100 лет):')
            return AGE
        context.user_data['age'] = age_val
        await update.message.reply_text('Какой у тебя рост (в см)?')
        return HEIGHT
    except ValueError:
        await update.message.reply_text('Пожалуйста, введи число.')


async def height(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        height_val = int(update.message.text)
        if height_val < 100 or height_val > 250:
            await update.message.reply_text('Пожалуйста, введите реальный рост (100-250 см):')
            return HEIGHT
        context.user_data['height'] = height_val
        await update.message.reply_text('Сколько ты весишь (в кг)?')
        return WEIGHT
    except ValueError:
        await update.message.reply_text('Пожалуйста, введи число.')


async def weight(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        weight_val = float(update.message.text)
        if weight_val < 30 or weight_val > 300:
            await update.message.reply_text('Пожалуйста, введите реальный вес (30-300 кг):')
            return WEIGHT
        context.user_data['weight'] = weight_val
        reply_keyboard = [['Похудение', 'Поддержание'], ['Набор массы']]
        await update.message.reply_text(
            'Какова твоя цель?',
            reply_markup=ReplyKeyboardMarkup(reply_keyboard, one_time_keyboard=True)
        )
        return GOAL
    except ValueError:
        await update.message.reply_text('Пожалуйста, введи число.')


async def goal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    goal_map = {'Похудение': 'loss', 'Поддержание': 'maintain', 'Набор массы': 'gain'}
    context.user_data['goal'] = goal_map.get(update.message.text, 'maintain')
    reply_keyboard = [['Сидячий', 'Легкая'], ['Умеренная', 'Высокая']]
    await update.message.reply_text(
        'Какой у тебя уровень активности?',
        reply_markup=ReplyKeyboardMarkup(reply_keyboard, one_time_keyboard=True)
    )
    return ACTIVITY


async def activity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    activity_map = {'Сидячий': 1.2, 'Легкая': 1.375, 'Умеренная': 1.55, 'Высокая': 1.725}
    activity_factor = activity_map.get(update.message.text, 1.2)
    context.user_data['activity'] = activity_factor
    context.user_data['activity_level'] = update.message.text

    # Расчет нормы калорий
    user_data = context.user_data
    if user_data['gender'] == '👨 Мужской' or user_data['gender'] == 'Мужской':
        bmr = 88.362 + (13.397 * user_data['weight']) + (4.799 * user_data['height']) - (5.677 * user_data['age'])
    else:
        bmr = 447.593 + (9.247 * user_data['weight']) + (3.098 * user_data['height']) - (4.330 * user_data['age'])

    tdee = bmr * activity_factor
    goal_factor = {'loss': 0.85, 'maintain': 1.0, 'gain': 1.15}
    daily_calories = int(tdee * goal_factor[user_data['goal']])

    # Сохранение пользователя
    user_id = update.message.from_user.id
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('''
        INSERT OR REPLACE INTO users 
        (user_id, username, full_name, age, gender, height, weight, goal, activity_level, daily_calories)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (user_id, update.message.from_user.username, update.message.from_user.full_name,
          user_data['age'], user_data['gender'], user_data['height'], user_data['weight'],
          user_data['goal'], user_data['activity_level'], daily_calories))

    # Настройки по умолчанию
    cur.execute('INSERT OR IGNORE INTO user_settings (user_id) VALUES (?)', (user_id,))
    conn.commit()
    conn.close()

    # Отслеживаем активность
    await track_activity(user_id, 'command')

    await update.message.reply_text(
        f'🎉 Регистрация завершена!\n\n'
        f'📊 Ваша дневная норма:\n'
        f'• Калории: {daily_calories} ккал\n'
        f'• Белки: {user_data["weight"] * 1.5:.0f}г\n'
        f'• Жиры: {user_data["weight"] * 0.8:.0f}г\n'
        f'• Углеводы: {(daily_calories - user_data["weight"] * 1.5 * 4 - user_data["weight"] * 0.8 * 9) / 4:.0f}г\n\n'
        f'Теперь ты можешь отслеживать свое питание!',
        reply_markup=main_menu_keyboard()
    )
    return ConversationHandler.END


# Статистика за сегодня
async def show_today_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    await track_activity(user_id, 'command')
    today = datetime.date.today().isoformat()

    conn = get_db_connection()
    cur = conn.cursor()

    # Получаем данные пользователя
    cur.execute('SELECT * FROM users WHERE user_id = ?', (user_id,))
    user_data = cur.fetchone()

    if not user_data:
        await update.message.reply_text('Сначала завершите регистрацию через /start')
        return

    # Получаем сегодняшние данные о питании
    cur.execute('''
        SELECT SUM(calories) as calories, SUM(protein) as protein, 
               SUM(fat) as fat, SUM(carbs) as carbs 
        FROM food_diary 
        WHERE user_id = ? AND date = ?
    ''', (user_id, today))
    totals = cur.fetchone()

    # Получаем список приемов пищи за сегодня
    cur.execute('''
        SELECT meal_type, product_name, grams, calories 
        FROM food_diary 
        WHERE user_id = ? AND date = ? 
        ORDER BY entry_id
    ''', (user_id, today))
    meals = cur.fetchall()

    conn.close()

    # Формируем сообщение
    message = f"📊 Статистика за {today}\n\n"

    if totals['calories']:
        calorie_percentage = (totals['calories'] / user_data['daily_calories']) * 100
        message += f"• Калории: {totals['calories']:.0f}/{user_data['daily_calories']} ({calorie_percentage:.1f}%)\n"
        message += f"• Белки: {totals['protein']:.1f}г\n"
        message += f"• Жиры: {totals['fat']:.1f}г\n"
        message += f"• Углеводы: {totals['carbs']:.1f}г\n\n"

        # Прогресс-бар для калорий
        bars = int(calorie_percentage / 10)
        progress_bar = "█" * bars + "░" * (10 - bars)
        message += f"Прогресс: [{progress_bar}] {calorie_percentage:.1f}%\n\n"

        # Список приемов пищи
        message += "🍽 Приемы пищи сегодня:\n"
        for meal in meals:
            message += f"• {meal['meal_type']}: {meal['product_name']} - {meal['grams']}г ({meal['calories']:.0f} ккал)\n"
    else:
        message += "Сегодня еще не было введено приемов пищи.\n"
        message += "Используйте '🍽 Ввести прием пищи' чтобы добавить."

    await update.message.reply_text(message)


async def show_recommendations(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    await track_activity(user_id, 'command')
    today = datetime.date.today().isoformat()

    print(f"🔍 DEBUG: Запрос рекомендаций от пользователя {user_id}")

    conn = get_db_connection()
    cur = conn.cursor()

    # Получаем данные пользователя
    cur.execute('SELECT * FROM users WHERE user_id = ?', (user_id,))
    user_data = cur.fetchone()

    if not user_data:
        await update.message.reply_text('Сначала завершите регистрацию через /start')
        conn.close()
        return

    # Получаем сегодняшние данные о питании
    cur.execute('''
        SELECT SUM(calories) as calories, SUM(protein) as protein, 
               SUM(fat) as fat, SUM(carbs) as carbs 
        FROM food_diary 
        WHERE user_id = ? AND date = ?
    ''', (user_id, today))
    totals = cur.fetchone()
    conn.close()

    print(f"🔍 DEBUG: Данные пользователя: {dict(user_data)}")
    print(f"🔍 DEBUG: Данные питания: {dict(totals)}")

    if not totals['calories'] or totals['calories'] is None:
        print("🔍 DEBUG: Нет данных о питании за сегодня")
        await update.message.reply_text(
            "📝 Сегодня еще нет данных о питании.\n"
            "Введите прием пищи чтобы получить персональные рекомендации."
        )
        return

    await update.message.reply_text("🧠 Анализирую ваше питание...")

    # Всегда используем локальные рекомендации (ИИ API требует реальный ключ)
    print("🔧 DEBUG: Используем локальные рекомендации")
    try:
        local_recommendations = generate_local_recommendations(dict(user_data), dict(totals))
        print(f"🔧 DEBUG: Сгенерированы локальные рекомендации")
        message = f"💡 Персональные рекомендации:\n\n{local_recommendations}"
        await update.message.reply_text(message)
    except Exception as e:
        print(f"❌ Ошибка генерации рекомендаций: {e}")
        await update.message.reply_text(
            "📊 На основе ваших данных:\n\n"
            f"• Съедено калорий: {totals['calories']:.0f} из {user_data['daily_calories']}\n"
            f"• Белки: {totals['protein']:.1f}г\n"
            f"• Жиры: {totals['fat']:.1f}г\n"
            f"• Углеводы: {totals['carbs']:.1f}г\n\n"
            "💡 Продолжайте следить за питанием!"
        )


# Обработчик ввода веса
async def weight_tracking_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text('Введите ваш текущий вес (в кг):')
    return WEIGHT_TRACKING


async def handle_weight_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        weight_val = float(update.message.text)

        if weight_val < 30 or weight_val > 300:
            await update.message.reply_text('Пожалуйста, введите реальный вес (30-300 кг):')
            return WEIGHT_TRACKING

        user_id = update.message.from_user.id

        # Сохраняем в базу данных
        conn = get_db_connection()
        cur = conn.cursor()

        # Обновляем вес в профиле
        cur.execute('UPDATE users SET weight = ? WHERE user_id = ?', (weight_val, user_id))

        # Добавляем запись в историю веса
        cur.execute('INSERT INTO weight_tracking (user_id, weight) VALUES (?, ?)', (user_id, weight_val))

        conn.commit()
        conn.close()

        # Отслеживаем активность
        await track_activity(user_id, 'weight')

        await update.message.reply_text(
            f'✅ Вес {weight_val} кг сохранен!',
            reply_markup=main_menu_keyboard()
        )

        return ConversationHandler.END

    except ValueError:
        await update.message.reply_text('Пожалуйста, введите число:')
        return WEIGHT_TRACKING


async def show_progress(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    await track_activity(user_id, 'command')
    
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('SELECT date, weight FROM weight_tracking WHERE user_id = ? ORDER BY date', (user_id,))
    records = cur.fetchall()
    conn.close()

    if len(records) < 2:
        await update.message.reply_text('📊 Для анализа прогресса нужно как минимум 2 записи о весе.\nВведите вес через кнопку "⚖️ Ввести вес"')
        return
    
    # Текстовый отчет вместо графика
    first_weight = records[0]['weight']
    last_weight = records[-1]['weight']
    diff = last_weight - first_weight
    trend = "📉 снизился" if diff < 0 else "📈 увеличился" if diff > 0 else "🔸 не изменился"
    
    message = f"📊 **Ваш прогресс:**\n\n"
    message += f"• Первая запись: {first_weight} кг\n"
    message += f"• Последняя запись: {last_weight} кг\n"
    message += f"• Вес {trend} на {abs(diff):.1f} кг\n\n"
    message += f"📝 Всего записей: {len(records)}\n"
    message += f"💡 Продолжайте следить за весом!"
    
    await update.message.reply_text(message, parse_mode='Markdown')
    
# Экспорт данных в CSV
async def export_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    await track_activity(user_id, 'command')

    conn = get_db_connection()
    cur = conn.cursor()

    # Данные о питании
    cur.execute('SELECT * FROM food_diary WHERE user_id = ? ORDER BY date, entry_id', (user_id,))
    food_data = cur.fetchall()

    # Данные о весе
    cur.execute('SELECT * FROM weight_tracking WHERE user_id = ? ORDER BY date', (user_id,))
    weight_data = cur.fetchall()
    conn.close()

    if not food_data and not weight_data:
        await update.message.reply_text('Нет данных для экспорта.')
        return

    # Создание CSV
    output = io.StringIO()
    writer = csv.writer(output)

    if food_data:
        writer.writerow(['Дневник питания'])
        writer.writerow(['Дата', 'Прием пищи', 'Продукт', 'Граммы', 'Калории', 'Белки', 'Жиры', 'Углеводы'])
        for row in food_data:
            writer.writerow([row['date'], row['meal_type'], row['product_name'], row['grams'],
                             row['calories'], row['protein'], row['fat'], row['carbs']])

    if weight_data:
        writer.writerow([])
        writer.writerow(['Отслеживание веса'])
        writer.writerow(['Дата', 'Вес (кг)'])
        for row in weight_data:
            writer.writerow([row['date'], row['weight']])

    output.seek(0)
    csv_data = output.getvalue().encode('utf-8')
    output.close()

    await update.message.reply_document(
        document=io.BytesIO(csv_data),
        filename=f'nutrition_data_{datetime.date.today()}.csv',
        caption='📤 Ваши данные экспортированы в CSV'
    )


# Настройка уведомлений
async def notification_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reply_keyboard = [['09:00', '12:00'], ['18:00', 'Выключить']]
    await update.message.reply_text(
        'Выберите время уведомлений:',
        reply_markup=ReplyKeyboardMarkup(reply_keyboard, one_time_keyboard=True)
    )
    return 0


async def handle_notification_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    time_str = update.message.text

    conn = get_db_connection()
    cur = conn.cursor()

    if time_str == 'Выключить':
        cur.execute('UPDATE user_settings SET notifications_enabled = FALSE WHERE user_id = ?', (user_id,))
        message = '🔕 Уведомления выключены.'
    else:
        cur.execute('UPDATE user_settings SET notifications_enabled = TRUE, notification_time = ? WHERE user_id = ?',
                    (time_str, user_id))
        message = f'🔔 Уведомления включены на {time_str}'

    conn.commit()
    conn.close()
    await update.message.reply_text(message, reply_markup=main_menu_keyboard())
    return ConversationHandler.END


# ИСПРАВЛЕННАЯ ФУНКЦИЯ "Мой профиль"
async def show_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    await track_activity(user_id, 'command')

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('SELECT * FROM users WHERE user_id = ?', (user_id,))
    user_data = cur.fetchone()

    if not user_data:
        await update.message.reply_text('Сначала завершите регистрацию через /start')
        conn.close()
        return

    # Получаем сегодняшние данные о питании
    today = datetime.date.today().isoformat()
    cur.execute('''
        SELECT SUM(calories) as calories, SUM(protein) as protein, 
               SUM(fat) as fat, SUM(carbs) as carbs 
        FROM food_diary 
        WHERE user_id = ? AND date = ?
    ''', (user_id, today))
    totals = cur.fetchone()

    conn.close()

    # Рассчитываем БЖУ
    protein_need = user_data['weight'] * 1.5  # 1.5г белка на кг веса
    fat_need = user_data['weight'] * 0.8  # 0.8г жиров на кг веса
    carbs_need = (user_data['daily_calories'] - protein_need * 4 - fat_need * 9) / 4

    # Формируем сообщение
    message = f"👤 **Ваш профиль**\n\n"
    message += f"• **Имя:** {user_data['full_name']}\n"
    message += f"• **Возраст:** {user_data['age']} лет\n"
    message += f"• **Пол:** {user_data['gender']}\n"
    message += f"• **Рост:** {user_data['height']} см\n"
    message += f"• **Вес:** {user_data['weight']} кг\n"
    message += f"• **Цель:** {user_data['goal']}\n"
    message += f"• **Активность:** {user_data['activity_level']}\n\n"

    message += "🎯 **Рекомендации:**\n"
    message += f"• **Калории:** {user_data['daily_calories']} ккал/день\n"
    message += f"• **Белки:** {protein_need:.0f} г\n"
    message += f"• **Жиры:** {fat_need:.0f} г\n"
    message += f"• **Углеводы:** {carbs_need:.0f} г\n\n"

    if totals['calories']:
        calorie_percentage = (totals['calories'] / user_data['daily_calories']) * 100
        bars = int(calorie_percentage / 10)
        progress_bar = "█" * bars + "░" * (10 - bars)

        message += f"📊 **Сегодня:**\n"
        message += f"• Съедено: {totals['calories']:.0f}/{user_data['daily_calories']} ккал\n"
        message += f"• Прогресс: [{progress_bar}] {calorie_percentage:.1f}%\n"
    else:
        message += "📊 **Сегодня еще не было приемов пищи**\n"

    await update.message.reply_text(message, parse_mode='Markdown')


# Показать цели
async def show_goals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    await track_activity(user_id, 'command')

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('SELECT * FROM users WHERE user_id = ?', (user_id,))
    user_data = cur.fetchone()
    conn.close()

    if not user_data:
        await update.message.reply_text('Сначала завершите регистрацию через /start')
        return

    goal_text = {'loss': 'Похудение', 'maintain': 'Поддержание веса', 'gain': 'Набор массы'}
    goal_desc = {
        'loss': '🥗 Снижение веса через дефицит калорий',
        'maintain': '⚖️ Поддержание текущего веса',
        'gain': '💪 Набор мышечной массы'
    }

    message = (
        f"🎯 Ваши цели:\n\n"
        f"• Основная цель: {goal_text.get(user_data['goal'], user_data['goal'])}\n"
        f"• {goal_desc.get(user_data['goal'], '')}\n\n"
        f"📊 Дневные нормы:\n"
        f"• Калории: {user_data['daily_calories']} ккал\n"
        f"• Белки: {user_data['weight'] * 1.5:.0f}г\n"
        f"• Жиры: {user_data['weight'] * 0.8:.0f}г\n"
        f"• Углеводы: {(user_data['daily_calories'] - user_data['weight'] * 1.5 * 4 - user_data['weight'] * 0.8 * 9) / 4:.0f}г"
    )

    await update.message.reply_text(message)


# Обработчик главного меню
async def handle_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    user_id = update.message.from_user.id
    await track_activity(user_id, 'command')

    if text == '🍽 Ввести прием пищи':
        await start_meal_input(update, context)
    elif text == '📊 Статистика сегодня':
        await show_today_stats(update, context)
    elif text == '⚖️ Ввести вес':
        await weight_tracking_cmd(update, context)
    elif text == '📈 График прогресса':
        await show_progress(update, context)
    elif text == '💡 Рекомендации ИИ':
        await show_recommendations(update, context)
    elif text == '🎯 Мои цели':
        await show_goals(update, context)
    elif text == '👤 Мой профиль':
        await show_profile(update, context)  # ТЕПЕРЬ РАБОТАЕТ!
    elif text == '⚙️ Настройки':
        await notification_settings(update, context)
    elif text == '📤 Экспорт данных':
        await export_data(update, context)
    else:
        await update.message.reply_text(
            'Используйте кнопки меню для навигации',
            reply_markup=main_menu_keyboard()
        )


# Обработка ввода пищи
async def start_meal_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reply_keyboard = [['Завтрак', 'Обед'], ['Ужин', 'Перекус']]
    await update.message.reply_text(
        'Выберите прием пищи:',
        reply_markup=ReplyKeyboardMarkup(reply_keyboard, one_time_keyboard=True)
    )
    return MEAL_TYPE


async def meal_type_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['meal_type'] = update.message.text
    await update.message.reply_text('Что ты съел(а)? Укажи продукт:', reply_markup=ReplyKeyboardRemove())
    return PRODUCT_NAME


async def product_name_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    print(f"🔍 DEBUG: product_name_handler вызвана с текстом: '{update.message.text}'")
    # Если пользователь хочет завершить ввод
    if update.message.text.lower() in ['завершить', 'готово', 'стоп', 'конец', 'отмена']:
        await update.message.reply_text(
            '✅ Ввод приема пищи завершен!',
            reply_markup=main_menu_keyboard()
        )
        return ConversationHandler.END

    product_name = update.message.text
    context.user_data['product_name'] = product_name

    await update.message.reply_text("🔍 Ищу информацию о продукте...")
    product = await search_product_api(product_name)

    if product:
        context.user_data['product'] = product
        await update.message.reply_text(
            f"✅ Найдено: {product['name']}\n"
            f"💡 100г содержит: {product['calories']} ккал\n\n"
            f"Сколько грамм ты съел(а)?"
        )
        return GRAMS
    else:
        await update.message.reply_text(
            '❌ Продукт не найден в базе. Попробуй другой продукт или уточни название:'
        )
        return PRODUCT_NAME


async def grams_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Если пользователь хочет завершить ввод
    if update.message.text.lower() in ['завершить', 'готово', 'стоп', 'конец', 'отмена']:
        await update.message.reply_text(
            '✅ Ввод приема пищи завершен!',
            reply_markup=main_menu_keyboard()
        )
        return ConversationHandler.END

    try:
        grams = float(update.message.text)

        if grams <= 0 or grams > 5000:
            await update.message.reply_text('Пожалуйста, введите реальное количество грамм (1-5000):')
            return GRAMS

        product = context.user_data['product']
        meal_type = context.user_data['meal_type']
        product_name = context.user_data['product_name']

        ratio = grams / 100
        calories = product['calories'] * ratio
        protein = product['protein_g'] * ratio
        fat = product['fat_total_g'] * ratio
        carbs = product['carbohydrates_total_g'] * ratio

        user_id = update.message.from_user.id

        # Сохраняем в базу данных
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute('''
            INSERT INTO food_diary (user_id, meal_type, product_name, grams, calories, protein, fat, carbs)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (user_id, meal_type, product_name, grams, calories, protein, fat, carbs))
        conn.commit()
        conn.close()

        # Отслеживаем активность
        await track_activity(user_id, 'food')

        await update.message.reply_text(
            f'✅ Успешно добавлено!\n\n'
            f'🍽 {meal_type}: {product_name} - {grams}г\n'
            f'📊 Пищевая ценность:\n'
            f'• Калории: {calories:.0f} ккал\n'
            f'• Белки: {protein:.1f}г\n'
            f'• Жиры: {fat:.1f}г\n'
            f'• Углеводы: {carbs:.1f}г\n\n'
            f'💡 Чтобы добавить еще продукт, введите его название\n'
            f'💡 Чтобы завершить ввод, напишите "завершить"'
        )

        # Возвращаемся к вводу названия продукта для добавления следующего
        return PRODUCT_NAME

    except ValueError:
        await update.message.reply_text('Пожалуйста, введи число:')
        return GRAMS


# Функция отмены для ConversationHandler
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        '❌ Операция отменена.',
        reply_markup=main_menu_keyboard()
    )
    return ConversationHandler.END


def main():
    # Токен теперь берется из переменной окружения
    application = Application.builder().token(BOT_TOKEN).build()

    # Регистрация
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('start', start)],
        states={
            GENDER: [MessageHandler(filters.Regex('^(👨 Мужской|👩 Женский)$'), gender)],
            AGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, age)],
            HEIGHT: [MessageHandler(filters.TEXT & ~filters.COMMAND, height)],
            WEIGHT: [MessageHandler(filters.TEXT & ~filters.COMMAND, weight)],
            GOAL: [MessageHandler(filters.Regex('^(Похудение|Поддержание|Набор массы)$'), goal)],
            ACTIVITY: [MessageHandler(filters.Regex('^(Сидячий|Легкая|Умеренная|Высокая)$'), activity)],
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    )

    # Ввод пищи
    meal_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex('^🍽 Ввести прием пищи$'), start_meal_input)],
        states={
            MEAL_TYPE: [MessageHandler(filters.Regex('^(Завтрак|Обед|Ужин|Перекус)$'), meal_type_handler)],
            PRODUCT_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, product_name_handler)],
            GRAMS: [MessageHandler(filters.TEXT & ~filters.COMMAND, grams_handler)],
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    )

    # Ввод веса
    weight_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex('^⚖️ Ввести вес$'), weight_tracking_cmd)],
        states={
            WEIGHT_TRACKING: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_weight_input)]
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    )

    # Уведомления
    notif_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex('^⚙️ Настройки$'), notification_settings)],
        states={
            0: [MessageHandler(filters.Regex('^(09:00|12:00|18:00|Выключить)$'), handle_notification_time)]
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    )

    # ВАЖНО: Сначала добавляем ConversationHandler, потом общий обработчик
    application.add_handler(conv_handler)
    application.add_handler(meal_conv)
    application.add_handler(weight_conv)
    application.add_handler(notif_conv)

    # Команды
    application.add_handler(CommandHandler("stats", show_today_stats))
    application.add_handler(CommandHandler("profile", show_profile))
    application.add_handler(CommandHandler("goals", show_goals))
    application.add_handler(CommandHandler("export", export_data))
    application.add_handler(CommandHandler("recommendations", show_recommendations))
    application.add_handler(CommandHandler("progress", show_progress))

    # НОВАЯ КОМАНДА ДЛЯ СТАТИСТИКИ ПОСЕЩЕНИЙ
    application.add_handler(CommandHandler("admin_stats", admin_stats))

    # Общий обработчик текстовых сообщений (должен быть ПОСЛЕДНИМ)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_main_menu))

    # Запуск бота
    print("🤖 Бот-нутрициолог запускается...")
    print(f"✅ Токен загружен: {BOT_TOKEN[:10]}...")
    print("✅ Нажмите Ctrl+C для остановки")

    # ЗАПУСК БОТА
    application.run_polling(
        timeout=30,
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES
    )


if __name__ == '__main__':
    # Инициализация баз данных
    init_db()
    main()
