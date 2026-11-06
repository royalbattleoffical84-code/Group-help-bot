import os, json, re, logging
from datetime import datetime, timedelta, timezone

import firebase_admin
from firebase_admin import credentials, db as rtdb
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton, ChatPermissions, ReactionTypeEmoji
)
from telegram.constants import ChatType
from telegram.error import TelegramError, Forbidden, BadRequest
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ChatMemberHandler, ContextTypes, filters
)

# ---------------- CONFIG ----------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
FIREBASE_DATABASE_URL = os.getenv("FIREBASE_DATABASE_URL", "").strip()
FIREBASE_SERVICE_ACCOUNT = os.getenv("FIREBASE_SERVICE_ACCOUNT", "").strip()
PORT = int(os.getenv("PORT", "10000"))
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
ROOT = "orvix_group_help_bot"

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
log = logging.getLogger("orvix")

missing = [k for k, v in {
    "BOT_TOKEN": BOT_TOKEN,
    "OWNER_ID": OWNER_ID,
    "FIREBASE_DATABASE_URL": FIREBASE_DATABASE_URL,
    "FIREBASE_SERVICE_ACCOUNT": FIREBASE_SERVICE_ACCOUNT,
    "WEBHOOK_URL": WEBHOOK_URL,
    "WEBHOOK_SECRET": WEBHOOK_SECRET
}.items() if not v]
if missing:
    raise RuntimeError("Missing Render environment variables: " + ", ".join(missing))

# Service account JSON is stored in Render env var, never in GitHub.
try:
    service_account_info = json.loads(FIREBASE_SERVICE_ACCOUNT)
except json.JSONDecodeError as exc:
    raise RuntimeError("FIREBASE_SERVICE_ACCOUNT must contain the full valid service-account JSON.") from exc

cred = credentials.Certificate(service_account_info)
firebase_admin.initialize_app(cred, {"databaseURL": FIREBASE_DATABASE_URL})
ROOT_REF = rtdb.reference(ROOT)

BOT_USERNAME = ""
WIZARD = {}

# ---------------- FIREBASE HELPERS ----------------
def ref(path=""):
    return rtdb.reference("/".join(x for x in (ROOT, path.strip("/")) if x))

def now():
    return datetime.now(timezone.utc).isoformat()

def key(value):
    return str(value).replace(".", "_").replace("$", "_").replace("#", "_").replace("[", "_").replace("]", "_").replace("/", "_")

def get_group(chat_id):
    return ref(f"groups/{chat_id}").get()

def ensure_group(chat):
    ref(f"groups/{chat.id}").update({
        "chat_id": chat.id,
        "title": chat.title or str(chat.id),
        "username": chat.username or "",
        "active": True,
        "updated_at": now()
    })
    settings = ref(f"group_settings/{chat.id}")
    if settings.get() is None:
        settings.set({
            "link_protection": True,
            "max_links": 0,
            "link_action": "mute",
            "mute_at": 3,
            "ban_at": 5,
            "kick_at": 7,
            "welcome_text": "",
            "rules_text": "Please follow the group rules."
        })

def set_group_inactive(chat_id):
    ref(f"groups/{chat_id}/active").set(False)

def get_settings(chat_id):
    return ref(f"group_settings/{chat_id}").get() or {}

def update_setting(chat_id, name, value):
    allowed = {"link_protection", "max_links", "link_action", "mute_at", "ban_at", "kick_at", "welcome_text", "rules_text"}
    if name in allowed:
        ref(f"group_settings/{chat_id}/{name}").set(value)

def increment(path):
    r = ref(path)
    value = int(r.get() or 0) + 1
    r.set(value)
    return value

def warnings_count(chat_id, user_id):
    return int(ref(f"warnings/{chat_id}/{user_id}/count").get() or 0)

def add_warning(chat_id, user_id):
    value = increment(f"warnings/{chat_id}/{user_id}/count")
    ref(f"warnings/{chat_id}/{user_id}/updated_at").set(now())
    return value

def log_action(chat_id, actor_id, target_id, action, details=""):
    item = ref(f"moderation_logs/{chat_id}").push()
    item.set({
        "actor_id": actor_id,
        "target_id": target_id,
        "action": action,
        "details": details,
        "created_at": now()
    })

def list_groups():
    data = ref("groups").get() or {}
    return [v for v in data.values() if isinstance(v, dict) and v.get("active")]

def group_link_count(chat_id, user_id):
    return int(ref(f"link_violations/{chat_id}/{user_id}/count").get() or 0)

def add_link_violation(chat_id, user_id):
    count = increment(f"link_violations/{chat_id}/{user_id}/count")
    ref(f"link_violations/{chat_id}/{user_id}/updated_at").set(now())
    return count

# ---------------- KEYBOARDS ----------------
def main_menu(owner=False):
    rows = [
        [KeyboardButton("📣 Broadcast"), KeyboardButton("🛡 Group Settings")],
        [KeyboardButton("⚠️ Moderation"), KeyboardButton("📊 Statistics")],
        [KeyboardButton("📖 Rules"), KeyboardButton("ℹ️ Help")]
    ]
    if owner:
        rows.insert(0, [KeyboardButton("👑 Owner Panel")])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)

def owner_panel_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📣 All Groups Broadcast", callback_data="owner_all")],
        [InlineKeyboardButton("🎯 Selected Group Broadcast", callback_data="owner_select")],
        [InlineKeyboardButton("👥 Connected Groups", callback_data="owner_groups")]
    ])

def settings_kb(chat_id, s):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"Link Protection: {'ON' if s.get('link_protection', True) else 'OFF'}", callback_data=f"cfg_toggle:{chat_id}")],
        [InlineKeyboardButton(f"Maximum links: {'Unlimited' if int(s.get('max_links',0)) == 0 else s.get('max_links')}", callback_data=f"cfg_max:{chat_id}")],
        [InlineKeyboardButton(f"Limit punishment: {str(s.get('link_action','mute')).upper()}", callback_data=f"cfg_linkaction:{chat_id}")],
        [InlineKeyboardButton(f"Mute after: {s.get('mute_at') or 'OFF'} warnings", callback_data=f"cfg_threshold:mute:{chat_id}")],
        [InlineKeyboardButton(f"Ban after: {s.get('ban_at') or 'OFF'} warnings", callback_data=f"cfg_threshold:ban:{chat_id}")],
        [InlineKeyboardButton(f"Kick after: {s.get('kick_at') or 'OFF'} warnings", callback_data=f"cfg_threshold:kick:{chat_id}")],
        [InlineKeyboardButton("✏️ Welcome Message", callback_data=f"cfg_text:welcome_text:{chat_id}")],
        [InlineKeyboardButton("✏️ Group Rules", callback_data=f"cfg_text:rules_text:{chat_id}")]
    ])

def broadcast_type_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 Text", callback_data="bc_type:text"),
         InlineKeyboardButton("🖼 Photo", callback_data="bc_type:photo")],
        [InlineKeyboardButton("📎 File", callback_data="bc_type:document")],
        [InlineKeyboardButton("❌ Cancel", callback_data="bc_cancel")]
    ])

def button_choice_kb():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("➕ Add URL Button", callback_data="bc_button:add"),
        InlineKeyboardButton("⏭ Skip Button", callback_data="bc_button:skip")
    ], [InlineKeyboardButton("❌ Cancel", callback_data="bc_cancel")]])

def cancel_kb():
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="bc_cancel")]])

# ---------------- TELEGRAM PERMISSIONS ----------------
async def is_admin(context, chat_id, user_id):
    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
        return member.status in ("administrator", "creator")
    except TelegramError:
        return False

async def require_group_admin(update, context):
    chat = update.effective_chat
    user = update.effective_user
    if not chat or chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await update.effective_message.reply_text("Is feature ko group mein use karein.")
        return False
    if not await is_admin(context, chat.id, user.id):
        await update.effective_message.reply_text("Sirf is group ke admins is setting ko manage kar sakte hain.")
        return False
    return True

async def apply_punishment(context, chat_id, user_id, action, reason):
    try:
        if action == "mute":
            until = datetime.now(timezone.utc) + timedelta(hours=1)
            await context.bot.restrict_chat_member(
                chat_id, user_id,
                permissions=ChatPermissions(can_send_messages=False),
                until_date=until
            )
        elif action == "ban":
            await context.bot.ban_chat_member(chat_id, user_id)
        elif action == "kick":
            await context.bot.ban_chat_member(chat_id, user_id)
            await context.bot.unban_chat_member(chat_id, user_id, only_if_banned=True)
        else:
            return False
        log_action(chat_id, None, user_id, action, reason)
        return True
    except TelegramError as exc:
        log.warning("Punishment failed: %s", exc)
        return False

async def evaluate_warning_actions(context, chat_id, user_id, count):
    s = get_settings(chat_id)
    # If multiple thresholds share a number, use the strongest action only.
    for action in ("kick", "ban", "mute"):
        threshold = s.get(f"{action}_at")
        if threshold and int(threshold) == count:
            ok = await apply_punishment(context, chat_id, user_id, action, f"Reached {count} warnings")
            try:
                await context.bot.send_message(chat_id, f"⚠️ User {user_id}: {action.upper()} {'applied' if ok else 'failed'} after {count} warnings.")
            except TelegramError:
                pass
            return

# ---------------- START / GROUP LIFECYCLE ----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    add_url = f"https://t.me/{BOT_USERNAME}?startgroup=true"
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("➕ Add ORVIX to a Group", url=add_url)]])
    if chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
        ensure_group(chat)
        await update.effective_message.reply_text(
            "👋 ORVIX GROUP HELP BOT\n\nLink protection, moderation aur announcements ke liye mujhe admin banayein.",
            reply_markup=kb
        )
    else:
        await update.effective_message.reply_text(
            "👋 Welcome to ORVIX GROUP HELP BOT!\n\n"
            "Group admins link protection, warnings, mute/ban/kick aur group broadcast manage kar sakte hain.\n"
            "Bot owner all connected groups ko broadcast kar sakta hai.",
            reply_markup=kb
        )
        await update.effective_message.reply_text("Menu buttons se features use karein.", reply_markup=main_menu(user.id == OWNER_ID))

async def my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cm = update.my_chat_member
    chat = cm.chat
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return
    if cm.new_chat_member.status in ("member", "administrator"):
        ensure_group(chat)
        if cm.new_chat_member.status == "administrator":
            await context.bot.send_message(chat.id, "🛡 ORVIX joined. Group admins: use 🛡 Group Settings to configure protection and punishment.")
    elif cm.new_chat_member.status in ("left", "kicked"):
        set_group_inactive(chat.id)

async def new_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    ensure_group(chat)
    s = get_settings(chat.id)
    if s.get("welcome_text"):
        for member in update.effective_message.new_chat_members:
            try:
                await update.effective_message.reply_text(s["welcome_text"].replace("{name}", member.full_name))
            except TelegramError:
                pass

# ---------------- LINK PROTECTION ----------------
URL_RE = re.compile(r"(?i)(https?://\S+|www\.\S+|t\.me/\S+|telegram\.me/\S+|\b[a-z0-9-]+\.(com|net|org|in|io|co|me|xyz|app|dev|gg)\b\S*)")

def contains_link(message):
    text = (message.text or "") + " " + (message.caption or "")
    if URL_RE.search(text):
        return True
    entities = (message.entities or []) + (message.caption_entities or [])
    return any(getattr(e, "type", "") in ("url", "text_link") for e in entities)

async def moderate_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
    if not msg or not chat or not user or chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return
    ensure_group(chat)
    s = get_settings(chat.id)
    if not s.get("link_protection", True) or not contains_link(msg):
        return
    # All group admins and the group owner are exempt.
    if await is_admin(context, chat.id, user.id):
        return
    try:
        await context.bot.set_message_reaction(chat.id, msg.message_id, reaction=[ReactionTypeEmoji("👎")])
    except TelegramError:
        pass
    try:
        await msg.delete()
    except TelegramError:
        return

    links = add_link_violation(chat.id, user.id)
    warning_count = add_warning(chat.id, user.id)
    log_action(chat.id, None, user.id, "link_removed", f"link_count={links}; warnings={warning_count}")
    try:
        await context.bot.send_message(chat.id, f"⚠️ Link removed. User ID: {user.id}. Warning: {warning_count}")
    except TelegramError:
        pass

    max_links = int(s.get("max_links", 0) or 0)
    if max_links > 0 and links == max_links:
        await apply_punishment(context, chat.id, user.id, s.get("link_action", "mute"), f"Reached {max_links} removed links")
    await evaluate_warning_actions(context, chat.id, user.id, warning_count)

# ---------------- BROADCAST WIZARD ----------------
async def begin_broadcast(message, user_id, destination):
    WIZARD[user_id] = {"step": "type", "destination": destination}
    await message.reply_text("Broadcast content type choose karein:", reply_markup=broadcast_type_kb())

async def send_preview(message, state):
    summary = state.get("text") or state.get("caption") or "(Attachment)"
    await message.reply_text(
        "📣 BROADCAST PREVIEW\n\n" + summary + "\n\nConfirm karne ke liye button dabayein.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Confirm Broadcast", callback_data="bc_confirm"),
                                           InlineKeyboardButton("❌ Cancel", callback_data="bc_cancel")]])
    )

async def deliver(context, chat_id, state):
    markup = None
    if state.get("button_url"):
        markup = InlineKeyboardMarkup([[InlineKeyboardButton(state["button_text"], url=state["button_url"])]])
    kind = state.get("kind", "text")
    if kind == "photo":
        sent = await context.bot.send_photo(chat_id, state["file_id"], caption=state.get("caption", ""), reply_markup=markup)
    elif kind == "document":
        sent = await context.bot.send_document(chat_id, state["file_id"], caption=state.get("caption", ""), reply_markup=markup)
    else:
        sent = await context.bot.send_message(chat_id, state.get("text", ""), reply_markup=markup)
    try:
        await context.bot.pin_chat_message(chat_id, sent.message_id, disable_notification=True)
    except TelegramError as exc:
        log.info("Pin failed for %s: %s", chat_id, exc)

async def execute_broadcast(q, context, user_id):
    state = WIZARD.get(user_id)
    if not state:
        await q.edit_message_text("Broadcast session expire ho gaya. Dobara start karein.")
        return
    destination = state["destination"]
    if destination == "all":
        targets = [int(g["chat_id"]) for g in list_groups()]
    else:
        targets = [int(destination)]
        if user_id != OWNER_ID and not await is_admin(context, targets[0], user_id):
            await q.edit_message_text("Sirf us group ke admins broadcast kar sakte hain.")
            WIZARD.pop(user_id, None)
            return
    success = failed = 0
    for chat_id in targets:
        try:
            await deliver(context, chat_id, state)
            success += 1
        except TelegramError as exc:
            failed += 1
            log.warning("Broadcast error for %s: %s", chat_id, exc)
            if isinstance(exc, Forbidden) or "chat not found" in str(exc).lower():
                set_group_inactive(chat_id)
    ref(f"broadcast_history/{key(user_id)}").push().set({
        "destination": "all" if destination == "all" else destination,
        "success": success, "failed": failed, "created_at": now()
    })
    WIZARD.pop(user_id, None)
    await q.edit_message_text(f"📣 Broadcast complete\n\n✅ Sent: {success}\n❌ Failed: {failed}\n📌 Pin attempted in destination groups.")

# ---------------- CALLBACKS ----------------
async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data
    user_id = q.from_user.id

    if data == "bc_cancel":
        WIZARD.pop(user_id, None)
        await q.edit_message_text("Cancelled.")
        return

    if data == "owner_all":
        if user_id != OWNER_ID:
            await q.answer("Owner only.", show_alert=True); return
        await begin_broadcast(q.message, user_id, "all")
        return

    if data == "owner_select":
        if user_id != OWNER_ID:
            await q.answer("Owner only.", show_alert=True); return
        groups = list_groups()
        if not groups:
            await q.edit_message_text("No active groups found."); return
        buttons = [[InlineKeyboardButton(g.get("title", str(g["chat_id"]))[:50], callback_data=f"owner_pick:{g['chat_id']}")] for g in groups[:80]]
        await q.edit_message_text("Select a destination group:", reply_markup=InlineKeyboardMarkup(buttons))
        return

    if data.startswith("owner_pick:"):
        if user_id != OWNER_ID:
            await q.answer("Owner only.", show_alert=True); return
        chat_id = int(data.split(":")[1])
        await begin_broadcast(q.message, user_id, chat_id)
        return

    if data == "owner_groups":
        if user_id != OWNER_ID:
            await q.answer("Owner only.", show_alert=True); return
        groups = list_groups()
        lines = ["👥 CONNECTED GROUPS"]
        lines.extend([f"• {g.get('title','Group')} — `{g['chat_id']}`" for g in groups[:60]])
        await q.edit_message_text("\n".join(lines), parse_mode="Markdown")
        return

    if data.startswith("bc_type:"):
        state = WIZARD.get(user_id)
        if not state:
            await q.edit_message_text("Wizard expired. Start again."); return
        kind = data.split(":")[1]
        state["kind"] = kind
        if kind == "text":
            state["step"] = "text"
            await q.edit_message_text("Broadcast text bhejein:", reply_markup=cancel_kb())
        else:
            state["step"] = "attachment"
            await q.edit_message_text("Photo ya document bhejein. Caption optional hai.", reply_markup=cancel_kb())
        return

    if data.startswith("bc_button:"):
        state = WIZARD.get(user_id)
        if not state:
            await q.edit_message_text("Wizard expired."); return
        if data.endswith("skip"):
            state["step"] = "confirm"
            await send_preview(q.message, state)
        else:
            state["step"] = "button_text"
            await q.edit_message_text("Button ka text bhejein:", reply_markup=cancel_kb())
        return

    if data == "bc_confirm":
        state = WIZARD.get(user_id)
        if not state:
            await q.edit_message_text("Wizard expired."); return
        if state["destination"] == "all" and user_id != OWNER_ID:
            await q.answer("Owner only.", show_alert=True); return
        await execute_broadcast(q, context, user_id)
        return

    # All group configuration changes are made only by that group's Telegram admins.
    parts = data.split(":")
    if parts[0] in ("cfg_toggle", "cfg_max", "cfg_linkaction", "cfg_threshold", "cfg_text", "cfg_setmax", "cfg_setaction", "cfg_setthreshold", "cfg_back"):
        chat_id = int(parts[-1])
        if not await is_admin(context, chat_id, user_id):
            await q.answer("Only this group's admins can change these settings.", show_alert=True); return
        s = get_settings(chat_id)
        if parts[0] == "cfg_toggle":
            update_setting(chat_id, "link_protection", not s.get("link_protection", True))
            await q.edit_message_reply_markup(reply_markup=settings_kb(chat_id, get_settings(chat_id)))
        elif parts[0] == "cfg_max":
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("3", callback_data=f"cfg_setmax:3:{chat_id}"),
                 InlineKeyboardButton("5", callback_data=f"cfg_setmax:5:{chat_id}"),
                 InlineKeyboardButton("10", callback_data=f"cfg_setmax:10:{chat_id}")],
                [InlineKeyboardButton("20", callback_data=f"cfg_setmax:20:{chat_id}"),
                 InlineKeyboardButton("Unlimited", callback_data=f"cfg_setmax:0:{chat_id}")],
                [InlineKeyboardButton("Back", callback_data=f"cfg_back:{chat_id}")]
            ])
            await q.edit_message_reply_markup(reply_markup=kb)
        elif parts[0] == "cfg_linkaction":
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("Mute", callback_data=f"cfg_setaction:mute:{chat_id}"),
                InlineKeyboardButton("Ban", callback_data=f"cfg_setaction:ban:{chat_id}"),
                InlineKeyboardButton("Kick", callback_data=f"cfg_setaction:kick:{chat_id}")
            ], [InlineKeyboardButton("Back", callback_data=f"cfg_back:{chat_id}")]])
            await q.edit_message_reply_markup(reply_markup=kb)
        elif parts[0] == "cfg_threshold":
            action = parts[1]
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton(str(n), callback_data=f"cfg_setthreshold:{action}:{n}:{chat_id}") for n in (2,3,5)],
                [InlineKeyboardButton("7", callback_data=f"cfg_setthreshold:{action}:7:{chat_id}"),
                 InlineKeyboardButton("10", callback_data=f"cfg_setthreshold:{action}:10:{chat_id}"),
                 InlineKeyboardButton("OFF", callback_data=f"cfg_setthreshold:{action}:0:{chat_id}")],
                [InlineKeyboardButton("Back", callback_data=f"cfg_back:{chat_id}")]
            ])
            await q.edit_message_reply_markup(reply_markup=kb)
        elif parts[0] == "cfg_text":
            field = parts[1]
            WIZARD[user_id] = {"step": "setting_text", "destination": chat_id, "field": field}
            await q.message.reply_text("New text bhejein. Cancel: /cancel")
        elif parts[0] == "cfg_back":
            await q.edit_message_reply_markup(reply_markup=settings_kb(chat_id, s))
        elif parts[0] == "cfg_setmax":
            value = int(parts[1])
            update_setting(chat_id, "max_links", value)
            await q.edit_message_reply_markup(reply_markup=settings_kb(chat_id, get_settings(chat_id)))
        elif parts[0] == "cfg_setaction":
            update_setting(chat_id, "link_action", parts[1])
            await q.edit_message_reply_markup(reply_markup=settings_kb(chat_id, get_settings(chat_id)))
        elif parts[0] == "cfg_setthreshold":
            action, value = parts[1], int(parts[2])
            update_setting(chat_id, f"{action}_at", value)
            await q.edit_message_reply_markup(reply_markup=settings_kb(chat_id, get_settings(chat_id)))
        return

    if data.startswith("mod:"):
        _, action, chat_s, target_s = data.split(":")
        chat_id, target_id = int(chat_s), int(target_s)
        if not await is_admin(context, chat_id, user_id):
            await q.answer("Only this group's admins.", show_alert=True); return
        try:
            if action == "warn":
                count = add_warning(chat_id, target_id)
                log_action(chat_id, user_id, target_id, "warn", f"warnings={count}")
                await q.edit_message_text(f"Warning recorded. Total: {count}")
                await evaluate_warning_actions(context, chat_id, target_id, count)
            elif action in ("mute", "ban", "kick"):
                ok = await apply_punishment(context, chat_id, target_id, action, "Manual group admin action")
                await q.edit_message_text(f"{'✅' if ok else '❌'} {action.upper()} {'applied' if ok else 'failed'}.")
            elif action == "unban":
                await context.bot.unban_chat_member(chat_id, target_id, only_if_banned=True)
                log_action(chat_id, user_id, target_id, "unban")
                await q.edit_message_text("User unbanned.")
        except TelegramError as exc:
            await q.edit_message_text(f"Action failed: {exc}")
        return

# ---------------- TEXT / MEDIA ROUTER ----------------
async def route_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg, user, chat = update.effective_message, update.effective_user, update.effective_chat
    if not msg or not user or not chat:
        return
    state = WIZARD.get(user.id)
    if state:
        step = state.get("step")
        if step == "text":
            if not msg.text:
                await msg.reply_text("Text message bhejein.")
                return
            state["text"] = msg.text
            state["step"] = "button"
            await msg.reply_text("URL button add karna hai?", reply_markup=button_choice_kb())
            return
        if step == "attachment":
            if state["kind"] == "photo" and msg.photo:
                state["file_id"] = msg.photo[-1].file_id
                state["caption"] = msg.caption or ""
            elif state["kind"] == "document" and msg.document:
                state["file_id"] = msg.document.file_id
                state["caption"] = msg.caption or ""
            else:
                await msg.reply_text("Selected type ka photo/document bhejein.")
                return
            state["step"] = "button"
            await msg.reply_text("URL button add karna hai?", reply_markup=button_choice_kb())
            return
        if step == "button_text":
            state["button_text"] = (msg.text or "").strip()
            state["step"] = "button_url"
            await msg.reply_text("Button URL bhejein (https:// se shuru).", reply_markup=cancel_kb())
            return
        if step == "button_url":
            url = (msg.text or "").strip()
            if not re.match(r"^https?://", url, re.I):
                await msg.reply_text("Valid http:// ya https:// URL bhejein.")
                return
            state["button_url"] = url
            state["step"] = "confirm"
            await send_preview(msg, state)
            return
        if step == "setting_text":
            if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP) or not await is_admin(context, state["destination"], user.id):
                WIZARD.pop(user.id, None)
                await msg.reply_text("Only that group's admins can change settings.")
                return
            update_setting(state["destination"], state["field"], msg.text or "")
            WIZARD.pop(user.id, None)
            await msg.reply_text("Saved to Firebase Realtime Database.")
            return

    if chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
        ensure_group(chat)

    text = (msg.text or "").strip()
    if text == "👑 Owner Panel":
        if user.id != OWNER_ID:
            await msg.reply_text("Owner only.")
        else:
            await msg.reply_text("ORVIX OWNER PANEL", reply_markup=owner_panel_kb())
    elif text == "📣 Broadcast":
        if chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
            if await require_group_admin(update, context):
                await begin_broadcast(msg, user.id, chat.id)
        elif user.id == OWNER_ID:
            await msg.reply_text("Choose broadcast destination:", reply_markup=owner_panel_kb())
        else:
            await msg.reply_text("Group broadcast is available to group admins inside their group.")
    elif text == "🛡 Group Settings":
        if await require_group_admin(update, context):
            await msg.reply_text("Group settings (managed by this group's admins):", reply_markup=settings_kb(chat.id, get_settings(chat.id)))
    elif text == "⚠️ Moderation":
        if await require_group_admin(update, context):
            WIZARD[user.id] = {"step": "moderation_target", "destination": chat.id}
            await msg.reply_text("Target user ka numeric Telegram ID bhejein. Cancel: /cancel")
    elif text == "📊 Statistics":
        if chat.type in (ChatType.GROUP, ChatType.SUPERGROUP) and await require_group_admin(update, context):
            logs = ref(f"moderation_logs/{chat.id}").get() or {}
            await msg.reply_text(f"Moderation actions logged: {len(logs)}")
        elif user.id == OWNER_ID:
            await msg.reply_text(f"Active connected groups: {len(list_groups())}")
    elif text == "📖 Rules":
        if chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
            await msg.reply_text(get_settings(chat.id).get("rules_text", "Please follow the group rules."))
    elif text == "ℹ️ Help":
        await msg.reply_text("Menu buttons: Owner Panel, Broadcast, Group Settings, Moderation, Statistics, Rules.")
    elif state and state.get("step") == "moderation_target":
        if not text.isdigit():
            await msg.reply_text("Numeric Telegram User ID bhejein.")
            return
        chat_id, target_id = int(state["destination"]), int(text)
        if not await is_admin(context, chat_id, user.id):
            WIZARD.pop(user.id, None)
            await msg.reply_text("Only this group's admins can moderate.")
            return
        WIZARD[user.id] = {"step": "moderation_action", "destination": chat_id, "target": target_id}
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("⚠️ Warn", callback_data=f"mod:warn:{chat_id}:{target_id}"),
             InlineKeyboardButton("🔇 Mute 1h", callback_data=f"mod:mute:{chat_id}:{target_id}")],
            [InlineKeyboardButton("🚫 Ban", callback_data=f"mod:ban:{chat_id}:{target_id}"),
             InlineKeyboardButton("👢 Kick", callback_data=f"mod:kick:{chat_id}:{target_id}")],
            [InlineKeyboardButton("🔓 Unban", callback_data=f"mod:unban:{chat_id}:{target_id}")]
        ])
        await msg.reply_text(f"Choose action for user {target_id}:", reply_markup=kb)
    else:
        await moderate_message(update, context)

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    WIZARD.pop(update.effective_user.id, None)
    await update.effective_message.reply_text("Cancelled.")

async def error_handler(update, context):
    log.exception("Unhandled bot error: %s", context.error)

# ---------------- STARTUP ----------------
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    async def post_init(application):
        global BOT_USERNAME
        me = await application.bot.get_me()
        BOT_USERNAME = me.username or ""
        await application.bot.set_my_commands([])  # feature actions are menu buttons
        log.info("ORVIX started as @%s", BOT_USERNAME)

    app.post_init = post_init
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(ChatMemberHandler(my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, new_members))
    app.add_handler(CallbackQueryHandler(callbacks))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, route_message))
    app.add_handler(MessageHandler((filters.PHOTO | filters.Document.ALL) & ~filters.COMMAND, route_message))
    app.add_error_handler(error_handler)

    webhook_path = "orvix-" + WEBHOOK_SECRET
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=webhook_path,
        webhook_url=f"{WEBHOOK_URL}/{webhook_path}",
        secret_token=WEBHOOK_SECRET,
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES
    )

if __name__ == "__main__":
    main()
