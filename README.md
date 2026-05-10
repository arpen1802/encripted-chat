# EncryptedChat

A self-hosted, end-to-end encrypted chat for small internal teams. Runs as a single FastAPI process with a SQLite database and a vanilla HTML/JS frontend. All message content is encrypted in the browser; the server only ever sees ciphertext.

```
┌────────────┐      HTTPS / WSS       ┌──────────────────┐
│  Browser   │  ───────────────────►  │  FastAPI server  │
│ (WebCrypto)│  ◄───────────────────  │   + SQLite DB    │
└────────────┘                        └──────────────────┘
   plaintext                            ciphertext only
```

---

## Quick start

```bash
git clone <this-repo> encrypted-chat
cd encrypted-chat
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Required: set a strong random secret. Server refuses to start without it.
export SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")

./start.sh
```

Open `http://localhost:8000` and log in with the seeded admin account printed on first boot (`admin` / `admin123`). **Change this password immediately.**

For anything beyond local development, **do not expose port 8000 directly** — put the app behind nginx/Caddy with TLS. See [Deployment](#deployment).

---

## Architecture

### How end-to-end encryption works

The server stores ciphertext only. Three layers of keys are involved:

1. **User identity key (RSA-OAEP, 2048-bit).** Generated in the browser the first time a user logs in. The public half is uploaded; the private half is wrapped with a key derived from the user's password (PBKDF2-SHA256, 250k iterations) and stored on the server. The server never sees the unwrapped private key.

2. **Conversation key (AES-GCM, 256-bit).** One per channel and one per DM pair. Used to encrypt and decrypt every message in that conversation.

3. **Wrapped conversation keys.** The conversation key is wrapped (encrypted) with each member's RSA public key and stored on the server, one wrapped copy per member. When a user opens a channel, the server hands them their wrapped copy, which they unwrap with their private key.

```
password ──PBKDF2──► wrapping key ──unwrap──► RSA private key
                                                     │
                                                     ▼
server: wrapped(channel_key, RSA_pub_user) ──unwrap──► channel key
                                                     │
                                                     ▼
                          AES-GCM encrypt/decrypt every message
```

### Message flow

**Sending to a channel:**
1. Client fetches its wrapped channel key (cached after first fetch).
2. Client unwraps it with its in-memory RSA private key.
3. Client encrypts plaintext with AES-GCM, generating a fresh IV per message.
4. Ciphertext + IV are POSTed; server stores them and broadcasts to channel members over WebSocket.

**Receiving:**
1. Client receives ciphertext + IV via WebSocket.
2. Decrypts with the cached channel key.
3. Renders plaintext.

**DMs** work the same way but use a per-pair AES-GCM key wrapped for both participants.

### Files

Files attached to messages are encrypted in the browser with the same conversation key before upload. The server stores opaque ciphertext blobs and never sees plaintext file contents. The original filename is encrypted as part of the message body, not stored as metadata.

### Repository layout

```
.
├── main.py             # FastAPI app: auth, REST API, WebSocket
├── static/index.html   # Single-file frontend (HTML + CSS + JS)
├── requirements.txt
├── start.sh            # Dev launch script (creates SECRET_KEY if absent, runs uvicorn --reload)
├── chat.db             # SQLite database (created on first run; gitignored)
└── uploads/            # Encrypted file blobs (created on first run; gitignored)
```

### Tech stack

- **Backend:** FastAPI, uvicorn, aiosqlite, bcrypt, PyJWT
- **Database:** SQLite (WAL mode)
- **Frontend:** Vanilla JS, Web Crypto API (no build step, no npm)
- **Realtime:** WebSocket (presence, typing, message fanout)

---

## Deployment

### Production checklist

Before exposing this to anyone:

1. Set `SECRET_KEY` to a long random value via environment variable. The server will refuse to start without it.
2. Put the app behind a reverse proxy (nginx, Caddy, or Traefik) terminating TLS.
3. Run as a non-root user under a process manager (systemd, supervisor, Docker).
4. Disable `--reload` in your launch command.
5. Restrict network access — this is designed for an internal LAN, not the public internet.
6. Set up regular SQLite backups (see [Admin runbook](#admin-runbook)).
7. Change the seeded admin password and create real user accounts.

### Caddy (simplest, automatic HTTPS)

Caddy with a `Caddyfile` like this gives you HTTPS, HTTP/2, and WebSocket support out of the box:

```caddy
chat.company.local {
    reverse_proxy 127.0.0.1:8000
}
```

For a public hostname, Caddy will obtain a Let's Encrypt cert automatically. For a `.local` hostname, terminate TLS with an internal CA cert.

### nginx

```nginx
server {
    listen 443 ssl http2;
    server_name chat.company.local;

    ssl_certificate     /etc/ssl/certs/chat.crt;
    ssl_certificate_key /etc/ssl/private/chat.key;

    # HSTS, only enable once you're sure HTTPS works
    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;

    client_max_body_size 50M;   # adjust to your file-upload ceiling

    location / {
        proxy_pass         http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header   Host              $host;
        proxy_set_header   X-Real-IP         $remote_addr;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
    }

    # WebSocket upgrade
    location /ws/ {
        proxy_pass         http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header   Upgrade           $http_upgrade;
        proxy_set_header   Connection        "upgrade";
        proxy_set_header   Host              $host;
        proxy_read_timeout 3600s;
    }
}

server {
    listen 80;
    server_name chat.company.local;
    return 301 https://$host$request_uri;
}
```

### systemd unit

Save as `/etc/systemd/system/encryptedchat.service`:

```ini
[Unit]
Description=EncryptedChat
After=network.target

[Service]
Type=simple
User=encryptedchat
Group=encryptedchat
WorkingDirectory=/opt/encryptedchat
EnvironmentFile=/etc/encryptedchat.env
ExecStart=/opt/encryptedchat/.venv/bin/uvicorn main:app --host 127.0.0.1 --port 8000
Restart=on-failure
RestartSec=5

# Hardening
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths=/opt/encryptedchat

[Install]
WantedBy=multi-user.target
```

`/etc/encryptedchat.env`:

```
SECRET_KEY=<64+ hex chars>
DB_PATH=/opt/encryptedchat/data/chat.db
UPLOADS_DIR=/opt/encryptedchat/data/uploads
ALLOWED_ORIGINS=https://chat.company.local
```

Enable and start:

```bash
sudo systemctl enable --now encryptedchat
```

### Environment variables

| Variable           | Default               | Notes                                                                 |
|--------------------|-----------------------|-----------------------------------------------------------------------|
| `SECRET_KEY`       | *(required)*          | Used to sign JWTs. Must be set; server refuses the default.           |
| `DB_PATH`          | `chat.db`             | SQLite file path.                                                     |
| `UPLOADS_DIR`      | `uploads`             | Directory for encrypted file blobs.                                   |
| `ALLOWED_ORIGINS`  | `*` (dev only)        | Comma-separated list. Set to the actual hostname(s) in production.    |
| `HOST`             | `0.0.0.0`             | Bind address. Behind a proxy, prefer `127.0.0.1`.                     |
| `PORT`             | `8000`                |                                                                       |

---

## Admin runbook

### First-time setup

The first run seeds:
- An admin user `admin` / `admin123`
- A `#general` channel with the admin as the only member

**Change the admin password from inside the app on first login.** The seed credentials are printed to stdout on startup so you don't lose them.

### Creating users

Only admins can create accounts. Open the admin panel (shield icon in the sidebar) and use the "Create New User" form. Users get added to every existing channel automatically. They generate their encryption keys on first login.

### Backups

The whole state is in two places:

```
$DB_PATH               # chat.db (default)
$UPLOADS_DIR           # encrypted file blobs (default: uploads/)
```

Take an online SQLite backup with the `.backup` command (safe while the server is running):

```bash
sqlite3 /opt/encryptedchat/data/chat.db ".backup '/backups/chat-$(date +%F).db'"
rsync -a /opt/encryptedchat/data/uploads/ /backups/uploads/
```

A nightly cron job covers most use cases. Encrypt backups at rest — they contain wrapped private keys, and an attacker with the backup plus a user's password can decrypt that user's history.

### Rotating SECRET_KEY

Rotating `SECRET_KEY` invalidates every active session (users have to log back in). It does **not** affect any encrypted message content — message keys are wrapped under user RSA keys, not the JWT secret.

```bash
# Generate a new one
python3 -c "import secrets; print(secrets.token_hex(32))"

# Update /etc/encryptedchat.env, then
sudo systemctl restart encryptedchat
```

### Removing a user

Deleting a user from the admin panel:
- Removes them from all channels.
- Cascades to delete their messages, channel/DM keys, and uploaded files.
- Closes any active WebSocket sessions.

If a user leaves the company, after deletion you should also rotate the keys of any channels they were a member of (current limitation — see below).

### Inspecting the database

```bash
sqlite3 /opt/encryptedchat/data/chat.db
sqlite> .tables
sqlite> SELECT id, username, email, is_admin, created_at FROM users;
```

Message tables only contain ciphertext — there's nothing useful for an admin to read there.

---

## Threat model

### What this protects against

- **Server compromise (read-only).** An attacker who gets read access to the database and uploads directory sees only ciphertext, wrapped keys, and bcrypt-hashed login passwords. They cannot read messages or files without separately compromising user passwords.
- **Network eavesdropping.** With HTTPS in front, all traffic is encrypted in transit. Even without it, message bodies are encrypted at the application layer.
- **Curious admins.** A workspace admin can create or delete accounts but cannot read any user's messages — they don't have the unwrapping key.
- **Stolen backups.** Same as server compromise: ciphertext only. **However**, backups contain wrapped private keys, and a weak user password becomes a foothold via offline brute-force. Use strong passwords.

### What this does NOT protect against

- **Active server compromise.** A compromised server can serve modified frontend JavaScript that exfiltrates plaintext or keys before encryption. End-to-end encryption in a web app fundamentally trusts the server to deliver honest code. For a higher-trust setup, ship the frontend as a packaged native client.
- **Endpoint compromise.** Malware on a user's machine that reads the browser's memory or DOM defeats E2E entirely.
- **Forward secrecy.** Channel/DM keys are long-lived. If a user's password is ever compromised, all of their historical messages can be decrypted. Mitigation: strong passwords, periodic key rotation (manual at present).
- **Membership-change confidentiality.** When a user is removed from a channel, the existing channel key is not rotated. They still hold a copy and can decrypt any message backups they made before removal. **Workaround:** create a new channel and migrate.
- **Metadata.** The server sees who messages whom, when, and in which channel. Message timing, sender, recipient, channel membership, and online presence are all visible to anyone with database access.
- **Brute-force of the wrapping password.** PBKDF2-SHA256 at 250k iterations is on the lower end of OWASP's recommended range. Strong passwords are critical.
- **Rogue admins.** An admin can create a new user, add them to a channel, and from that point forward read messages in that channel. They cannot read messages sent before the addition (they don't have the channel key wrapped for them). Audit `users` and `channel_members` periodically.

### Cryptographic primitives

| Use                       | Algorithm                                      |
|---------------------------|------------------------------------------------|
| Identity keypair          | RSA-OAEP, 2048-bit, SHA-256                    |
| Conversation key          | AES-GCM, 256-bit, fresh 96-bit IV per message  |
| Password → wrapping key   | PBKDF2-SHA256, 250,000 iterations, 16-byte salt|
| Login password storage    | bcrypt (cost factor default)                   |
| Session token             | JWT HS256, 8-hour expiry                       |

All cryptography happens in the browser via the Web Crypto API. No third-party crypto library is loaded.

---

## Limitations & known gaps

- Single-process deployment only. The WebSocket connection manager is in-memory; running multiple workers would break presence and broadcast.
- No message edit, delete, replies, threads, or reactions.
- No search (would require client-side decryption + indexing).
- No 2FA on login.
- No automatic key rotation on member removal.
- No audit log for admin actions.
- Mobile layout is functional but not optimised.

These are deliberate tradeoffs to keep the codebase small and auditable. Contributions welcome.

---

## License

Internal use. Add your company's preferred license here.
