# Price Drop Hunter 🎯

A **Telegram-controlled** price tracker that monitors Amazon and Flipkart products using **Google Sheets** as a live database. Add products by messaging your bot, and get automatic price-drop alerts every hour.

**No server needed** — runs free on GitHub Actions.

---

## 🔥 How It Works

```
You send /add <URL> <PRICE> to your Telegram bot
        ↓
GitHub Actions runs main.py every hour
        ↓
Phase 1: Processes your /add commands → auto-detects product name → adds to Google Sheet
Phase 2: Checks live prices for ALL tracked products → updates the sheet
Phase 3: Sends ONE consolidated Telegram message with alerts
```

---

## 📱 Telegram Commands

| Command | Example | What it does |
|---------|---------|--------------|
| `/add <URL> <PRICE>` | `/add https://amazon.in/dp/B0CHX1W1XY 55000` | Adds product to your watchlist |
| `/list` | `/list` | Shows all tracked products |

---

## 🚀 Setup Guide

### Step 1 — Create a Telegram Bot

1. Open Telegram → search **@BotFather** → send `/newbot`
2. Copy the **Bot Token** (e.g., `123456:ABC-DEF1234ghIkl-zyx57W2v`)
3. Send any message to your new bot, then open this URL:
   ```
   https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates
   ```
4. Find `"chat": {"id": 987654321}` — that's your **Chat ID**

### Step 2 — Set Up Google Sheets

#### 2a. Create a Service Account

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a new project (or use existing)
3. Enable **Google Sheets API** and **Google Drive API**
4. Go to **APIs & Services → Credentials → Create Credentials → Service Account**
5. Give it a name (e.g., `price-hunter-bot`)
6. Click on the service account → **Keys** → **Add Key** → **Create new key** → **JSON**
7. A `credentials.json` file will download — keep it safe!

#### 2b. Create the Google Sheet

1. Create a new Google Sheet
2. Copy the **Sheet ID** from the URL:
   ```
   https://docs.google.com/spreadsheets/d/SHEET_ID_IS_HERE/edit
   ```
3. **Share the sheet** with your service account email (found in `credentials.json` as `client_email`). Give it **Editor** access.
4. Create two tabs:
   - **Products** — with headers: `Name | URL | Target_Price | Current_Price`
   - **Settings** — put `0` in cell A1

> The script will auto-create these tabs if they don't exist, but creating them manually gives you cleaner headers.

### Step 3 — Push to GitHub & Add Secrets

```bash
git init
git add .
git commit -m "Price Drop Hunter — Telegram + Google Sheets"
git remote add origin https://github.com/<your-username>/<your-repo>.git
git push -u origin main
```

Go to **Settings → Secrets and variables → Actions** and add:

| Secret Name | Value |
|-------------|-------|
| `TELEGRAM_TOKEN` | Bot token from BotFather |
| `CHAT_ID` | Your Telegram chat ID |
| `SHEET_ID` | Google Sheet ID from the URL |
| `GOOGLE_CREDENTIALS` | **Paste the ENTIRE content** of `credentials.json` as a single-line string |

> **How to save `credentials.json` as a secret:**
> Open the file in a text editor, select all (`Ctrl+A`), copy (`Ctrl+C`), then paste directly into the GitHub Secret value field. It handles the JSON formatting automatically.

### Step 4 — Done!
I wanna do now manual check, so tell me all the things from start. 
The workflow runs **every hour** automatically. You can also click **Actions → Price Drop Hunter → Run workflow** to trigger it immediately.

---

## 🧪 Run Locally

```powershell
pip install -r requirements.txt
```

Create a `.env` file (already in `.gitignore`):

```env
TELEGRAM_TOKEN=123456:ABC-DEF1234ghIkl-zyx57W2v
CHAT_ID=987654321
SHEET_ID=your_google_sheet_id_here
GOOGLE_CREDENTIALS={"type":"service_account","project_id":"...full JSON here..."}
```

Run:

```powershell
python main.py
```

---

## 🏗️ Project Structure

```
Price Drop Hunter/
├── main.py                        # 3-phase script (commands → prices → notify)
├── requirements.txt               # Python dependencies
├── README.md
├── .env                           # Local secrets (git-ignored)
├── .gitignore
└── .github/
    └── workflows/
        └── scrape.yml             # Hourly cron + manual trigger
```

---

## ⚠️ Notes

- **Amazon/Flipkart may block requests** — the script uses a realistic browser User-Agent header, but it's not foolproof
- **CSS selectors can change** — if sites update their HTML, the selectors in `main.py` may need adjusting
- The cron schedule `0 * * * *` runs at minute 0 of every hour (UTC)
- Use the **"Run workflow"** button in GitHub Actions to force an immediate run after adding products
