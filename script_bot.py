# -*- coding: utf-8 -*-
import asyncio
import os
import datetime
import json
import sys
from telethon import TelegramClient, events
from telethon.tl import functions, types
from telethon.errors import SessionPasswordNeededError, FloodWaitError
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters, ContextTypes, 
    ConversationHandler, CallbackQueryHandler
)

# --------------------------
# Configuration & Global Data
# --------------------------
SESSION_DIR = "session"
os.makedirs(SESSION_DIR, exist_ok=True)
STATE_FILE = "state.json"
PASSWORD_FILE = "passwords.json" 
# Время доступа в секундах (30 минут)
ACCESS_TIMEOUT_SECONDS = 30 * 60 

# API KEYS - Hardcoded to avoid console prompts
DEFAULT_API_ID = 20111454 
DEFAULT_API_HASH = "e0040834c399df8ac420058eee0af322" 
# ТОКЕН: ИСПОЛЬЗУЙТЕ СВОЙ АКТУАЛЬНЫЙ ТОКЕН
BOT_TOKEN = "8243967657:AAFkeKxRcgzRObKrSwF2_PGr3g83s4NHD3U" 

# АДМИН: ВВЕДИТЕ ВАШ TELEGRAM ID ДЛЯ ПОЛУЧЕНИЯ ПОЛНОГО ДОСТУПА БЕЗ ПАРОЛЯ
ADMIN_ID = 5934507030  # <--- ЗАМЕНИТЕ НА СВОЙ ID

# Data Structures
clients = {}    # {chat_id: {session_name: TelegramClient}} - СВЯЗКА ЧАТ_ID и СЕССИЙ (для отслеживания, кто добавил)
loaded_clients = {} # {session_name: TelegramClient} - ЗАГРУЖЕННЫЕ СЕССИИ (полный список)
state = {}      # {session_name: {"auto_reply": bool, "trigger": str, "reply": str, "auto_read": bool}}
meta = {}       # {session_name: {"started": datetime, "login_time": datetime, "me": user_obj}}
passwords = {}  # {session_name: "clean_password_string"} <-- ХРАНИТ ЧИСТЫЕ ПАРОЛИ
access_grants = {} # {chat_id: {session_name: datetime.datetime}} - Хранит время, до которого разрешен доступ

# State for ConversationHandler
(ADD_PHONE, ADD_CODE, ADD_2FA, SET_PASSWORD, SELECT_ACCOUNT, 
 CONFIRM_PASSWORD, ACTION_SELECT, INPUT, PASS_SELECT_CHANGE) = range(9)

# Новые состояния для изменения 2FA
(INPUT_OLD_2FA, INPUT_NEW_2FA, INPUT_HINT_2FA, INPUT_EMAIL_2FA) = range(9, 13) 

# --------------------------
# Load and Save State
# --------------------------
def load_state():
    """Loads state and passwords from JSON files."""
    global state, passwords
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                state.update(json.load(f))
        except Exception:
            pass
    if os.path.exists(PASSWORD_FILE):
        try:
            with open(PASSWORD_FILE, "r", encoding="utf-8") as f:
                passwords.update(json.load(f))
        except Exception:
            pass

def save_state():
    """Saves state and passwords to JSON files."""
    try:
        with open(PASSWORD_FILE, "w", encoding="utf-8") as f:
            # Сохраняем чистые пароли
            json.dump(passwords, f, ensure_ascii=False, indent=2)
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[ERROR] Failed to save state/passwords: {e}")

# --------------------------
# Utilities and Access Control
# --------------------------
def session_name_from_client(client: TelegramClient) -> str:
    """Extracts session name from a Telethon client."""
    try:
        if client.session and client.session.filename:
            return os.path.basename(client.session.filename).replace(".session", "")
    except:
        pass
    return str(id(client)) 

async def resolve_entity(client: TelegramClient, peer_str: str):
    """Resolves a chat/user string (username or ID) to a Telethon entity."""
    try:
        return await client.get_entity(peer_str)
    except Exception:
        try:
            return int(peer_str)
        except Exception:
            raise ValueError(f"Could not resolve entity for '{peer_str}'")

def get_client(chat_id: str, session_name: str) -> TelegramClient | None:
    """Safely retrieves a client linked to a specific chat_id."""
    return loaded_clients.get(session_name)

def grant_access(chat_id: str, session_name: str):
    """Grants 30 minutes access to a session for a given chat_id."""
    expires_at = datetime.datetime.now() + datetime.timedelta(seconds=ACCESS_TIMEOUT_SECONDS)
    access_grants.setdefault(chat_id, {})[session_name] = expires_at

def check_access_validity(chat_id: str, session_name: str) -> bool:
    """
    Checks if access is still valid OR if the user is the Admin.
    """
    if str(chat_id) == str(ADMIN_ID):
        return True # <-- АДМИН НЕ ТРЕБУЕТ ПАРОЛЯ

    grants = access_grants.get(chat_id, {})
    expires_at = grants.get(session_name)
    
    if expires_at and datetime.datetime.now() < expires_at:
        return True
        
    if session_name in grants:
        del grants[session_name]
    if not grants:
        access_grants.pop(chat_id, None)
        
    return False

# --------------------------
# Handlers: AutoReply + AutoRead
# --------------------------
def make_handlers_for(client: TelegramClient):
    """Creates event handlers for a specific Telethon client."""
    name = session_name_from_client(client)
    
    async def on_new_message(event):
        if not await client.is_user_authorized(): return
        st = state.get(name, {})
        
        # Auto-reply logic
        if st.get("auto_reply") and event.is_private and not event.out:
            try:
                trigger = (st.get("trigger") or "").lower()
                reply_text = st.get("reply") or ""
                text = (event.raw_text or "").lower()
                if trigger and trigger in text and reply_text:
                    await event.respond(reply_text) 
            except Exception: pass
        
        # Auto-read logic
        if st.get("auto_read") and event.is_private and not event.out:
            try:
                mid = getattr(event.message, "id", None)
                if mid is not None:
                    peer = event.input_chat 
                    await client(functions.messages.ReadHistoryRequest(peer=peer, max_id=mid))
            except Exception: pass
                
    return on_new_message

# --------------------------
# Bot Functions (Menus and Handlers)
# --------------------------

def get_main_menu_keyboard(chat_id: str) -> InlineKeyboardMarkup:
    """Generates the main menu keyboard, ensuring account buttons are always visible for all users."""
    
    is_admin = str(chat_id) == str(ADMIN_ID)
    
    keyboard = [
        [InlineKeyboardButton("➕ Добавить аккаунт", callback_data="menu_add_acc")],
    ]
    
    # 1. Управление аккаунтами - ВСЕГДА ВИДНЫ
    if is_admin:
        # Админ всегда видит управление ВСЕМИ аккаунтами
        keyboard.append([InlineKeyboardButton("⚙️ Управление ВСЕМИ аккаунтами (Admin)", callback_data="menu_select_acc")])
    else:
        # Обычный пользователь всегда видит управление СВОИМ аккаунтом
        keyboard.append([InlineKeyboardButton("⚙️ Управление аккаунтами", callback_data="menu_select_acc")])
    
    # 2. Список аккаунтов и смена пароля - ВСЕГДА ВИДНЫ
    keyboard.append([InlineKeyboardButton("📄 Мой список аккаунтов", callback_data="menu_list_acc")])
    keyboard.append([InlineKeyboardButton("🔑 Сменить пароль доступа", callback_data="menu_change_pwd")])

    return InlineKeyboardMarkup(keyboard)


def get_account_selection_keyboard(chat_id: str, prefix: str) -> InlineKeyboardMarkup | None:
    """Generates keyboard for account selection with a specific callback prefix. 
    
    ВАЖНО: Теперь эта функция всегда возвращает список всех загруженных аккаунтов, 
    если пользователь - не админ, он выбирает любой, но должен ввести пароль.
    """
    
    is_admin = str(chat_id) == str(ADMIN_ID)
    
    # ВСЕГДА показываем ВСЕ загруженные сессии.
    sessions_to_show = loaded_clients.keys()

    if not sessions_to_show:
        return None
        
    keyboard = []
    account_names = sorted(list(sessions_to_show))
    
    for i in range(0, len(account_names), 2):
        row = []
        for name in account_names[i:i+2]:
            me_info = meta.get(name, {}).get("me")
            uname = getattr(me_info, 'username', name)
            
            # Статус теперь показывает:
            # 👑: Админ
            # 🔓: Обычный пользователь, у которого есть текущий доступ по времени (для prefix="act")
            # 🔑: Аккаунт защищен локальным паролем (для prefix="act")
            # ⚠️: Аккаунт не защищен локальным паролем (НОВЫЙ СТАТУС, чтобы избежать ошибки)
            
            if is_admin:
                 status = "👑" 
            else:
                 is_protected = name in passwords
                 is_accessible = check_access_validity(chat_id, name)
                 
                 if is_accessible and prefix == "act":
                     status = "🔓"
                 elif is_protected:
                     status = "🔑"
                 else:
                     status = "⚠️" # Аккаунт не привязан к паролю. Доступ будет невозможен.
                 
            row.append(InlineKeyboardButton(f"{status} @{uname}", callback_data=f"{prefix}_{name}"))
        keyboard.append(row)
        
    keyboard.append([InlineKeyboardButton("⬅️ Назад в меню", callback_data="menu_main")])
    return InlineKeyboardMarkup(keyboard)


async def main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, message_text: str = None):
    """Sends the main menu, either as a new message or by editing the current one."""
    chat_id = str(update.effective_chat.id if update.effective_chat else context.user_data.get('chat_id'))
    if not chat_id: return

    is_admin = chat_id == str(ADMIN_ID)
    
    keyboard = get_main_menu_keyboard(chat_id)
    text = message_text or "👋 **Главное меню.** Выберите действие:"
    
    if is_admin:
        text = f"👑 **[ADMIN MODE]** Выберите действие для управления {len(loaded_clients)} аккаунтами."

    if update.callback_query:
        query = update.callback_query
        await query.answer()
        try:
            await query.edit_message_text(text=text, reply_markup=keyboard, parse_mode='Markdown')
        except Exception: 
             # В случае ошибки редактирования (например, если сообщение старое), отправляем новое
             await query.message.reply_text(text=text, reply_markup=keyboard, parse_mode='Markdown')
    elif update.message:
        await update.message.reply_text(text=text, reply_markup=keyboard, parse_mode='Markdown')

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /start command."""
    context.user_data['chat_id'] = str(update.effective_chat.id)
    await main_menu(update, context, "👋 **Добро пожаловать!** Используйте кнопки для управления аккаунтами.")

# --------------------------
# Menu Handlers (CallbackQueryHandler)
# --------------------------

async def handle_menu_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Routes main menu callback queries."""
    query = update.callback_query
    data = query.data
    await query.answer()
    
    if data == "menu_main":
        return await main_menu(update, context)

    if data == "menu_list_acc":
        return await list_all_accounts_for_all(update, context) # Скорректированная функция

    if data == "menu_add_acc":
        await query.edit_message_text("📲 **Шаг 1/4:** Введите номер телефона с международным префиксом (напр. `+15551234567`):")
        return ADD_PHONE
        
    if data == "menu_select_acc":
        chat_id = str(query.message.chat_id)
        keyboard = get_account_selection_keyboard(chat_id, prefix="act")
        
        is_admin = chat_id == str(ADMIN_ID)
        
        if not keyboard:
            await query.edit_message_text("❌ В боте нет загруженных аккаунтов для управления.")
            return await main_menu(update, context)

        text = "👑 **[ADMIN MODE]** Выберите аккаунт для управления:" if is_admin else "👉 **Выберите аккаунт** и введите пароль для доступа (🔓 = доступен сейчас):"
        
        await query.edit_message_text(text, reply_markup=keyboard)
        return SELECT_ACCOUNT
        
    if data == "menu_change_pwd":
        chat_id = str(query.message.chat_id)
        # Для смены пароля показываем только те аккаунты, которые привязаны к чату (или все, если админ)
        keyboard = get_account_selection_keyboard(chat_id, prefix="chg") 
        
        if not keyboard:
            await query.edit_message_text("❌ Нет аккаунтов, привязанных к этому чату, для смены пароля.")
            return await main_menu(update, context)

        await query.edit_message_text("🔐 Выберите аккаунт для **смены** локального пароля доступа:", reply_markup=keyboard)
        return PASS_SELECT_CHANGE
        
    return ConversationHandler.END

async def list_all_accounts_for_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lists all accounts loaded in the bot for ALL users."""
    query = update.callback_query
    chat_id = str(query.message.chat_id)
    is_admin = chat_id == str(ADMIN_ID)
    
    sessions_to_list = loaded_clients.keys()
    
    response = ["📄 **Список ВСЕХ загруженных аккаунтов в боте:**"]
    
    if not sessions_to_list:
        response = ["❌ В боте нет загруженных аккаунтов."]
    else:
        for name in sessions_to_list:
            me_info = meta.get(name, {}).get("me", "Unknown User")
            uname = getattr(me_info, 'username', 'N/A')
            
            if is_admin:
                access_status = "👑 ADMIN"
            else:
                 # Показываем, есть ли пароль.
                 access_status = "🔑 Пароль Требуется" if name in passwords else "⚠️ Нет пароля"
                 
            response.append(f"- **{name}** (@{uname}) | Статус: Активен | Доступ: {access_status}")
        
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад в меню", callback_data="menu_main")]])
    await query.edit_message_text(text="\n".join(response), reply_markup=keyboard, parse_mode='Markdown')

# --------------------------
# Add Account Conversation
# --------------------------
async def add_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    phone = update.message.text.strip()
    session_name = phone.replace("+", "").strip()
    session_path = os.path.join(SESSION_DIR, session_name)
    
    if session_name in loaded_clients:
        client = loaded_clients[session_name]
    else:
        client = TelegramClient(session_path, DEFAULT_API_ID, DEFAULT_API_HASH)
        try:
            await client.connect()
            loaded_clients[session_name] = client
        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка подключения: `{e}`")
            return await cancel_return_to_menu(update, context)
            
    context.user_data['client'] = client
    context.user_data['phone'] = phone
    context.user_data['session_name'] = session_name
    
    try:
        if not await client.is_user_authorized():
            await client.send_code_request(phone)
            await update.message.reply_text(f"🔢 **Шаг 2/4:** Введите код, отправленный на `{phone}`:")
            return ADD_CODE
        else:
            await update.message.reply_text(f"✅ Аккаунт `{session_name}` уже авторизован. Введите **локальный пароль доступа** (для управления из этого чата):")
            return SET_PASSWORD
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка при отправке номера: `{e}`")
        if client.is_connected(): await client.disconnect()
        if session_name in loaded_clients: del loaded_clients[session_name]
        return await cancel_return_to_menu(update, context)

async def add_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    code = update.message.text.strip()
    client = context.user_data['client']
    session_name = context.user_data['session_name']
    
    try:
        await client.sign_in(context.user_data['phone'], code)
        await update.message.reply_text("✅ **Шаг 3/4:** Аккаунт авторизован. Введите **локальный пароль доступа** для этого аккаунта:")
        return SET_PASSWORD
    except SessionPasswordNeededError:
        await update.message.reply_text("🔒 **Шаг 3/4:** Требуется пароль 2FA. Введите пароль Telegram 2FA:")
        return ADD_2FA
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка при входе по коду: `{e}`")
        if client.is_connected(): await client.disconnect()
        if session_name in loaded_clients: del loaded_clients[session_name]
        return await cancel_return_to_menu(update, context)

async def add_2fa(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pwd = update.message.text.strip()
    client = context.user_data['client']
    session_name = context.user_data['session_name']
    
    try:
        await client.sign_in(password=pwd)
        await update.message.reply_text("✅ **Шаг 4/4:** Аккаунт авторизован. Введите **локальный пароль доступа** для этого аккаунта:")
        return SET_PASSWORD
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка при входе по 2FA: `{e}`")
        if client.is_connected(): await client.disconnect()
        if session_name in loaded_clients: del loaded_clients[session_name]
        return await cancel_return_to_menu(update, context)

async def set_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sets the local access password and finalizes account addition/linkage."""
    password = update.message.text.strip()
    chat_id = str(update.effective_chat.id)
    session_name = context.user_data['session_name']
    client = context.user_data['client']
    
    try:
        is_change_pwd = context.user_data.get('is_change_pwd', False)
        
        # Привязываем к чату (это нужно для функции list_my_accounts, но теперь используется list_all_accounts_for_all)
        # Оставляем привязку, чтобы знать, кто добавил сессию
        clients.setdefault(chat_id, {})[session_name] = client 
        
        if not is_change_pwd and session_name not in state:
             state.setdefault(session_name, {"auto_reply": False, "trigger": "", "reply": "", "auto_read": False})
             me_obj = await client.get_me()
             meta[session_name] = {
                "started": datetime.datetime.now(),
                "login_time": datetime.datetime.now(), 
                "me": me_obj
             }
             client.add_event_handler(make_handlers_for(client), events.NewMessage)
        
        # Сохраняем ЧИСТЫЙ пароль
        passwords[session_name] = password 
        save_state()
        
        if is_change_pwd:
             text = f"🎉 **Успех!** Локальный пароль для аккаунта `{session_name}` **успешно изменен** на: `{password}`"
        else:
             text = f"🎉 **Успех!** Аккаунт `{session_name}` теперь привязан и защищен локальным паролем: `{password}`\n\nИспользуйте меню для управления."

        await update.message.reply_text(text)
        return await cancel_return_to_menu(update, context)
        
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка во время финальной настройки/смены пароля: `{e}`")
        if not is_change_pwd:
            if client.is_connected(): await client.disconnect()
            if session_name in loaded_clients: del loaded_clients[session_name]
        return await cancel_return_to_menu(update, context)

# --------------------------
# Password Management Conversation
# --------------------------

async def pass_select_change(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the selection of the account to change the password for (via CallbackQuery)."""
    query = update.callback_query
    await query.answer()
    
    data_parts = query.data.split('_', 1)
    if len(data_parts) != 2:
        await query.edit_message_text("❌ Ошибка при выборе аккаунта.")
        return await cancel_return_to_menu(update, context)

    session_name = data_parts[1]
    
    context.user_data['session_name'] = session_name
    context.user_data['client'] = get_client(str(query.message.chat_id), session_name) 
    context.user_data['is_change_pwd'] = True 
    
    await query.edit_message_text(f"✨ Введите **новый пароль доступа** для `{session_name}`:")
    return SET_PASSWORD 

# --------------------------
# Select Account and Actions Conversation
# --------------------------

async def account_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the account selection, checks timeout/admin status, and prompts for password if needed."""
    query = update.callback_query
    await query.answer()
    
    chat_id = str(query.message.chat_id)
    is_admin = chat_id == str(ADMIN_ID)
    
    data_parts = query.data.split('_', 1)
    if len(data_parts) != 2:
        await query.edit_message_text("❌ Ошибка при выборе аккаунта.")
        return await cancel_return_to_menu(update, context)

    session_name = data_parts[1]
    context.user_data['session_name'] = session_name
    
    client = get_client(chat_id, session_name)
    
    if not client:
        await query.edit_message_text("❌ Аккаунт не загружен в память бота. Попробуйте перезапустить бота.")
        return await cancel_return_to_menu(update, context)

    context.user_data['client'] = client
    
    # 1. Администратор: доступ без пароля
    if is_admin:
        grant_access(chat_id, session_name) # На всякий случай обновляем доступ
        status_text = f"👑 **[ADMIN MODE]** Доступ подтвержден для `{session_name}`."
        keyboard = get_action_keyboard()
        await query.edit_message_text(
            f"{status_text}\nВыберите действие:", 
            reply_markup=keyboard,
            parse_mode='Markdown'
        )
        return ACTION_SELECT

    # 2. Обычный пользователь: проверка доступа по времени
    if check_access_validity(chat_id, session_name):
        expires_at = access_grants[chat_id][session_name]
        remaining = expires_at - datetime.datetime.now()
        status_text = (f"🔓 **Доступ подтвержден для** `{session_name}`.\n"
                     f"Осталось времени: **{int(remaining.total_seconds() // 60)} минут**.")
        
        keyboard = get_action_keyboard()
        await query.edit_message_text(
            f"{status_text}\nВыберите действие:", 
            reply_markup=keyboard,
            parse_mode='Markdown'
        )
        return ACTION_SELECT
        
    # 3. Обычный пользователь: требуется пароль
    if session_name not in passwords:
         # Аккаунт не имеет локального пароля - доступ для обычных пользователей невозможен.
         await query.edit_message_text(f"⚠️ Аккаунт `{session_name}` не защищен локальным паролем. Доступ только для Администратора.")
         return await cancel_return_to_menu(update, context)

    # Запрашиваем локальный пароль
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад в меню", callback_data="menu_main")]])
    await query.edit_message_text(f"🔑 Доступ истек. Введите **локальный пароль доступа** для аккаунта `{session_name}`:", reply_markup=keyboard)
    return CONFIRM_PASSWORD

async def confirm_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Authenticates the user, grants access, and shows action menu."""
    password = update.message.text.strip()
    session_name = context.user_data['session_name']
    chat_id = str(update.effective_chat.id)
    
    expected_pwd = passwords.get(session_name)
    
    if expected_pwd != password:
        await update.message.reply_text("❌ **Неверный пароль.** Операция отменена. Возврат в главное меню.")
        return await cancel_return_to_menu(update, context, clear_user_data=True)

    client = context.user_data['client']
    
    if not client.is_connected():
        try:
            await client.connect()
            if not await client.is_user_authorized():
                 await update.message.reply_text("❌ Клиент не авторизован. Пожалуйста, переподключите аккаунт.")
                 return await cancel_return_to_menu(update, context, clear_user_data=True)
        except Exception as e:
            await update.message.reply_text(f"❌ Не удалось переподключить клиент: `{e}`.")
            return await cancel_return_to_menu(update, context, clear_user_data=True)

    grant_access(chat_id, session_name)
    
    keyboard = get_action_keyboard()
    await update.message.reply_text(
        f"✅ **Пароль подтвержден для** `{session_name}`. Доступ выдан на 30 минут.\n"
        f"Выберите действие:", 
        reply_markup=keyboard
    )
    return ACTION_SELECT

def get_action_keyboard() -> InlineKeyboardMarkup:
    """Generates the main action keyboard for a selected account, including new security functions."""
    actions = [
        ("✉️ Отправить сообщение", "action_send_msg"), 
        ("📝 Показать 50 чатов", "action_show_chats"),
        ("👁️ Прочитать последние", "action_read_last"),
        ("👤 Список контактов", "action_show_contacts"),
        ("👥 Список групп/каналов", "action_show_groups"),
        
        ("🔑 Изменить лок. пароль", "action_change_local_pwd"),
        ("🔑 Показать лок. пароль", "action_show_local_pwd"),
        ("🔒 Статус и Подсказка 2FA", "action_show_2fa_status"), 
        ("🔒 Изменить 2FA (Telegram)", "action_change_2fa"),
        
        ("🤖 Вкл. Авто-ответ", "action_auto_reply_on"),
        ("🤖 Выкл. Авто-ответ", "action_auto_reply_off"),
        ("👀 Вкл. Авто-прочтение", "action_auto_read_on"),
        ("👀 Выкл. Авто-прочтение", "action_auto_read_off"),
        
        ("🗑️ Очистить историю", "action_clear_history"),
        ("⛔ Удалить сообщение", "action_delete_message"),
        ("📢 Массовая рассылка", "action_mass_broadcast"),
        ("⏰ Отложенное сообщение", "action_scheduled_message"),
        ("👍 Отправить реакцию", "action_send_reaction"),
        
        ("📸 Сменить фото", "action_change_photo"),
        ("✏️ Сменить имя", "action_change_name"),
        ("ℹ️ Инфо об аккаунте", "action_session_info"),
        ("📊 Статистика (сегодня)", "action_account_stats"),
        
        ("🚪 Выход (текущее устр.)", "action_logout_current"),
        ("💥 Выход (все устр.)", "action_logout_all"),
        ("🔥 Удалить сессию (файл)", "action_delete_session"),
        ("🛑 Отключить клиент (Admin)", "action_disconnect_client")
    ]

    keyboard = []
    row_size = 2 if len(actions) % 3 != 0 or len(actions) <= 12 else 3 
    
    for i in range(0, len(actions), row_size):
        row = []
        for j in range(row_size):
            if i + j < len(actions):
                row.append(InlineKeyboardButton(actions[i+j][0], callback_data=actions[i+j][1]))
        if row:
            keyboard.append(row)
        
    keyboard.append([InlineKeyboardButton("⬅️ Назад в Главное меню", callback_data="menu_main")])
    return InlineKeyboardMarkup(keyboard)


async def handle_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Maps the selected callback query to an action function and prompts for input."""
    query = update.callback_query
    data = query.data
    await query.answer()
    
    client = context.user_data['client']
    chat_id = str(query.message.chat_id)
    session_name = context.user_data['session_name']

    if not check_access_validity(chat_id, session_name):
        await query.edit_message_text(
            f"❌ **Доступ истек!** Для продолжения работы с `{session_name}` требуется повторный ввод пароля."
        )
        return SELECT_ACCOUNT 
    
    actions_map = {
        "action_send_msg": (send_message, ["Введите username или ID получателя:", "Введите текст сообщения:"]),
        "action_show_chats": (show_chats, []),
        "action_read_last": (read_last_messages, ["Введите ID или username чата:", "Сколько сообщений показать (по умолчанию 10):"]),
        "action_show_contacts": (show_contacts, []),
        "action_show_groups": (show_groups, []),
        
        "action_change_local_pwd": (change_local_password_start, ["Введите **новый** локальный пароль доступа:"]), 
        "action_show_local_pwd": (show_local_password, []),                                                        
        "action_show_2fa_status": (show_2fa_status, []), 
        "action_change_2fa": (change_2fa_start_conv, []),                                                               
        
        "action_auto_reply_on": (auto_reply_enable, ["Введите текст-триггер:", "Введите текст авто-ответа:"]),
        "action_auto_reply_off": (auto_reply_disable, []),
        "action_auto_read_on": (auto_read_enable, []),
        "action_auto_read_off": (auto_read_disable, []),
        "action_change_photo": (change_profile_photo, ["Введите путь к новому фото (доступный боту):"]),
        "action_change_name": (change_name, ["Введите имя:", "Введите фамилию (опционально):"]),
        "action_session_info": (session_info, []),
        "action_account_stats": (account_stats, []),
        "action_clear_history": (clear_history, ["Введите ID или username чата:"]),
        "action_delete_message": (delete_message, ["Введите ID или username чата:", "Введите ID сообщения:"]),
        "action_mass_broadcast": (mass_broadcast, ["Введите текст рассылки:"]),
        "action_scheduled_message": (scheduled_message, ["Введите username/ID получателя:", "Введите текст сообщения:", "Введите задержку в секундах:"]),
        "action_send_reaction": (send_reaction, ["Введите ID или username чата:", "Введите ID сообщения:", "Введите эмодзи реакции (напр. 👍):"]),
        
        "action_logout_current": (logout_current, ["**Подтвердите** выход из текущей сессии (y/n):"]),
        "action_logout_all": (logout_all_devices, ["**Подтвердите** выход из ВСЕХ сессий (y/n):"]),
        "action_delete_session": (delete_session, ["**Подтвердите** удаление файла сессии и отключение (y/n):"]),
        "action_disconnect_client": (disconnect_client, ["**Подтвердите** отключение клиента от сети (y/n):"]),
    }
    
    if data not in actions_map:
        await query.edit_message_text("❌ Неверный выбор. Пожалуйста, выберите действие из списка.")
        return ACTION_SELECT
        
    action_func, inputs = actions_map[data]
    context.user_data['action'] = action_func
    context.user_data['inputs'] = inputs
    context.user_data['current_input'] = 0
    context.user_data['input_values'] = [] 
    
    if data == "action_change_2fa":
         return await change_2fa_start_conv(update, context)

    try:
        await query.edit_message_text(f"Выбрано: **{action_func.__name__.replace('_', ' ').title()}**.")
    except: pass
    
    if not inputs:
        try:
            result = await action_func(client, update, context)
            await query.message.reply_text(result or "✅ **Действие выполнено успешно.**")
        except Exception as e:
            await query.message.reply_text(f"❌ **Ошибка:** `{type(e).__name__}: {e}`")
        
        keyboard = get_action_keyboard()
        await query.message.reply_text("↩️ **Выберите следующее действие:**", reply_markup=keyboard)
        return ACTION_SELECT 
        
    await query.message.reply_text(f"📝 **Ввод 1/{len(inputs)}:** {inputs[0]}")
    return INPUT

async def handle_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Collects inputs for actions and executes the function when all are collected."""
    context.user_data['input_values'].append(update.message.text.strip())
    context.user_data['current_input'] += 1
    
    current_input_index = context.user_data['current_input']
    total_inputs = len(context.user_data['inputs'])
    
    if current_input_index < total_inputs:
        next_input_prompt = context.user_data['inputs'][current_input_index]
        await update.message.reply_text(f"📝 **Ввод {current_input_index + 1}/{total_inputs}:** {next_input_prompt}")
        return INPUT
    
    try:
        result = await context.user_data['action'](context.user_data['client'], update, context)
        await update.message.reply_text(result or "✅ **Действие выполнено успешно.**")
    except Exception as e:
        await update.message.reply_text(f"❌ **Ошибка:** `{type(e).__name__}: {e}`")
        
    for key in ['action', 'inputs', 'input_values', 'current_input']:
        context.user_data.pop(key, None)
        
    keyboard = get_action_keyboard()
    await update.message.reply_text("↩️ **Выберите следующее действие:**", reply_markup=keyboard)
    return ACTION_SELECT

async def cancel_return_to_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, clear_user_data: bool = False):
    """Generic fallback/cancel command that returns to main menu."""
    if clear_user_data:
        for key in ['phone', 'client', 'session_name', 'action', 'inputs', 'input_values', 'is_change_pwd', 'auth_password', 'new_password', 'password_hint', 'auth_2fa_data']:
            context.user_data.pop(key, None)

    await asyncio.sleep(0.5) 
    
    if update.callback_query:
        try:
            await main_menu(update, context, "↩️ **Операция отменена/завершена. Возврат в Главное меню.**")
        except Exception:
            if update.callback_query.message:
                 await update.callback_query.message.reply_text("↩️ **Операция отменена/завершена. Возврат в Главное меню.**", reply_markup=get_main_menu_keyboard(str(update.effective_chat.id)))
    elif update.message:
        await main_menu(update, context, "↩️ **Операция отменена/завершена. Возврат в Главное меню.**")
        
    return ConversationHandler.END


# --------------------------
# Action Implementations
# --------------------------
def human_delta(dt: datetime.datetime) -> str:
    """Calculates and formats time difference in a human-readable way."""
    if dt is None: return "unknown"
    delta = datetime.datetime.now() - dt
    secs = int(delta.total_seconds())
    if secs < 60: return f"{secs}s"
    mins = secs // 60
    if mins < 60: return f"{mins}m{secs%60}s"
    hrs = mins // 60
    if hrs < 24: return f"{hrs}h{mins%60}m"
    days = hrs // 24
    return f"{days}d{hrs%24}h"

async def send_message(client, update, context):
    target, text = context.user_data['input_values']
    await client.send_message(await resolve_entity(client, target), text) 
    return "✅ Сообщение отправлено! (Длинные сообщения будут разбиты и отправлены автоматически)"

async def show_chats(client, update, context):
    result = []
    async for d in client.iter_dialogs(limit=50):
        kind = "User" if d.is_user else ("Channel" if d.is_channel else "Group")
        uname = getattr(d.entity, "username", "N/A")
        result.append(f"- **{d.name}** | Type={kind} | ID={d.id} | Username=**@{uname}** | Unread={d.unread_count}")
    return "📝 **Диалоги (Первые 50):**\n" + "\n".join(result) or "Диалоги не найдены"

async def read_last_messages(client, update, context):
    chat, lim = context.user_data['input_values']
    ent = await resolve_entity(client, chat)
    limit = int(lim) if lim and lim.isdigit() else 10
    msgs = await client.get_messages(ent, limit=limit)
    result = []
    for m in msgs:
        text = (m.message or "<Медиа/Служебное сообщение>").replace("\n", " ").strip()[:50]
        sender_id = m.sender_id
        is_out = "OUT" if m.out else "IN"
        result.append(f"[{m.id}] **{is_out}** from={sender_id} | {text}")
    return f"📜 **Последние {limit} сообщений в {chat}:**\n" + "\n".join(result) or "Сообщения не найдены"

async def show_contacts(client, update, context):
    result = await client(functions.contacts.GetContactsRequest(hash=0))
    contacts = result.users
    result_list = []
    for c in contacts:
        uname = getattr(c,'username','N/A')
        result_list.append(f"- **{c.first_name or ''} {c.last_name or ''}** | ID={c.id} | Username=**@{uname}**")
        
    return "👥 **Контакты:**\n" + "\n".join(result_list) or "Контакты не найдены"

async def show_groups(client, update, context):
    result = []
    async for d in client.iter_dialogs(limit=200):
        if d.is_group or d.is_channel:
            ent = d.entity
            kind = "Channel" if d.is_channel else "Group"
            uname = getattr(ent,'username','N/A')
            result.append(f"- **{d.name}** | Type={kind} | ID={d.id} | Username=**@{uname}**")
    return "🏛️ **Группы и Каналы:**\n" + "\n".join(result) or "Группы или каналы не найдены"

async def auto_reply_enable(client, update, context):
    name = session_name_from_client(client)
    trigger, reply = context.user_data['input_values']
    state.setdefault(name, {})["auto_reply"] = True
    state[name]["trigger"] = trigger
    state[name]["reply"] = reply
    save_state()
    return f"🤖 Авто-ответ **ВКЛЮЧЕН** для `{name}`.\nТриггер: `{trigger}`\nОтвет: `{reply}`"

async def auto_reply_disable(client, update, context):
    name = session_name_from_client(client)
    state.setdefault(name, {})["auto_reply"] = False
    save_state()
    return f"🤖 Авто-ответ **ОТКЛЮЧЕН** для `{name}`."

async def auto_read_enable(client, update, context):
    name = session_name_from_client(client)
    state.setdefault(name, {})["auto_read"] = True
    save_state()
    return f"👀 Авто-прочтение **ВКЛЮЧЕНО** для `{name}`."

async def auto_read_disable(client, update, context):
    name = session_name_from_client(client)
    state.setdefault(name, {})["auto_read"] = False
    save_state()
    return f"👀 Авто-прочтение **ОТКЛЮЧЕНО** для `{name}`."

async def change_local_password_start(client, update, context):
    """Handles the actual change of the local password (saving clean password)."""
    new_password = context.user_data['input_values'][0]
    session_name = context.user_data['session_name']
    
    passwords[session_name] = new_password
    save_state()
    
    return f"🔑 Локальный пароль доступа для `{session_name}` **успешно изменен** на: `{new_password}`."

async def show_local_password(client, update, context):
    """Shows the clean local password."""
    session_name = context.user_data['session_name']
    clean_pwd = passwords.get(session_name, "N/A (Пароль не установлен)")
    
    return (f"🔑 **Текущий локальный пароль доступа** для `{session_name}`:\n"
            f"Пароль: `{clean_pwd}`\n\n"
            f"⚠️ **Внимание:** Пароль хранится в виде чистого текста в `passwords.json`.")


async def show_2fa_status(client, update, context): 
    """Retrieves and displays Telegram 2FA (Cloud Password) status."""
    auth_pw = await client(functions.account.GetPasswordRequest())
    
    if auth_pw.has_recovery and auth_pw.email_unconfirmed_pattern is None:
        email_status = "✅ Есть (Подтвержден)"
    elif auth_pw.has_recovery and auth_pw.email_unconfirmed_pattern:
        email_status = f"⚠️ Есть, но не подтвержден (Начало: `{auth_pw.email_unconfirmed_pattern}`)"
    else:
        email_status = "❌ Нет"

    status = "✅ Установлен" if auth_pw.has_password else "❌ Не установлен"
    hint = f"`{auth_pw.hint}`" if auth_pw.hint else "Нет"
    
    return (f"🔒 **Статус Telegram 2FA (Облачный Пароль)**\n"
            f"⚠️ **ВНИМАНИЕ:** Технически невозможно извлечь облачный пароль Telegram в чистом виде.\n\n"
            f"Статус пароля: **{status}**\n"
            f"Подсказка: {hint}\n"
            f"Почта для восстановления: {email_status}")


# --- 2FA Change Conversation Functions ---

async def change_2fa_start_conv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Starts the 2FA change/set process."""
    client = context.user_data['client']
    auth_pw = await client(functions.account.GetPasswordRequest())
    context.user_data['auth_2fa_data'] = auth_pw
    
    if update.callback_query:
        msg_editor = update.callback_query.edit_message_text
    elif update.message:
        msg_editor = update.message.reply_text
    else:
        return ACTION_SELECT # Fallback

    if auth_pw.has_password:
        await msg_editor("🔑 **Шаг 1/4:** Введите **текущий** пароль 2FA Telegram:")
        return INPUT_OLD_2FA
    else:
        await msg_editor("✨ **Шаг 1/4:** Пароль 2FA не установлен. Введите **новый** пароль 2FA, который вы хотите установить:")
        return INPUT_NEW_2FA

async def input_old_2fa(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receives and checks the current 2FA password."""
    old_password = update.message.text.strip()
    client = context.user_data['client']
    
    try:
        # Проверяем пароль
        await client(functions.auth.CheckPasswordRequest(password=old_password))
        context.user_data['auth_password'] = old_password
        
        await update.message.reply_text("✅ **Пароль подтвержден.**\n\n✨ **Шаг 2/4:** Введите **новый** пароль 2FA (или тот же, если хотите изменить только почту/подсказку):")
        return INPUT_NEW_2FA
        
    except Exception as e:
        await update.message.reply_text(f"❌ **Ошибка:** Неверный текущий пароль 2FA: `{e}`. Операция отменена.")
        return await cancel_return_to_menu(update, context, clear_user_data=True)

async def input_new_2fa(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receives the new 2FA password."""
    new_password = update.message.text.strip()
    context.user_data['new_password'] = new_password
    
    await update.message.reply_text("📝 **Шаг 3/4:** Введите **подсказку** для нового пароля (или '-' для пропуска):")
    return INPUT_HINT_2FA

async def input_hint_2fa(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receives the password hint."""
    hint = update.message.text.strip()
    context.user_data['password_hint'] = hint if hint != '-' else None

    await update.message.reply_text("📧 **Шаг 4/4:** Введите **почту для восстановления** (или '-' для пропуска):")
    return INPUT_EMAIL_2FA

async def input_email_2fa(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receives the recovery email and finalizes the change/set operation."""
    recovery_email = update.message.text.strip()
    client = context.user_data['client']
    auth_pw = context.user_data.get('auth_2fa_data')
    
    result_text = None
    
    try:
        old_password = context.user_data.get('auth_password')
        new_password = context.user_data['new_password']
        hint = context.user_data['password_hint']
        email = recovery_email if recovery_email != '-' else None
        
        # Если почта не установлена или нет подтверждения
        if email and (not auth_pw or not auth_pw.has_recovery):
            # Установка или изменение пароля + отправка кода
            if old_password:
                await client(functions.account.UpdatePasswordSettingsRequest(
                    current_password=old_password,
                    new_password=new_password,
                    hint=hint,
                    email=email
                ))
            else:
                 await client(functions.account.SetPasswordRequest(
                    new_password=new_password,
                    hint=hint,
                    email=email,
                    no_spaces=True
                ))

            await update.message.reply_text("📧 ✅ **Пароль изменен/установлен.** На ваш email отправлен код подтверждения. Введите этот код для подтверждения почты:")
            return INPUT_EMAIL_2FA # Снова ждем ввод почты, но теперь как код
        
        elif email and 'EMAIL_UNCONFIRMED' in str(auth_pw.email_unconfirmed_pattern) and recovery_email != '-':
             # Пользователь вводит код подтверждения
             email_code = recovery_email
             await client(functions.account.ConfirmPasswordEmailRequest(code=email_code))
             result_text = "🎉 **Успех!** Почта для восстановления 2FA **успешно подтверждена**."
        
        else:
             # Изменение пароля/подсказки без изменения/установки почты
             if old_password:
                await client(functions.account.UpdatePasswordSettingsRequest(
                    current_password=old_password,
                    new_password=new_password,
                    hint=hint,
                    email=email
                ))
             else:
                  await client(functions.account.SetPasswordRequest(
                    new_password=new_password,
                    hint=hint,
                    email=email,
                    no_spaces=True
                ))
             result_text = "🎉 **Успех!** Пароль 2FA Telegram **успешно изменен/установлен** (без подтверждения почты)."
            
    except FloodWaitError as fw:
        await update.message.reply_text(f"❌ **Ошибка:** Превышен лимит запросов. Попробуйте через {fw.seconds} сек.")
        return await cancel_return_to_menu(update, context, clear_user_data=True)
    except Exception as e:
        await update.message.reply_text(f"❌ **Ошибка 2FA:** `{type(e).__name__}: {e}`. Операция отменена.")
        return await cancel_return_to_menu(update, context, clear_user_data=True)
            
    await update.message.reply_text(result_text)
    return await cancel_return_to_menu(update, context, clear_user_data=True)

# --- End 2FA Change Conversation Functions ---

async def change_profile_photo(client, update, context):
    path = context.user_data['input_values'][0]
    if not os.path.exists(path):
        return f"❌ Ошибка: Файл не найден по пути: `{path}`"
    
    file = await client.upload_file(path)
    await client(functions.photos.UploadProfilePhotoRequest(file=file))
    return "✅ Фото профиля изменено."

async def change_name(client, update, context):
    first, last = context.user_data['input_values']
    await client(functions.account.UpdateProfileRequest(first_name=first or None, last_name=(last or None)))
    return f"✅ Имя изменено на: **{first or ''} {last or ''}**"

async def session_info(client, update, context):
    name = session_name_from_client(client)
    started = meta.get(name, {}).get("started")
    login_time = meta.get(name, {}).get("login_time")
    
    me = await client.get_me() 
    meta[name]["me"] = me 
    
    return (f"ℹ️ **Информация об аккаунте** `{name}`\n"
            f"ID: `{me.id}`\n"
            f"Username: `@{getattr(me,'username','N/A')}`\n"
            f"Имя: **{getattr(me,'first_name','')} {getattr(me,'last_name','')}**\n"
            f"Бот запущен: {started.strftime('%Y-%m-%d %H:%M:%S') if started else 'N/A'} (Uptime: **{human_delta(started)}**)\n"
            f"Время входа: {login_time.strftime('%Y-%m-%d %H:%M:%S') if login_time else 'N/A'} (Со времени входа: **{human_delta(login_time)}**)")

async def clear_history(client, update, context):
    chat = context.user_data['input_values'][0]
    ent = await resolve_entity(client, chat)
    await client(functions.messages.DeleteHistoryRequest(peer=ent, max_id=0, revoke=True, just_clear=False))
    return f"⚠️ **Вся история очищена для** `{chat}`. (Это навсегда)"

async def delete_message(client, update, context):
    chat, mid = context.user_data['input_values']
    try:
        mid_int = int(mid)
    except ValueError:
        return "❌ ID сообщения должно быть целым числом."
        
    await client.delete_messages(await resolve_entity(client, chat), [mid_int], revoke=True)
    return f"✅ Сообщение ID `{mid}` удалено в `{chat}`."

async def mass_broadcast(client, update, context):
    text = context.user_data['input_values'][0]
    sent_count = 0
    errors = 0
    result = ["📢 **Начало рассылки...**"]
    
    async for d in client.iter_dialogs(limit=500):
        if d.is_user and not d.entity.bot and not d.is_channel:
            try:
                await client.send_message(d.id, text)
                sent_count += 1
                await asyncio.sleep(0.5) 
            except FloodWaitError as fw:
                await asyncio.sleep(fw.seconds)
            except Exception:
                errors += 1
                
    result.append(f"**[ГОТОВО] Рассылка завершена.**")
    result.append(f"Отправлено успешно: **{sent_count} чатам**.")
    result.append(f"Не удалось отправить: **{errors} чатам**.")
    return "\n".join(result)

async def account_stats(client, update, context):
    today = datetime.date.today()
    sent_today = 0
    recv_today = 0
    
    async for d in client.iter_dialogs(limit=20):
        msgs = await client.get_messages(d.id, limit=50) 
        for m in msgs:
            if getattr(m, "date", None) and m.date.date() == today:
                if getattr(m, "out", False): sent_today += 1
                else: recv_today += 1
                
    return (f"📊 **Статистика аккаунта (Сегодня)**\n"
            f"Отправлено сообщений: **{sent_today}**\n"
            f"Получено сообщений: **{recv_today}**")

async def scheduled_message(client, update, context):
    user, text, delay_str = context.user_data['input_values']
    
    try:
        delay = int(delay_str)
        if delay < 1:
            return "❌ Задержка должна быть положительной."
    except ValueError:
        return "❌ Задержка должна быть целым числом в секундах."

    await update.message.reply_text(f"⏳ Сообщение запланировано для `{user}` через **{delay} секунд**.")
    
    async def sender_task():
        await asyncio.sleep(delay)
        try:
            await client.send_message(await resolve_entity(client, user), text)
            await update.message.reply_text(f"✅ Отложенное сообщение отправлено для `{user}`.")
        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка при отправке отложенного сообщения для `{user}`: `{type(e).__name__}: {e}`")

    asyncio.create_task(sender_task())
    return "✅ Задача по отправке отложенного сообщения создана."

async def send_reaction(client, update, context):
    chat, mid_str, emoji = context.user_data['input_values']
    
    try:
        mid = int(mid_str)
        peer = await resolve_entity(client, chat)
    except ValueError:
        return "❌ ID сообщения должно быть целым числом."

    if len(emoji) > 5:
        return "❌ Кажется, это не эмодзи. Попробуйте один символ."

    await client(functions.messages.SendReactionRequest(
        peer=await client.get_input_entity(peer),
        msg_id=mid,
        reaction=types.ReactionEmoji(emoticon=emoji)
    ))
    return f"✅ Реакция **{emoji}** отправлена на сообщение ID `{mid}` в `{chat}`."

async def logout_current(client, update, context):
    name = session_name_from_client(client)
    confirm = context.user_data['input_values'][0].lower()
    if confirm == "y":
        try:
            await client.log_out()
            for chat_id, clients_dict in clients.items():
                if name in clients_dict: del clients_dict[name]
            return f"👋 **Выход выполнен** из текущей сессии для `{name}`."
        except Exception as e:
            return f"❌ Ошибка во время выхода: `{e}`"
    return "🚫 Выход отменен."

async def logout_all_devices(client, update, context):
    name = session_name_from_client(client)
    confirm = context.user_data['input_values'][0].lower()
    if confirm == "y":
        try:
            await client(functions.auth.ResetAuthorizationsRequest())
            await client.disconnect() 
            for chat_id, clients_dict in clients.items():
                if name in clients_dict: del clients_dict[name]
            return f"⚠️ **Выход выполнен со ВСЕХ устройств** для `{name}`. Требуется повторная авторизация."
        except Exception as e:
            return f"❌ Ошибка во время массового выхода: `{e}`"
    return "🚫 Выход отменен."
    
async def disconnect_client(client, update, context):
    name = session_name_from_client(client)
    confirm = context.user_data['input_values'][0].lower()
    if confirm != "y":
        return "🚫 Отключение клиента от сети отменено."
        
    try:
        if client.is_connected():
            await client.disconnect()
            return f"🛑 **Клиент** `{name}` **отключен от сети** (файл сессии сохранен)."
        else:
            return f"✅ Клиент `{name}` уже был отключен."
    except Exception as e:
        return f"❌ Ошибка при отключении клиента: `{e}`"

async def delete_session(client, update, context):
    name = session_name_from_client(client)
    confirm = context.user_data['input_values'][0].lower()
    if confirm != "y":
        return "🚫 Удаление сессии отменено."
        
    session_path = client.session.filename
    
    try: await client.log_out()
    except Exception: pass
        
    for chat_id_key, clients_dict in clients.items():
        if name in clients_dict: del clients_dict[name]
    if name in loaded_clients: del loaded_clients[name]

    if os.path.exists(session_path): os.remove(session_path)
        
    if name in state: del state[name]
    if name in meta: del meta[name]
    if name in passwords: del passwords[name]

    save_state()
    
    return f"🗑️ **Сессия** `{name}` **удалена** (файл удален, пароль отвязан). Клиент отключен."

# --------------------------
# Load All Accounts
# --------------------------
async def load_all_accounts():
    """Loads all session files and checks authorization status."""
    load_state()
    session_files = [f for f in os.listdir(SESSION_DIR) if f.endswith(".session")]
    awaitables = []
    
    for fname in session_files:
        async def process_session(fname):
            session_path = os.path.join(SESSION_DIR, fname)
            session_name = fname.replace(".session", "")
            
            if session_name in loaded_clients: return
            client = TelegramClient(session_path, DEFAULT_API_ID, DEFAULT_API_HASH)
            try:
                await client.start()
                if not await client.is_user_authorized():
                    await client.disconnect()
                    return
                
                me_obj = await client.get_me()
                meta[session_name] = {
                    "started": datetime.datetime.now(), 
                    "login_time": datetime.datetime.now(), 
                    "me": me_obj
                }
                client.add_event_handler(make_handlers_for(client), events.NewMessage)
                loaded_clients[session_name] = client
            except Exception:
                try:
                    if client.is_connected(): await client.disconnect()
                except: pass
        
        awaitables.append(process_session(fname))

    if awaitables:
        await asyncio.gather(*awaitables)


# --------------------------
# Main and Handlers Registration
# --------------------------

async def main():
    """Initializes and runs the bot."""
    
    # 1. Загрузка аккаунтов (работает в фоне)
    await load_all_accounts()
    
    # 2. Инициализация и запуск Telegram Bot API
    app = Application.builder().token(BOT_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(handle_menu_callbacks, pattern=r'^menu_main|menu_list_acc$'))

    add_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(handle_menu_callbacks, pattern=r'^menu_add_acc$')],
        states={
            ADD_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_phone)],
            ADD_CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_code)],
            ADD_2FA: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_2fa)],
            SET_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_password)],
        },
        fallbacks=[CommandHandler("cancel", cancel_return_to_menu), CallbackQueryHandler(cancel_return_to_menu, pattern=r'^menu_main$')],
        allow_reentry=True
    )
    
    action_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(handle_menu_callbacks, pattern=r'^menu_select_acc$')],
        states={
            SELECT_ACCOUNT: [CallbackQueryHandler(account_selected, pattern=r'^act_')],
            CONFIRM_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, confirm_password)],
            ACTION_SELECT: [CallbackQueryHandler(handle_action, pattern=r'^action_')],
            INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_input)],
            INPUT_OLD_2FA: [MessageHandler(filters.TEXT & ~filters.COMMAND, input_old_2fa)],
            INPUT_NEW_2FA: [MessageHandler(filters.TEXT & ~filters.COMMAND, input_new_2fa)],
            INPUT_HINT_2FA: [MessageHandler(filters.TEXT & ~filters.COMMAND, input_hint_2fa)],
            INPUT_EMAIL_2FA: [MessageHandler(filters.TEXT & ~filters.COMMAND, input_email_2fa)],
        },
        fallbacks=[CommandHandler("cancel", cancel_return_to_menu), CallbackQueryHandler(cancel_return_to_menu, pattern=r'^menu_main$')],
        allow_reentry=True
    )
    
    change_pass_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(handle_menu_callbacks, pattern=r'^menu_change_pwd$')],
        states={
            PASS_SELECT_CHANGE: [CallbackQueryHandler(pass_select_change, pattern=r'^chg_')],
            SET_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_password)] 
        },
        fallbacks=[CommandHandler("cancel", cancel_return_to_menu), CallbackQueryHandler(cancel_return_to_menu, pattern=r'^menu_main$')],
        allow_reentry=True
    )
    
    app.add_handler(add_conv)
    app.add_handler(action_conv)
    app.add_handler(change_pass_conv)
    
    try:
        await app.initialize()
        await app.start()
        await app.updater.start_polling() 
        
        while True:
            await asyncio.sleep(1)

    except asyncio.CancelledError: pass
    except Exception as e: print(f"[FATAL] PTB run failed: {e}")
    finally:
        if app.running:
            await app.updater.stop()
            await app.stop()
        await cleanup_clients()

async def cleanup_clients():
    """Safely disconnects all Telethon clients."""
    all_clients = set(loaded_clients.values())
    for chat_clients in clients.values():
        all_clients.update(chat_clients.values())

    disconnect_tasks = []
    for client in all_clients:
        if client and client.is_connected():
            disconnect_tasks.append(client.disconnect())

    if disconnect_tasks:
        await asyncio.gather(*disconnect_tasks, return_exceptions=True)

if __name__ == "__main__":
    
    if sys.platform == "win32":
        try:
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        except AttributeError: pass
            
    try:
        loop = asyncio.get_event_loop()
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        pass
    except Exception as e:
        print(f"[FATAL] An unexpected error occurred: {e}")
    finally:
        try:
            pending = asyncio.all_tasks(loop)
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        except Exception: pass
        finally:
            if loop.is_running():
                loop.stop()
            if not loop.is_closed():
                loop.close()
