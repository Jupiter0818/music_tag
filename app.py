import os
import re
import sys
import io
import json
import shutil
import base64
import tempfile
import threading
import webbrowser
import time
import zipfile
import requests
import concurrent.futures
import tkinter as tk
from tkinter import filedialog
from typing import List, Optional
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from mutagen.flac import FLAC, Picture
from mutagen.easyid3 import EasyID3
from mutagen.id3 import ID3, USLT, APIC, TRCK, TPOS, TPE2, TDRC
from mutagen.mp3 import MP3

try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

app = FastAPI(title="SonicTag Studio - Ultimate Edition")

export_progress = {}

def get_resource_path(relative_path):
    if hasattr(sys, '_MEIPASS'):
        return os.path.join(sys._MEIPASS, relative_path)
    return os.path.join(os.path.abspath("."), relative_path)

@app.get("/", response_class=HTMLResponse)
async def serve_index():
    html_path = get_resource_path("index.html")
    if not os.path.exists(html_path):
        raise HTTPException(status_code=404, detail="未找到 index.html")
    with open(html_path, "r", encoding="utf-8") as f:
        return f.read()

@app.get("/api/progress")
def get_progress(job_id: str):
    return export_progress.get(job_id, {"current": 0, "total": 0, "status": "idle", "file": "", "percent": 0})

@app.get("/api/select-folder")
def select_folder():
    try:
        root = tk.Tk()
        root.withdraw()
        root.attributes('-topmost', True)
        folder = filedialog.askdirectory(title="选择保存位置")
        root.destroy()
        return {"folder": folder if folder else ""}
    except Exception as e:
        print(f"[目录选择异常]: {e}")
        return {"folder": ""}

# 1. 本地音频物理参数透视与已有标签检测
@app.post("/api/inspect")
async def inspect_audio(file: UploadFile = File(...)):
    suffix = os.path.splitext(file.filename)[1].lower()
    temp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    temp_path = temp.name
    try:
        shutil.copyfileobj(file.file, temp)
        temp.close()

        specs = {"format": suffix.replace(".", "").upper(), "badge": "Audio", "color": "gray"}
        existing = {}

        if suffix == ".flac":
            audio = FLAC(temp_path)
            sr = audio.info.sample_rate
            bps = audio.info.bits_per_sample
            specs["sample_rate"] = sr
            specs["bits_per_sample"] = bps
            bitrate = int(audio.info.total_samples * bps * audio.info.channels / audio.info.length / 1000) if audio.info.length else 0
            specs["bitrate"] = bitrate

            if bps >= 24 or sr > 48000:
                specs["badge"] = "Hi-Res Lossless"
                specs["color"] = "gold"
            else:
                specs["badge"] = "Lossless"
                specs["color"] = "silver"
            specs["spec_text"] = f"{bps}-bit / {sr/1000:.1f} kHz"

            existing = {
                "title": audio.get("TITLE", [""])[0],
                "artist": audio.get("ARTIST", [""])[0],
                "album": audio.get("ALBUM", [""])[0],
                "genre": audio.get("GENRE", [""])[0],
                "track_num": audio.get("TRACKNUMBER", [""])[0],
                "disc_num": audio.get("DISCNUMBER", [""])[0],
                "album_artist": audio.get("ALBUMARTIST", [""])[0],
                "has_cover": len(audio.pictures) > 0
            }

        elif suffix == ".mp3":
            audio = MP3(temp_path)
            sr = audio.info.sample_rate
            br = int(audio.info.bitrate / 1000) if audio.info.bitrate else 0
            specs["sample_rate"] = sr
            specs["bitrate"] = br
            specs["badge"] = "MP3"
            specs["color"] = "gray"
            specs["spec_text"] = f"{br} kbps · {sr/1000:.1f} kHz"

            try:
                id3 = EasyID3(temp_path)
                existing = {
                    "title": id3.get("title", [""])[0],
                    "artist": id3.get("artist", [""])[0],
                    "album": id3.get("album", [""])[0],
                    "genre": id3.get("genre", [""])[0],
                    "track_num": id3.get("tracknumber", [""])[0],
                    "disc_num": id3.get("discnumber", [""])[0],
                    "album_artist": id3.get("performer", [""])[0],
                    "has_cover": False
                }
            except Exception:
                pass

        return {"specs": specs, "existing": existing}
    except Exception as e:
        print(f"[参数透视失败]: {e}")
        return {"specs": {"format": "AUDIO", "badge": "Audio", "color": "gray", "spec_text": ""}, "existing": {}}
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)

# 2. 全球音源并发聚合检索（含曲目编号与碟片信息）
def fetch_itunes_region(query: str, country: str):
    items = []
    try:
        url = f"https://itunes.apple.com/search?term={requests.utils.quote(query)}&entity=song&limit=6&country={country}"
        res = requests.get(url, timeout=5).json()
        for item in res.get("results", []):
            raw_cover = item.get("artworkUrl100", "")
            cover_url = raw_cover.replace("100x100bb.jpg", "1400x1400bb.jpg") if raw_cover else ""
            release_date = item.get("releaseDate", "")
            items.append({
                "id": str(item.get("trackId", "")),
                "title": item.get("trackName", ""),
                "artist": item.get("artistName", ""),
                "album": item.get("collectionName", ""),
                "album_artist": item.get("collectionArtistName") or item.get("artistName", ""),
                "genre": item.get("primaryGenreName", ""),
                "year": release_date[:4] if release_date else "",
                "track_num": item.get("trackNumber", 1),
                "track_total": item.get("trackCount", 1),
                "disc_num": item.get("discNumber", 1),
                "disc_total": item.get("discCount", 1),
                "cover_url": cover_url,
                "duration": int(item.get("trackTimeMillis", 0) / 1000)
            })
    except Exception:
        pass
    return items

@app.get("/api/search")
def search_metadata(query: str):
    results = []
    seen_ids = set()
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        future_us = executor.submit(fetch_itunes_region, query, "US")
        future_cn = executor.submit(fetch_itunes_region, query, "CN")
        us_results = future_us.result()
        cn_results = future_cn.result()

    for item in (us_results + cn_results):
        if item["id"] not in seen_ids:
            seen_ids.add(item["id"])
            results.append(item)

    return {"items": results[:8]}

# 3. 双语歌词时间戳对齐与合成
def parse_lrc_lines(lrc_text: str):
    if not lrc_text: return []
    lines = lrc_text.strip().split('\n')
    pattern = re.compile(r'\[(\d{2}):(\d{2})(?:\.(\d{2,3}))?\]')
    result = []
    for line in lines:
        times = pattern.findall(line)
        text = pattern.sub('', line).strip()
        if not times: continue
        for m in times:
            minute = int(m[0])
            sec = int(m[1])
            ms_str = m[2] if m[2] else "0"
            ms = int(ms_str.ljust(3, '0')[:3])
            result.append((minute * 60 + sec + ms / 1000.0, text))
    result.sort(key=lambda x: x[0])
    return result

def merge_bilingual_lrc(orig_lrc: str, trans_lrc: str, tolerance: float = 1.0) -> str:
    orig_lines = parse_lrc_lines(orig_lrc)
    trans_lines = parse_lrc_lines(trans_lrc)
    if not trans_lines: return orig_lrc
    if not orig_lines: return trans_lrc

    merged = []
    used_trans = set()
    for o_time, o_text in orig_lines:
        m = int(o_time // 60)
        s = int(o_time % 60)
        ms = int(round((o_time - int(o_time)) * 100))
        time_tag = f"[{m:02d}:{s:02d}.{ms:02d}]"
        merged.append(f"{time_tag}{o_text}")

        best_i = -1
        best_diff = tolerance
        for i, (t_time, t_text) in enumerate(trans_lines):
            if i in used_trans or not t_text: continue
            diff = abs(t_time - o_time)
            if diff < best_diff:
                best_diff = diff
                best_i = i

        if best_i != -1:
            used_trans.add(best_i)
            t_text = trans_lines[best_i][1]
            if t_text:
                merged.append(f"{time_tag}{t_text}")

    return '\n'.join(merged)

def fetch_netease_lyrics(track: str, artist: str):
    headers = {"Referer": "https://music.163.com/", "User-Agent": "Mozilla/5.0"}
    try:
        url = "https://music.163.com/api/search/get/web"
        params = {"s": f"{track} {artist}".strip(), "type": 1, "offset": 0, "total": "true", "limit": 2}
        res = requests.get(url, params=params, headers=headers, timeout=4).json()
        songs = res.get("result", {}).get("songs", [])
        if not songs: return "", ""

        song_id = songs[0]["id"]
        l_res = requests.get("https://music.163.com/api/song/lyric", params={"os": "pc", "id": song_id, "lv": -1, "kv": -1, "tv": -1}, headers=headers, timeout=4).json()
        return l_res.get("lrc", {}).get("lyric", ""), l_res.get("tlyric", {}).get("lyric", "")
    except Exception:
        return "", ""

def fetch_lrclib_lyrics(track: str, artist: str, duration: int = 0):
    headers = {"User-Agent": "SonicTag/4.0"}
    try:
        params = {"track_name": track, "artist_name": artist}
        if duration > 0: params["duration"] = duration
        r = requests.get("https://lrclib.net/api/get", params=params, headers=headers, timeout=4)
        if r.status_code == 200:
            data = r.json()
            return data.get("syncedLyrics") or data.get("plainLyrics") or ""
    except Exception:
        pass
    return ""

@app.get("/api/lyrics")
def get_lyrics(track: str, artist: str, duration: int = 0):
    ne_orig, ne_trans = fetch_netease_lyrics(track, artist)
    lrclib_orig = ""
    if not ne_orig:
        lrclib_orig = fetch_lrclib_lyrics(track, artist, duration)

    final_orig = ne_orig or lrclib_orig
    final_trans = ne_trans
    bilingual = ""
    if final_orig and final_trans:
        bilingual = merge_bilingual_lrc(final_orig, final_trans)

    return {
        "orig": final_orig,
        "trans": final_trans,
        "bilingual": bilingual,
        "has_translation": bool(final_trans),
        "lyrics": bilingual if bilingual else final_orig
    }

# 4. 封面与车机压缩处理
def process_cover_bytes(raw_bytes: bytes, compress_for_car: bool = False) -> tuple[bytes, str]:
    if not raw_bytes:
        return b"", "image/jpeg"
    mime = "image/png" if raw_bytes.startswith(b"\x89PNG") else "image/jpeg"

    if compress_for_car and HAS_PIL:
        try:
            img = Image.open(io.BytesIO(raw_bytes))
            if img.mode in ("RGBA", "P"):
                img = img.convert("RGB")
            max_size = 1000
            if img.width > max_size or img.height > max_size:
                img.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)
            out_buf = io.BytesIO()
            img.save(out_buf, format="JPEG", quality=85, optimize=True)
            return out_buf.getvalue(), "image/jpeg"
        except Exception as e:
            print(f"[封面压缩异常]: {e}")

    return raw_bytes, mime

def sanitize_filename(name: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', '_', name).strip()

def build_destination_path(base_folder: str, template: str, meta: dict, ext: str, original_filename: str) -> str:
    if template == "original" or not template:
        clean_base = sanitize_filename(os.path.splitext(original_filename)[0])
        return os.path.join(base_folder, f"[Tagged]_{clean_base}{ext}")

    track_str = str(meta.get("track_num") or "1").zfill(2)
    disc_str = str(meta.get("disc_num") or "1")

    replacements = {
        "{artist}": sanitize_filename(meta.get("artist") or "Unknown Artist"),
        "{album}": sanitize_filename(meta.get("album") or "Unknown Album"),
        "{title}": sanitize_filename(meta.get("title") or "Unknown Track"),
        "{track}": track_str,
        "{disc}": disc_str,
        "{year}": str(meta.get("year") or "")
    }

    rel_path = template
    for tag, val in replacements.items():
        rel_path = rel_path.replace(tag, val)

    rel_path = rel_path.replace("\\", "/").strip("/")
    full_path = os.path.join(base_folder, rel_path + ext)
    os.makedirs(os.path.dirname(full_path), exist_ok=True)
    return full_path

# 5. 音频元数据深度注入（含 Track、Disc、AlbumArtist）
def apply_tags_to_file(file_path: str, meta: dict, compress_for_car: bool = False):
    suffix = os.path.splitext(file_path)[1].lower()

    # 处理封面（支持外链、本地拖入 Base64）
    cover_bytes = b""
    cover_data_str = meta.get("cover_data") or meta.get("cover_url", "")
    if cover_data_str.startswith("data:image/"):
        try:
            b64_data = cover_data_str.split(",", 1)[1]
            cover_bytes = base64.b64decode(b64_data)
        except Exception:
            pass
    elif cover_data_str.startswith("http"):
        try:
            r = requests.get(cover_data_str, timeout=8)
            if r.status_code == 200:
                cover_bytes = r.content
        except Exception:
            pass

    cover_bytes, cover_mime = process_cover_bytes(cover_bytes, compress_for_car)

    title = meta.get("title", "")
    artist = meta.get("artist", "")
    album = meta.get("album", "")
    album_artist = meta.get("album_artist", "")
    genre = meta.get("genre", "")
    lyrics = meta.get("lyrics", "")
    year = meta.get("year", "")
    track_num = meta.get("track_num", "")
    track_total = meta.get("track_total", "")
    disc_num = meta.get("disc_num", "")
    disc_total = meta.get("disc_total", "")

    if suffix == ".flac":
        audio = FLAC(file_path)
        if title: audio["TITLE"] = title
        if artist: audio["ARTIST"] = artist
        if album: audio["ALBUM"] = album
        if album_artist: audio["ALBUMARTIST"] = album_artist
        if genre: audio["GENRE"] = genre
        if lyrics: audio["LYRICS"] = lyrics
        if year: audio["DATE"] = str(year)
        if track_num: audio["TRACKNUMBER"] = str(track_num)
        if track_total: audio["TRACKTOTAL"] = str(track_total)
        if disc_num: audio["DISCNUMBER"] = str(disc_num)
        if disc_total: audio["DISCTOTAL"] = str(disc_total)

        if cover_bytes:
            pic = Picture()
            pic.type = 3
            pic.mime = cover_mime
            pic.desc = "Front Cover"
            pic.data = cover_bytes
            audio.clear_pictures()
            audio.add_picture(pic)
        audio.save()

    elif suffix == ".mp3":
        id3 = ID3(file_path)
        id3.delete()

        easy_audio = EasyID3(file_path)
        if title: easy_audio["title"] = title
        if artist: easy_audio["artist"] = artist
        if album: easy_audio["album"] = album
        if genre: easy_audio["genre"] = genre
        easy_audio.save()

        id3 = ID3(file_path)
        if album_artist:
            id3.add(TPE2(encoding=3, text=album_artist))
        if year:
            id3.add(TDRC(encoding=3, text=str(year)))
        if track_num:
            t_val = f"{track_num}/{track_total}" if track_total else str(track_num)
            id3.add(TRCK(encoding=3, text=t_val))
        if disc_num:
            d_val = f"{disc_num}/{disc_total}" if disc_total else str(disc_num)
            id3.add(TPOS(encoding=3, text=d_val))
        if lyrics:
            id3.add(USLT(encoding=3, lang="eng", desc="", text=lyrics))
        if cover_bytes:
            id3.add(APIC(encoding=3, mime=cover_mime, type=3, desc="Front Cover", data=cover_bytes))
        id3.save(v2_version=3)

# 6. 批量导出与智能归档
@app.post("/api/batch-process")
def batch_process(
    files: List[UploadFile] = File(...),
    metadata_json: str = Form(...),
    export_lrc: bool = Form(False),
    save_to_local: bool = Form(False),
    target_folder: str = Form(""),
    organize_pattern: str = Form("original"),
    compress_for_car: bool = Form(False),
    job_id: str = Form("")
):
    try:
        meta_list = json.loads(metadata_json)
    except Exception:
        raise HTTPException(status_code=400, detail="元数据解析错误")

    total_files = len(files)
    if job_id:
        export_progress[job_id] = {
            "current": 0,
            "total": total_files,
            "status": "processing",
            "file": "正在初始化并解构音频流...",
            "percent": 25
        }

    # 模式 A: 本地目录归档直存
    if save_to_local:
        if not target_folder or not os.path.exists(target_folder):
            if job_id and job_id in export_progress:
                export_progress[job_id]["status"] = "error"
            raise HTTPException(status_code=400, detail="目标文件夹不存在")

        for i, file in enumerate(files):
            meta = meta_list[i] if i < len(meta_list) else {}
            ext = os.path.splitext(file.filename)[1].lower()

            out_audio = build_destination_path(target_folder, organize_pattern, meta, ext, file.filename)

            if job_id:
                calc_pct = int(25 + ((i + 0.3) / total_files) * 70)
                export_progress[job_id]["current"] = i + 1
                export_progress[job_id]["file"] = file.filename
                export_progress[job_id]["percent"] = calc_pct

            with open(out_audio, "wb") as f:
                shutil.copyfileobj(file.file, f)

            try:
                apply_tags_to_file(out_audio, meta, compress_for_car)
            except Exception as e:
                print(f"[写入音频失败]: {e}")

            if export_lrc and meta.get("lyrics"):
                base_without_ext = os.path.splitext(out_audio)[0]
                lrc_path = f"{base_without_ext}.lrc"
                try:
                    with open(lrc_path, "w", encoding="utf-8") as lf:
                        lf.write(meta["lyrics"])
                except Exception:
                    pass

        if job_id:
            export_progress[job_id]["percent"] = 100
            export_progress[job_id]["status"] = "done"

        try:
            os.startfile(target_folder)
        except Exception:
            pass

        return {"status": "ok", "saved_path": target_folder}

    # 模式 B: ZIP 压缩打包归档下载
    temp_dir = tempfile.mkdtemp()
    items_to_zip = []

    for i, file in enumerate(files):
        meta = meta_list[i] if i < len(meta_list) else {}
        ext = os.path.splitext(file.filename)[1].lower()

        out_audio = build_destination_path(temp_dir, organize_pattern, meta, ext, file.filename)
        rel_zip_name = os.path.relpath(out_audio, temp_dir)

        if job_id:
            calc_pct = int(25 + ((i + 0.3) / total_files) * 60)
            export_progress[job_id]["current"] = i + 1
            export_progress[job_id]["file"] = file.filename
            export_progress[job_id]["percent"] = calc_pct

        with open(out_audio, "wb") as f:
            shutil.copyfileobj(file.file, f)

        try:
            apply_tags_to_file(out_audio, meta, compress_for_car)
            items_to_zip.append((out_audio, rel_zip_name))
        except Exception:
            items_to_zip.append((out_audio, rel_zip_name))

        if export_lrc and meta.get("lyrics"):
            base_without_ext = os.path.splitext(out_audio)[0]
            lrc_path = f"{base_without_ext}.lrc"
            rel_lrc_name = os.path.relpath(lrc_path, temp_dir)
            try:
                with open(lrc_path, "w", encoding="utf-8") as lf:
                    lf.write(meta["lyrics"])
                items_to_zip.append((lrc_path, rel_lrc_name))
            except Exception:
                pass

    if job_id:
        export_progress[job_id]["file"] = "正在压缩封装为 ZIP..."
        export_progress[job_id]["percent"] = 90

    zip_path = os.path.join(temp_dir, "Tagged_Music.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for fpath, arcname in items_to_zip:
            zf.write(fpath, arcname=arcname)

    if job_id:
        export_progress[job_id]["percent"] = 100
        export_progress[job_id]["status"] = "done"

    return FileResponse(zip_path, media_type="application/zip", filename="Tagged_Music.zip")

def open_browser():
    time.sleep(1.2)
    webbrowser.open("http://127.0.0.1:8000")

if __name__ == "__main__":
    import uvicorn
    threading.Thread(target=open_browser, daemon=True).start()
    uvicorn.run(app, host="127.0.0.1", port=8000)