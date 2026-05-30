import logging
import sqlite3
import json
from datetime import datetime, timedelta
import pytz
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
import re
import threading
from flask import Flask, request, jsonify
import os

# 配置日志
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

# ==================== 核心配置 ====================
TOKEN = "8912954548:AAG-1rZVUabLEv9AOfJRQxGVax4ZiXWtC8g"
WEB_URL = "https://sellb-6ugh.onrender.com"
PORT = int(os.environ.get('PORT', 8080))

# 创始超级管理员（你自己的账户ID，用来接收审核通知和按钮）
FOUNDER_USERS = [8782394486]

# 销售收款配置
TRON_ADDRESS = "TVnjLwDrGjYVRTa1ukfoE2mFTmCxtrjoCw"
MONTHLY_PRICE = "1 TRX" 

flask_app = Flask(__name__)

# ========== 数据库初始化 ==========
def init_db():
    conn = sqlite3.connect('bot_data.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS settings
                 (group_id INTEGER PRIMARY KEY, operators TEXT DEFAULT '[]', exchange_rate REAL DEFAULT 7.2,
                  fee_rate REAL DEFAULT 0, is_active INTEGER DEFAULT 0, language TEXT DEFAULT 'chinese',
                  timezone TEXT DEFAULT 'Asia/Shanghai', show_usdt INTEGER DEFAULT 1)''')
    c.execute('''CREATE TABLE IF NOT EXISTS bills
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, group_id INTEGER, user_id INTEGER, username TEXT,
                  remark TEXT, amount REAL, usdt_amount REAL, exchange_rate REAL, bill_type TEXT,
                  timestamp TEXT, date_str TEXT, is_settled INTEGER DEFAULT 0)''')
    c.execute('''CREATE TABLE IF NOT EXISTS vip_users
                 (user_id INTEGER PRIMARY KEY, username TEXT, expire_time TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS dynamic_masters
                 (user_id INTEGER PRIMARY KEY, username TEXT, added_by INTEGER)''')
    conn.commit()
    conn.close()

# ========== 权限判定引擎 ==========

def get_all_masters():
    masters = list(FOUNDER_USERS)
    try:
        conn = sqlite3.connect('bot_data.db')
        c = conn.cursor()
        c.execute("SELECT user_id FROM dynamic_masters")
        rows = c.fetchall()
        conn.close()
        for row in rows:
            if row[0] not in masters: masters.append(row[0])
    except: pass
    return masters

def is_master(user_id):
    return user_id in get_all_masters()

def is_vip_user(user_id):
    if is_master(user_id): return True
    conn = sqlite3.connect('bot_data.db')
    c = conn.cursor()
    c.execute("SELECT expire_time FROM vip_users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    if row:
        try:
            expire = datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S")
            return datetime.now() < expire
        except: return False
    return False

def can_use(group_id, user_id):
    if is_master(user_id) or is_vip_user(user_id): return True
    ops = json.loads(get_setting(group_id, 'operators') or '[]')
    return user_id in ops

def get_setting(group_id, key):
    conn = sqlite3.connect('bot_data.db')
    c = conn.cursor()
    c.execute("SELECT * FROM settings WHERE group_id = ?", (group_id,))
    row = c.fetchone()
    conn.close()
    if not row: return None
    cols = ['group_id', 'operators', 'exchange_rate', 'fee_rate', 'is_active', 'language', 'timezone', 'show_usdt']
    return dict(zip(cols, row)).get(key)

def update_setting(group_id, key, value):
    conn = sqlite3.connect('bot_data.db')
    c = conn.cursor()
    c.execute("SELECT * FROM settings WHERE group_id = ?", (group_id,))
    if c.fetchone():
        c.execute(f"UPDATE settings SET {key} = ? WHERE group_id = ?", (value, group_id))
    else:
        c.execute("INSERT INTO settings (group_id, operators, exchange_rate, fee_rate, is_active, language, timezone, show_usdt) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                  (group_id, '[]', 7.2, 0, 0, 'chinese', 'Asia/Shanghai', 1))
        c.execute(f"UPDATE settings SET {key} = ? WHERE group_id = ?", (value, group_id))
    conn.commit()
    conn.close()

# ========== 界面菜单设计 ==========

def get_private_main_keyboard():
    keyboard = [
        [InlineKeyboardButton("💰 充值续费方法", callback_data="menu_renew"),
         InlineKeyboardButton("📅 检查到期时间", callback_data="menu_expire")],
        [InlineKeyboardButton("👑 添加新机器人主人", callback_data="menu_add_master"),
         InlineKeyboardButton("📖 机器人使用指南", callback_data="menu_help")],
        [InlineKeyboardButton("🌐 访问账单网页端", url=WEB_URL)]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_renew_text():
    return f"""
💰 <b>【智能记账系统 - 充值续费说明】</b>
---
📊 <b>当前价格：</b> <code>{MONTHLY_PRICE} / 每 30 天</code>
🌟 <b>特权包干：</b> 购买后，您名下在<b>【无数个群组】</b>拉入此机器人均可自动解锁，不受限制！

📌 <b>自主转账对账流程：</b>
1️⃣ 请向下方 <b>TRX/波场</b> 官方收币地址转账：
👉 <code>{TRON_ADDRESS}</code> <i>(点击可自动复制)</i>

2️⃣ 转账成功后，请直接在当前私聊框中<b>回复发送您的钱包转账地址</b>或<b>交易哈希 (TxID)</b>。
3️⃣ 系统客服核对无误后，会为您秒级开通多群授权！
"""

def get_uid_tutorial_text():
    return """
❓ <b>如何获取 Telegram 用户唯一 UID？</b>
---
为了保证机器人主人的唯一安全性，系统采用不可更改的数字 UID 进行绑定。请通过以下方式获取您的或您朋友的 UID：

🌟 <b>最快获取方式：</b>
1️⃣ 在 Telegram 搜索栏输入： @userinfobot 或 @username_to_id_bot
2️⃣ 点击进入该官方机器人，发送任意消息或点击 <code>/start</code>。
3️⃣ 对方机器人会立即回复一串数字（例如：<code>8782394486</code>），这串数字就是<b>您的 UID</b>。

👉 <b>请获取到 UID 数字后，直接在下方输入发送给本机器人！</b>
"""

# ========== 核心消息路由与业务逻辑 ==========

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private":
        welcome_text = (
            f"👋 您好，<b>{update.effective_user.first_name}</b>！欢迎使用智能记账多群分销版后台管理大厅。\n\n"
            f"💡 请使用下方的高级控制面板管理您的记账特权、绑定新主人或查看账单："
        )
        await update.message.reply_text(welcome_text, reply_markup=get_private_main_keyboard(), parse_mode="HTML")
    else:
        await update.message.reply_text("📊 记账机器人已在群组就绪！请输入 <code>上课</code> 开启记账。私聊我可查看充值大厅。", parse_mode="HTML")

async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    uid = query.from_user.id
    await query.answer()

    # --- 管理员点击“确认到账”或“拒绝”的逻辑 ---
    if query.data.startswith("admin_approve_"):
        target_uid = int(query.data.split("_")[2])
        conn = sqlite3.connect('bot_data.db')
        c = conn.cursor()
        expire_date = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
        c.execute("INSERT OR REPLACE INTO vip_users (user_id, username, expire_time) VALUES (?, ?, ?)", 
                  (target_uid, "包月买家", expire_date))
        conn.commit()
        conn.close()
        
        await query.message.edit_text(f"✅ <b>核对结果：已确认到账！</b>\n已成功为买家 (ID: <code>{target_uid}</code>) 激活多群独立记账白名单资格（30天）。", parse_mode="HTML")
        try:
            await context.bot.send_message(
                chat_id=target_uid, 
                text="🎉 <b>您的付款已核对成功！</b>\n系统已为您全面解锁多群无限制建群、无限记账 VIP 权限！如果您想把别人设为【机器人新主人】，请直接在私聊点击【👑 添加新机器人主人】按钮进行绑定。"
            )
        except: pass
        return

    elif query.data.startswith("admin_reject_"):
        target_uid = int(query.data.split("_")[2])
        await query.message.edit_text(f"❌ <b>核对结果：已拒绝开通。</b>\n已通知买家 (ID: <code>{target_uid}</code>) 账单未核对成功。", parse_mode="HTML")
        try:
            await context.bot.send_message(
                chat_id=target_uid, 
                text="⚠️ <b>通知：您的付款对账未通过审核。</b>\n请检查您发送的钱包地址/哈希是否正确，或联系官方客服人工核对。"
            )
        except: pass
        return

    # --- 买家控制大厅按钮 ---
    if query.data == "menu_renew":
        await query.message.reply_text(get_renew_text(), parse_mode="HTML")
        
    elif query.data == "menu_expire":
        conn = sqlite3.connect('bot_data.db')
        c = conn.cursor()
        c.execute("SELECT expire_time FROM vip_users WHERE user_id = ?", (uid,))
        row = c.fetchone()
        conn.close()
        
        if uid in FOUNDER_USERS:
            status_text = "📅 <b>特权状态：</b> ⚖️ 创始人账户（永久有效）"
        elif row:
            status_text = f"📅 <b>特权到期时间：</b> <code>{row[0]}</code>\n💡 只要在有效期内，您在任意群里都能正常上课记账。"
        else:
            status_text = "⚠️ <b>特权状态：</b> 您当前尚未开通多群包月VIP资格，或已过期。请点击【充值续费】获取授权。"
        await query.message.reply_text(status_text, parse_mode="HTML")

    elif query.data == "menu_add_master":
        if not (is_master(uid) or is_vip_user(uid)):
            await query.message.reply_text("❌ 抱歉，您当前还没有购买本机器人，无权添加新的机器人主人。请先充值开通。")
            return
            
        context.user_data['waiting_for_master_id'] = True
        await query.message.reply_text(
            "📝 <b>请输入您想添加的【新机器人主人】的 UID（纯数字）：</b>\n"
            "--------------------------------------------\n"
            "💡 如果您不知道如何获取 UID，请查看下方教程👇",
            parse_mode="HTML"
        )
        await query.message.reply_text(get_uid_tutorial_text(), parse_mode="HTML")

    elif query.data == "menu_help":
        help_msg = "📌 <b>记账命令格式：</b>\n`+1000` 记入款\n`-500` 记支出\n`上课` 开启\n`下课` 汇总归档"
        await query.message.reply_text(help_msg, parse_mode="Markdown")

async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    chat_type = update.effective_chat.type
    gid = update.effective_chat.id
    uid = update.effective_user.id
    username = update.effective_user.first_name

    if chat_type == "private":
        if context.user_data.get('waiting_for_master_id'):
            context.user_data['waiting_for_master_id'] = False 
            clean_uid = "".join(filter(str.isdigit, text))
            
            if clean_uid and len(clean_uid) >= 5:
                target_master_id = int(clean_uid)
                
                conn = sqlite3.connect('bot_data.db')
                c = conn.cursor()
                c.execute("INSERT OR REPLACE INTO dynamic_masters (user_id, username, added_by) VALUES (?, ?, ?)",
                          (target_master_id, "新绑定的主人", uid))
                conn.commit()
                conn.close()
                
                await update.message.reply_text(
                    f"🎉 <b>权限升级成功！</b>\n\n🤝 已成功将 <code>{target_master_id}</code> 设置为全新的<b>机器人主人（Master）</b>！",
                    parse_mode="HTML"
                )
                try:
                    await context.bot.send_message(
                        chat_id=target_master_id, 
                        text=f"🎉 告知：您已被买家 (ID: {uid}) 授权添加为本机器人的全局新主人！您现在可在任意群组内进行财务记账与管理操作。"
                    )
                except: pass
            else:
                await update.message.reply_text("❌ 您输入的 UID 格式不正确。请重新点击按钮重试。")
            return

        # 买家提交凭证：直接为管理员生成带“批准/拒绝”按钮的卡片！
        if len(text) >= 20:
            masters = get_all_masters()
            
            # 创建审批内联按钮
            admin_keyboard = [
                [
                    InlineKeyboardButton("✅ 确认到账（直接激活）", callback_data=f"admin_approve_{uid}"),
                    InlineKeyboardButton("❌ 拒绝（未收到款）", callback_data=f"admin_reject_{uid}")
                ]
            ]
            
            notification = (
                f"🔔 <b>买家提交付款对账通知</b>\n\n"
                f"👤 买家: {username} (ID: <code>{uid}</code>)\n"
                f"📝 提交的对账凭证/哈希地址:\n<code>{text}</code>\n\n"
                f"⚖️ <b>请核对您的钱包，并选择以下操作：</b>"
            )
            
            for m_id in masters:
                try: 
                    await context.bot.send_message(
                        chat_id=m_id, 
                        text=notification, 
                        reply_markup=InlineKeyboardMarkup(admin_keyboard), 
                        parse_mode="HTML"
                    )
                except: pass
            await update.message.reply_text("✅ 您的付款对账信息已成功递交！客服核对完毕后将立即全线激活您的多群使用权。")
            return
            
        await update.message.reply_text("💡 请使用下方的高级控制面板管理您的机器人：", reply_markup=get_private_main_keyboard(), parse_mode="HTML")
        return

    # 群组内控制
    if text in ['上课', 'အတန်းစ']:
        if not can_use(gid, uid): return
        update_setting(gid, 'is_active', 1)
        await update.message.reply_text("🟢 本群记账服务已开启！")
        return

    if text in ['下课', 'အတန်းဆင်း']:
        if not can_use(gid, uid): return
        if (get_setting(gid, 'is_active') or 0) == 0: return
        update_setting(gid, 'is_active', 0)
        await update.message.reply_text("🔴 下课成功！")
        return

    if text.startswith('设置操作人'):
        if not (is_master(uid) or is_vip_user(uid)):
            await update.message.reply_text("❌ 只有购买本机器人的VIP群主有权限添加群内财务操作员。")
            return
        target_id = None
        if update.message.reply_to_message:
            target_id = update.message.reply_to_message.from_user.id
        if target_id:
            ops = json.loads(get_setting(gid, 'operators') or '[]')
            if target_id not in ops:
                ops.append(target_id)
                update_setting(gid, 'operators', json.dumps(ops))
                await update.message.reply_text("✅ 已成功为本群添加一名财务操作员。")
        return

# ========== 核心网关与启动器 ==========

def main():
    init_db()
    threading.Thread(target=lambda: flask_app.run(host='0.0.0.0', port=PORT), daemon=True).start()
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CallbackQueryHandler(handle_callback_query))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))
    app.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()
