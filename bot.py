import asyncio
import base64
import json
import logging
import os
import re
import secrets
import sqlite3
from datetime import datetime, date, timezone
from datetime import time as dtime
from typing import Any, Awaitable, Callable, Dict, Optional, Union

import aiohttp
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    TelegramObject,
)
from dotenv import load_dotenv

# ============================================================
# ЗАГРУЗКА КОНФИГА
# ============================================================

load_dotenv()

# ID пользователя, которому доступна админ-панель (/admin) — зашит прямо
# в код (не в .env), чтобы её точно не мог вызвать никто другой.
TESTER_USER_ID = 7550216948

BOT_TOKEN = os.getenv("BOT_TOKEN")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "main")
GITHUB_FOLDER = os.getenv("GITHUB_FOLDER", "subs")  # папка в репо, куда класть txt

_github_repo_env = os.getenv("GITHUB_REPO")
_github_username = os.getenv("GITHUB_USERNAME")
_repository = os.getenv("REPOSITORY")

if _github_repo_env:
    GITHUB_REPO = _github_repo_env
elif _github_username and _repository:
    GITHUB_REPO = f"{_github_username}/{_repository}"
else:
    GITHUB_REPO = None

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не задан в .env")
if not GITHUB_TOKEN or not GITHUB_REPO:
    raise RuntimeError(
        "GITHUB_TOKEN должен быть задан, а репозиторий указан либо через "
        "GITHUB_REPO=owner/repo, либо через пару GITHUB_USERNAME + REPOSITORY в .env"
    )

VERCEL_TOKEN = os.getenv("VERCEL_TOKEN")
VERCEL_TEAM_ID = os.getenv("VERCEL_TEAM_ID")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("subsforge")

BOT_AUTHOR = "@matrosyak"

# ============================================================
# СОСТОЯНИЯ (FSM)
# ============================================================

class CreateSub(StatesGroup):
    waiting_name = State()
    waiting_description = State()
    waiting_url = State()
    waiting_interval = State()
    waiting_date = State()
    waiting_configs = State()
    waiting_link_style = State()


class EditSub(StatesGroup):
    waiting_name = State()
    waiting_description = State()
    waiting_url = State()
    waiting_interval = State()
    waiting_date = State()
    waiting_configs = State()


class Registration(StatesGroup):
    waiting_username = State()


class ChangeUsername(StatesGroup):
    waiting_confirm_current = State()
    waiting_new_username = State()
    waiting_confirm_change = State()


class AdminPanel(StatesGroup):
    waiting_ban_username = State()
    waiting_ban_reason = State()
    waiting_unban_username = State()
    waiting_broadcast_message = State()


# ============================================================
# РЕГУЛЯРКИ И ВАЛИДАЦИЯ
# ============================================================

CONFIG_LINE_RE = re.compile(
    r"^(vless|vmess|trojan|ss|hysteria2)://\S+(#.*)?$",
    re.IGNORECASE,
)

URL_RE = re.compile(
    r"^https?://[a-zA-Z0-9](?:[a-zA-Z0-9\-]*[a-zA-Z0-9])?"
    r"(?:\.[a-zA-Z0-9](?:[a-zA-Z0-9\-]*[a-zA-Z0-9])?)+"
    r"(?:/[^\s]*)?$"
)

MAX_NAME_LEN = 64
MAX_DESCRIPTION_LEN = 200
MAX_URL_LEN = 300
MIN_INTERVAL_HOURS = 1
MAX_INTERVAL_HOURS = 1000
DEFAULT_INTERVAL_HOURS = 24

RANDOM_SUFFIX_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"
RANDOM_SUFFIX_LEN = 7


def generate_random_suffix() -> str:
    """Случайный суффикс, добавляемый к нику в каждой НОВОЙ подписке, чтобы
    у разных подписок одного пользователя не совпадали GitHub-папка и
    Vercel project slug (иначе вторая подписка затирала бы первую)."""
    return "".join(secrets.choice(RANDOM_SUFFIX_ALPHABET) for _ in range(RANDOM_SUFFIX_LEN))

# Ник в боте попадает напрямую в GitHub-путь и в Vercel project slug, поэтому
# разрешаем только безопасные для URL символы: латиница, цифры, - и _.
USERNAME_RE = re.compile(r"^[a-zA-Z0-9_-]{3,32}$")
MIN_USERNAME_LEN = 3
MAX_USERNAME_LEN = 32


def validate_username(text: str) -> Optional[str]:
    text = text.strip()
    if not USERNAME_RE.match(text):
        return None
    return text


def extract_valid_configs(text: str) -> list[str]:
    lines = [line.strip() for line in text.splitlines()]
    valid = []
    seen = set()
    for line in lines:
        if not line:
            continue
        if CONFIG_LINE_RE.match(line):
            if line not in seen:
                seen.add(line)
                valid.append(line)
    return valid


def validate_sub_name(name: str) -> Optional[str]:
    name = name.strip()
    if not name:
        return None
    if len(name) > MAX_NAME_LEN:
        return None
    if any(ord(ch) < 32 for ch in name):
        return None
    return name


def validate_date(text: str) -> Optional[date]:
    text = text.strip()
    formats = ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y")
    for fmt in formats:
        try:
            d = datetime.strptime(text, fmt).date()
            if d < date.today():
                return None
            return d
        except ValueError:
            continue
    return None


def validate_interval(text: str) -> Optional[int]:
    text = text.strip()
    if not text.isdigit():
        return None
    value = int(text)
    if value < MIN_INTERVAL_HOURS or value > MAX_INTERVAL_HOURS:
        return None
    return value


def sanitize_free_text(text: str, max_len: int) -> str:
    text = text.strip()
    text = text.replace("\n", " ").replace("\r", " ")
    if len(text) > max_len:
        text = text[:max_len]
    return text


def validate_url(text: str) -> Optional[str]:
    text = text.strip()
    if len(text) > MAX_URL_LEN:
        return None
    if not URL_RE.match(text):
        return None
    return text


def clean_header_value(value: str) -> str:
    return value.replace("\n", " ").replace("\r", " ").strip()


def slugify(name: str) -> str:
    slug = name.strip().lower()
    slug = re.sub(r"[^a-z0-9а-яё]+", "-", slug, flags=re.IGNORECASE)
    slug = slug.strip("-")
    if not slug:
        slug = "subscription"
    return slug


_TRANSLIT_MAP = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "",
    "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


def vercel_safe_slug(name: str) -> str:
    lowered = name.strip().lower()
    translit = "".join(_TRANSLIT_MAP.get(ch, ch) for ch in lowered)
    slug = re.sub(r"[^a-z0-9_\-]+", "-", translit)
    slug = re.sub(r"-+", "-", slug).strip("-")
    if not slug:
        slug = "sub"
    return slug[:50]


def build_subscription_content(
    title: str,
    description: str,
    url: str,
    interval_hours: int,
    expire_dt: Optional[date],
    configs: list[str],
) -> str:
    if expire_dt is not None:
        expire_ts = int(
            datetime.combine(expire_dt, dtime(23, 59, 59), tzinfo=timezone.utc).timestamp()
        )
    else:
        expire_ts = 0

    lines = [
        f"#profile-title: {clean_header_value(title)}",
        f"#profile-update-interval: {interval_hours}",
        f"#subscription-userinfo: expire={expire_ts}; total=0; used=0",
    ]

    if url:
        lines.append(f"#profile-web-page-url: {clean_header_value(url)}")

    if description:
        lines.append(f"#announce: {clean_header_value(description)}")

    header = "\n".join(lines)
    body = "\n".join(configs)
    return f"{header}\n\n{body}\n"


# ============================================================
# GITHUB API
# ============================================================

async def _jsdelivr_purge(repo: str, branch: str, path: str) -> None:
    """Принудительно сбрасывает кэш jsDelivr для конкретного файла сразу
    после записи — без этого обновлённая подписка может отдаваться из кэша
    CDN ещё до 7 дней (штатное поведение jsDelivr)."""
    url = f"https://purge.jsdelivr.net/gh/{repo}@{branch}/{path}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    logger.warning(f"jsDelivr purge вернул {resp.status} для {path}")
    except Exception:
        logger.exception(f"Не удалось сделать purge jsDelivr для {path}")


class GitHubUploader:
    def __init__(self, token: str, repo: str, branch: str, folder: str):
        self.token = token
        self.repo = repo
        self.branch = branch
        self.folder = folder.strip("/")

    def _headers(self):
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _path_for(self, filename: str) -> str:
        return f"{self.folder}/{filename}" if self.folder else filename

    async def _upsert_at_path(self, path: str, content: str, commit_message: str) -> str:
        url = f"https://api.github.com/repos/{self.repo}/contents/{path}"
        encoded = base64.b64encode(content.encode("utf-8")).decode("utf-8")

        async with aiohttp.ClientSession() as session:
            sha = None
            async with session.get(
                url, headers=self._headers(), params={"ref": self.branch}
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    sha = data.get("sha")
                elif resp.status not in (404,):
                    body = await resp.text()
                    raise RuntimeError(
                        f"Ошибка GitHub API при проверке файла: {resp.status} {body}"
                    )

            payload = {
                "message": commit_message,
                "content": encoded,
                "branch": self.branch,
            }
            if sha:
                payload["sha"] = sha

            async with session.put(
                url, headers=self._headers(), json=payload
            ) as resp:
                if resp.status not in (200, 201):
                    body = await resp.text()
                    raise RuntimeError(
                        f"Ошибка GitHub API при загрузке файла: {resp.status} {body}"
                    )

        # Просим jsDelivr сразу сбросить кэш этого файла — иначе штатное
        # поведение CDN может показывать старую версию подписки до 7 дней.
        # Запускаем в фоне, чтобы не задерживать ответ пользователю; делаем
        # это независимо от текущего link_style — вдруг подписку позже
        # переключат на jsDelivr, и она сразу будет актуальной.
        asyncio.create_task(_jsdelivr_purge(self.repo, self.branch, path))

        return f"https://raw.githubusercontent.com/{self.repo}/{self.branch}/{path}"

    async def upload_text_file(self, filename: str, content: str) -> tuple[str, str]:
        path = self._path_for(filename)
        raw_url = await self._upsert_at_path(
            path, content, f"Add subscription {filename}"
        )
        return raw_url, path

    async def update_at_path(self, path: str, content: str) -> str:
        return await self._upsert_at_path(path, content, f"Update subscription {path}")

    async def delete_file(self, path: str) -> None:
        url = f"https://api.github.com/repos/{self.repo}/contents/{path}"

        async with aiohttp.ClientSession() as session:
            sha = None
            async with session.get(
                url, headers=self._headers(), params={"ref": self.branch}
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    sha = data.get("sha")
                elif resp.status == 404:
                    return
                else:
                    body = await resp.text()
                    raise RuntimeError(
                        f"Ошибка GitHub API при поиске файла для удаления: {resp.status} {body}"
                    )

            payload = {
                "message": f"Delete subscription {path}",
                "sha": sha,
                "branch": self.branch,
            }
            async with session.delete(
                url, headers=self._headers(), json=payload
            ) as resp:
                if resp.status not in (200, 204):
                    body = await resp.text()
                    raise RuntimeError(
                        f"Ошибка GitHub API при удалении файла: {resp.status} {body}"
                    )


github_uploader = GitHubUploader(
    token=GITHUB_TOKEN,
    repo=GITHUB_REPO,
    branch=GITHUB_BRANCH,
    folder=GITHUB_FOLDER,
)


# ============================================================
# VERCEL — единый шлюз вместо деплоя на каждую подписку
#
# Раньше каждая подписка создавала СВОЙ Vercel-проект/деплой — на Hobby-плане
# это упирается в лимит 100 деплойментов в сутки на весь аккаунт.
#
# Теперь деплоится ОДИН проект с serverless-функцией. Функция принимает
# /sub/<ник>/<суффикс>, на лету собирает путь к файлу на GitHub
# (subs/<ник>/<суффикс>.txt) и отдаёт его содержимое. Новые подписки просто
# кладут файл на GitHub — Vercel это не касается вообще, деплой не нужен.
# ============================================================

GATEWAY_FUNCTION_CODE_TEMPLATE = """\
const GITHUB_REPO = {repo!r};
const GITHUB_BRANCH = {branch!r};
const GITHUB_FOLDER = {folder!r};
const SAFE = /^[A-Za-z0-9_-]{{1,50}}$/;

module.exports = async (req, res) => {{
  const username = String(req.query.username || '');
  const suffix = String(req.query.suffix || '');

  if (!SAFE.test(username) || !SAFE.test(suffix)) {{
    res.status(400).send('Bad request');
    return;
  }}

  const target = `https://raw.githubusercontent.com/${{GITHUB_REPO}}/${{GITHUB_BRANCH}}/${{GITHUB_FOLDER}}/${{username}}/${{suffix}}.txt`;

  let upstream;
  try {{
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 8000);
    upstream = await fetch(target, {{ signal: controller.signal }});
    clearTimeout(timeout);
  }} catch (e) {{
    res.status(502).send('Upstream error');
    return;
  }}

  if (!upstream.ok) {{
    res.status(404).send('Subscription not found');
    return;
  }}

  const text = await upstream.text();
  res.setHeader('Content-Type', 'text/plain; charset=utf-8');
  res.setHeader('Cache-Control', 'no-store');
  res.status(200).send(text);
}};
"""

GATEWAY_VERCEL_JSON = {
    "version": 2,
    "rewrites": [
        {"source": "/sub/:username/:suffix", "destination": "/api/sub?username=:username&suffix=:suffix"}
    ],
}


class VercelUploader:
    def __init__(self, token: Optional[str], team_id: Optional[str]):
        self.token = token
        self.team_id = team_id

    @property
    def configured(self) -> bool:
        return bool(self.token)

    async def deploy_gateway(self, project_slug: str) -> str:
        """Деплоит (или обновляет) ОДИН проект-шлюз. Вызывается один раз при
        первом запуске бота — новые подписки этот метод больше НЕ трогают."""
        if not self.token:
            raise RuntimeError("VERCEL_TOKEN не задан в .env")

        function_code = GATEWAY_FUNCTION_CODE_TEMPLATE.format(
            repo=GITHUB_REPO, branch=GITHUB_BRANCH, folder=GITHUB_FOLDER
        )

        url = "https://api.vercel.com/v13/deployments"
        params = {"teamId": self.team_id} if self.team_id else {}
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }
        payload = {
            "name": project_slug,
            "files": [
                {"file": "vercel.json", "data": json.dumps(GATEWAY_VERCEL_JSON)},
                {"file": "api/sub.js", "data": function_code},
            ],
            "target": "production",
            "projectSettings": {"framework": None},
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(
                url, headers=headers, params=params, json=payload
            ) as resp:
                data = await resp.json()
                if resp.status not in (200, 201):
                    raise RuntimeError(f"Ошибка Vercel API: {resp.status} {data}")
                deployment_url = data.get("url")
                if not deployment_url:
                    raise RuntimeError("Vercel API не вернул адрес деплоя")
                return f"https://{deployment_url}"

    async def disable_deployment_protection(self, project_slug: str) -> None:
        """Отключает Vercel Authentication (Deployment Protection) для проекта
        через PATCH /v9/projects/{id}, передавая ssoProtection: null.
        Без этого шага *.vercel.app ссылка требует логин в Vercel и клиенты
        вроде Happ не видят конфиги (получают страницу логина вместо текста
        подписки). Вызывается один раз при деплое шлюза."""
        if not self.token:
            return

        url = f"https://api.vercel.com/v9/projects/{project_slug}"
        params = {"teamId": self.team_id} if self.team_id else {}
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }
        payload = {"ssoProtection": None}

        async with aiohttp.ClientSession() as session:
            async with session.patch(
                url, headers=headers, params=params, json=payload
            ) as resp:
                if resp.status not in (200, 201):
                    body = await resp.text()
                    raise RuntimeError(
                        f"Ошибка Vercel API при отключении защиты: {resp.status} {body}"
                    )


vercel_uploader = VercelUploader(VERCEL_TOKEN, VERCEL_TEAM_ID)

# Кэш ссылки на шлюз в оперативной памяти — заполняется при старте бота
# функцией init_vercel_gateway(), чтобы не дёргать Vercel API на каждую подписку.
VERCEL_GATEWAY_URL: Optional[str] = None
VERCEL_GATEWAY_PROJECT_SLUG = vercel_safe_slug(
    os.getenv("VERCEL_GATEWAY_SLUG", "") or f"{(GITHUB_REPO or 'subsforge').split('/')[-1]}-gateway"
)



# ============================================================
# ХРАНИЛИЩЕ ИСТОРИИ ПОДПИСОК (SQLite)
# ============================================================

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "subsforge.db")
MAX_SUBS_PER_USER_SHOWN = 15
MAX_SUBS_PER_USER = 20


def _db_init_sync() -> None:
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                description TEXT,
                url TEXT,
                interval_hours INTEGER,
                expire_date TEXT,
                expire_date_iso TEXT,
                configs_count INTEGER,
                configs_text TEXT NOT NULL DEFAULT '',
                raw_url TEXT NOT NULL,
                display_url TEXT,
                link_style TEXT DEFAULT 'github',
                file_path TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT NOT NULL,
                username_lower TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        existing_cols = {
            row[1] for row in conn.execute("PRAGMA table_info(subscriptions)").fetchall()
        }
        if "configs_text" not in existing_cols:
            conn.execute("ALTER TABLE subscriptions ADD COLUMN configs_text TEXT NOT NULL DEFAULT ''")
        if "expire_date_iso" not in existing_cols:
            conn.execute("ALTER TABLE subscriptions ADD COLUMN expire_date_iso TEXT")
        if "display_url" not in existing_cols:
            conn.execute("ALTER TABLE subscriptions ADD COLUMN display_url TEXT")
        if "link_style" not in existing_cols:
            conn.execute("ALTER TABLE subscriptions ADD COLUMN link_style TEXT DEFAULT 'github'")

        users_cols = {
            row[1] for row in conn.execute("PRAGMA table_info(users)").fetchall()
        }
        if "banned" not in users_cols:
            conn.execute("ALTER TABLE users ADD COLUMN banned INTEGER NOT NULL DEFAULT 0")
        if "ban_reason" not in users_cols:
            conn.execute("ALTER TABLE users ADD COLUMN ban_reason TEXT")

        conn.commit()
    finally:
        conn.close()


def _db_add_sync(record: dict) -> int:
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.execute(
            """
            INSERT INTO subscriptions
                (user_id, name, description, url, interval_hours, expire_date,
                 expire_date_iso, configs_count, configs_text, raw_url, display_url,
                 link_style, file_path, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record["user_id"],
                record["name"],
                record["description"],
                record["url"],
                record["interval_hours"],
                record["expire_date"],
                record["expire_date_iso"],
                record["configs_count"],
                record["configs_text"],
                record["raw_url"],
                record["display_url"],
                record["link_style"],
                record["file_path"],
                record["created_at"],
            ),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _db_list_sync(user_id: int, limit: int) -> list[dict]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM subscriptions WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def _db_count_sync(user_id: int) -> int:
    conn = sqlite3.connect(DB_PATH)
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM subscriptions WHERE user_id = ?", (user_id,)
        ).fetchone()
        return row[0] if row else 0
    finally:
        conn.close()


def _db_get_sync(sub_id: int, user_id: int) -> Optional[dict]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM subscriptions WHERE id = ? AND user_id = ?",
            (sub_id, user_id),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _db_delete_sync(sub_id: int, user_id: int) -> None:
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            "DELETE FROM subscriptions WHERE id = ? AND user_id = ?",
            (sub_id, user_id),
        )
        conn.commit()
    finally:
        conn.close()


def _db_update_sync(sub_id: int, user_id: int, fields: dict) -> None:
    if not fields:
        return
    columns = ", ".join(f"{k} = ?" for k in fields.keys())
    values = list(fields.values()) + [sub_id, user_id]
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            f"UPDATE subscriptions SET {columns} WHERE id = ? AND user_id = ?", values
        )
        conn.commit()
    finally:
        conn.close()


async def db_add_subscription(record: dict) -> int:
    return await asyncio.to_thread(_db_add_sync, record)


async def db_list_subscriptions(user_id: int, limit: int = MAX_SUBS_PER_USER_SHOWN) -> list[dict]:
    return await asyncio.to_thread(_db_list_sync, user_id, limit)


async def db_count_subscriptions(user_id: int) -> int:
    return await asyncio.to_thread(_db_count_sync, user_id)


async def db_get_subscription(sub_id: int, user_id: int) -> Optional[dict]:
    return await asyncio.to_thread(_db_get_sync, sub_id, user_id)


async def db_delete_subscription(sub_id: int, user_id: int) -> None:
    await asyncio.to_thread(_db_delete_sync, sub_id, user_id)


async def db_update_subscription(sub_id: int, user_id: int, **fields) -> None:
    await asyncio.to_thread(_db_update_sync, sub_id, user_id, fields)


def _db_get_user_sync(user_id: int) -> Optional[dict]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _db_username_taken_sync(username_lower: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    try:
        row = conn.execute(
            "SELECT 1 FROM users WHERE username_lower = ?", (username_lower,)
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def _db_create_user_sync(user_id: int, username: str, created_at: str) -> bool:
    """Возвращает True при успехе, False если ник уже занят (гонка запросов
    перехватывается уникальным индексом на username_lower)."""
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            "INSERT INTO users (user_id, username, username_lower, created_at) "
            "VALUES (?, ?, ?, ?)",
            (user_id, username, username.lower(), created_at),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()


async def db_get_user(user_id: int) -> Optional[dict]:
    return await asyncio.to_thread(_db_get_user_sync, user_id)


async def db_username_taken(username_lower: str) -> bool:
    return await asyncio.to_thread(_db_username_taken_sync, username_lower)


async def db_create_user(user_id: int, username: str) -> bool:
    return await asyncio.to_thread(
        _db_create_user_sync, user_id, username, datetime.now().isoformat()
    )


def _db_delete_user_sync(user_id: int) -> None:
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
        conn.commit()
    finally:
        conn.close()


async def db_delete_user(user_id: int) -> None:
    await asyncio.to_thread(_db_delete_user_sync, user_id)


def _db_rename_user_sync(user_id: int, new_username: str) -> bool:
    """Возвращает True при успехе, False если новый ник уже занят
    (гонка запросов перехватывается тем же уникальным индексом)."""
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            "UPDATE users SET username = ?, username_lower = ? WHERE user_id = ?",
            (new_username, new_username.lower(), user_id),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()


async def db_rename_user(user_id: int, new_username: str) -> bool:
    return await asyncio.to_thread(_db_rename_user_sync, user_id, new_username)


def _db_get_user_by_username_sync(username_lower: str) -> Optional[dict]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM users WHERE username_lower = ?", (username_lower,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


async def db_get_user_by_username(username_lower: str) -> Optional[dict]:
    return await asyncio.to_thread(_db_get_user_by_username_sync, username_lower)


def _db_set_ban_sync(user_id: int, banned: bool, reason: Optional[str]) -> None:
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            "UPDATE users SET banned = ?, ban_reason = ? WHERE user_id = ?",
            (1 if banned else 0, reason, user_id),
        )
        conn.commit()
    finally:
        conn.close()


async def db_set_ban(user_id: int, banned: bool, reason: Optional[str] = None) -> None:
    await asyncio.to_thread(_db_set_ban_sync, user_id, banned, reason)


def _db_count_users_sync() -> int:
    conn = sqlite3.connect(DB_PATH)
    try:
        row = conn.execute("SELECT COUNT(*) FROM users").fetchone()
        return row[0] if row else 0
    finally:
        conn.close()


async def db_count_users() -> int:
    return await asyncio.to_thread(_db_count_users_sync)


def _db_count_all_subscriptions_sync() -> int:
    conn = sqlite3.connect(DB_PATH)
    try:
        row = conn.execute("SELECT COUNT(*) FROM subscriptions").fetchone()
        return row[0] if row else 0
    finally:
        conn.close()


async def db_count_all_subscriptions() -> int:
    return await asyncio.to_thread(_db_count_all_subscriptions_sync)


def _db_list_all_user_ids_sync() -> list[int]:
    conn = sqlite3.connect(DB_PATH)
    try:
        rows = conn.execute("SELECT user_id FROM users").fetchall()
        return [row[0] for row in rows]
    finally:
        conn.close()


async def db_list_all_user_ids() -> list[int]:
    return await asyncio.to_thread(_db_list_all_user_ids_sync)


def _db_get_setting_sync(key: str) -> Optional[str]:
    conn = sqlite3.connect(DB_PATH)
    try:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def _db_set_setting_sync(key: str, value: str) -> None:
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        conn.commit()
    finally:
        conn.close()


async def db_get_setting(key: str) -> Optional[str]:
    return await asyncio.to_thread(_db_get_setting_sync, key)


async def db_set_setting(key: str, value: str) -> None:
    await asyncio.to_thread(_db_set_setting_sync, key, value)


def record_link(record: dict) -> str:
    return record.get("display_url") or record["raw_url"]


def extract_username_from_file_path(file_path: str) -> Optional[str]:
    """file_path имеет вид 'subs/<ник>/<суффикс>.txt' — достаём <ник>."""
    parts = file_path.split("/")
    if len(parts) < 2:
        return None
    return parts[-2]


async def has_pending_migration(user_id: int, current_username: str) -> bool:
    """True, если у пользователя есть подписки, файлы которых на GitHub всё
    ещё лежат под старым ником (т.е. после смены ника перенос ещё не
    подтверждён и не отклонён)."""
    records = await db_list_subscriptions(user_id, limit=100_000)
    if not records:
        return False
    return any(
        extract_username_from_file_path(r["file_path"]) != current_username
        for r in records
    )


RAW_GITHUB_PREFIX = "https://raw.githubusercontent.com/"


def build_display_url(link_style: str, raw_url: str, username: str, suffix: str) -> str:
    """Собирает итоговую ссылку подписки под выбранный стиль. Для cloudflare
    и jsdelivr — это готовые публичные CDN-прокси поверх raw.githubusercontent.com,
    без своего API и деплоя: просто меняем префикс URL."""
    if link_style == "vercel" and VERCEL_GATEWAY_URL:
        return f"{VERCEL_GATEWAY_URL}/sub/{username}/{suffix}"
    if link_style == "cloudflare":
        # raw.githack.com — прокси поверх GitHub raw через сеть Cloudflare.
        return raw_url.replace(RAW_GITHUB_PREFIX, "https://raw.githack.com/", 1)
    if link_style == "jsdelivr":
        # cdn.jsdelivr.net — один из крупнейших бесплатных CDN в мире
        # (Cloudflare + Fastly + Google Cloud CDN), 10к пользователей для
        # него не нагрузка вообще. Формат отличается от простой замены
        # префикса: /gh/<repo>@<branch>/<путь>.
        path_part = f"{GITHUB_FOLDER}/{username}/{suffix}.txt"
        return f"https://cdn.jsdelivr.net/gh/{GITHUB_REPO}@{GITHUB_BRANCH}/{path_part}"
    return raw_url



_db_init_sync()

# ============================================================
# КЛАВИАТУРЫ
# ============================================================

def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Создать подписку", callback_data="create_sub")],
            [InlineKeyboardButton(text="📂 Мои подписки", callback_data="my_subs")],
            [InlineKeyboardButton(text="✏️ Редактирование подписок", callback_data="edit_subs")],
            [InlineKeyboardButton(text="⚙️ Настройки", callback_data="settings")],
            [InlineKeyboardButton(text="❤️ Поддержать проект", callback_data="support_project")],
            [InlineKeyboardButton(text="ℹ️ О боте", callback_data="about")],
        ]
    )


def settings_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📛 Изменить ник", callback_data="settings_change_username")],
            [InlineKeyboardButton(text="📊 Мой профиль", callback_data="settings_profile")],
            [InlineKeyboardButton(text="🗑 Удалить все подписки", callback_data="settings_wipe_subs")],
            [InlineKeyboardButton(text="🏠 В меню", callback_data="menu")],
        ]
    )


def yes_no_kb(yes_callback: str, no_callback: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Да", callback_data=yes_callback),
                InlineKeyboardButton(text="❌ Нет", callback_data=no_callback),
            ]
        ]
    )


def admin_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Ресет ника", callback_data="admin_reset_nick")],
            [InlineKeyboardButton(text="📊 Статы", callback_data="admin_stats")],
            [InlineKeyboardButton(text="🚫 Бан", callback_data="admin_ban")],
            [InlineKeyboardButton(text="✅ Разбан", callback_data="admin_unban")],
            [InlineKeyboardButton(text="📢 Рассылка", callback_data="admin_broadcast")],
        ]
    )


def admin_cancel_kb() -> InlineKeyboardMarkup:
    return cancel_kb(cancel_callback="admin_cancel")


def cancel_kb(cancel_callback: str = "cancel") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="✖️ Отмена", callback_data=cancel_callback)]]
    )


def skip_kb(skip_callback: str, cancel_callback: str = "cancel") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⏭ Пропустить", callback_data=skip_callback)],
            [InlineKeyboardButton(text="✖️ Отмена", callback_data=cancel_callback)],
        ]
    )


def edit_clear_kb(label: str, clear_callback: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=label, callback_data=clear_callback)],
            [InlineKeyboardButton(text="✖️ Отмена", callback_data="edit_cancel")],
        ]
    )


def interval_kb(prefix: str = "interval", cancel_callback: str = "cancel") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="1 час", callback_data=f"{prefix}_1"),
                InlineKeyboardButton(text="6 часов", callback_data=f"{prefix}_6"),
            ],
            [
                InlineKeyboardButton(text="12 часов", callback_data=f"{prefix}_12"),
                InlineKeyboardButton(text="24 часа", callback_data=f"{prefix}_24"),
            ],
            [InlineKeyboardButton(text="48 часов", callback_data=f"{prefix}_48")],
            [InlineKeyboardButton(text="⏭ По умолчанию (24ч)", callback_data=f"{prefix}_skip")],
            [InlineKeyboardButton(text="✖️ Отмена", callback_data=cancel_callback)],
        ]
    )


def link_style_kb() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="🐙 GitHub (обычная ссылка)", callback_data="linkstyle_github")]]
    if VERCEL_GATEWAY_URL:
        rows.append([InlineKeyboardButton(text="🐸 Vercel (короткий домен)", callback_data="linkstyle_vercel")])
    rows.append([InlineKeyboardButton(text="☁️ Cloudflare (CDN)", callback_data="linkstyle_cloudflare")])
    rows.append([InlineKeyboardButton(text="⚡ jsDelivr (CDN)", callback_data="linkstyle_jsdelivr")])
    rows.append([InlineKeyboardButton(text="✖️ Отмена", callback_data="cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def back_to_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🏠 В меню", callback_data="menu")]]
    )


def confirm_configs_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Создать подписку", callback_data="confirm_create")],
            [InlineKeyboardButton(text="➕ Добавить ещё конфиги", callback_data="add_more")],
            [InlineKeyboardButton(text="✖️ Отмена", callback_data="cancel")],
        ]
    )


def _truncate_label(name: str, max_len: int = 24) -> str:
    return name if len(name) <= max_len else name[:max_len] + "…"


def my_subs_kb(records: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    for r in records:
        label = _truncate_label(r["name"])
        rows.append(
            [InlineKeyboardButton(text=f"🗑 Удалить «{label}»", callback_data=f"del_sub:{r['id']}")]
        )
    rows.append([InlineKeyboardButton(text="🏠 В меню", callback_data="menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def edit_subs_kb(records: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    for r in records:
        label = _truncate_label(r["name"])
        rows.append(
            [InlineKeyboardButton(text=f"✏️ {label}", callback_data=f"edit_pick:{r['id']}")]
        )
    rows.append([InlineKeyboardButton(text="🏠 В меню", callback_data="menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def edit_field_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📛 Название", callback_data="edit_field_name")],
            [InlineKeyboardButton(text="🗒 Описание", callback_data="edit_field_description")],
            [InlineKeyboardButton(text="🔗 Ссылка", callback_data="edit_field_url")],
            [InlineKeyboardButton(text="🔄 Автообновление", callback_data="edit_field_interval")],
            [InlineKeyboardButton(text="📅 Дата окончания", callback_data="edit_field_date")],
            [InlineKeyboardButton(text="🔑 Конфиги (заменить)", callback_data="edit_field_configs")],
            [InlineKeyboardButton(text="🔙 К списку подписок", callback_data="edit_subs")],
            [InlineKeyboardButton(text="📋 Скопировать конфиги", callback_data="edit_copy_configs")],
            [InlineKeyboardButton(text="🏠 В меню", callback_data="menu")],
        ]
    )


def edit_field_cancel_kb() -> InlineKeyboardMarkup:
    return cancel_kb(cancel_callback="edit_cancel")


def edit_copy_back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="edit_copy_back")]]
    )


# ============================================================
# ТЕКСТЫ
# ============================================================

WELCOME_TEXT = (
    "👋 <b>Добро пожаловать в SubsForgeBot!</b>\n\n"
    "Я помогу тебе создать VPN-подписку для клиентов:\n"
    "Happ, Hiddify, V2RayTun, V2Box, Incy, Streisand.\n\n"
    "Нажми кнопку ниже, чтобы начать."
)

ASK_USERNAME_TEXT = (
    "👋 <b>SubsForgeBot</b>\n\n"
    "Перед тем как начать, придумайте никнейм.\n"
    "Он будет использоваться в ссылках твоих подписок поэтому должен быть уникальным."
)

INVALID_USERNAME_TEXT = (
    f"⚠️ Ник должен быть от {MIN_USERNAME_LEN} до {MAX_USERNAME_LEN} символов "
    "и содержать только латинские буквы, цифры, «-» и «_» (без пробелов и "
    "эмодзи).\n\n" + ASK_USERNAME_TEXT
)

USERNAME_TAKEN_TEXT = (
    "🚫 Этот никнейм уже занят другим пользователем (регистр не важен).\n\n"
    "Попробуй другой.\n\n" + ASK_USERNAME_TEXT
)

SETTINGS_TEXT = "⚙️ <b>Настройки</b>\n\nЧто хочешь настроить?"

ASK_CONFIRM_CURRENT_USERNAME_TEXT = (
    "📛 <b>Изменение ника</b>\n\n"
    "Твой текущий ник: <b>{current}</b>\n\n"
    "Чтобы продолжить, напиши его ещё раз."
)

CONFIRM_CURRENT_USERNAME_MISMATCH_TEXT = (
    "⚠️ Не совпадает с текущим ником. Попробуй ещё раз.\n\n"
    + ASK_CONFIRM_CURRENT_USERNAME_TEXT
)

ASK_NEW_USERNAME_TEXT = "📛 Теперь придумай новый ник."

NEW_USERNAME_SAME_AS_OLD_TEXT = (
    "⚠️ Это твой текущий ник. Придумай другой.\n\n" + ASK_NEW_USERNAME_TEXT
)

CHANGE_USERNAME_INVALID_TEXT = (
    f"⚠️ Ник должен быть от {MIN_USERNAME_LEN} до {MAX_USERNAME_LEN} символов "
    "и содержать только латинские буквы, цифры, «-» и «_» (без пробелов и "
    "эмодзи).\n\n" + ASK_NEW_USERNAME_TEXT
)

CHANGE_USERNAME_TAKEN_TEXT = (
    "🚫 Этот ник уже занят другим пользователем.\n\n" + ASK_NEW_USERNAME_TEXT
)


def confirm_change_username_text(old: str, new: str) -> str:
    return (
        f"Вы уверены что хотите изменить ник?\n\n"
        f"Было: <b>{old}</b>\n"
        f"Станет: <b>{new}</b>"
    )


SUBS_MISSING_AFTER_RENAME_TEXT = (
    "📂 <b>Мои подписки</b>\n\n"
    "Ты сменил ник, поэтому старые подписки нужно перенести на новый ник, "
    "иначе их ссылки перестанут работать.\n\n"
    "Перенести подписки на новый ник?"
)

PENDING_MIGRATION_TEXT = (
    "⚠️ Ты сменил ник, а подписки ещё не перенесены.\n\n"
    "Сначала зайди в раздел «📂 Мои подписки» и реши, перенести старые "
    "подписки на новый ник или удалить их — потом сможешь пользоваться "
    "остальными разделами."
)

ABOUT_TEXT = (
    "ℹ️ <b>SubsForgeBot</b>\n\n"
    "Бот создаёт подписки из VPN-конфигов (vless, vmess, trojan, ss, hysteria2) "
    "и публикует их на GitHub."
)

SUPPORT_TEXT = (
    "❤️ <b>Поддержать проект</b>\n\n"
    "Если бот оказался полезен можно поддержать автора: "
    f"<b>{BOT_AUTHOR}</b>\n\n"
    "🎁 Например, можете кинуть ему подарок Telegram это лучший "
    "способ сказать спасибо и замотивировать на дальнейшую разработку."
)

ASK_NAME_TEXT = (
    "📝 <b>Шаг 1 из 7</b>\n\n"
    "Введи название подписки (например: <i>Мой VPN</i>)\n"
    "Оно попадёт в #profile-title и в имя файла."
)

ASK_DESCRIPTION_TEXT = (
    "🗒 <b>Шаг 2 из 7</b>\n\n"
    "Введи короткое описание подписки (попадёт в #announce).\n"
    "Например: <i>Личная подписка, не делиться доступом</i>.\n\n"
    "Можно пропустить."
)

ASK_URL_TEXT = (
    "🔗 <b>Шаг 3 из 7</b>\n\n"
    "Пришли ссылку (например, на Telegram-канал или профиль) попадёт в "
    "#profile-web-page-url.\n"
    "Формат: <code>https://t.me/halyava_vpnz</code>\n\n"
    "Можно пропустить."
)

ASK_INTERVAL_TEXT = (
    "🔄 <b>Шаг 4 из 7</b>\n\n"
    "Как часто клиент должен автообновлять подписку? Выбери вариант ниже "
    "или введи число часов вручную (1–1000)."
)

ASK_DATE_TEXT = (
    "📅 <b>Шаг 5 из 7</b>\n\n"
    "Введи дату окончания подписки в формате <b>ДД.ММ.ГГГГ</b>\n"
    "(например: 31.12.2026).\n\n"
    "Можно пропустить."
)

ASK_CONFIGS_TEXT = (
    "🔑 <b>Шаг 6 из 7</b>\n\n"
    "Пришли VPN-конфиги (можно несколько строк в одном сообщении).\n"
    "Поддерживаются протоколы:\n"
    "<code>vless://</code> <code>vmess://</code> <code>trojan://</code> "
    "<code>ss://</code> <code>hysteria2://</code>"
)

ASK_LINK_STYLE_TEXT = (
    "🎨 <b>Шаг 7 из 7 (по желанию)</b>\n\n"
    "Как оформить ссылку на подписку?\n\n"
    "🐙 <b>GitHub</b> - обычная прямая ссылка на raw-файл в репозитории "
    "<code>raw.githubusercontent.com/</code>\n\n"
    "🐸 <b>Vercel</b> - короткий домен вида <code>vercel.app/sub/</code>\n\n"
    "☁️ <b>Cloudflare</b> - ссылка Cloudflare вида <code>raw.githack.com</code>\n\n"
    "⚡️ <b>jsDelivr</b> - ссылка вида <code>cdn.jsdelivr.net</code>\n\n"
    "Не знаешь, что выбрать выбирай GitHub, это вариант по умолчанию."
)

INVALID_CONFIGS_TEXT = (
    "🚫 Это не похоже на VPN-конфиги.\n\n"
    "Пришли, пожалуйста, ссылки, начинающиеся с:\n"
    "<code>vless://</code>, <code>vmess://</code>, <code>trojan://</code>, "
    "<code>ss://</code> или <code>hysteria2://</code>"
)

NO_EXPIRY_LABEL = "без ограничения по сроку"

LINK_STYLE_LABELS = {
    "github": "🐙 GitHub",
    "vercel": "🐸 Vercel",
    "cloudflare": "☁️ Cloudflare",
    "jsdelivr": "⚡ jsDelivr",
}

EDIT_ASK_NAME_TEXT = "📛 Введи новое название подписки"
EDIT_ASK_DESCRIPTION_TEXT = "🗒 Введи новое описание."
EDIT_ASK_URL_TEXT = "🔗 Пришли новую ссылку в формате <code>https://t.me/halyava_vpnz</code>"
EDIT_ASK_INTERVAL_TEXT = "🔄 Выбери новый интервал автообновления или введи число часов вручную (1–1000)."
EDIT_ASK_DATE_TEXT = "📅 Введи новую дату окончания в формате ДД.ММ.ГГГГ"
EDIT_ASK_CONFIGS_TEXT = "🔑 Пришли новый полный список конфигов он полностью заменит текущий."


def edit_summary_text(record: dict, prefix: str = "") -> str:
    desc = record.get("description") or "—"
    url = record.get("url") or "—"
    style_label = LINK_STYLE_LABELS.get(record.get("link_style") or "github", "🐙 GitHub")
    return (
        f"{prefix}"
        "✏️ <b>Редактирование подписки</b>\n\n"
        f"📛 Название: <b>{record['name']}</b>\n"
        f"🗒 Описание: <b>{desc}</b>\n"
        f"🔗 Ссылка: <b>{url}</b>\n"
        f"🔄 Автообновление: <b>{record['interval_hours']} ч.</b>\n"
        f"📅 Действует до: <b>{record['expire_date']}</b>\n"
        f"🔑 Конфигов: <b>{record['configs_count']}</b>\n"
        f"🎨 Стиль ссылки: <b>{style_label}</b>\n\n"
        f"🔗 Ссылка подписки: <code>{record_link(record)}</code>\n\n"
        "Что хочешь изменить?)"
    )


def copy_configs_text(record: dict, configs_text: str, truncated: bool) -> str:
    warn = (
        "\n\n⚠️ Конфигов слишком много, чтобы уместить в одно сообщение "
        "целиком — показана только часть."
        if truncated
        else ""
    )
    return (
        f"📋 <b>Все конфиги подписки «{record['name']}»</b>{warn}\n\n"
        "Нажми на текст ниже, чтобы скопировать:\n\n"
        f"<code>{configs_text}</code>"
    )


# ============================================================
# РОУТЕР И ХЕНДЛЕРЫ
# ============================================================

router = Router()


class RegistrationMiddleware(BaseMiddleware):
    """Гейт для всего бота: сначала проверяет бан (блокирует вообще всё,
    включая /start), затем — что пользователь придумал уникальный ник,
    затем — что после смены ника подписки перенесены или удалены.
    Пропускает только команду /start и сам процесс ввода ника, чтобы не
    зациклить регистрацию."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: Union[Message, CallbackQuery],
        data: Dict[str, Any],
    ) -> Any:
        user = event.from_user
        if user is None:
            return await handler(event, data)

        user_record = await db_get_user(user.id)

        if user_record and user_record.get("banned"):
            reason = user_record.get("ban_reason") or "не указана"
            text = (
                "🚫 Вы забанены в этом боте.\n"
                f"Причина: {reason}\n\n"
                f"Если это ошибка напишите в поддержку {BOT_AUTHOR}."
            )
            if isinstance(event, CallbackQuery):
                await event.answer(text, show_alert=True)
            else:
                await event.answer(text)
            return None

        if isinstance(event, Message) and event.text and event.text.startswith("/start"):
            return await handler(event, data)

        state: FSMContext = data["state"]
        current_state = await state.get_state()
        if current_state == Registration.waiting_username.state:
            return await handler(event, data)

        if not user_record:
            if isinstance(event, CallbackQuery):
                await event.answer("Сначала нужно придумать ник", show_alert=True)
                sent = await event.message.answer(ASK_USERNAME_TEXT)
                bot_message_id = sent.message_id
            else:
                sent = await event.answer(ASK_USERNAME_TEXT)
                bot_message_id = sent.message_id
            await state.set_state(Registration.waiting_username)
            await state.update_data(bot_message_id=bot_message_id)
            return None

        data["username"] = user_record["username"]

        # После смены ника блокируем весь бот, кроме раздела "Мои подписки"
        # и самого действия переноса/удаления — пока пользователь не решит,
        # что делать со старыми подписками.
        if user.id != TESTER_USER_ID and await has_pending_migration(user.id, user_record["username"]):
            allowed_callbacks = {"my_subs", "restore_subs_yes", "restore_subs_no", "menu"}
            if isinstance(event, CallbackQuery):
                if event.data not in allowed_callbacks:
                    await event.answer(PENDING_MIGRATION_TEXT, show_alert=True)
                    return None
            else:
                await event.answer(PENDING_MIGRATION_TEXT)
                return None

        return await handler(event, data)


router.message.middleware(RegistrationMiddleware())
router.callback_query.middleware(RegistrationMiddleware())


@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    user_record = await db_get_user(message.from_user.id)
    if user_record:
        await message.answer(WELCOME_TEXT, reply_markup=main_menu_kb())
        return
    sent = await message.answer(ASK_USERNAME_TEXT)
    await state.set_state(Registration.waiting_username)
    await state.update_data(bot_message_id=sent.message_id)


@router.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext):
    """Админ-панель: доступна только TESTER_USER_ID. Для всех остальных —
    вообще никакой реакции, команда как будто не существует."""
    if message.from_user.id != TESTER_USER_ID:
        return

    await state.clear()
    await message.answer("🛠 <b>Админ-панель</b>", reply_markup=admin_kb())


@router.callback_query(F.data == "admin_cancel")
async def cb_admin_cancel(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != TESTER_USER_ID:
        return
    await state.clear()
    await call.message.edit_text("🛠 <b>Админ-панель</b>", reply_markup=admin_kb())
    await call.answer()


@router.callback_query(F.data == "admin_reset_nick")
async def cb_admin_reset_nick(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != TESTER_USER_ID:
        return
    await state.clear()
    await db_delete_user(call.from_user.id)
    await call.answer("Ник сброшен ✅")
    await call.message.edit_text(
        "🔄 Твой ник сброшен. Напиши /start, чтобы зарегистрироваться заново."
    )


@router.callback_query(F.data == "admin_stats")
async def cb_admin_stats(call: CallbackQuery):
    if call.from_user.id != TESTER_USER_ID:
        return
    users_count = await db_count_users()
    subs_count = await db_count_all_subscriptions()
    await call.message.edit_text(
        "📊 <b>Статистика</b>\n\n"
        f"👥 Пользователей: <b>{users_count}</b>\n"
        f"🔑 Подписок всего: <b>{subs_count}</b>",
        reply_markup=admin_kb(),
    )
    await call.answer()


@router.callback_query(F.data == "admin_ban")
async def cb_admin_ban(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != TESTER_USER_ID:
        return
    await state.set_state(AdminPanel.waiting_ban_username)
    await state.update_data(bot_message_id=call.message.message_id)
    await call.message.edit_text(
        "🚫 Введи ник пользователя в боте, которого хочешь забанить.",
        reply_markup=admin_cancel_kb(),
    )
    await call.answer()


@router.message(StateFilter(AdminPanel.waiting_ban_username))
async def process_ban_username(message: Message, state: FSMContext, bot: Bot):
    if message.from_user.id != TESTER_USER_ID:
        return

    data = await state.get_data()
    bot_message_id = data.get("bot_message_id")

    typed = (message.text or "").strip()
    await message.delete()

    target = await db_get_user_by_username(typed.lower())
    if not target:
        await bot.edit_message_text(
            chat_id=message.chat.id,
            message_id=bot_message_id,
            text=f"⚠️ Пользователь с ником «{typed}» не найден. Попробуй ещё раз.",
            reply_markup=admin_cancel_kb(),
        )
        return

    if target["user_id"] == TESTER_USER_ID:
        await bot.edit_message_text(
            chat_id=message.chat.id,
            message_id=bot_message_id,
            text="⚠️ Нельзя забанить самого себя. Введи другой ник.",
            reply_markup=admin_cancel_kb(),
        )
        return

    if target.get("banned"):
        await bot.edit_message_text(
            chat_id=message.chat.id,
            message_id=bot_message_id,
            text=f"⚠️ Пользователь «{target['username']}» уже забанен. Введи другой ник.",
            reply_markup=admin_cancel_kb(),
        )
        return

    await state.update_data(
        ban_target_id=target["user_id"], ban_target_username=target["username"]
    )
    await state.set_state(AdminPanel.waiting_ban_reason)
    await bot.edit_message_text(
        chat_id=message.chat.id,
        message_id=bot_message_id,
        text=f"🚫 Бан пользователя «{target['username']}».\n\nНапиши причину бана.",
        reply_markup=admin_cancel_kb(),
    )


@router.message(StateFilter(AdminPanel.waiting_ban_reason))
async def process_ban_reason(message: Message, state: FSMContext, bot: Bot):
    if message.from_user.id != TESTER_USER_ID:
        return

    data = await state.get_data()
    bot_message_id = data.get("bot_message_id")
    target_id = data.get("ban_target_id")
    target_username = data.get("ban_target_username")

    reason = (message.text or "").strip()
    await message.delete()

    if not reason:
        await bot.edit_message_text(
            chat_id=message.chat.id,
            message_id=bot_message_id,
            text="⚠️ Причина не может быть пустой. Напиши причину бана.",
            reply_markup=admin_cancel_kb(),
        )
        return

    await db_set_ban(target_id, True, reason)
    await state.clear()
    await bot.edit_message_text(
        chat_id=message.chat.id,
        message_id=bot_message_id,
        text=f"✅ Пользователь «{target_username}» забанен.\nПричина: {reason}",
        reply_markup=admin_kb(),
    )


@router.callback_query(F.data == "admin_unban")
async def cb_admin_unban(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != TESTER_USER_ID:
        return
    await state.set_state(AdminPanel.waiting_unban_username)
    await state.update_data(bot_message_id=call.message.message_id)
    await call.message.edit_text(
        "✅ Введи ник пользователя в боте, которого хочешь разбанить.",
        reply_markup=admin_cancel_kb(),
    )
    await call.answer()


@router.message(StateFilter(AdminPanel.waiting_unban_username))
async def process_unban_username(message: Message, state: FSMContext, bot: Bot):
    if message.from_user.id != TESTER_USER_ID:
        return

    data = await state.get_data()
    bot_message_id = data.get("bot_message_id")

    typed = (message.text or "").strip()
    await message.delete()

    target = await db_get_user_by_username(typed.lower())
    if not target:
        await bot.edit_message_text(
            chat_id=message.chat.id,
            message_id=bot_message_id,
            text=f"⚠️ Пользователь с ником «{typed}» не найден. Попробуй ещё раз.",
            reply_markup=admin_cancel_kb(),
        )
        return

    if not target.get("banned"):
        await bot.edit_message_text(
            chat_id=message.chat.id,
            message_id=bot_message_id,
            text=f"⚠️ Пользователь «{target['username']}» и так не забанен. Введи другой ник.",
            reply_markup=admin_cancel_kb(),
        )
        return

    await db_set_ban(target["user_id"], False, None)
    await state.clear()
    await bot.edit_message_text(
        chat_id=message.chat.id,
        message_id=bot_message_id,
        text=f"✅ Пользователь «{target['username']}» разбанен.",
        reply_markup=admin_kb(),
    )


@router.callback_query(F.data == "admin_broadcast")
async def cb_admin_broadcast(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != TESTER_USER_ID:
        return
    await state.set_state(AdminPanel.waiting_broadcast_message)
    await state.update_data(bot_message_id=call.message.message_id)
    await call.message.edit_text(
        "📢 Напиши сообщение, которое хочешь разослать всем пользователям бота.",
        reply_markup=admin_cancel_kb(),
    )
    await call.answer()


async def _run_broadcast(bot: Bot, admin_chat_id: int, bot_message_id: int, text: str) -> None:
    """Рассылает сообщение всем пользователям в фоне, не блокируя админ-панель.
    Небольшая пауза между отправками — чтобы не упереться в лимиты Telegram
    на количество сообщений в секунду при большом числе пользователей."""
    user_ids = await db_list_all_user_ids()
    success = 0
    failed = 0
    for user_id in user_ids:
        try:
            await bot.send_message(user_id, text, parse_mode=None)
            success += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)

    await bot.edit_message_text(
        chat_id=admin_chat_id,
        message_id=bot_message_id,
        text=f"✅ Рассылка завершена.\nДоставлено: {success}\nОшибок: {failed}",
        reply_markup=admin_kb(),
    )


@router.message(StateFilter(AdminPanel.waiting_broadcast_message))
async def process_broadcast_message(message: Message, state: FSMContext, bot: Bot):
    if message.from_user.id != TESTER_USER_ID:
        return

    data = await state.get_data()
    bot_message_id = data.get("bot_message_id")

    text = message.text or ""
    await message.delete()

    if not text.strip():
        await bot.edit_message_text(
            chat_id=message.chat.id,
            message_id=bot_message_id,
            text="⚠️ Сообщение не может быть пустым. Напиши текст рассылки.",
            reply_markup=admin_cancel_kb(),
        )
        return

    await state.clear()
    users_count = await db_count_users()
    await bot.edit_message_text(
        chat_id=message.chat.id,
        message_id=bot_message_id,
        text=f"⏳ Рассылка запущена в фоне. Получателей: {users_count}.",
        reply_markup=admin_kb(),
    )
    asyncio.create_task(_run_broadcast(bot, message.chat.id, bot_message_id, text))


@router.message(StateFilter(Registration.waiting_username))
async def process_username(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    bot_message_id = data.get("bot_message_id")

    raw = message.text or ""
    username = validate_username(raw)
    await message.delete()

    if not username:
        await bot.edit_message_text(
            chat_id=message.chat.id,
            message_id=bot_message_id,
            text=INVALID_USERNAME_TEXT,
        )
        return

    if await db_username_taken(username.lower()):
        await bot.edit_message_text(
            chat_id=message.chat.id,
            message_id=bot_message_id,
            text=USERNAME_TAKEN_TEXT,
        )
        return

    created = await db_create_user(message.from_user.id, username)
    if not created:
        # Кто-то занял этот ник за долю секунды до нас — просим выбрать другой.
        await bot.edit_message_text(
            chat_id=message.chat.id,
            message_id=bot_message_id,
            text=USERNAME_TAKEN_TEXT,
        )
        return

    await state.clear()
    await bot.edit_message_text(
        chat_id=message.chat.id,
        message_id=bot_message_id,
        text=f"✅ Ник <b>{username}</b> сохранён!\n\n" + WELCOME_TEXT,
        reply_markup=main_menu_kb(),
    )


@router.callback_query(F.data == "menu")
async def cb_menu(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text(WELCOME_TEXT, reply_markup=main_menu_kb())
    await call.answer()


@router.callback_query(F.data == "about")
async def cb_about(call: CallbackQuery):
    await call.message.edit_text(ABOUT_TEXT, reply_markup=back_to_menu_kb())
    await call.answer()


@router.callback_query(F.data == "support_project")
async def cb_support(call: CallbackQuery):
    await call.message.edit_text(SUPPORT_TEXT, reply_markup=back_to_menu_kb())
    await call.answer()


# ============================================================
# НАСТРОЙКИ
# ============================================================

@router.callback_query(F.data == "settings")
async def cb_settings(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text(SETTINGS_TEXT, reply_markup=settings_kb())
    await call.answer()


@router.callback_query(F.data == "settings_profile")
async def cb_settings_profile(call: CallbackQuery):
    user_record = await db_get_user(call.from_user.id)
    if not user_record:
        await call.answer("Профиль не найден", show_alert=True)
        return

    subs_count = await db_count_subscriptions(call.from_user.id)
    created_at = (user_record.get("created_at") or "")[:10]

    text = (
        "📊 <b>Мой профиль</b>\n\n"
        f"📛 Ник: <b>{user_record['username']}</b>\n"
        f"🆔 Telegram ID: <code>{call.from_user.id}</code>\n"
        f"📅 Зарегистрирован: {created_at}\n"
        f"🔑 Подписок: {subs_count}"
    )
    await call.message.edit_text(text, reply_markup=settings_kb())
    await call.answer()


@router.callback_query(F.data == "settings_wipe_subs")
async def cb_settings_wipe_subs(call: CallbackQuery):
    count = await db_count_subscriptions(call.from_user.id)
    if count == 0:
        await call.answer("У тебя нет подписок для удаления", show_alert=True)
        return

    await call.message.edit_text(
        f"🗑 Удалить <b>ВСЕ</b> твои подписки ({count} шт.)? Это необратимо.",
        reply_markup=yes_no_kb("wipe_subs_yes", "wipe_subs_no"),
    )
    await call.answer()


@router.callback_query(F.data == "wipe_subs_yes")
async def cb_wipe_subs_yes(call: CallbackQuery):
    records = await db_list_subscriptions(call.from_user.id, limit=100_000)
    for r in records:
        try:
            await github_uploader.delete_file(r["file_path"])
        except Exception:
            logger.exception(f"Не удалось удалить файл подписки id={r['id']} на GitHub")
        await db_delete_subscription(r["id"], call.from_user.id)

    await call.answer("Все подписки удалены ✅")
    await call.message.edit_text(SETTINGS_TEXT, reply_markup=settings_kb())


@router.callback_query(F.data == "wipe_subs_no")
async def cb_wipe_subs_no(call: CallbackQuery):
    await call.answer("Отменено")
    await call.message.edit_text(SETTINGS_TEXT, reply_markup=settings_kb())


@router.callback_query(F.data == "settings_cancel_change")
async def cb_settings_cancel_change(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text(SETTINGS_TEXT, reply_markup=settings_kb())
    await call.answer()


@router.callback_query(F.data == "settings_change_username")
async def cb_settings_change_username(call: CallbackQuery, state: FSMContext):
    user_record = await db_get_user(call.from_user.id)
    if not user_record:
        await call.answer("Профиль не найден", show_alert=True)
        return

    await state.set_state(ChangeUsername.waiting_confirm_current)
    await state.update_data(
        bot_message_id=call.message.message_id,
        current_username=user_record["username"],
    )
    await call.message.edit_text(
        ASK_CONFIRM_CURRENT_USERNAME_TEXT.format(current=user_record["username"]),
        reply_markup=cancel_kb(cancel_callback="settings_cancel_change"),
    )
    await call.answer()


@router.message(StateFilter(ChangeUsername.waiting_confirm_current))
async def process_confirm_current_username(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    bot_message_id = data.get("bot_message_id")
    current_username = data.get("current_username", "")

    typed = (message.text or "").strip()
    await message.delete()

    if typed.lower() != current_username.lower():
        await bot.edit_message_text(
            chat_id=message.chat.id,
            message_id=bot_message_id,
            text=CONFIRM_CURRENT_USERNAME_MISMATCH_TEXT.format(current=current_username),
            reply_markup=cancel_kb(cancel_callback="settings_cancel_change"),
        )
        return

    await state.set_state(ChangeUsername.waiting_new_username)
    await bot.edit_message_text(
        chat_id=message.chat.id,
        message_id=bot_message_id,
        text=ASK_NEW_USERNAME_TEXT,
        reply_markup=cancel_kb(cancel_callback="settings_cancel_change"),
    )


@router.message(StateFilter(ChangeUsername.waiting_new_username))
async def process_new_username(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    bot_message_id = data.get("bot_message_id")
    current_username = data.get("current_username", "")

    raw = message.text or ""
    new_username = validate_username(raw)
    await message.delete()

    if not new_username:
        await bot.edit_message_text(
            chat_id=message.chat.id,
            message_id=bot_message_id,
            text=CHANGE_USERNAME_INVALID_TEXT,
            reply_markup=cancel_kb(cancel_callback="settings_cancel_change"),
        )
        return

    if new_username.lower() == current_username.lower():
        await bot.edit_message_text(
            chat_id=message.chat.id,
            message_id=bot_message_id,
            text=NEW_USERNAME_SAME_AS_OLD_TEXT,
            reply_markup=cancel_kb(cancel_callback="settings_cancel_change"),
        )
        return

    if await db_username_taken(new_username.lower()):
        await bot.edit_message_text(
            chat_id=message.chat.id,
            message_id=bot_message_id,
            text=CHANGE_USERNAME_TAKEN_TEXT,
            reply_markup=cancel_kb(cancel_callback="settings_cancel_change"),
        )
        return

    await state.update_data(new_username=new_username)
    await state.set_state(ChangeUsername.waiting_confirm_change)
    await bot.edit_message_text(
        chat_id=message.chat.id,
        message_id=bot_message_id,
        text=confirm_change_username_text(current_username, new_username),
        reply_markup=yes_no_kb("confirm_change_username_yes", "confirm_change_username_no"),
    )


@router.callback_query(F.data == "confirm_change_username_yes", StateFilter(ChangeUsername.waiting_confirm_change))
async def cb_confirm_change_username_yes(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    new_username = data.get("new_username")

    if not new_username:
        await call.answer("Что-то пошло не так, начни заново", show_alert=True)
        await state.clear()
        await call.message.edit_text(SETTINGS_TEXT, reply_markup=settings_kb())
        return

    renamed = await db_rename_user(call.from_user.id, new_username)
    await state.clear()

    if not renamed:
        await call.answer("Этот ник только что заняли", show_alert=True)
        await call.message.edit_text(SETTINGS_TEXT, reply_markup=settings_kb())
        return

    await call.answer("Ник изменён ✅")

    # Если есть подписки под старым ником, сразу отправляем в "Мои подписки",
    # где предложим перенести их на новый ник или удалить.
    if await has_pending_migration(call.from_user.id, new_username):
        await render_my_subs(call)
        return

    await call.message.edit_text(
        f"✅ Ник изменён на <b>{new_username}</b>!\n\n" + WELCOME_TEXT,
        reply_markup=main_menu_kb(),
    )


@router.callback_query(F.data == "confirm_change_username_no", StateFilter(ChangeUsername.waiting_confirm_change))
async def cb_confirm_change_username_no(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.answer("Отменено")
    await call.message.edit_text(SETTINGS_TEXT, reply_markup=settings_kb())


async def migrate_subscription_to_new_username(record: dict, new_username: str) -> dict:
    """Перезаливает файл подписки на GitHub под новым ником и удаляет старый
    файл (тем самым старая папка ника на GitHub полностью освобождается,
    когда перенесены все подписки), возвращает обновлённые поля
    (file_path/raw_url/display_url) для db_update_subscription."""
    old_file_path = record["file_path"]
    suffix_with_ext = old_file_path.rsplit("/", 1)[-1]  # "abc1234.txt"
    suffix = suffix_with_ext[:-4] if suffix_with_ext.endswith(".txt") else suffix_with_ext
    new_filename = f"{new_username}/{suffix_with_ext}"

    configs = record["configs_text"].splitlines() if record["configs_text"] else []
    expire_dt = date.fromisoformat(record["expire_date_iso"]) if record.get("expire_date_iso") else None

    content = build_subscription_content(
        title=record["name"],
        description=record["description"] or "",
        url=record["url"] or "",
        interval_hours=record["interval_hours"],
        expire_dt=expire_dt,
        configs=configs,
    )

    new_raw_url, new_file_path = await github_uploader.upload_text_file(new_filename, content)

    try:
        await github_uploader.delete_file(old_file_path)
    except Exception:
        logger.exception(f"Не удалось удалить старый файл {old_file_path} при переносе подписки")

    new_display_url = build_display_url(
        record.get("link_style") or "github", new_raw_url, new_username, suffix
    )

    return {
        "file_path": new_file_path,
        "raw_url": new_raw_url,
        "display_url": new_display_url,
    }


@router.callback_query(F.data == "restore_subs_yes")
async def cb_restore_subs_yes(call: CallbackQuery):
    user_record = await db_get_user(call.from_user.id)
    if not user_record:
        await call.answer("Профиль не найден", show_alert=True)
        return
    new_username = user_record["username"]

    records = await db_list_subscriptions(call.from_user.id, limit=100_000)
    await call.message.edit_text("⏳ Переношу подписки на новый ник…")
    await call.answer()

    success = 0
    failed = 0
    for r in records:
        try:
            updates = await migrate_subscription_to_new_username(r, new_username)
            await db_update_subscription(r["id"], call.from_user.id, **updates)
            success += 1
        except Exception:
            logger.exception(f"Не удалось перенести подписку id={r['id']}")
            failed += 1

    if failed:
        await call.message.edit_text(
            f"⚠️ Перенесено {success} из {success + failed}. "
            f"{failed} подписок перенести не удалось — попробуй позже.",
            reply_markup=back_to_menu_kb(),
        )
        return

    await render_my_subs(call)


@router.callback_query(F.data == "restore_subs_no")
async def cb_restore_subs_no(call: CallbackQuery):
    records = await db_list_subscriptions(call.from_user.id, limit=100_000)
    for r in records:
        try:
            await github_uploader.delete_file(r["file_path"])
        except Exception:
            logger.exception(f"Не удалось удалить файл подписки id={r['id']} на GitHub")
        await db_delete_subscription(r["id"], call.from_user.id)

    await call.answer()
    await render_my_subs(call)


async def render_my_subs(call: CallbackQuery) -> None:
    user_record = await db_get_user(call.from_user.id)
    current_username = user_record["username"] if user_record else None

    records = await db_list_subscriptions(call.from_user.id)
    total = await db_count_subscriptions(call.from_user.id)

    if not records:
        await call.message.edit_text(
            f"📂 <b>Мои подписки</b> ({total}/{MAX_SUBS_PER_USER})\n\n"
            "Пока пусто ты ещё не создавал подписки.\n\n"
            "✅ Еще нечего удалять :(",
            reply_markup=back_to_menu_kb(),
        )
        return

    # После смены ника старые подписки физически лежат в папке под старым
    # ником на GitHub — показываем их как есть нельзя, предлагаем перенос.
    if current_username and await has_pending_migration(call.from_user.id, current_username):
        await call.message.edit_text(
            SUBS_MISSING_AFTER_RENAME_TEXT,
            reply_markup=yes_no_kb("restore_subs_yes", "restore_subs_no"),
        )
        return

    blocks = []
    for r in records:
        blocks.append(
            f"📛 <b>{r['name']}</b>\n"
            f"📅 {r['expire_date']} · 🔑 {r['configs_count']} конфиг(ов)\n"
            f"🔗 <code>{record_link(r)}</code>"
        )

    text = f"📂 <b>Мои подписки</b> ({total}/{MAX_SUBS_PER_USER})\n\n" + "\n\n".join(blocks)

    if total > len(records):
        text += f"\n\n… показаны последние {len(records)} из {total}."

    text += "\n\n✅ Хочешь удалить какую-нибудь?"

    await call.message.edit_text(text, reply_markup=my_subs_kb(records))


@router.callback_query(F.data == "my_subs")
async def cb_my_subs(call: CallbackQuery):
    await render_my_subs(call)
    await call.answer()


@router.callback_query(F.data.startswith("del_sub:"))
async def cb_delete_sub(call: CallbackQuery):
    try:
        sub_id = int(call.data.split(":", 1)[1])
    except (IndexError, ValueError):
        await call.answer("Некорректный запрос", show_alert=True)
        return

    record = await db_get_subscription(sub_id, call.from_user.id)
    if not record:
        await call.answer("Подписка не найдена (возможно, уже удалена)", show_alert=True)
        await render_my_subs(call)
        return

    try:
        await github_uploader.delete_file(record["file_path"])
    except Exception:
        logger.exception("Ошибка удаления файла на GitHub")
        await call.answer("Не удалось удалить файл на GitHub, попробуй позже", show_alert=True)
        return

    await db_delete_subscription(sub_id, call.from_user.id)
    await call.answer("Подписка удалена ✅")
    await render_my_subs(call)


@router.callback_query(F.data == "cancel")
async def cb_cancel(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text("❌ Создание подписки отменено.", reply_markup=main_menu_kb())
    await call.answer()


@router.callback_query(F.data == "create_sub")
async def cb_create_sub(call: CallbackQuery, state: FSMContext):
    current_count = await db_count_subscriptions(call.from_user.id)
    if current_count >= MAX_SUBS_PER_USER:
        await call.answer(
            f"🚫 Лимит подписок исчерпан ({MAX_SUBS_PER_USER}/{MAX_SUBS_PER_USER}). "
            "Удали одну из существующих, чтобы создать новую.",
            show_alert=True,
        )
        return

    await state.set_state(CreateSub.waiting_name)
    await state.update_data(
        bot_message_id=call.message.message_id,
        configs=[],
        description="",
        url="",
        update_interval=DEFAULT_INTERVAL_HOURS,
    )
    await call.message.edit_text(ASK_NAME_TEXT, reply_markup=cancel_kb())
    await call.answer()


@router.message(StateFilter(CreateSub.waiting_name))
async def process_name(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    bot_message_id = data.get("bot_message_id")

    name = validate_sub_name(message.text or "")
    await message.delete()

    if not name:
        await bot.edit_message_text(
            chat_id=message.chat.id,
            message_id=bot_message_id,
            text=(
                f"⚠️ Название пустое, слишком длинное (>{MAX_NAME_LEN} символов) "
                "или содержит перенос строки.\n\n" + ASK_NAME_TEXT
            ),
            reply_markup=cancel_kb(),
        )
        return

    await state.update_data(sub_name=name)
    await state.set_state(CreateSub.waiting_description)
    await bot.edit_message_text(
        chat_id=message.chat.id,
        message_id=bot_message_id,
        text=ASK_DESCRIPTION_TEXT,
        reply_markup=skip_kb("skip_description"),
    )


@router.callback_query(F.data == "skip_description", StateFilter(CreateSub.waiting_description))
async def cb_skip_description(call: CallbackQuery, state: FSMContext):
    await state.update_data(description="")
    await state.set_state(CreateSub.waiting_url)
    await call.message.edit_text(ASK_URL_TEXT, reply_markup=skip_kb("skip_url"))
    await call.answer()


@router.message(StateFilter(CreateSub.waiting_description))
async def process_description(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    bot_message_id = data.get("bot_message_id")

    text = sanitize_free_text(message.text or "", MAX_DESCRIPTION_LEN)
    await message.delete()

    if not text:
        await bot.edit_message_text(
            chat_id=message.chat.id,
            message_id=bot_message_id,
            text="⚠️ Пустое описание. Напиши текст или нажми «Пропустить».\n\n" + ASK_DESCRIPTION_TEXT,
            reply_markup=skip_kb("skip_description"),
        )
        return

    await state.update_data(description=text)
    await state.set_state(CreateSub.waiting_url)
    await bot.edit_message_text(
        chat_id=message.chat.id,
        message_id=bot_message_id,
        text=ASK_URL_TEXT,
        reply_markup=skip_kb("skip_url"),
    )


@router.callback_query(F.data == "skip_url", StateFilter(CreateSub.waiting_url))
async def cb_skip_url(call: CallbackQuery, state: FSMContext):
    await state.update_data(url="")
    await state.set_state(CreateSub.waiting_interval)
    await call.message.edit_text(ASK_INTERVAL_TEXT, reply_markup=interval_kb())
    await call.answer()


@router.message(StateFilter(CreateSub.waiting_url))
async def process_url(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    bot_message_id = data.get("bot_message_id")

    url = validate_url(message.text or "")
    await message.delete()

    if not url:
        await bot.edit_message_text(
            chat_id=message.chat.id,
            message_id=bot_message_id,
            text=(
                "⚠️ Это не похоже на ссылку. Нужна ссылка вида "
                "<code>https://t.me/halyava_vpnz</code> (с http:// или https://).\n\n"
                + ASK_URL_TEXT
            ),
            reply_markup=skip_kb("skip_url"),
        )
        return

    await state.update_data(url=url)
    await state.set_state(CreateSub.waiting_interval)
    await bot.edit_message_text(
        chat_id=message.chat.id,
        message_id=bot_message_id,
        text=ASK_INTERVAL_TEXT,
        reply_markup=interval_kb(),
    )


@router.callback_query(F.data.startswith("interval_"), StateFilter(CreateSub.waiting_interval))
async def cb_interval_preset(call: CallbackQuery, state: FSMContext):
    suffix = call.data.split("_", 1)[1]
    hours = DEFAULT_INTERVAL_HOURS if suffix == "skip" else int(suffix)

    await state.update_data(update_interval=hours)
    await state.set_state(CreateSub.waiting_date)
    await call.message.edit_text(ASK_DATE_TEXT, reply_markup=skip_kb("skip_date"))
    await call.answer()


@router.message(StateFilter(CreateSub.waiting_interval))
async def process_interval(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    bot_message_id = data.get("bot_message_id")

    hours = validate_interval(message.text or "")
    await message.delete()

    if hours is None:
        await bot.edit_message_text(
            chat_id=message.chat.id,
            message_id=bot_message_id,
            text=(
                f"⚠️ Введи целое число часов от {MIN_INTERVAL_HOURS} до "
                f"{MAX_INTERVAL_HOURS}, или выбери вариант ниже.\n\n" + ASK_INTERVAL_TEXT
            ),
            reply_markup=interval_kb(),
        )
        return

    await state.update_data(update_interval=hours)
    await state.set_state(CreateSub.waiting_date)
    await bot.edit_message_text(
        chat_id=message.chat.id,
        message_id=bot_message_id,
        text=ASK_DATE_TEXT,
        reply_markup=skip_kb("skip_date"),
    )


@router.callback_query(F.data == "skip_date", StateFilter(CreateSub.waiting_date))
async def cb_skip_date(call: CallbackQuery, state: FSMContext):
    await state.update_data(expire_date=NO_EXPIRY_LABEL, expire_date_iso=None, no_expiry=True)
    await state.set_state(CreateSub.waiting_configs)
    await call.message.edit_text(ASK_CONFIGS_TEXT, reply_markup=cancel_kb())
    await call.answer()


@router.message(StateFilter(CreateSub.waiting_date))
async def process_date(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    bot_message_id = data.get("bot_message_id")

    parsed = validate_date(message.text or "")
    await message.delete()

    if not parsed:
        await bot.edit_message_text(
            chat_id=message.chat.id,
            message_id=bot_message_id,
            text=(
                "⚠️ Некорректная дата. Формат: ДД.ММ.ГГГГ, дата должна быть в будущем.\n\n"
                + ASK_DATE_TEXT
            ),
            reply_markup=skip_kb("skip_date"),
        )
        return

    await state.update_data(
        expire_date=parsed.strftime("%d.%m.%Y"),
        expire_date_iso=parsed.isoformat(),
        no_expiry=False,
    )
    await state.set_state(CreateSub.waiting_configs)
    await bot.edit_message_text(
        chat_id=message.chat.id,
        message_id=bot_message_id,
        text=ASK_CONFIGS_TEXT,
        reply_markup=cancel_kb(),
    )


@router.message(StateFilter(CreateSub.waiting_configs))
async def process_configs(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    bot_message_id = data.get("bot_message_id")
    existing_configs: list[str] = data.get("configs", [])

    raw_text = message.text or ""
    await message.delete()

    new_valid = extract_valid_configs(raw_text)

    if not new_valid:
        await bot.edit_message_text(
            chat_id=message.chat.id,
            message_id=bot_message_id,
            text=INVALID_CONFIGS_TEXT,
            reply_markup=cancel_kb(),
        )
        return

    seen = set(existing_configs)
    for c in new_valid:
        if c not in seen:
            seen.add(c)
            existing_configs.append(c)

    await state.update_data(configs=existing_configs)

    preview = "\n".join(existing_configs[:5])
    more = f"\n… и ещё {len(existing_configs) - 5}" if len(existing_configs) > 5 else ""

    text = (
        f"✅ Добавлено конфигов: <b>{len(existing_configs)}</b>\n\n"
        f"<code>{preview}{more}</code>\n\n"
        "Можешь прислать ещё конфиги или создать подписку."
    )

    await bot.edit_message_text(
        chat_id=message.chat.id,
        message_id=bot_message_id,
        text=text,
        reply_markup=confirm_configs_kb(),
    )


@router.callback_query(F.data == "add_more", StateFilter(CreateSub.waiting_configs))
async def cb_add_more(call: CallbackQuery, state: FSMContext):
    await call.message.edit_text(ASK_CONFIGS_TEXT, reply_markup=cancel_kb())
    await call.answer()


@router.callback_query(F.data == "confirm_create", StateFilter(CreateSub.waiting_configs))
async def cb_confirm_create(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    configs: list[str] = data.get("configs", [])

    if not configs:
        await call.answer("Нет конфигов для создания подписки", show_alert=True)
        return

    await state.set_state(CreateSub.waiting_link_style)
    await call.message.edit_text(ASK_LINK_STYLE_TEXT, reply_markup=link_style_kb())
    await call.answer()


@router.callback_query(F.data.startswith("linkstyle_"), StateFilter(CreateSub.waiting_link_style))
async def cb_link_style(call: CallbackQuery, state: FSMContext):
    style = call.data.split("_", 1)[1]
    await finalize_subscription_creation(call, state, style)


async def finalize_subscription_creation(call: CallbackQuery, state: FSMContext, link_style: str) -> None:
    data = await state.get_data()
    sub_name: str = data.get("sub_name", "subscription")
    description: str = data.get("description", "")
    url: str = data.get("url", "")
    interval_hours: int = data.get("update_interval", DEFAULT_INTERVAL_HOURS)
    expire_date_display: str = data.get("expire_date", NO_EXPIRY_LABEL)
    expire_date_iso: Optional[str] = data.get("expire_date_iso")
    no_expiry: bool = data.get("no_expiry", expire_date_iso is None)
    configs: list[str] = data.get("configs", [])

    if not configs:
        await call.answer("Нет конфигов для создания подписки", show_alert=True)
        return

    expire_dt: Optional[date] = None if no_expiry else date.fromisoformat(expire_date_iso)

    user_record = await db_get_user(call.from_user.id)
    username = user_record["username"] if user_record else f"user{call.from_user.id}"

    await call.message.edit_text("⏳ Создаю файл подписки и загружаю на GitHub…")
    await call.answer()

    content = build_subscription_content(
        title=sub_name,
        description=description,
        url=url,
        interval_hours=interval_hours,
        expire_dt=expire_dt,
        configs=configs,
    )

    # Случайный суффикс уникален для КАЖДОЙ подписки — используется в имени
    # файла на GitHub и в Vercel project slug, чтобы разные подписки одного
    # пользователя не затирали друг друга.
    rand_suffix = generate_random_suffix()

    # Все подписки пользователя лежат в ОДНОЙ его папке на GitHub:
    # subs/<ник>/<rand>.txt — новые подписки просто добавляются файлами
    # в эту же папку, не создавая папку заново.
    filename = f"{username}/{rand_suffix}.txt"

    try:
        raw_url, file_path = await github_uploader.upload_text_file(filename, content)
    except Exception as e:
        logger.exception("Ошибка загрузки на GitHub")
        await call.message.edit_text(
            f"❌ Не удалось загрузить подписку на GitHub:\n<code>{e}</code>",
            reply_markup=back_to_menu_kb(),
        )
        await state.clear()
        return

    display_url = raw_url
    link_note = ""

    if link_style == "vercel" and not VERCEL_GATEWAY_URL:
        link_note = "\n⚠️ Vercel-шлюз недоступен, использована обычная ссылка GitHub."
        link_style = "github"
    elif link_style in ("vercel", "cloudflare", "jsdelivr"):
        # Никаких вызовов внешних API — просто собираем URL по уже готовому
        # шаблону (Vercel-шлюз развёрнут заранее, Cloudflare/jsDelivr —
        # это публичные CDN-прокси поверх GitHub raw).
        display_url = build_display_url(link_style, raw_url, username, rand_suffix)

    await db_add_subscription(
        {
            "user_id": call.from_user.id,
            "name": sub_name,
            "description": description,
            "url": url,
            "interval_hours": interval_hours,
            "expire_date": expire_date_display,
            "expire_date_iso": expire_date_iso if not no_expiry else None,
            "configs_count": len(configs),
            "configs_text": "\n".join(configs),
            "raw_url": raw_url,
            "display_url": display_url,
            "link_style": link_style,
            "file_path": file_path,
            "created_at": datetime.now().isoformat(),
        }
    )

    description_line = description or "—"
    url_line = url or "—"
    style_label = LINK_STYLE_LABELS.get(link_style, "🐙 GitHub")

    result_text = (
        "🎉 <b>Подписка создана!</b>\n\n"
        f"📛 Название: <b>{sub_name}</b>\n"
        f"🗒 Описание: <b>{description_line}</b>\n"
        f"🔗 Ссылка: <b>{url_line}</b>\n"
        f"🔄 Автообновление: <b>{interval_hours} ч.</b>\n"
        f"📅 Действует до: <b>{expire_date_display}</b>\n"
        f"🔑 Конфигов: <b>{len(configs)}</b>\n"
        f"🎨 Стиль ссылки: <b>{style_label}</b>\n\n"
        f"🔗 Ссылка для подключения:\n<code>{display_url}</code>{link_note}\n\n"
        "Добавь эту ссылку как подписку в Happ, Hiddify, Incy, V2RayTun, "
        "V2Box, Streisand."
    )

    await call.message.edit_text(result_text, reply_markup=back_to_menu_kb())
    await state.clear()


async def render_edit_menu(call: CallbackQuery, record: dict, prefix: str = "") -> None:
    await call.message.edit_text(edit_summary_text(record, prefix), reply_markup=edit_field_kb())


@router.callback_query(F.data == "edit_subs")
async def cb_edit_subs(call: CallbackQuery, state: FSMContext):
    await state.clear()
    records = await db_list_subscriptions(call.from_user.id)

    if not records:
        await call.message.edit_text(
            "✏️ <b>Редактирование подписок</b>\n\n"
            "У тебя пока нет подписок для редактирования.",
            reply_markup=back_to_menu_kb(),
        )
        await call.answer()
        return

    await call.message.edit_text(
        "✏️ <b>Редактирование подписок</b>\n\nВыбери подписку:",
        reply_markup=edit_subs_kb(records),
    )
    await call.answer()


@router.callback_query(F.data.startswith("edit_pick:"))
async def cb_edit_pick(call: CallbackQuery, state: FSMContext):
    try:
        sub_id = int(call.data.split(":", 1)[1])
    except (IndexError, ValueError):
        await call.answer("Некорректный запрос", show_alert=True)
        return

    record = await db_get_subscription(sub_id, call.from_user.id)
    if not record:
        await call.answer("Подписка не найдена", show_alert=True)
        return

    await state.set_state(None)
    await state.update_data(edit_sub_id=sub_id, bot_message_id=call.message.message_id)
    await render_edit_menu(call, record)
    await call.answer()


@router.callback_query(F.data == "edit_cancel")
async def cb_edit_cancel(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    sub_id = data.get("edit_sub_id")

    if not sub_id:
        await state.clear()
        await call.message.edit_text(WELCOME_TEXT, reply_markup=main_menu_kb())
        await call.answer()
        return

    record = await db_get_subscription(sub_id, call.from_user.id)
    if not record:
        await state.clear()
        await call.message.edit_text(
            "Подписка не найдена (возможно, уже удалена).", reply_markup=back_to_menu_kb()
        )
        await call.answer()
        return

    await state.set_state(None)
    await render_edit_menu(call, record)
    await call.answer()


@router.callback_query(F.data == "edit_copy_configs")
async def cb_edit_copy_configs(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    sub_id = data.get("edit_sub_id")

    record = await db_get_subscription(sub_id, call.from_user.id) if sub_id else None
    if not record:
        await call.answer("Подписка не найдена", show_alert=True)
        return

    configs_text = record.get("configs_text") or ""
    if not configs_text:
        await call.answer("В этой подписке нет конфигов", show_alert=True)
        return

    # У Telegram Bot API нет метода, который копирует текст в буфер обмена
    # пользователя по нажатию кнопки, — единственный способ дать это сделать
    # одним тапом: показать конфиги моноширинным блоком, по нажатию на
    # который клиент Telegram сам предлагает "Скопировать".
    truncated = False
    shown = configs_text
    if len(copy_configs_text(record, shown, False)) > 4096:
        truncated = True
        overhead = len(copy_configs_text(record, "", True))
        shown = configs_text[: max(0, 4096 - overhead - 50)]

    await call.message.edit_text(
        copy_configs_text(record, shown, truncated),
        reply_markup=edit_copy_back_kb(),
    )
    await call.answer()


@router.callback_query(F.data == "edit_copy_back")
async def cb_edit_copy_back(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    sub_id = data.get("edit_sub_id")

    record = await db_get_subscription(sub_id, call.from_user.id) if sub_id else None
    if not record:
        await state.clear()
        await call.message.edit_text(WELCOME_TEXT, reply_markup=main_menu_kb())
        await call.answer()
        return

    await render_edit_menu(call, record)
    await call.answer()


async def apply_edit_update_call(call: CallbackQuery, state: FSMContext, **fields) -> None:
    data = await state.get_data()
    sub_id = data.get("edit_sub_id")
    user_id = call.from_user.id

    record = await db_get_subscription(sub_id, user_id)
    if not record:
        await call.answer("Подписка не найдена", show_alert=True)
        await state.clear()
        await call.message.edit_text(WELCOME_TEXT, reply_markup=main_menu_kb())
        return

    record.update(fields)
    await db_update_subscription(sub_id, user_id, **fields)

    configs = record["configs_text"].splitlines() if record["configs_text"] else []
    expire_dt = date.fromisoformat(record["expire_date_iso"]) if record.get("expire_date_iso") else None

    content = build_subscription_content(
        title=record["name"],
        description=record["description"] or "",
        url=record["url"] or "",
        interval_hours=record["interval_hours"],
        expire_dt=expire_dt,
        configs=configs,
    )

    try:
        await github_uploader.update_at_path(record["file_path"], content)
    except Exception:
        logger.exception("Ошибка обновления файла на GitHub")
        await call.answer("Не удалось обновить файл на GitHub, попробуй позже", show_alert=True)
        return

    await state.set_state(None)
    await call.answer("Сохранено ✅")
    await render_edit_menu(call, record, prefix="✅ Обновлено!\n\n")


async def apply_edit_update_message(message: Message, state: FSMContext, bot: Bot, **fields) -> None:
    data = await state.get_data()
    sub_id = data.get("edit_sub_id")
    bot_message_id = data.get("bot_message_id")
    user_id = message.from_user.id

    record = await db_get_subscription(sub_id, user_id)
    if not record:
        await bot.edit_message_text(
            chat_id=message.chat.id,
            message_id=bot_message_id,
            text="Подписка не найдена (возможно, уже удалена).",
            reply_markup=back_to_menu_kb(),
        )
        await state.clear()
        return

    record.update(fields)
    await db_update_subscription(sub_id, user_id, **fields)

    configs = record["configs_text"].splitlines() if record["configs_text"] else []
    expire_dt = date.fromisoformat(record["expire_date_iso"]) if record.get("expire_date_iso") else None

    content = build_subscription_content(
        title=record["name"],
        description=record["description"] or "",
        url=record["url"] or "",
        interval_hours=record["interval_hours"],
        expire_dt=expire_dt,
        configs=configs,
    )

    try:
        await github_uploader.update_at_path(record["file_path"], content)
    except Exception:
        logger.exception("Ошибка обновления файла на GitHub")
        await bot.edit_message_text(
            chat_id=message.chat.id,
            message_id=bot_message_id,
            text="❌ Не удалось обновить файл на GitHub, попробуй позже.",
            reply_markup=edit_field_kb(),
        )
        await state.set_state(None)
        return

    await state.set_state(None)
    await bot.edit_message_text(
        chat_id=message.chat.id,
        message_id=bot_message_id,
        text=edit_summary_text(record, prefix="✅ Обновлено!\n\n"),
        reply_markup=edit_field_kb(),
    )


@router.callback_query(F.data == "edit_field_name")
async def cb_edit_field_name(call: CallbackQuery, state: FSMContext):
    await state.set_state(EditSub.waiting_name)
    await call.message.edit_text(EDIT_ASK_NAME_TEXT, reply_markup=edit_field_cancel_kb())
    await call.answer()


@router.callback_query(F.data == "edit_field_description")
async def cb_edit_field_description(call: CallbackQuery, state: FSMContext):
    await state.set_state(EditSub.waiting_description)
    await call.message.edit_text(
        EDIT_ASK_DESCRIPTION_TEXT,
        reply_markup=edit_clear_kb("🗑 Убрать описание", "edit_skip_description"),
    )
    await call.answer()


@router.callback_query(F.data == "edit_field_url")
async def cb_edit_field_url(call: CallbackQuery, state: FSMContext):
    await state.set_state(EditSub.waiting_url)
    await call.message.edit_text(
        EDIT_ASK_URL_TEXT,
        reply_markup=edit_clear_kb("🗑 Убрать ссылку", "edit_skip_url"),
    )
    await call.answer()


@router.callback_query(F.data == "edit_field_interval")
async def cb_edit_field_interval(call: CallbackQuery, state: FSMContext):
    await state.set_state(EditSub.waiting_interval)
    await call.message.edit_text(
        EDIT_ASK_INTERVAL_TEXT,
        reply_markup=interval_kb(prefix="editinterval", cancel_callback="edit_cancel"),
    )
    await call.answer()


@router.callback_query(F.data == "edit_field_date")
async def cb_edit_field_date(call: CallbackQuery, state: FSMContext):
    await state.set_state(EditSub.waiting_date)
    await call.message.edit_text(
        EDIT_ASK_DATE_TEXT,
        reply_markup=edit_clear_kb("♾ Без ограничения по сроку", "edit_skip_date"),
    )
    await call.answer()


@router.callback_query(F.data == "edit_field_configs")
async def cb_edit_field_configs(call: CallbackQuery, state: FSMContext):
    await state.set_state(EditSub.waiting_configs)
    await call.message.edit_text(EDIT_ASK_CONFIGS_TEXT, reply_markup=edit_field_cancel_kb())
    await call.answer()


@router.message(StateFilter(EditSub.waiting_name))
async def process_edit_name(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    bot_message_id = data.get("bot_message_id")

    name = validate_sub_name(message.text or "")
    await message.delete()

    if not name:
        await bot.edit_message_text(
            chat_id=message.chat.id,
            message_id=bot_message_id,
            text=(
                f"⚠️ Название пустое, слишком длинное (>{MAX_NAME_LEN} символов) "
                "или содержит перенос строки.\n\n" + EDIT_ASK_NAME_TEXT
            ),
            reply_markup=edit_field_cancel_kb(),
        )
        return

    await apply_edit_update_message(message, state, bot, name=name)


@router.callback_query(F.data == "edit_skip_description", StateFilter(EditSub.waiting_description))
async def cb_edit_skip_description(call: CallbackQuery, state: FSMContext):
    await apply_edit_update_call(call, state, description="")


@router.message(StateFilter(EditSub.waiting_description))
async def process_edit_description(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    bot_message_id = data.get("bot_message_id")

    text = sanitize_free_text(message.text or "", MAX_DESCRIPTION_LEN)
    await message.delete()

    if not text:
        await bot.edit_message_text(
            chat_id=message.chat.id,
            message_id=bot_message_id,
            text="⚠️ Пустое описание.\n\n" + EDIT_ASK_DESCRIPTION_TEXT,
            reply_markup=edit_clear_kb("🗑 Убрать описание", "edit_skip_description"),
        )
        return

    await apply_edit_update_message(message, state, bot, description=text)


@router.callback_query(F.data == "edit_skip_url", StateFilter(EditSub.waiting_url))
async def cb_edit_skip_url(call: CallbackQuery, state: FSMContext):
    await apply_edit_update_call(call, state, url="")


@router.message(StateFilter(EditSub.waiting_url))
async def process_edit_url(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    bot_message_id = data.get("bot_message_id")

    url = validate_url(message.text or "")
    await message.delete()

    if not url:
        await bot.edit_message_text(
            chat_id=message.chat.id,
            message_id=bot_message_id,
            text=(
                "⚠️ Это не похоже на ссылку. Нужна ссылка вида "
                "<code>https://t.me/halyava_vpnz</code>.\n\n" + EDIT_ASK_URL_TEXT
            ),
            reply_markup=edit_clear_kb("🗑 Убрать ссылку", "edit_skip_url"),
        )
        return

    await apply_edit_update_message(message, state, bot, url=url)


@router.callback_query(F.data.startswith("editinterval_"), StateFilter(EditSub.waiting_interval))
async def cb_edit_interval_preset(call: CallbackQuery, state: FSMContext):
    suffix = call.data.split("_", 1)[1]
    hours = DEFAULT_INTERVAL_HOURS if suffix == "skip" else int(suffix)
    await apply_edit_update_call(call, state, interval_hours=hours)


@router.message(StateFilter(EditSub.waiting_interval))
async def process_edit_interval(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    bot_message_id = data.get("bot_message_id")

    hours = validate_interval(message.text or "")
    await message.delete()

    if hours is None:
        await bot.edit_message_text(
            chat_id=message.chat.id,
            message_id=bot_message_id,
            text=(
                f"⚠️ Введи целое число часов от {MIN_INTERVAL_HOURS} до "
                f"{MAX_INTERVAL_HOURS}, или выбери вариант ниже.\n\n" + EDIT_ASK_INTERVAL_TEXT
            ),
            reply_markup=interval_kb(prefix="editinterval", cancel_callback="edit_cancel"),
        )
        return

    await apply_edit_update_message(message, state, bot, interval_hours=hours)


@router.callback_query(F.data == "edit_skip_date", StateFilter(EditSub.waiting_date))
async def cb_edit_skip_date(call: CallbackQuery, state: FSMContext):
    await apply_edit_update_call(call, state, expire_date=NO_EXPIRY_LABEL, expire_date_iso=None)


@router.message(StateFilter(EditSub.waiting_date))
async def process_edit_date(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    bot_message_id = data.get("bot_message_id")

    parsed = validate_date(message.text or "")
    await message.delete()

    if not parsed:
        await bot.edit_message_text(
            chat_id=message.chat.id,
            message_id=bot_message_id,
            text="⚠️ Некорректная дата. Формат: ДД.ММ.ГГГГ, дата должна быть в будущем.\n\n"
            + EDIT_ASK_DATE_TEXT,
            reply_markup=edit_clear_kb("♾ Без ограничения по сроку", "edit_skip_date"),
        )
        return

    await apply_edit_update_message(
        message,
        state,
        bot,
        expire_date=parsed.strftime("%d.%m.%Y"),
        expire_date_iso=parsed.isoformat(),
    )


@router.message(StateFilter(EditSub.waiting_configs))
async def process_edit_configs(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    bot_message_id = data.get("bot_message_id")

    raw_text = message.text or ""
    await message.delete()

    new_valid = extract_valid_configs(raw_text)

    if not new_valid:
        await bot.edit_message_text(
            chat_id=message.chat.id,
            message_id=bot_message_id,
            text=INVALID_CONFIGS_TEXT,
            reply_markup=edit_field_cancel_kb(),
        )
        return

    await apply_edit_update_message(
        message,
        state,
        bot,
        configs_text="\n".join(new_valid),
        configs_count=len(new_valid),
    )


@router.message(StateFilter(None))
async def fallback_message(message: Message):
    await message.answer(
        "Не понимаю эту команду 🙂\nНажми /start, чтобы открыть меню.",
        reply_markup=main_menu_kb(),
    )


# ============================================================
# ЗАПУСК
# ============================================================

async def init_vercel_gateway() -> None:
    """Один раз (за всё время жизни бота, не за сессию) деплоит проект-шлюз
    на Vercel и запоминает его URL в settings. При последующих запусках
    бота просто читает URL из БД — новый деплой не создаётся, поэтому
    лимит 100 деплойментов/сутки на создание подписок больше не тратится."""
    global VERCEL_GATEWAY_URL

    if not vercel_uploader.configured:
        logger.info("VERCEL_TOKEN не задан — стиль ссылки Vercel будет недоступен")
        return

    cached = await db_get_setting("vercel_gateway_url")
    if cached:
        VERCEL_GATEWAY_URL = cached
        logger.info(f"Vercel-шлюз уже развёрнут: {cached}")
        return

    try:
        gateway_url = await vercel_uploader.deploy_gateway(VERCEL_GATEWAY_PROJECT_SLUG)
        await vercel_uploader.disable_deployment_protection(VERCEL_GATEWAY_PROJECT_SLUG)
        await db_set_setting("vercel_gateway_url", gateway_url)
        VERCEL_GATEWAY_URL = gateway_url
        logger.info(f"Vercel-шлюз развёрнут: {gateway_url}")
    except Exception:
        logger.exception("Не удалось развернуть Vercel-шлюз — стиль ссылки Vercel будет недоступен")


async def main():
    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode="HTML"),
    )
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    await init_vercel_gateway()

    logger.info("SubsForgeBot запущен")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
