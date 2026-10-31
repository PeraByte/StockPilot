# Deploying StockPilot

StockPilot stores its data in a **SQLite file** and keeps logins in **signed cookies**. That design is perfect for one computer or one server with a disk. It does **not** work on "serverless" hosts such as Vercel, Netlify or AWS Lambda.

## Why users get sent back to the login page on Vercel

A serverless host runs your app as many short-lived copies ("instances"). Every copy has its **own temporary folder** (`/tmp`) that is thrown away when the copy is recycled. So each copy has:

- **its own empty database**: you register on copy A, the next click is handled by copy B, which has never heard of you, so you are sent to the login page;
- **its own random secret key** (unless you set one), so cookies signed by copy A are rejected by copy B.

"Add product" lands on one copy, "Save product" on another, and each asks you to log in again. Data you add also vanishes when a copy is recycled. This is not a session-timeout setting; sessions here last 12 hours and renew while you work. The only real fixes are a host with a **persistent disk** (below), or moving the data to a hosted database server (not included in this project).

## Environment variables

| Variable | Needed | Meaning |
|---|---|---|
| `STOCKPILOT_SECRET_KEY` | **always when deployed** | Signs login cookies. At least 16 characters; use a long random value. **Never put it in the code or in git.** Create one with `python3 -c "import secrets; print(secrets.token_hex(32))"`. |
| `STOCKPILOT_COOKIE_SECURE` | when the site uses `https://` | Set to `1` so the login cookie is only sent over HTTPS. |
| `STOCKPILOT_DATABASE` | optional | Full path of the SQLite file. Point it at a persistent disk or volume. |
| `STOCKPILOT_TIMEZONE` | optional | For example `Asia/Karachi` (the default). |

If a key such as `7272` was ever written into your code, treat it as public: anyone who knows the key can forge a login cookie. Generate a new random key and set it only as an environment variable.

## Option 1: PythonAnywhere (free account, no code changes)

Its disk is permanent and it runs Flask directly, so StockPilot works as it does on your laptop.

1. Create an account at pythonanywhere.com and open a **Bash console**.
2. Upload `stockpilot.zip` (Files tab), then run `unzip stockpilot.zip` (you get `~/stockpilot`).
3. Create the environment and install the requirements:
   ```
   cd ~/stockpilot
   python3 -m venv ~/venvs/stockpilot
   source ~/venvs/stockpilot/bin/activate
   pip install -r requirements.txt
   ```
4. **Web** tab: *Add a new web app*, choose *Manual configuration*, and pick the same Python version as your virtualenv.
5. On the Web tab set **Source code** and **Working directory** to `/home/YOUR_USERNAME/stockpilot` and **Virtualenv** to `/home/YOUR_USERNAME/venvs/stockpilot`.
6. Click the **WSGI configuration file** link and replace everything in it with:
   ```python
   import os, sys

   project = "/home/YOUR_USERNAME/stockpilot"
   if project not in sys.path:
       sys.path.insert(0, project)

   os.environ["STOCKPILOT_SECRET_KEY"] = "PASTE-YOUR-LONG-RANDOM-VALUE-HERE"
   os.environ["STOCKPILOT_COOKIE_SECURE"] = "1"

   from wsgi import application
   ```
7. Press **Reload**, then open `https://YOUR_USERNAME.pythonanywhere.com` and register.

Your data lives in `~/stockpilot/instance/stockpilot.db`. Download it now and then as a backup. Free-plan rules change, so read PythonAnywhere's help pages for current limits.

## Option 2: any server or platform with a persistent disk

This works on a VPS, or on Render, Railway or Fly.io **when a persistent disk or volume is attached** (without one the database is lost on every redeploy, just as on Vercel).

```
pip install -r requirements.txt gunicorn
export STOCKPILOT_SECRET_KEY="a-long-random-value"
export STOCKPILOT_COOKIE_SECURE=1                  # when served over https
export STOCKPILOT_DATABASE=/data/stockpilot.db     # a folder on the persistent disk
gunicorn wsgi:application --workers 2 --bind 0.0.0.0:8000
```

Several workers are fine: logins are signed cookies, so any worker accepts them, and checkout uses SQLite write locks. All workers must share the same database file on the same disk (not a network share).

## Staying on Vercel

You would need to move the data out of SQLite into a hosted database (for example PostgreSQL). That means rewriting the SQL layer (`database.py`, `inventory.py`, `sales.py`, `reports.py`, `schema.sql`), which this project does not include.

## What the app does to protect you

- On a serverless platform it refuses to start without `STOCKPILOT_SECRET_KEY`, with a message that says what to do, instead of silently logging everyone out.
- It rejects secret keys shorter than 16 characters.
- It logs a warning when the database sits in a temporary folder.
- When several workers start together on a fresh install, they all agree on one complete key file.
