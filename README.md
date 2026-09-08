**English** | [简体中文](README.zh-CN.md)

[![CI](https://github.com/yoashau/TGForward/actions/workflows/ci.yml/badge.svg)](https://github.com/yoashau/TGForward/actions/workflows/ci.yml)

# TGForward

A Telegram message extraction bot. Send a message link to copy text, photos, videos, and files to a private chat or a destination of your choice.

## Features

- **Message extraction**: Public and private sources, individual messages, albums, and directly forwarded messages.
- **Batch extraction**: Send multiple links, or append a count to extract consecutive messages, such as `https://t.me/example/100 10`.
- **Content settings**: Append captions, remove or replace words, add filename tags, set custom video thumbnails, and choose destinations.
- **Comment extraction**: Click the comment extraction button in the task message after extracting a post, or enable automatic comment extraction to include text and media from its discussion thread. The button shows progress in place; repeated clicks do not start concurrent tasks. Completion buttons only display results. Extracting comments again starts from the beginning and may duplicate previously delivered comments.
- **Accounts and helper bots**: Sign in with a personal account to access private content and bind a helper bot to handle sends.
- **Large files**: Upload files larger than 2 GB with a configured Premium account and staging channel.
- **Access management**: Administrators manage the allowlist; users can review their settings and extraction history.

### Getting started

1. Send `/start` to open the main menu.
2. Sign in under **Account & history**. Public sources usually do not require a personal account.
3. Send a message link. To stop, use the stop button in the progress message or send `/cancel`.
4. Use **Extraction settings** to configure captions, file rules, and destinations.

| Command | Purpose |
| --- | --- |
| `/start` | Main menu |
| `/setting` | Extraction settings |
| `/account` | Account and history |
| `/login`, `/logout` | Sign in to or out of a personal account |
| `/bindbot`, `/unbindbot` | Bind or unbind a helper bot |
| `/history`, `/me` | View history and account status |
| `/cancel` | Cancel the current operation |
| `/allow`, `/ban`, `/whitelist` | Manage the allowlist (administrators) |
| `/status`, `/broadcast` | View service status or send a broadcast (administrators) |

Private sources and discussion threads require a signed-in account with access. Public media is copied directly; private media is downloaded and uploaded. Filename and thumbnail settings apply to downloaded-and-uploaded files. Use a chat ID as the destination, or `chat_id/topic_id` for a topic.

### Interface language

Open **Language** in the main menu to choose English or Simplified Chinese. The choice is saved to your account and applies to menus, forms, confirmations, errors, and extraction/comment progress and results. Without a saved preference, Chinese Telegram locales use Chinese, other locales use English; an unavailable locale defaults to Chinese. Running tasks keep the language they started with. Original messages, captions, filenames, custom text, and third-party error details are not translated.

### Results and cancellation

Each link in a multi-link request has its own task message and extraction result. Final delivery is tracked per album member and per long-text segment. Comment counts refer to source messages: a long comment split into several segments still counts as one comment.

A post is successful only when its source scope is complete, its delivery parts are nonempty, and every part is confirmed delivered. Failure to read an album's source manifest stops that post rather than sending an incomplete manifest. Partial delivery is reported as incomplete. If a final send starts but a disconnection or forced cancellation prevents a definite response, its result is uncertain and it is not automatically resent. Successful post statistics and history are committed idempotently in one transaction; comment failures do not change the original post's result.

A user stop (`USER`) prevents new sends and waits for an ongoing final send to return; downloads and staging uploads may stop. Timeouts (`TIMEOUT`), account revocation (`REVOKED`), and shutdown (`SHUTDOWN`) may forcibly cancel tasks. Uploading through a Premium account to the staging channel is not final delivery: the copy to the destination must succeed. FloodWait and explicit text-entity rejections permit safe retries. Comment copies explicitly rejected for access reasons may fall back to downloading and uploading.

Partial album recovery records are kept for one hour. Account identity, user lifecycle, source version, destination, and actual payload distinguish recovery records; only confirmed members are reused. These records are an in-process recovery cache, not a persistent delivery journal. Missing local records after a crash do not establish the remote outcome, and exactly-once delivery across restarts is not guaranteed.

## Deployment

### First deployment

Requires Linux, Docker Compose V2, Git, and Python 3. No separate database service is needed.

```bash
git clone https://github.com/yoashau/TGForward.git
cd TGForward
sudo bash scripts/deploy.sh init
```

The first run creates the configuration file, generates independent keys, and pauses for configuration:

```bash
sudoedit /etc/tgforward/tgforward.env
```

Set the following four values, preserving the generated `MASTER_KEY` and `SALT_KEY`:

| Setting | Description |
| --- | --- |
| `API_ID`, `API_HASH` | Obtain from [my.telegram.org](https://my.telegram.org) |
| `BOT_TOKEN` | Obtain by creating a bot through [@BotFather](https://t.me/BotFather) |
| `OWNER_ID` | Administrator's numeric Telegram ID; separate multiple IDs with spaces |

Run the command again to build and start:

```bash
sudo bash scripts/deploy.sh init
```

For instances with existing user data, follow the [data import guide (Chinese)](docs/data-import.md) first. Do not initialize an empty database as if it were a new instance.

### Optional settings

These settings use the same configuration file:

| Setting | Description |
| --- | --- |
| `STRING`, `LOG_GROUP` | Premium account session string and staging channel ID; both are required for large-file uploads |
| `BATCH_DELAY` | Delay between batch extractions in seconds; default: `3` |
| `MAX_CONCURRENT_TRANSFERS` | Maximum concurrent transfers; default: `3` |
| `USER_COOLDOWN` | Minimum interval between extraction requests in seconds; default: `3` |
| `TASK_STALL_TIMEOUT` | Timeout for tasks without progress in seconds; default: `300` |

### Updates and maintenance

```bash
git pull --ff-only
sudo bash scripts/deploy.sh
```

Updates rebuild the application and wait for startup. They do not regenerate keys, initialize an empty database, or rewrite your source code.

```bash
sudo docker compose logs --tail=50 bot   # View recent logs
sudo docker compose restart bot        # Restart
sudo docker compose down               # Stop
```

Configuration and user data live outside the Git repository:

| Location | Contents |
| --- | --- |
| `/etc/tgforward/tgforward.env` | Account configuration and encryption keys |
| `/var/lib/tgforward/state/tgforward.sqlite3` | User accounts, settings, and history |
| `/var/lib/tgforward/thumbs/` | Custom thumbnails |

Container rebuilds, source updates, and `docker compose down -v` do not delete these host directories. **Keep your encryption keys unchanged**: replacing them prevents decryption of saved login credentials. For backups, stop the bot first, then back up the configuration file and the entire `/var/lib/tgforward` directory together.

## Project structure

```text
tgforward/
├── __main__.py       Application entry point
├── config.py         Configuration
├── handlers/         Commands and buttons
├── ui/               Menus and interactions
├── transfers/        Message extraction and file transfers
├── comments/         Discussion thread extraction
├── telegram/         Telegram connections and media adapters
├── storage/          User data and credentials
├── runtime/          Task management and service status
├── utils/            Text, link, and media utilities
└── tools/            Data import and maintenance tools
requirements/         Dependencies and pinned versions
tests/                Tests organized by feature
scripts/deploy.sh     Deployment entry point
```

See [CONTRIBUTING.md (Chinese)](CONTRIBUTING.md) for development and contribution instructions.
