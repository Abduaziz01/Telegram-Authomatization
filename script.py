# -*- coding: utf-8 -*-
import asyncio
import threading
import os
import datetime
import json
from telethon import TelegramClient, events
from telethon.tl import functions, types
from telethon.errors import SessionPasswordNeededError

# --------------------------
# Настройка
# --------------------------
SESSION_DIR = "sessions"
os.makedirs(SESSION_DIR, exist_ok=True)

STATE_FILE = "state.json"
DEFAULT_API_ID = 20111454
DEFAULT_API_HASH = "e0040834c399df8ac420058eee0af322"

clients = []  # список TelegramClient
state = {}    # { session_name: {"auto_reply":bool,"trigger":str,"reply":str,"auto_read":bool} }
meta = {}     # { session_name: {"started":datetime, "login_time":datetime, "me": user_obj} }

# --------------------------
# Загрузка и сохранение состояния
# --------------------------
def load_state():
    global state
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                state.update(json.load(f))
        except Exception as e:
            print(f"[WARN] Не удалось загрузить state.json: {e}")

def save_state():
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

# --------------------------
# Утилиты
# --------------------------
def session_name_from_client(client: TelegramClient) -> str:
    try:
        return os.path.basename(client.session.filename)
    except:
        return str(id(client))

async def resolve_entity(client: TelegramClient, peer_str: str):
    try:
        return await client.get_entity(peer_str)
    except:
        try:
            return int(peer_str)
        except Exception:
            raise

def human_delta(dt: datetime.datetime) -> str:
    if dt is None:
        return "unknown"
    delta = datetime.datetime.now() - dt
    secs = int(delta.total_seconds())
    if secs < 60:
        return f"{secs}s"
    mins = secs // 60
    if mins < 60:
        return f"{mins}m"
    hrs = mins // 60
    if hrs < 24:
        return f"{hrs}h{mins%60}m"
    days = hrs // 24
    return f"{days}d{hrs%24}h"

# --------------------------
# Handlers: AutoReply + AutoRead
# --------------------------
def make_handlers_for(client: TelegramClient):
    name = session_name_from_client(client)

    async def on_new_message(event):
        st = state.get(name, {})

        # --- AUTO-REPLY (для всех личек) ---
        try:
            if st.get("auto_reply") and event.is_private:
                trigger = (st.get("trigger") or "").lower()
                reply_text = st.get("reply") or ""
                text = (event.raw_text or "").lower()
                if trigger and trigger in text and reply_text:
                    await event.respond(reply_text)
                    print(f"[{name}] AutoReply -> replied to {event.sender_id}")
        except Exception as e:
            print(f"[{name}] AutoReply error: {e}")

        # --- AUTO-READ (для всех личек, даже не в контактах) ---
        try:
            if st.get("auto_read") and event.is_private:
                mid = getattr(event.message, "id", None)
                if mid is not None:
                    # Берём корректный InputPeer прямо из события
                    try:
                        peer = await event.get_input_chat()
                    except Exception:
                        # запасной вариант: input отправителя
                        peer = await event.get_input_sender()

                    try:
                        # низкоуровневый RPC – самый надёжный
                        await client(functions.messages.ReadHistoryRequest(
                            peer=peer,
                            max_id=mid
                        ))
                    except Exception:
                        # мягкий откат: high-level helper
                        try:
                            await client.send_read_acknowledge(peer, max_id=mid)
                        except Exception:
                            # последний шанс: отметить сам объект сообщения
                            try:
                                await event.message.mark_read()
                            except Exception:
                                pass
                    print(f"[{name}] AutoRead -> marked chat {event.chat_id} up to {mid}")
        except Exception as e:
            print(f"[{name}] AutoRead error: {e}")

    return on_new_message


# --------------------------
# Основные функции (30+)
# --------------------------
async def send_message(client):
    target = input("Введите username или ID получателя: ").strip()
    text = input("Введите текст сообщения: ").strip()
    await client.send_message(await resolve_entity(client, target), text)
    print("[OK] Сообщение отправлено")

async def send_photo(client):
    target = input("Кому (username/ID): ").strip()
    path = input("Путь к фото: ").strip()
    await client.send_file(await resolve_entity(client, target), path)
    print("[OK] Фото отправлено")

async def send_video(client):
    target = input("Кому (username/ID): ").strip()
    path = input("Путь к видео: ").strip()
    await client.send_file(await resolve_entity(client, target), path)
    print("[OK] Видео отправлено")

async def send_file_doc(client):
    target = input("Кому (username/ID): ").strip()
    path = input("Путь к файлу: ").strip()
    await client.send_file(await resolve_entity(client, target), path)
    print("[OK] Файл отправлен")

async def show_chats(client):
    async for d in client.iter_dialogs(limit=50):
        kind = "user"
        if d.is_group: kind = "group"
        if d.is_channel: kind = "channel"
        uname = getattr(d.entity, "username", None)
        print(f"- {d.name} | type={kind} | id={d.id} | username={uname} | unread={d.unread_count}")

async def read_last_messages(client):
    chat = input("ID или username чата: ").strip()
    ent = await resolve_entity(client, chat)
    lim = input("Сколько сообщений показать (Enter=10): ").strip()
    limit = int(lim) if lim.isdigit() else 10
    msgs = await client.get_messages(ent, limit=limit)
    for m in msgs:
        text = m.message or ""
        print(f"[{m.id}] from={m.sender_id} out={m.out} | {text}")

async def show_contacts(client):
    contacts = await client.get_contacts()
    for c in contacts:
        print(f"- {c.first_name or ''} {c.last_name or ''} | id={c.id} | username={getattr(c,'username',None)}")

async def show_groups(client):
    async for d in client.iter_dialogs(limit=200):
        if d.is_group or d.is_channel:
            ent = d.entity
            print(f"- {d.name} | id={d.id} | is_channel={d.is_channel} | is_group={d.is_group} | username={getattr(ent,'username',None)}")

async def auto_reply_enable(client):
    name = session_name_from_client(client)
    trigger = input("Триггер (часть текста): ").strip()
    reply = input("Текст автоответа: ").strip()
    state.setdefault(name, {})["auto_reply"] = True
    state[name]["trigger"] = trigger
    state[name]["reply"] = reply
    save_state()
    print(f"[{name}] AutoReply включён")

async def auto_reply_disable(client):
    name = session_name_from_client(client)
    state.setdefault(name, {})["auto_reply"] = False
    save_state()
    print(f"[{name}] AutoReply выключен")

async def auto_read_enable(client):
    name = session_name_from_client(client)
    state.setdefault(name, {})["auto_read"] = True
    save_state()
    print(f"[{name}] AutoRead включён")

async def auto_read_disable(client):
    name = session_name_from_client(client)
    state.setdefault(name, {})["auto_read"] = False
    save_state()
    print(f"[{name}] AutoRead выключен")

async def create_group(client):
    title = input("Название группы: ").strip()
    user = input("Добавить пользователя (username/ID): ").strip()
    group = await client.create_group(title, [await resolve_entity(client, user)])
    print(f"[OK] Группа создана: {getattr(group,'id',group)}")

async def create_channel(client):
    title = input("Название канала: ").strip()
    about = input("Описание: ").strip()
    await client(functions.channels.CreateChannelRequest(title=title, about=about, megagroup=False))
    print("[OK] Канал создан")

async def add_to_group(client):
    group = input("ID/username группы/канала: ").strip()
    user = input("username/ID пользователя: ").strip()
    grp = await resolve_entity(client, group)
    usr = await resolve_entity(client, user)
    await client(functions.channels.InviteToChannelRequest(grp, [usr]))
    print("[OK] Пользователь добавлен")

async def leave_group(client):
    chat = input("ID/username чата: ").strip()
    ent = await resolve_entity(client, chat)
    await client.delete_dialog(ent)
    print("[OK] Вышел из чата")

async def change_profile_photo(client):
    path = input("Путь к новому фото: ").strip()
    await client(functions.photos.UploadProfilePhotoRequest(file=await client.upload_file(path)))
    print("[OK] Фото профиля изменено")

async def change_name(client):
    first = input("Имя: ").strip()
    last = input("Фамилия (можно пусто): ").strip()
    await client(functions.account.UpdateProfileRequest(first_name=first or None, last_name=(last or None)))
    print("[OK] Имя изменено")

async def show_me(client):
    me = await client.get_me()
    print(f"ID={me.id} | username={getattr(me,'username',None)} | name={getattr(me,'first_name','')} {getattr(me,'last_name','')}")

async def unread_chats(client):
    async for d in client.iter_dialogs():
        if d.unread_count > 0:
            print(f"[{d.name}] непрочитанных: {d.unread_count}")

async def clear_history(client):
    chat = input("ID/username чата: ").strip()
    ent = await resolve_entity(client, chat)
    await client(functions.messages.DeleteHistoryRequest(peer=ent, max_id=0, revoke=True, just_clear=False))
    print("[OK] История удалена")

async def delete_message(client):
    chat = input("ID/username чата: ").strip()
    mid = int(input("ID сообщения: ").strip())
    await client.delete_messages(await resolve_entity(client, chat), [mid], revoke=True)
    print("[OK] Сообщение удалено")

async def mass_broadcast(client):
    text = input("Текст рассылки: ").strip()
    contacts = await client.get_contacts()
    for c in contacts:
        try:
            await client.send_message(c.id, text)
            print(f"[OK] -> {c.first_name or c.id}")
            await asyncio.sleep(0.5)
        except: pass
    print("[DONE] Рассылка завершена")

async def account_stats(client):
    today = datetime.date.today()
    sent_today = 0
    recv_today = 0
    async for d in client.iter_dialogs(limit=100):
        msgs = await client.get_messages(d.id, limit=200)
        for m in msgs:
            if getattr(m, "date", None) and m.date.date() == today:
                if getattr(m, "out", False): sent_today += 1
                else: recv_today += 1
    print(f"[Stats] Сегодня отправлено: {sent_today}, получено: {recv_today}")

async def scheduled_message(client):
    user = input("Кому отправить: ").strip()
    text = input("Текст: ").strip()
    delay = int(input("Через сколько секунд: ").strip() or 0)
    await asyncio.sleep(delay)
    await client.send_message(await resolve_entity(client, user), text)
    print("[OK] Отправлено по таймеру")

async def send_reaction(client):
    chat = input("ID/username чата: ").strip()
    mid = int(input("ID сообщения: ").strip())
    emoji = input("Реакция (например 👍): ").strip()
    peer = await resolve_entity(client, chat)
    await client(functions.messages.SendReactionRequest(
        peer=await client.get_input_entity(peer),
        msg_id=mid,
        reaction=types.ReactionEmoji(emoticon=emoji)
    ))
    print("[OK] Реакция отправлена")

async def logout_all_devices(client):
    confirm = input("Выйти СО ВСЕХ устройств? (y/n): ").strip().lower()
    if confirm == "y":
        await client(functions.auth.ResetAuthorizationsRequest())
        print("[OK] Вышел со всех устройств")

async def logout_current(client):
    confirm = input("Выйти с ТЕКУЩЕГО устройства? (y/n): ").strip().lower()
    if confirm == "y":
        await client.log_out()
        print("[OK] Вышел с текущего устройства")

async def session_info(client):
    name = session_name_from_client(client)
    started = meta.get(name, {}).get("started")
    login_time = meta.get(name, {}).get("login_time")
    me = await client.get_me()
    meta[name]["me"] = me
    print(f"--- Info for {name} ---")
    print(f"Session file: {getattr(client.session,'filename',None)}")
    print(f"Started at: {started} (uptime {human_delta(started)})")
    print(f"Login at: {login_time} (since login {human_delta(login_time)})")
    print(f"Account ID: {me.id}")
    print(f"Username: {getattr(me,'username',None)}")
    print(f"Name: {getattr(me,'first_name','')} {getattr(me,'last_name','')}")

# --------------------------
# Добавление аккаунта по номеру
# --------------------------
async def add_account_by_phone():
    phone = input("Введите номер телефона с +: ").strip()
    session_name = phone.replace("+", "")
    session_path = os.path.join(SESSION_DIR, session_name)
    client = TelegramClient(session_path, DEFAULT_API_ID, DEFAULT_API_HASH)
    await client.connect()
    if not await client.is_user_authorized():
        try:
            await client.send_code_request(phone)
            code = input(f"Код из Telegram/SMS для {phone}: ").strip()
            try:
                await client.sign_in(phone, code)
            except SessionPasswordNeededError:
                pwd = input("Требуется пароль 2FA. Введите пароль: ").strip()
                await client.sign_in(password=pwd)
        except Exception as e:
            print(f"[ERR] Вход: {e}")
            return None

    clients.append(client)
    state[session_name] = {"auto_reply": False, "trigger": "", "reply": "", "auto_read": False}
    meta[session_name] = {"started": datetime.datetime.now(), "login_time": datetime.datetime.now()}
    save_state()
    handler = make_handlers_for(client)
    client.add_event_handler(handler, events.NewMessage(incoming=True))
    asyncio.create_task(client.run_until_disconnected())
    print(f"[OK] Аккаунт {phone} добавлен и готов к использованию")
    return client

# --------------------------
# Меню в отдельном потоке
# --------------------------
def menu_thread(loop):
    def run_coro(coro): 
        return asyncio.run_coroutine_threadsafe(coro, loop).result()

    while True:
        print("\n=== Главное меню ===")
        print("1 - Показать аккаунты")
        print("2 - Добавить аккаунт по номеру")
        print("Q - Выход")
        choice = input("Выбор: ").strip().lower()

        if choice == "1":
            for idx, c in enumerate(clients, 1):
                print(f"{idx} - {session_name_from_client(c)}")
            sel = input("Выбери аккаунт по номеру или 0 для назад: ").strip()
            if sel == "0": continue
            if not sel.isdigit(): print("Неверный выбор"); continue
            i = int(sel) - 1
            if not (0 <= i < len(clients)): print("Неверный индекс"); continue
            client = clients[i]

            while True:
                print(f"\n--- Действия для {session_name_from_client(client)} ---")
                actions = [
                    ("Send Message", send_message),
                    ("Send Photo", send_photo),
                    ("Send Video", send_video),
                    ("Send File", send_file_doc),
                    ("Show Chats", show_chats),
                    ("Read Last Messages", read_last_messages),
                    ("Show Contacts", show_contacts),
                    ("Show Groups", show_groups),
                    ("AutoReply ON", auto_reply_enable),
                    ("AutoReply OFF", auto_reply_disable),
                    ("AutoRead ON", auto_read_enable),
                    ("AutoRead OFF", auto_read_disable),
                    ("Create Group", create_group),
                    ("Create Channel", create_channel),
                    ("Add to Group", add_to_group),
                    ("Leave Group", leave_group),
                    ("Change Profile Photo", change_profile_photo),
                    ("Change Name", change_name),
                    ("Show Me", show_me),
                    ("Unread Chats", unread_chats),
                    ("Clear History", clear_history),
                    ("Delete Message", delete_message),
                    ("Mass Broadcast", mass_broadcast),
                    ("Account Stats", account_stats),
                    ("Scheduled Message", scheduled_message),
                    ("Send Reaction", send_reaction),
                    ("Logout Current", logout_current),
                    ("Logout All", logout_all_devices),
                    ("Session Info", session_info)
                ]
                for idx, (label, _) in enumerate(actions, 1):
                    print(f"{idx} - {label}")
                print("0 - Back")
                a = input("Выбор: ").strip()
                if a == "0": break
                if not a.isdigit() or not (1 <= int(a) <= len(actions)):
                    print("[!] Неверный выбор"); continue
                fn = actions[int(a)-1][1]
                run_coro(fn(client))

        elif choice == "2":
            fut = asyncio.run_coroutine_threadsafe(add_account_by_phone(), loop)
            fut.result()

        elif choice == "q":
            print("Выход.")
            os._exit(0)
        else:
            print("[!] Неверный выбор")

# --------------------------
# Main
# --------------------------
async def main():
    load_state()
    loop = asyncio.get_running_loop()
    t = threading.Thread(target=menu_thread, args=(loop,), daemon=True)
    t.start()
    await asyncio.Event().wait()  # держим процесс живым

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Остановлено вручную")

