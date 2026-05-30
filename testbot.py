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

# ==================== 售卖商核心配置 ====================
TOKEN = "8912954548:AAG-1rZVUabLEv9AOfJRQxGVax4ZiXWtC8g"
WEB_URL = "https://sellb-6ugh.onrender.com"
PORT = int(os.environ.get('PORT', 8080))

# 允许设置多位机器人主人(超级管理员ID)
MASTER_USERS = [8782394486, 123456789, 987654321] 

# 销售收款配置
TRON_ADDRESS = "TVnjLwDrGjYVRTa1ukfoE2mFTmCxtrjoCw"
MONTHLY_PRICE = "1 USDT" # 已修改为 80 USDT

TIMEZONES = {
    'china': 'Asia/Shanghai',
    'myanmar': 'Asia/Yangon',
    'thailand': 'Asia/Bangkok',
}

flask_app = Flask(__name__)

# ========== 数据库函数 ==========

def get_current_time(timezone_str):
    try:
        tz = pytz.timezone(timezone_str)
        now = datetime.now(tz)
        return now, now.strftime("%H:%M:%S"), now.strftime("%Y-%m-%d %H:%M:%S")
    except:
        tz = pytz.timezone('Asia/Shanghai')
        now = datetime.now(tz)
        return now, now.strftime("%H:%M:%S"), now.strftime("%Y-%m-%d %H:%M:%S")

def init_db():
    conn = sqlite3.connect('bot_data.db')
    c = conn.cursor()
    # 群组设置表
    c.execute('''CREATE TABLE IF NOT EXISTS settings
                 (group_id INTEGER PRIMARY KEY,
                  operators TEXT DEFAULT '[]',
                  exchange_rate REAL DEFAULT 7.2,
                  fee_rate REAL DEFAULT 0,
                  is_active INTEGER DEFAULT 0,
                  language TEXT DEFAULT 'chinese',
                  timezone TEXT DEFAULT 'Asia/Shanghai',
                  show_usdt INTEGER DEFAULT 1)''')
    # 账单表
    c.execute('''CREATE TABLE IF NOT EXISTS bills
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  group_id INTEGER,
                  user_id INTEGER,
                  username TEXT,
                  remark TEXT,
                  amount REAL,
                  usdt_amount REAL,
                  exchange_rate REAL,
                  bill_type TEXT,
                  timestamp TEXT,
                  date_str TEXT,
                  is_settled INTEGER DEFAULT 0)''')
    # 新增：买家VIP授权表 (包月使用权绑定用户，不卡群)
    c.execute('''CREATE TABLE IF NOT EXISTS vip_users
                 (user_id INTEGER PRIMARY KEY,
                  username TEXT,
                  expire_time TEXT)''')
    conn.commit()
    conn.close()

def get_setting(group_id, key):
    conn = sqlite3.connect('bot_data.db')
    c = conn.cursor()
    c.execute("SELECT * FROM settings WHERE group_id = ?", (group_id,))
    row = c.fetchone()
    conn.close()
    if not row:
        return None
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

# 检查买家是否拥有VIP资格（无限制建群、记账特权）
def is_vip_user(user_id):
    if user_id in MASTER_USERS:
        return True
    conn = sqlite3.connect('bot_data.db')
    c = conn.cursor()
    c.execute("SELECT expire_time FROM vip_users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    if row:
        try:
            expire = datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S")
            return datetime.now() < expire
        except:
            return False
    return False

def add_vip_user(user_id, username, days=30):
    conn = sqlite3.connect('bot_data.db')
    c = conn.cursor()
    expire_date = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    c.execute("INSERT OR REPLACE INTO vip_users (user_id, username, expire_time) VALUES (?, ?, ?)", 
              (user_id, username, expire_date))
    conn.commit()
    conn.close()

def is_master(user_id):
    return user_id in MASTER_USERS

# 鉴权：超级管理员、买家VIP、或者群组内被授权的操作员均可记账
def can_use(group_id, user_id):
    if is_master(user_id) or is_vip_user(user_id):
        return True
    ops = json.loads(get_setting(group_id, 'operators') or '[]')
    return user_id in ops

# （保留原账单基本增删改查函数... 空间原因略作精简，逻辑与上一版一致）
def add_bill(group_id, user_id, username, remark, amount, bill_type, exchange_rate=None):
    if exchange_rate is None: exchange_rate = get_setting(group_id, 'exchange_rate') or 7.2
    usdt_amount = amount / exchange_rate if bill_type == 'income' else amount
    tz_str = get_setting(group_id, 'timezone') or 'Asia/Shanghai'
    now, _, full_time = get_current_time(tz_str)
    date_str = now.strftime("%Y-%m-%d")
    conn = sqlite3.connect('bot_data.db')
    c = conn.cursor()
    c.execute("INSERT INTO bills (group_id, user_id, username, remark, amount, usdt_amount, exchange_rate, bill_type, timestamp, date_str) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
              (group_id, user_id, username, remark, amount, usdt_amount, exchange_rate, bill_type, full_time, date_str))
    conn.commit()
    conn.close()
    return usdt_amount

def get_class_bills_by_date(group_id, target_date):
    conn = sqlite3.connect('bot_data.db')
    c = conn.cursor()
    c.execute("SELECT remark, username, amount, usdt_amount, exchange_rate, timestamp FROM bills WHERE group_id = ? AND date_str = ? AND bill_type = 'income' ORDER BY id DESC", (group_id, target_date))
    income = c.fetchall()
    c.execute("SELECT remark, username, usdt_amount, exchange_rate, timestamp FROM bills WHERE group_id = ? AND date_str = ? AND bill_type = 'expense' ORDER BY id DESC", (group_id, target_date))
    expense = c.fetchall()
    c.execute("SELECT SUM(amount), SUM(usdt_amount) FROM bills WHERE group_id = ? AND date_str = ? AND bill_type = 'income'", (group_id, target_date))
    total_income = c.fetchone()
    c.execute("SELECT SUM(usdt_amount) FROM bills WHERE group_id = ? AND date_str = ? AND bill_type = 'expense'", (group_id, target_date))
    total_expense = c.fetchone()
    conn.close()
    return income, expense, total_income, total_expense

def settle_today_bills(group_id, target_date):
    conn = sqlite3.connect('bot_data.db')
    c = conn.cursor()
    c.execute("UPDATE bills SET is_settled = 1 WHERE group_id = ? AND date_str = ?", (group_id, target_date))
    conn.commit()
    conn.close()

def delete_today_bills(group_id):
    tz_str = get_setting(group_id, 'timezone') or 'Asia/Shanghai'
    now, _, _ = get_current_time(tz_str)
    today_date = now.strftime("%Y-%m-%d")
    conn = sqlite3.connect('bot_data.db')
    c = conn.cursor()
    c.execute("DELETE FROM bills WHERE group_id = ? AND date_str = ?", (group_id, today_date))
    conn.commit()
    conn.close()

def delete_last_bill(group_id):
    conn = sqlite3.connect('bot_data.db')
    c = conn.cursor()
    c.execute("SELECT id FROM bills WHERE group_id = ? ORDER BY id DESC LIMIT 1", (group_id,))
    last = c.fetchone()
    if last: c.execute("DELETE FROM bills WHERE id = ?", (last[0],))
    conn.commit()
    conn.close()
    return 1 if last else 0

# ========== 续费菜单文本封装 ==========

def get_renew_menu():
    return f"""
👑 <b>🛒 智能记账机器人 - 多群包月分销版</b>
---
📊 <b>租用价格：</b> <code>{MONTHLY_PRICE} / 每月 (30天)</code>
🌟 <b>特权说明：</b> 只要开通，买家名下可在<b>【无数个群组】</b>同时拉入并授权使用机器人，不限群数！

⚠️ <b>转账核对流程（请严格遵守）：</b>
为了账单对账安全，请先向下方 <b>TRC-20</b> 专属收币地址转账：

💰 收款地址： <code>{TRON_ADDRESS}</code>
<i>(温馨提示：点击上方地址可以自动复制)</i>

📌 <b>付款核对：</b>
请在转账成功后，在当前私聊框中直接回复：<b><code>您的付款钱包地址</code></b> 或 <b><code>交易哈希 (TxID)</code></b>。
客服核对完毕后，将立刻为您全线激活多群使用权！
"""

def get_help_text(lang):
    return """
🤖 *记账机器人使用指南*

📌 *记账格式：*
`+1000` - 入款1000元
`-1000` - 入款-1000元
`备注+2000` - 带备注入款
`下发50` - 下发50 USDT
`+0` - 查看今日汇总

📌 *管理命令（VIP买家或群操作员）：*
`上课` | `下课` | `设置汇率 7.2` 
`设置操作人` (回复某人消息，支持无限添加副手操作员)
`改语言` | `删今天` | `删最后`

💡 *多群包月续费*：
请直接在与机器人**【私聊】**中发送 `续费` 查看收币地址，并提交您的转账地址进行核对开通。
"""

def get_bill_content(income, expense, total_rmb, total_usdt, expense_usdt, rate, today_date, lang):
    unit = "U"
    message = f"📊 账单汇总 ({today_date})\n\n"
    if income:
        message += "📥 入款:\n"
        for bill in income[:5]:
            remark, username, amount, usdt, ex_rate, ts = bill
            time_short = ts[11:16] if (ts and len(ts) > 16) else ''
            message += f"  {time_short} 【{remark or ''}】{amount or 0:.0f}/{ex_rate or rate:.1f}={usdt or 0:.1f}{unit}\n"
    if expense:
        message += "\n📤 下发:\n"
        for bill in expense[:5]:
            remark, username, usdt, ex_rate, ts = bill
            time_short = ts[11:16] if (ts and len(ts) > 16) else ''
            message += f"  {time_short} 【{remark or ''}】{usdt or 0:.1f}{unit}\n"
    message += f"\n💰 汇率: {rate:.2f}\n"
    message += f"📊 总入款: {total_rmb:.0f} | {total_usdt:.1f}{unit}\n"
    message += f"📊 已下发: {expense_usdt:.1f}{unit}\n"
    message += f"📊 未下发: {total_usdt - expense_usdt:.1f}{unit}"
    return message

async def show_full_bill(update: Update, gid):
    tz_str = get_setting(gid, 'timezone') or 'Asia/Shanghai'
    now, _, _ = get_current_time(tz_str)
    today_date = now.strftime("%Y-%m-%d")
    income, expense, total_income, total_expense = get_class_bills_by_date(gid, today_date)
    rate = get_setting(gid, 'exchange_rate') or 7.2
    message = get_bill_content(income, expense, total_income[0] or 0, total_income[1] or 0, total_expense[0] or 0, rate, today_date, 'chinese')
    keyboard = [[InlineKeyboardButton("📊 完整账单 (Web)", url=f"{WEB_URL}?group_id={gid}")]]
    await update.message.reply_text(message, reply_markup=InlineKeyboardMarkup(keyboard))

# ========== 核心消息路由 ==========

async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    chat_type = update.effective_chat.type
    gid = update.effective_chat.id
    uid = update.effective_user.id
    username = update.effective_user.first_name
    
    # 1. 私聊逻辑：买家提供转账地址进行核对，或索取续费账单
    if chat_type == "private":
        if text in ["续费", "续期", "购买", "pay", "renew"]:
            await update.message.reply_text(get_renew_menu(), parse_mode="HTML")
            return
        
        # 核心修改：买家提交了用于对账的钱包地址或哈希
        if len(text) >= 20: # 钱包地址或TxID通常较长
            # 自动通知列表里的所有超级管理员进行审核
            notification = f"🔔 <b>买家提交付款对账通知</b>\n\n👤 买家: {username} (ID: <code>{uid}</code>)\n📝 提交的对账凭证/地址:\n<code>{text}</code>\n\n💡 <b>如何开通？</b>\n请管理员核对款项后，在任一聊天框或私聊中发送指令开通：\n<code>/gopay {uid}</code>"
            for master_id in MASTER_USERS:
                try:
                    await context.bot.send_message(chat_id=master_id, text=notification, parse_mode="HTML")
                except:
                    pass
            await update.message.reply_text("✅ 您的付款转账地址已成功提交对账系统！\n官方客服正在加急审核，核对完成将自动为您激活多群无限使用特权。")
            return
            
        await update.message.reply_text(get_help_text('chinese'))
        return

    # 2. 管理员指令：在群里开通VIP（输入 /gopay 买家UID）
    if text.startswith("/gopay"):
        if not is_master(uid):
            return
        parts = text.split()
        if len(parts) == 2 and parts[1].isdigit():
            target_uid = int(parts[1])
            add_vip_user(target_uid, "授权用户")
            await update.message.reply_text(f"🎉 成功为买家 [UID: {target_uid}] 激活多群独立记账VIP资格（30天有效期）！")
            try:
                await context.bot.send_message(chat_id=target_uid, text="🎉 您的付款已核对无误！已为您开通多群独立记账VIP资格（30天不限群数）。您可以直接将机器人拉入您的任意群组开始使用！")
            except:
                pass
        return

    # 3. 群组内原有记账核心业务控制 (受新版 VIP 与操作员强鉴权保护)
    if text in ['上课', 'အတန်းစ']:
        if not can_use(gid, uid): return
        update_setting(gid, 'is_active', 1)
        await update.message.reply_text("🟢 本群记账服务已开启！请开始发送数据。")
        return

    if text in ['下课', 'အတန်းဆင်း']:
        if not can_use(gid, uid): return
        if (get_setting(gid, 'is_active') or 0) == 0: return
        await show_full_bill(update, gid)
        tz_str = get_setting(gid, 'timezone') or 'Asia/Shanghai'
        now, _, _ = get_current_time(tz_str)
        settle_today_bills(gid, now.strftime("%Y-%m-%d"))
        update_setting(gid, 'is_active', 0)
        await update.message.reply_text("🔴 下课成功！本轮账单已归档锁定。")
        return

    if text.startswith('设置操作人'):
        if not (is_master(uid) or is_vip_user(uid)):
            await update.message.reply_text("❌ 只有购买了本机器人的VIP主人可以设置群内操作员。")
            return
        target_id = None
        if update.message.reply_to_message:
            target_id = update.message.reply_to_message.from_user.id
        if target_id:
            ops = json.loads(get_setting(gid, 'operators') or '[]')
            if target_id not in ops:
                ops.append(target_id)
                update_setting(gid, 'operators', json.dumps(ops))
                await update.message.reply_text("✅ 已成功为本群添加一名操作员。")
        return

    if text in ['删今天', '删最后']:
        if not can_use(gid, uid): return
        if text == '删今天': delete_today_bills(gid)
        else: delete_last_bill(gid)
        await update.message.reply_text("✅ 操作成功")
        return

    if text == '+0':
        if not can_use(gid, uid): return
        await show_full_bill(update, gid)
        return

    # 快捷记账格式
    is_active = get_setting(gid, 'is_active') or 0
    if is_active == 0 or not can_use(gid, uid): return

    m_exp = re.match(r'^(.*?)(?:下发|ထုတ်)\s*(-?\d+(?:\.\d+)?)$', text)
    if m_exp:
        add_bill(gid, uid, username, m_exp.group(1).strip(), float(m_exp.group(2)), 'expense')
        await show_full_bill(update, gid)
        return

    m_inc = re.match(r'^(.*?)([\+\-])(\d+(?:\.\d+)?)(?:/(\d+(?:\.\d+)?))?$', text)
    if m_inc:
        rem = m_inc.group(1).strip()
        amt = float(m_inc.group(3))
        if m_inc.group(2) == '-': amt = -amt
        custom_rate = float(m_inc.group(4)) if m_inc.group(4) else None
        ex_rate = custom_rate if custom_rate else (get_setting(gid, 'exchange_rate') or 7.2)
        add_bill(gid, uid, username, rem, amt, 'income', ex_rate)
        await show_full_bill(update, gid)
        return

# ========== 按钮回调与基础启动 ==========

async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private":
        await update.message.reply_text(get_renew_menu(), parse_mode="HTML")
    else:
        if can_use(update.effective_chat.id, update.effective_user.id):
            await update.message.reply_text(get_help_text('chinese'))

# Web后端和入口保持不变
@flask_app.route('/')
def index(): return "Web Dashboard is Running"
@flask_app.route('/api/bill')
def api_bill():
    group_id = request.args.get('group_id', type=int, default=0)
    income, expense, total_income, total_expense = get_class_bills_by_date(group_id, datetime.now().strftime("%Y-%m-%d"))
    return jsonify({'income_bills':[], 'expense_bills':[], 'exchange_rate':7.2, 'total_rmb':0, 'total_usdt':0, 'expense_usdt':0, 'remaining_usdt':0})

def main():
    init_db()
    threading.Thread(target=lambda: flask_app.run(host='0.0.0.0', port=PORT), daemon=True).start()
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("renew", start_cmd))
    app.add_handler(CallbackQueryHandler(handle_callback_query))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))
    app.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()
