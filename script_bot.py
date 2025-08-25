# -*- coding: utf-8 -*-
"""
tg_accounts_bot.py
Single-file TeleBot frontend + Telethon backend.
Features:
- Admins (ADMINS) full control over all sessions (use, logout_all, remove local)
- Owner saved on add and always has full access
- Password-protected access for third parties
- Show last 5 messages by username or id
- Thread-safe structures and state persistence to state.json
- Handles 2FA, timeouts and common errors
Usage: set BOT_TOKEN, API_ID, API_HASH, ADMINS
"""
import os
import json
import time
import threading
import asyncio
import html
import hashlib
from typing import Optional, Dict, Any, List

from telebot import TeleBot, types, apihelper
from telethon import TelegramClient, events, functions
from telethon.errors import SessionPasswordNeededError, UsernameNotOccupiedError

# ---------------- CONFIG ----------------
BOT_TOKEN = "7577232373:AAGau19QU2x_TVmIJjQPWw60jb8WAySkgU4"
API_ID = 20111454
API_HASH = "e0040834c399df8ac420058eee0af322"

# set admin telegram ids
ADMINS = {6999672555}

SESSIONS_DIR = "sessions"
STATE_FILE = "state.json"
os.makedirs(SESSIONS_DIR, exist_ok=True)

bot = TeleBot(BOT_TOKEN, parse_mode="HTML")

# ---------------- concurrency primitives ----------------
_wrappers_lock = threading.RLock()
_pending_lock = threading.RLock()
_state_lock = threading.RLock()
_allowed_lock = threading.RLock()

# ---------------- runtime storage ----------------
wrappers: List["ClientWrapper"] = []
session_names: List[str] = []
pending_wrappers: Dict[int, Dict[str, Any]] = {}
pending_next = 0

state_store: Dict[str, Dict[str, Any]] = {}
user_fsm: Dict[int, Dict[str, Any]] = {}
allowed_sessions_per_user: Dict[int, List[int]] = {}

# ---------------- util ----------------
def _safe_write_state():
    with _state_lock:
        try:
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(state_store, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

def load_state():
    global state_store
    with _state_lock:
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, "r", encoding="utf-8") as f:
                    state_store = json.load(f)
            except Exception:
                state_store = {}
        else:
            state_store = {}

load_state()

def sanitize(s: str) -> str:
    return html.escape(str(s))

def set_fsm(user_id: int, state: str, data: Optional[Dict[str, Any]] = None):
    user_fsm[user_id] = {"state": state, "data": data or {}}

def get_fsm(user_id: int) -> Optional[Dict[str, Any]]:
    return user_fsm.get(user_id)

def clear_fsm(user_id: int):
    user_fsm.pop(user_id, None)

def hash_password(pwd: str, salt: Optional[str] = None) -> str:
    if salt is None:
        salt = os.urandom(8).hex()
    h = hashlib.sha256((salt + pwd).encode("utf-8")).hexdigest()
    return f"{salt}${h}"

def verify_password(stored: str, candidate: str) -> bool:
    try:
        salt, h = stored.split("$", 1)
        return hashlib.sha256((salt + candidate).encode("utf-8")).hexdigest() == h
    except Exception:
        return False

# ---------------- Telethon wrapper ----------------
class ClientWrapper:
    def __init__(self, session_name: str, api_id:int=API_ID, api_hash:str=API_HASH):
        self.session_name = session_name
        # Telethon accepts either a path or a session name; use full path to avoid collisions
        self.session_path = os.path.join(SESSIONS_DIR, session_name)
        self.api_id = api_id
        self.api_hash = api_hash
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.client: Optional[TelegramClient] = None
        self.thread: Optional[threading.Thread] = None
        self._ready = threading.Event()
        self._stop = False
        self._handlers_attached = False

    def start_thread(self, wait: float = 10.0):
        if self.thread and self.thread.is_alive():
            return
        t = threading.Thread(target=self._thread_main, daemon=True)
        self.thread = t
        t.start()
        self._ready.wait(timeout=wait)

    def _thread_main(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self.loop = loop
        try:
            # create client with file-based session name
            self.client = TelegramClient(self.session_path, self.api_id, self.api_hash)
            loop.run_until_complete(self._connect_and_idle())
        except Exception:
            try:
                loop.run_forever()
            except Exception:
                pass

    async def _connect_and_idle(self):
        try:
            await self.client.connect()
        except Exception:
            pass
        if not self._handlers_attached:
            attach_auto_handlers(self, self.session_name)
            self._handlers_attached = True
        self._ready.set()
        while not self._stop:
            await asyncio.sleep(60)

    def run_coro(self, coro):
        if not self.thread or not self.thread.is_alive() or self.loop is None:
            self.start_thread()
        wait_seconds = 0.0
        while self.loop is None and wait_seconds < 5.0:
            time.sleep(0.05)
            wait_seconds += 0.05
        if self.loop is None:
            raise RuntimeError("Client loop not available")
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return fut

    def is_authorized(self, timeout=5) -> bool:
        try:
            if not self.client:
                return False
            fut = self.run_coro(self.client.is_user_authorized())
            return bool(fut.result(timeout=timeout))
        except Exception:
            return False

    def disconnect(self):
        try:
            self._stop = True
            if self.client and self.loop:
                try:
                    self.run_coro(self.client.disconnect()).result(timeout=10)
                except Exception:
                    pass
        except Exception:
            pass

# ---------------- finalize / remove ----------------
def finalize_authorized_wrapper(wrapper: ClientWrapper, owner_user_id: Optional[int]=None):
    with _wrappers_lock:
        if wrapper.session_name in session_names:
            return session_names.index(wrapper.session_name)
        wrappers.append(wrapper)
        session_names.append(wrapper.session_name)
    with _state_lock:
        st = state_store.setdefault(wrapper.session_name, {
            "auto_reply": False,
            "trigger": "",
            "reply": "",
            "auto_read": False,
            "password": "",
            "owner_user_id": None
        })
        if owner_user_id is not None:
            st["owner_user_id"] = int(owner_user_id)
        _safe_write_state()
    return len(wrappers)-1

def remove_local_session_by_idx(idx:int):
    with _wrappers_lock:
        if 0 <= idx < len(wrappers):
            w = wrappers.pop(idx)
            name = session_names.pop(idx)
            try:
                w.disconnect()
            except Exception:
                pass
            base = os.path.join(SESSIONS_DIR, name)
            # remove common Telethon session file extensions
            for ext in ("", ".session", ".session-journal", ".sqlite", ".json"):
                path = base + ext
                try:
                    if os.path.exists(path):
                        os.remove(path)
                except Exception:
                    pass
            with _state_lock:
                if name in state_store:
                    state_store.pop(name)
                    _safe_write_state()
            return True
    return False

# ---------------- keyboards ----------------
def main_kb(user_id: Optional[int]=None):
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("➕ Добавить аккаунт", "📂 Аккаунты")
    kb.row("ℹ️ Помощь")
    if user_id and user_id in ADMINS:
        kb.row("⚙️ Админ")
    return kb

def accounts_kb():
    kb = types.InlineKeyboardMarkup()
    with _wrappers_lock:
        if not session_names:
            kb.add(types.InlineKeyboardButton("➕ Добавить", callback_data="add_account"))
            return kb
        for i, name in enumerate(session_names, 1):
            kb.add(types.InlineKeyboardButton(f"{i}. {name}", callback_data=f"acc:{i-1}"))
    kb.add(types.InlineKeyboardButton("➕ Добавить", callback_data="add_account"))
    return kb

def account_menu_kb(idx: int, limited: bool=False):
    kb = types.InlineKeyboardMarkup(row_width=2)
    actions = [
        ("✉️ Send", f"send:{idx}"),
        ("🖼 Send Photo", f"send_photo:{idx}"),
        ("📎 Send File", f"send_file:{idx}"),
        ("📇 Contacts", f"contacts:{idx}"),
        ("💬 Chats", f"chats:{idx}"),
        ("📂 Groups", f"groups:{idx}"),
        ("📣 Broadcast", f"broadcast:{idx}"),
        ("⏰ Schedule", f"schedule:{idx}"),
        ("🔁 Show last 5", f"show_last:{idx}")
    ]
    for lbl, cb in actions:
        kb.add(types.InlineKeyboardButton(lbl, callback_data=cb))
    if not limited:
        kb.add(types.InlineKeyboardButton("🤖 AR ON", f"ar_on:{idx}"),
               types.InlineKeyboardButton("⛔ AR OFF", f"ar_off:{idx}"))
        kb.add(types.InlineKeyboardButton("👁 ARD ON", f"ard_on:{idx}"),
               types.InlineKeyboardButton("🙈 ARD OFF", f"ard_off:{idx}"))
        kb.add(types.InlineKeyboardButton("ℹ️ Info", f"info:{idx}"),
               types.InlineKeyboardButton("🚪 Logout", f"logout:{idx}"))
        kb.add(types.InlineKeyboardButton("🗑 Удалить локальную сессию", f"remove_local:{idx}"))
    kb.add(types.InlineKeyboardButton("🔙 Назад", callback_data="back_accounts"))
    return kb

def password_choice_kb(idx:int):
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("Установить пароль", callback_data=f"set_pwd:{idx}"),
           types.InlineKeyboardButton("Пропустить", callback_data=f"skip_pwd:{idx}"))
    return kb

# ---------------- auto handlers ----------------
def attach_auto_handlers(wrapper: ClientWrapper, session_name: str):
    if not wrapper.client:
        return
    client = wrapper.client

    @client.on(events.NewMessage(incoming=True))
    async def _on_new(event):
        try:
            with _state_lock:
                st = state_store.get(session_name, {})
            if st.get("auto_reply") and event.is_private:
                trig = (st.get("trigger") or "").lower()
                rep = st.get("reply") or ""
                text = (event.raw_text or "").lower()
                if trig and trig in text and rep:
                    try:
                        await event.respond(rep)
                    except Exception:
                        pass
            if st.get("auto_read") and event.is_private:
                mid = getattr(event.message, "id", None)
                if mid is not None:
                    try:
                        peer = await event.get_input_chat()
                    except Exception:
                        try:
                            peer = await event.get_input_sender()
                        except Exception:
                            peer = None
                    if peer is not None:
                        try:
                            await client(functions.messages.ReadHistoryRequest(peer=peer, max_id=mid))
                        except Exception:
                            try:
                                await client.send_read_acknowledge(peer, max_id=mid)
                            except Exception:
                                try:
                                    await event.message.mark_read()
                                except Exception:
                                    pass
        except Exception:
            pass

# ---------------- permission checks ----------------
def is_owner_or_admin(user_id: int, session_idx: int) -> bool:
    if user_id in ADMINS:
        return True
    with _wrappers_lock:
        if 0 <= session_idx < len(session_names):
            sess = session_names[session_idx]
            with _state_lock:
                st = state_store.get(sess, {})
            owner = st.get("owner_user_id")
            if owner is not None and int(owner) == int(user_id):
                return True
    return False

def has_access(user_id:int, session_idx:int) -> bool:
    if is_owner_or_admin(user_id, session_idx):
        return True
    with _allowed_lock:
        allowed = allowed_sessions_per_user.get(int(user_id), [])
        return session_idx in allowed

# ---------------- entity resolution ----------------
def resolve_entity(wrapper: ClientWrapper, peer: str, timeout: float = 20.0):
    peer = peer.strip()
    if not peer:
        raise ValueError("Empty peer")
    if peer.startswith("http://") or peer.startswith("https://"):
        peer = peer.rstrip("/").split("/")[-1]
    # numeric id
    try:
        nid = int(peer)
        return nid
    except Exception:
        pass
    if not wrapper.client:
        raise RuntimeError("Client not ready")
    fut = wrapper.run_coro(wrapper.client.get_entity(peer))
    return fut.result(timeout=timeout)

# ---------------- TeleBot handlers ----------------
@bot.message_handler(commands=["start"])
def cmd_start(m):
    bot.send_message(m.chat.id, "Управление TG-аккаунтами. Выберите:", reply_markup=main_kb(m.from_user.id))

@bot.message_handler(func=lambda m: m.text == "ℹ️ Помощь")
def cmd_help(m):
    txt = ("Инструкция:\n"
           "• ➕ Добавить аккаунт — добавить по номеру (бот попросит код)\n"
           "• 📂 Аккаунты — список с подменю\n"
           "Владелец аккаунта — тот, кто добавил номер. Только владелец и админы имеют полный доступ.")
    bot.send_message(m.chat.id, txt)

@bot.message_handler(func=lambda m: m.text == "➕ Добавить аккаунт")
def msg_add_account(m):
    bot.send_message(m.chat.id, "Введите номер телефона в формате +7...")
    set_fsm(m.from_user.id, "adding_phone")

@bot.message_handler(func=lambda m: m.text == "📂 Аккаунты")
def msg_accounts(m):
    bot.send_message(m.chat.id, "Аккаунты:", reply_markup=accounts_kb())

@bot.message_handler(func=lambda m: m.text == "⚙️ Админ")
def msg_admin(m):
    if m.from_user.id not in ADMINS:
        bot.send_message(m.chat.id, "Доступно только админам.")
        return
    kb = types.InlineKeyboardMarkup()
    with _wrappers_lock:
        for i, name in enumerate(session_names,1):
            kb.add(types.InlineKeyboardButton(f"{i}. {name}", callback_data=f"admin_acc:{i-1}"))
    kb.add(types.InlineKeyboardButton("Обновить", callback_data="admin_refresh"))
    bot.send_message(m.chat.id, "Admin: список сессий", reply_markup=kb)

@bot.callback_query_handler(func=lambda c: True)
def cb_handler(call):
    data = call.data or ""
    chat_id = call.message.chat.id
    user_id = call.from_user.id
    try:
        if data == "back_accounts":
            try:
                bot.edit_message_text("Аккаунты:", chat_id, call.message.message_id, reply_markup=accounts_kb())
            except apihelper.ApiTelegramException:
                pass
            return

        if data == "add_account":
            bot.send_message(chat_id, "Введите номер телефона в формате +7...")
            set_fsm(user_id, "adding_phone")
            return

        if data.startswith("admin_refresh"):
            if user_id not in ADMINS:
                bot.answer_callback_query(call.id, "Access denied")
                return
            try:
                bot.edit_message_text("Admin: список сессий", chat_id, call.message.message_id, reply_markup=call.message.reply_markup)
            except Exception:
                pass
            return

        if data.startswith("admin_acc:"):
            if user_id not in ADMINS:
                bot.answer_callback_query(call.id, "Access denied")
                return
            idx = int(data.split(":",1)[1])
            with _wrappers_lock:
                if not (0 <= idx < len(wrappers)):
                    bot.answer_callback_query(call.id, "Аккаунт не найден")
                    return
            kb = types.InlineKeyboardMarkup(row_width=2)
            kb.add(types.InlineKeyboardButton("ℹ️ Info", callback_data=f"info:{idx}"),
                   types.InlineKeyboardButton("Выйти со всех устройств", callback_data=f"logout_all:{idx}"))
            kb.add(types.InlineKeyboardButton("Удалить локальную сессию", callback_data=f"remove_local:{idx}"))
            kb.add(types.InlineKeyboardButton("Использовать аккаунт", callback_data=f"use_as_admin:{idx}"))
            kb.add(types.InlineKeyboardButton("🔙 Назад", callback_data="admin_refresh"))
            try:
                bot.edit_message_text(f"Admin: <b>{sanitize(session_names[idx])}</b>", chat_id, call.message.message_id, reply_markup=kb)
            except apihelper.ApiTelegramException:
                pass
            return

        if data.startswith("use_as_admin:"):
            idx = int(data.split(":",1)[1])
            if user_id not in ADMINS:
                bot.answer_callback_query(call.id, "Access denied")
                return
            with _allowed_lock:
                # avoid duplicates
                lst = allowed_sessions_per_user.setdefault(user_id, [])
                if idx not in lst:
                    lst.append(idx)
            try:
                bot.edit_message_text(f"Меню аккаунта (admin): <b>{sanitize(session_names[idx])}</b>", chat_id, call.message.message_id, reply_markup=account_menu_kb(idx, limited=False))
            except apihelper.ApiTelegramException:
                pass
            return

        if data.startswith("acc:"):
            idx = int(data.split(":",1)[1])
            with _wrappers_lock:
                if not (0 <= idx < len(wrappers)):
                    bot.answer_callback_query(call.id, "Аккаунт не найден")
                    return
            if has_access(user_id, idx):
                limited = not is_owner_or_admin(user_id, idx)
                try:
                    bot.edit_message_text(f"Меню аккаунта: <b>{sanitize(session_names[idx])}</b>", chat_id, call.message.message_id, reply_markup=account_menu_kb(idx, limited=limited))
                except apihelper.ApiTelegramException:
                    pass
                return
            # require password for non-owner/non-admin
            with _state_lock:
                sess = session_names[idx]
                st = state_store.get(sess, {})
                pwd = st.get("password","")
            if pwd:
                set_fsm(user_id, "auth_password", {"idx": idx})
                bot.send_message(chat_id, "Установлен пароль. Введите пароль для доступа к аккаунту:")
                return
            bot.answer_callback_query(call.id, "Доступ запрещён. Обратитесь к владельцу или администратору.")
            return

        # per-account commands
        if data.startswith(("contacts:","chats:","groups:","info:","logout:","remove_local:","logout_all:","show_last:")):
            cmd, sidx = data.split(":",1)
            idx = int(sidx)
            with _wrappers_lock:
                if not (0 <= idx < len(wrappers)):
                    bot.answer_callback_query(call.id, "Аккаунт не найден")
                    return
            if cmd in ("contacts","chats","groups","show_last"):
                if not has_access(user_id, idx):
                    bot.answer_callback_query(call.id, "Нет доступа к этой операции")
                    return
            wrapper = wrappers[idx]
            if cmd == "contacts":
                try:
                    fut = wrapper.run_coro(wrapper.client(functions.contacts.GetContactsRequest(hash=0)))
                    res = fut.result(timeout=20)
                    users = getattr(res, "users", []) or []
                    lines = [f"- {getattr(u,'first_name','')} {getattr(u,'last_name','')} | id={u.id} | @{getattr(u,'username',None) or ''}" for u in users]
                    text = "\n".join(lines) or "Нет контактов."
                    try:
                        bot.edit_message_text(f"<pre>{sanitize(text)[:4000]}</pre>", chat_id, call.message.message_id, reply_markup=account_menu_kb(idx, limited=not is_owner_or_admin(user_id, idx)))
                    except apihelper.ApiTelegramException:
                        pass
                except Exception as e:
                    bot.answer_callback_query(call.id, f"Ошибка: {e}")
                return
            if cmd == "chats":
                async def collect():
                    out=[]
                    async for d in wrapper.client.iter_dialogs(limit=50):
                        nm = getattr(d, "name", None) or getattr(d.entity, "title", None) or ""
                        out.append(f"- {nm} | id={d.id}")
                    return out
                try:
                    lines = wrapper.run_coro(collect()).result(timeout=20)
                    text = "\n".join(lines) or "Нет диалогов."
                    try:
                        bot.edit_message_text(f"<pre>{sanitize(text)[:4000]}</pre>", chat_id, call.message.message_id, reply_markup=account_menu_kb(idx, limited=not is_owner_or_admin(user_id, idx)))
                    except apihelper.ApiTelegramException:
                        pass
                except Exception as e:
                    bot.answer_callback_query(call.id, f"Ошибка: {e}")
                return
            if cmd == "groups":
                async def collectg():
                    out=[]
                    async for d in wrapper.client.iter_dialogs(limit=200):
                        if d.is_group or d.is_channel:
                            nm = getattr(d, "name", None) or getattr(d.entity, "title", None) or ""
                            out.append(f"- {nm} | id={d.id} | is_channel={d.is_channel} | is_group={d.is_group}")
                    return out
                try:
                    lines = wrapper.run_coro(collectg()).result(timeout=30)
                    text = "\n".join(lines) or "Нет групп/каналов."
                    try:
                        bot.edit_message_text(f"<pre>{sanitize(text)[:4000]}</pre>", chat_id, call.message.message_id, reply_markup=account_menu_kb(idx, limited=not is_owner_or_admin(user_id, idx)))
                    except apihelper.ApiTelegramException:
                        pass
                except Exception as e:
                    bot.answer_callback_query(call.id, f"Ошибка: {e}")
                return
            if cmd == "info":
                try:
                    me = wrapper.run_coro(wrapper.client.get_me()).result(timeout=10)
                    started = "running" if wrapper.thread and wrapper.thread.is_alive() else "stopped"
                    text = (f"Session: {wrapper.session_path}\nStarted: {started}\nAccount ID: {me.id}\nUsername: {getattr(me,'username',None)}\nName: {getattr(me,'first_name','')} {getattr(me,'last_name','')}")
                    try:
                        bot.edit_message_text(f"<pre>{sanitize(text)}</pre>", chat_id, call.message.message_id, reply_markup=account_menu_kb(idx, limited=not is_owner_or_admin(user_id, idx)))
                    except apihelper.ApiTelegramException:
                        pass
                except Exception as e:
                    bot.answer_callback_query(call.id, f"Ошибка: {e}")
                return
            if cmd == "logout":
                try:
                    bot.edit_message_text("Выберите действие: ⤵️", chat_id, call.message.message_id, reply_markup=types.InlineKeyboardMarkup().add(
                        types.InlineKeyboardButton("Выйти со всех устройств", callback_data=f"logout_all:{idx}"),
                        types.InlineKeyboardButton("🔙 Назад", callback_data=f"acc:{idx}")
                    ))
                except apihelper.ApiTelegramException:
                    pass
                return
            if cmd == "remove_local":
                if not is_owner_or_admin(user_id, idx):
                    bot.answer_callback_query(call.id, "Нет доступа")
                    return
                ok = remove_local_session_by_idx(idx)
                if ok:
                    try:
                        bot.edit_message_text("Локальная сессия удалена.", chat_id, call.message.message_id, reply_markup=accounts_kb())
                    except apihelper.ApiTelegramException:
                        pass
                else:
                    bot.answer_callback_query(call.id, "Ошибка удаления")
                return
            if cmd == "logout_all":
                if not is_owner_or_admin(user_id, idx):
                    bot.answer_callback_query(call.id, "Нет доступа")
                    return
                try:
                    wrappers[idx].run_coro(wrappers[idx].client(functions.auth.ResetAuthorizationsRequest())).result(timeout=10)
                    bot.answer_callback_query(call.id, "Вышел со всех устройств")
                except Exception as e:
                    bot.answer_callback_query(call.id, f"Ошибка: {e}")
                return
            if cmd == "show_last":
                # ask for peer id/username
                set_fsm(user_id, "show_last_await_peer", {"idx": idx})
                bot.send_message(chat_id, "Введите username или id чата, чтобы получить 5 последних сообщений:")
                return

        # set password / skip after adding account
        if data.startswith(("set_pwd:","skip_pwd:")):
            cmd, sidx = data.split(":",1)
            idx = int(sidx)
            with _wrappers_lock:
                if not (0 <= idx < len(wrappers)):
                    bot.answer_callback_query(call.id, "Аккаунт не найден")
                    return
            if cmd == "skip_pwd":
                bot.answer_callback_query(call.id, "Пропущено")
                return
            if cmd == "set_pwd":
                set_fsm(user_id, "set_account_password", {"idx": idx})
                bot.send_message(chat_id, "Введите пароль для сессии:")
                return

        # auto-reply toggles (owner/admin only)
        if any(data.startswith(p) for p in ("ar_on:","ar_off:","ard_on:","ard_off:")):
            cmd, sidx = data.split(":",1)
            idx = int(sidx)
            if not is_owner_or_admin(user_id, idx):
                bot.answer_callback_query(call.id, "Нет доступа")
                return
            with _state_lock:
                name = session_names[idx]
                st = state_store.setdefault(name, {"auto_reply": False, "trigger": "", "reply": "", "auto_read": False, "password":"", "owner_user_id": None})
            if cmd == "ar_on":
                set_fsm(user_id, "ar_set_trigger", {"idx": idx})
                bot.send_message(chat_id, "Введите триггер (подстрока):")
            elif cmd == "ar_off":
                with _state_lock:
                    st["auto_reply"] = False
                    _safe_write_state()
                bot.answer_callback_query(call.id, "AutoReply выключен")
                try:
                    bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=account_menu_kb(idx))
                except apihelper.ApiTelegramException:
                    pass
            elif cmd == "ard_on":
                with _state_lock:
                    st["auto_read"] = True
                    _safe_write_state()
                bot.answer_callback_query(call.id, "AutoRead включён")
                try:
                    bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=account_menu_kb(idx))
                except apihelper.ApiTelegramException:
                    pass
            elif cmd == "ard_off":
                with _state_lock:
                    st["auto_read"] = False
                    _safe_write_state()
                bot.answer_callback_query(call.id, "AutoRead выключен")
                try:
                    bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=account_menu_kb(idx))
                except apihelper.ApiTelegramException:
                    pass
            return

        bot.answer_callback_query(call.id, "Действие не реализовано")
    except Exception as e:
        try:
            bot.answer_callback_query(call.id, f"Ошибка: {e}", show_alert=True)
        except Exception:
            pass

# ---------------- FSM message handler ----------------
@bot.message_handler(func=lambda m: get_fsm(m.from_user.id) is not None)
def fsm_handler(m):
    user_id = m.from_user.id
    st = get_fsm(user_id)
    if not st:
        return
    name = st["state"]
    data = st.get("data", {})

    # adding phone -> create pending wrapper, send code
    if name == "adding_phone":
        phone = m.text.strip()
        if not phone.startswith("+"):
            bot.send_message(m.chat.id, "Неверный формат. Должен начинаться с +. Попробуйте ещё раз.")
            return
        global pending_next
        pending_name = phone.replace("+","").replace(" ","").replace("-","")
        wrapper = ClientWrapper(pending_name)
        wrapper.start_thread()
        with _pending_lock:
            pending_id = pending_next
            pending_next += 1
            pending_wrappers[pending_id] = {"wrapper": wrapper, "owner_id": int(user_id), "phone": phone}
        try:
            fut = wrapper.run_coro(wrapper.client.send_code_request(phone))
            fut.result(timeout=20)
            set_fsm(user_id, "await_code", {"pending_id": pending_id, "phone": phone})
            bot.send_message(m.chat.id, f"Код отправлен на {phone}. Введите код:")
        except Exception as e:
            bot.send_message(m.chat.id, f"Ошибка отправки кода: {e}")
            try:
                wrapper.disconnect()
            except Exception:
                pass
            with _pending_lock:
                pending_wrappers.pop(pending_id, None)
            clear_fsm(user_id)
        return

    # await code
    if name == "await_code":
        code = m.text.strip()
        pending_id = data.get("pending_id")
        with _pending_lock:
            pending = pending_wrappers.get(pending_id)
        if not pending:
            bot.send_message(m.chat.id, "Внутренняя ошибка. Повторите добавление.")
            clear_fsm(user_id)
            return
        wrapper = pending["wrapper"]
        phone = pending["phone"]
        owner_id = pending["owner_id"]
        try:
            fut = wrapper.run_coro(wrapper.client.sign_in(phone, code))
            try:
                fut.result(timeout=30)
            except Exception as e:
                cause = getattr(e, "__cause__", None)
                if isinstance(cause, SessionPasswordNeededError) or "password" in str(e).lower() or "2fa" in str(e).lower():
                    set_fsm(user_id, "await_2fa", {"pending_id": pending_id, "owner_id": owner_id})
                    bot.send_message(m.chat.id, "Требуется пароль 2FA. Введите пароль:")
                    return
                else:
                    raise
            idx = finalize_authorized_wrapper(wrapper, owner_user_id=owner_id)
            with _pending_lock:
                pending_wrappers.pop(pending_id, None)
            bot.send_message(m.chat.id, f"Аккаунт добавлен и авторизован. Индекс: {idx}")
            bot.send_message(m.chat.id, "Хотите установить пароль для доступа к этой сессии?", reply_markup=password_choice_kb(idx))
            clear_fsm(user_id)
            return
        except Exception as e:
            bot.send_message(m.chat.id, f"Ошибка входа: {e}")
            try:
                wrapper.disconnect()
            except Exception:
                pass
            with _pending_lock:
                pending_wrappers.pop(pending_id, None)
            clear_fsm(user_id)
        return

    # await 2fa
    if name == "await_2fa":
        pwd = m.text.strip()
        pending_id = data.get("pending_id")
        owner_id = data.get("owner_id", user_id)
        with _pending_lock:
            pending = pending_wrappers.get(pending_id)
        if not pending:
            bot.send_message(m.chat.id, "Внутренняя ошибка. Повторите добавление.")
            clear_fsm(user_id)
            return
        wrapper = pending["wrapper"]
        try:
            wrapper.run_coro(wrapper.client.sign_in(password=pwd)).result(timeout=30)
            idx = finalize_authorized_wrapper(wrapper, owner_user_id=owner_id)
            with _pending_lock:
                pending_wrappers.pop(pending_id, None)
            bot.send_message(m.chat.id, f"2FA пройдена. Аккаунт добавлен. Индекс: {idx}")
            bot.send_message(m.chat.id, "Хотите установить пароль для доступа к этой сессии?", reply_markup=password_choice_kb(idx))
            clear_fsm(user_id)
        except Exception as e:
            bot.send_message(m.chat.id, f"Ошибка 2FA: {e}")
            try:
                wrapper.disconnect()
            except Exception:
                pass
            with _pending_lock:
                pending_wrappers.pop(pending_id, None)
            clear_fsm(user_id)
        return

    # set account password after adding
    if name == "set_account_password":
        pwd = m.text.strip()
        idx = data.get("idx")
        with _wrappers_lock:
            if idx is None or not (0 <= idx < len(session_names)):
                bot.send_message(m.chat.id, "Внутренняя ошибка.")
                clear_fsm(user_id)
                return
            sess = session_names[idx]
        with _state_lock:
            state_store.setdefault(sess, {"auto_reply": False, "trigger": "", "reply": "", "auto_read": False, "password":"", "owner_user_id": None})
            state_store[sess]["password"] = hash_password(pwd)
            _safe_write_state()
        bot.send_message(m.chat.id, "Пароль установлен.")
        clear_fsm(user_id)
        return

    # auth by password to access an account
    if name == "auth_password":
        pwd = m.text.strip()
        idx = data.get("idx")
        with _wrappers_lock:
            if idx is None or not (0 <= idx < len(session_names)):
                bot.send_message(m.chat.id, "Внутренняя ошибка.")
                clear_fsm(user_id)
                return
            sess = session_names[idx]
        with _state_lock:
            st = state_store.get(sess,{})
            stored = st.get("password","")
        if stored and verify_password(stored, pwd):
            with _allowed_lock:
                allowed_sessions_per_user.setdefault(int(user_id), []).append(idx)
            bot.send_message(m.chat.id, "Пароль корректен. Доступ предоставлен.")
            try:
                bot.send_message(m.chat.id, f"Меню аккаунта: <b>{sanitize(sess)}</b>", reply_markup=account_menu_kb(idx, limited=not is_owner_or_admin(user_id, idx)))
            except Exception:
                pass
        else:
            bot.send_message(m.chat.id, "Неверный пароль.")
        clear_fsm(user_id)
        return

    # set auto-reply trigger
    if name == "ar_set_trigger":
        trig = m.text.strip()
        idx = data.get("idx")
        if not is_owner_or_admin(user_id, idx):
            bot.send_message(m.chat.id, "Нет доступа.")
            clear_fsm(user_id)
            return
        with _wrappers_lock:
            sess = session_names[idx]
        with _state_lock:
            st = state_store.setdefault(sess, {"auto_reply": False, "trigger": "", "reply": "", "auto_read": False, "password":"", "owner_user_id": None})
            st["trigger"] = trig
        set_fsm(user_id, "ar_set_reply", {"idx": idx})
        bot.send_message(m.chat.id, "Введите текст автоответа:")
        return

    if name == "ar_set_reply":
        reply = m.text
        idx = data.get("idx")
        with _wrappers_lock:
            sess = session_names[idx]
        with _state_lock:
            st = state_store.setdefault(sess, {"auto_reply": False, "trigger": "", "reply": "", "auto_read": False, "password":"", "owner_user_id": None})
            st["reply"] = reply
            st["auto_reply"] = True
            _safe_write_state()
        bot.send_message(m.chat.id, "AutoReply включён.")
        clear_fsm(user_id)
        return

    # generic send flows
    if name.endswith("_await_peer"):
        cmd = name.split("_await_peer")[0]
        idx = data.get("idx")
        if idx is None:
            bot.send_message(m.chat.id, "Внутренняя ошибка: не указан аккаунт.")
            clear_fsm(user_id)
            return
        peer = m.text.strip()
        data["peer"] = peer
        set_fsm(user_id, f"{cmd}_await_text", data)
        if cmd in ("send_file","send_photo"):
            bot.send_message(m.chat.id, "Укажите путь к файлу на сервере:")
        elif cmd == "broadcast":
            bot.send_message(m.chat.id, "Введите текст рассылки:")
        elif cmd == "schedule":
            bot.send_message(m.chat.id, "Введите текст для отправки:")
        else:
            bot.send_message(m.chat.id, "Введите текст сообщения:")
        return

    if name.endswith("_await_text"):
        cmd = name.split("_await_text")[0]
        data["text"] = m.text
        idx = data.get("idx")
        peer = data.get("peer")
        with _wrappers_lock:
            if idx is None or not (0 <= idx < len(wrappers)):
                bot.send_message(m.chat.id, "Аккаунт не найден.")
                clear_fsm(user_id)
                return
            wrapper = wrappers[idx]
        try:
            entity = resolve_entity(wrapper, peer)
            if cmd == "send":
                wrapper.run_coro(wrapper.client.send_message(entity, data["text"])).result(timeout=20)
                bot.send_message(m.chat.id, "✅ Отправлено.")
            elif cmd in ("send_file", "send_photo"):
                path = data["text"].strip()
                wrapper.run_coro(wrapper.client.send_file(entity, path)).result(timeout=60)
                bot.send_message(m.chat.id, "✅ Файл отправлен.")
            elif cmd == "broadcast":
                contacts = wrapper.run_coro(wrapper.client(functions.contacts.GetContactsRequest(hash=0))).result(timeout=30)
                users = getattr(contacts, "users", []) or []
                sent = 0
                for u in users:
                    try:
                        wrapper.run_coro(wrapper.client.send_message(u.id, data["text"])).result(timeout=10)
                        sent += 1
                        time.sleep(0.2)
                    except Exception:
                        pass
                bot.send_message(m.chat.id, f"Рассылка завершена. Отправлено: {sent}")
            elif cmd == "schedule":
                set_fsm(m.user.id, "schedule_await_delay", data)
                bot.send_message(m.chat.id, "Через сколько секунд отправить? (число)")
                return
        except Exception as e:
            bot.send_message(m.chat.id, f"Ошибка: {e}")
        clear_fsm(user_id)
        return

    if name == "schedule_await_delay":
        data = st["data"]
        idx = data["idx"]
        peer = data["peer"]
        text = data["text"]
        try:
            delay = int(m.text.strip())
        except Exception:
            delay = 0
        with _wrappers_lock:
            wrapper = wrappers[idx]
        def delayed_send():
            time.sleep(delay)
            try:
                ent = resolve_entity(wrapper, peer)
                wrapper.run_coro(wrapper.client.send_message(ent, text)).result(timeout=30)
            except Exception:
                pass
        threading.Thread(target=delayed_send, daemon=True).start()
        bot.send_message(m.chat.id, f"Запланировано через {delay} сек.")
        clear_fsm(user_id)
        return

    # show last 5 messages flow
    if name == "show_last_await_peer":
        idx = data.get("idx")
        peer = m.text.strip()
        with _wrappers_lock:
            if idx is None or not (0 <= idx < len(wrappers)):
                bot.send_message(m.chat.id, "Аккаунт не найден.")
                clear_fsm(user_id)
                return
            wrapper = wrappers[idx]
        try:
            ent = resolve_entity(wrapper, peer)
            msgs = wrapper.run_coro(wrapper.client.get_messages(ent, limit=5)).result(timeout=20)
            lines = []
            for mm in reversed(msgs):
                txt = getattr(mm, "message", "") or ""
                sender = getattr(mm, "sender_id", None)
                t = getattr(mm, "date", None)
                lines.append(f"[{sender}] {t} : {txt}")
            text = "\n".join(lines) or "Нет сообщений."
            bot.send_message(m.chat.id, f"<pre>{sanitize(text)[:4000]}</pre>")
        except Exception as e:
            bot.send_message(m.chat.id, f"Ошибка получения сообщений: {e}")
        clear_fsm(user_id)
        return

    # fallback
    bot.send_message(m.chat.id, "Неизвестное состояние. Сброс.")
    clear_fsm(user_id)

# ---------------- restore existing sessions on startup ----------------
def restore_sessions():
    # find base names assuming Telethon uses files like sessions/<name>.session or sessions/<name>
    files = os.listdir(SESSIONS_DIR)
    bases = set()
    for fname in files:
        base, ext = os.path.splitext(fname)
        if base:
            bases.add(base)
    for base in sorted(bases):
        if base in session_names:
            continue
        try:
            w = ClientWrapper(base)
            w.start_thread()
            # small wait to let client init
            time.sleep(0.1)
            if w.is_authorized(timeout=3):
                finalize_authorized_wrapper(w)
            else:
                try:
                    w.disconnect()
                except Exception:
                    pass
        except Exception:
            try:
                w.disconnect()
            except Exception:
                pass

restore_sessions()

# ---------------- run bot ----------------
if __name__ == "__main__":
    print("TG accounts manager bot running...")
    try:
        bot.infinity_polling(timeout=60, long_polling_timeout=60)
    except KeyboardInterrupt:
        print("Stopped by user")
    except Exception as e:
        print("Polling stopped:", e)
