# 🦅 Solana Smart Money Tracker & Copy-Trading Bot

An asynchronous, event-driven Python bot designed to track target Solana wallets in real-time using the Helius RPC API. The system monitors "Smart Money" transactions and automatically executes paper trades with built-in risk management and rug-pull checks.

## 🌟 Key Features

* **Real-Time On-Chain Tracking:** Monitors specific wallet signatures asynchronously (e.g., GMGN Smart Money wallets with high win rates) using Helius RPC.
* **Automated Risk Management:** 
  * Hard Take Profit (+100%) and Stop Loss (-25%) engine.
  * Anti-FOMO slippage rejector (Max 15% price deviation from target's entry).
* **Security Filter:** Integrates with `api.rugcheck.xyz` to automatically reject tokens with "Freeze/Mint Authority" or extreme risk scores before buying.
* **Live Price Feed:** Uses Dexscreener API to fetch accurate, real-time Solana token pair pricing.
* **State Persistence & Analytics:** Saves active positions to `positions.json` to survive system restarts, and logs full trade history (entry, exit, PnL, exit reasoning) into `review.csv`.
* **Telegram Integration:** Sends real-time HTML-formatted execution alerts directly to your mobile device.

## 🛠️ Tech Stack
* **Language:** Python 3.11+
* **Core Libraries:** `aiohttp` (Async I/O), `asyncio`, `json`, `csv`
* **External APIs:** Helius RPC, Dexscreener API, Rugcheck API, Telegram Bot API

## 🚀 Installation & Usage

1. **Clone the repository:**
   ```bash
   git clone [https://github.com/cscom980/solana-smart-money-bot.git](https://github.com/cscom980/solana-smart-money-bot.git)
   cd solana-smart-money-bot