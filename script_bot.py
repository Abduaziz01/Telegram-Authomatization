# -*- coding: utf-8 -*-
"""
tg_accounts_bot.py
TeleBot (pyTelegramBotAPI) frontend + Telethon backend in one file.
Features:
- add account by phone (code + 2FA)
- list contacts / chats / groups
- send message / file / photo (FSM)
- broadcast to contacts
- schedule message
- auto-reply toggle (trigger + reply)
- auto-read toggle
- session info, logout all, remove local session
State persisted in state.json. Sessions in sessions/<name>.
"""

import os
import json
import time
import threading
import asyncio
import html
from typing import Optional, Dict, Any, List

from telebot import TeleBot, types, apihelper
from telethon import TelegramClient, events, functions
from telethon.errors import SessionPasswordNeededError

# ---------------- CONFIG (you provided these) ----------------
BOT_TOKEN = "8367219501:AAEk40KHWUPwKX9nBvXvYNfwXcti0effSSk"
API_ID = 20111454
API_HASH = "e0040834c399df8ac420058eee0af322"

SESSIONS_DIR = "sessions"
STATE_FILE = "state.json"
os.makedirs(SESSIONS_DIR, exist_ok=True)

bot = TeleBot(BOT_TOKEN, parse_mode="HTML")

# ---------------- storage ----------------
# Active authorized wrappers
wrappers: List["ClientWrapper"] = []
session_names: List[str] = []  # aligned with wrappers
# Pending wrappers (created before authorization completes): idx -> wrapper
pending_wrappers: Dict[int, "ClientWrapper"] = {}
pending_next = 0

# persistent per-session settings
state_store: Dict[str, Dict[str, Any]] = {}
# per-bot-user FSM state: chat_id -> {"state": str, "data": {...}}
user_fsm: Dict[int, Dict[str, Any]] = {}

def load_state():
    global state_store
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                state_store.update(json.load(f))
        except Exception:
            pass

def save_state():
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state_store, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

load_state()

# ---------------- Telethon wrapper ----------------
class ClientWrapper:
    """
    Wrapper runs a TelegramClient inside its own thread+event-loop.
    Client instance is created inside thread to avoid 'no running loop' errors.
    """
    def __init__(self, session_name: str, api_id:int=API_ID, api_hash:str=API_HASH):
        self.session_name = session_name
        self.session_path = os.path.join(SESSIONS_DIR, session_name)
        self.api_id = api_id
        self.api_hash = api_hash
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.client: Optional[TelegramClient] = None
        self.thread: Optional[threading.Thread] = None
        self._ready = threading.Event()
        self._stop = False
        self._handlers_attached = False

    def start_thread(self):
        if self.thread and self.thread.is_alive():
            return
        t = threading.Thread(target=self._thread_main, daemon=True)
        self.thread = t
        t.start()
        # wait until client is created and loop set
        self._ready.wait(timeout=10)

    def _thread_main(self):
        # create a fresh event loop in this thread and create TelegramClient here.
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self.loop = loop
        try:
            # create client inside this loop
            self.client = TelegramClient(self.session_path, self.api_id, self.api_hash)
            # connect and keep running
            loop.run_until_complete(self._connect_and_idle())
        except Exception:
            # if connect fails still keep loop running to accept run_coro calls
            try:
                loop.run_forever()
            except Exception:
                pass

    async def _connect_and_idle(self):
        try:
            await self.client.connect()
        except Exception:
            pass
        # attach handlers
        if not self._handlers_attached:
            attach_auto_handlers(self, self.session_name)
            self._handlers_attached = True
        self._ready.set()
        # keep loop alive
        while not self._stop:
            await asyncio.sleep(60)

    def run_coro(self, coro):
        # ensure thread/loop exists
        if not self.thread or not self.thread.is_alive():
            self.start_thread()
        # submit to loop
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return fut

    def is_authorized(self, timeout=5) -> bool:
        try:
            fut = self.run_coro(self.client.is_user_authorized())
            return fut.result(timeout=timeout)
        except Exception:
            return False

    def disconnect(self):
        try:
            self._stop = True
            if self.client:
                self.run_coro(self.client.disconnect()).result(timeout=10)
        except Exception:
            pass

# ---------------- helpers ----------------
def sanitize(s: str) -> str:
    return html.escape(s)

def set_fsm(chat_id: int, state: str, data: Optional[Dict[str, Any]] = None):
    user_fsm[chat_id] = {"state": state, "data": data or {}}

def get_fsm(chat_id: int) -> Optional[Dict[str, Any]]:
    return user_fsm.get(chat_id)

def clear_fsm(chat_id: int):
    user_fsm.pop(chat_id, None)

def finalize_authorized_wrapper(wrapper: ClientWrapper):
    """Move wrapper from pending to active wrappers after successful auth."""
    # avoid duplicates
    if wrapper.session_name in session_names:
        return session_names.index(wrapper.session_name)
    wrappers.append(wrapper)
    session_names.append(wrapper.session_name)
    state_store.setdefault(wrapper.session_name, {"auto_reply": False, "trigger": "", "reply": "", "auto_read": False})
    save_state()
    return len(wrappers)-1

def remove_local_session_by_idx(idx:int):
    """Remove local session files and wrapper."""
    if 0 <= idx < len(wrappers):
        w = wrappers.pop(idx)
        name = session_names.pop(idx)
        # disconnect
        try:
            w.disconnect()
        except Exception:
            pass
        # delete session files (Telethon may create several files with same base)
        base = os.path.join(SESSIONS_DIR, name)
        for ext in ("", ".session", ".session-journal", ".sqlite", ".json"):
            path = base + ext
            try:
                if os.path.exists(path):
                    os.remove(path)
            except Exception:
                pass
        # remove state
        if name in state_store:
            state_store.pop(name)
            save_state()
        return True
    return False

# ---------------- keyboards ----------------
def main_kb():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("➕ Добавить аккаунт", "📂 Аккаунты")
    kb.row("ℹ️ Помощь")
    return kb

def accounts_kb():
    kb = types.InlineKeyboardMarkup()
    if not session_names:
        kb.add(types.InlineKeyboardButton("➕ Добавить", callback_data="add_account"))
        return kb
    for i, name in enumerate(session_names, 1):
        kb.add(types.InlineKeyboardButton(f"{i}. {name}", callback_data=f"acc:{i-1}"))
    kb.add(types.InlineKeyboardButton("➕ Добавить", callback_data="add_account"))
    return kb

def account_menu_kb(idx: int):
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
        ("🤖 AR ON", f"ar_on:{idx}"),
        ("⛔ AR OFF", f"ar_off:{idx}"),
        ("👁 ARD ON", f"ard_on:{idx}"),
        ("🙈 ARD OFF", f"ard_off:{idx}"),
        ("ℹ️ Info", f"info:{idx}"),
        ("🚪 Logout", f"logout:{idx}"),
        ("🗑 Удалить локальную сессию", f"remove_local:{idx}")
    ]
    for lbl, cb in actions:
        kb.add(types.InlineKeyboardButton(lbl, callback_data=cb))
    kb.add(types.InlineKeyboardButton("🔙 Назад", callback_data="back_accounts"))
    return kb

# ---------------- attach auto handlers ----------------
def attach_auto_handlers(wrapper: ClientWrapper, session_name: str):
    if not wrapper.client:
        return
    client = wrapper.client

    @client.on(events.NewMessage(incoming=True))
    async def _on_new(event):
        try:
            st = state_store.get(session_name, {})
            if st.get("auto_reply") and event.is_private:
                trig = (st.get("trigger") or "").lower()
                rep = st.get("reply") or ""
                text = (event.raw_text or "").lower()
                if trig and trig in text and rep:
                    await event.respond(rep)
            if st.get("auto_read") and event.is_private:
                mid = getattr(event.message, "id", None)
                if mid is not None:
                    try:
                        peer = await event.get_input_chat()
                    except Exception:
                        peer = await event.get_input_sender()
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

# ---------------- TeleBot handlers ----------------
@bot.message_handler(commands=["start"])
def cmd_start(m):
    bot.send_message(m.chat.id, "Управление TG-аккаунтами. Выберите:", reply_markup=main_kb())

@bot.message_handler(func=lambda m: m.text == "ℹ️ Помощь")
def cmd_help(m):
    txt = ("Инструкция:\n"
           "• ➕ Добавить аккаунт — добавить по номеру (бот попросит код)\n"
           "• 📂 Аккаунты — список с подменю (отправка, чаты, контакты, автоответ и т.д.)")
    bot.send_message(m.chat.id, txt)

@bot.message_handler(func=lambda m: m.text == "➕ Добавить аккаунт")
def msg_add_account(m):
    bot.send_message(m.chat.id, "Введите номер телефона в формате +7...")
    set_fsm(m.chat.id, "adding_phone")

@bot.message_handler(func=lambda m: m.text == "📂 Аккаунты")
def msg_accounts(m):
    bot.send_message(m.chat.id, "Аккаунты:", reply_markup=accounts_kb())

@bot.callback_query_handler(func=lambda c: True)
def cb_handler(call):
    data = call.data or ""
    chat_id = call.message.chat.id
    try:
        if data == "back_accounts":
            try:
                bot.edit_message_text("Аккаунты:", chat_id, call.message.message_id, reply_markup=accounts_kb())
            except apihelper.ApiTelegramException:
                pass
            return

        if data == "add_account":
            bot.send_message(chat_id, "Введите номер телефона в формате +7...")
            set_fsm(chat_id, "adding_phone")
            return

        if data.startswith("acc:"):
            idx = int(data.split(":",1)[1])
            if not (0 <= idx < len(wrappers)):
                bot.answer_callback_query(call.id, "Аккаунт не найден")
                return
            name = session_names[idx]
            try:
                bot.edit_message_text(f"Меню аккаунта: <b>{sanitize(name)}</b>", chat_id, call.message.message_id, reply_markup=account_menu_kb(idx))
            except apihelper.ApiTelegramException:
                pass
            return

        # per-account simple commands
        if data.startswith(("contacts:","chats:","groups:","info:","logout:","remove_local:")):
            cmd, sidx = data.split(":",1)
            idx = int(sidx)
            if not (0 <= idx < len(wrappers)):
                bot.answer_callback_query(call.id, "Аккаунт не найден")
                return
            wrapper = wrappers[idx]
            if cmd == "contacts":
                fut = wrapper.run_coro(wrapper.client(functions.contacts.GetContactsRequest(hash=0)))
                try:
                    res = fut.result(timeout=20)
                    users = getattr(res, "users", []) or []
                    lines = [f"- {getattr(u,'first_name','')} {getattr(u,'last_name','')} | id={u.id} | @{getattr(u,'username',None) or ''}" for u in users]
                    text = "\n".join(lines) or "Нет контактов."
                    try:
                        bot.edit_message_text(f"<pre>{sanitize(text)[:4000]}</pre>", chat_id, call.message.message_id, reply_markup=account_menu_kb(idx))
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
                fut2 = wrapper.run_coro(collect())
                try:
                    lines = fut2.result(timeout=20)
                    text = "\n".join(lines) or "Нет диалогов."
                    try:
                        bot.edit_message_text(f"<pre>{sanitize(text)[:4000]}</pre>", chat_id, call.message.message_id, reply_markup=account_menu_kb(idx))
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
                futg = wrapper.run_coro(collectg())
                try:
                    lines = futg.result(timeout=30)
                    text = "\n".join(lines) or "Нет групп/каналов."
                    try:
                        bot.edit_message_text(f"<pre>{sanitize(text)[:4000]}</pre>", chat_id, call.message.message_id, reply_markup=account_menu_kb(idx))
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
                        bot.edit_message_text(f"<pre>{sanitize(text)}</pre>", chat_id, call.message.message_id, reply_markup=account_menu_kb(idx))
                    except apihelper.ApiTelegramException:
                        pass
                except Exception as e:
                    bot.answer_callback_query(call.id, f"Ошибка: {e}")
                return
            if cmd == "logout":
                # show logout options: logout all
                try:
                    bot.edit_message_text("Выберите действие: ⤵️", chat_id, call.message.message_id, reply_markup=types.InlineKeyboardMarkup().add(
                        types.InlineKeyboardButton("Выйти со всех устройств", callback_data=f"logout_all:{idx}"),
                        types.InlineKeyboardButton("🔙 Назад", callback_data=f"acc:{idx}")
                    ))
                except apihelper.ApiTelegramException:
                    pass
                return
            if cmd == "remove_local":
                ok = remove_local_session_by_idx(idx)
                if ok:
                    try:
                        bot.edit_message_text("Локальная сессия удалена.", chat_id, call.message.message_id, reply_markup=accounts_kb())
                    except apihelper.ApiTelegramException:
                        pass
                else:
                    bot.answer_callback_query(call.id, "Ошибка удаления")
                return

        # logout_all handler
        if data.startswith("logout_all:"):
            _, sidx = data.split(":",1)
            idx = int(sidx)
            if not (0 <= idx < len(wrappers)):
                bot.answer_callback_query(call.id, "Аккаунт не найден")
                return
            wrapper = wrappers[idx]
            try:
                wrapper.run_coro(wrapper.client(functions.auth.ResetAuthorizationsRequest())).result(timeout=10)
                bot.answer_callback_query(call.id, "Вышел со всех устройств")
            except Exception as e:
                bot.answer_callback_query(call.id, f"Ошибка: {e}")
            return

        # send / media / broadcast / schedule -> set FSM
        if any(data.startswith(p) for p in ("send:","send_photo:","send_file:","broadcast:","schedule:")):
            cmd, sidx = data.split(":",1)
            idx = int(sidx)
            # store idx in data for subsequent steps
            set_fsm(chat_id, f"{cmd}_await_peer", {"idx": idx})
            bot.send_message(chat_id, "Введите username или id получателя (или 'all' для broadcast):")
            return

        # auto-reply toggles
        if any(data.startswith(p) for p in ("ar_on:","ar_off:","ard_on:","ard_off:")):
            cmd, sidx = data.split(":",1)
            idx = int(sidx)
            if not (0 <= idx < len(wrappers)):
                bot.answer_callback_query(call.id, "Аккаунт не найден")
                return
            name = session_names[idx]
            st = state_store.setdefault(name, {"auto_reply": False, "trigger": "", "reply": "", "auto_read": False})
            if cmd == "ar_on":
                set_fsm(chat_id, "ar_set_trigger", {"idx": idx})
                bot.send_message(chat_id, "Введите триггер (подстрока):")
            elif cmd == "ar_off":
                st["auto_reply"] = False
                save_state()
                bot.answer_callback_query(call.id, "AutoReply выключен")
                try:
                    bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=account_menu_kb(idx))
                except apihelper.ApiTelegramException:
                    pass
            elif cmd == "ard_on":
                st["auto_read"] = True
                save_state()
                bot.answer_callback_query(call.id, "AutoRead включён")
                try:
                    bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=account_menu_kb(idx))
                except apihelper.ApiTelegramException:
                    pass
            elif cmd == "ard_off":
                st["auto_read"] = False
                save_state()
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
@bot.message_handler(func=lambda m: get_fsm(m.chat.id) is not None)
def fsm_handler(m):
    st = get_fsm(m.chat.id)
    name = st["state"]
    data = st.get("data", {})

    # adding phone -> create pending wrapper, send code
    if name == "adding_phone":
        phone = m.text.strip()
        if not phone.startswith("+"):
            bot.send_message(m.chat.id, "Неверный формат. Должен начинаться с +. Попробуйте ещё раз.")
            return
        global pending_next
        pending_name = phone.replace("+","").replace(" ","")
        wrapper = ClientWrapper(pending_name)
        # start thread so that client exists for send_code_request
        wrapper.start_thread()
        pending_id = pending_next
        pending_next += 1
        pending_wrappers[pending_id] = wrapper
        # try send code
        try:
            fut = wrapper.run_coro(wrapper.client.send_code_request(phone))
            fut.result(timeout=20)
            set_fsm(m.chat.id, "await_code", {"pending_id": pending_id, "phone": phone})
            bot.send_message(m.chat.id, f"Код отправлен на {phone}. Введите код:")
        except Exception as e:
            bot.send_message(m.chat.id, f"Ошибка отправки кода: {e}")
            # cleanup pending wrapper
            try:
                wrapper.disconnect()
            except Exception:
                pass
            pending_wrappers.pop(pending_id, None)
            clear_fsm(m.chat.id)
        return

    # await code
    if name == "await_code":
        code = m.text.strip()
        pending_id = data["pending_id"]
        phone = data["phone"]
        wrapper = pending_wrappers.get(pending_id)
        if not wrapper:
            bot.send_message(m.chat.id, "Внутренняя ошибка. Повторите добавление.")
            clear_fsm(m.chat.id)
            return
        try:
            fut = wrapper.run_coro(wrapper.client.sign_in(phone, code))
            try:
                fut.result(timeout=30)
            except Exception as e:
                # check 2FA requirement
                if isinstance(e.__cause__, SessionPasswordNeededError) or "password" in str(e).lower() or "2fa" in str(e).lower():
                    set_fsm(m.chat.id, "await_2fa", {"pending_id": pending_id})
                    bot.send_message(m.chat.id, "Требуется пароль 2FA. Введите пароль:")
                    return
                else:
                    raise
            # sign_in OK -> finalize wrapper into active list
            idx = finalize_authorized_wrapper(wrapper)
            # remove from pending
            pending_wrappers.pop(pending_id, None)
            bot.send_message(m.chat.id, f"Аккаунт добавлен и авторизован. Индекс: {idx}")
            clear_fsm(m.chat.id)
            return
        except Exception as e:
            bot.send_message(m.chat.id, f"Ошибка входа: {e}")
            # cleanup
            try:
                wrapper.disconnect()
            except Exception:
                pass
            pending_wrappers.pop(pending_id, None)
            clear_fsm(m.chat.id)
        return

    # await 2fa
    if name == "await_2fa":
        pwd = m.text.strip()
        pending_id = data["pending_id"]
        wrapper = pending_wrappers.get(pending_id)
        if not wrapper:
            bot.send_message(m.chat.id, "Внутренняя ошибка. Повторите добавление.")
            clear_fsm(m.chat.id)
            return
        try:
            wrapper.run_coro(wrapper.client.sign_in(password=pwd)).result(timeout=30)
            idx = finalize_authorized_wrapper(wrapper)
            pending_wrappers.pop(pending_id, None)
            bot.send_message(m.chat.id, f"2FA пройдена. Аккаунт добавлен. Индекс: {idx}")
            clear_fsm(m.chat.id)
        except Exception as e:
            bot.send_message(m.chat.id, f"Ошибка 2FA: {e}")
            try:
                wrapper.disconnect()
            except Exception:
                pass
            pending_wrappers.pop(pending_id, None)
            clear_fsm(m.chat.id)
        return

    # set auto-reply trigger
    if name == "ar_set_trigger":
        trig = m.text.strip()
        idx = data["idx"]
        if not (0 <= idx < len(wrappers)):
            bot.send_message(m.chat.id, "Аккаунт не найден.")
            clear_fsm(m.chat.id)
            return
        sess = session_names[idx]
        st = state_store.setdefault(sess, {"auto_reply": False, "trigger": "", "reply": "", "auto_read": False})
        st["trigger"] = trig
        set_fsm(m.chat.id, "ar_set_reply", {"idx": idx})
        bot.send_message(m.chat.id, "Введите текст автоответа:")
        return

    if name == "ar_set_reply":
        reply = m.text
        idx = data["idx"]
        sess = session_names[idx]
        st = state_store.setdefault(sess, {"auto_reply": False, "trigger": "", "reply": "", "auto_read": False})
        st["reply"] = reply
        st["auto_reply"] = True
        save_state()
        bot.send_message(m.chat.id, "AutoReply включён.")
        clear_fsm(m.chat.id)
        return

    # generic send flows: "<cmd>_await_peer" -> "<cmd>_await_text"
    if name.endswith("_await_peer"):
        cmd = name.split("_await_peer")[0]
        idx = data.get("idx")
        if idx is None:
            # sometimes idx passed via callback earlier; otherwise ask user to reselect
            bot.send_message(m.chat.id, "Внутренняя ошибка: не указан аккаунт.")
            clear_fsm(m.chat.id)
            return
        peer = m.text.strip()
        data["peer"] = peer
        set_fsm(m.chat.id, f"{cmd}_await_text", data)
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
        if idx is None or not (0 <= idx < len(wrappers)):
            bot.send_message(m.chat.id, "Аккаунт не найден.")
            clear_fsm(m.chat.id)
            return
        wrapper = wrappers[idx]
        try:
            # resolve entity first
            try:
                entity = wrapper.run_coro(wrapper.client.get_entity(peer)).result(timeout=20)
            except Exception:
                # fallback: try int id
                try:
                    entity = int(peer)
                except Exception:
                    raise
            if cmd == "send":
                wrapper.run_coro(wrapper.client.send_message(entity, data["text"])).result(timeout=20)
                bot.send_message(m.chat.id, "✅ Отправлено.")
            elif cmd in ("send_file", "send_photo"):
                path = data["text"].strip()
                wrapper.run_coro(wrapper.client.send_file(entity, path)).result(timeout=60)
                bot.send_message(m.chat.id, "✅ Файл отправлен.")
            elif cmd == "broadcast":
                # contacts
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
                # ask for delay
                set_fsm(m.chat.id, "schedule_await_delay", data)
                bot.send_message(m.chat.id, "Через сколько секунд отправить? (число)")
                return
        except Exception as e:
            bot.send_message(m.chat.id, f"Ошибка: {e}")
        clear_fsm(m.chat.id)
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
        wrapper = wrappers[idx]
        def delayed_send():
            time.sleep(delay)
            try:
                # resolve and send
                try:
                    ent = wrapper.run_coro(wrapper.client.get_entity(peer)).result(timeout=20)
                except Exception:
                    ent = int(peer)
                wrapper.run_coro(wrapper.client.send_message(ent, text)).result(timeout=30)
            except Exception:
                pass
        threading.Thread(target=delayed_send, daemon=True).start()
        bot.send_message(m.chat.id, f"Запланировано через {delay} сек.")
        clear_fsm(m.chat.id)
        return

    # fallback
    bot.send_message(m.chat.id, "Неизвестное состояние. Сброс.")
    clear_fsm(m.chat.id)

# ---------------- restore existing sessions on startup ----------------
def restore_sessions():
    for fname in os.listdir(SESSIONS_DIR):
        base, ext = os.path.splitext(fname)
        if not base:
            continue
        # Telethon session files might be named "<base>.session"
        if base in session_names:
            continue
        # attempt to create wrapper and check if authorized
        try:
            w = ClientWrapper(base)
            w.start_thread()
            time.sleep(0.05)
            if w.is_authorized(timeout=3):
                finalize_authorized_wrapper(w)
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

