import telebot
import json
import time
import re
import uuid
import sqlite3
from datetime import date
from google import genai
from google.genai.errors import APIError 

# ==========================================
# 1. НАСТРОЙКИ (ЗАПОЛНИТЕ ЭТО!)
# ==========================================

TELEGRAM_BOT_TOKEN = '7594215138:AAF-hobWwJ967treL60z0Xz6Z6Q7lhdPTgk' 
ADMIN_USER_ID = 1059221485 
GEMINI_API_KEY = 'AIzaSyBJpz0NIy6X_GXAlz2u68VRTuQhXNKscLM' 

# Ваш Username без @ (нужен для контактов в случае ошибок)
ADMIN_USERNAME = 'Abduaziz_Admin' 
# Номер карты для приема оплаты
ADMIN_CARD_NUMBER = '9860196617892605' 

DAILY_LIMIT = 3 
GEMINI_MODEL = 'gemini-2.5-flash' 
TELEGRAM_MAX_LENGTH = 4096 

# ==========================================
# 2. ИНИЦИАЛИЗАЦИЯ
# ==========================================

LIMITS_FILE = 'user_limits.json'
DB_NAME = 'orders.db'
bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)

# Тарифы: (Кол-во запросов, Цена, Валюта)
TARIFFS = {
    'buy_25': (25, 5000, 'UZS'),
    'buy_50': (50, 10000, 'UZS'),
    'buy_100': (100, 20000, 'UZS'),
    'buy_500': (500, 100000, 'UZS'),
    'buy_1000': (1000, 200000, 'UZS')
}

gemini_client = None
try:
    gemini_client = genai.Client(api_key=GEMINI_API_KEY)
    print("✅ Gemini Client успешно инициализирован.")
except Exception as e:
    print(f"❌ Ошибка Gemini: {e}")

# ==========================================
# 3. БАЗА ДАННЫХ (SQLite)
# ==========================================

def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            order_id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            requests INTEGER NOT NULL,
            price INTEGER NOT NULL,
            currency TEXT NOT NULL,
            status TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()

def create_order(order_id, user_id, requests, price, currency):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?)", 
                       (order_id, user_id, requests, price, currency, 'pending'))
        conn.commit()
        return True
    except Exception as e:
        print(f"DB Error: {e}")
        return False
    finally:
        conn.close()

def get_order(order_id):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM orders WHERE order_id = ?", (order_id,))
    row = cursor.fetchone()
    conn.close()
    if row:
        return {'order_id': row[0], 'user_id': row[1], 'requests': row[2], 
                'price': row[3], 'currency': row[4], 'status': row[5]}
    return None

def update_order_status(order_id, status):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("UPDATE orders SET status = ? WHERE order_id = ?", (status, order_id))
    conn.commit()
    conn.close()

def delete_order(order_id):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM orders WHERE order_id = ?", (order_id,))
    conn.commit()
    conn.close()

# ==========================================
# 4. ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ==========================================

def escape_markdown_v2(text):
    """
    Экранирует ВСЕ спецсимволы MarkdownV2.
    """
    if not text: return ""
    chars_to_escape = r'_*[]()~`>#+-=|{}.!'
    return re.sub(f'([{re.escape(chars_to_escape)}])', r'\\\1', str(text))

def load_limits():
    try:
        with open(LIMITS_FILE, 'r') as f:
            return json.load(f)
    except:
        return {}

def save_limits(limits):
    with open(LIMITS_FILE, 'w') as f:
        json.dump(limits, f, indent=4)

def check_and_update_limit(user_id, restore=False):
    limits = load_limits()
    uid = str(user_id)
    today = str(date.today())

    if uid not in limits:
        limits[uid] = {'date': today, 'remaining': DAILY_LIMIT, 'registered_date': today}
    elif limits[uid].get('date') != today:
        limits[uid]['date'] = today
        limits[uid]['remaining'] = DAILY_LIMIT
        if 'registered_date' not in limits[uid]: limits[uid]['registered_date'] = today

    if restore:
        if limits[uid]['remaining'] < DAILY_LIMIT:
            limits[uid]['remaining'] += 1
            save_limits(limits)
        return True, limits[uid]['remaining']

    if limits[uid]['remaining'] > 0:
        limits[uid]['remaining'] -= 1
        save_limits(limits)
        return True, limits[uid]['remaining']
    
    return False, 0

def add_requests(user_id, amount):
    limits = load_limits()
    uid = str(user_id)
    today = str(date.today())
    
    if uid not in limits:
        limits[uid] = {'date': today, 'remaining': DAILY_LIMIT + amount, 'registered_date': today}
    elif limits[uid].get('date') != today:
        limits[uid]['date'] = today
        limits[uid]['remaining'] = DAILY_LIMIT + amount
    else:
        limits[uid]['remaining'] += amount
    
    save_limits(limits)
    return limits[uid]['remaining']

def split_text(text):
    if len(text) <= TELEGRAM_MAX_LENGTH: return [text]
    chunks = []
    while text:
        if len(text) <= TELEGRAM_MAX_LENGTH:
            chunks.append(text)
            break
        chunk = text[:TELEGRAM_MAX_LENGTH]
        text = text[TELEGRAM_MAX_LENGTH:]
        chunks.append(chunk)
    return chunks

# ==========================================
# 5. КЛАВИАТУРЫ
# ==========================================

def kb_main(user_id):
    markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add('📊 Лимит', '❓ Помощь')
    if user_id == ADMIN_USER_ID:
        markup.add('🛠️ Админ')
    return markup

def kb_admin():
    markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add('📊 Статистика', '➕ Добавить запросы', '📝 Установить лимит', 
               '👥 Все пользователи', '📢 Бродкаст', '⬅️ Назад')
    return markup

def kb_tariffs():
    markup = telebot.types.InlineKeyboardMarkup(row_width=2)
    for k, v in TARIFFS.items():
        price_fmt = f"{v[1]:,}".replace(",", " ")
        btn = telebot.types.InlineKeyboardButton(f"{v[0]} зап. - {price_fmt} {v[2]}", callback_data=k)
        markup.add(btn)
    return markup

def kb_confirm(order_id):
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("✅ Подтвердить", callback_data=f"conf_{order_id}"),
               telebot.types.InlineKeyboardButton("❌ Отмена", callback_data=f"cancel_{order_id}"))
    return markup

def kb_paid(order_id):
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("✅ Я оплатил", callback_data=f"paid_{order_id}"),
               telebot.types.InlineKeyboardButton("❌ Отмена", callback_data=f"cancel_{order_id}"))
    return markup

def kb_admin_check(order_id, user_id):
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("✅ Одобрить", callback_data=f"grant_{order_id}_{user_id}"),
               telebot.types.InlineKeyboardButton("❌ Отклонить", callback_data=f"deny_{order_id}_{user_id}"))
    return markup

# ==========================================
# 6. ЛОГИКА ОПЛАТЫ (CALLBACKS)
# ==========================================

@bot.callback_query_handler(func=lambda call: True)
def handle_callbacks(call):
    user_id = call.from_user.id
    data = call.data
    
    # 1. Выбор тарифа
    if data.startswith('buy_'):
        requests, price, currency = TARIFFS[data]
        order_id = str(uuid.uuid4())[:8]
        
        if create_order(order_id, user_id, requests, price, currency):
            price_fmt = f"{price:,}".replace(",", " ")
            text = (f"💰 Вы выбрали:\n"
                    f"- Запросов: **{requests}**\n"
                    f"- К оплате: **{price_fmt} {currency}**\n\n"
                    f"Подтвердите заказ.")
            
            # Удаляем старую клавиатуру
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
            # Отправляем новое сообщение
            bot.send_message(user_id, escape_markdown_v2(text), parse_mode='MarkdownV2', reply_markup=kb_confirm(order_id))
    
    # 2. Подтверждение -> Показ реквизитов
    elif data.startswith('conf_'):
        order_id = data.split('_')[1]
        order = get_order(order_id)
        if order:
            update_order_status(order_id, 'wait_pay')
            price_fmt = f"{order['price']:,}".replace(",", " ")
            text = (f"💳 **Реквизиты для оплаты**:\n\n"
                    f"- Карта: `{ADMIN_CARD_NUMBER}`\n"
                    f"- Сумма: **{price_fmt} {order['currency']}**\n"
                    f"- ID Заказа: `{order_id}`\n\n"
                    f"После оплаты нажмите **'Я оплатил'**.")
            bot.edit_message_text(escape_markdown_v2(text), call.message.chat.id, call.message.message_id, 
                                  parse_mode='MarkdownV2', reply_markup=kb_paid(order_id))
    
    # 3. Нажал "Я оплатил"
    elif data.startswith('paid_'):
        order_id = data.split('_')[1]
        update_order_status(order_id, 'wait_check')
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        
        msg = bot.send_message(user_id, escape_markdown_v2("📄 Пожалуйста, отправьте **скриншот чека** (фото или файл)."), parse_mode='MarkdownV2')
        bot.register_next_step_handler(msg, process_check, order_id)
        
    # 4. Отмена
    elif data.startswith('cancel_'):
        order_id = data.split('_')[1]
        delete_order(order_id)
        bot.edit_message_text(escape_markdown_v2("❌ Заказ отменен."), call.message.chat.id, call.message.message_id, parse_mode='MarkdownV2')

    # 5. Админ: Одобрить
    elif data.startswith('grant_'):
        _, order_id, target_user = data.split('_')
        order = get_order(order_id)
        if order:
            new_bal = add_requests(target_user, order['requests'])
            # Уведомляем юзера
            try:
                bot.send_message(target_user, escape_markdown_v2(f"✅ Оплата подтверждена!\nВам начислено {order['requests']} запросов.\nВсего: {new_bal}"), parse_mode='MarkdownV2')
            except: pass
            
            # Ответ админу
            bot.edit_message_caption(chat_id=call.message.chat.id, message_id=call.message.message_id,
                                     caption=escape_markdown_v2(f"✅ Заказ {order_id} одобрен. Запросы начислены."), parse_mode='MarkdownV2')
            delete_order(order_id)
    
    # 6. Админ: Отклонить
    elif data.startswith('deny_'):
        _, order_id, target_user = data.split('_')
        try:
            bot.send_message(target_user, escape_markdown_v2(f"❌ Ваша оплата по заказу {order_id} отклонена администратором."), parse_mode='MarkdownV2')
        except: pass
        
        bot.edit_message_caption(chat_id=call.message.chat.id, message_id=call.message.message_id,
                                 caption=escape_markdown_v2(f"❌ Заказ {order_id} отклонен."), parse_mode='MarkdownV2')
        delete_order(order_id)

def process_check(message, order_id):
    if not (message.photo or message.document):
        msg = bot.send_message(message.chat.id, escape_markdown_v2("❌ Это не фото. Отправьте скриншот чека."), parse_mode='MarkdownV2')
        bot.register_next_step_handler(msg, process_check, order_id)
        return

    order = get_order(order_id)
    if not order: return

    bot.send_message(message.chat.id, escape_markdown_v2("✅ Чек отправлен на проверку админу."), parse_mode='MarkdownV2')

    # Шлём админу
    caption = (f"💰 **НОВЫЙ ЧЕК**\n"
               f"- User ID: `{message.from_user.id}`\n"
               f"- Username: @{escape_markdown_v2(str(message.from_user.username))}\n"
               f"- Сумма: **{order['price']}**\n"
               f"- Запросов: **{order['requests']}**")
    
    if message.photo:
        bot.send_photo(ADMIN_USER_ID, message.photo[-1].file_id, caption=escape_markdown_v2(caption), 
                       parse_mode='MarkdownV2', reply_markup=kb_admin_check(order_id, message.from_user.id))
    elif message.document:
        bot.send_document(ADMIN_USER_ID, message.document.file_id, caption=escape_markdown_v2(caption), 
                          parse_mode='MarkdownV2', reply_markup=kb_admin_check(order_id, message.from_user.id))

# ==========================================
# 7. ОБРАБОТЧИКИ СООБЩЕНИЙ
# ==========================================

@bot.message_handler(commands=['start'])
def start(message):
    check_and_update_limit(message.from_user.id, restore=True)
    text = "🤖 Привет! Я AI-ассистент на базе Gemini.\nЗадай мне вопрос."
    bot.send_message(message.chat.id, escape_markdown_v2(text), parse_mode='MarkdownV2', reply_markup=kb_main(message.from_user.id))

@bot.message_handler(func=lambda m: m.text == '❓ Помощь' or m.text == '/help')
def help_handler(message):
    text = f"❓ **Помощь**\n- Лимит: **{DAILY_LIMIT}** запросов/день.\n- Если лимит кончился, можно докупить."
    bot.send_message(message.chat.id, escape_markdown_v2(text), parse_mode='MarkdownV2')

@bot.message_handler(func=lambda m: m.text == '📊 Лимит' or m.text == '/limit')
def limit_handler(message):
    limits = load_limits()
    uid = str(message.from_user.id)
    today = str(date.today())
    
    rem = DAILY_LIMIT
    if uid in limits and limits[uid]['date'] == today:
        rem = limits[uid]['remaining']
    
    text = f"📊 **Статус**\n- Доступно: **{rem}**\n- Сброс: завтра"
    bot.send_message(message.chat.id, escape_markdown_v2(text), parse_mode='MarkdownV2')

# --- АДМИНКА ---

@bot.message_handler(func=lambda m: m.text == '🛠️ Админ')
def admin_menu(message):
    if message.from_user.id != ADMIN_USER_ID: return
    bot.send_message(message.chat.id, escape_markdown_v2("🛠️ Админ-панель"), parse_mode='MarkdownV2', reply_markup=kb_admin())

@bot.message_handler(func=lambda m: m.text == '⬅️ Назад')
def back(message):
    if message.from_user.id != ADMIN_USER_ID: return
    bot.send_message(message.chat.id, escape_markdown_v2("Главное меню"), parse_mode='MarkdownV2', reply_markup=kb_main(ADMIN_USER_ID))

@bot.message_handler(func=lambda m: m.text == '👥 Все пользователи')
def all_users(message):
    if message.from_user.id != ADMIN_USER_ID: return
    limits = load_limits()
    if not limits:
        bot.send_message(message.chat.id, "Пусто.")
        return
    
    lines = []
    for uid, data in limits.items():
        lines.append(f"`{uid}` | Ост: {data.get('remaining')} | Рег: {data.get('registered_date')}")
    
    full_text = "\n".join(lines)
    for chunk in split_text(full_text):
        bot.send_message(message.chat.id, escape_markdown_v2(chunk), parse_mode='MarkdownV2')

@bot.message_handler(func=lambda m: m.text == '📢 Бродкаст')
def broadcast_step1(message):
    if message.from_user.id != ADMIN_USER_ID: return
    msg = bot.send_message(message.chat.id, escape_markdown_v2("📢 Введите текст рассылки (поддерживается MarkdownV2):"), parse_mode='MarkdownV2')
    bot.register_next_step_handler(msg, broadcast_step2)

def broadcast_step2(message):
    text = message.text
    limits = load_limits()
    count = 0
    bot.send_message(message.chat.id, escape_markdown_v2(f"⏳ Рассылка на {len(limits)} юзеров..."), parse_mode='MarkdownV2')
    
    for uid in limits:
        try:
            # Тут мы НЕ экранируем текст, так как админ сам пишет форматирование.
            # Если админ ошибется в разметке, отправим как plain text.
            try:
                bot.send_message(int(uid), text, parse_mode='MarkdownV2')
            except:
                bot.send_message(int(uid), text) # fallback без форматирования
            count += 1
            time.sleep(0.1)
        except Exception as e:
            pass # Юзер блокнул бота и т.д.
            
    bot.send_message(message.chat.id, escape_markdown_v2(f"✅ Рассылка завершена. Отправлено: {count}"), parse_mode='MarkdownV2')

@bot.message_handler(func=lambda m: m.text == '➕ Добавить запросы')
def add_req_step1(message):
    if message.from_user.id != ADMIN_USER_ID: return
    msg = bot.send_message(message.chat.id, escape_markdown_v2("📝 Формат: ID КОЛИЧЕСТВО\nПример: 123456 10"), parse_mode='MarkdownV2')
    bot.register_next_step_handler(msg, add_req_step2)

def add_req_step2(message):
    try:
        uid, amt = map(int, message.text.split())
        new = add_requests(uid, amt)
        bot.send_message(message.chat.id, escape_markdown_v2(f"✅ Добавлено. Новый лимит: {new}"), parse_mode='MarkdownV2')
        try: bot.send_message(uid, escape_markdown_v2(f"🎉 Вам добавлено {amt} запросов!"), parse_mode='MarkdownV2')
        except: pass
    except:
        bot.send_message(message.chat.id, escape_markdown_v2("❌ Ошибка формата."), parse_mode='MarkdownV2')

@bot.message_handler(func=lambda m: m.text == '📝 Установить лимит')
def set_lim_step1(message):
    if message.from_user.id != ADMIN_USER_ID: return
    msg = bot.send_message(message.chat.id, escape_markdown_v2("📝 Формат: ID НОВЫЙ_ЛИМИТ\nПример: 123456 50"), parse_mode='MarkdownV2')
    bot.register_next_step_handler(msg, set_lim_step2)

def set_lim_step2(message):
    try:
        uid, limit = map(int, message.text.split())
        # Логика перезаписи
        limits = load_limits()
        today = str(date.today())
        if str(uid) not in limits: limits[str(uid)] = {'date': today, 'registered_date': today}
        limits[str(uid)]['remaining'] = limit
        limits[str(uid)]['date'] = today
        save_limits(limits)
        
        bot.send_message(message.chat.id, escape_markdown_v2(f"✅ Установлено: {limit}"), parse_mode='MarkdownV2')
        try: bot.send_message(uid, escape_markdown_v2(f"🎉 Ваш лимит обновлен: {limit}"), parse_mode='MarkdownV2')
        except: pass
    except:
        bot.send_message(message.chat.id, escape_markdown_v2("❌ Ошибка формата."), parse_mode='MarkdownV2')

@bot.message_handler(func=lambda m: m.text == '📊 Статистика')
def stats_view(message):
    if message.from_user.id != ADMIN_USER_ID: return
    limits = load_limits()
    total = len(limits)
    today = str(date.today())
    active = sum(1 for v in limits.values() if v.get('date') == today and v['remaining'] < DAILY_LIMIT)
    bot.send_message(message.chat.id, escape_markdown_v2(f"📊 Статистика\nВсего: {total}\nАктивных сегодня: {active}"), parse_mode='MarkdownV2')

# ==========================================
# 8. AI HANDLER (САМЫЙ ВАЖНЫЙ)
# ==========================================

@bot.message_handler(func=lambda m: True)
def ai_reply(message):
    user_id = message.from_user.id
    has_limit, _ = check_and_update_limit(user_id)
    
    if not has_limit:
        text = "❌ Лимит на сегодня исчерпан!\nВыберите тариф, чтобы продолжить:"
        bot.send_message(message.chat.id, escape_markdown_v2(text), parse_mode='MarkdownV2', reply_markup=kb_tariffs())
        return

    bot.send_chat_action(message.chat.id, 'typing')
    
    try:
        response = gemini_client.models.generate_content(model=GEMINI_MODEL, contents=message.text)
        if response.text:
            # ЧИСТИМ ОТВЕТ AI ОТ ОШИБОК РАЗМЕТКИ
            clean_text = escape_markdown_v2(response.text)
            for chunk in split_text(clean_text):
                bot.send_message(message.chat.id, chunk, parse_mode='MarkdownV2')
        else:
            bot.reply_to(message, "Пустой ответ от AI.")
            check_and_update_limit(user_id, restore=True)
            
    except Exception as e:
        print(f"AI Error: {e}")
        bot.reply_to(message, "Ошибка AI. Попробуйте позже.")
        check_and_update_limit(user_id, restore=True)

# ==========================================
# 9. ЗАПУСК
# ==========================================

if __name__ == '__main__':
    init_db()
    print("🚀 Бот запущен...")
    bot.polling(none_stop=True)
