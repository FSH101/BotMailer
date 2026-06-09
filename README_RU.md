# Telegram Drive Group Link Bot

Тестовый вариант: Telegram-бот читает файлы из лички или группы, принимает документы/фото/медиа до 20 МБ, загружает их в Google Drive, ведёт реестр в Google Sheets и показывает простую панель на Render.

## Что делает

- Принимает документы, фото, видео, аудио и голосовые из Telegram.
- Может работать в группе: читать общий чат и вытаскивать оттуда файлы.
- Если файл больше 20 МБ — не скачивает, пишет ошибку и фиксирует её в реестре.
- Создаёт папки на Google Drive по датам: `09.06.2026`, `10.06.2026` и т.д.
- Загружает файлы в папку дня.
- Записывает строку в Google Sheets: дата, время, отправитель, имя файла, размер, комментарий, ссылка на файл, ссылка на папку.
- Веб-панель `/panel` читает Google Sheets и показывает реестр.
- По команде `/folder today` выдаёт ссылку на папку дня.
- По команде `/email today` отправляет на почту ссылку на папку дня, если включён SMTP.

## Команды бота

```text
/start
/help
/whoami
/folder today
/email today
/panel
```

## Важная логика

Render Free используется только как обработчик. Файлы и учёт хранятся не на Render:

```text
Telegram → Render webhook → Google Drive → Google Sheets → панель Render
```

Локальная папка `/tmp` используется только временно во время загрузки файла.

## Переменные окружения Render

Минимально обязательные:

```env
BOT_TOKEN=токен_бота
TELEGRAM_SECRET=любая_длинная_строка
PUBLIC_URL=https://your-service.onrender.com
ADMIN_IDS=твой_telegram_id
# можно оставить пустым на время настройки, но лучше потом заполнить
ALLOWED_CHAT_IDS=
ALLOW_GROUP_USERS=true
SEND_ACK_IN_GROUP=false

GOOGLE_CLIENT_ID=...
GOOGLE_CLIENT_SECRET=...
GOOGLE_REFRESH_TOKEN=...
GOOGLE_SHEET_ID=...

PANEL_LOGIN=admin
PANEL_PASSWORD=сложный_пароль
```

Дополнительно:

```env
TIMEZONE=Europe/Moscow
MAX_TELEGRAM_DOWNLOAD_MB=20
GOOGLE_ROOT_FOLDER_NAME=Telegram Documents
GOOGLE_ROOT_FOLDER_ID=
GOOGLE_SHEET_TAB=Реестр
DRIVE_LINK_ACCESS=private
ALLOWED_CHAT_IDS=
ALLOW_GROUP_USERS=true
SEND_ACK_IN_GROUP=false
```

`DRIVE_LINK_ACCESS`:

```text
private — ссылки работают только для аккаунтов, у которых есть доступ к Drive.
anyone_with_link — бот делает файлы/папки доступными всем, у кого есть ссылка.
```

Для теста удобнее `anyone_with_link`, но для реальных документов безопаснее `private`.

## Email-настройки

Если нужно отправлять ссылку на папку по команде `/email today`:

```env
EMAIL_ENABLED=true
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=your_gmail@gmail.com
SMTP_PASSWORD=пароль_приложения_google
SMTP_FROM=your_gmail@gmail.com
EMAIL_TO=recipient@gmail.com
AUTO_EMAIL_ON_UPLOAD=false
```

`AUTO_EMAIL_ON_UPLOAD=true` будет отправлять письмо после каждого загруженного файла. Обычно это неудобно, лучше оставить `false` и отправлять ссылку по команде.


## Работа в группе Telegram

Чтобы бот видел все файлы в группе, одного добавления в чат недостаточно. Нужно сделать одно из двух:

### Вариант 1 — сделать бота администратором группы

Это самый простой вариант. Telegram пишет, что боты-администраторы получают все сообщения в группе.

Права на удаление/бан/изменение группы ему не нужны. Достаточно добавить его админом без опасных прав.

### Вариант 2 — отключить Privacy Mode у бота

Через BotFather:

```text
/mybots
выбрать бота
Bot Settings
Group Privacy
Turn off
```

После отключения privacy mode бота лучше удалить из группы и добавить заново, чтобы настройка точно применилась.

### Безопаснее ограничить конкретную группу

После добавления бота в группу напиши в этой группе:

```text
/whoami
```

В Render logs будет виден `chat_id`, либо можно временно посмотреть его в панели/реестре после первого файла. Потом вставь его в Render:

```env
ALLOWED_CHAT_IDS=-1001234567890
```

Если оставить `ALLOWED_CHAT_IDS` пустым, бот будет принимать файлы из любого чата, куда его добавят. Для теста можно, для реальной работы лучше ограничить.

### Важное поведение в группе

- Обычный текст бот молча игнорирует.
- Файлы до 20 МБ загружает в Google Drive.
- Файлы больше 20 МБ фиксирует как ошибку.
- По умолчанию `SEND_ACK_IN_GROUP=false`, поэтому бот не спамит группу ответами после каждого файла.
- Команды `/folder`, `/email`, `/panel` остаются доступными только админам из `ADMIN_IDS`.
- Старые сообщения из истории группы бот не вытащит. Он обрабатывает только новые сообщения после добавления/настройки.

## Как получить Telegram ID

1. Запусти бота без `ADMIN_IDS` или временно оставь пустым.
2. Напиши боту `/whoami`.
3. Скопируй ID в `ADMIN_IDS`.

Можно несколько админов:

```env
ADMIN_IDS=111111111,222222222
```

## Google: что создать

### 1. Включить API

В Google Cloud Console включить:

- Google Drive API
- Google Sheets API

### 2. Создать OAuth Client

Тип: Desktop app.

Получишь:

```text
GOOGLE_CLIENT_ID
GOOGLE_CLIENT_SECRET
```

### 3. Создать Google Sheet

Создай пустую таблицу Google Sheets. Из URL возьми ID:

```text
https://docs.google.com/spreadsheets/d/ВОТ_ЭТО_ID/edit
```

Вставь в Render:

```env
GOOGLE_SHEET_ID=ВОТ_ЭТО_ID
```

### 4. Получить Refresh Token

На своём ПК:

```bash
python -m venv .venv
source .venv/bin/activate   # Linux/Mac
# или Windows:
# .venv\Scripts\activate
pip install -r requirements.txt
python get_google_refresh_token.py
```

Скрипт откроет браузер. Авторизуйся в Google. Потом он выведет:

```env
GOOGLE_REFRESH_TOKEN=...
```

Скопируй значение в переменные Render.

## Развёртывание на Render

1. Создай GitHub-репозиторий.
2. Залей туда файлы из этого архива.
3. В Render создай New Web Service.
4. Подключи репозиторий.
5. Build command:

```bash
pip install -r requirements.txt
```

6. Start command:

```bash
uvicorn app:app --host 0.0.0.0 --port $PORT
```

7. Добавь переменные окружения.
8. После деплоя открой:

```text
https://your-service.onrender.com/ping
```

Если пишет `pong`, сервис живой.

## Webhook Telegram

Бот сам ставит webhook при старте, если заданы:

```env
BOT_TOKEN=...
PUBLIC_URL=https://your-service.onrender.com
TELEGRAM_SECRET=...
```

Webhook будет:

```text
https://your-service.onrender.com/telegram/TELEGRAM_SECRET
```

## Панель

Адрес:

```text
https://your-service.onrender.com/panel
```

Логин и пароль берутся из:

```env
PANEL_LOGIN=admin
PANEL_PASSWORD=сложный_пароль
```

## Как будить Render

В UptimeRobot или cron-job.org поставь проверку каждые 5 минут:

```text
https://your-service.onrender.com/ping
```

## Ограничения тестовой версии

- Файлы больше 20 МБ не скачиваются.
- Архивы не собираются — вместо них используется ссылка на папку Google Drive.
- Реестр хранится в Google Sheets.
- Панель простая, без редактирования строк.
- Если Google OAuth app оставить в Testing, refresh token может слетать. Для стабильной работы лучше перевести OAuth consent screen в In production.
