# SolBot v2.0 — Solana Auto-Trading Bot

A professional Telegram-controlled Solana trading bot with full safety features,
multi-wallet support, token scanning, and detailed analytics.

---

## File Structure

```
solana-trade-bot/
├── main.py                  ← Entry point
├── config.py                ← All configuration & constants
├── requirements.txt
├── Procfile                 ← Railway process definition
├── nixpacks.toml            ← Railway build config
├── runtime.txt              ← Python version pin
├── .env.example             ← Environment variable template
├── .gitignore
├── core/
│   ├── database.py          ← SQLite persistence layer
│   ├── wallet.py            ← Wallet create/import/balance/transfer
│   ├── jupiter.py           ← Jupiter DEX quotes, swaps, simulation
│   ├── safety.py            ← Honeypot, freeze, liquidity, rug checks
│   ├── trader.py            ← Buy/sell engine, position monitor
│   ├── scanner.py           ← New token scanner (pump.fun, Raydium, Meteora)
│   ├── alert_monitor.py     ← Price alert background task
│   └── scheduler.py        ← APScheduler daily report
├── handlers/
│   ├── commands.py          ← All /command handlers
│   └── signal_handler.py   ← Channel signal message parser
└── utils/
    ├── state.py             ← In-memory bot state singleton
    ├── crypto.py            ← AES-256-GCM key encryption
    └── parser.py            ← Solana address extractor
```

---

## Quick Setup

### 1. Prerequisites

- Python 3.11+
- A Telegram Bot Token (from [@BotFather](https://t.me/BotFather))
- Your Telegram numeric user ID (from [@userinfobot](https://t.me/userinfobot))
- A Solana wallet with SOL
- (Recommended) A paid Solana RPC endpoint — [Helius](https://helius.xyz) free tier works well

---

### 2. Get a Telegram Bot Token

1. Open Telegram → search **@BotFather**
2. Send `/newbot`
3. Follow prompts — give it a name and username
4. Copy the token: `123456:ABC-DEF...`

---

### 3. Get your Telegram User ID

1. Open Telegram → search **@userinfobot**
2. Send `/start`
3. It will reply with your numeric ID, e.g. `123456789`

---

### 4. Generate an Encryption Key

Run this once locally or on any Python environment:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

Copy the output (64 hex characters). This encrypts your stored wallet keys.

---

### 5. Deploy to Railway

#### Option A — GitHub → Railway (Recommended)

1. **Create a GitHub repository** (public or private)
2. Upload all files maintaining the exact folder structure shown above
3. Go to [railway.app](https://railway.app) → **New Project** → **Deploy from GitHub repo**
4. Select your repository
5. Railway will auto-detect Python and build using `nixpacks.toml`

#### Option B — Railway CLI

```bash
npm install -g @railway/cli
railway login
railway init
railway up
```

---

### 6. Set Environment Variables on Railway

In your Railway project → **Variables** tab, add:

| Variable | Value | Required |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Your bot token from BotFather | ✅ |
| `TELEGRAM_OWNER_ID` | Your numeric Telegram user ID | ✅ |
| `ENCRYPTION_KEY` | 64-char hex string (step 4) | ✅ |
| `SOLANA_RPC_URL` | Your RPC endpoint URL | ✅ |
| `SOLANA_WS_URL` | Your WebSocket RPC URL | ✅ |
| `HELIUS_API_KEY` | Helius API key (for token age checks) | Optional |
| `BIRDEYE_API_KEY` | BirdEye API key (enhanced data) | Optional |

**Example for free Solana RPC:**
```
SOLANA_RPC_URL=https://api.mainnet-beta.solana.com
SOLANA_WS_URL=wss://api.mainnet-beta.solana.com
```

**Example for Helius (recommended):**
```
SOLANA_RPC_URL=https://mainnet.helius-rpc.com/?api-key=YOUR_HELIUS_KEY
SOLANA_WS_URL=wss://mainnet.helius-rpc.com/?api-key=YOUR_HELIUS_KEY
HELIUS_API_KEY=YOUR_HELIUS_KEY
```

---

### 7. Configure Railway Service Settings

In Railway → your service → **Settings**:

- **Start command**: `python main.py` (auto-detected from Procfile)
- **Health check**: Not required (long-polling bot, not a web server)
- **Restart policy**: Set to **On Failure** with 3 retries

---

### 8. Verify Deployment

1. Check **Deployments** tab — build should show green
2. Open **Logs** tab — you should see:
   ```
   Starting SolBot v2.0...
   Database initialised.
   Bot command menu registered.
   Position monitor started.
   Token scanner started.
   Alert monitor started.
   SolBot ready.
   ```
3. Open Telegram — bot should send you a startup message

---

### 9. Add a Wallet

In Telegram with your bot:

```
/createwallet main
```

The bot will create a new Solana wallet and show you the private key ONCE — save it securely.

Or import an existing wallet:
```
/importwallet <your_base58_private_key> main
```

Fund the wallet by sending SOL to the address shown.

---

### 10. First Trade Setup

```
# Start the bot
/run

# Check status
/status

# Set trading parameters
/setprofit 2.0        ← sell at 2x (100% gain)
/setposition 5        ← use 5% of balance per trade
/setslippage 500      ← 5% slippage tolerance
/setmaxloss 2.0       ← stop if down 2 SOL today
/setdailytrades 10    ← max 10 trades per day

# Add a signal channel (get signals from)
/addchannel @yourchannel

# Enable paper trading first to test without real money
/paper

# Test a manual trade
/trade So11111111111111111111111111111111111111112
```

---

### 11. Enable Token Scanner

```
/scanner
```

This enables auto-discovery of new tokens from pump.fun, Raydium, and Meteora.
The scanner waits at least 5 minutes after token creation before buying (configurable).

---

## Important Safety Notes

1. **Start with paper trading** (`/paper`) to verify everything works before using real SOL
2. **Set a daily loss cap** (`/setmaxloss`) — never risk more than you can afford to lose
3. **Use a dedicated trading wallet** — never put your main wallet private key in the bot
4. **Degen trading is high risk** — new tokens can go to zero instantly
5. **Keep your ENCRYPTION_KEY safe** — losing it means losing access to stored wallets
6. **The public Solana RPC has rate limits** — use a paid RPC for reliable operation

---

## Monitoring & Logs

- `/logs` — Last 20 audit log entries
- `/status` — Full dashboard
- `/positions` — Live P&L on open positions
- Railway Logs tab — Full application output

---

## Troubleshooting

| Problem | Solution |
|---|---|
| Bot not responding | Check Railway logs for errors; verify `TELEGRAM_BOT_TOKEN` is correct |
| "Unauthorized" on commands | Verify `TELEGRAM_OWNER_ID` is your exact numeric user ID |
| Trades failing | Check wallet has SOL + 0.01 for fees; check RPC is responsive |
| "No Jupiter route" | Token may have no liquidity; check on dexscreener.com |
| Build fails on Railway | Ensure `requirements.txt` is in root directory |
| SQLite errors | Check Railway has persistent volume or use Railway's volume mount |

---

## Persistent Storage on Railway

By default, Railway's filesystem resets on redeploy. To persist the SQLite database:

1. In Railway → your service → **Volumes**
2. Add a volume mounted at `/app/data`
3. Set env var: `DATABASE_PATH=/app/data/bot.db`

---

## Environment Variables Reference

```env
TELEGRAM_BOT_TOKEN=      # Required: from BotFather
TELEGRAM_OWNER_ID=       # Required: your numeric Telegram ID
ENCRYPTION_KEY=          # Required: 64-char hex (python -c "import secrets; print(secrets.token_hex(32))")
SOLANA_RPC_URL=          # Required: Solana RPC HTTP URL
SOLANA_WS_URL=           # Required: Solana RPC WebSocket URL
JUPITER_API_URL=         # Optional: defaults to https://quote-api.jup.ag/v6
HELIUS_API_KEY=          # Optional: for token age & metadata
BIRDEYE_API_KEY=         # Optional: enhanced price feeds
DATABASE_PATH=           # Optional: defaults to data/bot.db
```
