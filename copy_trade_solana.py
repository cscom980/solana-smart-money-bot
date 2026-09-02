import asyncio
import aiohttp
import logging
import json
import os
import csv
from datetime import datetime

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

SOLANA_RPC_URL = os.getenv("SOLANA_RPC_URL")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

INITIAL_PAPER_BALANCE = 500.0   # Start Equity ($500 USD)
TRADE_SIZE_USD = 50.0           # Equity per trade ($50 USD)
MAX_OPEN_POSITIONS = 2          # Max open position
TAKE_PROFIT_PCT = 100.0         # Hard TP: Auto-sell if profit +100%
STOP_LOSS_PCT = 25.0            # Hard SL: Auto-sell if loss -25%
MAX_PRICE_DEVIATION_PCT = 15.0  # Anti-FOMO: Maksimal toleransi kenaikan harga dari entry target (15%)

POSITIONS_FILE = "positions.json"
REVIEW_CSV_FILE = "review.csv"

IGNORED_MINTS = {
    "So11111111111111111111111111111111111111112",  # WSOL
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
}

TARGET_WALLETS = {
    "2UwD3WCCndHxCRekDQgvZbM1H2M7qVuxLGKaWXqg8cVK": {"name": "GMGN Smart Money #1 (61% WR)", "min_buy_usd": 100},
    "5t4Tz7qewAHSpDC1YADAmogtiE3Cwud4juNjjVXaDs1p": {"name": "GMGN Smart Money #2 (59% WR)", "min_buy_usd": 100},
    "AE4MPGvpMeCA7MwUakAxAQZTzcijPAXcFsoAQmtLrL4V": {"name": "KOL fih (85% WinRate)", "min_buy_usd": 100}
}

portfolio = {
    "cash_usd": INITIAL_PAPER_BALANCE,
    "positions": {},  # mint: {symbol, entry_price, tokens, amount_usd, target_wallet}
    "pnl_history": []
}

# --- HELPER: PERSISTENSI STATE JSON ---
def load_positions() -> dict:
    if os.path.exists(POSITIONS_FILE):
        try:
            with open(POSITIONS_FILE, "r") as f:
                pos = json.load(f)
                logging.info(f"📂 [SYSTEM] Memuat {len(pos)} posisi aktif dari {POSITIONS_FILE}")
                return pos
        except Exception as e:
            logging.error(f"Error loading {POSITIONS_FILE}: {e}")
    return {}

def save_positions(positions: dict):
    try:
        with open(POSITIONS_FILE, "w") as f:
            json.dump(positions, f, indent=4)
    except Exception as e:
        logging.error(f"Error saving {POSITIONS_FILE}: {e}")

# --- HELPER: LOGGING PERFORMANCE TO CSV ---
def log_to_review_csv(symbol: str, target_wallet: str, entry_price: float, exit_price: float, pnl_usd: float, pnl_pct: float, exit_reason: str):
    file_exists = os.path.exists(REVIEW_CSV_FILE)
    try:
        with open(REVIEW_CSV_FILE, mode="a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            # Jika file baru dibuat, tambahkan Header
            if not file_exists:
                writer.writerow([
                    "Timestamp", 
                    "Nama Koin", 
                    "Target Wallet", 
                    "Harga Open ($)", 
                    "Harga Close ($)", 
                    "PnL ($)", 
                    "PnL (%)", 
                    "Alasan Exit"
                ])
            # Record the transaction line
            writer.writerow([
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                symbol,
                target_wallet,
                f"{entry_price:.8f}",
                f"{exit_price:.8f}",
                f"{pnl_usd:+.2f}",
                f"{pnl_pct:+.2f}%",
                exit_reason
            ])
            logging.info(f"📊 [CSV LOGGED] transaction {symbol} recorded to {REVIEW_CSV_FILE}")
    except Exception as e:
        logging.error(f"Error writing to {REVIEW_CSV_FILE}: {e}")

# --- HELPER: TELEGRAM NOTIFIER ---
async def send_telegram(session: aiohttp.ClientSession, message: str):
    if not TELEGRAM_BOT_TOKEN or TELEGRAM_BOT_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN":
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML"
    }
    try:
        async with session.post(url, json=payload, timeout=5) as resp:
            pass
    except Exception as e:
        logging.error(f"Error Telegram Notification: {e}")

# --- 1. LOOSE SECURITY FILTER ---
async def check_rugcheck_solana(session: aiohttp.ClientSession, mint_address: str) -> bool:
    url = f"https://api.rugcheck.xyz/v1/tokens/{mint_address}/report/summary"
    try:
        async with session.get(url, timeout=5) as resp:
            if resp.status == 200:
                data = await resp.json()
                score = data.get("score", 0)
                risks = data.get("risks", [])
                
                for r in risks:
                    risk_name = r.get("name", "").lower()
                    if "freeze" in risk_name or "mint" in risk_name:
                        logging.warning(f"🛡️ [HARD REJECT] {mint_address[:10]}... Detection Freeze/Mint Authority!")
                        return False
                
                if score > 5000:
                    logging.warning(f"⚠️ [SCORE REJECT] {mint_address[:10]}... Extreme Risk Score ({score})")
                    return False
                    
                return True
    except Exception:
        pass
    return False

# --- 2. DEXSCREENER PRICE FEED ---
async def get_solana_token_price(session: aiohttp.ClientSession, mint_address: str):
    url = f"https://api.dexscreener.com/latest/dex/tokens/{mint_address}"
    try:
        async with session.get(url, timeout=5) as resp:
            if resp.status == 200:
                data = await resp.json()
                pairs = data.get("pairs") or []
                for pair in pairs:
                    if pair.get("chainId") == "solana":
                        price = float(pair.get("priceUsd", 0))
                        symbol = pair.get("baseToken", {}).get("symbol", "UNKNOWN")
                        return price, symbol
    except Exception:
        pass
    return 0.0, "UNKNOWN"

# --- 3. EVENT PROCESSOR ---
async def process_solana_event(session: aiohttp.ClientSession, wallet_address: str, sig: str):
    payload = {
        "jsonrpc": "2.0", "id": 1,
        "method": "getTransaction",
        "params": [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}]
    }
    try:
        async with session.post(SOLANA_RPC_URL, json=payload, timeout=5) as resp:
            if resp.status == 200:
                res = await resp.json()
                tx_info = res.get("result") or {}
                meta = tx_info.get("meta") or {}
                
                pre_balances = meta.get("preTokenBalances") or []
                post_balances = meta.get("postTokenBalances") or []

                for post in post_balances:
                    owner = post.get("owner", "").lower()
                    if owner == wallet_address.lower():
                        mint_address = post.get("mint")
                        if not mint_address or mint_address in IGNORED_MINTS:
                            continue

                        post_amount = float(post.get("uiTokenAmount", {}).get("uiAmount") or 0)
                        
                        pre_amount = 0.0
                        for pre in pre_balances:
                            if pre.get("mint") == mint_address and pre.get("owner", "").lower() == wallet_address.lower():
                                pre_amount = float(pre.get("uiTokenAmount", {}).get("uiAmount") or 0)
                                break

                        # SCENARIO A: Target Wallet SELLS Tokens (Copy-Sell Trigger)
                        if post_amount < pre_amount and mint_address in portfolio["positions"]:
                            pos = portfolio["positions"][mint_address]
                            if pos["target_wallet"] == wallet_address:
                                current_price, symbol = await get_solana_token_price(session, mint_address)
                                price_to_use = current_price if current_price > 0 else pos["entry_price"]
                                
                                sell_value_usd = pos["tokens"] * price_to_use
                                pnl_usd = sell_value_usd - pos["amount_usd"]
                                pnl_pct = (pnl_usd / pos["amount_usd"]) * 100
                                
                                portfolio["cash_usd"] += sell_value_usd
                                portfolio["pnl_history"].append(pnl_usd)
                                
                                target_name = TARGET_WALLETS.get(wallet_address, {}).get("name", wallet_address[:6])
                                
                                # Log to CSV & Save JSON
                                log_to_review_csv(symbol, target_name, pos["entry_price"], price_to_use, pnl_usd, pnl_pct, "Copy-Sell")
                                del portfolio["positions"][mint_address]
                                save_positions(portfolio["positions"])
                                
                                msg = (
                                    f"🔴 <b>[COPY-SELL EXECUTED]</b>\n"
                                    f"<b>Token:</b> ${symbol}\n"
                                    f"<b>Target:</b> {target_name}\n"
                                    f"<b>Realized PnL:</b> ${pnl_usd:+.2f} ({pnl_pct:+.2f}%)\n"
                                    f"<b>Cash Balance:</b> ${portfolio['cash_usd']:.2f} USD"
                                )
                                logging.info(f"🔴 [COPY-SELL EXECUTED] Target Wallet ({target_name}) Jual {symbol}! PnL: ${pnl_usd:+.2f}")
                                await send_telegram(session, msg)
                                return

                        # SCENARIO B: Target Wallet Buys Token (Copy-Buy Trigger)
                        elif post_amount > pre_amount and mint_address not in portfolio["positions"]:
                            if len(portfolio["positions"]) >= MAX_OPEN_POSITIONS:
                                return

                            is_safe = await check_rugcheck_solana(session, mint_address)
                            if not is_safe:
                                return

                            current_price, symbol = await get_solana_token_price(session, mint_address)
                            if current_price <= 0:
                                return

                            target_entry_price = current_price * 0.90 
                            price_deviation = ((current_price - target_entry_price) / target_entry_price) * 100

                            if price_deviation > MAX_PRICE_DEVIATION_PCT:
                                logging.warning(f"⚠️ [ANTI-FOMO REJECT] Harga {symbol} sudah melesat +{price_deviation:.1f}%!")
                                return

                            if portfolio["cash_usd"] >= TRADE_SIZE_USD:
                                target_name = TARGET_WALLETS.get(wallet_address, {}).get("name", wallet_address[:6])
                                tokens_bought = TRADE_SIZE_USD / current_price
                                portfolio["cash_usd"] -= TRADE_SIZE_USD
                                
                                portfolio["positions"][mint_address] = {
                                    "symbol": symbol,
                                    "entry_price": current_price,
                                    "tokens": tokens_bought,
                                    "amount_usd": TRADE_SIZE_USD,
                                    "target_wallet": wallet_address
                                }
                                save_positions(portfolio["positions"])
                                
                                msg = (
                                    f"🟢 <b>[PAPER BUY SUCCESS]</b>\n"
                                    f"<b>Token:</b> ${symbol}\n"
                                    f"<b>Price:</b> ${current_price:.6f}\n"
                                    f"<b>Amount:</b> ${TRADE_SIZE_USD} USD\n"
                                    f"<b>Following:</b> {target_name}\n"
                                    f"<b>Open Slots:</b> {len(portfolio['positions'])}/{MAX_OPEN_POSITIONS}"
                                )
                                logging.info(f"🟢 [PAPER BUY SUCCESS] Buy ${TRADE_SIZE_USD} {symbol} @ ${current_price:.6f}")
                                await send_telegram(session, msg)

    except Exception:
        pass

# --- 4. HARD PRICE MONITOR ENGINE ---
async def position_monitor_loop(session: aiohttp.ClientSession):
    logging.info("🛡️ [POSITION MONITOR] Monitoring Engine Hard TP/SL Active...")
    
    while True:
        open_mints = list(portfolio["positions"].keys())
        
        for mint_address in open_mints:
            pos = portfolio["positions"].get(mint_address)
            if not pos:
                continue

            current_price, symbol = await get_solana_token_price(session, mint_address)
            if current_price <= 0:
                continue

            entry_price = pos["entry_price"]
            target_name = TARGET_WALLETS.get(pos["target_wallet"], {}).get("name", pos["target_wallet"][:6])
            pnl_pct = ((current_price - entry_price) / entry_price) * 100
            
            # Hard Take Profit (+100%)
            if pnl_pct >= TAKE_PROFIT_PCT:
                sell_val = pos["tokens"] * current_price
                pnl_usd = sell_val - pos["amount_usd"]
                portfolio["cash_usd"] += sell_val
                portfolio["pnl_history"].append(pnl_usd)
                
                # Log to CSV & Save JSON
                log_to_review_csv(symbol, target_name, entry_price, current_price, pnl_usd, pnl_pct, "Hard TP (+100%)")
                del portfolio["positions"][mint_address]
                save_positions(portfolio["positions"])
                
                msg = (
                    f"🎯 <b>[HARD TAKE PROFIT +100%]</b>\n"
                    f"<b>Token:</b> ${symbol}\n"
                    f"<b>Exit Price:</b> ${current_price:.6f}\n"
                    f"<b>PnL:</b> +${pnl_usd:.2f} (+{pnl_pct:.2f}%)\n"
                    f"<b>Cash Balance:</b> ${portfolio['cash_usd']:.2f} USD"
                )
                logging.info(f"🎯 [HARD TAKE PROFIT +100%] Auto-Sell {symbol} @ ${current_price:.6f}")
                await send_telegram(session, msg)

            # Hard Stop Loss (-25%)
            elif pnl_pct <= -STOP_LOSS_PCT:
                sell_val = pos["tokens"] * current_price
                pnl_usd = sell_val - pos["amount_usd"]
                portfolio["cash_usd"] += sell_val
                portfolio["pnl_history"].append(pnl_usd)
                
                # Log to CSV & Save JSON
                log_to_review_csv(symbol, target_name, entry_price, current_price, pnl_usd, pnl_pct, "Hard SL (-25%)")
                del portfolio["positions"][mint_address]
                save_positions(portfolio["positions"])
                
                msg = (
                    f"🛑 <b>[HARD STOP LOSS -25%]</b>\n"
                    f"<b>Token:</b> ${symbol}\n"
                    f"<b>Exit Price:</b> ${current_price:.6f}\n"
                    f"<b>PnL:</b> -${abs(pnl_usd):.2f} ({pnl_pct:.2f}%)\n"
                    f"<b>Cash Balance:</b> ${portfolio['cash_usd']:.2f} USD"
                )
                logging.warning(f"🛑 [HARD STOP LOSS -25%] Auto-Sell {symbol} @ ${current_price:.6f}")
                await send_telegram(session, msg)

        await asyncio.sleep(4)

# --- 5. REAL-TIME LISTENER LOOP ---
async def solana_listener_loop(session: aiohttp.ClientSession):
    logging.info("👀 [SOLANA LISTENER] Memulai pemantauan transaksi real-time Solana...")
    processed_sigs = set()

    while True:
        if len(portfolio["positions"]) >= MAX_OPEN_POSITIONS:
            await asyncio.sleep(3)
            
        active_wallets = list(TARGET_WALLETS.keys())
        for wallet in active_wallets:
            payload = {
                "jsonrpc": "2.0", "id": 1,
                "method": "getSignaturesForAddress",
                "params": [wallet, {"limit": 2}]
            }
            try:
                async with session.post(SOLANA_RPC_URL, json=payload, timeout=5) as resp:
                    if resp.status == 200:
                        res = await resp.json()
                        sigs = res.get("result") or []
                        for sig_data in sigs:
                            sig = sig_data.get("signature")
                            if sig and sig not in processed_sigs:
                                processed_sigs.add(sig)
                                asyncio.create_task(process_solana_event(session, wallet, sig))
            except Exception:
                pass

        await asyncio.sleep(3)

# --- MAIN EXECUTION ---
async def main():
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    async with aiohttp.ClientSession(headers=headers) as session:
        portfolio["positions"] = load_positions()
        
        logging.info(f"✨ Total Active Target Solana Wallets: {len(TARGET_WALLETS)}")
        for addr, info in TARGET_WALLETS.items():
            logging.info(f"   └─ {info['name']} ({addr[:10]}...)")
            
        if portfolio["positions"]:
            logging.info(f"📌 [RESTORED] Tracking {len(portfolio['positions'])} active positions from the previous restart:")
            for mint, pos in portfolio["positions"].items():
                logging.info(f"   └─ ${pos['symbol']} @ ${pos['entry_price']:.6f}")

        await send_telegram(session, "🚀 <b>[SYSTEM START]</b> Solana Copy-Trading Bot Active on VPS!")

        await asyncio.gather(
            solana_listener_loop(session),
            position_monitor_loop(session)
        )

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Solana bot stopped by user.")