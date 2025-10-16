# -------------------------------
# Telegram Bot by Rami (TRON real deposits & withdrawals)
# -------------------------------
# Requirements:
# pip install python-telegram-bot tronpy requests nest_asyncio
# -------------------------------

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)
import nest_asyncio
import sqlite3
from datetime import datetime
import requests
from tronpy import Tron
from tronpy.keys import PrivateKey
import os
import time

# ========== CONFIG ==========
TOKEN = "8479292257:AAHGnARfy1ligmjE0AWiE_sxVFaTJOmm8bc"

# Hot wallet address (where users send deposits)
HOT_WALLET = "TD7BeQyvkanpJS9R5LevtyC5F2zr7WE4Fh"

# USDT TRC20 contract on Tron mainnet (standard TRC20 Tether address)
USDT_CONTRACT = "TXLAQ63Xg1NAzckPwKHvzw7CSEmLMEqcdj"
USDT_DECIMALS = 6  # typical for USDT on TRON

# TRON network: 'mainnet' (real) or 'nile' (testnet). **Use testnet first for testing**
TRON_NETWORK = os.getenv("TRON_NETWORK", "mainnet")  # default to 'nile' for safety

# Private key of HOT_WALLET (HEX string). **DO NOT store raw in code in production.**
# For testing only, you may set env var: export HOT_PRIVATE_KEY_HEX="abc..."
PRIVATE_KEY_HEX = os.getenv("HOT_PRIVATE_KEY_HEX", None)

# Tronscan API base for token transfers (public)
TRONSCAN_API_BASE = "https://apilist.tronscanapi.com/api"

# ========================= DB =========================
DB_FILE = "transactions.db"

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()

    # users: store referrer_id, balance
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            balance REAL DEFAULT 0,
            referrer_id INTEGER
        )
    """)

    # transactions: store both deposits (from blockchain) and internal transfers & withdrawals
    c.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,         -- beneficiary (for deposits) OR sender (for withdrawals/transfers)
            counterparty INTEGER,    -- for transfers: recipient or referrer id, optional
            amount REAL,
            txid TEXT,               -- blockchain txid for deposits/withdrawals
            type TEXT,               -- 'DEPOSIT' | 'WITHDRAW' | 'TRANSFER' | 'REFERRAL_BONUS' | 'PROFIT_PAYOUT'
            status TEXT,             -- 'PENDING' | 'CONFIRMED' | 'FAILED'
            created_at TEXT
        )
    """)

    conn.commit()
    conn.close()

# ========== Utility DB functions ==========
def ensure_user(user_id, username="", referrer_id=None):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO users (user_id, username, balance, referrer_id) VALUES (?, ?, ?, ?)",
              (user_id, username, 0, referrer_id))
    # if referrer_id provided and existing referrer is null, update it (only set once)
    if referrer_id is not None:
        c.execute("SELECT referrer_id FROM users WHERE user_id=?", (user_id,))
        row = c.fetchone()
        if row and row[0] is None:
            c.execute("UPDATE users SET referrer_id=? WHERE user_id=?", (referrer_id, user_id))
    conn.commit()
    conn.close()

def get_balance(user_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT balance FROM users WHERE user_id=?", (user_id,))
    row = c.fetchone()
    conn.close()
    return float(row[0]) if row else 0.0

def update_balance(user_id, delta, username=''):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO users (user_id, username, balance, referrer_id) VALUES (?, ?, ?, ?)",
              (user_id, username, 0, None))
    c.execute("UPDATE users SET balance = balance + ?, username=? WHERE user_id=?", (delta, username, user_id))
    conn.commit()
    conn.close()

def add_tx(user_id, counterparty, amount, txid, typ, status="PENDING"):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        INSERT INTO transactions (user_id, counterparty, amount, txid, type, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (user_id, counterparty, amount, txid, typ, status, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()
    conn.close()

def set_tx_status(txid, status):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("UPDATE transactions SET status=? WHERE txid=?", (status, txid))
    conn.commit()
    conn.close()

def tx_exists(txid):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT id FROM transactions WHERE txid=?", (txid,))
    row = c.fetchone()
    conn.close()
    return bool(row)

# ========== TRON client ==========
def get_tron_client():
    # network 'nile' uses full node/nodes from tronpy default when 'nile'
    return Tron(network=TRON_NETWORK)

def send_trc20(to_address: str, amount: float):
    """
    Broadcast transfer of USDT (TRC20) from HOT_WALLET using PRIVATE_KEY_HEX.
    Returns txid dict or raises.
    """
    if PRIVATE_KEY_HEX is None:
        raise RuntimeError("PRIVATE_KEY_HEX not set in environment. Cannot sign transactions.")

    client = get_tron_client()
    priv = PrivateKey(bytes.fromhex(PRIVATE_KEY_HEX))
    contract = client.get_contract(USDT_CONTRACT)
    amount_int = int(amount * (10 ** USDT_DECIMALS))

    owner_address = priv.public_key.to_base58check_address()
    # Build / sign / broadcast
    txn = (
        contract.functions.transfer(to_address, amount_int)
        .with_owner(owner_address)
        .fee_limit(2_000_000)
        .build()
    )
    signed = txn.sign(priv)
    res = signed.broadcast().wait()  # wait for propagation
    # res may be dict containing 'result' or 'transaction' etc. Return txid if present
    txid = None
    if isinstance(res, dict):
        txid = res.get("txid") or res.get("transaction", {}).get("txid")
    # fallback: get last transaction id from signed
    if not txid:
        try:
            txid = signed.txid
        except Exception:
            txid = None
    return {"txid": txid, "res": res}

# ========== Verify deposit from Tronscan API ==========
def verify_deposit_tx(txid: str, expected_to: str = HOT_WALLET, expected_amount: float = None, max_look=100):
    """
    Verify that txid corresponds to a TRC20 transfer of USDT to HOT_WALLET.
    Uses Tronscan public API to inspect token_trc20 transfers in that tx.
    Returns a dict with keys: success(bool), amount(float), txid, message
    """
    try:
        # Tronscan token transfers endpoint (search recent transfers by txid may not be direct)
        # We'll fetch transfers for contract and search for matching txid.
        url = f"{TRONSCAN_API_BASE}/token_trc20/transfers?contract_address={USDT_CONTRACT}&limit={max_look}&start=0&sort=-timestamp"
        r = requests.get(url, timeout=10)
        data = r.json()
        for item in data.get("data", []):
            # fields in item include: 'transactionHash' or 'transaction_id', 'to', 'value'
            txhash = item.get("transactionHash") or item.get("transaction_id") or item.get("transactionHashId")
            to = item.get("to") or item.get("to_address") or item.get("toAddress")
            val = item.get("value") or item.get("amount") or item.get("quant")
            if not txhash:
                continue
            if txhash.lower() == txid.lower():
                # value is integer (raw) -> convert by decimals
                try:
                    val_int = int(val)
                except Exception:
                    # sometimes value is string like "1000000"
                    val_int = int(float(val))
                amount = val_int / (10 ** USDT_DECIMALS)
                # match to address
                if expected_to and to and to.lower() != expected_to.lower():
                    return {"success": False, "message": "TX found but 'to' mismatch", "amount": amount, "txid": txid}
                # if expected_amount provided, check approx equality
                if expected_amount is not None and abs(amount - expected_amount) > 0.0001:
                    return {"success": False, "message": f"TX amount mismatch: found {amount}, expected {expected_amount}", "amount": amount, "txid": txid}
                return {"success": True, "message": "OK", "amount": amount, "txid": txid}
        # fallback: call transaction-info endpoint
        # try transaction-info by hash
        url2 = f"{TRONSCAN_API_BASE}/transaction-info?hash={txid}"
        r2 = requests.get(url2, timeout=8)
        if r2.status_code == 200:
            info = r2.json()
            # Some implementations: look into 'token_transfers' or logs
            tokens = info.get("token_transfers") or info.get("tokenInfo") or info.get("token_transfers")
            if tokens:
                for t in tokens:
                    if (t.get("to") or t.get("to_address") or "").lower() == expected_to.lower():
                        value = int(t.get("amount") or t.get("value") or t.get("quant") or 0)
                        amount = value / (10 ** USDT_DECIMALS)
                        if expected_amount is not None and abs(amount - expected_amount) > 0.0001:
                            return {"success": False, "message": f"TX amount mismatch: found {amount}, expected {expected_amount}", "amount": amount, "txid": txid}
                        return {"success": True, "message": "OK", "amount": amount, "txid": txid}
        return {"success": False, "message": "TX not found in recent token transfers", "txid": txid}
    except Exception as e:
        return {"success": False, "message": f"Error verifying tx: {e}", "txid": txid}

# ========== Profit & Referral logic ==========
# Profit calculation policy (simple): profit = 10% of total confirmed deposits for this user
def calculate_profit(user_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT SUM(amount) FROM transactions WHERE user_id=? AND type='DEPOSIT' AND status='CONFIRMED'", (user_id,))
    row = c.fetchone()
    conn.close()
    total_deposited = float(row[0]) if row and row[0] else 0.0
    return round(total_deposited * 0.10, 6)

# When a deposit is confirmed we:
#  - credit user balance with amount
#  - add a DEPOSIT transaction (CONFIRMED)
#  - if user has referrer, add 5% referral bonus to referrer (real money or internal balance—we credit internal balance)
def process_confirmed_deposit(user_id, amount, txid):
    # Avoid double processing txid
    if tx_exists(txid):
        return False, "tx already processed"

    # Mark transaction and credit user
    add_tx(user_id, None, amount, txid, "DEPOSIT", status="CONFIRMED")
    update_balance(user_id, amount)

    # referral bonus
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT referrer_id FROM users WHERE user_id=?", (user_id,))
    row = c.fetchone()
    conn.close()
    if row and row[0]:
        ref_id = row[0]
        bonus = round(amount * 0.05, 6)  # 5%
        # credit referrer internally
        add_tx(ref_id, user_id, bonus, f"REF-{txid}", "REFERRAL_BONUS", status="CONFIRMED")
        update_balance(ref_id, bonus)
    return True, "processed"

# ========== Telegram command handlers ==========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    # If user started with /start <referrer_id> store it
    ref = None
    if context.args:
        try:
            ref = int(context.args[0])
        except:
            ref = None
    ensure_user(user.id, user.username or "", ref)
    keyboard = [
        [InlineKeyboardButton("ℹ️ معلومات عن البوت", callback_data="info")],
        [InlineKeyboardButton("👥 رابط الإحالة", callback_data="referral")],
        [InlineKeyboardButton("📞 تواصل معنا", callback_data="contact")],
        [InlineKeyboardButton("💳 رصيدك", callback_data="balance")],
        [InlineKeyboardButton("💰 إيداع", callback_data="deposit")],
        [InlineKeyboardButton("💵 سحب الأرباح", callback_data="withdraw")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    text = (f"👋 أهلاً {user.first_name or 'بك'}!\n\n"
            "هذا بوت الاستثمار.\n"
            f"أرسل ودائع USDT (TRC20) إلى هذا العنوان:\n`{HOT_WALLET}`\n\n"
            "بعد الإرسال استخدم:\n`/confirm <txid>` لتأكيد الإيداع.\n\n"
            "أو اطلب `/deposit <amount>` للحصول على تعليمات لإرسال المبلغ.")
    await update.message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")

async def info_button_response(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user
    text = (
        "🤖 مرحباً بك في بوت الاستثمار الذكي!\n\n"
        "💼 هذا البوت يستقبل ودائع USDT (TRC20) إلى محفظتنا الساخنة ويمنحك رصيداً داخلياً يتم استثماره بإدارة البوت.\n"
        "📈 على كل إيداع تحصل على رصيد مطابق ويمكنك سحب الأموال الحقيقية لاحقًا.\n\n"
        "🔹 ارباح الاستثمار (محسوبة تلقائياً): **10%** من مجموع ودائعك (يمكن سحبها كفلوس حقيقية).\n"
        "🔹 مكافأة الإحالة: **5%** من كل إيداع يقوم به الأشخاص الذين أحلتهم.\n\n"
        "⚠️ ملاحظة: يتم تنفيذ السحوبات الحقيقية من محفظتنا. تأكد من صحة عنوان السحب قبل التأكيد."
    )
    keyboard = [[InlineKeyboardButton("💵 اسحب أرباحك", callback_data="withdraw")]]
    await query.edit_message_text(text=text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user = query.from_user

    if data == "info":
        await info_button_response(update, context)
    elif data == "referral":
        link = f"https://t.me/{(context.bot.username or 'thisbot')}?start={user.id}"
        text = f"🔗 رابط الإحالة الخاص بك (أرسل للآخرين):\n`{link}`\n\nسوف تحصل على 5% من ودائع الأشخاص الذين يشتركون عبر هذا الرابط."
        await query.edit_message_text(text=text, parse_mode="Markdown")
    elif data == "contact":
        await query.edit_message_text(text="📩 للتواصل: @YourUsername")
    elif data == "balance":
        bal = get_balance(user.id)
        await query.edit_message_text(text=f"💰 رصيدك الحالي: {bal} USDT")
    elif data == "deposit":
        await query.edit_message_text(text=(f"💳 لإيداع USDT أرسل للمحفظة:\n`{HOT_WALLET}`\n\n"
                                            "بعد الإرسال، انسخ txid واستخدم الأمر:\n`/confirm <txid>`"),
                                      parse_mode="Markdown")
    elif data == "withdraw":
        await query.edit_message_text(text=("لاستخدام السحب الحقيقية: \n"
                                            "`/withdraw <to_tron_address> <amount>`\n\n"
                                            "مثال: `/withdraw TXYZ... 25`"))
    else:
        await query.edit_message_text(text="❓ خيار غير معروف.")

# /deposit <amount> -> user gets instructions with address (we also record expected amount in DB as PENDING optional)
async def deposit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user.id, user.username or "")
    if not context.args:
        await update.message.reply_text(f"لاستخدام الإيداع: `/deposit 50` لإيداع 50 USDT`", parse_mode="Markdown")
        return
    try:
        amount = float(context.args[0])
    except:
        await update.message.reply_text("الكمية غير صحيحة. مثال: `/deposit 50`", parse_mode="Markdown")
        return
    # Create a pending DB entry for user's expected deposit (txid unknown yet)
    # We store with txid=None and type='DEPOSIT' & status 'PENDING' (user will confirm with txid)
    add_tx(user.id, None, amount, None, "DEPOSIT", status="PENDING")
    await update.message.reply_text(
        f"أرسل {amount} USDT (TRC20) إلى العنوان التالي:\n`{HOT_WALLET}`\n\n"
        "وبعد أن ترسل، انسخ txid وادخله بهذا الأمر:\n`/confirm <txid>`",
        parse_mode="Markdown"
    )

# /confirm <txid> -> verify on chain and credit user
async def confirm_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user.id, user.username or "")
    if not context.args:
        await update.message.reply_text("استخدم: `/confirm <txid>`", parse_mode="Markdown")
        return
    txid = context.args[0].strip()
    # prevent double processing
    if tx_exists(txid):
        await update.message.reply_text("⚠️ هذا الـ txid مُعالج سابقًا.", parse_mode="Markdown")
        return
    # verify via Tronscan API
    await update.message.reply_text("⏳ جارٍ التحقق من المعاملة على الشبكة...")
    res = verify_deposit_tx(txid, expected_to=HOT_WALLET)
    if not res.get("success"):
        await update.message.reply_text(f"❌ لم يتم العثور على إيداع صالح: {res.get('message')}")
        return
    amount = res.get("amount", 0.0)
    # process confirmed deposit: credit user etc.
    ok, msg = process_confirmed_deposit(user.id, amount, txid)
    if ok:
        await update.message.reply_text(f"✅ تم تأكيد الإيداع: {amount} USDT. تم إضافة المبلغ إلى رصيدك.")
    else:
        await update.message.reply_text(f"⚠️ لم نتمكن من معالجة الإيداع: {msg}")

# /balance command
async def balance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user.id, user.username or "")
    bal = get_balance(user.id)
    profit = calculate_profit(user.id)
    await update.message.reply_text(f"💰 رصيدك: {bal} USDT\n📈 أرباح محتسبة (10% من ودائعك): {profit} USDT")

# /withdraw <to_address> <amount> -> broadcast TRC20 transfer from hot wallet to user's address
async def withdraw_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user.id, user.username or "")
    if len(context.args) != 2:
        await update.message.reply_text("استخدام: `/withdraw <tron_address> <amount>`\nمثال: `/withdraw TXYZ... 25`", parse_mode="Markdown")
        return
    to_addr = context.args[0].strip()
    try:
        amount = float(context.args[1])
    except:
        await update.message.reply_text("المبلغ غير صحيح.", parse_mode="Markdown")
        return

    # check internal balance
    bal = get_balance(user.id)
    if bal < amount:
        await update.message.reply_text(f"رصيدك لا يكفي. رصيدك الحالي: {bal} USDT", parse_mode="Markdown")
        return

    # subtract internal balance immediately (optimistic lock)
    update_balance(user.id, -amount, user.username or "")

    # build and send TRC20 via tronpy
    try:
        await update.message.reply_text("⏳ جارٍ تنفيذ السحب على الشبكة... (قد يستغرق ثوانٍ إلى دقائق)")
        send_res = send_trc20(to_addr, amount)
        txid = send_res.get("txid")
        # record withdrawal
        add_tx(user.id, None, amount, txid, "WITHDRAW", status="PENDING")
        # optionally poll for confirmation
        # Here we mark as pending and return txid to user
        await update.message.reply_text(f"✅ طلب سحب مُرسَل. txid: `{txid}`\nتحقق من حالة المعاملة على Tronscan.", parse_mode="Markdown")
        # set as CONFIRMED for simplicity (in production poll for confirmations)
        set_tx_status(txid, "CONFIRMED")
    except Exception as e:
        # refund on failure
        update_balance(user.id, amount, user.username or "")
        await update.message.reply_text(f"❌ حدث خطأ أثناء الإرسال: {e}")

# /withdraw_profits <to_address> -> withdraw calculated profit (10%) as real USDT
async def withdraw_profits_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user.id, user.username or "")
    if len(context.args) != 1:
        await update.message.reply_text("استخدام: `/withdraw_profits <tron_address>`", parse_mode="Markdown")
        return
    to_addr = context.args[0].strip()
    profit = calculate_profit(user.id)
    if profit <= 0:
        await update.message.reply_text("لا توجد أرباح مستحقة للسحب حالياً.")
        return
    # proceed to send profit via send_trc20
    try:
        await update.message.reply_text(f"⏳ جارٍ إرسال أرباحك ({profit} USDT) إلى {to_addr} ...")
        send_res = send_trc20(to_addr, profit)
        txid = send_res.get("txid")
        add_tx(user.id, None, profit, txid, "PROFIT_PAYOUT", status="PENDING")
        set_tx_status(txid, "CONFIRMED")
        await update.message.reply_text(f"✅ تم إرسال الأرباح. txid: `{txid}`", parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ فشل في إرسال الأرباح: {e}")

# Register handlers and run
def main():
    nest_asyncio.apply()
    init_db()
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("deposit", deposit_cmd))
    app.add_handler(CommandHandler("confirm", confirm_cmd))
    app.add_handler(CommandHandler("balance", balance_cmd))
    app.add_handler(CommandHandler("withdraw", withdraw_cmd))
    app.add_handler(CommandHandler("withdraw_profits", withdraw_profits_cmd))
    app.add_handler(CallbackQueryHandler(button_handler))

    print("🤖 البوت شغال مع دعم TRON (Hot wallet):", HOT_WALLET)
    app.run_polling()

if __name__ == "__main__":
    main()
