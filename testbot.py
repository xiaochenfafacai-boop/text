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

# 1. 允许设置多位机器人主人(超级管理员ID，在这里添加3-4个你的合作人ID)
MASTER_USERS = [8782394486, 123456789, 987654321] 

# 2. 销售收款配置
TRON_ADDRESS = "TVnjLwDrGjYVRTa1ukfoE2mFTmCxtrjoCw"
MONTHLY_PRICE = "1 USDT"

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
    c.execute('''CREATE TABLE IF NOT EXISTS settings
                 (group_id INTEGER PRIMARY KEY,
                  operators TEXT DEFAULT '[]',
                  exchange_rate REAL DEFAULT 7.2,
                  fee_rate REAL DEFAULT 0,
                  is_active INTEGER DEFAULT 0,
                  language TEXT DEFAULT 'chinese',
                  timezone TEXT DEFAULT 'Asia/Shanghai',
                  show_usdt INTEGER DEFAULT 1)''')
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

# 核心修改：支持多主人验证
def is_master(user_id):
    return user_id in MASTER_USERS

def is_operator(group_id, user_id):
    ops = json.loads(get_setting(group_id, 'operators') or '[]')
    return user_id in ops

def can_use(group_id, user_id):
    return is_master(user_id) or is_operator(group_id, user_id)

def add_bill(group_id, user_id, username, remark, amount, bill_type, exchange_rate=None):
    if exchange_rate is None:
        exchange_rate = get_setting(group_id, 'exchange_rate') or 7.2
    if bill_type == 'income':
        usdt_amount = amount / exchange_rate
    else:
        usdt_amount = amount
    tz_str = get_setting(group_id, 'timezone') or 'Asia/Shanghai'
    now, _, full_time = get_current_time(tz_str)
    date_str = now.strftime("%Y-%m-%d")
    
    conn = sqlite3.connect('bot_data.db')
    c = conn.cursor()
    c.execute('''INSERT INTO bills 
                 (group_id, user_id, username, remark, amount, usdt_amount, exchange_rate, bill_type, timestamp, date_str, is_settled)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)''',
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
    updated = c.rowcount
    conn.commit()
    conn.close()
    return updated

def delete_today_bills(group_id):
    tz_str = get_setting(group_id, 'timezone') or 'Asia/Shanghai'
    now, _, _ = get_current_time(tz_str)
    today_date = now.strftime("%Y-%m-%d")
    conn = sqlite3.connect('bot_data.db')
    c = conn.cursor()
    c.execute("DELETE FROM bills WHERE group_id = ? AND date_str = ?", (group_id, today_date))
    deleted = c.rowcount
    conn.commit()
    conn.close()
    return deleted

def delete_last_bill(group_id):
    conn = sqlite3.connect('bot_data.db')
    c = conn.cursor()
    c.execute("SELECT id FROM bills WHERE group_id = ? ORDER BY id DESC LIMIT 1", (group_id,))
    last = c.fetchone()
    if last:
        c.execute("DELETE FROM bills WHERE id = ?", (last[0],))
        deleted = 1
    else:
        deleted = 0
    conn.commit()
    conn.close()
    return deleted

def delete_all_bills(group_id):
    conn = sqlite3.connect('bot_data.db')
    c = conn.cursor()
    c.execute("DELETE FROM bills WHERE group_id = ?", (group_id,))
    deleted = c.rowcount
    conn.commit()
    conn.close()
    return deleted

def delete_user_bills(group_id, name):
    conn = sqlite3.connect('bot_data.db')
    c = conn.cursor()
    c.execute("DELETE FROM bills WHERE group_id = ? AND (LOWER(username) = ? OR LOWER(remark) = ?)", (group_id, name.lower(), name.lower()))
    deleted = c.rowcount
    conn.commit()
    conn.close()
    return deleted

# ========== Web 页面与 API ==========

@flask_app.route('/')
def index():
    return '''
    <!DOCTYPE html>
    <html>
    <head><meta charset="UTF-8"><title>课时历史账单系统</title><style>*{margin:0;padding:0;box-sizing:border-box;}body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Arial,sans-serif;background:#f0f2f5;padding:20px;}.container{max-width:1400px;margin:0 auto;background:white;border-radius:16px;box-shadow:0 4px 20px rgba(0,0,0,0.1);overflow:hidden;}.header{background:linear-gradient(135deg,#667eea 0%,#764ba2 100%);color:white;padding:24px 30px;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:15px;}.header-text{flex:1;}.header h1{font-size:28px;margin-bottom:8px;}.date-picker-box{background:rgba(255,255,255,0.2);padding:10px 15px;border-radius:8px;color:white;}.date-picker-box label{font-size:14px;margin-right:8px;font-weight:bold;}.date-picker-box input{border:none;padding:6px 10px;border-radius:4px;font-size:14px;outline:none;}.content{padding:24px 30px;}.section{margin-bottom:32px;}.section-title{font-size:18px;font-weight:600;margin-bottom:16px;padding-bottom:8px;border-bottom:2px solid #667eea;}table{width:100%;border-collapse:collapse;font-size:14px;}th,td{padding:12px 10px;text-align:left;border-bottom:1px solid #eef2f6;}th{background:#f8f9fc;font-weight:600;}.stats-box{background:linear-gradient(135deg,#f8f9fc 0%,#f0f2f5 100%);border-radius:12px;padding:24px;margin-top:20px;}.stats-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:16px;}.stat-card{background:white;padding:16px;border-radius:12px;text-align:center;}.stat-label{font-size:12px;color:#888;margin-bottom:8px;}.stat-value{font-size:24px;font-weight:700;color:#333;}.stat-item{display:flex;justify-content:space-between;padding:8px 0;border-bottom:1px solid #eef2f6;}.stat-name{font-weight:500;color:#333;}.stat-number{color:#667eea;font-weight:600;}.loading{text-align:center;padding:50px;color:#888;}</style></head>
    <body>
        <div class="container">
            <div class="header">
                <div class="header-text">
                    <h1>📋 实时课堂账单历史明细</h1>
                    <p id="dateInfo">默认同步实时账单</p>
                </div>
                <div class="date-picker-box">
                    <label>📅 选择账单日期:</label>
                    <input type="date" id="targetDate" onchange="onDateChange()">
                </div>
            </div>
            <div class="content" id="content"><div class="loading">正在同步实时账单...</div></div>
        </div>
        <script>
            let GROUP_ID = null;
            let currentSelectedDate = "";

            const today = new Date();
            const yyyy = today.getFullYear();
            let mm = today.getMonth() + 1;
            let dd = today.getDate();
            if (mm < 10) mm = '0' + mm;
            if (dd < 10) dd = '0' + dd;
            currentSelectedDate = `${yyyy}-${mm}-${dd}`;
            document.getElementById('targetDate').value = currentSelectedDate;

            function getGroupID() { 
                const urlParams = new URLSearchParams(window.location.search); 
                GROUP_ID = urlParams.get('group_id'); 
                if (!GROUP_ID) { 
                    document.getElementById('content').innerHTML = '<div class="loading">❌ 请通过机器人的 "查看完整账单" 按钮访问</div>'; 
                    return false; 
                } 
                return true; 
            }

            function onDateChange() {
                currentSelectedDate = document.getElementById('targetDate').value;
                loadData();
            }

            async function loadData() { 
                if (!GROUP_ID) return;
                try { 
                    const response = await fetch(`/api/bill?group_id=${GROUP_ID}&date=${currentSelectedDate}`); 
                    const data = await response.json(); 
                    if (data.error || (!data.income_bills.length && !data.expense_bills.length)) { 
                        document.getElementById('content').innerHTML = `<div class="loading">📅 ${currentSelectedDate} 暂无账单数据记录</div>`; 
                        return; 
                    }
                    let suffix = data.show_usdt ? ' USDT' : '';
                    let html = '';
                    
                    if (data.income_bills && data.income_bills.length > 0) { 
                        html += `<div class="section"><div class="section-title">📥 入款记录 (${data.income_bills.length} 笔)</div><table><thead><tr><th>备注</th><th>时间</th><th>金额(元)</th><th>汇率</th><th>等值数量</th><th>操作人</th></tr></thead><tbody>`; 
                        for (const bill of data.income_bills) { 
                            html += `<tr><td><b>${bill.remark}</b></td><td>${bill.time}</td><td>${bill.amount}</td><td>${bill.exchange_rate}</td><td>${bill.usdt}${suffix}</td><td>${bill.username}</td></tr>`; 
                        } 
                        html += `</tbody></table></div>`; 
                    }
                    
                    if (data.expense_bills && data.expense_bills.length > 0) { 
                        html += `<div class="section"><div class="section-title">📤 下发记录 (${data.expense_bills.length} 笔)</div><table><thead><tr><th>备注</th><th>时间</th><th>下发数量</th><th>操作人</th></tr></thead><tbody>`; 
                        for (const bill of data.expense_bills) { 
                            html += `<tr><td><b>${bill.remark}</b></td><td>${bill.time}</td><td>${bill.usdt}${suffix}</td><td>${bill.username}</td></tr>`; 
                        } 
                        html += `</tbody></table></div>`; 
                    }
                    
                    if (data.remark_stats && data.remark_stats.length > 0) { 
                        html += `<div class="section"><div class="section-title">📊 备注分类统计</div>`; 
                        for (const stat of data.remark_stats) { 
                            html += `<div class="stat-item"><span class="stat-name">📝 ${stat.remark}</span><span class="stat-number">${stat.count}笔 | ${stat.amount}元 | ${stat.usdt}${suffix}</span></div>`; 
                        } 
                        html += `</div>`; 
                    }
                    
                    html += `<div class="stats-box"><div class="stats-grid"><div class="stat-card"><div class="stat-label">💰 费率</div><div class="stat-value">${data.fee_rate}%</div></div><div class="stat-card"><div class="stat-label">💱 汇率</div><div class="stat-value">${data.exchange_rate}</div></div><div class="stat-card"><div class="stat-label">📥 总入款(元)</div><div class="stat-value">${data.total_rmb}</div></div><div class="stat-card"><div class="stat-label">💵 总入款数量</div><div class="stat-value">${data.total_usdt}${suffix}</div></div><div class="stat-card"><div class="stat-label">📤 已下发</div><div class="stat-value">${data.expense_usdt}${suffix}</div></div><div class="stat-card"><div class="stat-label">📊 未下发</div><div class="stat-value">${data.remaining_usdt}${suffix}</div></div></div></div>`;
                    
                    document.getElementById('content').innerHTML = html;
                } catch (err) { 
                    document.getElementById('content').innerHTML = '<div class="loading">❌ 数据解析错误或网络异常，请重新从群内打开链接</div>'; 
                }
            }
            if (getGroupID()) { 
                loadData(); 
                setInterval(() => {
                    const t = new Date();
                    let m = t.getMonth() + 1; let d = t.getDate();
                    if (m < 10) m = '0' + m; if (d < 10) d = '0' + d;
                    if (currentSelectedDate === `${t.getFullYear()}-${m}-${d}`) {
                        loadData();
                    }
                }, 4000);
            }
        </script>
    </body>
    </html>
    '''

@flask_app.route('/api/bill')
def api_bill():
    try:
        group_id = request.args.get('group_id', type=int, default=0)
        tz_str = get_setting(group_id, 'timezone') or 'Asia/Shanghai'
        now, _, _ = get_current_time(tz_str)
        today_str = now.strftime("%Y-%m-%d")
        target_date = request.args.get('date', default=today_str)
        
        income, expense, total_income, total_expense = get_class_bills_by_date(group_id, target_date)
        
        rate = get_setting(group_id, 'exchange_rate') or 7.2
        fee_rate = get_setting(group_id, 'fee_rate') or 0
        show_usdt = get_setting(group_id, 'show_usdt') or 1
        
        total_rmb = total_income[0] if (total_income and total_income[0]) else 0
        total_usdt = total_income[1] if (total_income and total_income[1]) else 0
        expense_usdt = total_expense[0] if (total_expense and total_expense[0]) else 0
        
        income_bills = []
        expense_bills = []
        
        for row in income:
            remark, username, amount, usdt, ex_rate, ts = row
            time_str = ts[5:16] if (ts and len(ts) > 11) else (ts or '-')
            income_bills.append({
                'remark': remark or '-', 'username': username or '未知', 'amount': f"{amount or 0:.0f}", 
                'usdt': f"{usdt or 0:.2f}", 'exchange_rate': f"{ex_rate or rate:.2f}", 'time': time_str
            })
            
        for row in expense:
            remark, username, usdt, ex_rate, ts = row
            time_str = ts[5:16] if (ts and len(ts) > 11) else (ts or '-')
            expense_bills.append({
                'remark': remark or '-', 'username': username or '未知', 'usdt': f"{usdt or 0:.2f}", 'time': time_str
            })

        remark_stats = []
        conn = sqlite3.connect('bot_data.db')
        c = conn.cursor()
        c.execute("SELECT remark, COUNT(*), SUM(amount), SUM(usdt_amount) FROM bills WHERE group_id = ? AND date_str = ? AND bill_type = 'income' GROUP BY remark ORDER BY SUM(usdt_amount) DESC", (group_id, target_date))
        for row in c.fetchall():
            remark_stats.append({
                'remark': row[0] if row[0] else '无备注', 'count': row[1] or 0, 
                'amount': f"{row[2] or 0:.0f}", 'usdt': f"{row[3] or 0:.2f}"
            })
        conn.close()
        
        return jsonify({
            'exchange_rate': f"{rate:.2f}", 'fee_rate': f"{fee_rate:.0f}", 'total_rmb': f"{total_rmb:.0f}", 
            'total_usdt': f"{total_usdt:.2f}", 'expense_usdt': f"{expense_usdt:.2f}", 
            'remaining_usdt': f"{total_usdt - expense_usdt:.2f}", 'show_usdt': int(show_usdt), 
            'income_bills': income_bills, 'expense_bills': expense_bills, 'remark_stats': remark_stats
        })
    except Exception as e:
        logging.error(f"API Error: {str(e)}")
        return jsonify({'error': True, 'msg': str(e)}), 500

# ========== 文本菜单渲染 (包含续费私聊逻辑) ==========

def get_help_text(lang):
    if lang == 'myanmar':
        return """
🤖 *စာရင်းကိုင်ဘော့ အကူအညီ* (Help)

📌 *စာရင်းသွင်းရန် ပုံစံများ：*
`+1000` - Ngwe Win 1000 Kyat
`-1000` - Ngwe Win -1000 Kyat
`MatChet+2000` - MatChet Gyi Ngwe Thwin Ranyan
`MatChet-2000` - MatChet Gyi Ngwe Hnut Ranyan
`Thut50` - 50 USDT Thut Ranyan
`MatChetThut50` - MatChet Gyi 50 USDT Thut Ranyan
`+0` - YaNay SaYinChoke KyiRanyan

📌 *စီမံခန့်ခွဲရေး ကွတ်ကီးများ：*
`အတန်းစ` - SaYinKoing Sinit PhwintChin (上课)
`အတန်းဆင်း` - SaYinPate Pyee ShinLinChin (下课)
`ငွေလဲနှုန်း 7.2` - NgweLeHnoat ThatMatRanyan
`အော်ပရေတာခန့်ရန်` - SaYinKoing KhantRanyan 
`အော်ပရေတာစာရင်း` - Operator SaYin KyiRanyan
`ဘာသာစကား` - BarTharSakar PyaungRanyan (中文/မြန်မာ)
`အချိန်သတ်မှတ်` - AChainZone PyaungRanyan

📌 *ဖျက်သိမ်းခြင်း ကွတ်ကီးများ：*
`ယနေ့ဖျက်` - YaNay MatTan ArLong PhyetRanyan
`နောက်ဆုံးဖျက်` - NauatSone SaYin 1 Saung PhyetRanyan

💡 *ဘော့တ်သက်တမ်းတိုးရန် (续费)：*
စက်ရုပ်ကို သီးသန့်စာတို (Private Chat) ပေးပို့ပြီး `续费` သို့မဟုတ် `/renew` ဟု ရိုက်နှိပ်ပါ။
"""
    else:
        return """
🤖 *记账机器人使用指南*

📌 *记账格式：*
`+1000` - 入款1000元
`-1000` - 入款-1000元 (扣减款)
`备注+2000` - 带备注入款
`备注-2000` - 带备注减款
`下发50` - 下发50 USDT
`备注下发50` - 带备注下发50 USDT
`+0` - 查看今日汇总

📌 *管理命令（任意全权拥有者或操作人）：*
`上课` - 开启记账模式（开始全新记账）
`下课` - 关闭记账模式（锁定并结束本轮，但不清除历史数据）
`设置汇率 7.2` - 设置汇率
`设置操作人` - 设置操作人（回复某人消息后发送，支持无限添加）
`查看操作员列表` - 查看操作人列表
`改语言` - 切换语言（中文/缅甸语）
`设置时间` - 设置时区

📌 *删除命令：*
`删今天` - 删除今日所有账单
`删最后` - 删除最后一笔账单

💡 *机器人续费*：
请直接与机器人**【私聊】**发送 `续费` 或点击 `/renew` 获取TRC-20专属支付地址和菜单。
"""

# 新增：私聊续费详情菜单
def get_renew_menu():
    return f"""
✨ <b>🛒 记账机器人续费服务系统</b> ✨
---
📊 <b>租用费率：</b> <code>{MONTHLY_PRICE} / 月 (30天)</code>
🛡️ <b>多端主人：</b> 现已支持3-4个超级管理员账号联合操控

⚠️ <b>续费支付说明：</b>
请通过支持 <b>TRC-20 (TRON)</b> 网络通路的钱包，向下方专属充值地址转入等额资金：

💰 <code>{TRON_ADDRESS}</code>
<i>(点击上方地址可直接复制)</i>

💡 <b>提示：</b>转账完成后，请将账单截图及您的【群组ID】打包发送至官方客服审核开通。
"""

def get_bill_content(income, expense, total_rmb, total_usdt, expense_usdt, rate, today_date, lang):
    unit = "U" 
    if lang == 'myanmar':
        income_title, expense_title, rate_text, total_text, exp_text, rem_text = "📥 ငွေဝင်", "📤 ထုတ်ငွေ", "💰 လဲနှုန်း", "📊 စုစုပေါင်း", "📊 ထုတ်ပြီး", "📊 ကျန်ငွေ"
    else:
        income_title, expense_title, rate_text, total_text, exp_text, rem_text = "📥 入款", "📤 下发", "💰 汇率", "📊 总入款", "📊 已下发", "📊 未下发"
        
    message = f"📊 账单汇总 ({today_date})\n\n"
    if income:
        message += f"{income_title}:\n"
        for bill in income[:5]:
            remark, username, amount, usdt, ex_rate, ts = bill
            time_short = ts[11:16] if (ts and len(ts) > 16) else ''
            rem_str = f"【{remark}】" if remark else ""
            message += f"  {time_short} {rem_str}{amount or 0:.0f}/{ex_rate or rate:.1f}={usdt or 0:.1f}{unit}\n"
        message += "\n"
        
    if expense:
        message += f"{expense_title}:\n"
        for bill in expense[:5]:
            remark, username, usdt, ex_rate, ts = bill
            time_short = ts[11:16] if (ts and len(ts) > 16) else ''
            rem_str = f"【{remark}】" if remark else ""
            message += f"  {time_short} {rem_str}{usdt or 0:.1f}{unit}\n"
        message += "\n"
        
    message += f"{rate_text}: {rate:.2f}\n"
    message += f"{total_text}: {total_rmb:.0f} | {total_usdt:.1f}{unit}\n"
    message += f"{exp_text}: {expense_usdt:.1f}{unit}\n"
    message += f"{rem_text}: {total_usdt - expense_usdt:.1f}{unit}"
    return message

async def show_full_bill(update: Update, gid):
    tz_str = get_setting(gid, 'timezone') or 'Asia/Shanghai'
    now, _, _ = get_current_time(tz_str)
    today_date = now.strftime("%Y-%m-%d")
    
    income, expense, total_income, total_expense = get_class_bills_by_date(gid, today_date)
    rate = get_setting(gid, 'exchange_rate') or 7.2
    lang = get_setting(gid, 'language') or 'chinese'
    total_rmb = total_income[0] or 0
    total_usdt = total_income[1] or 0
    expense_usdt = total_expense[0] or 0
    
    message = get_bill_content(income, expense, total_rmb, total_usdt, expense_usdt, rate, today_date, lang)
    keyboard = [
        [InlineKeyboardButton("📊 完整账单 (Web)", url=f"{WEB_URL}?group_id={gid}")],
        [InlineKeyboardButton("📖 帮助 (Help)", callback_data='show_help')]
    ]
    await update.message.reply_text(message, reply_markup=InlineKeyboardMarkup(keyboard))

# ========== 统一文本监听与指令处理 ==========

async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    chat_type = update.effective_chat.type
    gid = update.effective_chat.id
    uid = update.effective_user.id
    username = update.effective_user.first_name
    
    # 【新增售卖业务逻辑】：优先处理私聊续费业务
    if chat_type == "private":
        if text in ["续费", "续期", "购买", "pay", "renew"]:
            keyboard = [[InlineKeyboardButton("💳 确认复制收款地址", callback_data='copy_address')]]
            await update.message.reply_text(get_renew_menu(), parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
            return
        # 私聊如果不满足上述词汇，默认显示通用业务帮助
        await update.message.reply_text(get_help_text('chinese'))
        return

    # 群组内的原有记账业务逻辑控制
    if text in ['上课', 'အတန်းစ']:
        if not can_use(gid, uid): return
        update_setting(gid, 'is_active', 1)
        msg = "🟢 记账模式已开启！请发送数据记账。"
        if get_setting(gid, 'language') == 'myanmar':
            msg = "🟢 စာရင်းကိုင်မုဒ်ကို ဖွင့်လိုက်ပါပြီ။"
        await update.message.reply_text(msg)
        return

    if text in ['下课', 'အတန်းဆင်း']:
        if not can_use(gid, uid): return
        is_active = get_setting(gid, 'is_active') or 0
        if is_active == 0:
            return
        
        await show_full_bill(update, gid)
        tz_str = get_setting(gid, 'timezone') or 'Asia/Shanghai'
        now, _, _ = get_current_time(tz_str)
        today_date = now.strftime("%Y-%m-%d")
        
        settle_today_bills(gid, today_date)
        update_setting(gid, 'is_active', 0)
        
        msg = f"🔴 下课成功！在线账单已归档锁定。"
        if get_setting(gid, 'language') == 'myanmar':
            msg = "🔴 အတန်းဆင်းခြင်း အောင်မြင်ပါသည်။"
        await update.message.reply_text(msg)
        return

    if text in ['查看操作员列表', 'အော်ပရေတာစာရင်း']:
        if not can_use(gid, uid): return
        ops = json.loads(get_setting(gid, 'operators') or '[]')
        if not ops:
            await update.message.reply_text("📋 暂无操作人")
            return
        message = "📋 操作人列表:\n"
        for oid in ops:
            try:
                member = await context.bot.get_chat_member(gid, oid)
                message += f"  • {member.user.first_name}\n"
            except:
                message += f"  • ID: {oid}\n"
        await update.message.reply_text(message)
        return

    # 支持多主人（3-4人）架构下的无限操作人设置
    if text.startswith('设置操作人') or text.startswith('အော်ပရေတာခန့်ရန်'):
        if not is_master(uid):
            await update.message.reply_text("❌ 只有合法的超级管理员权限可以设置群内操作员")
            return
        
        target_id = None
        target_name = ""

        if update.message.entities:
            for entity in update.message.entities:
                if entity.type == "text_mention" and entity.user:
                    target_id = entity.user.id
                    target_name = entity.user.first_name
                    break
                elif entity.type == "mention":
                    mention_text = text[entity.offset:entity.offset + entity.length]
                    target_name = mention_text

        if not target_id:
            m_ops = re.match(r'^(?:设置操作人|အော်ပရေတာခန့်ရန်)\s+(\d+)$', text)
            if m_ops:
                target_id = int(m_ops.group(1))
                target_name = f"UID: {target_id}"

        if not target_id and update.message.reply_to_message:
            target = update.message.reply_to_message.from_user
            target_id = target.id
            target_name = target.first_name

        if target_id:
            ops = json.loads(get_setting(gid, 'operators') or '[]')
            if target_id not in ops:
                ops.append(target_id)
                update_setting(gid, 'operators', json.dumps(ops))
                await update.message.reply_text(f"✅ 授权成功！已被多端主人设为该群操作人： {target_name}")
            else:
                await update.message.reply_text(f"ℹ️ {target_name} 已经拥有操作人权限")
        else:
            if target_name.startswith("@"):
                await update.message.reply_text(
                    f"❌ 无法提取 {target_name} 的加密 ID。\n\n"
                    f"💡 解决方法：\n"
                    f"在输入命令时，**请在弹出的群成员列表中用手指点击选中他**。发送后多端主人即可直接设置！"
                )
            else:
                await update.message.reply_text("❌ 未识别到有效用户。格式：`设置操作人 @用户名`（需点击群成员高亮蓝色发送）。")
        return

    if text in ['改语言', 'ဘာသာစကား']:
        if not can_use(gid, uid): return
        current = get_setting(gid, 'language') or 'chinese'
        new_lang = 'myanmar' if current == 'chinese' else 'chinese'
        update_setting(gid, 'language', new_lang)
        msg = "✅ 已切换为中文" if new_lang == 'chinese' else "✅ မြန်မာဘာသာသို့ ပြောင်းလဲပြီးပါပြီ"
        await update.message.reply_text(msg)
        return

    if text in ['删今天', 'ယနေ့ဖျက်']:
        if not can_use(gid, uid): return
        deleted = delete_today_bills(gid)
        await update.message.reply_text(f"✅ 已删除今日所有账单")
        return

    if text in ['删最后', 'နောက်ဆုံးဖျက်']:
        if not can_use(gid, uid): return
        deleted = delete_last_bill(gid)
        await update.message.reply_text("✅ 已删除最后一笔" if deleted else "📭 暂无账单")
        return

    if text in ['全部清单', 'စာရင်းအားလုံးဖျက်']:
        if not can_use(gid, uid): return
        delete_all_bills(gid)
        await update.message.reply_text("✅ 已清空全量总历史账单")
        return

    m_rate = re.match(r'^(?:设置汇率|ငွေလဲနှုန်း)\s+(\d+(?:\.\d+)?)$', text)
    if m_rate:
        if not can_use(gid, uid): return
        rate = float(m_rate.group(1))
        update_setting(gid, 'exchange_rate', rate)
        await update.message.reply_text(f"✅ 汇率已设为 {rate}")
        return

    m_tz = re.match(r'^(?:设置时间|အချိန်သတ်မှတ်)\s+([a-zA-Z]+)$', text)
    if m_tz:
        if not can_use(gid, uid): return
        tz_name = m_tz.group(1).lower()
        if tz_name in TIMEZONES:
            update_setting(gid, 'timezone', TIMEZONES[tz_name])
            await update.message.reply_text("✅ 时区修改成功")
        return

    m_del_user = re.match(r'^(?:清单\+|မှတ်တမ်းဖျက်\+)(.+)$', text)
    if m_del_user:
        if not can_use(gid, uid): return
        target_name = m_del_user.group(1).strip()
        delete_user_bills(gid, target_name)
        await update.message.reply_text(f"✅ 已清空【{target_name}】的账单")
        return

    is_active = get_setting(gid, 'is_active') or 0
    if is_active == 0 or not can_use(gid, uid):
        return

    if text == '+0':
        await show_full_bill(update, gid)
        return

    m_exp = re.match(r'^(.*?)(?:下发|ထုတ်)\s*(-?\d+(?:\.\d+)?)$', text)
    if m_exp:
        rem = m_exp.group(1).strip()
        amt = float(m_exp.group(2))
        add_bill(gid, uid, username, rem, amt, 'expense')
        await show_full_bill(update, gid)
        return

    m_inc = re.match(r'^(.*?)([\+\-])(\d+(?:\.\d+)?)(?:/(\d+(?:\.\d+)?))?$', text)
    if m_inc:
        rem = m_inc.group(1).strip()
        sign = m_inc.group(2)
        amt = float(m_inc.group(3))
        if sign == '-': 
            amt = -amt
        custom_rate = float(m_inc.group(4)) if m_inc.group(4) else None
        ex_rate = custom_rate if custom_rate else (get_setting(gid, 'exchange_rate') or 7.2)
        
        add_bill(gid, uid, username, rem, amt, 'income', ex_rate)
        await show_full_bill(update, gid)
        return

# ========== 内联按钮回调控制 ==========

async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    gid = update.effective_chat.id
    uid = update.effective_user.id
    
    # 续费地址回调响应（私聊）
    if query.data == 'copy_address':
        await query.answer("已为您核对：TRC20 地址复制成功！", show_alert=True)
        return

    # 群组内的账单按键操作保护
    if update.effective_chat.type != "private":
        if not can_use(gid, uid):
            await query.answer("❌ 您当前不在本群操作员或多主人名单中，无法越权点击", show_alert=True)
            return

    await query.answer()
    data = query.data

    if data == 'show_help':
        lang = get_setting(gid, 'language') or 'chinese'
        keyboard = [[InlineKeyboardButton("🔙 返回记账 (Back)", callback_data='back_to_main')]]
        await query.edit_message_text(get_help_text(lang), reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if data == 'back_to_main':
        tz_str = get_setting(gid, 'timezone') or 'Asia/Shanghai'
        now, _, _ = get_current_time(tz_str)
        today_date = now.strftime("%Y-%m-%d")
        
        income, expense, total_income, total_expense = get_class_bills_by_date(gid, today_date)
        rate = get_setting(gid, 'exchange_rate') or 7.2
        lang = get_setting(gid, 'language') or 'chinese'
        total_rmb = total_income[0] or 0
        total_usdt = total_income[1] or 0
        expense_usdt = total_expense[0] or 0
        
        message = get_bill_content(income, expense, total_rmb, total_usdt, expense_usdt, rate, today_date, lang)
        keyboard = [
            [InlineKeyboardButton("📊 完整账单 (Web)", url=f"{WEB_URL}?group_id={gid}")],
            [InlineKeyboardButton("📖 帮助 (Help)", callback_data='show_help')]
        ]
        await query.edit_message_text(message, reply_markup=InlineKeyboardMarkup(keyboard))

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_type = update.effective_chat.type
    if chat_type == "private":
        # 私聊直接弹出销售与充值菜单
        keyboard = [[InlineKeyboardButton("💳 充值续费 (TRC-20)", callback_data='copy_address')]]
        await update.message.reply_text(get_renew_menu(), parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        gid = update.effective_chat.id
        uid = update.effective_user.id
        if not can_use(gid, uid): return
        lang = get_setting(gid, 'language') or 'chinese'
        await update.message.reply_text(get_help_text(lang))

async def renew_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # 处理通过 /renew 进来的购买请求
    keyboard = [[InlineKeyboardButton("💳 充值续费 (TRC-20)", callback_data='copy_address')]]
    await update.message.reply_text(get_renew_menu(), parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

def run_web():
    flask_app.run(host='0.0.0.0', port=PORT)

def main():
    init_db()
    print("🤖 多端售卖商版 - 强鉴权高级记账分销版启动...")
    threading.Thread(target=run_web, daemon=True).start()
    
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", start_cmd))
    app.add_handler(CommandHandler("renew", renew_cmd))
    app.add_handler(CallbackQueryHandler(handle_callback_query))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))
    
    app.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()
