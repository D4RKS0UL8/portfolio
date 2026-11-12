"""
Media manipulation commands: .convert, .caption, .resize

Design notes:
  - All three commands work off either an attachment on the invoking
    message, OR an attachment on the message being replied to - same
    "reply to the thing you mean" pattern people expect from bots like
    this.
  - Heavy lifting (ffmpeg subprocess calls, Pillow compositing) always
    runs off the event loop (asyncio.to_thread / create_subprocess_exec)
    so one big video never freezes the whole bot.
  - Every failure path returns a discord.Embed built by one of the
    `err_*` builders below instead of a bare exception - the goal is
    that nothing a user can plausibly do produces an ugly traceback in
    chat. `_pick()` rotates between a few phrasings so hammering the
    same mistake five times in a row doesn't read like a broken record.
  - `.caption` renders the caption band with Pillow (image + gif) or as
    a PNG stacked on top of the video via ffmpeg's vstack filter for
    video input - one text-wrapping code path for all three media
    kinds instead of fighting ffmpeg's drawtext wrapping.
"""
import asyncio
import io
import json
import os
import random
import re
import shutil
import tempfile

import discord
from discord.ext import commands
from PIL import Image, ImageDraw, ImageFont, ImageSequence, UnidentifiedImageError

from utils import imaging
from utils.helpers import error_embed

# ---------------------------------------------------------------- limits

MAX_INPUT_BYTES = 50 * 1024 * 1024        # refuse to even download past this
MAX_OUTPUT_BYTES = 25 * 1024 * 1024       # Discord's non-boosted upload ceiling
GIF_TARGET_BITRATE_KBPS = 3900             # stays under the requested 4000kbps
MAX_VIDEO_SECONDS = 45                     # ffmpeg pass would just take too long past this
MAX_GIF_FRAMES = 600                       # guard against someone feeding in a feature film
MIN_DIMENSION, MAX_DIMENSION = 16, 4096    # sane bounds for .resize
DOWNLOAD_TIMEOUT = 20
FFPROBE_TIMEOUT = 15
FFMPEG_TIMEOUT = 90
MAX_CAPTION_CHARS = 300

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff"}
GIF_EXTS = {".gif"}
VIDEO_EXTS = {".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v"}
ALL_EXTS = IMAGE_EXTS | GIF_EXTS | VIDEO_EXTS

_SIZE_RE = re.compile(r"^(\d{1,5})x(\d{1,5})$", re.IGNORECASE)


def _pick(*variants: str) -> str:
    return random.choice(variants)


class MediaError(commands.CommandError):
    """Wraps an already-built embed so command bodies can just
    `raise MediaError(err_whatever())` and let one except-block ship it."""
    def __init__(self, embed: discord.Embed):
        self.embed = embed
        super().__init__(embed.description or "media error")


# ================================================================== #
#  Every way this can go wrong, with a little personality attached.  #
# ================================================================== #

def err_no_attachment(cmd: str) -> discord.Embed:
    return error_embed(_pick(
        f"I've got nothing to `{cmd}`. Attach a file, or reply to a message that has one.",
        f"`{cmd}` needs a file to chew on and you handed me air. Attach one or reply to one.",
        f"Empty-handed. Attach media to this message, or reply to the message that has it, then try `{cmd}` again.",
    ))


def err_reference_unresolvable() -> discord.Embed:
    return error_embed(_pick(
        "I tried to peek at the message you replied to and Discord gave me nothing back. Try replying again, or attach the file directly.",
        "That reply pointed at a message I couldn't fetch - deleted, maybe? Attach the file straight to this message instead.",
    ))


def err_unsupported_ext(ext: str) -> discord.Embed:
    shown = ext or "(no extension)"
    return error_embed(_pick(
        f"`{shown}` isn't a format I know how to open. Try an image (png/jpg/webp), a gif, or a video (mp4/mov/webm/mkv).",
        f"I don't speak `{shown}`. Give me a still image, a gif, or a common video format and we're good.",
    ))


def err_file_too_large(size_mb: float) -> discord.Embed:
    return error_embed(
        f"That file is **{size_mb:.1f} MB** - bigger than the {MAX_INPUT_BYTES // (1024*1024)} MB I'm willing to "
        f"pull down and process. Compress it or trim it first."
    )


def err_download_failed(status: int | None = None) -> discord.Embed:
    detail = f" (HTTP {status})" if status else ""
    return error_embed(_pick(
        f"Discord's CDN handed me nothing but static{detail}. Try again in a moment.",
        f"The download died halfway{detail}. Could be a hiccup on Discord's end - give it another shot.",
    ))


def err_download_timeout() -> discord.Embed:
    return error_embed(f"Downloading that file took longer than {DOWNLOAD_TIMEOUT}s, so I gave up. Try again, or use a smaller file.")


def err_corrupt_media(detail: str = "") -> discord.Embed:
    base = _pick(
        "That file looks like media but reads like noise - I can't decode it. Try re-exporting or re-saving it and sending again.",
        "Whatever's in that file, it isn't playable. It may be partially uploaded or corrupted - try sending it again.",
    )
    return error_embed(base + (f"\n```\n{detail[:300]}\n```" if detail else ""))


def err_no_visual_stream() -> discord.Embed:
    return error_embed("That file has no video or image data I can find - just audio, or nothing at all. I need something visual to work with.")


def err_zero_duration() -> discord.Embed:
    return error_embed("That video reports a length of zero seconds. There's nothing there to convert.")


def err_video_too_long(seconds: float) -> discord.Embed:
    return error_embed(
        f"That video is **{seconds:.0f}s** long - past my {MAX_VIDEO_SECONDS}s cutoff for this command. "
        f"Trim it down and send it again."
    )


def err_too_many_frames(count: int) -> discord.Embed:
    return error_embed(
        f"That gif has **{count}** frames, which is more than I'll chew through here ({MAX_GIF_FRAMES} max). "
        f"Shorten it or convert a shorter clip instead."
    )


def err_ffmpeg_failed(detail: str = "") -> discord.Embed:
    base = _pick(
        "ffmpeg took one look at that file and gave up. It might use a codec I can't touch, or be subtly broken.",
        "The conversion step failed partway through. The source file may be using something unusual under the hood.",
    )
    return error_embed(base + (f"\n```\n{detail[-300:]}\n```" if detail else ""))


def err_ffmpeg_timeout() -> discord.Embed:
    return error_embed(f"Conversion ran past {FFMPEG_TIMEOUT}s and I killed it. That's usually a sign the source file is too large or too long for this command.")


def err_pillow_failed(detail: str = "") -> discord.Embed:
    base = "Pillow couldn't open that as an image, even though the extension says it should work."
    return error_embed(base + (f"\n```\n{detail[:300]}\n```" if detail else ""))


def err_output_too_large(size_mb: float) -> discord.Embed:
    return error_embed(
        f"The result came out to **{size_mb:.1f} MB**, over Discord's upload limit. Try a smaller source file, "
        f"a shorter clip, or smaller target dimensions."
    )


def err_bad_convert_target(target: str | None) -> discord.Embed:
    shown = target or "(nothing)"
    return error_embed(f"I only know how to `.convert gif` right now - `{shown}` isn't a target I support.")


def err_empty_caption() -> discord.Embed:
    return error_embed(_pick(
        "Caption what, exactly? Give me some text after `.caption`.",
        "You asked for a caption and gave me silence. Add text after the command.",
    ))


def err_caption_too_long(length: int) -> discord.Embed:
    return error_embed(f"That caption is **{length}** characters - I cap it at {MAX_CAPTION_CHARS} so it doesn't swallow the whole image.")


def err_no_font_loaded() -> discord.Embed:
    return error_embed(
        "There's no font loaded for me to render text with (`.listfonts` shows none), and Pillow's fallback bitmap "
        "font can't be scaled cleanly. Ask the owner to `.addfont` one first."
    )


def err_bad_resize_format(raw: str | None) -> discord.Embed:
    shown = raw or "(nothing)"
    return error_embed(f"`{shown}` isn't a size I understand. Use `WIDTHxHEIGHT`, e.g. `.resize 1080x1080`.")


def err_resize_out_of_range(w: int, h: int) -> discord.Embed:
    return error_embed(
        f"`{w}x{h}` is outside what I'll attempt - each side needs to be between {MIN_DIMENSION} and {MAX_DIMENSION}px."
    )


def err_generic(exc: Exception) -> discord.Embed:
    return error_embed(_pick(
        f"Something broke that I wasn't expecting: `{type(exc).__name__}: {exc}`. That's on me - try again, and flag it if it keeps happening.",
        f"Well, that's new. `{type(exc).__name__}: {exc}`. Try again in a bit.",
    ))


# ================================================================== #
#  helpers                                                            #
# ================================================================== #

def _ext(filename: str) -> str:
    return os.path.splitext(filename)[1].lower()


def _kind_of(ext: str) -> str | None:
    if ext in IMAGE_EXTS:
        return "image"
    if ext in GIF_EXTS:
        return "gif"
    if ext in VIDEO_EXTS:
        return "video"
    return None


async def _resolve_attachment(ctx) -> discord.Attachment:
    if ctx.message.attachments:
        return ctx.message.attachments[0]

    ref = ctx.message.reference
    if ref is not None:
        resolved = ref.resolved
        if resolved is None or isinstance(resolved, discord.DeletedReferencedMessage):
            try:
                resolved = await ctx.channel.fetch_message(ref.message_id)
            except discord.HTTPException:
                raise MediaError(err_reference_unresolvable())
        if resolved.attachments:
            return resolved.attachments[0]

    raise MediaError(err_no_attachment(ctx.invoked_with or ctx.command.name))


async def _download(session, attachment: discord.Attachment) -> bytes:
    if attachment.size and attachment.size > MAX_INPUT_BYTES:
        raise MediaError(err_file_too_large(attachment.size / (1024 * 1024)))
    try:
        async with asyncio.timeout(DOWNLOAD_TIMEOUT):
            async with session.get(attachment.url) as resp:
                if resp.status != 200:
                    raise MediaError(err_download_failed(resp.status))
                data = await resp.read()
    except asyncio.TimeoutError:
        raise MediaError(err_download_timeout())
    except MediaError:
        raise
    except Exception:
        raise MediaError(err_download_failed())

    if len(data) > MAX_INPUT_BYTES:
        raise MediaError(err_file_too_large(len(data) / (1024 * 1024)))
    return data


async def _run(*args: str, timeout: float) -> tuple[bytes, bytes, int]:
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    return stdout, stderr, proc.returncode


async def _probe(path: str) -> dict:
    if shutil.which("ffprobe") is None:
        raise MediaError(err_generic(RuntimeError("ffprobe is not installed on this host")))
    try:
        stdout, stderr, code = await _run(
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height,avg_frame_rate,r_frame_rate,duration,nb_frames",
            "-show_entries", "format=duration",
            "-of", "json", path,
            timeout=FFPROBE_TIMEOUT,
        )
    except asyncio.TimeoutError:
        raise MediaError(err_ffmpeg_timeout())

    if code != 0:
        raise MediaError(err_corrupt_media(stderr.decode(errors="ignore")))

    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        raise MediaError(err_corrupt_media())

    streams = data.get("streams") or []
    if not streams:
        raise MediaError(err_no_visual_stream())

    stream = streams[0]
    fmt = data.get("format") or {}
    duration = stream.get("duration") or fmt.get("duration")
    stream["_duration"] = float(duration) if duration not in (None, "N/A") else 0.0

    fps_raw = stream.get("avg_frame_rate") or stream.get("r_frame_rate") or "0/1"
    try:
        num, den = fps_raw.split("/")
        fps = float(num) / float(den) if float(den) != 0 else 0.0
    except (ValueError, ZeroDivisionError):
        fps = 0.0
    stream["_fps"] = fps if fps > 0 else 10.0  # sane fallback, never divide by this being zero
    return stream


def _even(n: int) -> int:
    """Most video encoders choke on odd dimensions."""
    n = max(2, int(n))
    return n if n % 2 == 0 else n - 1


def _main_font_path() -> str:
    fonts = imaging.get_loaded_fonts()
    if not fonts:
        raise MediaError(err_no_font_loaded())
    first = fonts[0]
    path = os.path.join(imaging.FONTS_DIR, first.name)
    if not os.path.isfile(path):
        raise MediaError(err_no_font_loaded())
    return path


def _wrap_lines(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    words = text.split()
    lines, current = [], ""
    for word in words:
        trial = f"{current} {word}".strip()
        if draw.textlength(trial, font=font) <= max_width or not current:
            current = trial
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def _build_caption_band(width: int, text: str, font_path: str) -> Image.Image:
    """A white PNG, `width` wide, sized to fit `text` wrapped and
    rendered at font-size = 10% of width - reused for images, every
    frame of a gif, and stacked onto video via ffmpeg."""
    font_size = max(10, round(width * 0.10))
    font = ImageFont.truetype(font_path, font_size)
    scratch = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    lines = _wrap_lines(scratch, text, font, width - 40)

    line_height = font.getbbox("Ag")[3] + 10
    band_height = _even(line_height * len(lines) + 40)

    band = Image.new("RGB", (width, band_height), "white")
    draw = ImageDraw.Draw(band)
    y = 20
    for line in lines:
        line_w = draw.textlength(line, font=font)
        draw.text(((width - line_w) / 2, y), line, font=font, fill="black")
        y += line_height
    return band


# ================================================================== #
#  the cog                                                            #
# ================================================================== #

class Media(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def _send_error(self, ctx, embed: discord.Embed):
        await ctx.send(embed=embed)

    # ------------------------------------------------------------ .convert

    @commands.command(name="convert")
    async def convert(self, ctx, target: str = None):
        """.convert gif - turn an attached (or replied-to) image/video
        into a gif: same fps as the source, dimensions trimmed down a
        touch, bitrate held under 4000kbps."""
        if not target or target.lower() != "gif":
            return await self._send_error(ctx, err_bad_convert_target(target))

        attachment = await _resolve_attachment(ctx)
        ext = _ext(attachment.filename)
        if ext not in ALL_EXTS:
            return await self._send_error(ctx, err_unsupported_ext(ext))
        if ext in GIF_EXTS:
            return await ctx.send(embed=error_embed("That's already a gif. Nothing for me to do here."))

        async with ctx.typing():
            try:
                data = await _download(self.bot.session, attachment)
                out_bytes = await asyncio.to_thread(self._convert_to_gif_sync, data, ext)
            except MediaError as e:
                return await self._send_error(ctx, e.embed)
            except Exception as e:
                return await self._send_error(ctx, err_generic(e))

        if len(out_bytes) > MAX_OUTPUT_BYTES:
            return await self._send_error(ctx, err_output_too_large(len(out_bytes) / (1024 * 1024)))

        await ctx.send(file=discord.File(io.BytesIO(out_bytes), filename="converted.gif"))

    def _convert_to_gif_sync(self, data: bytes, ext: str) -> bytes:
        # Runs in a worker thread: does the temp-file dance + shells
        # out to ffmpeg. Split out from the async command body so the
        # event loop is never blocked by ffprobe/ffmpeg's own blocking
        # subprocess calls made from a sync context here would need to
        # be - instead we call the async helpers via a private loop.
        return asyncio.run(self._convert_to_gif_async(data, ext))

    async def _convert_to_gif_async(self, data: bytes, ext: str) -> bytes:
        if shutil.which("ffmpeg") is None:
            raise MediaError(err_generic(RuntimeError("ffmpeg is not installed on this host")))

        with tempfile.TemporaryDirectory() as tmp:
            in_path = os.path.join(tmp, f"in{ext}")
            palette_path = os.path.join(tmp, "palette.png")
            out_path = os.path.join(tmp, "out.gif")
            with open(in_path, "wb") as f:
                f.write(data)

            try:
                probe = await _probe(in_path)
            except MediaError:
                if ext not in IMAGE_EXTS:
                    raise
                # ffprobe can be fussy about plain still images; fall
                # back to Pillow to at least confirm it decodes before
                # treating a single frame as a 1-frame "gif" source.
                try:
                    Image.open(io.BytesIO(data)).verify()
                except UnidentifiedImageError as e:
                    raise MediaError(err_pillow_failed(str(e)))
                probe = {"width": None, "height": None, "_fps": 1.0, "_duration": 0.0}

            duration = probe.get("_duration") or 0.0
            if ext in VIDEO_EXTS and duration > MAX_VIDEO_SECONDS:
                raise MediaError(err_video_too_long(duration))

            width, height = probe.get("width"), probe.get("height")
            if width and height:
                scale_filter = f"scale={_even(width * 0.9)}:{_even(height * 0.9)}:flags=lanczos"
            else:
                scale_filter = "scale=iw*0.9:ih*0.9:flags=lanczos"
            fps = probe.get("_fps") or 10.0

            palette_filter = f"fps={fps},{scale_filter},palettegen"
            use_filter = f"fps={fps},{scale_filter}[x];[x][1:v]paletteuse"

            _, stderr, code = await _run(
                "ffmpeg", "-y", "-i", in_path, "-vf", palette_filter, palette_path,
                timeout=FFMPEG_TIMEOUT,
            )
            if code != 0:
                raise MediaError(err_ffmpeg_failed(stderr.decode(errors="ignore")))

            _, stderr, code = await _run(
                "ffmpeg", "-y", "-i", in_path, "-i", palette_path,
                "-lavfi", use_filter,
                "-b:v", f"{GIF_TARGET_BITRATE_KBPS}k",
                out_path,
                timeout=FFMPEG_TIMEOUT,
            )
            if code != 0:
                raise MediaError(err_ffmpeg_failed(stderr.decode(errors="ignore")))

            with open(out_path, "rb") as f:
                return f.read()

    # ------------------------------------------------------------ .caption

    @commands.command(name="caption")
    async def caption(self, ctx, *, text: str = None):
        """.caption <text> - adds a white band across the top with your
        text, sized to ~10% of the media's width, on an image/gif/video."""
        if not text or not text.strip():
            return await self._send_error(ctx, err_empty_caption())
        text = text.strip()
        if len(text) > MAX_CAPTION_CHARS:
            return await self._send_error(ctx, err_caption_too_long(len(text)))

        attachment = await _resolve_attachment(ctx)
        ext = _ext(attachment.filename)
        kind = _kind_of(ext)
        if kind is None:
            return await self._send_error(ctx, err_unsupported_ext(ext))

        try:
            font_path = _main_font_path()
        except MediaError as e:
            return await self._send_error(ctx, e.embed)

        async with ctx.typing():
            try:
                data = await _download(self.bot.session, attachment)
                if kind == "image":
                    out_bytes, out_name = await asyncio.to_thread(self._caption_image_sync, data, font_path), "captioned.png"
                elif kind == "gif":
                    out_bytes, out_name = await asyncio.to_thread(self._caption_gif_sync, data, text, font_path), "captioned.gif"
                else:
                    out_bytes, out_name = await self._caption_video(data, ext, text, font_path), "captioned.mp4"
            except MediaError as e:
                return await self._send_error(ctx, e.embed)
            except Exception as e:
                return await self._send_error(ctx, err_generic(e))

        if len(out_bytes) > MAX_OUTPUT_BYTES:
            return await self._send_error(ctx, err_output_too_large(len(out_bytes) / (1024 * 1024)))
        await ctx.send(file=discord.File(io.BytesIO(out_bytes), filename=out_name))

    def _caption_image_sync(self, data: bytes, text: str, font_path: str) -> bytes:
        try:
            im = Image.open(io.BytesIO(data)).convert("RGB")
        except UnidentifiedImageError as e:
            raise MediaError(err_pillow_failed(str(e)))
        band = _build_caption_band(im.width, text, font_path)
        canvas = Image.new("RGB", (im.width, im.height + band.height), "white")
        canvas.paste(band, (0, 0))
        canvas.paste(im, (0, band.height))
        out = io.BytesIO()
        canvas.save(out, format="PNG")
        return out.getvalue()

    def _caption_gif_sync(self, data: bytes, text: str, font_path: str) -> bytes:
        try:
            src = Image.open(io.BytesIO(data))
        except UnidentifiedImageError as e:
            raise MediaError(err_pillow_failed(str(e)))

        frame_count = getattr(src, "n_frames", 1)
        if frame_count > MAX_GIF_FRAMES:
            raise MediaError(err_too_many_frames(frame_count))

        band = _build_caption_band(src.width, text, font_path)
        out_frames, durations = [], []
        for frame in ImageSequence.Iterator(src):
            rgba = frame.convert("RGBA")
            canvas = Image.new("RGBA", (src.width, src.height + band.height), "white")
            canvas.paste(band.convert("RGBA"), (0, 0))
            canvas.paste(rgba, (0, band.height), rgba)
            out_frames.append(canvas.convert("P", palette=Image.ADAPTIVE))
            durations.append(frame.info.get("duration", 100))

        out = io.BytesIO()
        out_frames[0].save(
            out, format="GIF", save_all=True, append_images=out_frames[1:],
            duration=durations, loop=src.info.get("loop", 0), disposal=2,
        )
        return out.getvalue()

    async def _caption_video(self, data: bytes, ext: str, text: str, font_path: str) -> bytes:
        if shutil.which("ffmpeg") is None:
            raise MediaError(err_generic(RuntimeError("ffmpeg is not installed on this host")))

        with tempfile.TemporaryDirectory() as tmp:
            in_path = os.path.join(tmp, f"in{ext}")
            band_path = os.path.join(tmp, "band.png")
            out_path = os.path.join(tmp, "out.mp4")
            with open(in_path, "wb") as f:
                f.write(data)

            probe = await _probe(in_path)
            duration = probe.get("_duration") or 0.0
            if duration > MAX_VIDEO_SECONDS:
                raise MediaError(err_video_too_long(duration))
            width = probe.get("width")
            if not width:
                raise MediaError(err_no_visual_stream())

            band = await asyncio.to_thread(_build_caption_band, _even(width), text, font_path)
            band.save(band_path)

            _, stderr, code = await _run(
                "ffmpeg", "-y", "-i", in_path, "-i", band_path,
                "-filter_complex", "[1:v][0:v]vstack=inputs=2[v]",
                "-map", "[v]", "-map", "0:a?",
                "-c:v", "libx264", "-c:a", "aac", "-shortest",
                out_path,
                timeout=FFMPEG_TIMEOUT,
            )
            if code != 0:
                raise MediaError(err_ffmpeg_failed(stderr.decode(errors="ignore")))

            with open(out_path, "rb") as f:
                return f.read()

    # ------------------------------------------------------------- .resize

    @commands.command(name="resize")
    async def resize(self, ctx, size: str = None):
        """.resize WIDTHxHEIGHT - stretches (not crops) the attached
        image/gif/video to exactly fit that canvas, ignoring the
        original aspect ratio."""
        match = _SIZE_RE.match(size or "")
        if not match:
            return await self._send_error(ctx, err_bad_resize_format(size))
        width, height = int(match.group(1)), int(match.group(2))
        if not (MIN_DIMENSION <= width <= MAX_DIMENSION and MIN_DIMENSION <= height <= MAX_DIMENSION):
            return await self._send_error(ctx, err_resize_out_of_range(width, height))

        attachment = await _resolve_attachment(ctx)
        ext = _ext(attachment.filename)
        kind = _kind_of(ext)
        if kind is None:
            return await self._send_error(ctx, err_unsupported_ext(ext))

        async with ctx.typing():
            try:
                data = await _download(self.bot.session, attachment)
                if kind == "image":
                    out_bytes, out_name = await asyncio.to_thread(self._resize_image_sync, data, width, height), "resized.png"
                elif kind == "gif":
                    out_bytes, out_name = await asyncio.to_thread(self._resize_gif_sync, data, width, height), "resized.gif"
                else:
                    out_bytes, out_name = await self._resize_video(data, ext, width, height), "resized.mp4"
            except MediaError as e:
                return await self._send_error(ctx, e.embed)
            except Exception as e:
                return await self._send_error(ctx, err_generic(e))

        if len(out_bytes) > MAX_OUTPUT_BYTES:
            return await self._send_error(ctx, err_output_too_large(len(out_bytes) / (1024 * 1024)))
        await ctx.send(file=discord.File(io.BytesIO(out_bytes), filename=out_name))

    def _resize_image_sync(self, data: bytes, width: int, height: int) -> bytes:
        try:
            im = Image.open(io.BytesIO(data)).convert("RGB")
        except UnidentifiedImageError as e:
            raise MediaError(err_pillow_failed(str(e)))
        stretched = im.resize((width, height), Image.LANCZOS)
        out = io.BytesIO()
        stretched.save(out, format="PNG")
        return out.getvalue()

    def _resize_gif_sync(self, data: bytes, width: int, height: int) -> bytes:
        try:
            src = Image.open(io.BytesIO(data))
        except UnidentifiedImageError as e:
            raise MediaError(err_pillow_failed(str(e)))
        frame_count = getattr(src, "n_frames", 1)
        if frame_count > MAX_GIF_FRAMES:
            raise MediaError(err_too_many_frames(frame_count))

        out_frames, durations = [], []
        for frame in ImageSequence.Iterator(src):
            stretched = frame.convert("RGBA").resize((width, height), Image.LANCZOS)
            out_frames.append(stretched.convert("P", palette=Image.ADAPTIVE))
            durations.append(frame.info.get("duration", 100))

        out = io.BytesIO()
        out_frames[0].save(
            out, format="GIF", save_all=True, append_images=out_frames[1:],
            duration=durations, loop=src.info.get("loop", 0), disposal=2,
        )
        return out.getvalue()

    async def _resize_video(self, data: bytes, ext: str, width: int, height: int) -> bytes:
        if shutil.which("ffmpeg") is None:
            raise MediaError(err_generic(RuntimeError("ffmpeg is not installed on this host")))

        with tempfile.TemporaryDirectory() as tmp:
            in_path = os.path.join(tmp, f"in{ext}")
            out_path = os.path.join(tmp, "out.mp4")
            with open(in_path, "wb") as f:
                f.write(data)

            probe = await _probe(in_path)
            duration = probe.get("_duration") or 0.0
            if duration > MAX_VIDEO_SECONDS:
                raise MediaError(err_video_too_long(duration))

            _, stderr, code = await _run(
                "ffmpeg", "-y", "-i", in_path,
                "-vf", f"scale={_even(width)}:{_even(height)}:flags=lanczos",
                "-c:a", "copy",
                out_path,
                timeout=FFMPEG_TIMEOUT,
            )
            if code != 0:
                raise MediaError(err_ffmpeg_failed(stderr.decode(errors="ignore")))

            with open(out_path, "rb") as f:
                return f.read()


async def setup(bot):
    await bot.add_cog(Media(bot))
import asyncio
import io
import json
import os
import random
import re
import shutil
import tempfile
import discord
from discord.ext import commands
from PIL import Image, ImageDraw, ImageFont, ImageSequence, UnidentifiedImageError
from utils import imaging
from utils.helpers import error_embed

MAX_INPUT_BYTES = 50 * 1024 * 1024
MAX_OUTPUT_BYTES = 25 * 1024 * 1024
GIF_TARGET_BITRATE_KBPS = 3900
MAX_VIDEO_SECONDS = 45
MAX_GIF_FRAMES = 600
MIN_DIMENSION, MAX_DIMENSION = (16, 4096)
DOWNLOAD_TIMEOUT = 20
FFPROBE_TIMEOUT = 15
FFMPEG_TIMEOUT = 90
MAX_CAPTION_CHARS = 300

IMAGE_EXTS = {'.png', '.jpg', '.jpeg', '.webp', '.bmp', '.tiff'}
GIF_EXTS = {'.gif'}
VIDEO_EXTS = {'.mp4', '.mov', '.webm', '.mkv', '.avi', '.m4v'}
ALL_EXTS = IMAGE_EXTS | GIF_EXTS | VIDEO_EXTS
_SIZE_RE = re.compile('^(\\d{1,5})x(\\d{1,5})$', re.IGNORECASE)

def _pick(*variants: str) -> str:
    return random.choice(variants)

class MediaError(commands.CommandError):
    def __init__(self, embed: discord.Embed):
        self.embed = embed
        super().__init__(embed.description or 'media error')

def err_no_attachment(cmd: str) -> discord.Embed:
    return error_embed(_pick(f"I've got nothing to `{cmd}`. Attach a file, or reply to a message that has one.", f'`{cmd}` needs a file to chew on and you handed me air. Attach one or reply to one.', f'Empty-handed. Attach media to this message, or reply to the message that has it, then try `{cmd}` again.'))

def err_reference_unresolvable() -> discord.Embed:
    return error_embed(_pick('I tried to peek at the message you replied to and Discord gave me nothing back. Try replying again, or attach the file directly.', "That reply pointed at a message I couldn't fetch - deleted, maybe? Attach the file straight to this message instead."))

def err_unsupported_ext(ext: str) -> discord.Embed:
    shown = ext or '(no extension)'
    return error_embed(_pick(f"`{shown}` isn't a format I know how to open. Try an image (png/jpg/webp), a gif, or a video (mp4/mov/webm/mkv).", f"I don't speak `{shown}`. Give me a still image, a gif, or a common video format and we're good."))

def err_file_too_large(size_mb: float) -> discord.Embed:
    return error_embed(f"That file is **{size_mb:.1f} MB** - bigger than the {MAX_INPUT_BYTES // (1024 * 1024)} MB I'm willing to pull down and process. Compress it or trim it first.")

def err_download_failed(status: int | None=None) -> discord.Embed:
    detail = f' (HTTP {status})' if status else ''
    return error_embed(_pick(f"Discord's CDN handed me nothing but static{detail}. Try again in a moment.", f"The download died halfway{detail}. Could be a hiccup on Discord's end - give it another shot."))

def err_download_timeout() -> discord.Embed:
    return error_embed(f'Downloading that file took longer than {DOWNLOAD_TIMEOUT}s, so I gave up. Try again, or use a smaller file.')

def err_corrupt_media(detail: str='') -> discord.Embed:
    base = _pick("That file looks like media but reads like noise - I can't decode it. Try re-exporting or re-saving it and sending again.", "Whatever's in that file, it isn't playable. It may be partially uploaded or corrupted - try sending it again.")
    return error_embed(base + (f'\n```\n{detail[:300]}\n```' if detail else ''))

def err_no_visual_stream() -> discord.Embed:
    return error_embed('That file has no video or image data I can find - just audio, or nothing at all. I need something visual to work with.')

def err_zero_duration() -> discord.Embed:
    return error_embed("That video reports a length of zero seconds. There's nothing there to convert.")

def err_video_too_long(seconds: float) -> discord.Embed:
    return error_embed(f'That video is **{seconds:.0f}s** long - past my {MAX_VIDEO_SECONDS}s cutoff for this command. Trim it down and send it again.')

def err_too_many_frames(count: int) -> discord.Embed:
    return error_embed(f"That gif has **{count}** frames, which is more than I'll chew through here ({MAX_GIF_FRAMES} max). Shorten it or convert a shorter clip instead.")

def err_ffmpeg_failed(detail: str='') -> discord.Embed:
    base = _pick("ffmpeg took one look at that file and gave up. It might use a codec I can't touch, or be subtly broken.", 'The conversion step failed partway through. The source file may be using something unusual under the hood.')
    return error_embed(base + (f'\n```\n{detail[-300:]}\n```' if detail else ''))

def err_ffmpeg_timeout() -> discord.Embed:
    return error_embed(f"Conversion ran past {FFMPEG_TIMEOUT}s and I killed it. That's usually a sign the source file is too large or too long for this command.")

def err_pillow_failed(detail: str='') -> discord.Embed:
    base = "Pillow couldn't open that as an image, even though the extension says it should work."
    return error_embed(base + (f'\n```\n{detail[:300]}\n```' if detail else ''))

def err_output_too_large(size_mb: float) -> discord.Embed:
    return error_embed(f"The result came out to **{size_mb:.1f} MB**, over Discord's upload limit. Try a smaller source file, a shorter clip, or smaller target dimensions.")

def err_bad_convert_target(target: str | None) -> discord.Embed:
    shown = target or '(nothing)'
    return error_embed(f"I only know how to `.convert gif` right now - `{shown}` isn't a target I support.")

def err_empty_caption() -> discord.Embed:
    return error_embed(_pick('Caption what, exactly? Give me some text after `.caption`.', 'You asked for a caption and gave me silence. Add text after the command.'))

def err_caption_too_long(length: int) -> discord.Embed:
    return error_embed(f"That caption is **{length}** characters - I cap it at {MAX_CAPTION_CHARS} so it doesn't swallow the whole image.")

def err_no_font_loaded() -> discord.Embed:
    return error_embed("There's no font loaded for me to render text with (`.listfonts` shows none), and Pillow's fallback bitmap font can't be scaled cleanly. Ask the owner to `.addfont` one first.")

def err_bad_resize_format(raw: str | None) -> discord.Embed:
    shown = raw or '(nothing)'
    return error_embed(f"`{shown}` isn't a size I understand. Use `WIDTHxHEIGHT`, e.g. `.resize 1080x1080`.")

def err_resize_out_of_range(w: int, h: int) -> discord.Embed:
    return error_embed(f"`{w}x{h}` is outside what I'll attempt - each side needs to be between {MIN_DIMENSION} and {MAX_DIMENSION}px.")

def err_generic(exc: Exception) -> discord.Embed:
    return error_embed(_pick(f"Something broke that I wasn't expecting: `{type(exc).__name__}: {exc}`. That's on me - try again, and flag it if it keeps happening.", f"Well, that's new. `{type(exc).__name__}: {exc}`. Try again in a bit."))

def _ext(filename: str) -> str:
    return os.path.splitext(filename)[1].lower()

def _kind_of(ext: str) -> str | None:
    if ext in IMAGE_EXTS:
        return 'image'
    if ext in GIF_EXTS:
        return 'gif'
    if ext in VIDEO_EXTS:
        return 'video'
    return None

async def _resolve_attachment(ctx) -> discord.Attachment:
    # Check current message
    if ctx.message.attachments:
        return ctx.message.attachments[0]

    # Check referenced message (reply)
    ref = ctx.message.reference
    if ref is not None:
        message_id = ref.message_id
        resolved = ref.resolved

        if not resolved or isinstance(resolved, discord.DeletedReferencedMessage) or not getattr(resolved, 'attachments', None):
            try:
                resolved = await ctx.channel.fetch_message(message_id)
            except discord.HTTPException:
                raise MediaError(err_reference_unresolvable())

        if resolved and resolved.attachments:
            return resolved.attachments[0]

    # Check recent history in channel if user didn't reply directly
    async for msg in ctx.channel.history(limit=10):
        if msg.attachments:
            return msg.attachments[0]

    raise MediaError(err_no_attachment(ctx.invoked_with or ctx.command.name))

async def _download(session, attachment: discord.Attachment) -> bytes:
    if attachment.size and attachment.size > MAX_INPUT_BYTES:
        raise MediaError(err_file_too_large(attachment.size / (1024 * 1024)))

    async def _fetch():
        async with session.get(attachment.url) as resp:
            if resp.status != 200:
                body_preview = (await resp.text())[:300]
                print(f'[media] non-200 fetching {attachment.url!r}: HTTP {resp.status} - {body_preview!r}')
                raise MediaError(err_download_failed(resp.status))
            return await resp.read()

    try:
        data = await asyncio.wait_for(_fetch(), timeout=DOWNLOAD_TIMEOUT)
    except asyncio.TimeoutError:
        raise MediaError(err_download_timeout())
    except MediaError:
        raise
    except Exception as e:
        print(f'[media] download failed for {attachment.url!r}: {type(e).__name__}: {e}')
        raise MediaError(err_download_failed())

    if len(data) > MAX_INPUT_BYTES:
        raise MediaError(err_file_too_large(len(data) / (1024 * 1024)))
    return data

async def _run(*args: str, timeout: float) -> tuple[bytes, bytes, int]:
    proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    return (stdout, stderr, proc.returncode)

async def _probe(path: str) -> dict:
    if shutil.which('ffprobe') is None:
        raise MediaError(err_generic(RuntimeError('ffprobe is not installed on this host')))
    try:
        stdout, stderr, code = await _run('ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries', 'stream=width,height,avg_frame_rate,r_frame_rate,duration,nb_frames', '-show_entries', 'format=duration', '-of', 'json', path, timeout=FFPROBE_TIMEOUT)
    except asyncio.TimeoutError:
        raise MediaError(err_ffmpeg_timeout())
    if code != 0:
        raise MediaError(err_corrupt_media(stderr.decode(errors='ignore')))
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        raise MediaError(err_corrupt_media())
    streams = data.get('streams') or []
    if not streams:
        raise MediaError(err_no_visual_stream())
    stream = streams[0]
    fmt = data.get('format') or {}
    duration = stream.get('duration') or fmt.get('duration')
    stream['_duration'] = float(duration) if duration not in (None, 'N/A') else 0.0
    fps_raw = stream.get('avg_frame_rate') or stream.get('r_frame_rate') or '0/1'
    try:
        num, den = fps_raw.split('/')
        fps = float(num) / float(den) if float(den) != 0 else 0.0
    except (ValueError, ZeroDivisionError):
        fps = 0.0
    stream['_fps'] = fps if fps > 0 else 10.0
    return stream

def _even(n: int) -> int:
    n = max(2, int(n))
    return n if n % 2 == 0 else n - 1

def _main_font_path() -> str:
    fonts = imaging.get_loaded_fonts()
    if not fonts:
        raise MediaError(err_no_font_loaded())
    first = fonts[0]
    path = os.path.join(imaging.FONTS_DIR, first.name)
    if not os.path.isfile(path):
        raise MediaError(err_no_font_loaded())
    return path

def _wrap_lines(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    words = text.split()
    lines, current = ([], '')
    for word in words:
        trial = f'{current} {word}'.strip()
        if draw.textlength(trial, font=font) <= max_width or not current:
            current = trial
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines

def _build_caption_band(width: int, text: str, font_path: str) -> Image.Image:
    font_size = max(10, round(width * 0.1))
    font = ImageFont.truetype(font_path, font_size)
    scratch = ImageDraw.Draw(Image.new('RGB', (1, 1)))
    lines = _wrap_lines(scratch, text, font, width - 40)
    line_height = font.getbbox('Ag')[3] + 10
    band_height = _even(line_height * len(lines) + 40)
    band = Image.new('RGB', (width, band_height), 'white')
    draw = ImageDraw.Draw(band)
    y = 20
    for line in lines:
        line_w = draw.textlength(line, font=font)
        draw.text(((width - line_w) / 2, y), line, font=font, fill='black')
        y += line_height
    return band

class Media(commands.Cog):

    def __init__(self, bot):
        self.bot = bot

    async def _send_error(self, ctx, embed: discord.Embed):
        await ctx.send(embed=embed)

    @commands.command(name='convert')
    async def convert(self, ctx, target: str=None):
        if not target or target.lower() != 'gif':
            return await self._send_error(ctx, err_bad_convert_target(target))
        attachment = await _resolve_attachment(ctx)
        ext = _ext(attachment.filename)
        if ext not in ALL_EXTS:
            return await self._send_error(ctx, err_unsupported_ext(ext))
        if ext in GIF_EXTS:
            return await ctx.send(embed=error_embed("That's already a gif. Nothing for me to do here."))
        async with ctx.typing():
            try:
                data = await _download(self.bot.session, attachment)
                out_bytes = await asyncio.to_thread(self._convert_to_gif_sync, data, ext)
            except MediaError as e:
                return await self._send_error(ctx, e.embed)
            except Exception as e:
                return await self._send_error(ctx, err_generic(e))
        if len(out_bytes) > MAX_OUTPUT_BYTES:
            return await self._send_error(ctx, err_output_too_large(len(out_bytes) / (1024 * 1024)))
        await ctx.send(file=discord.File(io.BytesIO(out_bytes), filename='converted.gif'))

    def _convert_to_gif_sync(self, data: bytes, ext: str) -> bytes:
        return asyncio.run(self._convert_to_gif_async(data, ext))

    async def _convert_to_gif_async(self, data: bytes, ext: str) -> bytes:
        if shutil.which('ffmpeg') is None:
            raise MediaError(err_generic(RuntimeError('ffmpeg is not installed on this host')))
        with tempfile.TemporaryDirectory() as tmp:
            in_path = os.path.join(tmp, f'in{ext}')
            palette_path = os.path.join(tmp, 'palette.png')
            out_path = os.path.join(tmp, 'out.gif')
            with open(in_path, 'wb') as f:
                f.write(data)
            try:
                probe = await _probe(in_path)
            except MediaError:
                if ext not in IMAGE_EXTS:
                    raise
                try:
                    Image.open(io.BytesIO(data)).verify()
                except UnidentifiedImageError as e:
                    raise MediaError(err_pillow_failed(str(e)))
                probe = {'width': None, 'height': None, '_fps': 1.0, '_duration': 0.0}
            duration = probe.get('_duration') or 0.0
            if ext in VIDEO_EXTS and duration > MAX_VIDEO_SECONDS:
                raise MediaError(err_video_too_long(duration))
            width, height = (probe.get('width'), probe.get('height'))
            if width and height:
                scale_filter = f'scale={_even(width * 0.9)}:{_even(height * 0.9)}:flags=lanczos'
            else:
                scale_filter = 'scale=iw*0.9:ih*0.9:flags=lanczos'
            fps = probe.get('_fps') or 10.0
            palette_filter = f'fps={fps},{scale_filter},palettegen'
            use_filter = f'fps={fps},{scale_filter}[x];[x][1:v]paletteuse'
            _, stderr, code = await _run('ffmpeg', '-y', '-i', in_path, '-vf', palette_filter, palette_path, timeout=FFMPEG_TIMEOUT)
            if code != 0:
                raise MediaError(err_ffmpeg_failed(stderr.decode(errors='ignore')))
            _, stderr, code = await _run('ffmpeg', '-y', '-i', in_path, '-i', palette_path, '-lavfi', use_filter, '-b:v', f'{GIF_TARGET_BITRATE_KBPS}k', out_path, timeout=FFMPEG_TIMEOUT)
            if code != 0:
                raise MediaError(err_ffmpeg_failed(stderr.decode(errors='ignore')))
            with open(out_path, 'rb') as f:
                return f.read()

    @commands.command(name='caption')
    async def caption(self, ctx, *, text: str=None):
        if not text or not text.strip():
            return await self._send_error(ctx, err_empty_caption())
        text = text.strip()
        if len(text) > MAX_CAPTION_CHARS:
            return await self._send_error(ctx, err_caption_too_long(len(text)))
        attachment = await _resolve_attachment(ctx)
        ext = _ext(attachment.filename)
        kind = _kind_of(ext)
        if kind is None:
            return await self._send_error(ctx, err_unsupported_ext(ext))
        try:
            font_path = _main_font_path()
        except MediaError as e:
            return await self._send_error(ctx, e.embed)
        async with ctx.typing():
            try:
                data = await _download(self.bot.session, attachment)
                if kind == 'image':
                    out_bytes, out_name = (await asyncio.to_thread(self._caption_image_sync, data, text, font_path), 'captioned.png')
                elif kind == 'gif':
                    out_bytes, out_name = (await asyncio.to_thread(self._caption_gif_sync, data, text, font_path), 'captioned.gif')
                else:
                    out_bytes, out_name = (await self._caption_video(data, ext, text, font_path), 'captioned.mp4')
            except MediaError as e:
                return await self._send_error(ctx, e.embed)
            except Exception as e:
                return await self._send_error(ctx, err_generic(e))
        if len(out_bytes) > MAX_OUTPUT_BYTES:
            return await self._send_error(ctx, err_output_too_large(len(out_bytes) / (1024 * 1024)))
        await ctx.send(file=discord.File(io.BytesIO(out_bytes), filename=out_name))

    def _caption_image_sync(self, data: bytes, text: str, font_path: str) -> bytes:
        try:
            im = Image.open(io.BytesIO(data)).convert('RGB')
        except UnidentifiedImageError as e:
            raise MediaError(err_pillow_failed(str(e)))
        band = _build_caption_band(im.width, text, font_path)
        canvas = Image.new('RGB', (im.width, im.height + band.height), 'white')
        canvas.paste(band, (0, 0))
        canvas.paste(im, (0, band.height))
        out = io.BytesIO()
        canvas.save(out, format='PNG')
        return out.getvalue()

    def _caption_gif_sync(self, data: bytes, text: str, font_path: str) -> bytes:
        try:
            src = Image.open(io.BytesIO(data))
        except UnidentifiedImageError as e:
            raise MediaError(err_pillow_failed(str(e)))
        frame_count = getattr(src, 'n_frames', 1)
        if frame_count > MAX_GIF_FRAMES:
            raise MediaError(err_too_many_frames(frame_count))
        band = _build_caption_band(src.width, text, font_path)
        out_frames, durations = ([], [])
        for frame in ImageSequence.Iterator(src):
            rgba = frame.convert('RGBA')
            canvas = Image.new('RGBA', (src.width, src.height + band.height), 'white')
            canvas.paste(band.convert('RGBA'), (0, 0))
            canvas.paste(rgba, (0, band.height), rgba)
            out_frames.append(canvas.convert('P', palette=Image.ADAPTIVE))
            durations.append(frame.info.get('duration', 100))
        out = io.BytesIO()
        out_frames[0].save(out, format='GIF', save_all=True, append_images=out_frames[1:], duration=durations, loop=src.info.get('loop', 0), disposal=2)
        return out.getvalue()

    async def _caption_video(self, data: bytes, ext: str, text: str, font_path: str) -> bytes:
        if shutil.which('ffmpeg') is None:
            raise MediaError(err_generic(RuntimeError('ffmpeg is not installed on this host')))
        with tempfile.TemporaryDirectory() as tmp:
            in_path = os.path.join(tmp, f'in{ext}')
            band_path = os.path.join(tmp, 'band.png')
            out_path = os.path.join(tmp, 'out.mp4')
            with open(in_path, 'wb') as f:
                f.write(data)
            probe = await _probe(in_path)
            duration = probe.get('_duration') or 0.0
            if duration > MAX_VIDEO_SECONDS:
                raise MediaError(err_video_too_long(duration))
            width = probe.get('width')
            if not width:
                raise MediaError(err_no_visual_stream())
            band = await asyncio.to_thread(_build_caption_band, _even(width), text, font_path)
            band.save(band_path)
            _, stderr, code = await _run('ffmpeg', '-y', '-i', in_path, '-i', band_path, '-filter_complex', '[1:v][0:v]vstack=inputs=2[v]', '-map', '[v]', '-map', '0:a?', '-c:v', 'libx264', '-c:a', 'aac', '-shortest', out_path, timeout=FFMPEG_TIMEOUT)
            if code != 0:
                raise MediaError(err_ffmpeg_failed(stderr.decode(errors='ignore')))
            with open(out_path, 'rb') as f:
                return f.read()

    @commands.command(name='resize')
    async def resize(self, ctx, size: str=None):
        match = _SIZE_RE.match(size or '')
        if not match:
            return await self._send_error(ctx, err_bad_resize_format(size))
        width, height = (int(match.group(1)), int(match.group(2)))
        if not (MIN_DIMENSION <= width <= MAX_DIMENSION and MIN_DIMENSION <= height <= MAX_DIMENSION):
            return await self._send_error(ctx, err_resize_out_of_range(width, height))
        attachment = await _resolve_attachment(ctx)
        ext = _ext(attachment.filename)
        kind = _kind_of(ext)
        if kind is None:
            return await self._send_error(ctx, err_unsupported_ext(ext))
        async with ctx.typing():
            try:
                data = await _download(self.bot.session, attachment)
                if kind == 'image':
                    out_bytes, out_name = (await asyncio.to_thread(self._resize_image_sync, data, width, height), 'resized.png')
                elif kind == 'gif':
                    out_bytes, out_name = (await asyncio.to_thread(self._resize_gif_sync, data, width, height), 'resized.gif')
                else:
                    out_bytes, out_name = (await self._resize_video(data, ext, width, height), 'resized.mp4')
            except MediaError as e:
                return await self._send_error(ctx, e.embed)
            except Exception as e:
                return await self._send_error(ctx, err_generic(e))
        if len(out_bytes) > MAX_OUTPUT_BYTES:
            return await self._send_error(ctx, err_output_too_large(len(out_bytes) / (1024 * 1024)))
        await ctx.send(file=discord.File(io.BytesIO(out_bytes), filename=out_name))

    def _resize_image_sync(self, data: bytes, width: int, height: int) -> bytes:
        try:
            im = Image.open(io.BytesIO(data)).convert('RGB')
        except UnidentifiedImageError as e:
            raise MediaError(err_pillow_failed(str(e)))
        stretched = im.resize((width, height), Image.LANCZOS)
        out = io.BytesIO()
        stretched.save(out, format='PNG')
        return out.getvalue()

    def _resize_gif_sync(self, data: bytes, width: int, height: int) -> bytes:
        try:
            src = Image.open(io.BytesIO(data))
        except UnidentifiedImageError as e:
            raise MediaError(err_pillow_failed(str(e)))
        frame_count = getattr(src, 'n_frames', 1)
        if frame_count > MAX_GIF_FRAMES:
            raise MediaError(err_too_many_frames(frame_count))
        out_frames, durations = ([], [])
        for frame in ImageSequence.Iterator(src):
            stretched = frame.convert('RGBA').resize((width, height), Image.LANCZOS)
            out_frames.append(stretched.convert('P', palette=Image.ADAPTIVE))
            durations.append(frame.info.get('duration', 100))
        out = io.BytesIO()
        out_frames[0].save(out, format='GIF', save_all=True, append_images=out_frames[1:], duration=durations, loop=src.info.get('loop', 0), disposal=2)
        return out.getvalue()

    async def _resize_video(self, data: bytes, ext: str, width: int, height: int) -> bytes:
        if shutil.which('ffmpeg') is None:
            raise MediaError(err_generic(RuntimeError('ffmpeg is not installed on this host')))
        with tempfile.TemporaryDirectory() as tmp:
            in_path = os.path.join(tmp, f'in{ext}')
            out_path = os.path.join(tmp, 'out.mp4')
            with open(in_path, 'wb') as f:
                f.write(data)
            probe = await _probe(in_path)
            duration = probe.get('_duration') or 0.0
            if duration > MAX_VIDEO_SECONDS:
                raise MediaError(err_video_too_long(duration))
            _, stderr, code = await _run('ffmpeg', '-y', '-i', in_path, '-vf', f'scale={_even(width)}:{_even(height)}:flags=lanczos', '-c:a', 'copy', out_path, timeout=FFMPEG_TIMEOUT)
            if code != 0:
                raise MediaError(err_ffmpeg_failed(stderr.decode(errors='ignore')))
            with open(out_path, 'rb') as f:
                return f.read()

async def setup(bot):
    await bot.add_cog(Media(bot))
