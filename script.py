# -*- coding: utf-8 -*-
import asyncio
import threading
import os
import datetime
from telethon import TelegramClient, events
from telethon.tl import functions, types
from telethon.tl.functions.channels import CreateChannelRequest, InviteToChannelRequest
from telethon.tl.functions.photos import UploadProfilePhotoRequest
from telethon.tl.functions.account import UpdateProfileRequest
from telethon.errors import SessionPasswordNeededError

# --------------------------
# Настройка аккаунтов
# --------------------------
accounts = [
    {"name": "account1", "api_id": 20111454, "api_hash": "e0040834c399df8ac420058eee0af322"},
]

SESSION_DIR = "sessions"
os.makedirs(SESSION_DIR, exist_ok=True)

# --------------------------
# Глобальные структуры
# --------------------------
clients = []   # список TelegramClient
state = {}     # { session_name: {"auto_reply":bool,"trigger":str,"reply":str,"auto_read":bool} }
meta = {}      # { session_name: {"started":datetime, "login_time":datetime, "me": user_obj} }

# --------------------------
# Утилиты
# --------------------------
def session_name_from_client(client: TelegramClient) -> str:
    try:
        return os.path.basename(client.session.filename)
    except Exception:
        return str(id(client))

async def resolve_entity(client: TelegramClient, peer_str: str):
    try:
        return await client.get_entity(peer_str)
    except Exception:
        try:
            return int(peer_str)
        except Exception:
            raise

async def ensure_started(client: TelegramClient, label: str):
    await client.connect()
    login_time = None
    if not await client.is_user_authorized():
        phone = input(f"[{label}] Номер телефона (с +): ").strip()
        try:
            await client.send_code_request(phone)
            code = input(f"[{label}] Код из Telegram/SMS: ").strip()
            try:
                await client.sign_in(phone=phone, code=code)
                login_time = datetime.datetime.now()
            except SessionPasswordNeededError:
                pwd = input(f"[{label}] Требуется пароль 2FA. Введите пароль: ").strip()
                await client.sign_in(password=pwd)
                login_time = datetime.datetime.now()
        except Exception:
            await client.start(phone=lambda: phone,
                               code_callback=lambda: input(f"[{label}] Код: ").strip(),
                               password=lambda: (input(f"[{label}] 2FA (если есть): ").strip() or None))
            login_time = datetime.datetime.now()
    else:
        login_time = datetime.datetime.now()
    return login_time

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
# Handler: автоответ + автопросмотр
# --------------------------
def make_handlers_for(client: TelegramClient):
    name = session_name_from_client(client)
    async def on_new_message(event):
        st = state.get(name, {})
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

        try:
            if st.get("auto_read") and event.is_private:
                mid = getattr(event.message, "id", None)
                if mid is not None:
                    peer = await client.get_input_entity(event.chat_id)
                    await client(functions.messages.ReadHistoryRequest(peer=peer, max_id=mid))
                    print(f"[{name}] AutoRead -> marked chat {event.chat_id} up to {mid}")
        except Exception as e:
            print(f"[{name}] AutoRead error: {e}")
    return on_new_message

# --------------------------
# Основные функции
# --------------------------
async def send_message(client):
    try:
        target = input("Введите username или ID получателя: ").strip()
        text = input("Введите текст сообщения: ").strip()
        await client.send_message(await resolve_entity(client, target), text)
        print("[OK] Сообщение отправлено")
    except Exception as e:
        print("[ERR] send_message:", e)

async def send_photo(client):
    try:
        target = input("Кому (username/ID): ").strip()
        path = input("Путь к фото: ").strip()
        await client.send_file(await resolve_entity(client, target), path)
        print("[OK] Фото отправлено")
    except Exception as e:
        print("[ERR] send_photo:", e)

async def send_video(client):
    try:
        target = input("Кому (username/ID): ").strip()
        path = input("Путь к видео: ").strip()
        await client.send_file(await resolve_entity(client, target), path)
        print("[OK] Видео отправлено")
    except Exception as e:
        print("[ERR] send_video:", e)

async def send_file_doc(client):
    try:
        target = input("Кому (username/ID): ").strip()
        path = input("Путь к файлу: ").strip()
        await client.send_file(await resolve_entity(client, target), path)
        print("[OK] Файл отправлен")
    except Exception as e:
        print("[ERR] send_file_doc:", e)

async def show_chats(client):
    try:
        async for d in client.iter_dialogs(limit=50):
            kind = "user"
            if d.is_group: kind = "group"
            if d.is_channel: kind = "channel"
            uname = getattr(d.entity, "username", None)
            print(f"- {d.name} | type={kind} | id={d.id} | username={uname} | unread={d.unread_count}")
    except Exception as e:
        print("[ERR] show_chats:", e)

async def read_last_messages(client):
    try:
        chat = input("ID или username чата: ").strip()
        ent = await resolve_entity(client, chat)
        lim = input("Сколько сообщений показать (Enter=10): ").strip()
        limit = int(lim) if lim.isdigit() else 10
        msgs = await client.get_messages(ent, limit=limit)
        for m in msgs:
            text = m.message or ""
            print(f"[{m.id}] from={m.sender_id} out={m.out} | {text}")
    except Exception as e:
        print("[ERR] read_last_messages:", e)

async def show_contacts(client):
    try:
        contacts = await client.get_contacts()
        for c in contacts:
            print(f"- {c.first_name or ''} {c.last_name or ''} | id={c.id} | username={getattr(c,'username',None)}")
    except Exception as e:
        print("[ERR] show_contacts:", e)

async def show_groups(client):
    try:
        async for d in client.iter_dialogs(limit=200):
            if d.is_group or d.is_channel:
                ent = d.entity
                print(f"- {d.name} | id={d.id} | is_channel={d.is_channel} | is_group={d.is_group} | username={getattr(ent,'username',None)}")
    except Exception as e:
        print("[ERR] show_groups:", e)

async def auto_reply_enable(client):
    name = session_name_from_client(client)
    trigger = input("Триггер (часть текста): ").strip()
    if not trigger:
        print("Триггер пуст. Отмена.")
        return
    reply = input("Текст автоответа: ").strip()
    confirm = input(f"Включить автоответ при '{trigger}' -> '{reply}'? (y/n): ").strip().lower()
    if confirm != "y":
        print("Отмена.")
        return
    state.setdefault(name, {})["auto_reply"] = True
    state[name]["trigger"] = trigger
    state[name]["reply"] = reply
    print(f"[{name}] AutoReply включён")

async def auto_reply_disable(client):
    name = session_name_from_client(client)
    state.setdefault(name, {})["auto_reply"] = False
    print(f"[{name}] AutoReply выключен")

async def auto_read_enable(client):
    name = session_name_from_client(client)
    state.setdefault(name, {})["auto_read"] = True
    print(f"[{name}] AutoRead включён (личные чаты)")

async def auto_read_disable(client):
    name = session_name_from_client(client)
    state.setdefault(name, {})["auto_read"] = False
    print(f"[{name}] AutoRead выключен")

async def create_group(client):
    try:
        title = input("Название группы: ").strip()
        user = input("Добавить пользователя (username/ID): ").strip()
        group = await client.create_group(title, [await resolve_entity(client, user)])
        print(f"[OK] Группа создана: {getattr(group,'id',group)}")
    except Exception as e:
        print("[ERR] create_group:", e)

async def create_channel(client):
    try:
        title = input("Название канала: ").strip()
        about = input("Описание: ").strip()
        await client(CreateChannelRequest(title=title, about=about, megagroup=False))
        print("[OK] Канал создан")
    except Exception as e:
        print("[ERR] create_channel:", e)

async def add_to_group(client):
    try:
        group = input("ID/username группы/канала: ").strip()
        user = input("username/ID пользователя: ").strip()
        grp = await resolve_entity(client, group)
        usr = await resolve_entity(client, user)
        await client(InviteToChannelRequest(grp, [usr]))
        print("[OK] Пользователь добавлен")
    except Exception as e:
        print("[ERR] add_to_group:", e)

async def leave_group(client):
    try:
        chat = input("ID/username чата: ").strip()
        ent = await resolve_entity(client, chat)
        await client.delete_dialog(ent)
        print("[OK] Вышел из чата")
    except Exception as e:
        print("[ERR] leave_group:", e)

async def change_profile_photo(client):
    try:
        path = input("Путь к новому фото: ").strip()
        await client(UploadProfilePhotoRequest(file=await client.upload_file(path)))
        print("[OK] Фото профиля изменено")
    except Exception as e:
        print("[ERR] change_profile_photo:", e)

async def change_name(client):
    try:
        first = input("Имя: ").strip()
        last = input("Фамилия (можно пусто): ").strip()
        await client(UpdateProfileRequest(first_name=first or None, last_name=(last or None)))
        print("[OK] Имя изменено")
    except Exception as e:
        print("[ERR] change_name:", e)

async def show_me(client):
    try:
        me = await client.get_me()
        print(f"ID={me.id} | username={getattr(me,'username',None)} | name={getattr(me,'first_name','')} {getattr(me,'last_name','')}")
    except Exception as e:
        print("[ERR] show_me:", e)

async def unread_chats(client):
    try:
        async for d in client.iter_dialogs():
            if d.unread_count > 0:
                print(f"[{d.name}] непрочитанных: {d.unread_count}")
    except Exception as e:
        print("[ERR] unread_chats:", e)

async def clear_history(client):
    try:
        chat = input("ID/username чата: ").strip()
        ent = await resolve_entity(client, chat)
        await client(functions.messages.DeleteHistoryRequest(peer=ent, max_id=0, revoke=True, just_clear=False))
        print("[OK] История удалена")
    except Exception as e:
        print("[ERR] clear_history:", e)

async def delete_message(client):
    try:
        chat = input("ID/username чата: ").strip()
        mid = int(input("ID сообщения: ").strip())
        await client.delete_messages(await resolve_entity(client, chat), [mid], revoke=True)
        print("[OK] Сообщение удалено")
    except Exception as e:
        print("[ERR] delete_message:", e)

async def mass_broadcast(client):
    try:
        text = input("Текст рассылки: ").strip()
        contacts = await client.get_contacts()
        for c in contacts:
            try:
                await client.send_message(c.id, text)
                print(f"[OK] -> {c.first_name or c.id}")
                await asyncio.sleep(0.5)
            except Exception as e:
                print(f"[ERR] {c.id}: {e}")
        print("[DONE] Рассылка завершена")
    except Exception as e:
        print("[ERR] mass_broadcast:", e)

async def account_stats(client):
    try:
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
    except Exception as e:
        print("[ERR] account_stats:", e)

async def scheduled_message(client):
    try:
        user = input("Кому отправить: ").strip()
        text = input("Текст: ").strip()
        delay = int(input("Через сколько секунд: ").strip() or 0)
        print(f"[Timer] Жду {delay} сек...")
        await asyncio.sleep(delay)
        await client.send_message(await resolve_entity(client, user), text)
        print("[OK] Отправлено по таймеру")
    except Exception as e:
        print("[ERR] scheduled_message:", e)

async def send_reaction(client):
    try:
        chat = input("ID/username чата: ").strip()
        mid = int(input("ID сообщения: ").strip())
        emoji = input("Реакция (например 👍): ").strip()
        if not emoji:
            print("Пустая реакция. Отмена.")
            return
        peer = await resolve_entity(client, chat)
        try:
            await client.send_reaction(peer, mid, emoji)
            print("[OK] Реакция отправлена (high-level)")
        except Exception:
            await client(functions.messages.SendReactionRequest(
                peer=await client.get_input_entity(peer),
                msg_id=mid,
                reaction=types.ReactionEmoji(emoticon=emoji)
            ))
            print("[OK] Реакция отправлена (RPC)")
    except Exception as e:
        print("[ERR] send_reaction:", e)

async def logout_all_devices(client):
    try:
        confirm = input("Выйти СО ВСЕХ устройств? (y/n): ").strip().lower()
        if confirm == "y":
            await client(functions.auth.ResetAuthorizationsRequest())
            print("[OK] Вышел со всех устройств")
        else:
            print("Отмена")
    except Exception as e:
        print("[ERR] logout_all_devices:", e)

async def logout_current(client):
    try:
        confirm = input("Выйти с ТЕКУЩЕГО устройства? (y/n): ").strip().lower()
        if confirm == "y":
            await client.log_out()
            print("[OK] Вышел с текущего устройства")
        else:
            print("Отмена")
    except Exception as e:
        print("[ERR] logout_current:", e)

async def session_info(client):
    try:
        name = session_name_from_client(client)
        started = meta.get(name, {}).get("started")
        login_time = meta.get(name, {}).get("login_time")
        me = await client.get_me()
        meta[name]["me"] = me
        print(f"--- Info for {name} ---")
        print(f"Session file: {getattr(client.session,'filename',None)}")
        print(f"Started at: {started} (uptime {human_delta(started)})")
        print(f"Login via session at: {login_time} (since login {human_delta(login_time)})")
        print(f"Account ID: {me.id}")
        print(f"Username: {getattr(me,'username',None)}")
        print(f"Name: {getattr(me,'first_name','')} {getattr(me,'last_name','')}")
    except Exception as e:
        print("[ERR] session_info:", e)

# --------------------------
# Регистрация и запуск клиента
# --------------------------
async def start_and_register_client(name, api_id, api_hash):
    session_path = os.path.join(SESSION_DIR, name)
    client = TelegramClient(session_path, api_id, api_hash)
    login_time = await ensure_started(client, name)
    sn = session_name_from_client(client)
    meta.setdefault(sn, {})["started"] = datetime.datetime.now()
    meta[sn]["login_time"] = login_time
    handler = make_handlers_for(client)
    client.add_event_handler(handler, events.NewMessage(incoming=True))
    asyncio.create_task(client.run_until_disconnected())
    clients.append(client)
    state[sn] = {"auto_reply": False, "trigger": "", "reply": "", "auto_read": False}
    print(f"[+] {sn} started; login_time={login_time}")

# --------------------------
# Меню в отдельном потоке
# --------------------------
def menu_thread(loop):
    def run_coro(coro): 
        return asyncio.run_coroutine_threadsafe(coro, loop).result()  # Ждем завершения функции

    while True:
        print("\n=== Главное меню ===")
        print("1 - Показать аккаунты")
        print("2 - Добавить аккаунт")
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
                run_coro(fn(client))  # ждем выполнения функции полностью

        elif choice == "2":
            try:
                name = input("Имя сессии: ").strip()
                api_id = int(input("API_ID: ").strip())
                api_hash = input("API_HASH: ").strip()
                fut = asyncio.run_coroutine_threadsafe(start_and_register_client(name, api_id, api_hash), loop)
                fut.result()  # ждем пока добавится
                print("[OK] Аккаунт добавлен")
            except Exception as e:
                print("[ERR add account]:", e)

        elif choice == "q":
            print("Выход.")
            os._exit(0)
        else:
            print("[!] Неверный выбор")

# --------------------------
# Main
# --------------------------
async def main():
    for acc in accounts:
        await start_and_register_client(acc["name"], acc["api_id"], acc["api_hash"])

    loop = asyncio.get_running_loop()
    t = threading.Thread(target=menu_thread, args=(loop,), daemon=True)
    t.start()

    await asyncio.Event().wait()  # держим процесс живым

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Остановлено вручную")

