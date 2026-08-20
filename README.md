# TNC Chatbot

AI Chatbot cho guild Discord TNC (Albion Online). Tách từ repo chính [Bot_Albion_TNC](https://github.com/your-repo/Bot_Albion_TNC) để deploy riêng trên Render, giảm tải cho bot gốc.

## Features

- **AI Chat** — Failover chain 8 step (Ollama → Gemini → OpenRouter)
- **Vision support** — Xử lý ảnh attachments
- **Channel summarization** — Tóm tắt lịch sử channel
- **RAG Library** — Scan & retrieval từ channel library
- **Auto item lookup** — Trả lời câu hỏi về item Albion
- **Wiki search** — DuckDuckGo search Albion Wiki
- **Learning/Memory** — `/remember`, `/teach`, `/recall`

## Deploy

### Render
1. Clone repo này
2. Tạo Discord bot mới trên [Developer Portal](https://discord.com/developers/applications)
3. Copy `.env.example` → `.env`, điền env vars
4. Deploy trên Render: Python 3.11, port 5000
5. Start command: `python -m bot.main`

### Env Vars
| Var | Mô tả |
|-----|-------|
| `DISCORD_TOKEN` | Token bot mới (KHÔNG dùng chung main bot) |
| `DISCORD_GUILD_ID` | Guild ID (giống main bot) |
| `SUPABASE_URL` | Supabase URL (chung main bot) |
| `SUPABASE_SERVICE_ROLE_KEY` | Supabase service role key |
| `OPENROUTER_API_KEY` | OpenRouter API key |
| `GEMINI_API_KEY` | Google Gemini API key |
| `HEARTBEAT_STATUS_KEY` | `tnc_chatbot_status.json` (khác main bot) |

## Core Files

Copy từ [Bot_Albion_TNC](https://github.com/your-repo/Bot_Albion_TNC) — commit `HEAD` ngày 2026-08-20.

Khi update core/ ở repo gốc, cần sync sang repo này:
- [ ] `bot/core/config.py`
- [ ] `bot/core/db.py`
- [ ] `bot/core/storage.py`
- [ ] `bot/core/config_store.py`
- [ ] `bot/core/webserver.py`
- [ ] `bot/core/system_logger.py`
- [ ] `bot/core/permissions.py`
