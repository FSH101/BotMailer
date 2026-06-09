import asyncio
import html
import json
import logging
import mimetypes
import os
import re
import smtplib
import tempfile
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("telegram-drive-link-bot")

APP_NAME = "Telegram Drive Group Link Bot"
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
TELEGRAM_SECRET = os.getenv("TELEGRAM_SECRET", "tg-secret").strip()
PUBLIC_URL = os.getenv("PUBLIC_URL", "").strip().rstrip("/")
ADMIN_IDS = {int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().lstrip('-').isdigit()}
ALLOWED_CHAT_IDS = {int(x.strip()) for x in os.getenv("ALLOWED_CHAT_IDS", "").split(",") if x.strip().lstrip('-').isdigit()}
ALLOW_GROUP_USERS = os.getenv("ALLOW_GROUP_USERS", "true").strip().lower() in {"1", "true", "yes", "on"}
SEND_ACK_IN_GROUP = os.getenv("SEND_ACK_IN_GROUP", "false").strip().lower() in {"1", "true", "yes", "on"}

TIMEZONE = os.getenv("TIMEZONE", "Europe/Moscow")
MAX_TELEGRAM_DOWNLOAD_MB = int(os.getenv("MAX_TELEGRAM_DOWNLOAD_MB", "20"))
MAX_TELEGRAM_DOWNLOAD_BYTES = MAX_TELEGRAM_DOWNLOAD_MB * 1024 * 1024

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "").strip()
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "").strip()
GOOGLE_REFRESH_TOKEN = os.getenv("GOOGLE_REFRESH_TOKEN", "").strip()
GOOGLE_ROOT_FOLDER_ID = os.getenv("GOOGLE_ROOT_FOLDER_ID", "").strip()
GOOGLE_ROOT_FOLDER_NAME = os.getenv("GOOGLE_ROOT_FOLDER_NAME", "Telegram Documents").strip()
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "").strip()
GOOGLE_SHEET_TAB = os.getenv("GOOGLE_SHEET_TAB", "Реестр").strip()
DRIVE_LINK_ACCESS = os.getenv("DRIVE_LINK_ACCESS", "private").strip().lower()  # private | anyone_with_link

PANEL_LOGIN = os.getenv("PANEL_LOGIN", "admin")
PANEL_PASSWORD = os.getenv("PANEL_PASSWORD", "admin")

EMAIL_ENABLED = os.getenv("EMAIL_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
AUTO_EMAIL_ON_UPLOAD = os.getenv("AUTO_EMAIL_ON_UPLOAD", "false").strip().lower() in {"1", "true", "yes", "on"}
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "").strip()
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "").strip()
SMTP_FROM = os.getenv("SMTP_FROM", SMTP_USER).strip()
EMAIL_TO = os.getenv("EMAIL_TO", "").strip()

SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
]
HEADERS = [
    "Дата",
    "Время",
    "Отправитель",
    "Telegram ID",
    "Chat ID",
    "Чат",
    "Тип",
    "Имя файла",
    "Размер, байт",
    "Комментарий",
    "Ссылка на файл",
    "Ссылка на папку дня",
    "Drive File ID",
    "Drive Folder ID",
    "Статус",
    "Ошибка",
]

app = FastAPI(title=APP_NAME)
security = HTTPBasic()
_google_services_cache: Optional[Tuple[Any, Any]] = None
_root_folder_cache: Optional[str] = None
_sheet_ready = False


def now_local() -> datetime:
    return datetime.now(ZoneInfo(TIMEZONE))


def tg_api_url(method: str) -> str:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is empty")
    return f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"


def safe_filename(name: str) -> str:
    name = (name or "file").strip().replace("\x00", "")
    name = re.sub(r"[\\/:*?\"<>|]+", "_", name)
    name = re.sub(r"\s+", " ", name).strip()
    if not name:
        name = "file"
    return name[:180]


def escape_drive_query_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def google_services() -> Tuple[Any, Any]:
    global _google_services_cache
    if _google_services_cache:
        return _google_services_cache
    missing = [
        name for name, value in {
            "GOOGLE_CLIENT_ID": GOOGLE_CLIENT_ID,
            "GOOGLE_CLIENT_SECRET": GOOGLE_CLIENT_SECRET,
            "GOOGLE_REFRESH_TOKEN": GOOGLE_REFRESH_TOKEN,
            "GOOGLE_SHEET_ID": GOOGLE_SHEET_ID,
        }.items() if not value
    ]
    if missing:
        raise RuntimeError("Missing env vars: " + ", ".join(missing))
    creds = Credentials(
        token=None,
        refresh_token=GOOGLE_REFRESH_TOKEN,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        scopes=SCOPES,
    )
    drive = build("drive", "v3", credentials=creds, cache_discovery=False)
    sheets = build("sheets", "v4", credentials=creds, cache_discovery=False)
    _google_services_cache = (drive, sheets)
    return _google_services_cache


def ensure_permission_anyone(drive: Any, file_id: str) -> None:
    if DRIVE_LINK_ACCESS != "anyone_with_link":
        return
    try:
        drive.permissions().create(
            fileId=file_id,
            body={"type": "anyone", "role": "reader"},
            fields="id",
            supportsAllDrives=True,
        ).execute()
    except HttpError as exc:
        # If permission already exists or domain policy blocks public sharing, keep the upload working.
        log.warning("Could not set public permission for %s: %s", file_id, exc)


def find_folder(drive: Any, name: str, parent_id: Optional[str] = None) -> Optional[str]:
    name_q = escape_drive_query_value(name)
    q = f"mimeType='application/vnd.google-apps.folder' and trashed=false and name='{name_q}'"
    if parent_id:
        q += f" and '{parent_id}' in parents"
    result = drive.files().list(
        q=q,
        spaces="drive",
        fields="files(id, name)",
        pageSize=10,
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    ).execute()
    files = result.get("files", [])
    return files[0]["id"] if files else None


def create_folder(drive: Any, name: str, parent_id: Optional[str] = None) -> str:
    metadata: Dict[str, Any] = {"name": name, "mimeType": "application/vnd.google-apps.folder"}
    if parent_id:
        metadata["parents"] = [parent_id]
    folder = drive.files().create(body=metadata, fields="id", supportsAllDrives=True).execute()
    folder_id = folder["id"]
    ensure_permission_anyone(drive, folder_id)
    return folder_id


def root_folder_id(drive: Any) -> str:
    global _root_folder_cache
    if GOOGLE_ROOT_FOLDER_ID:
        return GOOGLE_ROOT_FOLDER_ID
    if _root_folder_cache:
        return _root_folder_cache
    found = find_folder(drive, GOOGLE_ROOT_FOLDER_NAME)
    if found:
        _root_folder_cache = found
    else:
        _root_folder_cache = create_folder(drive, GOOGLE_ROOT_FOLDER_NAME)
    ensure_permission_anyone(drive, _root_folder_cache)
    return _root_folder_cache


def ensure_day_folder(drive: Any, day: str) -> str:
    root_id = root_folder_id(drive)
    found = find_folder(drive, day, root_id)
    if found:
        ensure_permission_anyone(drive, found)
        return found
    return create_folder(drive, day, root_id)


def folder_link(folder_id: str) -> str:
    return f"https://drive.google.com/drive/folders/{folder_id}"


def file_link(file_id: str) -> str:
    return f"https://drive.google.com/file/d/{file_id}/view"


def ensure_sheet_ready() -> None:
    global _sheet_ready
    if _sheet_ready:
        return
    _, sheets = google_services()
    spreadsheet = sheets.spreadsheets().get(spreadsheetId=GOOGLE_SHEET_ID).execute()
    existing_tabs = {s["properties"]["title"] for s in spreadsheet.get("sheets", [])}
    if GOOGLE_SHEET_TAB not in existing_tabs:
        sheets.spreadsheets().batchUpdate(
            spreadsheetId=GOOGLE_SHEET_ID,
            body={"requests": [{"addSheet": {"properties": {"title": GOOGLE_SHEET_TAB}}}]},
        ).execute()
    first_row = sheets.spreadsheets().values().get(
        spreadsheetId=GOOGLE_SHEET_ID,
        range=f"'{GOOGLE_SHEET_TAB}'!A1:P1",
    ).execute().get("values", [])
    if not first_row:
        sheets.spreadsheets().values().update(
            spreadsheetId=GOOGLE_SHEET_ID,
            range=f"'{GOOGLE_SHEET_TAB}'!A1:P1",
            valueInputOption="USER_ENTERED",
            body={"values": [HEADERS]},
        ).execute()
    _sheet_ready = True


def append_registry_row(row: List[Any]) -> None:
    ensure_sheet_ready()
    _, sheets = google_services()
    sheets.spreadsheets().values().append(
        spreadsheetId=GOOGLE_SHEET_ID,
        range=f"'{GOOGLE_SHEET_TAB}'!A:P",
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": [row]},
    ).execute()


def read_registry_rows(limit: int = 300) -> Tuple[List[str], List[List[str]]]:
    ensure_sheet_ready()
    _, sheets = google_services()
    values = sheets.spreadsheets().values().get(
        spreadsheetId=GOOGLE_SHEET_ID,
        range=f"'{GOOGLE_SHEET_TAB}'!A:P",
    ).execute().get("values", [])
    if not values:
        return HEADERS, []
    headers = values[0]
    rows = values[1:]
    return headers, rows[-limit:]


async def telegram_request(method: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(tg_api_url(method), json=payload)
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram error {method}: {data}")
        return data


async def send_message(chat_id: int, text: str, disable_web_page_preview: bool = False) -> None:
    try:
        await telegram_request("sendMessage", {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": disable_web_page_preview,
        })
    except Exception:
        log.exception("Could not send Telegram message")


async def download_telegram_file(file_id: str, target: Path) -> None:
    file_info = await telegram_request("getFile", {"file_id": file_id})
    file_path = file_info["result"]["file_path"]
    url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
    async with httpx.AsyncClient(timeout=120) as client:
        async with client.stream("GET", url) as resp:
            resp.raise_for_status()
            with target.open("wb") as f:
                async for chunk in resp.aiter_bytes():
                    f.write(chunk)


def upload_to_drive(local_path: Path, drive_name: str, day_folder_id: str, mime_type: Optional[str]) -> Tuple[str, str]:
    drive, _ = google_services()
    media = MediaFileUpload(str(local_path), mimetype=mime_type or "application/octet-stream", resumable=True)
    metadata = {"name": drive_name, "parents": [day_folder_id]}
    created = drive.files().create(
        body=metadata,
        media_body=media,
        fields="id, webViewLink",
        supportsAllDrives=True,
    ).execute()
    file_id = created["id"]
    ensure_permission_anyone(drive, file_id)
    return file_id, created.get("webViewLink") or file_link(file_id)


def send_email(subject: str, body: str, to_addr: Optional[str] = None) -> None:
    if not EMAIL_ENABLED:
        raise RuntimeError("EMAIL_ENABLED=false")
    missing = [name for name, value in {
        "SMTP_USER": SMTP_USER,
        "SMTP_PASSWORD": SMTP_PASSWORD,
        "SMTP_FROM": SMTP_FROM,
        "EMAIL_TO": EMAIL_TO,
    }.items() if not value]
    if missing:
        raise RuntimeError("Email settings missing: " + ", ".join(missing))
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = SMTP_FROM
    msg["To"] = to_addr or EMAIL_TO
    msg.set_content(body)
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=60) as smtp:
        smtp.starttls()
        smtp.login(SMTP_USER, SMTP_PASSWORD)
        smtp.send_message(msg)


def get_sender_name(message: Dict[str, Any]) -> str:
    user = message.get("from") or {}
    parts = [user.get("first_name"), user.get("last_name")]
    full = " ".join([p for p in parts if p])
    username = user.get("username")
    if username:
        return f"{full} (@{username})" if full else f"@{username}"
    return full or "Неизвестно"


def chat_title(message: Dict[str, Any]) -> str:
    chat = message.get("chat") or {}
    return chat.get("title") or chat.get("username") or chat.get("first_name") or str(chat.get("id", ""))


def is_private_chat(message: Dict[str, Any]) -> bool:
    return (message.get("chat") or {}).get("type") == "private"


def is_allowed(message: Dict[str, Any], *, command: bool = False) -> bool:
    user_id = (message.get("from") or {}).get("id")
    chat_id = (message.get("chat") or {}).get("id")
    private = is_private_chat(message)

    # If ALLOWED_CHAT_IDS is set, accept files/commands only from these chats,
    # except private admin commands.
    if ALLOWED_CHAT_IDS and chat_id not in ALLOWED_CHAT_IDS:
        if not (private and user_id in ADMIN_IDS):
            return False

    # Commands that manage bot/panel/email stay admin-only when ADMIN_IDS is configured.
    if command and ADMIN_IDS:
        return user_id in ADMIN_IDS

    # In groups we usually need to accept documents from all participants of an allowed chat.
    if not private and ALLOW_GROUP_USERS:
        return True

    if not ADMIN_IDS:
        return True
    return user_id in ADMIN_IDS


def extract_incoming_file(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if "document" in message:
        d = message["document"]
        return {
            "kind": "document",
            "file_id": d["file_id"],
            "file_name": d.get("file_name") or "document",
            "file_size": d.get("file_size", 0),
            "mime_type": d.get("mime_type"),
        }
    if "photo" in message:
        photos = message.get("photo") or []
        if photos:
            p = photos[-1]
            return {
                "kind": "photo",
                "file_id": p["file_id"],
                "file_name": f"photo_{message.get('message_id', '0')}.jpg",
                "file_size": p.get("file_size", 0),
                "mime_type": "image/jpeg",
            }
    if "video" in message:
        v = message["video"]
        return {
            "kind": "video",
            "file_id": v["file_id"],
            "file_name": v.get("file_name") or f"video_{message.get('message_id', '0')}.mp4",
            "file_size": v.get("file_size", 0),
            "mime_type": v.get("mime_type") or "video/mp4",
        }
    if "audio" in message:
        a = message["audio"]
        return {
            "kind": "audio",
            "file_id": a["file_id"],
            "file_name": a.get("file_name") or f"audio_{message.get('message_id', '0')}.mp3",
            "file_size": a.get("file_size", 0),
            "mime_type": a.get("mime_type") or "audio/mpeg",
        }
    if "voice" in message:
        v = message["voice"]
        return {
            "kind": "voice",
            "file_id": v["file_id"],
            "file_name": f"voice_{message.get('message_id', '0')}.ogg",
            "file_size": v.get("file_size", 0),
            "mime_type": v.get("mime_type") or "audio/ogg",
        }
    if "animation" in message:
        a = message["animation"]
        return {
            "kind": "animation",
            "file_id": a["file_id"],
            "file_name": a.get("file_name") or f"animation_{message.get('message_id', '0')}.mp4",
            "file_size": a.get("file_size", 0),
            "mime_type": a.get("mime_type") or "video/mp4",
        }
    return None

async def handle_command(message: Dict[str, Any]) -> bool:
    text = (message.get("text") or "").strip()
    if not text.startswith("/"):
        return False
    chat_id = message["chat"]["id"]
    user_id = (message.get("from") or {}).get("id")
    cmd = text.split()[0].split("@", 1)[0].lower()

    if cmd == "/whoami":
        await send_message(chat_id, f"Ваш Telegram ID: {user_id}")
        return True

    if not is_allowed(message, command=True):
        await send_message(chat_id, "Нет доступа. Отправьте /whoami администратору.")
        return True

    if cmd in {"/start", "/help"}:
        await send_message(chat_id,
            "Бот принимает документы/фото/медиа до 20 МБ из лички или разрешённой группы, загружает их в Google Drive и ведёт реестр в Google Sheets.\n\n"
            "Команды:\n"
            "/whoami — узнать свой Telegram ID\n"
            "/folder today — ссылка на папку за сегодня\n"
            "/email today — отправить ссылку на папку дня на email\n"
            "/panel — ссылка на панель\n"
        )
        return True

    if cmd == "/panel":
        if PUBLIC_URL:
            await send_message(chat_id, f"Панель: {PUBLIC_URL}/panel")
        else:
            await send_message(chat_id, "PUBLIC_URL не задан в переменных Render.")
        return True

    if cmd == "/folder":
        drive, _ = google_services()
        args = text.split(maxsplit=1)
        target = args[1].strip() if len(args) > 1 else "today"
        day = now_local().strftime("%d.%m.%Y") if target.lower() in {"today", "сегодня"} else target
        day_folder = ensure_day_folder(drive, day)
        await send_message(chat_id, f"Папка за {day}:\n{folder_link(day_folder)}", disable_web_page_preview=True)
        return True

    if cmd == "/email":
        try:
            drive, _ = google_services()
            args = text.split(maxsplit=1)
            target = args[1].strip() if len(args) > 1 else "today"
            day = now_local().strftime("%d.%m.%Y") if target.lower() in {"today", "сегодня"} else target
            day_folder = ensure_day_folder(drive, day)
            link = folder_link(day_folder)
            send_email(
                subject=f"Ссылка на документы за {day}",
                body=f"Документы за {day}:\n{link}\n\nРеестр: https://docs.google.com/spreadsheets/d/{GOOGLE_SHEET_ID}/edit",
            )
            await send_message(chat_id, f"Письмо отправлено: {EMAIL_TO}\nПапка: {link}", disable_web_page_preview=True)
        except Exception as exc:
            log.exception("Email command failed")
            await send_message(chat_id, f"Ошибка отправки email: {exc}")
        return True

    return False


async def process_file_message(message: Dict[str, Any]) -> None:
    chat_id = message["chat"]["id"]
    user = message.get("from") or {}
    user_id = user.get("id")
    if not is_allowed(message):
        # В группах молча игнорируем чужие/неразрешённые чаты, чтобы не спамить.
        if is_private_chat(message):
            await send_message(chat_id, "Нет доступа. Отправьте /whoami администратору.")
        return

    incoming = extract_incoming_file(message)
    if not incoming:
        # В группе privacy отключается ради чтения всех сообщений, поэтому обычный текст игнорируем молча.
        if is_private_chat(message):
            await send_message(chat_id, "Пришлите документ, фото или медиафайл. Текст без файла я пока не учитываю.")
        return

    dt = now_local()
    day = dt.strftime("%d.%m.%Y")
    time_s = dt.strftime("%H:%M:%S")
    sender = get_sender_name(message)
    chat_name = chat_title(message)
    comment = message.get("caption") or ""
    original_name = safe_filename(incoming["file_name"])
    file_size = int(incoming.get("file_size") or 0)
    kind = incoming["kind"]

    drive, _ = google_services()
    day_folder_id = ensure_day_folder(drive, day)
    day_folder_link = folder_link(day_folder_id)

    if file_size > MAX_TELEGRAM_DOWNLOAD_BYTES:
        error = f"Файл больше {MAX_TELEGRAM_DOWNLOAD_MB} МБ: {file_size} байт"
        row = [day, time_s, sender, user_id, chat_id, chat_name, kind, original_name, file_size, comment, "", day_folder_link, "", day_folder_id, "ERROR_TOO_LARGE", error]
        try:
            append_registry_row(row)
        except Exception:
            log.exception("Could not append error row")
        await send_message(chat_id, f"Ошибка: файл больше {MAX_TELEGRAM_DOWNLOAD_MB} МБ. Не сохраняю.\nИмя: {original_name}")
        return

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir) / original_name
        should_ack = SEND_ACK_IN_GROUP or is_private_chat(message)
        if should_ack:
            await send_message(chat_id, f"Принял: {original_name}\nЗагружаю в Google Drive...")
        try:
            await download_telegram_file(incoming["file_id"], tmp_path)
            # Extra safety after download
            actual_size = tmp_path.stat().st_size
            if actual_size > MAX_TELEGRAM_DOWNLOAD_BYTES:
                raise ValueError(f"Файл после скачивания больше лимита: {actual_size} байт")

            prefix = dt.strftime("%H%M%S") + f"_{message.get('message_id', '0')}_"
            drive_name = safe_filename(prefix + original_name)
            mime_type = incoming.get("mime_type") or mimetypes.guess_type(original_name)[0]
            file_id, web_link = upload_to_drive(tmp_path, drive_name, day_folder_id, mime_type)
            row = [day, time_s, sender, user_id, chat_id, chat_name, kind, original_name, actual_size, comment, web_link, day_folder_link, file_id, day_folder_id, "OK", ""]
            append_registry_row(row)

            if EMAIL_ENABLED and AUTO_EMAIL_ON_UPLOAD:
                try:
                    send_email(
                        subject=f"Новый документ: {original_name}",
                        body=(
                            f"Получен новый документ.\n\n"
                            f"Дата: {day} {time_s}\n"
                            f"Отправитель: {sender}\n"
                            f"Имя файла: {original_name}\n"
                            f"Размер: {actual_size} байт\n"
                            f"Комментарий: {comment}\n\n"
                            f"Файл: {web_link}\n"
                            f"Папка дня: {day_folder_link}\n"
                        ),
                    )
                except Exception:
                    log.exception("Auto email failed")

            if should_ack:
                await send_message(chat_id, f"Готово.\nФайл: {web_link}\nПапка дня: {day_folder_link}", disable_web_page_preview=True)
        except Exception as exc:
            log.exception("File processing failed")
            row = [day, time_s, sender, user_id, chat_id, chat_name, kind, original_name, file_size, comment, "", day_folder_link, "", day_folder_id, "ERROR", str(exc)]
            try:
                append_registry_row(row)
            except Exception:
                log.exception("Could not append failure row")
            await send_message(chat_id, f"Ошибка обработки файла: {exc}")


@app.on_event("startup")
async def on_startup() -> None:
    if PUBLIC_URL and BOT_TOKEN:
        webhook_url = f"{PUBLIC_URL}/telegram/{TELEGRAM_SECRET}"
        try:
            await telegram_request("setWebhook", {"url": webhook_url, "drop_pending_updates": False})
            log.info("Telegram webhook set: %s", webhook_url)
        except Exception:
            log.exception("Could not set Telegram webhook")


@app.get("/health")
async def health() -> Dict[str, str]:
    return {"status": "ok"}


@app.get("/ping")
async def ping() -> PlainTextResponse:
    return PlainTextResponse("pong")


@app.post("/telegram/{secret}")
async def telegram_webhook(secret: str, request: Request) -> JSONResponse:
    if secret != TELEGRAM_SECRET:
        raise HTTPException(status_code=404)
    update = await request.json()
    message = update.get("message") or update.get("edited_message")
    if not message:
        return JSONResponse({"ok": True})
    try:
        if await handle_command(message):
            return JSONResponse({"ok": True})
        await process_file_message(message)
    except Exception:
        log.exception("Unhandled update: %s", json.dumps(update, ensure_ascii=False)[:2000])
        chat_id = (message.get("chat") or {}).get("id")
        if chat_id:
            await send_message(chat_id, "Внутренняя ошибка. Смотри логи Render.")
    return JSONResponse({"ok": True})


def check_panel_auth(credentials: HTTPBasicCredentials = Depends(security)) -> str:
    if credentials.username == PANEL_LOGIN and credentials.password == PANEL_PASSWORD:
        return credentials.username
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Unauthorized",
        headers={"WWW-Authenticate": "Basic"},
    )


@app.get("/", response_class=HTMLResponse)
async def index(_: str = Depends(check_panel_auth)) -> HTMLResponse:
    return await panel(_)


@app.get("/panel", response_class=HTMLResponse)
async def panel(_: str = Depends(check_panel_auth)) -> HTMLResponse:
    try:
        headers, rows = read_registry_rows(limit=500)
    except Exception as exc:
        safe_exc = html.escape(str(exc))
        return HTMLResponse(f"<h1>Ошибка Google Sheets</h1><pre>{safe_exc}</pre>", status_code=500)

    def cell(value: Any) -> str:
        s = html.escape(str(value))
        if isinstance(value, str) and value.startswith("http"):
            return f'<a href="{html.escape(value)}" target="_blank">открыть</a>'
        return s

    head_html = "".join(f"<th>{html.escape(str(h))}</th>" for h in headers)
    body_html = ""
    for row in reversed(rows):
        padded = row + [""] * (len(headers) - len(row))
        body_html += "<tr>" + "".join(f"<td>{cell(v)}</td>" for v in padded[:len(headers)]) + "</tr>"

    root_link = ""
    try:
        drive, _ = google_services()
        root_link = folder_link(root_folder_id(drive))
    except Exception:
        root_link = ""

    sheet_link = f"https://docs.google.com/spreadsheets/d/{GOOGLE_SHEET_ID}/edit" if GOOGLE_SHEET_ID else ""
    html_page = f"""
<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{APP_NAME}</title>
<style>
:root {{ color-scheme: dark; }}
body {{ margin:0; font-family: Arial, sans-serif; background:#111; color:#eee; }}
header {{ padding:18px 22px; background:#181818; border-bottom:1px solid #333; position:sticky; top:0; z-index:2; }}
h1 {{ margin:0 0 8px 0; font-size:22px; }}
a {{ color:#9ecbff; }}
.wrap {{ padding:18px 22px; }}
.cards {{ display:flex; gap:12px; flex-wrap:wrap; margin-bottom:16px; }}
.card {{ background:#1b1b1b; border:1px solid #333; border-radius:12px; padding:12px 14px; }}
.tablebox {{ overflow:auto; border:1px solid #333; border-radius:12px; max-height:72vh; }}
table {{ border-collapse:collapse; width:100%; min-width:1400px; }}
th, td {{ border-bottom:1px solid #2b2b2b; padding:8px 10px; text-align:left; font-size:13px; vertical-align:top; }}
th {{ background:#202020; position:sticky; top:0; z-index:1; }}
tr:hover {{ background:#1a2430; }}
.small {{ color:#aaa; font-size:13px; }}
</style>
</head>
<body>
<header>
<h1>Панель учёта документов</h1>
<div class="small">Данные берутся из Google Sheets. Файлы хранятся в Google Drive.</div>
</header>
<div class="wrap">
<div class="cards">
  <div class="card">Записей показано: <b>{len(rows)}</b></div>
  <div class="card">Google Sheet: {'<a target="_blank" href="'+html.escape(sheet_link)+'">открыть</a>' if sheet_link else 'не задан'}</div>
  <div class="card">Папка Drive: {'<a target="_blank" href="'+html.escape(root_link)+'">открыть</a>' if root_link else 'не задана'}</div>
</div>
<div class="tablebox"><table><thead><tr>{head_html}</tr></thead><tbody>{body_html}</tbody></table></div>
</div>
</body>
</html>
"""
    return HTMLResponse(html_page)
