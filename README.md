# Discord Music Bot

以下為LLM協助撰寫

The following was written with the assistance of LLM.

一個以播放穩定性為優先設計的 Discord 音樂機器人。透過較長的載入時間，換取更流暢、穩定的播放體驗。

A Discord music bot designed with playback stability as its primary goal. It trades longer loading times for smoother and more consistent playback.

主要特色 | Features

- 穩定流暢的播放體驗：盡可能減少音樂播放過程中的卡頓與中斷。

- 一致的播放速度：著重維持穩定的播放速度，避免音樂忽快忽慢。

- 音樂佇列管理：支援新增歌曲、查看佇列、跳過歌曲及清空佇列。

- 互動式控制面板：透過 Discord 按鈕控制播放、暫停、繼續及停止。

- 背景播放管理：支援背景播放任務與語音頻道連線管理。

- Stable and smooth playback: Minimizes stuttering and interruptions during playback.

- Consistent playback speed: Aims to prevent sudden changes in playback speed.

- Music queue management: Supports adding tracks, viewing the queue, skipping tracks, and clearing the queue.

- Interactive control panel: Provides Discord buttons for playback, pause, resume, and stop controls.

- Background playback management: Manages playback tasks and voice channel connections.

設計取捨 | Trade-off

本專案優先考慮播放穩定性，因此音樂開始播放前可能需要較長的載入時間。若點播後沒有立即播放，請稍候片刻，避免重複提交相同的歌曲。

This project prioritizes playback stability, so tracks may take longer to load before playback begins. If a track does not start immediately, please wait a little longer before submitting the same request again.

系統需求 | Requirements

- Python 3.10 或更新版本（建議使用與相依套件相容的版本）。

- Discord Bot Token。

- 已啟用語音功能的 Discord 伺服器。

- FFmpeg，以及專案所使用的 Python 套件。

- Python 3.10 or later (use a version compatible with the project dependencies).

- A Discord Bot Token.

- A Discord server with voice channel support.

- FFmpeg and the Python packages used by the project.

安裝方式 | Installation

1. 下載專案 | Clone the repository

2. 建立虛擬環境 | Create a virtual environment

python -m venv .venv

啟用虛擬環境：

Activate the virtual environment:

Windows

.venv\Scripts\activate

Linux / macOS

source .venv/bin/activate

3. 安裝相依套件 | Install dependencies

pip install -r requirements.txt

請確認系統已安裝 FFmpeg，並且能從命令列正常執行。

Make sure FFmpeg is installed and accessible from your command line.

4. 設定環境變數 | Configure environment variables

在專案根目錄建立 ".env" 檔案，並填入機器人 Token：

Create a ".env" file in the project root and add your bot token:

DISCORD_TOKEN=YOUR_DISCORD_BOT_TOKEN

請勿將 ".env" 或任何包含 Token 的檔案提交至公開儲存庫。

Never commit ".env" or any file containing your bot token to a public repository.

5. 啟動機器人 | Run the bot

python bot.py

指令與控制 | Commands and Controls

以下為常見功能概述。實際可用指令與按鈕請以 "bot.py" 中的實作為準。

The following is a general overview. Refer to "bot.py" for the exact commands and available controls.

功能（中文）| Feature (English)| 說明
點播歌曲| Play a track| 提交歌曲並加入播放佇列。
查看佇列| View queue| 查看目前等待播放的歌曲。
跳過歌曲| Skip track| 跳過目前播放的歌曲。
清空佇列| Clear queue| 清除等待播放的歌曲。
暫停／繼續| Pause / Resume| 暫停或繼續目前的播放。
停止播放| Stop playback| 停止播放並依程式實作處理佇列。

專案檔案 | Project Files

.
├── bot.py
├── requirements.txt
├── .env
├── .gitignore
└── README.md

- "bot.py"：機器人的主要程式。

- "requirements.txt"：Python 相依套件清單。

- ".env"：存放機器人 Token 等環境設定。

- ".gitignore"：指定不應提交至 Git 的檔案。

- "README.md"：專案說明文件。

- "bot.py": Main bot implementation.

- "requirements.txt": Python dependency list.

- ".env": Stores the bot token and other environment settings.

- ".gitignore": Lists files that should not be committed to Git.

- "README.md": Project documentation.

注意事項 | Notes

- 請妥善保管 Discord Bot Token，不要將其公開。

- 播放開始前可能需要較長的載入時間，這是本專案目前的主要設計取捨。

- 若要調整播放行為、音源處理或語音傳輸方式，請先確認修改不會影響播放穩定性。

- 請遵守音源平台的使用條款，以及所在地區適用的著作權規定。

- Keep your Discord bot token private.

- Longer loading times are the primary trade-off of the current design.

- When modifying playback, audio source handling, or voice transmission, verify that the changes do not compromise playback stability.

- Follow the terms of service of audio platforms and applicable copyright laws.

授權 | License

本專案採用 GNU General Public License v3.0（GPL-3.0）授權。你可以自由使用、研究、修改及散布本專案，但散布程式或其衍生作品時，必須遵守 GPL-3.0 的相關條款。

This project is licensed under the GNU General Public License v3.0 (GPL-3.0). You are free to use, study, modify, and distribute this project, subject to the terms of the GPL-3.0 when distributing the program or derivative works.
