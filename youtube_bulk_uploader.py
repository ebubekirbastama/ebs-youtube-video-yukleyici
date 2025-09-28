import os
import threading
import queue
import time
from datetime import datetime
from typing import Optional, Dict, Any, List
from urllib.parse import urlparse, parse_qs

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

# UI
from ttkbootstrap import Style
from ttkbootstrap.constants import *

# Veri
import pandas as pd

# Google / YouTube API
import google.auth.transport.requests
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload


try:
    from PIL import Image
    PIL_AVAILABLE = True
except Exception:
    PIL_AVAILABLE = False

try:
    from moviepy.editor import VideoFileClip
    MOVIEPY_AVAILABLE = True
except Exception:
    MOVIEPY_AVAILABLE = False

# ---- Ayarlar ----
SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube"
]
CLIENT_SECRET_FILE = "client_secret.json"
TOKEN_FILE = "token.json"

REQUIRED_COLUMNS = [
    "video_path", "title", "description", "tags",
    "privacyStatus", "publishAt", "playlist_id",
    "thumbnail_path", "categoryId", "made_for_kids"
]
# Opsiyonel kolonlar (Shorts tespiti için)
OPTIONAL_COLUMNS = ["is_short", "duration_seconds"]

# Thumbnail kuralları
SUPPORTED_THUMB_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp"}
MAX_THUMB_SIZE_MB = 2
MIN_THUMB_WIDTH = 1280
MIN_THUMB_HEIGHT = 720

# ===================== Yardımcılar =====================

def load_table(path: str) -> pd.DataFrame:
    ext = os.path.splitext(path)[1].lower()
    if ext in [".xlsx", ".xls"]:
        df = pd.read_excel(path)
    elif ext == ".csv":
        df = pd.read_csv(path)
    else:
        raise ValueError("Lütfen .xlsx/.xls veya .csv dosyası seçin.")
    for col in REQUIRED_COLUMNS + OPTIONAL_COLUMNS:
        if col not in df.columns:
            df[col] = ""
    return df[REQUIRED_COLUMNS + OPTIONAL_COLUMNS].copy()

def get_youtube_service() -> Any:
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(google.auth.transport.requests.Request())
            except Exception:
                creds = None
        if not creds:
            if not os.path.exists(CLIENT_SECRET_FILE):
                raise FileNotFoundError(
                    f"'{CLIENT_SECRET_FILE}' bulunamadı. Google OAuth istemci dosyasını bu adla ekleyin."
                )
            flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
            with open(TOKEN_FILE, "w", encoding="utf-8") as token:
                token.write(creds.to_json())
    return build("youtube", "v3", credentials=creds)

def safe_bool(x: Any) -> bool:
    if isinstance(x, bool):
        return x
    s = str(x).strip().lower()
    return s in ["true", "1", "evet", "yes"]

def parse_tags(tag_str: str) -> List[str]:
    if not isinstance(tag_str, str):
        return []
    return [t.strip() for t in tag_str.split(",") if t.strip()]

def build_body(row: pd.Series) -> Dict[str, Any]:
    title = str(row.get("title", "")).strip()
    description = str(row.get("description", "")).replace("\\n", "\n")
    tags = parse_tags(row.get("tags", ""))
    privacy = str(row.get("privacyStatus", "public")).strip().lower() or "public"
    category_id = str(row.get("categoryId", "22")).strip() or "22"
    made_for_kids = safe_bool(row.get("made_for_kids", "false"))

    body = {
        "snippet": {
            "title": title,
            "description": description,
            "categoryId": category_id,
        },
        "status": {
            "privacyStatus": "private" if privacy == "scheduled" else privacy,
            "selfDeclaredMadeForKids": made_for_kids
        }
    }
    if tags:
        body["snippet"]["tags"] = tags
    return body

def detect_is_short(row: pd.Series, log_cb=None) -> bool:
    # 1) Kullanıcı sütunları
    if str(row.get("is_short", "")).strip():
        return safe_bool(row["is_short"])
    # 2) Süre sütunu
    try:
        dur = float(row.get("duration_seconds", 0) or 0)
        if dur > 0:
            return dur < 61
    except Exception:
        pass
    # 3) moviepy ile otomatik süre (varsa)
    video_path = str(row.get("video_path", "")).strip()
    if MOVIEPY_AVAILABLE and os.path.exists(video_path):
        try:
            with VideoFileClip(video_path) as clip:
                if clip.duration and clip.duration < 61:
                    return True
        except Exception:
            if log_cb: log_cb("Süre okunamadı (moviepy).")
    # 4) Basit ipucu: dosya adı “short/shorts” içeriyorsa
    fname = os.path.basename(video_path).lower()
    return "short" in fname or "shorts" in fname

def validate_thumbnail(thumb_path: str, log_cb=None) -> bool:
    if not thumb_path or not os.path.exists(thumb_path):
        if log_cb: log_cb("Thumbnail yok veya yol geçersiz, atlanıyor.")
        return False
    ext = os.path.splitext(thumb_path)[1].lower()
    if ext not in SUPPORTED_THUMB_EXTS:
        if log_cb: log_cb(f"Thumbnail uzantısı desteklenmiyor ({ext}). Desteklenen: {', '.join(SUPPORTED_THUMB_EXTS)}. Atlanıyor.")
        return False
    size_mb = os.path.getsize(thumb_path) / (1024 * 1024)
    if size_mb > MAX_THUMB_SIZE_MB:
        if log_cb: log_cb(f"Thumbnail {size_mb:.2f} MB (> {MAX_THUMB_SIZE_MB} MB). Atlanıyor.")
        return False
    if PIL_AVAILABLE:
        try:
            with Image.open(thumb_path) as im:
                w, h = im.size
                if w < MIN_THUMB_WIDTH or h < MIN_THUMB_HEIGHT:
                    if log_cb: log_cb(f"Thumbnail küçük ({w}x{h}). Önerilen min {MIN_THUMB_WIDTH}x{MIN_THUMB_HEIGHT}. Atlanıyor.")
                    return False
        except Exception:
            if log_cb: log_cb("Thumbnail açılırken hata oluştu, atlanıyor.")
            return False
    return True

def normalize_playlist_id(x: str) -> str:
    """Tam URL gelirse list= parametresinden ID'yi çıkarır, boşlukları temizler."""
    s = (x or "").strip()
    if not s:
        return ""
    if s.startswith("http://") or s.startswith("https://"):
        qs = parse_qs(urlparse(s).query)
        pid = (qs.get("list") or [""])[0]
        return pid.strip()
    return s  # zaten ID gibi

def playlist_exists(youtube, playlist_id: str) -> bool:
    """Playlist gerçekten mevcut mu ve erişimin var mı?"""
    if not playlist_id:
        return False
    try:
        resp = youtube.playlists().list(part="id", id=playlist_id, maxResults=1).execute()
        return bool(resp.get("items"))
    except HttpError:
        return False

def list_my_playlists(youtube, log_cb=None):
    """Hesaptaki tüm oynatma listelerini başlık + ID olarak logla."""
    page_token = None
    count = 0
    while True:
        resp = youtube.playlists().list(
            part="id,snippet",
            mine=True, maxResults=50, pageToken=page_token
        ).execute()
        for it in resp.get("items", []):
            count += 1
            title = it["snippet"]["title"]
            pid = it["id"]
            if log_cb: log_cb(f"[PL{count:02}] {title}  |  ID: {pid}")
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    if count == 0 and log_cb:
        log_cb("Bu hesapta hiç playlist bulunamadı.")

# ===================== Yükleme Akışı =====================

def upload_video(youtube, row: pd.Series, progress_cb=None, log_cb=None) -> Dict[str, Any]:
    video_path = str(row.get("video_path", "")).strip()
    if not os.path.exists(video_path):
        raise FileNotFoundError(f"Video bulunamadı: {video_path}")

    body = build_body(row)
    media = MediaFileUpload(video_path, chunksize=8 * 1024 * 1024, resumable=True)

    request = youtube.videos().insert(
        part="snippet,status",
        body=body,
        media_body=media
    )

    response = None
    last_progress = 0
    while True:
        try:
            status, response = request.next_chunk()
        except HttpError as e:
            if log_cb: log_cb(f"HTTP Hatası: {e}")
            time.sleep(5)
            continue
        except Exception as e:
            raise e

        if status:
            pct = int(status.progress() * 100)
            if pct != last_progress:
                last_progress = pct
                if progress_cb:
                    progress_cb(pct)
        if response is not None:
            break

    video_id = response.get("id")
    if log_cb: log_cb(f"Yüklendi: https://www.youtube.com/watch?v={video_id}")

    # ---- SHORTS ise: API ile özel thumbnail atlama ----
    is_short = detect_is_short(row, log_cb=log_cb)
    if is_short and log_cb:
        log_cb("Shorts tespit edildi (<60 sn veya işaretlendi). API üzerinden özel thumbnail yükleme atlanacak.")

    # ---- Thumbnail (Shorts değilse ve valide ise) ----
    if not is_short:
        thumb_path = str(row.get("thumbnail_path", "")).strip()
        if validate_thumbnail(thumb_path, log_cb=log_cb):
            try:
                youtube.thumbnails().set(
                    videoId=video_id,
                    media_body=thumb_path
                ).execute()
                if log_cb: log_cb("Thumbnail ayarlandı.")
            except HttpError as e:
                if log_cb: log_cb(f"Thumbnail hatası: {e}")

    # ---- Zamanlama (scheduled) ----
    privacy = str(row.get("privacyStatus", "public")).strip().lower()
    publish_at = str(row.get("publishAt", "")).strip()
    if privacy == "scheduled" and publish_at:
        try:
            youtube.videos().update(
                part="status",
                body={
                    "id": video_id,
                    "status": {
                        "privacyStatus": "private",
                        "publishAt": publish_at
                    }
                }
            ).execute()
            if log_cb: log_cb(f"Yayın zamanı ayarlandı: {publishAt}")
        except HttpError as e:
            if log_cb: log_cb(f"Zamanlama hatası: {e}")

    # ---- Playlist ----
    raw_playlist = str(row.get("playlist_id", "")).strip()
    pl_id = normalize_playlist_id(raw_playlist)

    if pl_id:
        if playlist_exists(youtube, pl_id):
            try:
                youtube.playlistItems().insert(
                    part="snippet",
                    body={
                        "snippet": {
                            "playlistId": pl_id,
                            "resourceId": {"kind": "youtube#video", "videoId": video_id}
                        }
                    }
                ).execute()
                if log_cb: log_cb(f"Playlist'e eklendi: {pl_id}")
            except HttpError as e:
                if log_cb: log_cb(f"Playlist ekleme hatası: {e}")
        else:
            if log_cb: log_cb(f"Uyarı: Playlist bulunamadı/erişim yok: {pl_id}. ID’yi ve kanalı kontrol et.")

    return {"video_id": video_id}

# ===================== Worker ve GUI =====================

class UploadWorker(threading.Thread):
    def __init__(self, app, task_queue: queue.Queue, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.app = app
        self.task_queue = task_queue
        self.daemon = True

    def run(self):
        youtube = None
        try:
            youtube = get_youtube_service()
        except Exception as e:
            self.app.log(f"Yetkilendirme/Youtube servisi hatası: {e}")
            return

        while True:
            try:
                idx = self.task_queue.get(timeout=1)
            except queue.Empty:
                if self.app.stop_flag:
                    return
                continue

            if idx is None:
                self.task_queue.task_done()
                return

            row = self.app.df.iloc[idx]
            self.app.set_status(idx, "Yükleniyor...")
            try:
                def pg(p): self.app.set_progress(idx, p)
                def lg(msg): self.app.log(f"[{idx+1}] {msg}")

                res = upload_video(youtube, row, progress_cb=pg, log_cb=lg)
                self.app.set_status(idx, "Tamamlandı")
                self.app.mark_url(idx, f"https://www.youtube.com/watch?v={res['video_id']}")
            except Exception as e:
                self.app.set_status(idx, "Hata")
                self.app.log(f"[{idx+1}] Hata: {e}")
            finally:
                self.task_queue.task_done()
                if self.app.stop_flag:
                    return

class App:
    def __init__(self, root):
        self.root = root
        self.root.title("YouTube Toplu Yükleyici")
        self.root.geometry("1100x720")
        self.style = Style("flatly")
        self.stop_flag = False

        self.df: Optional[pd.DataFrame] = None
        self.file_path_var = tk.StringVar()
        self.concurrent_var = tk.IntVar(value=2)

        self.task_queue = queue.Queue()
        self.workers: List[UploadWorker] = []

        self.build_gui()

    def build_gui(self):
        top = ttk.Frame(self.root, padding=12)
        top.pack(side=tk.TOP, fill=tk.X)

        ttk.Label(top, text="Excel/CSV Dosyası:", width=18).pack(side=tk.LEFT)
        ttk.Entry(top, textvariable=self.file_path_var).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=6)

        ttk.Button(top, text="Dosya Seç", command=self.choose_file, bootstyle=PRIMARY).pack(side=tk.LEFT, padx=4)
        ttk.Button(top, text="Google'da Yetkilendir", command=self.authorize, bootstyle=INFO).pack(side=tk.LEFT, padx=4)

        ctrl = ttk.Frame(self.root, padding=(12, 0, 12, 12))
        ctrl.pack(side=tk.TOP, fill=tk.X)

        ttk.Label(ctrl, text="Eşzamanlı İş (1-5):").pack(side=tk.LEFT)
        ttk.Spinbox(ctrl, from_=1, to=5, textvariable=self.concurrent_var, width=5).pack(side=tk.LEFT, padx=6)

        ttk.Button(ctrl, text="Yüklemeyi Başlat", command=self.start_uploads, bootstyle=SUCCESS).pack(side=tk.LEFT, padx=4)
        ttk.Button(ctrl, text="Durdur", command=self.stop_uploads, bootstyle=WARNING).pack(side=tk.LEFT, padx=4)

        # >> Yeni buton: Oynatma listelerimi göster
        ttk.Button(ctrl, text="Oynatma Listelerimi Göster", command=self.show_playlists, bootstyle=SECONDARY).pack(side=tk.LEFT, padx=4)

        # Tablo
        self.tree = ttk.Treeview(self.root,
                                 columns=("title","privacy","status","progress","url"),
                                 show="headings", height=12)
        for cid, text in [("title","Başlık"), ("privacy","Gizlilik"),
                          ("status","Durum"), ("progress","İlerleme %"), ("url","YouTube URL")]:
            self.tree.heading(cid, text=text)
        self.tree.column("title", width=420, anchor=tk.W)
        self.tree.column("privacy", width=110, anchor=tk.CENTER)
        self.tree.column("status", width=140, anchor=tk.CENTER)
        self.tree.column("progress", width=120, anchor=tk.CENTER)
        self.tree.column("url", width=320, anchor=tk.W)
        self.tree.pack(fill=tk.BOTH, expand=True, padx=12)

        # Alt: Log
        bottom = ttk.Frame(self.root, padding=12)
        bottom.pack(side=tk.BOTTOM, fill=tk.BOTH)
        ttk.Label(bottom, text="Log / Çıktı:").pack(anchor=tk.W)
        self.log_text = tk.Text(bottom, height=9)
        self.log_text.pack(fill=tk.BOTH, expand=True)

    def log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_text.insert(tk.END, f"[{ts}] {msg}\n")
        self.log_text.see(tk.END)

    def choose_file(self):
        path = filedialog.askopenfilename(
            title="Excel/CSV seçin",
            filetypes=[("Excel","*.xlsx *.xls"), ("CSV","*.csv")]
        )
        if path:
            try:
                df = load_table(path)
                self.df = df
                self.file_path_var.set(path)
                self.populate_tree()
                self.log(f"{len(df)} satır yüklendi.")
            except Exception as e:
                messagebox.showerror("Hata", str(e))

    def authorize(self):
        try:
            _ = get_youtube_service()
            messagebox.showinfo("Tamam", "Yetkilendirme başarılı.")
        except Exception as e:
            messagebox.showerror("Yetkilendirme Hatası", str(e))

    def populate_tree(self):
        for i in self.tree.get_children():
            self.tree.delete(i)
        if self.df is None:
            return
        for idx, row in self.df.iterrows():
            title = str(row.get("title", ""))
            privacy = str(row.get("privacyStatus",""))
            self.tree.insert("", tk.END, iid=str(idx),
                             values=(title, privacy, "Hazır", "0", ""))

    def set_status(self, idx: int, status: str):
        vals = list(self.tree.item(str(idx), "values"))
        if len(vals) == 5:
            vals[2] = status
            self.tree.item(str(idx), values=vals)

    def set_progress(self, idx: int, percent: int):
        vals = list(self.tree.item(str(idx), "values"))
        if len(vals) == 5:
            vals[3] = str(percent)
            self.tree.item(str(idx), values=vals)

    def mark_url(self, idx: int, url: str):
        vals = list(self.tree.item(str(idx), "values"))
        if len(vals) == 5:
            vals[4] = url
            self.tree.item(str(idx), values=vals)

    def start_uploads(self):
        if self.df is None or self.df.empty:
            messagebox.showwarning("Uyarı", "Önce Excel/CSV yükleyin.")
            return
        self.stop_flag = False
        for i in range(len(self.df)):
            self.task_queue.put(i)
        conc = max(1, min(5, int(self.concurrent_var.get() or 2)))
        for _ in range(conc):
            w = UploadWorker(self, self.task_queue)
            w.start()
            self.workers.append(w)
        self.log(f"Yükleme başladı. Eşzamanlı işler: {conc}")

    def stop_uploads(self):
        self.stop_flag = True
        while True:
            try:
                self.task_queue.get_nowait()
                self.task_queue.task_done()
            except queue.Empty:
                break
        self.log("Durdurma işareti verildi.")

    # ---- Yeni: Playlistleri göster ----
    def show_playlists(self):
        try:
            yt = get_youtube_service()
            self.log("Playlistler alınıyor...")
            list_my_playlists(yt, log_cb=self.log)
        except Exception as e:
            messagebox.showerror("Hata", str(e))


def main():
    root = tk.Tk()
    app = App(root)
    root.mainloop()

if __name__ == "__main__":
    main()
