"""
main.py — FastAPI web dashboard for multi-tenant trading agent.

Provides:
- User registration & login
- Broker credential management
- Telegram bot linking
- User dashboard with P&L summary
- Signal mode settings

Run: uvicorn web.main:app --host 0.0.0.0 --port 8000
"""

import os
import uuid
from datetime import datetime
from fastapi import FastAPI, HTTPException, Form, Request, Depends
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
import psycopg2
from pydantic import BaseModel

from config.logger import get_logger
from data.store import (
    get_conn, get_user, get_user_by_chat_id, get_all_active_users,
    get_user_pnl_summary, get_user_signals, get_user_trades
)
from web.secrets import encrypt_json, hash_password, verify_password
from web.dependencies import get_db, verify_broker_credentials

log = get_logger("web")

# ============================================================================
# FastAPI Setup
# ============================================================================

app = FastAPI(
    title="OptionBuddy Trading Agent",
    description="Multi-tenant autonomous trading agent dashboard",
    version="2.0.0"
)

# ============================================================================
# Pydantic Models
# ============================================================================

class UserRegister(BaseModel):
    username: str
    password: str
    email: str | None = None


class BrokerConnect(BaseModel):
    broker_type: str
    api_key: str
    api_secret: str


# ============================================================================
# HTML Templates
# ============================================================================

def get_html(title: str, content: str) -> str:
    """Wrap content in basic HTML."""
    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>{title}</title>
        <style>
            body {{ font-family: Arial, sans-serif; max-width: 600px; margin: 50px auto; }}
            input, select {{ padding: 8px; margin: 5px 0; width: 100%; box-sizing: border-box; }}
            button {{ padding: 10px 20px; background: #007bff; color: white; border: none; cursor: pointer; }}
            button:hover {{ background: #0056b3; }}
            .success {{ color: green; }}
            .error {{ color: red; }}
            .info {{ color: blue; }}
            h1 {{ color: #333; }}
            hr {{ margin: 20px 0; }}
        </style>
    </head>
    <body>
        {content}
    </body>
    </html>
    """


# ============================================================================
# Health Check
# ============================================================================

@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "ok", "service": "OptionBuddy Web Dashboard"}


# ============================================================================
# Registration Routes
# ============================================================================

@app.get("/register", response_class=HTMLResponse)
async def register_page():
    """Show registration form."""
    content = """
    <h1>Register for OptionBuddy</h1>
    <form action="/register" method="post">
        <label>Username:</label>
        <input type="text" name="username" placeholder="Choose a username" required>

        <label>Password:</label>
        <input type="password" name="password" placeholder="Enter password" required>

        <label>Email (optional):</label>
        <input type="email" name="email" placeholder="your@email.com">

        <button type="submit">Sign Up</button>
    </form>
    <p>Already registered? <a href="/login">Login here</a></p>
    """
    return get_html("Register - OptionBuddy", content)


@app.post("/register", response_class=HTMLResponse)
async def register_user(
    username: str = Form(...),
    password: str = Form(...),
    email: str = Form(None),
    db=Depends(get_db)
):
    """Create new user account."""
    cursor = db.cursor()

    try:
        # Check if username exists
        cursor.execute("SELECT user_id FROM users WHERE username = %s", (username,))
        if cursor.fetchone():
            raise HTTPException(400, "Username already taken")

        # Create user
        user_id = str(uuid.uuid4())
        password_hash = hash_password(password)

        cursor.execute("""
            INSERT INTO users (user_id, username, password_hash, signal_mode, is_paused)
            VALUES (%s, %s, %s, %s, %s)
        """, (user_id, username, password_hash, "personal", False))

        db.commit()
        log.info(f"User registered: {username}")

        # Redirect to broker selection
        content = f"""
        <h1>Registration Successful!</h1>
        <p class="success">✓ Account created for <b>{username}</b></p>
        <p>Next step: Connect your broker account</p>
        <a href="/connect-broker?user_id={user_id}"><button>Connect Broker</button></a>
        """
        return get_html("Registration Success", content)

    except psycopg2.Error as e:
        log.error(f"Registration error: {e}")
        raise HTTPException(400, f"Registration failed: {str(e)}")
    finally:
        cursor.close()


@app.get("/login", response_class=HTMLResponse)
async def login_page():
    """Show login form."""
    content = """
    <h1>Login to OptionBuddy</h1>
    <form action="/login" method="post">
        <label>Username:</label>
        <input type="text" name="username" placeholder="Enter username" required>

        <label>Password:</label>
        <input type="password" name="password" placeholder="Enter password" required>

        <button type="submit">Login</button>
    </form>
    <p>Don't have an account? <a href="/register">Register here</a></p>
    """
    return get_html("Login - OptionBuddy", content)


@app.post("/login")
async def login_user(
    username: str = Form(...),
    password: str = Form(...),
    db=Depends(get_db)
):
    """Authenticate user."""
    cursor = db.cursor()

    try:
        cursor.execute(
            "SELECT user_id, password_hash FROM users WHERE username = %s",
            (username,)
        )
        result = cursor.fetchone()

        if not result:
            raise HTTPException(401, "Invalid username or password")

        user_id, password_hash = result

        if not verify_password(password, password_hash):
            raise HTTPException(401, "Invalid username or password")

        log.info(f"User logged in: {username}")

        # Redirect to dashboard
        return RedirectResponse(url=f"/dashboard/{user_id}", status_code=303)

    finally:
        cursor.close()


# ============================================================================
# Broker Connection Routes
# ============================================================================

@app.get("/connect-broker", response_class=HTMLResponse)
async def broker_selection_page(user_id: str):
    """Show broker selection form."""
    content = f"""
    <h1>Connect Your Broker</h1>
    <p>Select your broker and enter API credentials:</p>
    <form action="/connect-broker" method="post">
        <input type="hidden" name="user_id" value="{user_id}">

        <label>Broker:</label>
        <select name="broker_type" required>
            <option value="">-- Select Broker --</option>
            <option value="indstocks">INDstocks</option>
            <option value="zerodha">Zerodha</option>
            <option value="grow">Grow</option>
        </select>

        <label>API Key:</label>
        <input type="text" name="api_key" placeholder="API Key" required>

        <label>API Secret:</label>
        <input type="password" name="api_secret" placeholder="API Secret" required>

        <button type="submit">Connect Broker</button>
    </form>
    """
    return get_html("Connect Broker - OptionBuddy", content)


@app.post("/connect-broker")
async def connect_broker(
    user_id: str = Form(...),
    broker_type: str = Form(...),
    api_key: str = Form(...),
    api_secret: str = Form(...),
    db=Depends(get_db)
):
    """Validate and save broker credentials."""
    try:
        # Verify credentials
        if not verify_broker_credentials(broker_type, {'api_key': api_key, 'api_secret': api_secret}):
            raise HTTPException(400, "Invalid broker credentials")

        # Encrypt and save
        encrypted_creds = encrypt_json({'api_key': api_key, 'api_secret': api_secret})

        cursor = db.cursor()
        cursor.execute("""
            UPDATE users
            SET broker_type = %s, broker_credentials = %s, updated_at = NOW()
            WHERE user_id = %s
        """, (broker_type, encrypted_creds, user_id))
        db.commit()
        cursor.close()

        log.info(f"Broker connected for user {user_id}: {broker_type}")

        # Next step: Link Telegram
        content = f"""
        <h1>Broker Connected!</h1>
        <p class="success">✓ {broker_type.title()} connected successfully</p>
        <p>Next step: Link your Telegram bot</p>
        <a href="/link-telegram?user_id={user_id}"><button>Link Telegram</button></a>
        """
        return get_html("Broker Connected", content)

    except HTTPException:
        raise
    except Exception as e:
        log.error(f"Broker connection error: {e}")
        raise HTTPException(400, f"Failed to connect broker: {str(e)}")


# ============================================================================
# Telegram Linking Routes
# ============================================================================

@app.get("/link-telegram", response_class=HTMLResponse)
async def link_telegram_page(user_id: str):
    """Show Telegram linking instructions."""
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN")
    bot_username = os.getenv("TELEGRAM_BOT_USERNAME", "OptionBuddyBot")

    content = f"""
    <h1>Link Telegram Bot</h1>
    <p>Click the button below to start chatting with the OptionBuddy trading bot:</p>
    <a href="https://t.me/{bot_username}?start={user_id}" target="_blank">
        <button style="font-size: 16px;">Start Bot on Telegram</button>
    </a>
    <hr>
    <p><b>After clicking above:</b></p>
    <ol>
        <li>Click "Start" in Telegram</li>
        <li>Send this command to the bot: <code>/verify {user_id}</code></li>
        <li>Wait for confirmation</li>
    </ol>
    <p>Once verified, your dashboard will be ready!</p>
    <a href="/dashboard/{user_id}"><button>Go to Dashboard</button></a>
    """
    return get_html("Link Telegram - OptionBuddy", content)


@app.post("/verify-telegram")
async def verify_telegram(
    user_id: str,
    chat_id: int,
    db=Depends(get_db)
):
    """Verify and link Telegram chat_id to user."""
    cursor = db.cursor()

    try:
        cursor.execute("""
            UPDATE users
            SET telegram_chat_id = %s, updated_at = NOW()
            WHERE user_id = %s
        """, (chat_id, user_id))
        db.commit()

        log.info(f"Telegram linked for user {user_id}: chat_id {chat_id}")
        return {"status": "success", "message": "Telegram linked successfully"}

    except Exception as e:
        log.error(f"Telegram linking error: {e}")
        raise HTTPException(400, f"Failed to link Telegram: {str(e)}")
    finally:
        cursor.close()


# ============================================================================
# Dashboard Routes
# ============================================================================

@app.get("/dashboard/{user_id}", response_class=HTMLResponse)
async def dashboard(user_id: str, db=Depends(get_db)):
    """User dashboard with P&L, positions, and settings."""
    try:
        user = get_user(user_id)
        if not user:
            raise HTTPException(404, "User not found")

        # Get P&L summary
        pnl_summary = get_user_pnl_summary(user_id)
        unrealised = pnl_summary.get("unrealised", 0) if pnl_summary else 0
        realised = pnl_summary.get("realised", 0) if pnl_summary else 0

        # Get recent signals
        signals = get_user_signals(user_id, limit=5)
        signals_html = "".join([
            f"<li>{s['ts']}: {s['signal']} ({s['confidence']}%)</li>"
            for s in signals
        ])

        content = f"""
        <h1>Dashboard: {user.get('username', 'N/A')}</h1>
        <hr>
        <h2>Portfolio Summary</h2>
        <p><b>Unrealised P&L:</b> ₹{unrealised:.2f}</p>
        <p><b>Realised P&L:</b> ₹{realised:.2f}</p>
        <p><b>Broker:</b> {user.get('broker_type', 'N/A').title()}</p>
        <p><b>Signal Mode:</b> {user.get('signal_mode', 'N/A')}</p>
        <p><b>Status:</b> {'🔴 Paused' if user.get('is_paused') else '🟢 Active'}</p>

        <hr>
        <h2>Recent Signals</h2>
        <ul>
            {signals_html if signals_html else '<li>No signals yet</li>'}
        </ul>

        <hr>
        <h2>Settings</h2>
        <form action="/update-settings" method="post">
            <input type="hidden" name="user_id" value="{user_id}">

            <label>Signal Mode:</label>
            <select name="signal_mode">
                <option value="personal" {'selected' if user.get('signal_mode') == 'personal' else ''}>Personal Portfolio Only</option>
                <option value="shared" {'selected' if user.get('signal_mode') == 'shared' else ''}>Shared Pool Only</option>
                <option value="both" {'selected' if user.get('signal_mode') == 'both' else ''}>Both</option>
            </select>

            <label>Daily Loss Limit (₹):</label>
            <input type="number" name="daily_loss_limit" value="{user.get('daily_loss_limit', 50000)}" required>

            <button type="submit">Save Settings</button>
        </form>

        <hr>
        <p><a href="/logout">Logout</a></p>
        """

        return get_html(f"Dashboard - {user.get('username', 'User')}", content)

    except HTTPException:
        raise
    except Exception as e:
        log.error(f"Dashboard error: {e}")
        raise HTTPException(500, f"Failed to load dashboard: {str(e)}")


@app.post("/update-settings")
async def update_settings(
    user_id: str = Form(...),
    signal_mode: str = Form(...),
    daily_loss_limit: float = Form(...),
    db=Depends(get_db)
):
    """Update user settings."""
    cursor = db.cursor()

    try:
        cursor.execute("""
            UPDATE users
            SET signal_mode = %s, daily_loss_limit = %s, updated_at = NOW()
            WHERE user_id = %s
        """, (signal_mode, daily_loss_limit, user_id))
        db.commit()

        log.info(f"Settings updated for user {user_id}")
        return RedirectResponse(url=f"/dashboard/{user_id}", status_code=303)

    finally:
        cursor.close()


@app.get("/logout")
async def logout():
    """Logout and redirect to home."""
    return RedirectResponse(url="/register", status_code=303)


# ============================================================================
# Admin Routes (for monitoring)
# ============================================================================

@app.get("/admin/users", response_class=HTMLResponse)
async def admin_users(api_key: str = ""):
    """List all active users (admin only)."""
    # Simple API key check
    if api_key != os.getenv("ADMIN_API_KEY", ""):
        raise HTTPException(401, "Unauthorized")

    users = get_all_active_users()

    users_html = "".join([
        f"<tr><td>{u['user_id'][:8]}</td><td>{u['username']}</td><td>{u['broker_type']}</td><td>{'🟢' if not u['is_paused'] else '🔴'}</td></tr>"
        for u in users
    ])

    content = f"""
    <h1>Active Users</h1>
    <table border="1" style="width: 100%;">
        <tr><th>User ID</th><th>Username</th><th>Broker</th><th>Status</th></tr>
        {users_html if users_html else '<tr><td colspan="4">No active users</td></tr>'}
    </table>
    <p>Total: {len(users)} users</p>
    """

    return get_html("Admin - Users", content)


# ============================================================================
# Error Handlers
# ============================================================================

@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """Handle HTTP exceptions."""
    content = f"""
    <h1>Error {exc.status_code}</h1>
    <p class="error">{exc.detail}</p>
    <a href="/register"><button>Go Home</button></a>
    """
    return HTMLResponse(content=get_html(f"Error {exc.status_code}", content), status_code=exc.status_code)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
