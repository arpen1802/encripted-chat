"""
Encrypted Chat Server — FastAPI backend
Run with: uvicorn main:app --host 0.0.0.0 --port 8000
"""
import asyncio
import json
import os
import uuid
import random
import aiosqlite
import bcrypt
from datetime import datetime, timedelta
from typing import Optional, Dict, List
from fastapi import (
    FastAPI, WebSocket, WebSocketDisconnect,
    HTTPException, Depends, UploadFile, File, Request
)
from fastapi.staticfiles import StaticFiles
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
import jwt
from pathlib import Path

# ─── Config ────────────────────────────────────────────────────────────────────
SECRET_KEY = os.getenv("SECRET_KEY", "CHANGE_ME_in_production_use_random_32chars")
ALGORITHM  = "HS256"
TOKEN_EXP_HOURS = 8
DB_PATH    = os.getenv("DB_PATH", "chat.db")
UPLOADS_DIR = Path(os.getenv("UPLOADS_DIR", "uploads"))
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_BYTES", str(50 * 1024 * 1024)))  # 50 MB default

AVATAR_COLORS = [
    "#7c3aed","#2563eb","#059669","#d97706",
    "#dc2626","#0891b2","#be185d","#65a30d",
]

app = FastAPI(title="EncryptedChat", docs_url=None, redoc_url=None)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="api/token")


# ─── WebSocket Connection Manager ──────────────────────────────────────────────
class ConnectionManager:
    def __init__(self):
        self.connections: Dict[str, WebSocket] = {}   # user_id → ws

    async def connect(self, user_id: str, ws: WebSocket):
        await ws.accept()
        # Close stale connection if any
        old = self.connections.pop(user_id, None)
        if old:
            try: await old.close()
            except: pass
        self.connections[user_id] = ws

    def disconnect(self, user_id: str):
        self.connections.pop(user_id, None)

    async def send(self, user_id: str, data: dict):
        ws = self.connections.get(user_id)
        if ws:
            try:
                await ws.send_json(data)
            except Exception:
                self.disconnect(user_id)

    async def broadcast(self, user_ids: List[str], data: dict):
        for uid in user_ids:
            await self.send(uid, data)

    def online(self) -> List[str]:
        return list(self.connections.keys())


manager = ConnectionManager()


# ─── Database ──────────────────────────────────────────────────────────────────
async def get_db():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        yield db


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id            TEXT PRIMARY KEY,
                username      TEXT UNIQUE NOT NULL,
                email         TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                public_key    TEXT,
                wrapped_priv  TEXT,
                priv_iv       TEXT,
                priv_salt     TEXT,
                is_admin      INTEGER DEFAULT 0,
                avatar_color  TEXT DEFAULT '#7c3aed',
                created_at    TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS channels (
                id          TEXT PRIMARY KEY,
                name        TEXT NOT NULL,
                description TEXT DEFAULT '',
                created_by  TEXT,
                created_at  TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS channel_members (
                channel_id TEXT NOT NULL,
                user_id    TEXT NOT NULL,
                joined_at  TEXT DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (channel_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS channel_keys (
                channel_id  TEXT NOT NULL,
                user_id     TEXT NOT NULL,
                wrapped_key TEXT NOT NULL,
                PRIMARY KEY (channel_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS messages (
                id                TEXT PRIMARY KEY,
                channel_id        TEXT NOT NULL,
                sender_id         TEXT NOT NULL,
                encrypted_content TEXT NOT NULL,
                iv                TEXT NOT NULL,
                file_id           TEXT,
                file_name         TEXT,
                timestamp         TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS dm_messages (
                id               TEXT PRIMARY KEY,
                sender_id        TEXT NOT NULL,
                recipient_id     TEXT NOT NULL,
                enc_for_sender   TEXT NOT NULL,
                enc_for_recipient TEXT NOT NULL,
                iv_sender        TEXT NOT NULL,
                iv_recipient     TEXT NOT NULL,
                file_id          TEXT,
                file_name        TEXT,
                timestamp        TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS dm_keys (
                user1_id  TEXT NOT NULL,
                user2_id  TEXT NOT NULL,
                key_for_1 TEXT,
                key_for_2 TEXT,
                PRIMARY KEY (user1_id, user2_id)
            );

            CREATE TABLE IF NOT EXISTS files (
                id            TEXT PRIMARY KEY,
                stored_name   TEXT NOT NULL,
                original_name TEXT NOT NULL,
                mime_type     TEXT,
                size          INTEGER,
                uploaded_by   TEXT,
                uploaded_at   TEXT DEFAULT CURRENT_TIMESTAMP
            );
        """)
        await db.commit()

        # Seed admin + #general if DB is empty
        cur = await db.execute("SELECT COUNT(*) FROM users")
        if (await cur.fetchone())[0] == 0:
            admin_id   = str(uuid.uuid4())
            pw_hash    = bcrypt.hashpw(b"admin123", bcrypt.gensalt()).decode()
            chan_id     = str(uuid.uuid4())
            await db.execute(
                "INSERT INTO users (id,username,email,password_hash,is_admin,avatar_color) VALUES (?,?,?,?,1,?)",
                (admin_id, "admin", "admin@company.local", pw_hash, AVATAR_COLORS[0])
            )
            await db.execute(
                "INSERT INTO channels (id,name,description,created_by) VALUES (?,?,?,?)",
                (chan_id, "general", "General company chat", admin_id)
            )
            await db.execute(
                "INSERT INTO channel_members (channel_id,user_id) VALUES (?,?)",
                (chan_id, admin_id)
            )
            await db.commit()
            print("=" * 60)
            print("  Default admin created:  admin / admin123")
            print("  Please change the password after first login!")
            print("=" * 60)


@app.on_event("startup")
async def startup():
    await init_db()


# ─── Auth helpers ───────────────────────────────────────────────────────────────
def make_token(user_id: str) -> str:
    exp = datetime.utcnow() + timedelta(hours=TOKEN_EXP_HOURS)
    return jwt.encode({"sub": user_id, "exp": exp}, SECRET_KEY, algorithm=ALGORITHM)


async def current_user(token: str = Depends(oauth2_scheme)):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        uid = payload["sub"]
    except Exception:
        raise HTTPException(401, "Invalid or expired token")
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM users WHERE id=?", (uid,))
        row = await cur.fetchone()
    if not row:
        raise HTTPException(401, "User not found")
    return dict(row)


async def admin_user(me=Depends(current_user)):
    if not me["is_admin"]:
        raise HTTPException(403, "Admin only")
    return me


def user_public(u: dict) -> dict:
    return {k: v for k, v in u.items() if k not in ("password_hash",)}


# ─── Pydantic models ────────────────────────────────────────────────────────────
class UserCreate(BaseModel):
    username: str
    email: str
    password: str
    is_admin: bool = False

class PasswordChange(BaseModel):
    old_password: str
    new_password: str

class KeysUpload(BaseModel):
    public_key:  str
    wrapped_priv: str
    priv_iv:      str
    priv_salt:    str

class ChannelCreate(BaseModel):
    name: str
    description: str = ""

class MsgCreate(BaseModel):
    encrypted_content: str
    iv: str
    file_id:   Optional[str] = None
    file_name: Optional[str] = None

class DMMsgCreate(BaseModel):
    enc_for_sender:    str
    enc_for_recipient: str
    iv_sender:         str
    iv_recipient:      str
    file_id:   Optional[str] = None
    file_name: Optional[str] = None

class DMKeyStore(BaseModel):
    recipient_id:    str
    key_for_me:      str
    key_for_them:    str


# ─── Auth endpoints ─────────────────────────────────────────────────────────────
@app.post("/api/token")
async def login(form: OAuth2PasswordRequestForm = Depends()):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM users WHERE username=?", (form.username,))
        u = await cur.fetchone()
    if not u or not bcrypt.checkpw(form.password.encode(), u["password_hash"].encode()):
        raise HTTPException(401, "Invalid username or password")
    return {
        "access_token": make_token(u["id"]),
        "token_type":   "bearer",
        "user": {
            "id":          u["id"],
            "username":    u["username"],
            "email":       u["email"],
            "is_admin":    bool(u["is_admin"]),
            "avatar_color":u["avatar_color"],
            "has_keys":    u["public_key"] is not None,
            "public_key":  u["public_key"],
            "wrapped_priv":u["wrapped_priv"],
            "priv_iv":     u["priv_iv"],
            "priv_salt":   u["priv_salt"],
        },
    }


@app.get("/api/me")
async def get_me(me=Depends(current_user)):
    return user_public(me)


@app.post("/api/me/password")
async def change_password(data: PasswordChange, me=Depends(current_user)):
    if not bcrypt.checkpw(data.old_password.encode(), me["password_hash"].encode()):
        raise HTTPException(400, "Current password is incorrect")
    new_hash = bcrypt.hashpw(data.new_password.encode(), bcrypt.gensalt()).decode()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET password_hash=? WHERE id=?", (new_hash, me["id"]))
        await db.commit()
    return {"status": "ok"}


# ─── Key management ─────────────────────────────────────────────────────────────
@app.post("/api/keys")
async def upload_keys(data: KeysUpload, me=Depends(current_user)):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET public_key=?,wrapped_priv=?,priv_iv=?,priv_salt=? WHERE id=?",
            (data.public_key, data.wrapped_priv, data.priv_iv, data.priv_salt, me["id"])
        )
        await db.commit()
    return {"status": "ok"}


@app.get("/api/keys/{user_id}")
async def get_keys(user_id: str, _=Depends(current_user)):
    """
    Returns ONLY the public key for the given user. The wrapped private key,
    its IV, and salt are sensitive — they enable offline brute-force of the
    user's password — and must never be returned for anyone other than the
    owner. The owner receives their own wrapped material as part of the
    /api/token login response and via /api/me.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT public_key FROM users WHERE id=?", (user_id,)
        )
        row = await cur.fetchone()
    if not row:
        raise HTTPException(404, "User not found")
    return {"public_key": row["public_key"]}


# ─── User management ────────────────────────────────────────────────────────────
@app.get("/api/users")
async def list_users(me=Depends(current_user)):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id,username,email,is_admin,avatar_color,created_at,public_key FROM users ORDER BY username"
        )
        rows = await cur.fetchall()
    online = set(manager.online())
    return [
        {**dict(r), "online": r["id"] in online, "has_keys": r["public_key"] is not None}
        for r in rows
    ]


@app.post("/api/users")
async def create_user(data: UserCreate, _=Depends(admin_user)):
    uid      = str(uuid.uuid4())
    pw_hash  = bcrypt.hashpw(data.password.encode(), bcrypt.gensalt()).decode()
    color    = random.choice(AVATAR_COLORS)
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT INTO users (id,username,email,password_hash,is_admin,avatar_color) VALUES (?,?,?,?,?,?)",
                (uid, data.username, data.email, pw_hash, int(data.is_admin), color)
            )
            # Add to every channel
            cur = await db.execute("SELECT id FROM channels")
            for ch in await cur.fetchall():
                await db.execute(
                    "INSERT OR IGNORE INTO channel_members (channel_id,user_id) VALUES (?,?)",
                    (ch[0], uid)
                )
            await db.commit()
    except aiosqlite.IntegrityError:
        raise HTTPException(400, "Username or email already taken")

    # Notify online users
    await manager.broadcast(manager.online(), {
        "type": "user_created",
        "user": {"id": uid, "username": data.username, "avatar_color": color, "is_admin": data.is_admin}
    })
    return {"id": uid, "username": data.username, "email": data.email, "is_admin": data.is_admin, "avatar_color": color}


@app.delete("/api/users/{user_id}")
async def delete_user(user_id: str, me=Depends(admin_user)):
    if user_id == me["id"]:
        raise HTTPException(400, "Cannot delete yourself")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM users WHERE id=?", (user_id,))
        await db.commit()
    await manager.broadcast(manager.online(), {"type": "user_deleted", "user_id": user_id})
    return {"status": "deleted"}


# ─── Channels ───────────────────────────────────────────────────────────────────
@app.get("/api/channels")
async def list_channels(me=Depends(current_user)):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("""
            SELECT c.id, c.name, c.description, c.created_at,
                   COUNT(cm2.user_id) AS member_count
            FROM channels c
            JOIN channel_members cm ON c.id=cm.channel_id AND cm.user_id=?
            LEFT JOIN channel_members cm2 ON c.id=cm2.channel_id
            GROUP BY c.id ORDER BY c.name
        """, (me["id"],))
        return [dict(r) for r in await cur.fetchall()]


@app.post("/api/channels")
async def create_channel(data: ChannelCreate, me=Depends(current_user)):
    cid  = str(uuid.uuid4())
    name = data.name.lower().replace(" ", "-")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO channels (id,name,description,created_by) VALUES (?,?,?,?)",
            (cid, name, data.description, me["id"])
        )
        cur = await db.execute("SELECT id FROM users")
        for u in await cur.fetchall():
            await db.execute(
                "INSERT OR IGNORE INTO channel_members (channel_id,user_id) VALUES (?,?)",
                (cid, u[0])
            )
        await db.commit()
    chan = {"id": cid, "name": name, "description": data.description, "member_count": 0}
    await manager.broadcast(manager.online(), {"type": "channel_created", "channel": chan})
    return chan


@app.get("/api/channels/{cid}/members")
async def channel_members(cid: str, me=Depends(current_user)):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("""
            SELECT u.id, u.username, u.avatar_color, u.public_key, u.is_admin
            FROM users u
            JOIN channel_members cm ON u.id=cm.user_id AND cm.channel_id=?
            ORDER BY u.username
        """, (cid,))
        rows = await cur.fetchall()
    online = set(manager.online())
    return [{**dict(r), "online": r["id"] in online, "has_keys": r["public_key"] is not None} for r in rows]


# ─── Channel encryption keys ─────────────────────────────────────────────────────
@app.post("/api/channels/{cid}/keys")
async def store_channel_keys(cid: str, payload: dict, _=Depends(current_user)):
    """payload = {user_id: wrapped_key_base64, ...}"""
    async with aiosqlite.connect(DB_PATH) as db:
        for uid, wk in payload.items():
            await db.execute(
                "INSERT OR REPLACE INTO channel_keys (channel_id,user_id,wrapped_key) VALUES (?,?,?)",
                (cid, uid, wk)
            )
        await db.commit()
    return {"stored": len(payload)}


@app.get("/api/channels/{cid}/keys/me")
async def my_channel_key(cid: str, me=Depends(current_user)):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT wrapped_key FROM channel_keys WHERE channel_id=? AND user_id=?",
            (cid, me["id"])
        )
        row = await cur.fetchone()
    return {"wrapped_key": row[0] if row else None}


# ─── Messages ───────────────────────────────────────────────────────────────────
@app.get("/api/channels/{cid}/messages")
async def get_messages(cid: str, limit: int = 80, me=Depends(current_user)):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?", (cid, me["id"])
        )
        if not await cur.fetchone():
            raise HTTPException(403, "Not a member")
        cur = await db.execute("""
            SELECT m.id, m.encrypted_content, m.iv, m.file_id, m.file_name, m.timestamp,
                   u.id AS sender_id, u.username AS sender_name, u.avatar_color
            FROM messages m JOIN users u ON m.sender_id=u.id
            WHERE m.channel_id=?
            ORDER BY m.timestamp DESC LIMIT ?
        """, (cid, limit))
        rows = await cur.fetchall()
    return [dict(r) for r in reversed(rows)]


@app.post("/api/channels/{cid}/messages")
async def post_message(cid: str, msg: MsgCreate, me=Depends(current_user)):
    mid = str(uuid.uuid4())
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?", (cid, me["id"])
        )
        if not await cur.fetchone():
            raise HTTPException(403, "Not a member")
        await db.execute(
            "INSERT INTO messages (id,channel_id,sender_id,encrypted_content,iv,file_id,file_name,timestamp) VALUES (?,?,?,?,?,?,?,?)",
            (mid, cid, me["id"], msg.encrypted_content, msg.iv, msg.file_id, msg.file_name, now)
        )
        await db.commit()
        cur = await db.execute("SELECT user_id FROM channel_members WHERE channel_id=?", (cid,))
        members = [r[0] for r in await cur.fetchall()]

    payload = {
        "id": mid, "channel_id": cid,
        "encrypted_content": msg.encrypted_content, "iv": msg.iv,
        "file_id": msg.file_id, "file_name": msg.file_name,
        "sender_id": me["id"], "sender_name": me["username"],
        "avatar_color": me["avatar_color"], "timestamp": now,
    }
    await manager.broadcast(members, {"type": "channel_msg", "message": payload})
    return payload


# ─── DMs ────────────────────────────────────────────────────────────────────────
@app.get("/api/dm/{other_id}/messages")
async def get_dm(other_id: str, me=Depends(current_user)):
    my_id = me["id"]
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("""
            SELECT m.id, m.sender_id, m.recipient_id,
                   m.enc_for_sender, m.enc_for_recipient,
                   m.iv_sender, m.iv_recipient,
                   m.file_id, m.file_name, m.timestamp,
                   u.username AS sender_name, u.avatar_color
            FROM dm_messages m JOIN users u ON m.sender_id=u.id
            WHERE (m.sender_id=? AND m.recipient_id=?) OR (m.sender_id=? AND m.recipient_id=?)
            ORDER BY m.timestamp ASC LIMIT 100
        """, (my_id, other_id, other_id, my_id))
        rows = await cur.fetchall()

    result = []
    for r in rows:
        d = dict(r)
        if d["sender_id"] == my_id:
            d["encrypted_content"] = d["enc_for_sender"]
            d["iv"] = d["iv_sender"]
        else:
            d["encrypted_content"] = d["enc_for_recipient"]
            d["iv"] = d["iv_recipient"]
        result.append(d)
    return result


@app.post("/api/dm/{other_id}/messages")
async def post_dm(other_id: str, msg: DMMsgCreate, me=Depends(current_user)):
    mid = str(uuid.uuid4())
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO dm_messages
            (id,sender_id,recipient_id,enc_for_sender,enc_for_recipient,iv_sender,iv_recipient,file_id,file_name,timestamp)
            VALUES (?,?,?,?,?,?,?,?,?,?)
        """, (mid, me["id"], other_id,
              msg.enc_for_sender, msg.enc_for_recipient,
              msg.iv_sender, msg.iv_recipient,
              msg.file_id, msg.file_name, now))
        await db.commit()

    base = {
        "id": mid, "sender_id": me["id"], "recipient_id": other_id,
        "sender_name": me["username"], "avatar_color": me["avatar_color"],
        "file_id": msg.file_id, "file_name": msg.file_name, "timestamp": now,
    }
    await manager.send(other_id, {"type": "dm_msg", "from_user": me["id"], "message": {
        **base, "encrypted_content": msg.enc_for_recipient, "iv": msg.iv_recipient
    }})
    await manager.send(me["id"], {"type": "dm_msg", "from_user": me["id"], "message": {
        **base, "encrypted_content": msg.enc_for_sender, "iv": msg.iv_sender
    }})
    return base


# ─── DM encryption keys ──────────────────────────────────────────────────────────
@app.post("/api/dm/keys")
async def store_dm_keys(data: DMKeyStore, me=Depends(current_user)):
    u1, u2 = sorted([me["id"], data.recipient_id])
    k1 = data.key_for_me      if me["id"] == u1 else data.key_for_them
    k2 = data.key_for_them    if me["id"] == u1 else data.key_for_me
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO dm_keys (user1_id,user2_id,key_for_1,key_for_2) VALUES (?,?,?,?)",
            (u1, u2, k1, k2)
        )
        await db.commit()
    return {"status": "ok"}


@app.get("/api/dm/{other_id}/keys/me")
async def get_dm_key(other_id: str, me=Depends(current_user)):
    u1, u2 = sorted([me["id"], other_id])
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT key_for_1,key_for_2 FROM dm_keys WHERE user1_id=? AND user2_id=?", (u1, u2)
        )
        row = await cur.fetchone()
    if not row:
        return {"wrapped_key": None}
    return {"wrapped_key": row[0] if me["id"] == u1 else row[1]}


# ─── File upload / download ──────────────────────────────────────────────────────
# Files are end-to-end encrypted: the client encrypts with the conversation
# (channel/DM) key, prepends the IV (12 bytes) to the ciphertext, and uploads
# the resulting opaque blob. Original filename and MIME type are encrypted
# into the message body, NOT stored on the server. The server sees only
# opaque bytes and a UUID.
@app.post("/api/files")
async def upload_file(file: UploadFile = File(...), me=Depends(current_user)):
    fid     = str(uuid.uuid4())
    stored  = fid                              # no extension; the blob is ciphertext
    content = await file.read()
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"File too large (max {MAX_UPLOAD_BYTES} bytes)")
    (UPLOADS_DIR / stored).write_bytes(content)
    async with aiosqlite.connect(DB_PATH) as db:
        # original_name/mime_type intentionally left empty — they would leak
        # metadata. The real values live inside the encrypted message body.
        await db.execute(
            "INSERT INTO files (id,stored_name,original_name,mime_type,size,uploaded_by) VALUES (?,?,?,?,?,?)",
            (fid, stored, "", "application/octet-stream", len(content), me["id"])
        )
        await db.commit()
    return {"file_id": fid, "size": len(content)}


@app.get("/api/files/{fid}")
async def get_file(fid: str, _=Depends(current_user)):
    """
    Returns the stored blob as opaque bytes. New uploads are ciphertext that
    the client decrypts; legacy uploads (pre-encryption) still have an
    original_name in the DB and are served with their original filename so
    they remain usable.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT stored_name,original_name FROM files WHERE id=?", (fid,))
        row = await cur.fetchone()
    if not row:
        raise HTTPException(404, "File not found")
    stored, orig = row
    kwargs = {"media_type": "application/octet-stream"}
    if orig:                                   # legacy plaintext file
        kwargs["filename"] = orig
    return FileResponse(UPLOADS_DIR / stored, **kwargs)


# ─── WebSocket ───────────────────────────────────────────────────────────────────
@app.websocket("/ws/{token}")
async def websocket_endpoint(ws: WebSocket, token: str):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        uid = payload["sub"]
    except Exception:
        await ws.close(code=1008)
        return

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT username FROM users WHERE id=?", (uid,))
        row = await cur.fetchone()
    if not row:
        await ws.close(code=1008)
        return
    username = row[0]

    await manager.connect(uid, ws)
    await manager.broadcast(
        [u for u in manager.online() if u != uid],
        {"type": "presence", "user_id": uid, "username": username, "online": True}
    )

    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
                if msg.get("type") == "typing":
                    cid = msg.get("channel_id")
                    if cid:
                        async with aiosqlite.connect(DB_PATH) as db:
                            cur = await db.execute(
                                "SELECT user_id FROM channel_members WHERE channel_id=?", (cid,)
                            )
                            members = [r[0] for r in await cur.fetchall()]
                        await manager.broadcast(
                            [m for m in members if m != uid],
                            {"type": "typing", "user_id": uid, "username": username, "channel_id": cid}
                        )
                elif msg.get("type") == "ping":
                    await manager.send(uid, {"type": "pong"})
            except Exception:
                pass
    except WebSocketDisconnect:
        manager.disconnect(uid)
        await manager.broadcast(
            manager.online(),
            {"type": "presence", "user_id": uid, "username": username, "online": False}
        )


# ─── Serve static frontend ───────────────────────────────────────────────────────
app.mount("/", StaticFiles(directory="static", html=True), name="static")
