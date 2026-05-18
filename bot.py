#!/usr/bin/env python3
"""텔레그램 멀티그룹 밴 관리 봇"""

import json
import logging
import os
import sqlite3
from datetime import datetime

from dotenv import load_dotenv

load_dotenv()

from telegram import Chat, Update
from telegram.constants import ChatMemberStatus
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import Application, ContextTypes, MessageHandler, filters

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "banbot.db")


# ── Database ──────────────────────────────────────────────────────────────────

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS groups (
                chat_id  INTEGER PRIMARY KEY,
                title    TEXT,
                settings TEXT DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS bans (
                user_id        INTEGER PRIMARY KEY,
                username       TEXT,
                first_name     TEXT,
                reason         TEXT,
                banned_by      INTEGER,
                banned_by_name TEXT,
                banned_at      TEXT
            );
            CREATE TABLE IF NOT EXISTS managers (
                user_id    INTEGER,
                chat_id    INTEGER,
                username   TEXT,
                first_name TEXT,
                added_by   INTEGER,
                added_at   TEXT,
                PRIMARY KEY (user_id, chat_id)
            );
        """)


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


# Groups
def register_group(chat_id: int, title: str):
    with _conn() as c:
        c.execute("INSERT OR IGNORE INTO groups (chat_id, title) VALUES (?,?)", (chat_id, title))
        c.execute("UPDATE groups SET title=? WHERE chat_id=?", (title, chat_id))


def all_group_ids() -> list[int]:
    with _conn() as c:
        return [r["chat_id"] for r in c.execute("SELECT chat_id FROM groups")]


# Bans
def get_ban(uid: int):
    with _conn() as c:
        return c.execute("SELECT * FROM bans WHERE user_id=?", (uid,)).fetchone()


def save_ban(uid: int, username, first_name, reason: str, by_id: int, by_name: str):
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO bans VALUES (?,?,?,?,?,?,?)",
            (uid, username, first_name, reason, by_id, by_name,
             datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )


def del_ban(uid: int) -> bool:
    with _conn() as c:
        return c.execute("DELETE FROM bans WHERE user_id=?", (uid,)).rowcount > 0


# Settings
def get_settings(chat_id: int) -> dict:
    with _conn() as c:
        row = c.execute("SELECT settings FROM groups WHERE chat_id=?", (chat_id,)).fetchone()
    return json.loads(row["settings"]) if row else {}


def put_setting(chat_id: int, key: str, val: str):
    s = get_settings(chat_id)
    s[key] = val
    with _conn() as c:
        c.execute("UPDATE groups SET settings=? WHERE chat_id=?",
                  (json.dumps(s, ensure_ascii=False), chat_id))


def pop_setting(chat_id: int, key: str) -> bool:
    s = get_settings(chat_id)
    if key not in s:
        return False
    del s[key]
    with _conn() as c:
        c.execute("UPDATE groups SET settings=? WHERE chat_id=?",
                  (json.dumps(s, ensure_ascii=False), chat_id))
    return True


# Managers
def is_manager(uid: int, chat_id: int) -> bool:
    with _conn() as c:
        return c.execute(
            "SELECT 1 FROM managers WHERE user_id=? AND chat_id=?", (uid, chat_id)
        ).fetchone() is not None


def add_manager(uid: int, chat_id: int, username, first_name, by_id: int):
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO managers VALUES (?,?,?,?,?,?)",
            (uid, chat_id, username, first_name, by_id,
             datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )


def rm_manager(uid: int, chat_id: int) -> bool:
    with _conn() as c:
        return c.execute(
            "DELETE FROM managers WHERE user_id=? AND chat_id=?", (uid, chat_id)
        ).rowcount > 0


# ── Helpers ───────────────────────────────────────────────────────────────────

def fmt_user(u) -> str:
    name = getattr(u, "full_name", None) or getattr(u, "first_name", "") or ""
    uname = getattr(u, "username", None)
    return f"{name} (@{uname})" if uname else f"{name} [ID: {u.id}]"


async def admin_or_manager(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    uid = update.effective_user.id
    cid = update.effective_chat.id
    try:
        m = await context.bot.get_chat_member(cid, uid)
        if m.status in (ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR):
            return True
    except TelegramError:
        pass
    return is_manager(uid, cid)


async def only_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    uid = update.effective_user.id
    cid = update.effective_chat.id
    try:
        m = await context.bot.get_chat_member(cid, uid)
        return m.status in (ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR)
    except TelegramError:
        return False


async def find_user(context: ContextTypes.DEFAULT_TYPE, token: str, chat_id: int):
    """@username 또는 숫자 ID로 user 객체 반환, 없으면 None"""
    try:
        key = token if token.startswith("@") else int(token)
        return (await context.bot.get_chat_member(chat_id, key)).user
    except (TelegramError, ValueError):
        pass

    # 현재 그룹에 없어도 DB에서 찾기 (ban/unban 대상이었던 경우)
    with _conn() as c:
        if token.startswith("@"):
            row = c.execute(
                "SELECT user_id id, username, first_name FROM bans WHERE username=?",
                (token[1:],),
            ).fetchone()
        else:
            try:
                row = c.execute(
                    "SELECT user_id id, username, first_name FROM bans WHERE user_id=?",
                    (int(token),),
                ).fetchone()
            except ValueError:
                row = None

    if row:
        class _U:
            id = row["id"]
            username = row["username"]
            first_name = row["first_name"]
            full_name = row["first_name"] or ""
        return _U()

    return None


async def do_ban_all(context: ContextTypes.DEFAULT_TYPE, uid: int) -> dict:
    ok, fail = [], []
    for gid in all_group_ids():
        try:
            await context.bot.ban_chat_member(gid, uid)
            ok.append(gid)
        except (Forbidden, BadRequest, TelegramError) as e:
            fail.append((gid, str(e)))
    return {"ok": ok, "fail": fail}


async def do_unban_all(context: ContextTypes.DEFAULT_TYPE, uid: int) -> dict:
    ok, fail = [], []
    for gid in all_group_ids():
        try:
            await context.bot.unban_chat_member(gid, uid, only_if_banned=True)
            ok.append(gid)
        except (Forbidden, BadRequest, TelegramError) as e:
            fail.append((gid, str(e)))
    return {"ok": ok, "fail": fail}


# ── Main message handler ──────────────────────────────────────────────────────

COMMANDS = {".ban", ".unban", ".review", ".set", ".unset", ".manager", ".unmanager"}


async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.text:
        return

    chat = update.effective_chat
    text = msg.text.strip()

    if chat.type in (Chat.GROUP, Chat.SUPERGROUP):
        register_group(chat.id, chat.title or "")

    if not text.startswith("."):
        return

    parts = text.split(None, 1)
    cmd = parts[0][1:].lower()
    rest = parts[1].strip() if len(parts) > 1 else ""

    if f".{cmd}" not in COMMANDS:
        return

    # ── .ban ─────────────────────────────────────────────────────────────────
    if cmd == "ban":
        if not await admin_or_manager(update, context):
            await msg.reply_text("❌ 권한이 없습니다.")
            return

        target = None
        reason = "사유 없음"

        if msg.reply_to_message:
            target = msg.reply_to_message.from_user
            reason = rest or "사유 없음"
        elif rest:
            toks = rest.split(None, 1)
            target = await find_user(context, toks[0], chat.id)
            reason = toks[1].strip() if len(toks) > 1 else "사유 없음"

        if not target:
            await msg.reply_text(
                "❌ 대상을 찾을 수 없습니다.\n\n"
                "사용법:\n"
                "• `.ban @태그 사유`\n"
                "• `.ban 고유번호 사유`\n"
                "• 메시지에 답장 후 `.ban 사유`"
            )
            return

        if target.id == context.bot.id:
            await msg.reply_text("❌ 봇은 차단할 수 없습니다.")
            return

        by = update.effective_user
        save_ban(
            target.id,
            getattr(target, "username", None),
            getattr(target, "first_name", None),
            reason,
            by.id,
            fmt_user(by),
        )

        res = await do_ban_all(context, target.id)
        await msg.reply_text(
            f"🔨 **{fmt_user(target)}** 차단 완료\n"
            f"📋 사유: {reason}\n"
            f"✅ 적용: {len(res['ok'])}개 그룹"
            + (f"\n⚠️ 실패: {len(res['fail'])}개" if res["fail"] else ""),
            parse_mode="Markdown",
        )

    # ── .unban ───────────────────────────────────────────────────────────────
    elif cmd == "unban":
        if not await admin_or_manager(update, context):
            await msg.reply_text("❌ 권한이 없습니다.")
            return

        token = rest.split()[0] if rest else ""
        if not token:
            await msg.reply_text("❌ 사용법: `.unban @태그` 또는 `.unban 고유번호`")
            return

        target = await find_user(context, token, chat.id)
        uid = target.id if target else None
        if uid is None:
            try:
                uid = int(token)
            except ValueError:
                await msg.reply_text("❌ 사용자를 찾을 수 없습니다.")
                return

        removed = del_ban(uid)
        res = await do_unban_all(context, uid)
        name = fmt_user(target) if target else f"ID {uid}"

        await msg.reply_text(
            f"✅ **{name}** 차단 해제\n"
            f"✅ 적용: {len(res['ok'])}개 그룹"
            + (f"\n⚠️ 실패: {len(res['fail'])}개" if res["fail"] else "")
            + ("" if removed else "\n_(차단 기록 없었음)_"),
            parse_mode="Markdown",
        )

    # ── .review ──────────────────────────────────────────────────────────────
    elif cmd == "review":
        if not await admin_or_manager(update, context):
            await msg.reply_text("❌ 권한이 없습니다.")
            return

        uid = None
        label = "?"

        if msg.reply_to_message:
            uid = msg.reply_to_message.from_user.id
            label = fmt_user(msg.reply_to_message.from_user)
        elif rest:
            token = rest.split()[0]
            target = await find_user(context, token, chat.id)
            if target:
                uid = target.id
                label = fmt_user(target)
            else:
                try:
                    uid = int(token)
                    label = f"ID {uid}"
                except ValueError:
                    await msg.reply_text("❌ 사용자를 찾을 수 없습니다.")
                    return
        else:
            await msg.reply_text(
                "❌ 사용법:\n"
                "• `.review @태그`\n"
                "• `.review 고유번호`\n"
                "• 메시지에 답장 후 `.review`"
            )
            return

        ban = get_ban(uid)
        if ban:
            await msg.reply_text(
                f"📋 **차단 정보**\n"
                f"👤 {label}\n"
                f"🆔 ID: `{ban['user_id']}`\n"
                f"📝 사유: {ban['reason']}\n"
                f"🔨 차단자: {ban['banned_by_name']}\n"
                f"🕐 일시: {ban['banned_at']}",
                parse_mode="Markdown",
            )
        else:
            await msg.reply_text(f"✅ **{label}** 차단 기록 없음", parse_mode="Markdown")

    # ── .set ─────────────────────────────────────────────────────────────────
    elif cmd == "set":
        if not await admin_or_manager(update, context):
            await msg.reply_text("❌ 권한이 없습니다.")
            return

        if not rest:
            s = get_settings(chat.id)
            if s:
                lines = "\n".join(f"  `{k}` = `{v}`" for k, v in s.items())
                await msg.reply_text(f"⚙️ **현재 그룹 설정**\n{lines}", parse_mode="Markdown")
            else:
                await msg.reply_text("⚙️ 설정된 항목 없음\n사용법: `.set 키 값`")
            return

        toks = rest.split(None, 1)
        if len(toks) < 2:
            await msg.reply_text("❌ 사용법: `.set 키 값`")
            return

        put_setting(chat.id, toks[0], toks[1])
        await msg.reply_text(f"✅ `{toks[0]}` = `{toks[1]}`", parse_mode="Markdown")

    # ── .unset ───────────────────────────────────────────────────────────────
    elif cmd == "unset":
        if not await admin_or_manager(update, context):
            await msg.reply_text("❌ 권한이 없습니다.")
            return

        if not rest:
            await msg.reply_text("❌ 사용법: `.unset 키`")
            return

        key = rest.split()[0]
        if pop_setting(chat.id, key):
            await msg.reply_text(f"✅ `{key}` 설정 제거 완료", parse_mode="Markdown")
        else:
            await msg.reply_text(f"⚠️ `{key}` 설정이 없습니다.", parse_mode="Markdown")

    # ── .manager ─────────────────────────────────────────────────────────────
    elif cmd == "manager":
        if not await only_admin(update, context):
            await msg.reply_text("❌ 그룹 관리자만 매니저를 지정할 수 있습니다.")
            return

        target = None
        if msg.reply_to_message:
            target = msg.reply_to_message.from_user
        elif rest:
            token = rest.split()[0]
            target = await find_user(context, token, chat.id)

        if not target:
            await msg.reply_text(
                "❌ 대상을 찾을 수 없습니다.\n\n"
                "사용법:\n"
                "• `.manager @태그`\n"
                "• `.manager 고유번호`\n"
                "• 메시지에 답장 후 `.manager`"
            )
            return

        if target.id == context.bot.id:
            await msg.reply_text("❌ 봇은 매니저로 등록할 수 없습니다.")
            return

        add_manager(
            target.id, chat.id,
            getattr(target, "username", None),
            getattr(target, "first_name", None),
            update.effective_user.id,
        )
        await msg.reply_text(
            f"✅ **{fmt_user(target)}** 매니저 등록 완료",
            parse_mode="Markdown",
        )

    # ── .unmanager ───────────────────────────────────────────────────────────
    elif cmd == "unmanager":
        if not await only_admin(update, context):
            await msg.reply_text("❌ 그룹 관리자만 매니저를 해제할 수 있습니다.")
            return

        target = None
        if msg.reply_to_message:
            target = msg.reply_to_message.from_user
        elif rest:
            token = rest.split()[0]
            target = await find_user(context, token, chat.id)

        if not target:
            if rest:
                try:
                    uid = int(rest.split()[0])
                    if rm_manager(uid, chat.id):
                        await msg.reply_text(f"✅ ID `{uid}` 매니저 해제", parse_mode="Markdown")
                    else:
                        await msg.reply_text(f"⚠️ ID `{uid}` 는 매니저가 아닙니다.", parse_mode="Markdown")
                except ValueError:
                    await msg.reply_text("❌ 사용자를 찾을 수 없습니다.")
            else:
                await msg.reply_text(
                    "❌ 사용법:\n"
                    "• `.unmanager @태그`\n"
                    "• `.unmanager 고유번호`\n"
                    "• 메시지에 답장 후 `.unmanager`"
                )
            return

        if rm_manager(target.id, chat.id):
            await msg.reply_text(f"✅ **{fmt_user(target)}** 매니저 해제", parse_mode="Markdown")
        else:
            await msg.reply_text(
                f"⚠️ **{fmt_user(target)}** 은(는) 매니저가 아닙니다.", parse_mode="Markdown"
            )


async def on_new_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """봇이 그룹에 추가될 때 자동 등록"""
    chat = update.effective_chat
    if chat.type not in (Chat.GROUP, Chat.SUPERGROUP):
        return
    for member in update.message.new_chat_members or []:
        if member.id == context.bot.id:
            register_group(chat.id, chat.title or "")
            logger.info("그룹 등록: %s (%d)", chat.title, chat.id)


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN 환경변수를 설정해 주세요.")

    init_db()
    logger.info("DB 초기화 완료")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, on_new_chat_member))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))

    logger.info("봇 시작...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
