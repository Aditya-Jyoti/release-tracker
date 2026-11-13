"""Entry point: wiring, command handlers and job scheduling."""

from __future__ import annotations

import html
import logging
from dataclasses import dataclass

import httpx
from dotenv import load_dotenv
from telegram import BotCommand, LinkPreviewOptions, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    ApplicationHandlerStop,
    CommandHandler,
    ContextTypes,
    Defaults,
    TypeHandler,
)

from .checker import Checker, SendFunc
from .config import Config, ConfigError
from .db import Database
from .log import setup_logging
from .trackers import FetchError, InvalidTarget, RateLimited, TargetNotFound, TrackerRegistry
from .trackers.github_release import GitHubReleaseTracker

log = logging.getLogger("bot")

COMMANDS = [
    ("track", "Track: /track owner/repo [--tag latest|release|...] [--pre]"),
    ("untrack", "Stop tracking: /untrack owner/repo"),
    ("list", "Show tracked items"),
    ("check", "Check for updates now"),
    ("help", "Show usage"),
]


@dataclass
class Services:
    config: Config
    db: Database
    registry: TrackerRegistry
    checker: Checker
    http: httpx.AsyncClient


def services(context: ContextTypes.DEFAULT_TYPE) -> Services:
    return context.application.bot_data["services"]


# --------------------------------------------------------------------------- telegram sending


def make_sender(app: Application) -> SendFunc:
    async def send(chat_id: int, text: str) -> bool:
        ctx = {"chat_id": chat_id}
        try:
            await app.bot.send_message(chat_id, text)
            return True
        except Forbidden as exc:
            # User blocked the bot / bot removed from group. Nothing to retry.
            log.warning("cannot message chat (blocked?), skipping", extra={**ctx, "error": str(exc)})
            return True
        except RetryAfter as exc:
            log.warning("telegram flood control, will retry next cycle", extra={**ctx, "error": str(exc)})
            return False
        except BadRequest as exc:
            # Permanent (chat not found, bad markup). Don't loop on it forever.
            log.error("telegram rejected message", extra={**ctx, "error": str(exc)})
            return True
        except NetworkError as exc:
            log.warning("telegram network error, will retry next cycle", extra={**ctx, "error": str(exc)})
            return False
        except TelegramError as exc:
            log.error("telegram error, will retry next cycle", extra={**ctx, "error": str(exc)})
            return False

    return send


# --------------------------------------------------------------------------- handlers


def help_text(registry: TrackerRegistry) -> str:
    kinds = "\n".join(
        f"• <code>{t.kind}</code> — {html.escape(t.description)}"
        + "".join(f"\n    <code>{f}</code> {html.escape(h)}" for f, h in t.flags.items())
        + "".join(f"\n    <code>{f} &lt;value&gt;</code> {html.escape(h)}" for f, h in t.value_flags.items())
        for t in registry.trackers
    )
    return (
        "<b>Release tracker</b>\n\n"
        "/track <code>owner/repo</code> [--tag latest|release|…] [--pre] — start tracking (GitHub URLs work too)\n"
        "/untrack <code>owner/repo</code> — stop tracking\n"
        "/list — show what you're tracking\n"
        "/check — check for updates now\n"
        "/help — this message\n\n"
        f"Commands accept an optional kind first, e.g. <code>/track github owner/repo</code>. "
        f"Default kind: <code>{registry.default_kind}</code>.\n\n"
        f"<b>Tracker kinds</b>\n{kinds}"
    )


def describe_options(options: dict) -> str:
    parts = []
    if options.get("pre"):
        parts.append("incl. pre-releases")
    if options.get("tag"):
        parts.append(f"tag: {options['tag']}")
    return ", ".join(parts)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(help_text(services(context).registry))


async def cmd_track(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = services(context)
    msg = update.effective_message
    chat_id = update.effective_chat.id
    try:
        parsed = s.registry.parse_args(context.args or [])
        tracker = parsed.tracker
        target = tracker.normalize(parsed.target)
        options = tracker.parse_options(parsed.flags)
    except InvalidTarget as exc:
        await msg.reply_text(f"⚠️ {html.escape(str(exc))}\nUsage: /track <code>owner/repo</code> [--tag latest|release|…] [--pre]")
        return

    existing = s.db.get_item(chat_id, tracker.kind, target)
    if existing and existing.options == options:
        await msg.reply_text(f"Already tracking <b>{html.escape(target)}</b>.")
        return

    try:
        versions = await tracker.fetch(target, [options])
    except TargetNotFound:
        await msg.reply_text(f"❌ <b>{html.escape(target)}</b> was not found (or is private).")
        return
    except RateLimited:
        await msg.reply_text("⏳ Rate limited by upstream right now. Try again in a few minutes.")
        return
    except FetchError as exc:
        log.warning("baseline fetch failed", extra={"target": target, "error": str(exc)})
        await msg.reply_text("⚠️ Couldn't reach upstream right now. Try again shortly.")
        return

    latest = tracker.select_latest(versions, options)
    _, created = s.db.upsert_item(chat_id, tracker.kind, target, options, latest.id if latest else None)
    log.info(
        "tracking" if created else "tracking options updated",
        extra={"chat_id": chat_id, "kind": tracker.kind, "target": target, "options": options,
               "baseline": latest.id if latest else None},
    )

    flags = describe_options(options)
    flags = f" ({flags})" if flags else ""
    current = (
        f"Current {latest.label}: <code>{html.escape(latest.id)}</code>"
        if latest
        else "Nothing published yet"
    )
    verb = "Now tracking" if created else "Updated"
    await msg.reply_text(
        f"✅ {verb} <b>{html.escape(target)}</b>{flags}.\n{current}. I'll notify you about newer ones."
    )


async def cmd_untrack(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = services(context)
    msg = update.effective_message
    try:
        parsed = s.registry.parse_args(context.args or [])
        target = parsed.tracker.normalize(parsed.target)
    except InvalidTarget as exc:
        await msg.reply_text(f"⚠️ {html.escape(str(exc))}\nUsage: /untrack <code>owner/repo</code>")
        return
    if s.db.remove_item(update.effective_chat.id, parsed.tracker.kind, target):
        log.info("untracked", extra={"chat_id": update.effective_chat.id, "kind": parsed.tracker.kind, "target": target})
        await msg.reply_text(f"🗑 Stopped tracking <b>{html.escape(target)}</b>.")
    else:
        await msg.reply_text(f"You weren't tracking <b>{html.escape(target)}</b>.")


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = services(context)
    items = s.db.list_items(update.effective_chat.id)
    if not items:
        await update.effective_message.reply_text("Nothing tracked yet. Try /track <code>owner/repo</code>.")
        return
    lines = [f"<b>Tracking {len(items)} item{'s' if len(items) != 1 else ''}:</b>"]
    for it in items:
        tracker = s.registry.get(it.kind) if it.kind in s.registry else None
        url = tracker.target_url(it.target) if tracker else None
        name = f'<a href="{html.escape(url)}">{html.escape(it.target)}</a>' if url else html.escape(it.target)
        kind = "" if it.kind == s.registry.default_kind else f"[{html.escape(it.kind)}] "
        version = f"<code>{html.escape(it.last_version)}</code>" if it.last_version else "<i>none yet</i>"
        flags = describe_options(it.options)
        flags = f" · {flags}" if flags else ""
        lines.append(f"• {kind}{name} — {version}{flags}")
    await update.effective_message.reply_text("\n".join(lines))


async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = services(context)
    msg = update.effective_message
    if s.checker.running:
        await msg.reply_text("A check is already running; results will arrive shortly.")
        return
    status = await msg.reply_text("🔎 Checking…")
    report = await s.checker.run(make_sender(context.application))

    mine = {(i.kind, i.target) for i in s.db.list_items(update.effective_chat.id)}
    my_errors = {k: v for k, v in report.errors.items() if k in mine}
    text = f"Checked {report.targets} target(s); sent {report.notifications} notification(s)."
    if my_errors:
        text += "\n\nProblems:\n" + "\n".join(
            f"• {html.escape(t)}: {html.escape(e)}" for (_, t), e in sorted(my_errors.items())
        )
    try:
        await status.edit_text(text)
    except TelegramError:
        await msg.reply_text(text)


async def gate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    allowed = services(context).config.allowed_chat_ids
    if allowed is None:
        return
    chat = update.effective_chat
    if chat is None or chat.id not in allowed:
        log.warning(
            "ignoring update from unauthorized chat",
            extra={"chat_id": chat.id if chat else None,
                   "user_id": update.effective_user.id if update.effective_user else None},
        )
        raise ApplicationHandlerStop


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("unhandled error in handler", exc_info=context.error)


# --------------------------------------------------------------------------- job


async def scheduled_check(context: ContextTypes.DEFAULT_TYPE) -> None:
    s = services(context)
    if s.checker.running:
        log.info("previous check still running, skipping this tick")
        return
    try:
        await s.checker.run(make_sender(context.application))
    except Exception:
        # Never let the job die; next tick will try again.
        log.exception("check cycle crashed")


# --------------------------------------------------------------------------- wiring


def build_registry(config: Config, http: httpx.AsyncClient) -> TrackerRegistry:
    return TrackerRegistry(
        [
            GitHubReleaseTracker(http, token=config.github_token, fallback_to_tags=config.github_fallback_to_tags),
            # Add new trackers here, e.g. DockerHubTagTracker(http),
        ],
        default_kind="github",
    )


def build_application(config: Config) -> Application:
    db = Database(config.db_path)

    async def post_init(app: Application) -> None:
        http = httpx.AsyncClient(
            timeout=httpx.Timeout(20.0, connect=10.0),
            headers={"User-Agent": "telegram-release-tracker/1.0"},
            follow_redirects=True,  # renamed/transferred repos redirect
        )
        registry = build_registry(config, http)
        app.bot_data["services"] = Services(config, db, registry, Checker(db, registry), http)

        app.job_queue.run_repeating(scheduled_check, interval=config.check_interval, first=15, name="check")
        try:
            await app.bot.set_my_commands([BotCommand(c, d) for c, d in COMMANDS])
        except TelegramError as exc:
            log.warning("could not set bot commands", extra={"error": str(exc)})
        log.info(
            "bot started",
            extra={"interval_s": config.check_interval, "db": str(config.db_path),
                   "github_token": bool(config.github_token),
                   "allowlist": sorted(config.allowed_chat_ids) if config.allowed_chat_ids else None},
        )

    async def post_shutdown(app: Application) -> None:
        s: Services | None = app.bot_data.get("services")
        if s:
            await s.http.aclose()
        db.close()
        log.info("bot stopped")

    app = (
        ApplicationBuilder()
        .token(config.telegram_token)
        .defaults(Defaults(parse_mode=ParseMode.HTML, link_preview_options=LinkPreviewOptions(is_disabled=True)))
        .concurrent_updates(True)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    app.add_handler(TypeHandler(Update, gate), group=-1)
    app.add_handler(CommandHandler(["start", "help"], cmd_help))
    app.add_handler(CommandHandler("track", cmd_track))
    app.add_handler(CommandHandler("untrack", cmd_untrack))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("check", cmd_check))
    app.add_error_handler(on_error)
    return app


def main() -> None:
    load_dotenv()  # no-op if there's no .env; never overrides real env vars
    try:
        config = Config.from_env()
    except ConfigError as exc:
        raise SystemExit(f"config error: {exc}") from None
    setup_logging(config.log_level, config.log_format)
    build_application(config).run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)


if __name__ == "__main__":
    main()
