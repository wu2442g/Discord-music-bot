import asyncio
import json
import os
import queue
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from types import MethodType
from typing import Optional, List
from dotenv import dotenv_values, load_dotenv
import discord
from discord.oggparse import OggStream
from discord import app_commands, Interaction, ButtonStyle
from discord.ext import commands
from discord.ui import View, Button
import yt_dlp

# Configuration
ENV_FILE = Path(__file__).resolve().with_name(".env")
load_dotenv(dotenv_path=ENV_FILE)
TOKEN = (
    os.getenv("DISCORD_TOKEN", "").strip()
    or (dotenv_values(ENV_FILE).get("DISCORD_TOKEN") or "").strip()
)
CACHE_DIR = Path(os.getenv("MUSIC_CACHE_DIR", "./cache")).expanduser()
MUSIC_AUDIO_MODE = os.getenv("MUSIC_AUDIO_MODE", "opus").strip().lower()
STATE_FILE = Path("./music_bot_state.json")
MAX_DURATION = 300 * 60  # Maximum accepted track duration in seconds.
MAX_QUEUE_SIZE = 100
MAX_PLAYLIST_SIZE = 50  # Maximum number of tracks imported from one playlist.
EMPTY_VOICE_TIMEOUT = 24 * 60 * 60
AUDIO_BUFFER_FRAMES = 25
AUDIO_BUFFER_PRIME_FRAMES = 25
AUDIO_BUFFER_PRIME_TIMEOUT = 2.0
SONG_TRANSITION_PRELOAD_SECONDS = 0.5


if MUSIC_AUDIO_MODE not in {"pcm", "opus"}:
    raise ValueError("MUSIC_AUDIO_MODE must be either 'pcm' or 'opus'")

CACHE_DIR.mkdir(parents=True, exist_ok=True)

# Remove stale cache artifacts left by an interrupted run.
for cache_file in CACHE_DIR.iterdir():
    if cache_file.is_file():
        try:
            cache_file.unlink()
        except OSError as error:
            print(f"Could not remove stale cache file {cache_file}: {error}")


@dataclass
class Song:
    title: str
    url: str
    duration: int
    requester: discord.Member
    file_path: Optional[Path] = None
    download_task: Optional[asyncio.Task] = None
    progress: float = 0.0  # Download progress percentage.
    discarded: bool = False


class PrefetchedAudioSource(discord.AudioSource):
    def __init__(self, source: discord.AudioSource, first_frame: bytes):
        self.source = source
        self._frames: queue.Queue[bytes] = queue.Queue(maxsize=AUDIO_BUFFER_FRAMES)
        self._frames.put_nowait(first_frame)
        self._stop = threading.Event()
        self._finished = threading.Event()
        self._primed = threading.Event()
        self._current_error: Optional[Exception] = None
        self.buffer_wait_count = 0
        self.buffer_wait_over_10ms_count = 0
        self.buffer_wait_total_seconds = 0.0
        self.buffer_wait_max_seconds = 0.0
        self.read_gap_over_22ms_count = 0
        self.read_gap_over_30ms_count = 0
        self.read_gap_max_seconds = 0.0
        self._last_read_at: Optional[float] = None
        self._reader_thread = threading.Thread(
            target=self._read_ahead,
            name="audio-source-reader",
            daemon=True,
        )
        self._reader_thread.start()

    def _read_ahead(self):
        try:
            while not self._stop.is_set():
                frame = self.source.read()
                if not frame:
                    break

                while not self._stop.is_set():
                    try:
                        self._frames.put(frame, timeout=0.1)
                        break
                    except queue.Full:
                        continue

                if self._frames.qsize() >= AUDIO_BUFFER_PRIME_FRAMES:
                    self._primed.set()
        except Exception as error:
            if not self._stop.is_set():
                self._current_error = error
        finally:
            self._finished.set()
            self._primed.set()

    def wait_until_primed(self) -> bool:
        self._primed.wait(AUDIO_BUFFER_PRIME_TIMEOUT)
        return self._frames.qsize() >= AUDIO_BUFFER_PRIME_FRAMES or self._finished.is_set()

    def read(self) -> bytes:
        now = time.perf_counter()
        if self._last_read_at is not None:
            read_gap = now - self._last_read_at
            self.read_gap_max_seconds = max(self.read_gap_max_seconds, read_gap)
            if read_gap > 0.022:
                self.read_gap_over_22ms_count += 1
            if read_gap > 0.030:
                self.read_gap_over_30ms_count += 1
        self._last_read_at = now

        while True:
            try:
                return self._frames.get_nowait()
            except queue.Empty:
                started = time.perf_counter()
                try:
                    frame = self._frames.get(timeout=0.1)
                except queue.Empty:
                    if self._finished.is_set() and self._current_error is not None:
                        raise self._current_error
                    if self._finished.is_set() or self._stop.is_set():
                        return b""
                    waited = time.perf_counter() - started
                    self._record_buffer_wait(waited)
                    continue
                waited = time.perf_counter() - started
                self._record_buffer_wait(waited)
                return frame

    def _record_buffer_wait(self, waited: float):
        self.buffer_wait_total_seconds += waited
        self.buffer_wait_max_seconds = max(self.buffer_wait_max_seconds, waited)
        if waited > 0.002:
            self.buffer_wait_count += 1
        if waited > 0.010:
            self.buffer_wait_over_10ms_count += 1

    def is_opus(self) -> bool:
        return self.source.is_opus()

    def cleanup(self):
        self._stop.set()
        self.source.cleanup()
        if self._reader_thread is not threading.current_thread():
            self._reader_thread.join(timeout=1.0)


class CachedOpusAudioSource(discord.AudioSource):
    def __init__(self, file_path: Path):
        self._file = file_path.open("rb")

    def read(self) -> bytes:
        packet_length = self._file.read(4)
        if not packet_length:
            return b""
        if len(packet_length) != 4:
            raise RuntimeError("Cached Opus packet length is truncated")

        size = int.from_bytes(packet_length, byteorder="big")
        if size == 0:
            raise RuntimeError("Cached Opus packet has an invalid zero length")

        packet = self._file.read(size)
        if len(packet) != size:
            raise RuntimeError("Cached Opus packet data is truncated")
        return packet

    def is_opus(self) -> bool:
        return True

    def cleanup(self):
        self._file.close()


class GuildMusicPlayer:
    def __init__(self, bot: commands.Bot, guild: discord.Guild):
        self.bot = bot
        self.guild = guild
        self.queue: asyncio.Queue[Song] = asyncio.Queue()
        self.current: Optional[Song] = None
        self.voice_client: Optional[discord.VoiceClient] = None
        self.lock = asyncio.Lock()
        self.playing = False
        self.loop_mode = 0  # 0=off, 1=track, 2=queue
        self.volume = 0.5
        self.control_message: Optional[discord.Message] = None
        self.music_channel_id: Optional[int] = None
        self._download_sem = asyncio.Semaphore(1)  # Limit concurrent downloads to reduce contention.
        self.keep_voice = False
        self._empty_voice_task: Optional[asyncio.Task] = None
        self._play_generation = 0
        self._play_task: Optional[asyncio.Task] = None

    async def enqueue(self, song: Song) -> bool:
        async with self.lock:
            if self.queue.qsize() >= MAX_QUEUE_SIZE:
                return False
            await self.queue.put(song)
            if not self.playing:
                if not self._play_task or self._play_task.done():
                    self._play_task = asyncio.create_task(self.play_loop())
            else:
                self._start_preload()
            return True

    async def enqueue_many(self, songs: List[Song]) -> int:
        added = 0
        for s in songs:
            if await self.enqueue(s):
                added += 1
            else:
                break
        return added

    def _start_preload(self):
        # Preload the next track while the current track is playing.
        asyncio.create_task(self._preload_next())

    def _start_download(self, song: Song, slow: bool = False):
        task = asyncio.create_task(self._download(song, slow=slow))
        song.download_task = task

        def report_unhandled_error(done_task: asyncio.Task):
            if done_task.cancelled():
                return
            error = done_task.exception()
            if error:
                print(f"Download failed for {song.title}: {error}")

        task.add_done_callback(report_unhandled_error)
        return task

    async def _preload_next(self):
        if self.queue.empty():
            return
        next_song = self.queue._queue[0]
        if (
            not next_song.discarded
            and next_song.file_path is None
            and next_song.download_task is None
        ):
            self._start_download(next_song, slow=True)

    @staticmethod
    def _remove_cache_file(path: Optional[Path]):
        if path is None:
            return
        try:
            path.unlink(missing_ok=True)
        except OSError as error:
            print(f"Could not remove cache file {path}: {error}")

    @classmethod
    def _remove_partial_download(cls, file_id: str):
        for path in CACHE_DIR.glob(f"{file_id}.*"):
            cls._remove_cache_file(path)

    @staticmethod
    def _prepare_opus_file(source_path: Path, ogg_path: Path, output_path: Path) -> Path:
        probe = subprocess.run(
            [
                "ffprobe",
                "-v", "error",
                "-select_streams", "a:0",
                "-show_entries", "stream=codec_name",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(source_path),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if probe.returncode != 0:
            raise RuntimeError(f"FFprobe could not inspect audio: {probe.stderr.strip()}")

        codec_name = probe.stdout.strip().splitlines()
        if not codec_name:
            raise RuntimeError("Downloaded media contains no audio stream")

        command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "error",
            "-y",
            "-i", str(source_path),
            "-map", "0:a:0",
            "-map_metadata", "-1",
            "-vn",
        ]
        if codec_name[0] == "opus":
            command.extend(["-c:a", "copy"])
        else:
            command.extend([
                "-c:a", "libopus",
                "-b:a", "128k",
                "-vbr", "on",
                "-application", "audio",
                "-ar", "48000",
                "-ac", "2",
            ])
        command.extend(["-f", "ogg", str(ogg_path)])

        conversion = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
        )
        if conversion.returncode != 0:
            raise RuntimeError(f"FFmpeg Opus preparation failed: {conversion.stderr.strip()}")
        if not ogg_path.is_file() or ogg_path.stat().st_size == 0:
            raise RuntimeError("FFmpeg produced an empty Ogg Opus file")

        packet_count = 0
        try:
            with ogg_path.open("rb") as ogg_file, output_path.open("wb") as packet_file:
                for packet in OggStream(ogg_file).iter_packets():
                    if packet.startswith((b"OpusHead", b"OpusTags")):
                        continue
                    if not packet:
                        raise RuntimeError("Ogg stream contains an empty Opus packet")
                    packet_file.write(len(packet).to_bytes(4, byteorder="big"))
                    packet_file.write(packet)
                    packet_count += 1
        finally:
            ogg_path.unlink(missing_ok=True)

        if packet_count == 0 or not output_path.is_file() or output_path.stat().st_size == 0:
            output_path.unlink(missing_ok=True)
            raise RuntimeError("FFmpeg produced no Opus audio packets")
        return output_path

    def _discard_song(self, song: Song):
        song.discarded = True
        self._remove_cache_file(song.file_path)
        song.file_path = None

    async def _create_audio_source(self, song: Song) -> discord.AudioSource:
        if song.file_path is None:
            raise RuntimeError(f"No audio file available for {song.title}")

        if MUSIC_AUDIO_MODE == "opus":
            ffmpeg_source = CachedOpusAudioSource(song.file_path)
        else:
            ffmpeg_source = discord.FFmpegPCMAudio(
                str(song.file_path),
                options="-vn -ar 48000 -ac 2 -af aresample=48000",
            )
        first_frame_task = asyncio.create_task(asyncio.to_thread(ffmpeg_source.read))
        try:
            first_frame = await asyncio.shield(first_frame_task)
        except asyncio.CancelledError:
            try:
                await first_frame_task
            finally:
                ffmpeg_source.cleanup()
            raise
        except Exception:
            ffmpeg_source.cleanup()
            raise

        if not first_frame:
            ffmpeg_source.cleanup()
            raise RuntimeError(f"Audio source returned no packets for {song.title}")

        buffered_source = PrefetchedAudioSource(ffmpeg_source, first_frame)
        try:
            await asyncio.to_thread(buffered_source.wait_until_primed)
        except asyncio.CancelledError:
            buffered_source.cleanup()
            raise
        except Exception:
            buffered_source.cleanup()
            raise
        if MUSIC_AUDIO_MODE == "opus":
            return buffered_source
        return discord.PCMVolumeTransformer(buffered_source, volume=self.volume)

    async def _download(self, song: Song, slow: bool = False) -> Path:
        async with self._download_sem:
            if song.discarded:
                raise asyncio.CancelledError

            file_id = str(uuid.uuid4())
            outtmpl = str(CACHE_DIR / f"{file_id}.%(ext)s")

            def progress_hook(d):
                if d["status"] == "downloading":
                    total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                    if total > 0:
                        song.progress = (d.get("downloaded_bytes", 0) / total) * 100
                elif d["status"] == "finished":
                    song.progress = 99.0

            ydl_opts = {
                "format": "bestaudio/best",
                "outtmpl": outtmpl,
                "quiet": True,
                "no_warnings": True,
                "noplaylist": True,
                "progress_hooks": [progress_hook],
                "concurrent_fragment_downloads": 1 if slow else 3,
            }

            def _do_download():
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(song.url, download=True)
                    downloaded_path = Path(ydl.prepare_filename(info))
                if MUSIC_AUDIO_MODE != "opus":
                    return downloaded_path

                ogg_path = CACHE_DIR / f"{file_id}.opus.ogg"
                opus_path = CACHE_DIR / f"{file_id}.opus.frames"
                prepared_path = self._prepare_opus_file(
                    downloaded_path,
                    ogg_path,
                    opus_path,
                )
                self._remove_cache_file(downloaded_path)
                return prepared_path

            worker = asyncio.create_task(asyncio.to_thread(_do_download))
            try:
                path = await asyncio.shield(worker)
            except asyncio.CancelledError:
                try:
                    path = await worker
                except Exception:
                    self._remove_partial_download(file_id)
                else:
                    self._remove_cache_file(path)
                    self._remove_partial_download(file_id)
                raise
            except Exception:
                self._remove_partial_download(file_id)
                raise

            if song.discarded:
                self._remove_cache_file(path)
                self._remove_partial_download(file_id)
                raise asyncio.CancelledError

            song.file_path = path
            song.progress = 100.0
            return path

    async def play_loop(self):
        async with self.lock:
            if self.playing:
                return
            self.playing = True
            generation = self._play_generation

        has_started_track = False
        while generation == self._play_generation:
            try:
                song = await asyncio.wait_for(self.queue.get(), timeout=300)
            except asyncio.TimeoutError:
                if generation != self._play_generation:
                    break
                if self.keep_voice and self.voice_client and self.voice_client.is_connected():
                    continue
                break

            self.current = song

            # Wait until the track is available locally before playback.
            if song.file_path is None:
                if song.download_task is None:
                    self._start_download(song, slow=False)
                try:
                    await asyncio.shield(song.download_task)
                except asyncio.CancelledError:
                    if generation != self._play_generation or song.discarded:
                        break
                    raise
                except Exception as error:
                    print(f"Could not prepare {song.title}: {error}")
                    self._discard_song(song)
                    self.current = None
                    await self.update_control_panel()
                    continue
            if generation != self._play_generation:
                break
            if song.discarded:
                self.current = None
                continue

            if not song.file_path or not song.file_path.exists():
                self._discard_song(song)
                self.current = None
                await self.update_control_panel()
                continue

            # Connect to the requesters voice channel when needed.
            if not self.voice_client or not self.voice_client.is_connected():
                if song.requester.voice and song.requester.voice.channel:
                    try:
                        self.voice_client = await song.requester.voice.channel.connect()
                    except (discord.ClientException, discord.HTTPException) as error:
                        print(f"Could not connect for {song.title}: {error}")
                        self._discard_song(song)
                        self.current = None
                        await self.update_control_panel()
                        continue
                else:
                    self._discard_song(song)
                    self.current = None
                    await self.update_control_panel()
                    continue

            try:
                source = await self._create_audio_source(song)
            except asyncio.CancelledError:
                if generation != self._play_generation or song.discarded:
                    break
                raise
            except Exception as error:
                print(f"Could not initialize audio for {song.title}: {error}")
                self._discard_song(song)
                self.current = None
                await self.update_control_panel()
                continue

            if has_started_track:
                try:
                    await asyncio.sleep(SONG_TRANSITION_PRELOAD_SECONDS)
                except asyncio.CancelledError:
                    source.cleanup()
                    raise
                if generation != self._play_generation:
                    source.cleanup()
                    break

            active_voice_client = self.voice_client
            original_send_audio_packet = active_voice_client.send_audio_packet
            send_audio_packet_impl = type(active_voice_client).send_audio_packet
            packet_send_stats: dict[str, float] = {
                "over_5ms": 0,
                "over_10ms": 0,
                "max_ms": 0.0,
            }

            def tracked_send_audio_packet(
                _voice_client: discord.VoiceClient,
                data: bytes,
                *,
                encode: bool = True,
            ):
                started = time.perf_counter()
                try:
                    send_audio_packet_impl(
                        _voice_client,
                        data,
                        encode=encode,
                    )
                finally:
                    elapsed_ms = (time.perf_counter() - started) * 1000
                    packet_send_stats["max_ms"] = max(
                        packet_send_stats["max_ms"],
                        elapsed_ms,
                    )
                    if elapsed_ms > 5:
                        packet_send_stats["over_5ms"] += 1
                    if elapsed_ms > 10:
                        packet_send_stats["over_10ms"] += 1

            active_voice_client.send_audio_packet = MethodType(
                tracked_send_audio_packet,
                active_voice_client,
            )

            def restore_packet_sender():
                active_voice_client.send_audio_packet = original_send_audio_packet

            def after_callback(error):
                restore_packet_sender()
                if error:
                    print(f"Playback error: {error}")
                buffered_source = (
                    source.original
                    if isinstance(source, discord.PCMVolumeTransformer)
                    else source
                )
                if isinstance(buffered_source, PrefetchedAudioSource):
                    print(
                        f"Audio buffer stats for {song.title}: "
                        f"buffer_waits_over_2ms={buffered_source.buffer_wait_count}, "
                        f"buffer_waits_over_10ms={buffered_source.buffer_wait_over_10ms_count}, "
                        f"buffer_wait_total_ms={buffered_source.buffer_wait_total_seconds * 1000:.1f}, "
                        f"buffer_wait_max_ms={buffered_source.buffer_wait_max_seconds * 1000:.1f}, "
                        f"read_gaps_over_22ms={buffered_source.read_gap_over_22ms_count}, "
                        f"read_gaps_over_30ms={buffered_source.read_gap_over_30ms_count}, "
                        f"read_gap_max_ms={buffered_source.read_gap_max_seconds * 1000:.1f}, "
                        f"packet_sends_over_5ms={packet_send_stats['over_5ms']}, "
                        f"packet_sends_over_10ms={packet_send_stats['over_10ms']}, "
                        f"packet_send_max_ms={packet_send_stats['max_ms']:.1f}"
                    )

            try:
                active_voice_client.play(source, after=after_callback)
            except (discord.ClientException, discord.HTTPException) as error:
                restore_packet_sender()
                source.cleanup()
                print(f"Could not start playback for {song.title}: {error}")
                self._discard_song(song)
                self.current = None
                await self.update_control_panel()
                continue
            has_started_track = True
            self._start_preload()
            await self.update_control_panel()

            # Keep the playback task alive until the active source finishes.
            while (
                generation == self._play_generation
                and self.voice_client
                and (self.voice_client.is_playing() or self.voice_client.is_paused())
            ):
                await asyncio.sleep(0.5)

            if generation != self._play_generation or self.current is not song:
                continue

            # Apply the configured repeat mode after playback.
            if self.loop_mode == 1:  # Repeat the current track.
                await self.queue.put(song)
            elif self.loop_mode == 2:  # 清單循環
                await self.queue.put(song)
            else:
                self._remove_cache_file(song.file_path)
                song.file_path = None

            self.current = None
            await self.update_control_panel()

        if generation == self._play_generation:
            self.playing = False
            if self.voice_client:
                await self.voice_client.disconnect()
                self.voice_client = None

    def _cancel_empty_voice_timeout(self):
        if self._empty_voice_task and not self._empty_voice_task.done():
            self._empty_voice_task.cancel()
        self._empty_voice_task = None

    def update_empty_voice_timer(self):
        voice_client = self.voice_client
        if not self.keep_voice or not voice_client or not voice_client.is_connected():
            self._cancel_empty_voice_timeout()
            return

        if any(not member.bot for member in voice_client.channel.members):
            self._cancel_empty_voice_timeout()
            return

        if not self._empty_voice_task or self._empty_voice_task.done():
            self._empty_voice_task = asyncio.create_task(self._leave_after_empty_timeout())

    async def _leave_after_empty_timeout(self):
        try:
            await asyncio.sleep(EMPTY_VOICE_TIMEOUT)
            voice_client = self.voice_client
            if (
                self.keep_voice
                and voice_client
                and voice_client.is_connected()
                and not any(not member.bot for member in voice_client.channel.members)
            ):
                self._empty_voice_task = None
                await self.stop()
        except asyncio.CancelledError:
            raise
        finally:
            if self._empty_voice_task is asyncio.current_task():
                self._empty_voice_task = None

    async def skip(self):
        if self.voice_client and self.voice_client.is_playing():
            self.voice_client.stop()

    async def stop(self):
        self.keep_voice = False
        self._cancel_empty_voice_timeout()
        self._play_generation += 1
        songs_to_discard = []
        if self.current:
            songs_to_discard.append(self.current)
        songs_to_discard.extend(self.queue._queue)
        for song in songs_to_discard:
            song.discarded = True

        play_task = self._play_task
        if play_task and play_task is not asyncio.current_task() and not play_task.done():
            play_task.cancel()
            try:
                await play_task
            except asyncio.CancelledError:
                pass
        self._play_task = None
        async with self.lock:
            while not self.queue.empty():
                self.queue.get_nowait()
            if self.voice_client:
                self.voice_client.stop()
                await self.voice_client.disconnect()
                self.voice_client = None
            for song in songs_to_discard:
                self._remove_cache_file(song.file_path)
                song.file_path = None
            self.playing = False
            self.current = None
            await self.update_control_panel()

    async def set_volume(self, delta: float) -> bool:
        if MUSIC_AUDIO_MODE == "opus":
            return False

        self.volume = max(0.0, min(1.0, self.volume + delta))
        if (
            self.voice_client
            and isinstance(self.voice_client.source, discord.PCMVolumeTransformer)
        ):
            self.voice_client.source.volume = self.volume
        await self.update_control_panel()
        return True

    async def toggle_pause(self):
        if not self.voice_client:
            return
        if self.voice_client.is_paused():
            self.voice_client.resume()
        else:
            self.voice_client.pause()
        await self.update_control_panel()

    async def seek(self, seconds: int):
        # A seek implementation can rebuild the source from the requested offset.
        pass

    async def update_control_panel(self):
        if not self.control_message:
            return
        embed = self.build_embed()
        try:
            await self.control_message.edit(embed=embed, view=ControlView(self))
        except Exception:
            pass

    def build_embed(self) -> discord.Embed:
        embed = discord.Embed(title="Music Control Panel", color=0x5865F2)
        if self.current:
            progress = f"{self.current.progress:.0f}%" if self.current.progress < 100 else "已就緒"
            embed.add_field(
                name="Now Playing",
                value=f"**{self.current.title}**\nLoading progress: {progress}",
                inline=False
            )
        else:
            embed.add_field(name="Now Playing", value="Nothing is playing.", inline=False)

        qsize = self.queue.qsize()
        embed.add_field(name="Queue", value=f"{qsize} tracks", inline=True)
        volume_text = (
            f"{int(self.volume * 100)}%"
            if MUSIC_AUDIO_MODE == "pcm"
            else "Opus 直送（不可調整）"
        )
        embed.add_field(name="Volume", value=volume_text, inline=True)
        loop_text = ["關閉", "單曲循環", "清單循環"][self.loop_mode]
        embed.add_field(name="Repeat", value=loop_text, inline=True)
        return embed


# Playback controls
class ControlView(View):
    def __init__(self, player: GuildMusicPlayer):
        super().__init__(timeout=None)
        self.player = player

    @discord.ui.button(label="隨機播放", style=ButtonStyle.secondary, row=0, custom_id="music:shuffle")
    async def shuffle(self, interaction: Interaction, button: Button):
        await interaction.response.defer()
        # Shuffle the queued tracks while preserving the current track.
        items = list(self.player.queue._queue)
        import random
        random.shuffle(items)
        self.player.queue = asyncio.Queue()
        for s in items:
            await self.player.queue.put(s)
        await self.player.update_control_panel()

    @discord.ui.button(label="-10%", style=ButtonStyle.secondary, row=0, custom_id="music:volume_down")
    async def vol_down(self, interaction: Interaction, button: Button):
        await interaction.response.defer(ephemeral=True)
        if not await self.player.set_volume(-0.1):
            await interaction.followup.send("Opus 直送模式下無法即時調整音量。", ephemeral=True)

    @discord.ui.button(label="暫停", style=ButtonStyle.primary, row=0, custom_id="music:pause")
    async def pause(self, interaction: Interaction, button: Button):
        await interaction.response.defer()
        await self.player.toggle_pause()

    @discord.ui.button(label="+10%", style=ButtonStyle.secondary, row=0, custom_id="music:volume_up")
    async def vol_up(self, interaction: Interaction, button: Button):
        await interaction.response.defer(ephemeral=True)
        if not await self.player.set_volume(0.1):
            await interaction.followup.send("Opus 直送模式下無法即時調整音量。", ephemeral=True)

    @discord.ui.button(label="上一首", style=ButtonStyle.secondary, row=1, custom_id="music:previous")
    async def prev(self, interaction: Interaction, button: Button):
        await interaction.response.send_message("上一首功能開發中", ephemeral=True)

    @discord.ui.button(label="-10秒", style=ButtonStyle.secondary, row=1, custom_id="music:seek_back")
    async def seek_back(self, interaction: Interaction, button: Button):
        await interaction.response.send_message("快退功能開發中", ephemeral=True)

    @discord.ui.button(label="重複模式", style=ButtonStyle.secondary, row=1, custom_id="music:loop")
    async def loop(self, interaction: Interaction, button: Button):
        await interaction.response.defer()
        self.player.loop_mode = (self.player.loop_mode + 1) % 3
        await self.player.update_control_panel()

    @discord.ui.button(label="+10秒", style=ButtonStyle.secondary, row=1, custom_id="music:seek_forward")
    async def seek_fwd(self, interaction: Interaction, button: Button):
        await interaction.response.send_message("快進功能開發中", ephemeral=True)

    @discord.ui.button(label="下一首", style=ButtonStyle.secondary, row=1, custom_id="music:next")
    async def next(self, interaction: Interaction, button: Button):
        await interaction.response.defer()
        await self.player.skip()

    @discord.ui.button(label="面板更新", style=ButtonStyle.secondary, row=2, custom_id="music:refresh")
    async def refresh(self, interaction: Interaction, button: Button):
        await interaction.response.defer()
        await self.player.update_control_panel()

    @discord.ui.button(label="待播清單", style=ButtonStyle.secondary, row=2, custom_id="music:queue")
    async def show_queue(self, interaction: Interaction, button: Button):
        q = list(self.player.queue._queue)[:15]
        if not q and not self.player.current:
            return await interaction.response.send_message("佇列是空的", ephemeral=True)
        text = ""
        if self.player.current:
            text += f"**正在播放**：{self.player.current.title}\n\n"
        for i, s in enumerate(q, 1):
            text += f"{i}. {s.title}\n"
        await interaction.response.send_message(text or "空的", ephemeral=True)

    @discord.ui.button(label="停止播放", style=ButtonStyle.danger, row=2, custom_id="music:stop")
    async def stop(self, interaction: Interaction, button: Button):
        await interaction.response.defer()
        await self.player.stop()


class MusicBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.voice_states = True
        super().__init__(command_prefix="!", intents=intents)
        self.players: dict[int, GuildMusicPlayer] = {}
        self._panels_restored = False
        print(f"Audio output mode: {MUSIC_AUDIO_MODE}")
        try:
            self._music_state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except FileNotFoundError:
            self._music_state = {}
        except (json.JSONDecodeError, OSError) as error:
            print(f"Music state could not be loaded from {STATE_FILE}: {error}")
            self._music_state = {}

    async def setup_hook(self):
        await self.tree.sync()
        print("Slash commands synced.")

    def save_panel_state(self, guild_id: int, channel_id: int, message_id: int):
        self._music_state[str(guild_id)] = {
            "channel_id": channel_id,
            "message_id": message_id,
        }
        try:
            STATE_FILE.write_text(
                json.dumps(self._music_state, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as error:
            print(f"Music panel state could not be saved to {STATE_FILE}: {error}")

    async def restore_music_panels(self):
        if self._panels_restored:
            return
        self._panels_restored = True

        for guild in self.guilds:
            player = self.get_player(guild)
            saved = self._music_state.get(str(guild.id), {})
            channel = guild.get_channel(saved.get("channel_id"))
            if channel is None and saved.get("channel_id"):
                try:
                    channel = await self.fetch_channel(saved["channel_id"])
                except discord.HTTPException as error:
                    print(f"Could not restore music channel in guild {guild.id}: {error}")
            if channel is None:
                channel = discord.utils.get(guild.text_channels, name="music-requests")

            message = None
            if isinstance(channel, discord.TextChannel):
                player.music_channel_id = channel.id
                message_id = saved.get("message_id")
                if message_id:
                    try:
                        message = await channel.fetch_message(message_id)
                    except discord.HTTPException as error:
                        print(f"Could not restore music panel in guild {guild.id}: {error}")

                if message is None:
                    try:
                        async for candidate in channel.history(limit=100):
                            if (
                                candidate.author == self.user
                                and candidate.embeds
                                and candidate.embeds[0].title == "Music Control Panel"
                            ):
                                message = candidate
                                break
                    except discord.HTTPException as error:
                        print(f"Could not search for music panel in guild {guild.id}: {error}")

            if message is None or not isinstance(channel, discord.TextChannel):
                print(f"No saved music panel found for guild {guild.id}.")
                continue

            player.control_message = message
            view = ControlView(player)
            try:
                await message.edit(embed=player.build_embed(), view=view)
            except discord.HTTPException as error:
                print(f"Could not update music panel in guild {guild.id}: {error}")
                continue
            self.add_view(view, message_id=message.id)
            self.save_panel_state(guild.id, channel.id, message.id)

    def get_player(self, guild: discord.Guild) -> GuildMusicPlayer:
        if guild.id not in self.players:
            self.players[guild.id] = GuildMusicPlayer(self, guild)
        return self.players[guild.id]


bot = MusicBot()


async def resolve_query(query: str, playlist: bool = False) -> List[dict]:
    """回傳歌曲 info 列表（支援 playlist）"""
    ydl_opts = {
        "format": "bestaudio/best",
        "quiet": True,
        "no_warnings": True,
        "default_search": "ytsearch",
        "extract_flat": "in_playlist" if playlist else False,
        "noplaylist": not playlist,
    }

    def _extract():
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            return ydl.extract_info(query, download=False)

    try:
        info = await asyncio.to_thread(_extract)
        if "entries" in info:
            entries = [e for e in info["entries"] if e][:MAX_PLAYLIST_SIZE]
            return entries
        return [info]
    except Exception as e:
        print(f"Extract error: {e}")
        return []


@bot.tree.command(name="play", description="點播音樂（支援 URL / 關鍵字 / 播放清單）")
@app_commands.describe(query="YouTube / SoundCloud / 其他連結或搜尋關鍵字")
async def play(interaction: Interaction, query: str):
    await interaction.response.defer(thinking=True)

    if not interaction.user.voice or not interaction.user.voice.channel:
        return await interaction.followup.send("請先加入語音頻道。")

    is_playlist = "list=" in query or "playlist" in query.lower()
    infos = await resolve_query(query, playlist=is_playlist)
    if not infos:
        return await interaction.followup.send("無法解析音源。")

    player = bot.get_player(interaction.guild)
    songs = []
    for info in infos:
        duration = info.get("duration") or 0
        if duration > MAX_DURATION:
            continue
        songs.append(Song(
            title=info.get("title", "Unknown"),
            url=info.get("webpage_url") or info.get("url") or query,
            duration=duration,
            requester=interaction.user,
        ))

    if not songs:
        return await interaction.followup.send("沒有可播放的歌曲（可能超過時長限制）。")

    added = await player.enqueue_many(songs)
    if added == 0:
        return await interaction.followup.send("佇列已滿。")

    if len(songs) == 1:
        msg = f"已加入：**{songs[0].title}**"
    else:
        msg = f"已匯入播放清單，共加入 **{added}** 首歌曲。"

    await interaction.followup.send(msg)


@bot.tree.command(name="stay_voice", description="讓機器人留在你的語音頻道，空頻道 24 小時後離開")
async def stay_voice(interaction: Interaction):
    await interaction.response.defer(ephemeral=True)

    if not interaction.guild or not interaction.user.voice or not interaction.user.voice.channel:
        return await interaction.followup.send("請先加入語音頻道。", ephemeral=True)

    player = bot.get_player(interaction.guild)
    channel = interaction.user.voice.channel
    try:
        if player.voice_client and player.voice_client.is_connected():
            if player.voice_client.channel != channel:
                await player.voice_client.move_to(channel)
        else:
            player.voice_client = await channel.connect()
    except (discord.ClientException, discord.HTTPException) as error:
        print(f"Could not connect to voice channel in guild {interaction.guild.id}: {error}")
        return await interaction.followup.send("無法加入語音頻道，請確認機器人有連線權限。", ephemeral=True)

    player.keep_voice = True
    player.update_empty_voice_timer()
    await interaction.followup.send(
        "已啟用語音常駐。最後一位使用者離開後，機器人最多會停留 24 小時。",
        ephemeral=True,
    )


@bot.tree.command(name="setup_music_channel", description="建立專用點播頻道（含常駐 UI）")
async def setup_music_channel(interaction: Interaction):
    await interaction.response.defer()

    # Create a dedicated request channel with bot access.
    overwrites = {
        interaction.guild.default_role: discord.PermissionOverwrite(send_messages=True, read_messages=True),
        interaction.guild.me: discord.PermissionOverwrite(send_messages=True, manage_messages=True),
    }
    channel = await interaction.guild.create_text_channel(
        name="music-requests",
        overwrites=overwrites,
        topic="Send a URL or search query here to queue audio."
    )

    player = bot.get_player(interaction.guild)
    player.music_channel_id = channel.id

    embed = player.build_embed()
    view = ControlView(player)
    msg = await channel.send(embed=embed, view=view)
    player.control_message = msg
    bot.save_panel_state(interaction.guild.id, channel.id, msg.id)

    await interaction.followup.send(f"已建立點播頻道：{channel.mention}\n在頻道裡傳連結或關鍵字就會直接播放。")


@bot.tree.command(name="skip", description="跳過目前歌曲")
async def skip(interaction: Interaction):
    player = bot.get_player(interaction.guild)
    await player.skip()
    await interaction.response.send_message("已跳過。")


@bot.tree.command(name="stop", description="停止播放並清空佇列")
async def stop(interaction: Interaction):
    player = bot.get_player(interaction.guild)
    await player.stop()
    await interaction.response.send_message("已停止。")


@bot.tree.command(name="queue", description="查看佇列")
async def queue_cmd(interaction: Interaction):
    player = bot.get_player(interaction.guild)
    q = list(player.queue._queue)[:20]
    lines = []
    if player.current:
        lines.append(f"**正在播放**：{player.current.title}")
    for i, s in enumerate(q, 1):
        lines.append(f"{i}. {s.title}")
    await interaction.response.send_message("\n".join(lines) or "佇列是空的。")


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return

    player = bot.get_player(message.guild)
    if player.music_channel_id and message.channel.id == player.music_channel_id:
        content = message.content.strip()
        if not content:
            return

        # Message deletion is best-effort and depends on channel permissions.
        try:
            await message.delete()
        except Exception:
            pass

        # Treat messages in the request channel as playback queries.
        if not message.author.voice or not message.author.voice.channel:
            await message.channel.send(f"{message.author.mention} 請先加入語音頻道。", delete_after=8)
            return

        status_message = await message.channel.send(
            f"正在載入音源，請稍候……" 
        )

        is_playlist = "list=" in content or "playlist" in content.lower()
        infos = await resolve_query(content, playlist=is_playlist)
        if not infos:
            await status_message.edit(content="無法解析音源。")
            asyncio.create_task(status_message.delete(delay=6))
            return

        songs = []
        for info in infos:
            duration = info.get("duration") or 0
            if duration > MAX_DURATION:
                continue
            songs.append(Song(
                title=info.get("title", "Unknown"),
                url=info.get("webpage_url") or info.get("url") or content,
                duration=duration,
                requester=message.author,
            ))

        added = await player.enqueue_many(songs)
        if added:
            await status_message.edit(content=f"已加入 {added} 首")
            asyncio.create_task(status_message.delete(delay=6))
            await player.update_control_panel()
        else:
            await status_message.edit(content="佇列已滿或無法加入。")
            asyncio.create_task(status_message.delete(delay=6))

    await bot.process_commands(message)


@bot.event
async def on_voice_state_update(
    member: discord.Member,
    before: discord.VoiceState,
    after: discord.VoiceState,
):
    if member.bot:
        return

    player = bot.players.get(member.guild.id)
    if not player or not player.voice_client or not player.voice_client.is_connected():
        return

    voice_channel = player.voice_client.channel
    if before.channel != voice_channel and after.channel != voice_channel:
        return

    has_humans = any(not voice_member.bot for voice_member in voice_channel.members)
    if has_humans:
        player.update_empty_voice_timer()
    elif player.keep_voice:
        player.update_empty_voice_timer()
    else:
        await player.stop()


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    await bot.restore_music_panels()


if __name__ == "__main__":
    if not TOKEN:
        print("請設定環境變數 DISCORD_TOKEN")
    else:
        bot.run(TOKEN)