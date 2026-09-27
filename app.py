import os
import sys
import json
import shutil
import tempfile
import threading
import webbrowser
import time
import zipfile
import requests
import concurrent.futures
import tkinter as tk
from tkinter import filedialog
from typing import List
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from mutagen.flac import FLAC, Picture
from mutagen.easyid3 import EasyID3
from mutagen.id3 import ID3, USLT, APIC

app = FastAPI(title="SonicTag - Apple Glass Edition")

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

# 1. 弹出系统原生选择文件夹
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

# 2. 全球音源智能聚合检索（并发查询 US + CN 并自动去重排序）
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
                "genre": item.get("primaryGenreName", ""),
                "year": release_date[:4] if release_date else "",
                "cover_url": cover_url,
                "duration": int(item.get("trackTimeMillis", 0) / 1000)
            })
    except Exception:
        pass
    return items

@app.get("/api/search")
def search_metadata(query: str):
    # 并发请求 US (全球库) 与 CN (国区)，彻底告别手动切区
    results = []
    seen_ids = set()
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        future_us = executor.submit(fetch_itunes_region, query, "US")
        future_cn = executor.submit(fetch_itunes_region, query, "CN")
        
        us_results = future_us.result()
        cn_results = future_cn.result()

    # 优先并入更全的全球库，再补充国区特供库
    for item in (us_results + cn_results):
        if item["id"] not in seen_ids:
            seen_ids.add(item["id"])
            results.append(item)

    return {"items": results[:8]}

# 3. 精准歌词搜索
@app.get("/api/lyrics")
def get_lyrics(track: str, artist: str, duration: int = 0):
    headers = {"User-Agent": "SonicTag/3.0"}
    try:
        params = {"track_name": track, "artist_name": artist}
        if duration > 0:
            params["duration"] = duration
        r = requests.get("https://lrclib.net/api/get", params=params, headers=headers, timeout=5)
        if r.status_code == 200:
            data = r.json()
            synced = data.get("syncedLyrics")
            plain = data.get("plainLyrics")
            if synced or plain:
                return {"lyrics": synced if synced else plain}
    except Exception:
        pass

    try:
        r = requests.get("https://lrclib.net/api/search", params={"track_name": track, "artist_name": artist}, headers=headers, timeout=5)
        if r.status_code == 200:
            items = r.json()
            if isinstance(items, list) and len(items) > 0:
                first = items[0]
                return {"lyrics": first.get("syncedLyrics") or first.get("plainLyrics") or ""}
    except Exception:
        pass

    return {"lyrics": ""}

# 4. 音频元数据写入
def apply_tags_to_file(file_path: str, meta: dict):
    suffix = os.path.splitext(file_path)[1].lower()
    cover_bytes = b""
    cover_mime = "image/jpeg"
    cover_url = meta.get("cover_url", "")
    
    if cover_url:
        try:
            r = requests.get(cover_url, timeout=8)
            if r.status_code == 200:
                cover_bytes = r.content
                if cover_bytes.startswith(b"\x89PNG"):
                    cover_mime = "image/png"
        except Exception as e:
            print(f"[封面保存跳过]: {e}")

    title = meta.get("title", "")
    artist = meta.get("artist", "")
    album = meta.get("album", "")
    genre = meta.get("genre", "")
    lyrics = meta.get("lyrics", "")

    if suffix == ".flac":
        audio = FLAC(file_path)
        if title: audio["TITLE"] = title
        if artist: audio["ARTIST"] = artist
        if album: audio["ALBUM"] = album
        if genre: audio["GENRE"] = genre
        if lyrics: audio["LYRICS"] = lyrics

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
        if lyrics:
            id3.add(USLT(encoding=3, lang="eng", desc="", text=lyrics))
        if cover_bytes:
            id3.add(APIC(encoding=3, mime=cover_mime, type=3, desc="Front Cover", data=cover_bytes))
        id3.save(v2_version=3)

# 5. 批量处理与写出
@app.post("/api/batch-process")
async def batch_process(
    files: List[UploadFile] = File(...),
    metadata_json: str = Form(...),
    export_lrc: bool = Form(False),
    save_to_local: bool = Form(False),
    target_folder: str = Form("")
):
    try:
        meta_list = json.loads(metadata_json)
    except Exception:
        raise HTTPException(status_code=400, detail="元数据解析错误")

    if save_to_local:
        if not target_folder or not os.path.exists(target_folder):
            raise HTTPException(status_code=400, detail="目标文件夹不存在")

        for i, file in enumerate(files):
            meta = meta_list[i] if i < len(meta_list) else {}
            base_name, ext = os.path.splitext(file.filename)
            out_audio = os.path.join(target_folder, f"[Tagged]_{base_name}{ext}")

            with open(out_audio, "wb") as f:
                shutil.copyfileobj(file.file, f)

            try:
                apply_tags_to_file(out_audio, meta)
            except Exception as e:
                print(f"[写入音频失败]: {e}")

            if export_lrc and meta.get("lyrics"):
                lrc_path = os.path.join(target_folder, f"[Tagged]_{base_name}.lrc")
                try:
                    with open(lrc_path, "w", encoding="utf-8") as lf:
                        lf.write(meta["lyrics"])
                except Exception:
                    pass

        try:
            os.startfile(target_folder)
        except Exception:
            pass

        return {"status": "ok", "saved_path": target_folder}

    # ZIP 网页导出
    temp_dir = tempfile.mkdtemp()
    items_to_zip = []

    for i, file in enumerate(files):
        meta = meta_list[i] if i < len(meta_list) else {}
        base_name, ext = os.path.splitext(file.filename)
        out_audio = os.path.join(temp_dir, file.filename)

        with open(out_audio, "wb") as f:
            shutil.copyfileobj(file.file, f)

        try:
            apply_tags_to_file(out_audio, meta)
            items_to_zip.append((out_audio, f"[Tagged]_{file.filename}"))
        except Exception:
            items_to_zip.append((out_audio, f"[Tagged]_{file.filename}"))

        if export_lrc and meta.get("lyrics"):
            lrc_path = os.path.join(temp_dir, f"{base_name}.lrc")
            try:
                with open(lrc_path, "w", encoding="utf-8") as lf:
                    lf.write(meta["lyrics"])
                items_to_zip.append((lrc_path, f"[Tagged]_{base_name}.lrc"))
            except Exception:
                pass

    zip_path = os.path.join(temp_dir, "Tagged_Music.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for fpath, arcname in items_to_zip:
            zf.write(fpath, arcname=arcname)

    return FileResponse(zip_path, media_type="application/zip", filename="Tagged_Music.zip")

def open_browser():
    time.sleep(1.2)
    webbrowser.open("http://127.0.0.1:8000")

if __name__ == "__main__":
    import uvicorn
    threading.Thread(target=open_browser, daemon=True).start()
    uvicorn.run(app, host="127.0.0.1", port=8000)