# Phone-based Telegram account login

The account onboarding menu supports QR and phone-number login, each with direct or configured-proxy connection.

For phone login, Telegram sends a one-time code in Telegram or by SMS. If two-step verification is enabled, the bot can accept the second factor in the private chat. The app does not write that value to PostgreSQL or application logs and attempts to delete the incoming message. Telegram message deletion is not guaranteed, and the bot/server owner can access messages sent to the bot. Use this only on a server you trust; for sensitive accounts, prefer a trusted terminal.

Session files are written under the configured data directory with restricted filesystem permissions. Never commit session files or send them to chats.

Newly connected accounts remain AI-disabled and use manual reply mode until the operator explicitly changes their settings.
