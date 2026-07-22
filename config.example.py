"""Copy this file to config.py and fill in your values.
config.py is gitignored - your secrets never reach GitHub."""

# --- Databento (required for live data) ------------------------------
DATABENTO_API_KEY = "db-PASTE-YOUR-KEY-HERE"

# --- MT5 account (OPTIONAL) -------------------------------------------
# Leave these as None to simply attach to the account already logged in
# inside your running MT5 terminal (the usual way). Fill them in only if
# you want the script to log the terminal into a specific account itself,
# e.g. for unattended restarts.
MT5_LOGIN = None          # e.g. 12345678  (account number)
MT5_PASSWORD = None       # e.g. "your-password"
MT5_SERVER = None         # e.g. "MetaQuotes-Demo" or your broker's server name
