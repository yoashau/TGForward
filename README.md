[![CI](https://github.com/yoashau/TGForward/actions/workflows/ci.yml/badge.svg)](https://github.com/yoashau/TGForward/actions/workflows/ci.yml)

# TGForward

Telegram 消息提取机器人。发送消息链接，即可把文字、图片、视频和文件转存到私聊或指定聊天。

## 项目功能

- **消息提取**：支持公开和私有来源、单条消息、相册，以及直接转发的消息。
- **批量提取**：一次发送多个链接；链接后加数字可连续提取，如 `https://t.me/example/100 10`。
- **内容设置**：追加文案、删词、替换词、文件名标签、自定义视频封面和发送目标。
- **评论提取**：未开启“同时提取评论”时，在提取完成后的任务管理消息中点击“💬 提取评论”；开启后自动提取关联评论区的文字和媒体。
  按钮原地显示提取状态，重复点击不会同时启动多个任务；完成按钮只显示结果；「重新提取评论」会从头读取，已发送评论可能重复。
- **账号与辅助 bot**：登录个人账号访问私有内容，绑定辅助机器人分担发送任务。
- **大文件**：配置 Premium 账号与中转频道后，支持上传超过 2GB 的文件。
- **访问管理**：管理员管理白名单，用户查看自己的配置和提取记录。

### 开始使用

1. 向机器人发送 `/start`，打开主菜单。
2. 在“账号与记录”中登录账号；公开来源通常无需登录。
3. 发送消息链接。需要停止时点击进度消息上的停止按钮，或发送 `/cancel`。
4. 在“提取设置”中修改文案、文件规则和发送目标。

| 命令 | 用途 |
| --- | --- |
| `/start` | 主菜单 |
| `/setting` | 提取设置 |
| `/account` | 账号与记录 |
| `/login`、`/logout` | 登录、退出个人账号 |
| `/bindbot`、`/unbindbot` | 绑定、解绑辅助 bot |
| `/history`、`/me` | 查看历史和个人状态 |
| `/cancel` | 取消当前操作 |
| `/allow`、`/ban`、`/whitelist` | 管理白名单（管理员） |
| `/status`、`/broadcast` | 查看运行状态、发送广播（管理员） |

私有来源和评论区需要登录账号拥有访问权限。公开媒体直接复制，私有媒体下载后上传；
文件名和封面设置作用于下载上传的文件。发送目标可填写聊天 ID，话题目标填写 `聊天ID/话题ID`。

### 提取结果与取消

一次请求包含多个链接时，每个链接拥有独立的管理消息和提取结果。
相册按成员、长文字按分段记录最终目标送达事实；评论数量按源评论消息计算，
一条长文字评论的多个分段仍计一条评论。

只有源消息范围完整、发送部分非空且全部确认送达，才记录整帖成功。
相册源清单读取失败会停止该帖，不发送残缺清单。部分送达显示未完成；
最终发送已启动但网络断开或被强制终止、没有明确返回时，显示结果无法确认，
不会自动重发。原帖的成功统计和历史在同一事务中幂等提交，评论失败不改变原帖结果。

普通停止（USER）阻止新的发送，等待已经开始的最终发送返回；下载及中转上传可以停止。
超时（TIMEOUT）、账号撤销（REVOKED）和服务关闭（SHUTDOWN）可以强制终止任务。
Premium 上传至中转频道不计作最终送达，只有复制到目标成功才算完成。
FloodWait 和明确的文本实体拒绝允许安全重试；评论复制遇到明确访问拒绝时可下载发送。

相册部分成功记录保留一小时，使用账号身份、用户生命周期、源版本、目标及实际内容
区分恢复记录，只复用已确认发送的成员。记录属于进程内恢复缓存，不是持久化发送日志；
进程崩溃后的远端结果不依据本地记录缺失推断，也不承诺跨重启 exactly-once。

## 项目部署

### 首次部署

需要 Linux、Docker Compose V2、Git 和 Python 3。无需安装独立数据库服务。

```bash
git clone https://github.com/yoashau/TGForward.git
cd TGForward
sudo bash scripts/deploy.sh init
```

第一次运行会创建配置文件并生成独立密钥，然后停下来等待填写：

```bash
sudoedit /etc/tgforward/tgforward.env
```

填写以下四项，保留已经生成的 `MASTER_KEY` 和 `SALT_KEY`：

| 配置 | 说明 |
| --- | --- |
| `API_ID`、`API_HASH` | 从 [my.telegram.org](https://my.telegram.org) 获取 |
| `BOT_TOKEN` | 在 [@BotFather](https://t.me/BotFather) 创建机器人后获取 |
| `OWNER_ID` | 管理员的 Telegram 数字 ID；多个 ID 用空格分隔 |

再次执行即可构建并启动：

```bash
sudo bash scripts/deploy.sh init
```

已有用户数据的实例请先完成[数据导入](docs/data-import.md)，不要按新实例初始化空库。

### 可选设置

同一配置文件还可以设置：

| 配置 | 说明 |
| --- | --- |
| `STRING`、`LOG_GROUP` | Premium 账号的 session string 和中转频道 ID，两项配合启用大文件上传 |
| `BATCH_DELAY` | 批量提取的间隔秒数，默认 `3` |
| `MAX_CONCURRENT_TRANSFERS` | 同时进行的传输数量，默认 `3` |
| `USER_COOLDOWN` | 两次提取之间的最小间隔秒数，默认 `3` |
| `TASK_STALL_TIMEOUT` | 无进度任务的超时秒数，默认 `300` |

### 更新与日常管理

```bash
git pull --ff-only
sudo bash scripts/deploy.sh
```

更新只重建应用并等待启动完成，不重新生成密钥、不初始化空数据库，也不改写你的源码。

```bash
sudo docker compose logs --tail=50 bot   # 查看运行情况
sudo docker compose restart bot        # 重启
sudo docker compose down               # 停止
```

配置和用户数据不放在 Git 仓库中：

| 位置 | 内容 |
| --- | --- |
| `/etc/tgforward/tgforward.env` | 账号配置和加密密钥 |
| `/var/lib/tgforward/state/tgforward.sqlite3` | 用户账号、设置和历史记录 |
| `/var/lib/tgforward/thumbs/` | 自定义封面 |

容器重建、源码更新或 `docker compose down -v` 不会删除这些宿主目录。
**不要随意更换密钥**，否则已保存的登录凭据将无法解密。
需要备份时先停止机器人，再一起保存配置文件和整个 `/var/lib/tgforward` 目录。

## 项目结构

```text
tgforward/
├── __main__.py       应用入口
├── config.py         配置
├── handlers/         命令与按钮
├── ui/               菜单与交互
├── transfers/        消息提取与文件传输
├── comments/         评论区读取
├── telegram/         Telegram 连接与媒体适配
├── storage/          用户数据与凭据
├── runtime/          任务管理与运行状态
├── utils/            文本、链接与媒体工具
└── tools/            数据导入与维护工具
requirements/         依赖及锁定版本
tests/                按功能分类的测试
scripts/deploy.sh     部署入口
```

开发与贡献请参阅 [CONTRIBUTING.md](CONTRIBUTING.md)。
