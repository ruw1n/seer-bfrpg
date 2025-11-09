# Security & Privacy Notes
_Last updated: 2025-11-09_

This project uses a **bot token** (Discord OAuth2). To safely share your code:
- **Never include your real `.env` file** or any file that contains `DISCORD_TOKEN`.
- Your client ID/secret and invite URL live only in the **Discord Developer Portal** (web), not in the code.
- The cogs here (`*.py`) do **not** contain secrets by design.

If you ever accidentally published a real token, **reset it** in the Discord Developer Portal (Bot → Reset Token) and update your local `.env`.
