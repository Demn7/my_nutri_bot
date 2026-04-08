import sqlite3
from datetime import datetime, timedelta


DB_NAME = 'visits.db'


def init_db():
    """Инициализация базы данных для счетчика посещений"""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    # Таблица посещений пользователей
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS user_visits (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            first_visit TIMESTAMP,
            last_visit TIMESTAMP,
            visit_count INTEGER DEFAULT 0
        )
    ''')

    # Таблица для ежедневной статистики
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS daily_stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date DATE UNIQUE,
            new_users INTEGER DEFAULT 0,
            total_visits INTEGER DEFAULT 0
        )
    ''')

    conn.commit()
    conn.close()


def update_visit_counter(user_id, username=None, first_name=None, last_name=None):
    """Обновление счетчика посещений пользователя"""
    now = datetime.now()
    today = now.date()

    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    # Проверяем, есть ли пользователь
    cursor.execute('SELECT visit_count FROM user_visits WHERE user_id = ?', (user_id,))
    result = cursor.fetchone()

    if result:
        # Пользователь уже был - увеличиваем счетчик
        visit_count = result[0] + 1
        cursor.execute('''
            UPDATE user_visits 
            SET username = ?, first_name = ?, last_name = ?, 
                last_visit = ?, visit_count = ?
            WHERE user_id = ?
        ''', (username, first_name, last_name, now, visit_count, user_id))

        # Обновляем total_visits в daily_stats
        cursor.execute('''
            INSERT INTO daily_stats (date, new_users, total_visits)
            VALUES (?, 0, 1)
            ON CONFLICT(date) DO UPDATE SET
                total_visits = total_visits + 1
        ''', (today,))

    else:
        # Новый пользователь
        visit_count = 1
        cursor.execute('''
            INSERT INTO user_visits 
            (user_id, username, first_name, last_name, first_visit, last_visit, visit_count)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (user_id, username, first_name, last_name, now, now, visit_count))

        # Обновляем статистику новых пользователей за сегодня
        cursor.execute('''
            INSERT INTO daily_stats (date, new_users, total_visits)
            VALUES (?, 1, 1)
            ON CONFLICT(date) DO UPDATE SET
                new_users = new_users + 1,
                total_visits = total_visits + 1
        ''', (today,))

    conn.commit()
    conn.close()

    return visit_count


def get_visit_stats():
    """Получение статистики посещений для админа"""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    # Общее количество пользователей
    cursor.execute('SELECT COUNT(*) FROM user_visits')
    total_users = cursor.fetchone()[0]

    # Общее количество визитов
    cursor.execute('SELECT SUM(visit_count) FROM user_visits')
    total_visits = cursor.fetchone()[0] or 0

    # Уникальные пользователи за сегодня
    today = datetime.now().date()
    cursor.execute('''
        SELECT COUNT(*) FROM user_visits 
        WHERE DATE(last_visit) = ?
    ''', (today,))
    unique_today = cursor.fetchone()[0]

    # Уникальные пользователи за неделю
    week_ago = today - timedelta(days=7)
    cursor.execute('''
        SELECT COUNT(*) FROM user_visits 
        WHERE DATE(last_visit) >= ?
    ''', (week_ago,))
    unique_week = cursor.fetchone()[0]

    # Уникальные пользователи за месяц
    month_ago = today - timedelta(days=30)
    cursor.execute('''
        SELECT COUNT(*) FROM user_visits 
        WHERE DATE(last_visit) >= ?
    ''', (month_ago,))
    unique_month = cursor.fetchone()[0]

    # Топ-10 пользователей по визитам
    cursor.execute('''
        SELECT user_id, username, first_name, visit_count 
        FROM user_visits 
        ORDER BY visit_count DESC 
        LIMIT 10
    ''')
    top_users = cursor.fetchall()

    conn.close()


    return {
        'total_users': total_users,
        'total_visits': total_visits,
        'unique_today': unique_today,
        'unique_week': unique_week,
        'unique_month': unique_month,
        'top_users': top_users
    }
