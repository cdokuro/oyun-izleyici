#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PC & Oyun İzleyici — Hafif Arka Plan Servisi (v4.3.0)

PySide6 / Qt YÜKLEMEZ! Yalnızca standart kütüphane + psutil + SQLite + Win32/Linux
yerel çağrılarıyla ~20-25 MB RAM bandında 7/24 sessizce çalışır:

  1. Aktif uygulama ve web sitesi (domain) süre takibi (boşta/idle algılamalı)
  2. Program & oyun kurulum / güncelleme / kaldırma takibi (Registry, Steam, Epic, pacman/flatpak)
  3. Kritik sistem değişikliği takibi (GPU sürücüsü, OS/kernel, başlangıç öğeleri, hosts, PATH, indirilenler)
  4. Uygulama bazlı RAM, CPU, GPU % ve adanmış VRAM tüketim ölçümü (Win32 PDH / Linux fdinfo)
  5. Otomatik oyun algılama → oyun açılınca canlı HUD'u (--hud) başlatır, bitince kapatır
  6. Özel GitHub deposuna çoklu PC senkronizasyonu (oyunlar + aktivite özeti + kritik olaylar)
  7. Doğal Türkçe günlük / haftalık / aylık özet raporu üretici (SummaryEngine)
"""

import base64
import ctypes
import getpass
import glob
import hashlib
import json
import os
import platform
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict

APP_VERSION = "4.3.0"  # MAJOR.MINOR.PATCH — güncelleme rozeti yalnızca MAJOR veya MINOR yükselince çıkar
IS_WIN = sys.platform.startswith("win")

if IS_WIN:
    import msvcrt
    import winreg
    fcntl = None
    _LOCAL = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
    _ROAM = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Roaming")
    DATA_DIR = os.path.join(_LOCAL, "oyun-izleyici")
    CONFIG_DIR = os.path.join(_ROAM, "oyun-izleyici")
    INSTALLED_BIN = os.path.join(DATA_DIR, "app", "oyun_izleyici.pyw")
    DAEMON_BIN = os.path.join(DATA_DIR, "app", "pc_izleyici.py")
    MANGOHUD_DIR = os.path.join(DATA_DIR, "canli")
else:
    import fcntl
    winreg = None
    _LOCAL = _ROAM = None
    DATA_DIR = os.path.join(os.path.expanduser("~"), ".local", "share", "oyun-izleyici")
    CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".config", "oyun-izleyici")
    INSTALLED_BIN = os.path.join(os.path.expanduser("~"), ".local", "bin", "oyun-izleyici")
    DAEMON_BIN = os.path.join(os.path.expanduser("~"), ".local", "bin", "pc_izleyici.py")
    MANGOHUD_DIR = "/tmp/mangohud_live"

DB_PATH = os.path.join(DATA_DIR, "oyunlar.db")
BACKUP_DIR = os.path.join(DATA_DIR, "yedekler")
REPO_DIR = os.path.join(DATA_DIR, "depo")
MACHINE_CONF = os.path.join(CONFIG_DIR, "makine.conf")
TOKEN_PATH = os.path.join(CONFIG_DIR, "github.token")
KOMUTA_KEY_PATH = os.path.join(CONFIG_DIR, "komuta.key")
HUD_STOP_FLAG = os.path.join(DATA_DIR, ".hud_stop")
NOTIFY_FILE = os.path.join(DATA_DIR, ".notify.json")

IDLE_THRESHOLD_SEC = 120.0
POLL_ACTIVE_SEC = 2.0
POLL_RESOURCE_SEC = 30.0
POLL_BROWSER_SEC = 25.0
POLL_AUDIT_SEC = 300.0
POLL_HEARTBEAT_SEC = 60.0
POLL_INGEST_SEC = 300.0
FLUSH_SEC = 30.0
SYNC_INTERVAL_SEC = 15 * 60
HUD_IDLE_GRACE_SEC = 95.0
SYNC_ACTIVITY_DAYS = 90
SYNC_MAX_EVENTS = 1500

try:
    import psutil
except ImportError:
    psutil = None


# =============================================================================
# YARDIMCI FONKSİYONLAR & MAKİNE AYARLARI
# =============================================================================
def _hidden():
    if IS_WIN:
        return {"creationflags": 0x08000000}
    return {}


def _slug(text):
    tr = str.maketrans("çğıöşüÇĞİÖŞÜ", "cgiosuCGIOSU")
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").translate(tr).lower()).strip("-")
    return s or "pc"


def normalize_repo_slug(raw):
    s = (raw or "").strip()
    if not s:
        return ""
    m = re.match(r"^(?:git@github\.com:|https?://github\.com/)([^/\s]+/[^/\s]+?)(?:\.git)?/?$", s, re.I)
    if m:
        return m.group(1)
    if re.match(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", s):
        return s[:-4] if s.endswith(".git") else s
    return ""


def load_machine_config():
    cfg = {}
    try:
        with open(MACHINE_CONF, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    cfg[k.strip()] = v.strip()
    except OSError:
        pass
    host = platform.node() or "pc"
    cfg.setdefault("kimlik", _slug(host))
    cfg.setdefault("ad", host)
    cfg.setdefault("senkron", "acik")
    cfg.setdefault("hud_oto", "acik")
    cfg.setdefault("hud_hep_acik", "kapali")
    cfg.setdefault("hafif_mod", "acik")
    return cfg


def save_machine_config(updates):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    lines, done = [], set()
    try:
        with open(MACHINE_CONF, encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if s and not s.startswith("#") and "=" in s:
                    k = s.split("=", 1)[0].strip()
                    if k in updates:
                        lines.append(f"{k} = {updates[k]}\n")
                        done.add(k)
                        continue
                lines.append(line)
    except OSError:
        pass
    for k, v in updates.items():
        if k not in done:
            lines.append(f"{k} = {v}\n")
    tmp = MACHINE_CONF + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.writelines(lines)
    os.replace(tmp, MACHINE_CONF)


def read_github_token():
    try:
        with open(TOKEN_PATH, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def read_komuta_key():
    try:
        with open(KOMUTA_KEY_PATH, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def _read_live_fps_temp():
    """Oyun açıkken canlı MangoHud/PresentMon CSV son satırından anlık FPS ve sıcaklık okur (0 maliyet)."""
    try:
        csvs = glob.glob(os.path.join(MANGOHUD_DIR, "*.csv"))
        if not csvs:
            return None, None, None
        newest = max(csvs, key=os.path.getmtime)
        if time.time() - os.path.getmtime(newest) > 8.0:
            return None, None, None
        with open(newest, "rb") as f:
            f.seek(0, os.SEEK_END)
            sz = f.tell()
            f.seek(max(0, sz - 2048))
            chunk = f.read().decode("utf-8", errors="ignore").splitlines()
        with open(newest, "r", encoding="utf-8", errors="ignore") as f:
            hdr = ""
            for _ in range(5):
                ln = f.readline().strip()
                if "fps" in ln.lower() and "," in ln:
                    hdr = ln
                    break
        if not hdr or not chunk:
            return None, None, None
        cols = [c.strip().lower() for c in hdr.split(",")]
        for ln in reversed(chunk):
            parts = ln.strip().split(",")
            if len(parts) >= len(cols):
                row = dict(zip(cols, parts))
                def _f(k):
                    try:
                        v = float(row.get(k, ""))
                        return v if v > 0 else None
                    except ValueError:
                        return None
                return _f("fps"), _f("gpu_temp"), _f("cpu_temp")
    except Exception:
        pass
    return None, None, None


class KomutaPusher:
    """serhatbahar.com/komuta (Pi-hole API) için sessiz 60 sn canlı sağlık nabzı ve toplu aktarım."""

    def __init__(self, store, cfg):
        self.store = store
        self.cfg = cfg
        self.mid = cfg["kimlik"]
        self._busy_hb = False
        self._busy_ing = False
        self._prev_disk_bytes = None
        self._prev_disk_ts = None
        self._last_stutter_ts = 0.0

    def _endpoint(self):
        cfg = load_machine_config()
        url = (cfg.get("komuta_url") or "").strip().rstrip("/")
        key = read_komuta_key() or (cfg.get("komuta_key") or "").strip()
        return url, key, cfg

    def _post(self, url, key, payload, timeout=8):
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        req = urllib.request.Request(
            url, data=raw, method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Komuta-Key": key,
                "User-Agent": f"oyun-izleyici/{APP_VERSION}",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read(256)

    def send_heartbeat(self, active_app="", active_game="", agent_ram_mb=0.0, last_samples=None):
        if self._busy_hb:
            return
        url, key, cfg = self._endpoint()
        if not url or not key:
            return

        ram_pct = ram_used = ram_total = cpu_load = disk_pct = uptime_s = None
        disk_label = "C:" if IS_WIN else "/"
        warnings = []
        stutter_event = None
        if psutil:
            try:
                vm = psutil.virtual_memory()
                ram_pct = round(vm.percent, 1)
                ram_used = round(vm.used / (1024 ** 3), 2)
                ram_total = round(vm.total / (1024 ** 3), 1)
                cpu_load = round(psutil.cpu_percent(interval=None), 1)
                uptime_s = round(time.time() - psutil.boot_time(), 0)
                root_path = os.environ.get("SystemDrive", "C:") + "\\" if IS_WIN else "/"
                du = psutil.disk_usage(root_path)
                disk_pct = round(du.percent, 1)
                if disk_pct >= 85:
                    warnings.append(f"Sistem diski ({disk_label}) %{disk_pct:.0f} dolu")
                # Oyun sırasında arka plan disk/CPU sıçraması (Stutter Dedektifi)
                dio = psutil.disk_io_counters()
                now_m = time.monotonic()
                disk_mbps = 0.0
                if dio:
                    cur_b = dio.read_bytes + dio.write_bytes
                    if self._prev_disk_bytes is not None and now_m > self._prev_disk_ts:
                        disk_mbps = max(0.0, (cur_b - self._prev_disk_bytes) / ((now_m - self._prev_disk_ts) * 1048576.0))
                    self._prev_disk_bytes = cur_b
                    self._prev_disk_ts = now_m
                if active_game and last_samples and (time.time() - self._last_stutter_ts) > 180:
                    culprits = [
                        (app, m) for app, m in last_samples.items()
                        if app != active_game and (m.get("cpu_pct", 0) >= 18.0 or (disk_mbps >= 80.0 and m.get("cpu_pct", 0) >= 5.0))
                    ]
                    if culprits:
                        culprits.sort(key=lambda kv: kv[1].get("cpu_pct", 0), reverse=True)
                        c_app, cm = culprits[0]
                        self._last_stutter_ts = time.time()
                        stutter_event = {
                            "ts": time.time(),
                            "title": f"{active_game} sırasında arka plan yükü: {c_app}",
                            "detail": f"Oyun açıkken {c_app} işlemciyi %{cm.get('cpu_pct', 0):.0f} (RAM {fmt_mb_tr(cm.get('ram_mb', 0))}, toplam disk {disk_mbps:.0f} MB/s) kullandı.",
                        }
                        self.store.add_event("stutter", stutter_event["title"], stutter_event["detail"], "important")
            except Exception:
                pass

        fps, gpu_temp, cpu_temp = _read_live_fps_temp()
        if gpu_temp is None and not IS_WIN:
            # Linux amdgpu / thermal_zone hızlı okuma
            for p in glob.glob("/sys/class/drm/card*/device/hwmon/hwmon*/temp1_input"):
                try:
                    gpu_temp = round(int(open(p).read().strip()) / 1000.0, 1)
                    break
                except OSError:
                    pass

        payload = {
            "machine_id": self.mid,
            "name": cfg.get("ad") or self.mid,
            "gpu_temp": gpu_temp,
            "cpu_temp": cpu_temp,
            "cpu_load": cpu_load,
            "ram_pct": ram_pct,
            "ram_used_gb": ram_used,
            "ram_total_gb": ram_total,
            "disk_pct": disk_pct,
            "disk_label": disk_label,
            "active_app": active_app,
            "active_game": active_game,
            "fps": fps,
            "agent_ram_mb": round(agent_ram_mb, 1),
            "uptime_s": uptime_s,
            "warnings": warnings,
            "stutter_event": stutter_event,
        }
        self._busy_hb = True

        def _work():
            try:
                self._post(f"{url}/heartbeat", key, payload)
            except Exception:
                pass
            finally:
                self._busy_hb = False

        threading.Thread(target=_work, daemon=True).start()

    def send_ingest(self, timeline_events=None):
        if self._busy_ing:
            return
        url, key, _cfg = self._endpoint()
        if not url or not key:
            return
        bundle = self.store.export_activity_bundle()
        evts = [
            json.loads(ln) for ln in self.store.export_events_lines().splitlines() if ln.strip()
        ]
        tl = (timeline_events or [])[:500]
        payload = {
            "machine_id": self.mid,
            "activity": bundle.get("activity") or [],
            "resources": bundle.get("resources") or [],
            "events": evts[-250:],
            "timeline": tl,
        }
        self._busy_ing = True

        def _work():
            try:
                self._post(f"{url}/ingest", key, payload, timeout=15)
                # Başarılı gönderim sonrası watermark'ı kaydır
                if tl:
                    try:
                        self.store.set_timeline_watermark(tl[-1]["event_id"])
                    except Exception:
                        pass
            except Exception:
                pass
            finally:
                self._busy_ing = False

        threading.Thread(target=_work, daemon=True).start()


def fmt_duration_tr(sec, short=False):
    sec = max(0, int(round(sec or 0)))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h >= 1:
        return f"{h} sa {m} dk" if m else f"{h} sa"
    if m >= 1:
        return f"{m} dk" if short else (f"{m} dk {s} sn" if s else f"{m} dk")
    return f"{s} sn"


def fmt_mb_tr(mb):
    if mb is None or mb <= 0:
        return "0 MB"
    if mb >= 1024:
        return f"{mb / 1024.0:.2f} GB"
    return f"{mb:.0f} MB"


def today_str(ts=None):
    return time.strftime("%Y-%m-%d", time.localtime(ts if ts is not None else time.time()))


def day_range_cutoff(days):
    if not days or days <= 0:
        return "1970-01-01"
    return time.strftime("%Y-%m-%d", time.localtime(time.time() - (days - 1) * 86400))


# =============================================================================
# UYGULAMA & WEB SİTESİ SINIFLANDIRICI
# =============================================================================
BROWSER_EXES = {
    "chrome": "Chrome",
    "msedge": "Microsoft Edge",
    "firefox": "Firefox",
    "zen": "Zen Browser",
    "brave": "Brave",
    "opera": "Opera",
    "vivaldi": "Vivaldi",
    "chromium": "Chromium",
    "waterfox": "Waterfox",
    "librewolf": "LibreWolf",
    "arc": "Arc",
    "floorp": "Floorp",
}

KNOWN_APPS = {
    "code": ("VS Code", "Geliştirme"),
    "antigravity": ("Antigravity", "Geliştirme"),
    "cursor": ("Cursor", "Geliştirme"),
    "pycharm64": ("PyCharm", "Geliştirme"),
    "idea64": ("IntelliJ IDEA", "Geliştirme"),
    "devenv": ("Visual Studio", "Geliştirme"),
    "windowsterminal": ("Windows Terminal", "Geliştirme"),
    "wt": ("Windows Terminal", "Geliştirme"),
    "powershell": ("PowerShell", "Geliştirme"),
    "pwsh": ("PowerShell", "Geliştirme"),
    "cmd": ("Komut İstemi", "Geliştirme"),
    "kitty": ("Kitty Terminal", "Geliştirme"),
    "alacritty": ("Alacritty", "Geliştirme"),
    "konsole": ("Konsole", "Geliştirme"),
    "discord": ("Discord", "İletişim"),
    "telegram": ("Telegram", "İletişim"),
    "whatsapp": ("WhatsApp", "İletişim"),
    "slack": ("Slack", "İletişim"),
    "ms-teams": ("Teams", "İletişim"),
    "teams": ("Teams", "İletişim"),
    "zoom": ("Zoom", "İletişim"),
    "spotify": ("Spotify", "Medya"),
    "vlc": ("VLC", "Medya"),
    "mpv": ("mpv", "Medya"),
    "obs64": ("OBS Studio", "Medya"),
    "steam": ("Steam", "Oyun Platformu"),
    "steamwebhelper": ("Steam", "Oyun Platformu"),
    "epicgameslauncher": ("Epic Games", "Oyun Platformu"),
    "galaxyclient": ("GOG Galaxy", "Oyun Platformu"),
    "eadesktop": ("EA App", "Oyun Platformu"),
    "upc": ("Ubisoft Connect", "Oyun Platformu"),
    "battle.net": ("Battle.net", "Oyun Platformu"),
    "explorer": ("Dosya Gezgini", "Sistem"),
    "taskmgr": ("Görev Yöneticisi", "Sistem"),
    "systemsettings": ("Windows Ayarları", "Sistem"),
    "regedit": ("Kayıt Defteri", "Sistem"),
    "radeonsoftware": ("AMD Adrenalin", "Sistem"),
    "nvcontainer": ("NVIDIA", "Sistem"),
    "notepad": ("Not Defteri", "Araç"),
    "notepad++": ("Notepad++", "Araç"),
    "obsidian": ("Obsidian", "Araç"),
    "notion": ("Notion", "Araç"),
    "winword": ("Word", "Araç"),
    "excel": ("Excel", "Araç"),
    "powerpnt": ("PowerPoint", "Araç"),
    "photoshop": ("Photoshop", "Tasarım"),
    "blender": ("Blender", "Tasarım"),
    "figma": ("Figma", "Tasarım"),
    "anydesk": ("AnyDesk", "Araç"),
}

GAME_PATH_MARKERS = (
    "steamapps\\common\\", "steamapps/common/",
    "epic games\\", "epic games/",
    "xboxgames\\", "gog games\\", "gog galaxy\\games\\",
    "riot games\\", "ea games\\", "origin games\\",
    "ubisoft game launcher\\games\\", "battle.net\\",
    "program files (x86)\\steam\\steamapps",
)

NON_GAME_EXES = {
    "steam", "steamwebhelper", "steamservice", "gameoverlayui", "epicgameslauncher",
    "epicwebhelper", "crashreportclient", "ue4prereqsetup_x64", "vc_redist.x64",
    "easyanticheat_eos", "beservice", "vgc", "riotclientservices", "riotclientux",
    "eadesktop", "eabackgroundservice", "upc", "battle.net", "galaxyclient",
    "python", "pythonw", "cmd", "powershell", "conhost", "dwm", "explorer",
    "searchhost", "shellexperiencehost", "startmenuexperiencehost", "lockapp",
    "applicationframehost", "textinputhost", "widgetservice", "presentmon",
    "svchost", "csrss", "lsass", "wininit", "services", "system", "idle",
    "msedgewebview2", "webviewhost", "runtimebroker", "dllhost", "sihost",
    "taskhostw", "fontdrvhost", "audiodg", "spoolsv", "wmiprvse", "ctfmon",
    "nvidia share", "nvcontainer", "radeonsoftware", "amdrsserv", "amdow",
    "oyun-izleyici", "oyun_izleyici", "pc_izleyici",
}

SITE_TITLE_PATTERNS = [
    (re.compile(r"\bYouTube\b", re.I), "youtube.com"),
    (re.compile(r"\bGitHub\b", re.I), "github.com"),
    (re.compile(r"\bReddit\b", re.I), "reddit.com"),
    (re.compile(r"\bTechnopat\b", re.I), "technopat.net"),
    (re.compile(r"Donanım\s?Haber|DonanimHaber", re.I), "donanimhaber.com"),
    (re.compile(r"\bTwitch\b", re.I), "twitch.tv"),
    (re.compile(r"\bKick\b\s*[-–|]|\bKick\.com\b", re.I), "kick.com"),
    (re.compile(r"\bChatGPT\b", re.I), "chatgpt.com"),
    (re.compile(r"\bClaude\b\s*($|[-–|])", re.I), "claude.ai"),
    (re.compile(r"\bGoogle Gemini\b|\bGemini\b\s*[-–|]", re.I), "gemini.google.com"),
    (re.compile(r"\bNexus Mods\b", re.I), "nexusmods.com"),
    (re.compile(r"\bCalypso\s*Çeviri\b", re.I), "calypsoceviri.com"),
    (re.compile(r"\bApex\s*(Çeviri|Yama)\b", re.I), "apexyama.com"),
    (re.compile(r"\bSteam\b", re.I), "store.steampowered.com"),
    (re.compile(r"\bStack Overflow\b", re.I), "stackoverflow.com"),
    (re.compile(r"\bWikipedia\b|\bVikipedi\b", re.I), "wikipedia.org"),
    (re.compile(r"\bGmail\b", re.I), "mail.google.com"),
    (re.compile(r"\bGoogle Drive\b", re.I), "drive.google.com"),
    (re.compile(r"\bGoogle Docs\b|\bGoogle Dokümanlar\b", re.I), "docs.google.com"),
    (re.compile(r"Google\s*(Arama|Search)\b", re.I), "google.com"),
    (re.compile(r"\bWhatsApp\b", re.I), "web.whatsapp.com"),
    (re.compile(r"\bInstagram\b", re.I), "instagram.com"),
    (re.compile(r"\bLinkedIn\b", re.I), "linkedin.com"),
    (re.compile(r"\bNetflix\b", re.I), "netflix.com"),
    (re.compile(r"\bSpotify\b", re.I), "open.spotify.com"),
    (re.compile(r"\bDisney\+", re.I), "disneyplus.com"),
    (re.compile(r"\bPrime Video\b", re.I), "primevideo.com"),
    (re.compile(r"\bDiscord\b", re.I), "discord.com"),
    (re.compile(r"\bTwitter\b|\s/\sX\b|\bon X\b", re.I), "x.com"),
    (re.compile(r"\bEkşi Sözlük\b|\bekşi sözlük\b", re.I), "eksisozluk.com"),
    (re.compile(r"\bSahibinden\b", re.I), "sahibinden.com"),
    (re.compile(r"\bHepsiburada\b", re.I), "hepsiburada.com"),
    (re.compile(r"\bTrendyol\b", re.I), "trendyol.com"),
    (re.compile(r"\bAmazon\b", re.I), "amazon.com.tr"),
    (re.compile(r"\bN11\b", re.I), "n11.com"),
    (re.compile(r"\bAkakçe\b", re.I), "akakce.com"),
    (re.compile(r"\bEpey\b", re.I), "epey.com"),
    (re.compile(r"\bIMDb\b", re.I), "imdb.com"),
    (re.compile(r"\bMetacritic\b", re.I), "metacritic.com"),
    (re.compile(r"\bHowLongToBeat\b", re.I), "howlongtobeat.com"),
    (re.compile(r"\bProtonDB\b", re.I), "protondb.com"),
    (re.compile(r"\bArchWiki\b|\bArch Linux\b", re.I), "wiki.archlinux.org"),
    (re.compile(r"\bLocalhost\b|127\.0\.0\.1", re.I), "localhost"),
]

BROWSER_SUFFIX_RE = re.compile(
    r"\s*[-–—|]\s*(Google Chrome|Microsoft\s*Edge|Mozilla Firefox|Firefox|Zen Browser|Zen|"
    r"Brave|Opera|Opera GX|Vivaldi|Chromium|Arc|Floorp|LibreWolf)(\s*\(.*?\))?\s*$",
    re.I,
)
DOMAIN_IN_TITLE_RE = re.compile(
    r"\b([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.(?:com|net|org|io|dev|ai|gg|tv|tr|com\.tr|gov\.tr|edu\.tr|app|co|me|xyz|info))\b",
    re.I,
)

GAME_TITLE_OVERRIDES = {
    "pbz": "Phantom Blade Zero",
    "cs2": "Counter-Strike 2",
    "deadlock": "Deadlock",
    "eldenring": "Elden Ring",
    "cyberpunk2077": "Cyberpunk 2077",
    "bg3": "Baldur's Gate 3",
    "bg3_dx11": "Baldur's Gate 3",
    "rdr2": "Red Dead Redemption 2",
    "witcher3": "The Witcher 3",
    "doomthedarkages": "DOOM: The Dark Ages",
    "doometernalx64vk": "DOOM Eternal",
}


def clean_domain(url):
    try:
        u = urllib.parse.urlparse(url)
        host = (u.hostname or "").lower().strip(".")
        if host.startswith("www."):
            host = host[4:]
        if not host or "." not in host and host != "localhost":
            return ""
        return host
    except Exception:
        return ""


def classify_process(exe_name, exe_path="", known_games=None):
    """(temiz_ad, kategori, is_game, is_browser) döndürür."""
    stem = os.path.splitext(os.path.basename(exe_name or ""))[0]
    low = stem.lower()
    if low in BROWSER_EXES:
        return BROWSER_EXES[low], "Tarayıcı", False, True
    if low in KNOWN_APPS:
        name, cat = KNOWN_APPS[low]
        return name, cat, False, False
    path_low = (exe_path or "").lower()
    if low not in NON_GAME_EXES:
        if low in GAME_TITLE_OVERRIDES:
            return GAME_TITLE_OVERRIDES[low], "Oyun", True, False
        if any(m in path_low for m in GAME_PATH_MARKERS):
            # Klasör adını kullanmak genelde PBZ.exe'den daha anlamlı olur
            nice = _game_name_from_path(exe_path, stem)
            return nice, "Oyun", True, False
        if known_games and low in known_games:
            return known_games[low], "Oyun", True, False
    if not stem:
        return "Bilinmeyen", "Diğer", False, False
    nice = stem if any(c.isupper() for c in stem[1:]) else stem.replace("_", " ").replace("-", " ").title()
    return nice, "Diğer", False, False


def _game_name_from_path(exe_path, fallback_stem):
    low = (exe_path or "").lower()
    for marker in ("steamapps\\common\\", "steamapps/common/", "epic games\\", "epic games/",
                   "xboxgames\\", "gog games\\", "riot games\\"):
        idx = low.find(marker)
        if idx != -1:
            rest = exe_path[idx + len(marker):]
            folder = re.split(r"[\\/]", rest)[0].strip()
            if folder:
                fl = folder.lower()
                if fl in GAME_TITLE_OVERRIDES:
                    return GAME_TITLE_OVERRIDES[fl]
                if fallback_stem.lower() in GAME_TITLE_OVERRIDES:
                    return GAME_TITLE_OVERRIDES[fallback_stem.lower()]
                return folder
    return GAME_TITLE_OVERRIDES.get(fallback_stem.lower(), fallback_stem)


def strip_browser_suffix(title):
    t = (title or "").strip()
    t = BROWSER_SUFFIX_RE.sub("", t).strip()
    # Başındaki bildirim sayısını temizle: "(3) YouTube" -> "YouTube"
    t = re.sub(r"^\(\d+\+?\)\s*", "", t)
    return t


# =============================================================================
# TIMELINE COLLECTOR — Film Şeridi olay filtresi (saf mantık, I/O'suz)
# =============================================================================
class TimelineCollector:
    """Ön plan değişimlerini olay kaydına yazıp yazmayacağına karar veren saf sınıf.

    Kurallar (feed):
      (a) `name` değişti → hemen kaydet (günlük kap dolu olsa bile).
      (b) Aynı `name`, başlık değişti ama son olaydan < title_min_gap_sec geçti → None.
      (c) kind=="web" ve `domain` değişti → gap'e rağmen kaydet (kap dolu değilse).
      (d) Günlük sayaç daily_cap dolmuşsa o gün için yalnız (a) kaydedilir.
    Dönen olay: {"ts", "os_user", "kind", "name", "title"}; kaydedilmeyecekse None.
    """

    def __init__(self, daily_cap=1000, title_min_gap_sec=10.0, idle_marker_sec=300.0):
        self.daily_cap = daily_cap
        self.title_min_gap_sec = title_min_gap_sec
        self.idle_marker_sec = idle_marker_sec
        self._last = None        # son kaydedilen: (name, title, domain, ts)
        self._day = None         # günlük sayaç anahtarı: "YYYY-MM-DD" (time.localtime)
        self._count = 0          # Süreç-bazlı sınır: yeniden başlatmada sıfırlanır; DB'den yüklenmez (bilinçli).
        self._idle_active = False
        self._idle_peak = 0.0

    def _day_key(self, ts):
        return time.strftime("%Y-%m-%d", time.localtime(ts))

    def feed(self, ts, os_user, kind, name, title, domain=""):
        day = self._day_key(ts)
        if day != self._day:
            self._day = day
            self._count = 0
        if self._last is not None:
            last_name, last_title, last_domain, last_ts = self._last
            if name != last_name:                       # (a) uygulama değişimi — her koşulda
                pass
            elif self._count >= self.daily_cap:         # (d) kap dolu → yalnız (a)
                return None
            elif kind == "web" and domain != last_domain:  # (c) domain değişimi
                pass
            elif title == last_title:                   # değişim yok
                return None
            elif ts - last_ts < self.title_min_gap_sec:  # (b) başlık throttle
                return None
        event = {"ts": ts, "os_user": os_user, "kind": kind, "name": name, "title": title}
        self._last = (name, title, domain, ts)
        self._count += 1
        return event

    def poll_idle(self, ts, os_user, idle_sec):
        """Bekleme süresince durumu izler; yalnız dönüş kenarında tek olay üretir."""
        if idle_sec is not None and idle_sec >= self.idle_marker_sec:
            self._idle_active = True
            self._idle_peak = max(self._idle_peak, idle_sec)
            return None
        if not self._idle_active:
            return None
        peak = self._idle_peak
        self._idle_active = False
        self._idle_peak = 0.0
        return {"kind": "sistem", "name": "beklemede",
                "title": f"{fmt_duration_tr(peak)} boyunca beklemede"}


# =============================================================================
# VERİTABANI KATMANI — SQLite v4 (v3 oturum tablolarıyla %100 geriye uyumlu)
# =============================================================================
class ActivityStore:
    """oyunlar.db içinde v4 aktivite, web, kaynak tüketimi ve olay tablolarını yönetir."""

    def __init__(self, machine_id, path=DB_PATH):
        self.machine_id = machine_id
        self.path = path
        self.changed_since_sync = False
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=15)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.execute("PRAGMA busy_timeout=10000")
        self._init_schema()

    def close(self):
        try:
            self.db.close()
        except Exception:
            pass

    def _init_schema(self):
        with self.db:
            self.db.execute("""
                CREATE TABLE IF NOT EXISTS machines (
                    machine_id TEXT PRIMARY KEY, name TEXT, gpu TEXT, cpu TEXT, ram TEXT,
                    os TEXT, has_battery INTEGER
                )""")
            self.db.execute("""
                CREATE TABLE IF NOT EXISTS activity_daily (
                    machine_id TEXT NOT NULL,
                    day TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    name TEXT NOT NULL,
                    category TEXT NOT NULL,
                    seconds REAL NOT NULL DEFAULT 0,
                    visits INTEGER NOT NULL DEFAULT 0,
                    last_title TEXT,
                    last_ts REAL,
                    PRIMARY KEY (machine_id, day, kind, name)
                )""")
            self.db.execute("CREATE INDEX IF NOT EXISTS idx_act_day ON activity_daily(day, machine_id)")
            self.db.execute("""
                CREATE TABLE IF NOT EXISTS resource_daily (
                    machine_id TEXT NOT NULL,
                    day TEXT NOT NULL,
                    app TEXT NOT NULL,
                    samples INTEGER NOT NULL DEFAULT 0,
                    ram_sum_mb REAL NOT NULL DEFAULT 0,
                    ram_max_mb REAL NOT NULL DEFAULT 0,
                    gpu_sum_pct REAL NOT NULL DEFAULT 0,
                    gpu_max_pct REAL NOT NULL DEFAULT 0,
                    vram_sum_mb REAL NOT NULL DEFAULT 0,
                    vram_max_mb REAL NOT NULL DEFAULT 0,
                    cpu_sum_pct REAL NOT NULL DEFAULT 0,
                    cpu_max_pct REAL NOT NULL DEFAULT 0,
                    PRIMARY KEY (machine_id, day, app)
                )""")
            self.db.execute("CREATE INDEX IF NOT EXISTS idx_res_day ON resource_daily(day, machine_id)")
            self.db.execute("""
                CREATE TABLE IF NOT EXISTS pc_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    machine_id TEXT NOT NULL,
                    ts REAL NOT NULL,
                    kind TEXT NOT NULL,
                    severity TEXT NOT NULL DEFAULT 'info',
                    title TEXT NOT NULL,
                    detail TEXT,
                    event_key TEXT NOT NULL,
                    UNIQUE(machine_id, event_key)
                )""")
            self.db.execute("CREATE INDEX IF NOT EXISTS idx_evt_ts ON pc_events(ts DESC, machine_id)")
            self.db.execute("""
                CREATE TABLE IF NOT EXISTS audit_snapshots (
                    scope TEXT PRIMARY KEY,
                    updated_ts REAL NOT NULL,
                    payload TEXT NOT NULL
                )""")
            self.db.execute("""
                CREATE TABLE IF NOT EXISTS timeline_events (
                    ts REAL NOT NULL,
                    os_user TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    name TEXT NOT NULL,
                    title TEXT NOT NULL
                )""")
            self.db.execute("CREATE INDEX IF NOT EXISTS idx_tl_ts ON timeline_events(ts)")
            self.db.execute("""
                CREATE TABLE IF NOT EXISTS timeline_watermark (
                    id INTEGER PRIMARY KEY CHECK(id=1),
                    ts REAL NOT NULL
                )""")
            ver = self.db.execute("PRAGMA user_version").fetchone()[0]
            if ver < 4:
                self.db.execute("PRAGMA user_version=4")

    # --- Yazma işlemleri ---
    def flush_activity(self, items):
        """items: list of (day, kind, name, category, seconds, visits, last_title, last_ts)"""
        if not items:
            return
        with self.db:
            for day, kind, name, cat, secs, visits, title, ts in items:
                self.db.execute("""
                    INSERT INTO activity_daily
                        (machine_id, day, kind, name, category, seconds, visits, last_title, last_ts)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(machine_id, day, kind, name) DO UPDATE SET
                        category = CASE WHEN excluded.category != 'Diğer' THEN excluded.category ELSE activity_daily.category END,
                        seconds = activity_daily.seconds + excluded.seconds,
                        visits = activity_daily.visits + excluded.visits,
                        last_title = COALESCE(excluded.last_title, activity_daily.last_title),
                        last_ts = MAX(COALESCE(activity_daily.last_ts, 0), COALESCE(excluded.last_ts, 0))
                """, (self.machine_id, day, kind, name, cat, secs, visits, title, ts))
        self.changed_since_sync = True

    def record_web_visits(self, day_domain_counts):
        """Tarayıcı geçmişinden günlük domain ziyaret sayılarını güvenli şekilde günceller (MAX)."""
        if not day_domain_counts:
            return
        with self.db:
            for (day, domain), (count, title, last_ts) in day_domain_counts.items():
                self.db.execute("""
                    INSERT INTO activity_daily
                        (machine_id, day, kind, name, category, seconds, visits, last_title, last_ts)
                    VALUES (?, ?, 'web', ?, 'Web', 0, ?, ?, ?)
                    ON CONFLICT(machine_id, day, kind, name) DO UPDATE SET
                        visits = MAX(activity_daily.visits, excluded.visits),
                        last_title = COALESCE(excluded.last_title, activity_daily.last_title),
                        last_ts = MAX(COALESCE(activity_daily.last_ts, 0), COALESCE(excluded.last_ts, 0))
                """, (self.machine_id, day, domain, count, title, last_ts))
        self.changed_since_sync = True

    def flush_resources(self, day, app_samples):
        """app_samples: dict[app] -> dict(ram_mb, gpu_pct, vram_mb, cpu_pct)"""
        if not app_samples:
            return
        with self.db:
            for app, m in app_samples.items():
                ram = float(m.get("ram_mb", 0.0))
                gpu = float(m.get("gpu_pct", 0.0))
                vram = float(m.get("vram_mb", 0.0))
                cpu = float(m.get("cpu_pct", 0.0))
                self.db.execute("""
                    INSERT INTO resource_daily
                        (machine_id, day, app, samples, ram_sum_mb, ram_max_mb,
                         gpu_sum_pct, gpu_max_pct, vram_sum_mb, vram_max_mb,
                         cpu_sum_pct, cpu_max_pct)
                    VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(machine_id, day, app) DO UPDATE SET
                        samples = resource_daily.samples + 1,
                        ram_sum_mb = resource_daily.ram_sum_mb + excluded.ram_sum_mb,
                        ram_max_mb = MAX(resource_daily.ram_max_mb, excluded.ram_max_mb),
                        gpu_sum_pct = resource_daily.gpu_sum_pct + excluded.gpu_sum_pct,
                        gpu_max_pct = MAX(resource_daily.gpu_max_pct, excluded.gpu_max_pct),
                        vram_sum_mb = resource_daily.vram_sum_mb + excluded.vram_sum_mb,
                        vram_max_mb = MAX(resource_daily.vram_max_mb, excluded.vram_max_mb),
                        cpu_sum_pct = resource_daily.cpu_sum_pct + excluded.cpu_sum_pct,
                        cpu_max_pct = MAX(resource_daily.cpu_max_pct, excluded.cpu_max_pct)
                """, (self.machine_id, day, app, ram, ram, gpu, gpu, vram, vram, cpu, cpu))
        self.changed_since_sync = True

    def add_event(self, kind, title, detail="", severity="info", ts=None, event_key=None):
        ts = float(ts if ts is not None else time.time())
        if not event_key:
            raw = f"{kind}|{title}|{int(ts // 60)}"
            event_key = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
        with self.db:
            cur = self.db.execute("""
                INSERT OR IGNORE INTO pc_events
                    (machine_id, ts, kind, severity, title, detail, event_key)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (self.machine_id, ts, kind, severity, title, detail or "", event_key))
        if cur.rowcount > 0:
            self.changed_since_sync = True
            return True
        return False

    def load_snapshot(self, scope):
        row = self.db.execute("SELECT payload FROM audit_snapshots WHERE scope=?", (scope,)).fetchone()
        if not row:
            return None
        try:
            return json.loads(row["payload"])
        except ValueError:
            return None

    def save_snapshot(self, scope, obj):
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO audit_snapshots (scope, updated_ts, payload) VALUES (?, ?, ?)",
                (scope, time.time(), json.dumps(obj, ensure_ascii=False, sort_keys=True)),
            )

    def upsert_machine(self, mid, info):
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO machines VALUES (?, ?, ?, ?, ?, ?, ?)",
                (mid, info.get("name") or mid, info.get("gpu"), info.get("cpu"),
                 info.get("ram"), info.get("os"), 1 if info.get("has_battery") else 0),
            )

    # --- Çoklu PC Senkron Dışa / İçe Aktarım ---
    def export_activity_bundle(self):
        cutoff = day_range_cutoff(SYNC_ACTIVITY_DAYS)
        act_rows = [
            dict(r) for r in self.db.execute("""
                SELECT day, kind, name, category, ROUND(seconds, 1) AS seconds, visits, last_title, last_ts
                FROM activity_daily WHERE machine_id=? AND day>=? ORDER BY day DESC, seconds DESC
            """, (self.machine_id, cutoff))
        ]
        res_rows = [
            dict(r) for r in self.db.execute("""
                SELECT day, app, samples,
                       ROUND(ram_sum_mb, 1) AS ram_sum_mb, ROUND(ram_max_mb, 1) AS ram_max_mb,
                       ROUND(gpu_sum_pct, 1) AS gpu_sum_pct, ROUND(gpu_max_pct, 1) AS gpu_max_pct,
                       ROUND(vram_sum_mb, 1) AS vram_sum_mb, ROUND(vram_max_mb, 1) AS vram_max_mb,
                       ROUND(cpu_sum_pct, 1) AS cpu_sum_pct, ROUND(cpu_max_pct, 1) AS cpu_max_pct
                FROM resource_daily WHERE machine_id=? AND day>=? ORDER BY day DESC, ram_sum_mb DESC
            """, (self.machine_id, cutoff))
        ]
        return {"v": 4, "machine_id": self.machine_id, "updated_ts": round(time.time(), 1),
                "activity": act_rows, "resources": res_rows}

    def export_events_lines(self):
        rows = self.db.execute("""
            SELECT ts, kind, severity, title, detail, event_key
            FROM pc_events WHERE machine_id=? ORDER BY ts DESC LIMIT ?
        """, (self.machine_id, SYNC_MAX_EVENTS)).fetchall()
        out = []
        for r in reversed(rows):
            out.append(json.dumps({
                "ts": round(r["ts"], 1), "kind": r["kind"], "severity": r["severity"],
                "title": r["title"], "detail": r["detail"] or "", "event_key": r["event_key"],
            }, ensure_ascii=False, sort_keys=True))
        return "\n".join(out) + ("\n" if out else "")

    def import_remote_activity(self, mid, bundle):
        if not isinstance(bundle, dict) or mid == self.machine_id:
            return
        with self.db:
            self.db.execute("DELETE FROM activity_daily WHERE machine_id=?", (mid,))
            for r in bundle.get("activity") or []:
                self.db.execute("""
                    INSERT OR REPLACE INTO activity_daily
                        (machine_id, day, kind, name, category, seconds, visits, last_title, last_ts)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (mid, r.get("day", ""), r.get("kind", "app"), r.get("name", ""),
                      r.get("category", "Diğer"), float(r.get("seconds", 0)),
                      int(r.get("visits", 0)), r.get("last_title"), r.get("last_ts")))
            self.db.execute("DELETE FROM resource_daily WHERE machine_id=?", (mid,))
            for r in bundle.get("resources") or []:
                self.db.execute("""
                    INSERT OR REPLACE INTO resource_daily
                        (machine_id, day, app, samples, ram_sum_mb, ram_max_mb,
                         gpu_sum_pct, gpu_max_pct, vram_sum_mb, vram_max_mb,
                         cpu_sum_pct, cpu_max_pct)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (mid, r.get("day", ""), r.get("app", ""), int(r.get("samples", 0)),
                      float(r.get("ram_sum_mb", 0)), float(r.get("ram_max_mb", 0)),
                      float(r.get("gpu_sum_pct", 0)), float(r.get("gpu_max_pct", 0)),
                      float(r.get("vram_sum_mb", 0)), float(r.get("vram_max_mb", 0)),
                      float(r.get("cpu_sum_pct", 0)), float(r.get("cpu_max_pct", 0))))

    def import_remote_events(self, mid, rows):
        if not rows:
            return
        with self.db:
            if mid != self.machine_id:
                self.db.execute("DELETE FROM pc_events WHERE machine_id=?", (mid,))
            for r in rows:
                ek = r.get("event_key")
                if not ek:
                    continue
                self.db.execute("""
                    INSERT OR IGNORE INTO pc_events
                        (machine_id, ts, kind, severity, title, detail, event_key)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (mid, float(r.get("ts", 0)), r.get("kind", "sistem"),
                      r.get("severity", "info"), r.get("title", ""), r.get("detail", ""), ek))

    # --- Timeline (Film Şeridi) ---
    def add_timeline_event(self, ts, os_user, kind, name, title):
        """Film Şeridi: ön plan değişim olayını timeline_events tablosuna yazar."""
        with self.db:
            self.db.execute(
                "INSERT INTO timeline_events(ts, os_user, kind, name, title) VALUES (?,?,?,?,?)",
                (float(ts), str(os_user or ""), str(kind or ""), str(name or ""), str(title or ""))
            )

    def fetch_timeline_since(self, since_rowid, limit=500):
        """Son gönderilen rowid'den sonraki olayları deterministik sırayla döner."""
        rows = self.db.execute(
            "SELECT rowid AS event_id, ts, os_user, kind, name, title FROM timeline_events "
            "WHERE rowid > ? ORDER BY rowid ASC LIMIT ?",
            (int(since_rowid), int(limit))
        ).fetchall()
        return [dict(r) for r in rows]

    def prune_timeline(self, keep_days=30):
        """keep_days günden eski timeline olaylarını siler; silinen satır sayısını döner."""
        cutoff = time.time() - float(keep_days) * 86400.0
        with self.db:
            cur = self.db.execute("DELETE FROM timeline_events WHERE ts < ?", (cutoff,))
        return cur.rowcount

    def get_timeline_watermark(self):
        """Son başarıyla gönderilen timeline olayının rowid'si (başlangıçta 0)."""
        row = self.db.execute("SELECT ts FROM timeline_watermark WHERE id=1").fetchone()
        return int(row[0]) if row else 0

    def set_timeline_watermark(self, event_id):
        """Başarılı ingest sonrası watermark'ı günceller.

        Thread-safe: send_ingest'in arka plan thread'inden çağrılır; kendi kısa
        ömürlü bağlantısını açar (check_same_thread sorununu önler).
        """
        conn = sqlite3.connect(self.path, timeout=5)
        try:
            conn.execute(
                "INSERT OR REPLACE INTO timeline_watermark(id, ts) VALUES (1, ?)",
                (int(event_id),)
            )
            conn.commit()
        finally:
            conn.close()

    # --- Okuma / Analitik Sorguları (Dashboard & SummaryEngine) ---
    def _where_day(self, machine=None, days=7):
        conds, args = [], []
        if machine:
            conds.append("machine_id=?")
            args.append(machine)
        if days and days > 0:
            conds.append("day>=?")
            args.append(day_range_cutoff(days))
        return (" WHERE " + " AND ".join(conds)) if conds else "", args

    def top_apps(self, machine=None, days=7, limit=50):
        w, a = self._where_day(machine, days)
        extra = " AND kind='app'" if w else " WHERE kind='app'"
        return self.db.execute(f"""
            SELECT name, category, SUM(seconds) AS secs, SUM(visits) AS visits,
                   MAX(last_title) AS last_title, MAX(last_ts) AS last_ts
            FROM activity_daily{w}{extra}
            GROUP BY name ORDER BY secs DESC LIMIT ?
        """, a + [limit]).fetchall()

    def top_websites(self, machine=None, days=7, limit=60):
        w, a = self._where_day(machine, days)
        extra = " AND kind='web'" if w else " WHERE kind='web'"
        return self.db.execute(f"""
            SELECT name AS domain, SUM(seconds) AS secs, SUM(visits) AS visits,
                   MAX(last_title) AS last_title, MAX(last_ts) AS last_ts
            FROM activity_daily{w}{extra}
            GROUP BY name ORDER BY secs DESC, visits DESC LIMIT ?
        """, a + [limit]).fetchall()

    def category_totals(self, machine=None, days=7):
        w, a = self._where_day(machine, days)
        extra = " AND kind='app'" if w else " WHERE kind='app'"
        return self.db.execute(f"""
            SELECT category, SUM(seconds) AS secs
            FROM activity_daily{w}{extra}
            GROUP BY category ORDER BY secs DESC
        """, a).fetchall()

    def total_active_seconds(self, machine=None, days=7):
        w, a = self._where_day(machine, days)
        extra = " AND kind='app'" if w else " WHERE kind='app'"
        r = self.db.execute(f"SELECT COALESCE(SUM(seconds), 0) FROM activity_daily{w}{extra}", a).fetchone()
        return float(r[0] or 0.0)

    def top_resources(self, machine=None, days=7, limit=40):
        w, a = self._where_day(machine, days)
        return self.db.execute(f"""
            SELECT app,
                   SUM(samples) AS samples,
                   SUM(ram_sum_mb) / NULLIF(SUM(samples), 0) AS avg_ram_mb,
                   MAX(ram_max_mb) AS max_ram_mb,
                   SUM(gpu_sum_pct) / NULLIF(SUM(samples), 0) AS avg_gpu_pct,
                   MAX(gpu_max_pct) AS max_gpu_pct,
                   SUM(vram_sum_mb) / NULLIF(SUM(samples), 0) AS avg_vram_mb,
                   MAX(vram_max_mb) AS max_vram_mb,
                   SUM(cpu_sum_pct) / NULLIF(SUM(samples), 0) AS avg_cpu_pct,
                   MAX(cpu_max_pct) AS max_cpu_pct
            FROM resource_daily{w}
            GROUP BY app
            HAVING SUM(samples) > 0
            ORDER BY avg_ram_mb DESC LIMIT ?
        """, a + [limit]).fetchall()

    def list_events(self, machine=None, days=30, kind=None, search="", limit=300):
        conds, args = [], []
        if machine:
            conds.append("machine_id=?")
            args.append(machine)
        if days and days > 0:
            conds.append("ts>=?")
            args.append(time.time() - days * 86400)
        if kind and kind != "tumu":
            if kind == "kurulum":
                conds.append("kind IN ('program_kuruldu','program_guncellendi','program_kaldirildi','oyun_kuruldu','oyun_guncellendi','oyun_kaldirildi')")
            elif kind == "kritik":
                conds.append("(severity IN ('important','critical') OR kind='kritik_degisiklik')")
            else:
                conds.append("kind=?")
                args.append(kind)
        if search:
            conds.append("(LOWER(title) LIKE ? OR LOWER(detail) LIKE ?)")
            q = f"%{search.lower()}%"
            args.extend([q, q])
        w = (" WHERE " + " AND ".join(conds)) if conds else ""
        return self.db.execute(
            f"SELECT * FROM pc_events{w} ORDER BY ts DESC LIMIT ?", args + [limit]
        ).fetchall()

    def machines(self):
        rows = {r["machine_id"]: dict(r) for r in self.db.execute("SELECT * FROM machines")}
        for tbl in ("activity_daily", "pc_events"):
            for r in self.db.execute(f"SELECT DISTINCT machine_id FROM {tbl}"):
                rows.setdefault(r["machine_id"], {"machine_id": r["machine_id"], "name": r["machine_id"]})
        rows.setdefault(self.machine_id, {"machine_id": self.machine_id, "name": self.machine_id})
        return sorted(rows.values(), key=lambda m: (m["machine_id"] != self.machine_id, m["name"] or ""))


# =============================================================================
# DOĞAL TÜRKÇE ÖZET RAPOR ÜRETİCİ (Günlük / Haftalık / Aylık)
# =============================================================================
class SummaryEngine:
    """SQLite verisinden okunabilir Türkçe özet kartları ve Markdown raporu üretir."""

    PERIOD_LABELS = {1: "Bugün", 7: "Son 7 Gün", 30: "Son 30 Gün", 0: "Tüm Zamanlar"}

    @classmethod
    def build_digest(cls, store, machine=None, days=7):
        label = cls.PERIOD_LABELS.get(days, f"Son {days} Gün")
        total_sec = store.total_active_seconds(machine, days)
        apps = store.top_apps(machine, days, limit=10)
        webs = store.top_websites(machine, days, limit=10)
        cats = store.category_totals(machine, days)
        res = store.top_resources(machine, days, limit=10)
        evts = store.list_events(machine, days=days if days else 3650, limit=50)

        # Oyun oturumları (mevcut v3 sessions tablosu varsa)
        game_secs, game_sessions, top_game = 0.0, 0, None
        try:
            conds, args = [], []
            if machine:
                conds.append("machine_id=?")
                args.append(machine)
            if days and days > 0:
                conds.append("start_ts>=?")
                args.append(time.time() - days * 86400)
            w = (" WHERE " + " AND ".join(conds)) if conds else ""
            row = store.db.execute(f"SELECT COUNT(*), COALESCE(SUM(duration_s),0) FROM sessions{w}", args).fetchone()
            game_sessions, game_secs = int(row[0] or 0), float(row[1] or 0.0)
            tg = store.db.execute(
                f"SELECT game, SUM(duration_s) AS s, AVG(avg_fps) AS fps FROM sessions{w} "
                "GROUP BY game ORDER BY s DESC LIMIT 1", args
            ).fetchone()
            if tg:
                top_game = (tg["game"], float(tg["s"] or 0), float(tg["fps"] or 0))
        except Exception:
            pass

        bullets = []
        if total_sec >= 60:
            top_app_str = ", ".join(f"<b>{a['name']}</b> ({fmt_duration_tr(a['secs'], short=True)})" for a in apps[:3])
            bullets.append(
                f"<b>{label}</b> bilgisayarda toplam <b>{fmt_duration_tr(total_sec)}</b> aktif vakit geçirdin. "
                f"En çok kullandıkların: {top_app_str}."
            )
        else:
            bullets.append(
                f"<b>{label}</b> için aktif kullanım kaydı yeni başlıyor — servis arka planda sessizce veri topluyor."
            )

        if top_game:
            bullets.append(
                f"<b>Oyunlar:</b> {game_sessions} oturumda toplam <b>{fmt_duration_tr(game_secs)}</b> oynadın "
                f"(en çok <b>{top_game[0]}</b> — {fmt_duration_tr(top_game[1], short=True)}, ort. {top_game[2]:.0f} FPS)."
            )

        if webs:
            top_w = []
            for w_row in webs[:4]:
                if w_row["secs"] >= 30:
                    top_w.append(f"<b>{w_row['domain']}</b> ({fmt_duration_tr(w_row['secs'], short=True)})")
                elif w_row["visits"] > 0:
                    top_w.append(f"<b>{w_row['domain']}</b> ({w_row['visits']} ziyaret)")
            if top_w:
                bullets.append(f"<b>Web'de gezindiklerin:</b> {', '.join(top_w)}.")

        if res:
            by_ram = sorted(res, key=lambda r: r["avg_ram_mb"] or 0, reverse=True)[:3]
            ram_txt = ", ".join(
                f"<b>{r['app']}</b> (ort. {fmt_mb_tr(r['avg_ram_mb'])}, maks. {fmt_mb_tr(r['max_ram_mb'])})"
                for r in by_ram if (r["avg_ram_mb"] or 0) >= 30
            )
            if ram_txt:
                bullets.append(f"<b>RAM tüketimi liderleri:</b> {ram_txt}.")
            by_gpu = [r for r in sorted(res, key=lambda r: (r["max_vram_mb"] or 0) + (r["avg_gpu_pct"] or 0) * 50, reverse=True)
                      if (r["max_vram_mb"] or 0) >= 128 or (r["avg_gpu_pct"] or 0) >= 2.0][:3]
            if by_gpu:
                gpu_txt = ", ".join(
                    f"<b>{r['app']}</b> (ort. %{r['avg_gpu_pct']:.0f} GPU, maks. {fmt_mb_tr(r['max_vram_mb'])} VRAM)"
                    for r in by_gpu
                )
                bullets.append(f"<b>GPU & VRAM tüketimi:</b> {gpu_txt}.")

        installs = [e for e in evts if e["kind"] in ("program_kuruldu", "oyun_kuruldu", "program_guncellendi", "oyun_guncellendi", "program_kaldirildi", "oyun_kaldirildi")]
        crits = [e for e in evts if e["kind"] == "kritik_degisiklik" or e["severity"] == "critical"]
        if installs or crits:
            parts = []
            if installs:
                sample = ", ".join(e["title"] for e in installs[:3])
                parts.append(f"<b>{len(installs)} kurulum/güncelleme</b> ({sample})")
            if crits:
                sample = ", ".join(e["title"] for e in crits[:2])
                parts.append(f"<b>{len(crits)} kritik değişiklik</b> ({sample})")
            bullets.append(f"<b>Sistem & Kurulum hareketleri:</b> {'; '.join(parts)}.")

        kpis = {
            "active_str": fmt_duration_tr(total_sec, short=True) if total_sec >= 60 else "Yeni",
            "game_str": fmt_duration_tr(game_secs, short=True) if game_secs >= 60 else "0 dk",
            "top_web": webs[0]["domain"] if webs else "—",
            "top_ram": f"{res[0]['app']} ({fmt_mb_tr(res[0]['avg_ram_mb'])})" if res else "—",
            "events_count": len(evts),
            "installs_count": len(installs),
            "critical_count": len(crits),
        }
        return {"label": label, "kpis": kpis, "bullets": bullets, "apps": apps, "webs": webs,
                "cats": cats, "resources": res, "events": evts}

    @classmethod
    def to_markdown(cls, store, machine=None, days=7):
        d = cls.build_digest(store, machine, days)
        strip_html = lambda s: re.sub(r"</?b>", "**", s)
        lines = [f"# PC & Oyun Aktivite Raporu — {d['label']}",
                 f"_Oluşturulma: {time.strftime('%Y-%m-%d %H:%M')}_", ""]
        lines.append("## Özet")
        for b in d["bullets"]:
            lines.append(f"- {strip_html(b)}")
        if d["apps"]:
            lines += ["", "## En Çok Kullanılan Uygulamalar", "| Uygulama | Kategori | Süre |", "|---|---|---|"]
            for a in d["apps"]:
                lines.append(f"| {a['name']} | {a['category']} | {fmt_duration_tr(a['secs'])} |")
        if d["webs"]:
            lines += ["", "## Web Siteleri", "| Site | Aktif Süre | Ziyaret |", "|---|---|---|"]
            for w in d["webs"]:
                lines.append(f"| {w['domain']} | {fmt_duration_tr(w['secs'])} | {w['visits']} |")
        if d["resources"]:
            lines += ["", "## RAM & GPU Tüketimi", "| Uygulama | Ort. RAM | Maks. RAM | Ort. GPU | Maks. VRAM |", "|---|---|---|---|---|"]
            for r in d["resources"]:
                lines.append(
                    f"| {r['app']} | {fmt_mb_tr(r['avg_ram_mb'])} | {fmt_mb_tr(r['max_ram_mb'])} "
                    f"| %{r['avg_gpu_pct']:.1f} | {fmt_mb_tr(r['max_vram_mb'])} |"
                )
        if d["events"]:
            lines += ["", "## Kurulumlar & Kritik Değişiklikler"]
            for e in d["events"][:30]:
                ts_s = time.strftime("%Y-%m-%d %H:%M", time.localtime(e["ts"]))
                det = f" — {e['detail']}" if e["detail"] else ""
                lines.append(f"- `{ts_s}` **[{e['kind']}]** {e['title']}{det}")
        return "\n".join(lines) + "\n"


# =============================================================================
# AKTİF PENCERE & IDLE ALGILAMA (Windows Win32 / Linux X11-Wayland)
# =============================================================================
if IS_WIN:
    class _LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

    _user32 = ctypes.windll.user32
    _kernel32 = ctypes.windll.kernel32
    _kernel32.GetTickCount.restype = ctypes.c_uint32
    _kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    _kernel32.SetProcessWorkingSetSize.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_size_t]
    _user32.GetForegroundWindow.restype = ctypes.c_void_p
    _user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    _user32.GetWindowTextLengthW.argtypes = [ctypes.c_void_p]
    _user32.GetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]


def trim_memory():
    """Python çöp toplayıcıyı çalıştırır ve Windows'ta kullanılmayan bellek sayfalarını iade eder."""
    import gc
    gc.collect()
    if IS_WIN:
        try:
            _kernel32.SetProcessWorkingSetSize(_kernel32.GetCurrentProcess(), ctypes.c_size_t(-1), ctypes.c_size_t(-1))
        except Exception:
            pass


def get_idle_seconds():
    if IS_WIN:
        try:
            lii = _LASTINPUTINFO()
            lii.cbSize = ctypes.sizeof(_LASTINPUTINFO)
            if _user32.GetLastInputInfo(ctypes.byref(lii)):
                tick = _kernel32.GetTickCount()
                diff = (tick - lii.dwTime) & 0xFFFFFFFF
                return diff / 1000.0
        except Exception:
            pass
        return 0.0
    # Linux Wayland (KDE Plasma / GNOME) via D-Bus ScreenSaver
    try:
        out = subprocess.check_output(
            ["gdbus", "call", "--session", "--dest", "org.freedesktop.ScreenSaver",
             "--object-path", "/org/freedesktop/ScreenSaver",
             "--method", "org.freedesktop.ScreenSaver.GetSessionIdleTime"],
            timeout=1, stderr=subprocess.DEVNULL,
        ).decode("utf-8", "replace")
        m = re.search(r"(\d+)", out)
        if m:
            return int(m.group(1)) / 1000.0
    except Exception:
        pass
    try:
        out = subprocess.check_output(["xprintidle"], timeout=1, stderr=subprocess.DEVNULL).decode().strip()
        return int(out) / 1000.0
    except Exception:
        return 0.0


def _linux_proc_from_pid(pid):
    exe_name, exe_path = "", ""
    try:
        exe_path = os.readlink(f"/proc/{pid}/exe")
        exe_name = os.path.basename(exe_path)
    except OSError:
        try:
            exe_name = open(f"/proc/{pid}/comm").read().strip()
        except OSError:
            pass
    return exe_name, exe_path


def get_foreground_info():
    """(pid, exe_name, exe_path, window_title) döndürür."""
    if IS_WIN:
        try:
            hwnd = _user32.GetForegroundWindow()
            if not hwnd:
                return None, "", "", ""
            pid = ctypes.c_ulong(0)
            _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            length = _user32.GetWindowTextLengthW(hwnd)
            title = ""
            if 0 < length < 512:
                buf = ctypes.create_unicode_buffer(length + 1)
                _user32.GetWindowTextW(hwnd, buf, length + 1)
                title = buf.value
            exe_name, exe_path = "", ""
            if pid.value and psutil:
                try:
                    p = psutil.Process(pid.value)
                    exe_name = p.name() or ""
                    try:
                        exe_path = p.exe() or ""
                    except (psutil.AccessDenied, psutil.ZombieProcess):
                        exe_path = ""
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            return pid.value, exe_name, exe_path, title
        except Exception:
            return None, "", "", ""
    # Linux 1: Hyprland (Wayland)
    if os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
        try:
            raw = subprocess.check_output(["hyprctl", "activewindow", "-j"], timeout=1, stderr=subprocess.DEVNULL).decode("utf-8", "replace")
            info = json.loads(raw)
            pid = int(info.get("pid") or 0)
            if pid > 0:
                en, ep = _linux_proc_from_pid(pid)
                return pid, en or (info.get("class") or ""), ep, info.get("title") or ""
        except Exception:
            pass
    # Linux 2: KDE Plasma Wayland (kdotool)
    if shutil.which("kdotool"):
        try:
            wid = subprocess.check_output(["kdotool", "getactivewindow"], timeout=1, stderr=subprocess.DEVNULL).decode().strip()
            if wid:
                pid_s = subprocess.check_output(["kdotool", "getwindowpid", wid], timeout=1, stderr=subprocess.DEVNULL).decode().strip()
                title = subprocess.check_output(["kdotool", "getwindowname", wid], timeout=1, stderr=subprocess.DEVNULL).decode("utf-8", "replace").strip()
                pid = int(pid_s)
                en, ep = _linux_proc_from_pid(pid)
                return pid, en, ep, title
        except Exception:
            pass
    # Linux 3: X11 / XWayland (xdotool)
    try:
        wid = subprocess.check_output(["xdotool", "getactivewindow"], timeout=1, stderr=subprocess.DEVNULL).decode().strip()
        pid = int(subprocess.check_output(["xdotool", "getwindowpid", wid], timeout=1, stderr=subprocess.DEVNULL).decode().strip())
        title = subprocess.check_output(["xdotool", "getwindowname", wid], timeout=1, stderr=subprocess.DEVNULL).decode("utf-8", "replace").strip()
        en, ep = _linux_proc_from_pid(pid)
        return pid, en, ep, title
    except Exception:
        return None, "", "", ""


# =============================================================================
# TARAYICI GEÇMİŞİ OKUYUCU (Salt Okunur — Eklenti Gerektirmez)
# =============================================================================
class BrowserDomainResolver:
    """Chrome / Edge / Brave / Opera / Vivaldi / Firefox / Zen geçmişinden domain eşleştirir."""

    def __init__(self):
        self.title_to_domain = {}  # normalized_title -> (domain, ts, url)
        self._last_scan = 0.0
        self._seen_visit_max = {}

    @staticmethod
    def _discover_dbs():
        dbs = []
        home = os.path.expanduser("~")
        if IS_WIN:
            local = _LOCAL or ""
            roam = _ROAM or ""
            patterns = [
                ("chromium", os.path.join(local, r"Google\Chrome\User Data\*\History")),
                ("chromium", os.path.join(local, r"Microsoft\Edge\User Data\*\History")),
                ("chromium", os.path.join(local, r"BraveSoftware\Brave-Browser\User Data\*\History")),
                ("chromium", os.path.join(local, r"Vivaldi\User Data\*\History")),
                ("chromium", os.path.join(roam, r"Opera Software\*\History")),
                ("firefox", os.path.join(roam, r"Mozilla\Firefox\Profiles\*\places.sqlite")),
                ("firefox", os.path.join(roam, r"zen\Profiles\*\places.sqlite")),
            ]
        else:
            patterns = [
                ("chromium", os.path.join(home, ".config/google-chrome/*/History")),
                ("chromium", os.path.join(home, ".config/chromium/*/History")),
                ("chromium", os.path.join(home, ".config/BraveSoftware/Brave-Browser/*/History")),
                ("chromium", os.path.join(home, ".config/vivaldi/*/History")),
                ("firefox", os.path.join(home, ".mozilla/firefox/*/places.sqlite")),
                ("firefox", os.path.join(home, ".zen/*/places.sqlite")),
            ]
        for kind, pat in patterns:
            for p in glob.glob(pat):
                if os.path.isfile(p):
                    dbs.append((kind, p))
        return dbs

    def refresh(self, store=None, days_back=7):
        """Tarayıcı DB'lerinin geçici kopyasından son ziyaretleri okur (asla kilitlemez)."""
        now = time.time()
        self._last_scan = now
        cutoff_unix = now - days_back * 86400
        day_counts = {}  # (day, domain) -> [count, title, max_ts]
        title_map = {}

        for kind, db_path in self._discover_dbs():
            tmp_path = None
            try:
                fd, tmp_path = tempfile.mkstemp(prefix="oi_br_", suffix=".sqlite")
                os.close(fd)
                shutil.copy2(db_path, tmp_path)
                # WAL dosyası varsa onu da kopyala ki en taze sekmeler görünsün
                for ext in ("-wal", "-shm"):
                    if os.path.isfile(db_path + ext):
                        try:
                            shutil.copy2(db_path + ext, tmp_path + ext)
                        except OSError:
                            pass
                conn = sqlite3.connect(f"file:{tmp_path}?mode=ro", uri=True, timeout=3)
                try:
                    if kind == "chromium":
                        # Chromium time: microseconds since 1601-01-01
                        cut_ch = int((cutoff_unix + 11644473600) * 1_000_000)
                        q = """
                            SELECT u.url, u.title, v.visit_time
                            FROM visits v JOIN urls u ON v.url = u.id
                            WHERE v.visit_time >= ?
                            ORDER BY v.visit_time DESC LIMIT 2000
                        """
                        for url, title, vt in conn.execute(q, (cut_ch,)):
                            ts = vt / 1_000_000.0 - 11644473600.0
                            dom = clean_domain(url)
                            if not dom:
                                continue
                            self._accum(dom, title or "", ts, title_map, day_counts, url)
                    else:
                        # Firefox/Zen time: microseconds since 1970-01-01
                        cut_ff = int(cutoff_unix * 1_000_000)
                        q = """
                            SELECT p.url, p.title, v.visit_date
                            FROM moz_historyvisits v JOIN moz_places p ON v.place_id = p.id
                            WHERE v.visit_date >= ?
                            ORDER BY v.visit_date DESC LIMIT 2000
                        """
                        for url, title, vt in conn.execute(q, (cut_ff,)):
                            ts = vt / 1_000_000.0
                            dom = clean_domain(url)
                            if not dom:
                                continue
                            self._accum(dom, title or "", ts, title_map, day_counts, url)
                finally:
                    conn.close()
            except Exception:
                pass
            finally:
                if tmp_path:
                    for ext in ("", "-wal", "-shm"):
                        try:
                            if os.path.exists(tmp_path + ext):
                                os.remove(tmp_path + ext)
                        except OSError:
                            pass

        if title_map:
            self.title_to_domain = title_map
        if store and day_counts:
            store.record_web_visits({k: (v[0], v[1], v[2]) for k, v in day_counts.items()})

    @staticmethod
    def _accum(dom, title, ts, title_map, day_counts, url=""):
        norm = strip_browser_suffix(title).lower()[:80]
        if norm and (norm not in title_map or ts > title_map[norm][1]):
            title_map[norm] = (dom, ts, url)
        day = today_str(ts)
        key = (day, dom)
        if key not in day_counts:
            day_counts[key] = [1, title[:120], ts]
        else:
            day_counts[key][0] += 1
            if ts > day_counts[key][2]:
                day_counts[key][2] = ts
                if title:
                    day_counts[key][1] = title[:120]

    def resolve_title(self, raw_title):
        """Tarayıcı sekme başlığından kesin domain bulur (geçmiş eşleşmesi -> bilinen site -> regex)."""
        clean = strip_browser_suffix(raw_title)
        low = clean.lower()[:80]
        if not low or low in ("new tab", "yeni sekme", "start page", "başlangıç sayfası", "about:blank"):
            return ""
        # 1) Birebir veya önek eşleşmesi (gerçek tarayıcı geçmişinden)
        if low in self.title_to_domain:
            return self.title_to_domain[low][0]
        if len(low) >= 12:
            prefix = low[:28]
            for k, (dom, _ts, *_rest) in self.title_to_domain.items():
                if k.startswith(prefix) or low.startswith(k[:28]):
                    return dom
        # 2) Bilinen site desenleri
        for pat, dom in SITE_TITLE_PATTERNS:
            if pat.search(clean):
                return dom
        # 3) Başlık içinde doğrudan alan adı geçiyorsa (ör. "docs.python.org")
        m = DOMAIN_IN_TITLE_RE.search(clean)
        if m:
            d = m.group(1).lower()
            if d.startswith("www."):
                d = d[4:]
            return d
        return ""

    def resolve_title_url(self, raw_title):
        """Tarayıcı sekme başlığından (domain, url) çifti bulur; bulamazsa None."""
        clean = strip_browser_suffix(raw_title)
        low = clean.lower()[:80]
        if not low or low in ("new tab", "yeni sekme", "start page", "başlangıç sayfası", "about:blank"):
            return None
        # 1) Birebir veya önek eşleşmesi (gerçek tarayıcı geçmişinden)
        if low in self.title_to_domain:
            dom, _ts, url = self.title_to_domain[low]
            return (dom, url)
        if len(low) >= 12:
            prefix = low[:28]
            for k, (dom, _ts, url) in self.title_to_domain.items():
                if k.startswith(prefix) or low.startswith(k[:28]):
                    return (dom, url)
        # 2) Bilinen site desenleri (URL bilinmiyor)
        for pat, dom in SITE_TITLE_PATTERNS:
            if pat.search(clean):
                return (dom, "")
        # 3) Başlık içinde doğrudan alan adı geçiyorsa
        m = DOMAIN_IN_TITLE_RE.search(clean)
        if m:
            d = m.group(1).lower()
            if d.startswith("www."):
                d = d[4:]
            return (d, "")
        return None


# =============================================================================
# UYGULAMA BAZLI RAM / CPU / GPU / VRAM ÖLÇÜMÜ (Windows PDH + Linux fdinfo)
# =============================================================================
if IS_WIN:
    class _PDH_FMT_COUNTERVALUE_D(ctypes.Structure):
        _fields_ = [("CStatus", ctypes.c_uint32), ("doubleValue", ctypes.c_double)]

    class _PDH_FMT_COUNTERVALUE_ITEM_W(ctypes.Structure):
        _fields_ = [("szName", ctypes.c_wchar_p), ("FmtValue", _PDH_FMT_COUNTERVALUE_D)]

    _PID_RE = re.compile(r"pid_(\d+)_", re.I)


class WinPdhGpuSampler:
    """Saf Win32 pdh.dll ile süreç bazlı GPU % ve adanmış VRAM (MB) okur (0 alt süreç)."""

    PDH_FMT_DOUBLE = 0x00000200
    PDH_MORE_DATA = 0x800007D2

    def __init__(self):
        self.pdh = None
        self.query = ctypes.c_void_p(0)
        self.c_vram = ctypes.c_void_p(0)
        self.c_eng = ctypes.c_void_p(0)
        self.ready = False
        if not IS_WIN:
            return
        try:
            self.pdh = ctypes.windll.pdh
            self.pdh.PdhOpenQueryW.argtypes = [ctypes.c_wchar_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
            self.pdh.PdhAddEnglishCounterW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
            self.pdh.PdhCollectQueryData.argtypes = [ctypes.c_void_p]
            self.pdh.PdhGetFormattedCounterArrayW.argtypes = [
                ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32),
                ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p,
            ]
            self.pdh.PdhCloseQuery.argtypes = [ctypes.c_void_p]
            if self.pdh.PdhOpenQueryW(None, None, ctypes.byref(self.query)) != 0:
                return
            r1 = self.pdh.PdhAddEnglishCounterW(
                self.query, r"\GPU Process Memory(*)\Dedicated Usage", None, ctypes.byref(self.c_vram)
            )
            r2 = self.pdh.PdhAddEnglishCounterW(
                self.query, r"\GPU Engine(*)\Utilization Percentage", None, ctypes.byref(self.c_eng)
            )
            if r1 == 0 or r2 == 0:
                self.pdh.PdhCollectQueryData(self.query)
                self.ready = True
        except Exception:
            self.ready = False

    def _read_array(self, counter_handle):
        if not counter_handle or not counter_handle.value:
            return {}
        buf_size = ctypes.c_uint32(0)
        item_count = ctypes.c_uint32(0)
        res = self.pdh.PdhGetFormattedCounterArrayW(
            counter_handle, self.PDH_FMT_DOUBLE, ctypes.byref(buf_size), ctypes.byref(item_count), None
        )
        if (res & 0xFFFFFFFF) != self.PDH_MORE_DATA or buf_size.value == 0:
            return {}
        buf = (ctypes.c_byte * buf_size.value)()
        res = self.pdh.PdhGetFormattedCounterArrayW(
            counter_handle, self.PDH_FMT_DOUBLE, ctypes.byref(buf_size), ctypes.byref(item_count), ctypes.byref(buf)
        )
        if res != 0:
            return {}
        items = ctypes.cast(buf, ctypes.POINTER(_PDH_FMT_COUNTERVALUE_ITEM_W))
        by_pid = defaultdict(float)
        for i in range(item_count.value):
            it = items[i]
            if it.FmtValue.CStatus != 0 or not it.szName:
                continue
            m = _PID_RE.search(it.szName)
            if m:
                val = it.FmtValue.doubleValue
                if val > 0:
                    by_pid[int(m.group(1))] += val
        return by_pid

    def sample(self):
        """(vram_mb_by_pid, gpu_pct_by_pid) döndürür."""
        if not self.ready:
            return {}, {}
        try:
            if self.pdh.PdhCollectQueryData(self.query) != 0:
                return {}, {}
            vram_bytes = self._read_array(self.c_vram)
            gpu_pct = self._read_array(self.c_eng)
            vram_mb = {pid: b / (1024.0 * 1024.0) for pid, b in vram_bytes.items() if b >= 1024 * 1024}
            gpu_clamped = {pid: min(100.0, v) for pid, v in gpu_pct.items() if v >= 0.1}
            return vram_mb, gpu_clamped
        except Exception:
            return {}, {}

    def close(self):
        if self.ready and self.pdh and self.query.value:
            try:
                self.pdh.PdhCloseQuery(self.query)
            except Exception:
                pass
            self.ready = False


def linux_sample_gpu_by_pid():
    """Linux AMD/Intel (/proc/<pid>/fdinfo drm-memory-vram) ve NVIDIA pmon okur."""
    vram_mb = defaultdict(float)
    gpu_pct = defaultdict(float)
    if IS_WIN:
        return vram_mb, gpu_pct
    try:
        for pid_s in os.listdir("/proc"):
            if not pid_s.isdigit():
                continue
            fdinfo_dir = f"/proc/{pid_s}/fdinfo"
            try:
                seen_inodes = set()
                for fd in os.listdir(fdinfo_dir)[:64]:
                    p = os.path.join(fdinfo_dir, fd)
                    with open(p, "r", errors="ignore") as f:
                        txt = f.read(1024)
                    if "drm-memory-vram:" not in txt:
                        continue
                    cid = re.search(r"drm-client-id:\s*(\d+)", txt)
                    if cid:
                        if cid.group(1) in seen_inodes:
                            continue
                        seen_inodes.add(cid.group(1))
                    m = re.search(r"drm-memory-vram:\s*(\d+)\s*KiB", txt)
                    if m:
                        vram_mb[int(pid_s)] += int(m.group(1)) / 1024.0
            except OSError:
                continue
    except OSError:
        pass
    return vram_mb, gpu_pct


class ResourceSampler:
    """Her 30 saniyede uygulama bazlı RAM, CPU, GPU % ve VRAM tüketimini örnekler."""

    def __init__(self):
        self._win_pdh = WinPdhGpuSampler() if IS_WIN else None
        self._cpu_cores = max(1, os.cpu_count() or 1)
        self._prev_cpu = {}  # pid -> (cpu_time_sum, wall_ts)

    def sample(self, known_games=None):
        """dict[clean_app_name] -> {ram_mb, cpu_pct, gpu_pct, vram_mb}"""
        if not psutil:
            return {}
        now = time.time()
        vram_by_pid, gpu_by_pid = (self._win_pdh.sample() if self._win_pdh else linux_sample_gpu_by_pid())
        agg = defaultdict(lambda: {"ram_mb": 0.0, "cpu_pct": 0.0, "gpu_pct": 0.0, "vram_mb": 0.0})
        new_cpu = {}

        for proc in psutil.process_iter(["pid", "name", "memory_info", "cpu_times"]):
            try:
                info = proc.info
                pid = info["pid"]
                name = info["name"] or ""
                stem = os.path.splitext(name)[0].lower()
                if not name or stem in (
                    "system idle process", "system", "registry", "memory compression", "idle",
                    "svchost", "dwm", "csrss", "lsass", "wininit", "winlogon", "services",
                    "smss", "fontdrvhost", "sihost", "ctfmon", "dllhost", "runtimebroker",
                    "taskhostw", "audiodg", "spoolsv", "wmiprvse", "searchindexer",
                    "msmpeng", "nissrv", "securityhealthservice", "amdrsserv", "amdow",
                    "atiesrxx", "atieclxx", "nvdisplay.container", "presentmon",
                ):
                    continue
                mem = info.get("memory_info")
                ram_mb = (mem.rss / (1024.0 * 1024.0)) if mem else 0.0
                ct = info.get("cpu_times")
                cpu_pct = 0.0
                if ct:
                    total_t = (ct.user or 0.0) + (ct.system or 0.0)
                    new_cpu[pid] = (total_t, now)
                    prev = self._prev_cpu.get(pid)
                    if prev and now > prev[1]:
                        dt = now - prev[1]
                        cpu_pct = max(0.0, min(100.0, ((total_t - prev[0]) / dt) * 100.0 / self._cpu_cores))
                vram = vram_by_pid.get(pid, 0.0)
                gpu = gpu_by_pid.get(pid, 0.0)
                if ram_mb < 15.0 and vram < 10.0 and gpu < 0.5 and cpu_pct < 0.5:
                    continue
                exe_path = ""
                if stem not in BROWSER_EXES and stem not in KNOWN_APPS and stem not in NON_GAME_EXES:
                    try:
                        exe_path = proc.exe() or ""
                    except (psutil.AccessDenied, psutil.NoSuchProcess):
                        pass
                app_name, _cat, _is_g, _is_b = classify_process(name, exe_path, known_games)
                slot = agg[app_name]
                slot["ram_mb"] += ram_mb
                slot["cpu_pct"] = min(100.0, slot["cpu_pct"] + cpu_pct)
                slot["gpu_pct"] = min(100.0, slot["gpu_pct"] + gpu)
                slot["vram_mb"] += vram
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue

        self._prev_cpu = new_cpu
        # İlk 35 en çok kaynak tüketen uygulamayı kaydet (DB'yi temiz tutar)
        ranked = sorted(
            agg.items(),
            key=lambda kv: kv[1]["ram_mb"] + kv[1]["vram_mb"] * 1.5 + kv[1]["gpu_pct"] * 40,
            reverse=True,
        )[:35]
        return dict(ranked)

    def close(self):
        if self._win_pdh:
            self._win_pdh.close()


# =============================================================================
# SİSTEM DENETÇİSİ — Kurulum, Oyun, Sürücü, Başlangıç, Hosts, İndirilenler
# =============================================================================
class SystemAuditor:
    """Anlık sistem görüntüsü alıp önceki görüntüyle farkları pc_events tablosuna yazar."""

    INSTALLER_EXTS = {".exe", ".msi", ".msix", ".appx", ".zip", ".rar", ".7z", ".deb", ".rpm", ".appimage", ".pkg.tar.zst"}

    def __init__(self, store):
        self.store = store
        self.known_games = {}  # lower_exe_or_folder -> display_name

    # --- 1) Kurulu Programlar ---
    def _scan_programs(self):
        progs = {}
        if IS_WIN and winreg:
            roots = [
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
                (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
            ]
            for hive, sub in roots:
                try:
                    with winreg.OpenKey(hive, sub) as k:
                        n = winreg.QueryInfoKey(k)[0]
                        for i in range(n):
                            try:
                                sk_name = winreg.EnumKey(k, i)
                                with winreg.OpenKey(k, sk_name) as sk:
                                    def _v(field):
                                        try:
                                            val, _ = winreg.QueryValueEx(sk, field)
                                            return str(val).strip()
                                        except OSError:
                                            return ""
                                    name = _v("DisplayName")
                                    if not name or _v("SystemComponent") == "1":
                                        continue
                                    progs[name] = {
                                        "ver": _v("DisplayVersion"),
                                        "pub": _v("Publisher"),
                                        "date": _v("InstallDate"),
                                    }
                            except OSError:
                                continue
                except OSError:
                    continue
        else:
            # Linux: pacman veya dpkg + flatpak
            for cmd, sep in ((["pacman", "-Q"], " "), (["dpkg-query", "-W", "-f=${Package}\t${Version}\n"], "\t")):
                try:
                    out = subprocess.check_output(cmd, timeout=10, stderr=subprocess.DEVNULL).decode("utf-8", "replace")
                    for ln in out.splitlines():
                        parts = ln.strip().split(sep, 1)
                        if len(parts) == 2:
                            progs[parts[0]] = {"ver": parts[1], "pub": cmd[0], "date": ""}
                    break
                except Exception:
                    continue
        return progs

    # --- 2) Kurulu Oyunlar (Steam + Epic) ---
    def _scan_games(self):
        games = {}
        steam_libs = set()
        if IS_WIN:
            for base in (r"C:\Program Files (x86)\Steam", r"C:\Program Files\Steam"):
                if os.path.isdir(base):
                    steam_libs.add(os.path.join(base, "steamapps"))
            vdf_candidates = [os.path.join(p, "libraryfolders.vdf") for p in list(steam_libs)]
        else:
            home = os.path.expanduser("~")
            for base in (os.path.join(home, ".local/share/Steam"), os.path.join(home, ".steam/steam")):
                if os.path.isdir(base):
                    steam_libs.add(os.path.join(base, "steamapps"))
            vdf_candidates = [os.path.join(p, "libraryfolders.vdf") for p in list(steam_libs)]

        for vdf in vdf_candidates:
            if os.path.isfile(vdf):
                try:
                    txt = open(vdf, encoding="utf-8", errors="ignore").read()
                    for m in re.finditer(r'"path"\s+"([^"]+)"', txt):
                        p = m.group(1).replace("\\\\", "\\")
                        sa = os.path.join(p, "steamapps")
                        if os.path.isdir(sa):
                            steam_libs.add(sa)
                except OSError:
                    pass

        for sa in steam_libs:
            for acf in glob.glob(os.path.join(sa, "appmanifest_*.acf")):
                try:
                    txt = open(acf, encoding="utf-8", errors="ignore").read()
                    name_m = re.search(r'"name"\s+"([^"]+)"', txt)
                    bid_m = re.search(r'"buildid"\s+"([^"]+)"', txt)
                    dir_m = re.search(r'"installdir"\s+"([^"]+)"', txt)
                    upd_m = re.search(r'"LastUpdated"\s+"(\d+)"', txt)
                    if not name_m:
                        continue
                    name = name_m.group(1).strip()
                    if any(skip in name.lower() for skip in ("steamworks common redistributables", "proton ", "steam linux runtime")):
                        continue
                    inst_dir = dir_m.group(1).strip() if dir_m else ""
                    games[f"steam:{name}"] = {
                        "name": name, "platform": "Steam",
                        "build": bid_m.group(1) if bid_m else "",
                        "dir": inst_dir,
                        "updated": int(upd_m.group(1)) if upd_m else 0,
                    }
                    if inst_dir:
                        self.known_games[inst_dir.lower()] = name
                except OSError:
                    continue

        if IS_WIN:
            epic_dir = os.path.join(os.environ.get("PROGRAMDATA", r"C:\ProgramData"),
                                    r"Epic\EpicGamesLauncher\Data\Manifests")
            for item in glob.glob(os.path.join(epic_dir, "*.item")):
                try:
                    d = json.load(open(item, encoding="utf-8"))
                    name = (d.get("DisplayName") or "").strip()
                    if name:
                        games[f"epic:{name}"] = {
                            "name": name, "platform": "Epic Games",
                            "build": str(d.get("AppVersionString") or ""),
                            "dir": os.path.basename(d.get("InstallLocation") or ""),
                            "updated": int(os.path.getmtime(item)),
                        }
                except Exception:
                    continue
        return games

    # --- 3) Kritik Sistem Durumu (Sürücü, OS, Startup, Hosts, PATH) ---
    def _scan_system(self):
        sys_info = {"os": platform.platform(), "kernel": platform.release()}
        # Hosts dosyası hash
        hosts_p = r"C:\Windows\System32\drivers\etc\hosts" if IS_WIN else "/etc/hosts"
        try:
            sys_info["hosts_sha"] = hashlib.sha256(open(hosts_p, "rb").read()).hexdigest()[:16]
        except OSError:
            sys_info["hosts_sha"] = ""
        # PATH hash
        sys_info["path_sha"] = hashlib.sha256((os.environ.get("PATH") or "").encode("utf-8")).hexdigest()[:16]

        startup = {}
        if IS_WIN and winreg:
            # Windows build & UBR
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion") as k:
                    b = winreg.QueryValueEx(k, "CurrentBuildNumber")[0]
                    ubr = winreg.QueryValueEx(k, "UBR")[0]
                    sys_info["win_build"] = f"{b}.{ubr}"
            except OSError:
                pass
            # GPU Sürücü sürümleri
            drivers = {}
            try:
                cls_key = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, cls_key) as k:
                    for i in range(16):
                        try:
                            sk_name = f"{i:04d}"
                            with winreg.OpenKey(k, sk_name) as sk:
                                desc = winreg.QueryValueEx(sk, "DriverDesc")[0]
                                ver = winreg.QueryValueEx(sk, "DriverVersion")[0]
                                drivers[str(desc)] = str(ver)
                        except OSError:
                            continue
            except OSError:
                pass
            sys_info["gpu_drivers"] = drivers
            # Başlangıç programları (HKCU/HKLM Run)
            for label, hive, path in (
                ("HKCU", winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run"),
                ("HKLM", winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Run"),
            ):
                try:
                    with winreg.OpenKey(hive, path) as k:
                        n = winreg.QueryInfoKey(k)[1]
                        for i in range(n):
                            nm, val, _ = winreg.EnumValue(k, i)
                            startup[f"{label}:{nm}"] = str(val)
                except OSError:
                    continue
        else:
            # Linux autostart
            ad = os.path.join(os.path.expanduser("~"), ".config/autostart")
            if os.path.isdir(ad):
                for fn in sorted(os.listdir(ad)):
                    startup[fn] = fn
        sys_info["startup"] = startup
        return sys_info

    # --- 4) İndirilenler klasörü kurulum dosyaları ---
    def _scan_downloads(self):
        dl_dir = os.path.join(os.path.expanduser("~"), "Downloads")
        out = {}
        if not os.path.isdir(dl_dir):
            return out
        try:
            for entry in os.scandir(dl_dir):
                if not entry.is_file():
                    continue
                ext = os.path.splitext(entry.name)[1].lower()
                if ext in self.INSTALLER_EXTS:
                    st = entry.stat()
                    out[entry.name] = {"size": st.st_size, "mtime": int(st.st_mtime)}
        except OSError:
            pass
        return out

    # --- Farkları hesapla ve kaydet ---
    def run_audit(self):
        now = time.time()
        # 1) Programlar
        progs = self._scan_programs()
        prev_progs = self.store.load_snapshot("programs")
        if prev_progs is None:
            self.store.save_snapshot("programs", progs)
            # İlk çalışmada son 7 gün içinde kurulmuş Windows programlarını geçmişe ekle
            cutoff_d = time.strftime("%Y%m%d", time.localtime(now - 7 * 86400))
            for name, meta in progs.items():
                dt = meta.get("date") or ""
                if len(dt) == 8 and dt.isdigit() and dt >= cutoff_d:
                    try:
                        ts = time.mktime(time.strptime(dt, "%Y%m%d")) + 43200
                    except ValueError:
                        ts = now
                    ver = f" v{meta['ver']}" if meta.get("ver") else ""
                    self.store.add_event(
                        "program_kuruldu", f"Program Kuruldu: {name}{ver}",
                        meta.get("pub", ""), "important", ts=ts,
                        event_key=f"init_prog:{name}:{dt}",
                    )
        else:
            for name, meta in progs.items():
                if name not in prev_progs:
                    ver = f" v{meta['ver']}" if meta.get("ver") else ""
                    self.store.add_event(
                        "program_kuruldu", f"Program Kuruldu: {name}{ver}",
                        meta.get("pub", ""), "important", ts=now,
                    )
                elif meta.get("ver") and prev_progs[name].get("ver") and meta["ver"] != prev_progs[name]["ver"]:
                    self.store.add_event(
                        "program_guncellendi",
                        f"Program Güncellendi: {name}",
                        f"{prev_progs[name]['ver']} → {meta['ver']}",
                        "info", ts=now,
                    )
            for name, meta in prev_progs.items():
                if name not in progs:
                    self.store.add_event(
                        "program_kaldirildi", f"Program Kaldırıldı: {name}",
                        meta.get("ver", ""), "important", ts=now,
                    )
            if progs != prev_progs:
                self.store.save_snapshot("programs", progs)

        # 2) Oyunlar
        games = self._scan_games()
        prev_games = self.store.load_snapshot("games")
        if prev_games is None:
            self.store.save_snapshot("games", games)
            for k, g in games.items():
                if g.get("updated") and g["updated"] >= now - 7 * 86400:
                    self.store.add_event(
                        "oyun_guncellendi",
                        f"Oyun Kuruldu/Güncellendi: {g['name']}",
                        f"{g['platform']} (build {g['build']})" if g.get("build") else g["platform"],
                        "important", ts=g["updated"],
                        event_key=f"init_game:{k}:{g.get('build','')}",
                    )
        else:
            for k, g in games.items():
                if k not in prev_games:
                    self.store.add_event(
                        "oyun_kuruldu", f"Oyun Kuruldu: {g['name']}",
                        g["platform"], "important", ts=now,
                    )
                elif g.get("build") and prev_games[k].get("build") and g["build"] != prev_games[k]["build"]:
                    self.store.add_event(
                        "oyun_guncellendi", f"Oyun Güncellendi: {g['name']}",
                        f"{g['platform']}: build {prev_games[k]['build']} → {g['build']}",
                        "info", ts=now,
                    )
            for k, g in prev_games.items():
                if k not in games:
                    self.store.add_event(
                        "oyun_kaldirildi", f"Oyun Kaldırıldı: {g['name']}",
                        g["platform"], "important", ts=now,
                    )
            if games != prev_games:
                self.store.save_snapshot("games", games)

        # 3) Kritik Sistem Değişiklikleri
        sys_now = self._scan_system()
        prev_sys = self.store.load_snapshot("system")
        if prev_sys is None:
            self.store.save_snapshot("system", sys_now)
            drv_cnt = len(sys_now.get("gpu_drivers") or {})
            self.store.add_event(
                "sistem",
                "PC & Oyun İzleyici v4 İlk Sistem Taraması",
                f"{len(progs)} program, {len(games)} oyun, {len(sys_now.get('startup', {}))} başlangıç öğesi, {drv_cnt} GPU sürücüsü kaydedildi.",
                "info", ts=now, event_key="v4_initial_baseline",
            )
        else:
            if sys_now.get("win_build") and prev_sys.get("win_build") and sys_now["win_build"] != prev_sys["win_build"]:
                self.store.add_event(
                    "kritik_degisiklik", "Windows Sürümü Güncellendi",
                    f"{prev_sys['win_build']} → {sys_now['win_build']}", "critical", ts=now,
                )
            if sys_now.get("kernel") != prev_sys.get("kernel"):
                self.store.add_event(
                    "kritik_degisiklik", "Çekirdek / OS Sürümü Değişti",
                    f"{prev_sys.get('kernel')} → {sys_now.get('kernel')}", "critical", ts=now,
                )
            if sys_now.get("hosts_sha") and prev_sys.get("hosts_sha") and sys_now["hosts_sha"] != prev_sys["hosts_sha"]:
                self.store.add_event(
                    "kritik_degisiklik", "Hosts Dosyası Değiştirildi",
                    "Sistem hosts dosyasında değişiklik tespit edildi.", "critical", ts=now,
                )
            if sys_now.get("path_sha") and prev_sys.get("path_sha") and sys_now["path_sha"] != prev_sys["path_sha"]:
                self.store.add_event(
                    "kritik_degisiklik", "Ortam Değişkeni (PATH) Değişti",
                    "Kullanıcı veya sistem PATH ayarı güncellendi.", "important", ts=now,
                )
            old_drv = prev_sys.get("gpu_drivers") or {}
            for gpu_n, ver in (sys_now.get("gpu_drivers") or {}).items():
                if gpu_n in old_drv and old_drv[gpu_n] != ver:
                    self.store.add_event(
                        "kritik_degisiklik", f"Ekran Kartı Sürücüsü Güncellendi: {gpu_n}",
                        f"{old_drv[gpu_n]} → {ver}", "critical", ts=now,
                    )
            old_st = prev_sys.get("startup") or {}
            new_st = sys_now.get("startup") or {}
            for k, v in new_st.items():
                if k not in old_st:
                    self.store.add_event(
                        "kritik_degisiklik", f"Yeni Başlangıç Programı Eklendi: {k}",
                        v[:200], "critical", ts=now,
                    )
            for k in old_st:
                if k not in new_st:
                    self.store.add_event(
                        "kritik_degisiklik", f"Başlangıç Programı Kaldırıldı: {k}",
                        old_st[k][:200], "important", ts=now,
                    )
            if sys_now != prev_sys:
                self.store.save_snapshot("system", sys_now)

        # 4) İndirilen kurulum/arşiv dosyaları
        dls = self._scan_downloads()
        prev_dls = self.store.load_snapshot("downloads")
        if prev_dls is None:
            self.store.save_snapshot("downloads", dls)
            for fn, meta in dls.items():
                if meta["mtime"] >= now - 3 * 86400:
                    self.store.add_event(
                        "dosya_indirildi", f"İndirilen Dosya: {fn}",
                        fmt_mb_tr(meta["size"] / (1024 * 1024)), "info",
                        ts=meta["mtime"], event_key=f"dl:{fn}:{meta['size']}",
                    )
        else:
            for fn, meta in dls.items():
                if fn not in prev_dls:
                    self.store.add_event(
                        "dosya_indirildi", f"Yeni İndirilen Kurulum/Arşiv: {fn}",
                        fmt_mb_tr(meta["size"] / (1024 * 1024)), "info", ts=now,
                    )
            if dls != prev_dls:
                self.store.save_snapshot("downloads", dls)


# =============================================================================
# HAFİF GITHUB SENKRONİZYONU (Özel depo: oyunlar + aktivite özeti + olaylar)
# =============================================================================
def github_api_request(method, url, token=None, body=None, timeout=20):
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "oyun-izleyici/4.0",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token.strip()}"
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return resp.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            msg = json.loads(raw).get("message") or raw
        except ValueError:
            msg = raw
        raise RuntimeError(f"HTTP {e.code}: {msg[:180]}") from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"Ağ hatası: {e.reason}") from None


class DaemonSync:
    """Arka plan servisinin kendi GitHub senkronu (aktivite_ozet.json + olaylar.jsonl + güncelleme)."""

    def __init__(self, store, cfg):
        self.store = store
        self.cfg = cfg
        self.mid = cfg["kimlik"]
        self.last_ok = None
        self.last_error = None
        self.update_available = False
        self.remote_version = None
        self._busy = False
        self._next_at = time.time() + 20

    @staticmethod
    def _write_if_changed(path, text):
        try:
            with open(path, encoding="utf-8", newline="") as f:
                if f.read() == text:
                    return False
        except OSError:
            pass
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp, path)
        return True

    def _api_read(self, slug, rel, branch, token):
        q = urllib.parse.quote(rel, safe="/")
        url = f"https://api.github.com/repos/{slug}/contents/{q}?ref={urllib.parse.quote(branch)}"
        _, data = github_api_request("GET", url, token=token)
        if not isinstance(data, dict):
            return None, None
        sha = data.get("sha")
        if data.get("encoding") == "base64" and data.get("content") is not None:
            return base64.b64decode(data["content"].encode("ascii")).decode("utf-8", errors="replace"), sha
        if sha:
            _, blob = github_api_request("GET", f"https://api.github.com/repos/{slug}/git/blobs/{sha}", token=token)
            if blob.get("encoding") == "base64" and blob.get("content") is not None:
                return base64.b64decode(blob["content"].encode("ascii")).decode("utf-8", errors="replace"), sha
        return None, sha

    def _api_put_if_changed(self, slug, rel, branch, token, local_txt, msg):
        remote_txt, remote_sha = None, None
        try:
            remote_txt, remote_sha = self._api_read(slug, rel, branch, token)
        except RuntimeError as e:
            if "HTTP 404" not in str(e):
                raise
        if remote_txt == local_txt:
            return
        payload = {
            "message": msg,
            "content": base64.b64encode(local_txt.encode("utf-8")).decode("ascii"),
            "branch": branch,
        }
        if remote_sha:
            payload["sha"] = remote_sha
        q = urllib.parse.quote(rel, safe="/")
        github_api_request("PUT", f"https://api.github.com/repos/{slug}/contents/{q}", token=token, body=payload)

    def _git_cmd(self, git_exe, *args, timeout=90):
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
        if not IS_WIN:
            env.update(GIT_ASKPASS="true", SSH_ASKPASS="true")
        p = subprocess.run([git_exe, "-C", REPO_DIR, *args], capture_output=True, text=True,
                           timeout=timeout, env=env, **_hidden())
        if p.returncode != 0:
            msg = (p.stderr or p.stdout or "").strip()
            raise RuntimeError(msg.splitlines()[-1] if msg else f"git {args[0]} başarısız")
        return p.stdout

    def _find_git(self):
        g = shutil.which("git")
        if g:
            return g
        if IS_WIN:
            for c in (r"C:\Program Files\Git\cmd\git.exe", r"C:\Program Files (x86)\Git\cmd\git.exe"):
                if os.path.isfile(c):
                    return c
        return None

    def maybe_sync(self, force=False):
        if self._busy:
            return
        if not force and time.time() < self._next_at:
            return
        self.cfg = load_machine_config()
        slug = normalize_repo_slug(self.cfg.get("kayit_deposu", ""))
        token = read_github_token()
        git_exe = self._find_git() if os.path.isdir(os.path.join(REPO_DIR, ".git")) else None
        if self.cfg.get("senkron", "acik") == "kapali" or (not slug and not git_exe):
            self._next_at = time.time() + SYNC_INTERVAL_SEC
            return
        self._next_at = time.time() + SYNC_INTERVAL_SEC
        # Önce DB verilerini ana iş parçacığında hazırla (SQLite thread-safe)
        bundle_txt = json.dumps(self.store.export_activity_bundle(), ensure_ascii=False, separators=(",", ":")) + "\n"
        events_txt = self.store.export_events_lines()
        own_dir = os.path.join(REPO_DIR, "makineler", self.mid)
        self._write_if_changed(os.path.join(own_dir, "aktivite_ozet.json"), bundle_txt)
        self._write_if_changed(os.path.join(own_dir, "olaylar.jsonl"), events_txt)
        self.store.changed_since_sync = False

        self._busy = True
        if git_exe:
            threading.Thread(target=self._git_worker, args=(git_exe,), daemon=True).start()
        else:
            threading.Thread(
                target=self._worker,
                args=(slug, token, bundle_txt, events_txt),
                daemon=True,
            ).start()

    def _git_worker(self, git_exe):
        """Linux/Windows Git+SSH deploy key deposu olan PC'ler için otomatik senkron."""
        remote_imports = []
        try:
            rel = os.path.join("makineler", self.mid)
            self._git_cmd(git_exe, "add", "--", rel)
            if self._git_cmd(git_exe, "status", "--porcelain", "--", rel).strip():
                self._git_cmd(
                    git_exe,
                    "-c", f"user.name=oyun-izleyici ({self.mid})",
                    "-c", f"user.email={self.mid}@oyun-izleyici.local",
                    "commit", "-q", "-m", f"{self.cfg.get('ad') or self.mid}: aktivite ve olaylar güncellendi",
                )
            self._git_cmd(git_exe, "pull", "-q", "--rebase", "--autostash")
            ahead = self._git_cmd(git_exe, "rev-list", "--count", "@{u}..HEAD").strip()
            if ahead and ahead != "0":
                self._git_cmd(git_exe, "push", "-q")

            base = os.path.join(REPO_DIR, "makineler")
            for d in sorted(glob.glob(os.path.join(base, "*"))):
                mid = os.path.basename(d)
                if mid == self.mid or not os.path.isdir(d):
                    continue
                bundle_obj, evt_rows = None, []
                bp = os.path.join(d, "aktivite_ozet.json")
                if os.path.isfile(bp):
                    try:
                        bundle_obj = json.loads(open(bp, encoding="utf-8").read())
                    except Exception:
                        pass
                ep = os.path.join(d, "olaylar.jsonl")
                if os.path.isfile(ep):
                    try:
                        for ln in open(ep, encoding="utf-8"):
                            if ln.strip():
                                evt_rows.append(json.loads(ln))
                    except Exception:
                        pass
                remote_imports.append((mid, bundle_obj, evt_rows))

            prog_p = os.path.join(REPO_DIR, "program", "oyun-izleyici")
            if os.path.isfile(prog_p):
                try:
                    self._check_ver(open(prog_p, encoding="utf-8", errors="ignore").read(4096))
                except OSError:
                    pass

            self._pending_imports = remote_imports
            self.last_ok = time.time()
            self.last_error = None
        except Exception as e:
            self.last_error = str(e)
        finally:
            self._busy = False

    def _worker(self, slug, token, bundle_txt, events_txt):
        remote_imports = []
        try:
            _, repo = github_api_request("GET", f"https://api.github.com/repos/{slug}", token=token)
            branch = repo.get("default_branch") or "main"
            msg = f"{self.cfg.get('ad') or self.mid}: aktivite ve olaylar güncellendi"

            if token:
                self._api_put_if_changed(slug, f"makineler/{self.mid}/aktivite_ozet.json", branch, token, bundle_txt, msg)
                self._api_put_if_changed(slug, f"makineler/{self.mid}/olaylar.jsonl", branch, token, events_txt, msg)

            # Diğer makinelerin aktivite ve olay dosyalarını indir
            entries = []
            try:
                _, listing = github_api_request(
                    "GET", f"https://api.github.com/repos/{slug}/contents/makineler?ref={urllib.parse.quote(branch)}",
                    token=token,
                )
                if isinstance(listing, list):
                    entries = [it["name"] for it in listing if it.get("type") == "dir" and it.get("name")]
            except RuntimeError as e:
                if "HTTP 404" not in str(e):
                    raise

            for mid in entries:
                if mid == self.mid:
                    continue
                tdir = os.path.join(REPO_DIR, "makineler", mid)
                os.makedirs(tdir, exist_ok=True)
                bundle_obj, evt_rows = None, []
                for fname in ("aktivite_ozet.json", "olaylar.jsonl", "makine.json"):
                    try:
                        txt, _ = self._api_read(slug, f"makineler/{mid}/{fname}", branch, token)
                        if txt is not None:
                            self._write_if_changed(os.path.join(tdir, fname), txt)
                            if fname == "aktivite_ozet.json":
                                bundle_obj = json.loads(txt)
                            elif fname == "olaylar.jsonl":
                                for ln in txt.splitlines():
                                    if ln.strip():
                                        try:
                                            evt_rows.append(json.loads(ln))
                                        except ValueError:
                                            pass
                    except Exception:
                        pass
                remote_imports.append((mid, bundle_obj, evt_rows))

            # Uygulama güncellemesi kontrolü (program/oyun-izleyici & program/pc_izleyici.py)
            for rel, local_name in (("program/oyun-izleyici", "oyun-izleyici"), ("program/pc_izleyici.py", "pc_izleyici.py")):
                try:
                    ptxt, _ = self._api_read(slug, rel, branch, token)
                    if ptxt is not None:
                        self._write_if_changed(os.path.join(REPO_DIR, "program", local_name), ptxt)
                        if local_name == "oyun-izleyici":
                            self._check_ver(ptxt)
                except Exception:
                    pass

            self._pending_imports = remote_imports
            self.last_ok = time.time()
            self.last_error = None
        except Exception as e:
            self.last_error = str(e)
        finally:
            self._busy = False

    def apply_pending_imports(self):
        items = getattr(self, "_pending_imports", None)
        if not items:
            return
        self._pending_imports = None
        for mid, bundle_obj, evt_rows in items:
            try:
                if bundle_obj:
                    self.store.import_remote_activity(mid, bundle_obj)
                if evt_rows:
                    self.store.import_remote_events(mid, evt_rows)
            except Exception:
                pass

    def _check_ver(self, txt):
        m = re.search(r'^APP_VERSION\s*=\s*["\'](\d+)\.(\d+)\.(\d+)["\']', txt[:4096], re.M)
        if not m:
            return
        rem = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
        loc = tuple(int(x) for x in APP_VERSION.split("."))
        if rem[:2] > loc[:2]:
            self.update_available = True
            self.remote_version = f"{rem[0]}.{rem[1]}.{rem[2]}"
        else:
            self.update_available = False
            self.remote_version = None


# =============================================================================
# SAF WIN32 SİSTEM TEPSİSİ (Qt YOK — ~1 MB RAM!)
# =============================================================================
if IS_WIN:
    WM_DESTROY = 0x0002
    WM_CLOSE = 0x0010
    WM_COMMAND = 0x0111
    WM_USER = 0x0400
    WM_TRAY = WM_USER + 20
    WM_LBUTTONDBLCLK = 0x0203
    WM_RBUTTONUP = 0x0205
    WM_LBUTTONUP = 0x0202
    NIM_ADD = 0x00000000
    NIM_MODIFY = 0x00000001
    NIM_DELETE = 0x00000002
    NIF_MESSAGE = 0x00000001
    NIF_ICON = 0x00000002
    NIF_TIP = 0x00000004
    NIF_INFO = 0x00000010
    MF_STRING = 0x00000000
    MF_SEPARATOR = 0x00000800
    MF_CHECKED = 0x00000008
    MF_DEFAULT = 0x00001000
    TPM_RIGHTBUTTON = 0x0002
    TPM_BOTTOMALIGN = 0x0020

    WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t)

    class WNDCLASSW(ctypes.Structure):
        _fields_ = [
            ("style", ctypes.c_uint), ("lpfnWndProc", WNDPROC),
            ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
            ("hInstance", ctypes.c_void_p), ("hIcon", ctypes.c_void_p),
            ("hCursor", ctypes.c_void_p), ("hbrBackground", ctypes.c_void_p),
            ("lpszMenuName", ctypes.c_wchar_p), ("lpszClassName", ctypes.c_wchar_p),
        ]

    class NOTIFYICONDATAW(ctypes.Structure):
        _fields_ = [
            ("cbSize", ctypes.c_uint), ("hWnd", ctypes.c_void_p),
            ("uID", ctypes.c_uint), ("uFlags", ctypes.c_uint),
            ("uCallbackMessage", ctypes.c_uint), ("hIcon", ctypes.c_void_p),
            ("szTip", ctypes.c_wchar * 128), ("dwState", ctypes.c_uint),
            ("dwStateMask", ctypes.c_uint), ("szInfo", ctypes.c_wchar * 256),
            ("uVersion", ctypes.c_uint), ("szInfoTitle", ctypes.c_wchar * 64),
            ("dwInfoFlags", ctypes.c_uint),
        ]

    class POINT(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

    class MSG(ctypes.Structure):
        _fields_ = [
            ("hwnd", ctypes.c_void_p), ("message", ctypes.c_uint),
            ("wParam", ctypes.c_size_t), ("lParam", ctypes.c_ssize_t),
            ("time", ctypes.c_uint), ("pt", POINT),
        ]

    class BITMAPV5HEADER(ctypes.Structure):
        _fields_ = [
            ("bV5Size", ctypes.c_uint32), ("bV5Width", ctypes.c_int32),
            ("bV5Height", ctypes.c_int32), ("bV5Planes", ctypes.c_uint16),
            ("bV5BitCount", ctypes.c_uint16), ("bV5Compression", ctypes.c_uint32),
            ("bV5SizeImage", ctypes.c_uint32), ("bV5XPelsPerMeter", ctypes.c_int32),
            ("bV5YPelsPerMeter", ctypes.c_int32), ("bV5ClrUsed", ctypes.c_uint32),
            ("bV5ClrImportant", ctypes.c_uint32), ("bV5RedMask", ctypes.c_uint32),
            ("bV5GreenMask", ctypes.c_uint32), ("bV5BlueMask", ctypes.c_uint32),
            ("bV5AlphaMask", ctypes.c_uint32), ("bV5CSType", ctypes.c_uint32),
            ("bV5Endpoints", ctypes.c_byte * 36), ("bV5GammaRed", ctypes.c_uint32),
            ("bV5GammaGreen", ctypes.c_uint32), ("bV5GammaBlue", ctypes.c_uint32),
            ("bV5Intent", ctypes.c_uint32), ("bV5ProfileData", ctypes.c_uint32),
            ("bV5ProfileSize", ctypes.c_uint32), ("bV5Reserved", ctypes.c_uint32),
        ]

    class ICONINFO(ctypes.Structure):
        _fields_ = [
            ("fIcon", ctypes.c_int), ("xHotspot", ctypes.c_uint32),
            ("yHotspot", ctypes.c_uint32), ("hbmMask", ctypes.c_void_p),
            ("hbmColor", ctypes.c_void_p),
        ]

    def _create_tray_hicon():
        """16x16 parlak zümrüt yeşili 'nabız/oyun' tepsi simgesi üretir."""
        try:
            gdi32 = ctypes.windll.gdi32
            user32 = ctypes.windll.user32
            user32.GetDC.argtypes = [ctypes.c_void_p]
            user32.GetDC.restype = ctypes.c_void_p
            user32.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            user32.CreateIconIndirect.argtypes = [ctypes.POINTER(ICONINFO)]
            user32.CreateIconIndirect.restype = ctypes.c_void_p
            user32.LoadIconW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            user32.LoadIconW.restype = ctypes.c_void_p
            gdi32.CreateDIBSection.argtypes = [
                ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint,
                ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, ctypes.c_uint,
            ]
            gdi32.CreateDIBSection.restype = ctypes.c_void_p
            gdi32.CreateBitmap.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p]
            gdi32.CreateBitmap.restype = ctypes.c_void_p
            gdi32.DeleteObject.argtypes = [ctypes.c_void_p]

            w, h = 16, 16
            bi = BITMAPV5HEADER()
            bi.bV5Size = ctypes.sizeof(BITMAPV5HEADER)
            bi.bV5Width = w
            bi.bV5Height = -h
            bi.bV5Planes = 1
            bi.bV5BitCount = 32
            bi.bV5Compression = 3  # BI_BITFIELDS
            bi.bV5RedMask = 0x00FF0000
            bi.bV5GreenMask = 0x0000FF00
            bi.bV5BlueMask = 0x000000FF
            bi.bV5AlphaMask = 0xFF000000

            bits = ctypes.c_void_p()
            hdc = user32.GetDC(None)
            hbm_color = gdi32.CreateDIBSection(hdc, ctypes.byref(bi), 0, ctypes.byref(bits), None, 0)
            user32.ReleaseDC(None, hdc)
            if not hbm_color or not bits.value:
                return user32.LoadIconW(None, ctypes.c_void_p(32512))

            px = bytearray(w * h * 4)
            # Koyu yuvarlak zemin + parlak yeşil nabız çizgisi
            pulse = {(3, 9), (4, 9), (5, 9), (6, 6), (7, 4), (8, 11), (9, 8), (10, 7), (11, 9), (12, 9)}
            for y in range(h):
                for x in range(w):
                    dx, dy = x - 7.5, y - 7.5
                    dist = (dx * dx + dy * dy) ** 0.5
                    idx = (y * w + x) * 4
                    if (x, y) in pulse:
                        px[idx:idx + 4] = bytes([0x78, 0xFF, 0x45, 0xFF])  # BGRA parlak yeşil
                    elif dist <= 7.2:
                        if dist >= 6.0:
                            px[idx:idx + 4] = bytes([0x68, 0xD8, 0x35, 0xFF])  # dış halka
                        else:
                            px[idx:idx + 4] = bytes([0x24, 0x1A, 0x16, 0xF5])  # koyu zemin
            ctypes.memmove(bits.value, bytes(px), len(px))
            hbm_mask = gdi32.CreateBitmap(w, h, 1, 1, None)
            ii = ICONINFO(1, 0, 0, hbm_mask, hbm_color)
            hicon = user32.CreateIconIndirect(ctypes.byref(ii))
            gdi32.DeleteObject(hbm_color)
            gdi32.DeleteObject(hbm_mask)
            return hicon or user32.LoadIconW(None, ctypes.c_void_p(32512))
        except Exception:
            u = ctypes.windll.user32
            u.LoadIconW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            u.LoadIconW.restype = ctypes.c_void_p
            return u.LoadIconW(None, ctypes.c_void_p(32512))

    class WinTrayController:
        """Hafif Win32 sistem tepsisi (Qt gerektirmez)."""

        CMD_DASHBOARD = 1001
        CMD_WEB_KOMUTA = 1007
        CMD_TOGGLE_HUD = 1002
        CMD_TOGGLE_AUTO = 1003
        CMD_TOGGLE_ALWAYS = 1004
        CMD_SYNC = 1005
        CMD_SETTINGS = 1006
        CMD_EXIT = 1099

        def __init__(self, daemon):
            self.daemon = daemon
            self.hwnd = None
            self.hicon = None
            self._wndproc = WNDPROC(self._proc)
            self._u32 = ctypes.windll.user32
            self._s32 = ctypes.windll.shell32
            self._k32 = ctypes.windll.kernel32
            self._k32.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
            self._k32.GetModuleHandleW.restype = ctypes.c_void_p
            self._u32.DefWindowProcW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
            self._u32.DefWindowProcW.restype = ctypes.c_ssize_t
            self._u32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
            self._u32.RegisterClassW.restype = ctypes.c_uint16
            self._u32.CreateWindowExW.argtypes = [
                ctypes.c_uint, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint,
                ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
            ]
            self._u32.CreateWindowExW.restype = ctypes.c_void_p
            self._u32.DestroyWindow.argtypes = [ctypes.c_void_p]
            self._u32.CreatePopupMenu.argtypes = []
            self._u32.CreatePopupMenu.restype = ctypes.c_void_p
            self._u32.DestroyMenu.argtypes = [ctypes.c_void_p]
            self._u32.AppendMenuW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_wchar_p]
            self._u32.SetMenuDefaultItem.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint]
            self._u32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
            self._u32.TrackPopupMenu.argtypes = [
                ctypes.c_void_p, ctypes.c_uint, ctypes.c_int, ctypes.c_int,
                ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p,
            ]
            self._u32.PostMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
            self._u32.GetCursorPos.argtypes = [ctypes.POINTER(POINT)]
            self._u32.MsgWaitForMultipleObjects.argtypes = [
                ctypes.c_uint, ctypes.c_void_p, ctypes.c_int, ctypes.c_uint, ctypes.c_uint,
            ]
            self._u32.PeekMessageW.argtypes = [
                ctypes.POINTER(MSG), ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint, ctypes.c_uint,
            ]
            self._u32.TranslateMessage.argtypes = [ctypes.POINTER(MSG)]
            self._u32.DispatchMessageW.argtypes = [ctypes.POINTER(MSG)]
            self._s32.Shell_NotifyIconW.argtypes = [ctypes.c_uint, ctypes.POINTER(NOTIFYICONDATAW)]

        def start(self):
            hinst = self._k32.GetModuleHandleW(None)
            cls_name = f"OyunIzleyiciDaemonTray_{os.getpid()}"
            wc = WNDCLASSW()
            wc.lpfnWndProc = self._wndproc
            wc.hInstance = hinst
            wc.lpszClassName = cls_name
            self._u32.RegisterClassW(ctypes.byref(wc))
            self.hwnd = self._u32.CreateWindowExW(
                0, cls_name, "Oyun İzleyici Servis", 0, 0, 0, 0, 0, None, None, hinst, None
            )
            self.hicon = _create_tray_hicon()
            self._nid = NOTIFYICONDATAW()
            self._nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
            self._nid.hWnd = self.hwnd
            self._nid.uID = 1
            self._nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
            self._nid.uCallbackMessage = WM_TRAY
            self._nid.hIcon = self.hicon
            self._nid.szTip = f"Oyun & PC İzleyici v{APP_VERSION} · Hafif Mod"
            self._s32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(self._nid))

        def update_tip(self, text):
            if not self.hwnd:
                return
            try:
                self._nid.uFlags = NIF_TIP
                self._nid.szTip = text[:127]
                self._s32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(self._nid))
            except Exception:
                pass

        def balloon(self, title, body):
            if not self.hwnd:
                return
            try:
                self._nid.uFlags = NIF_INFO
                self._nid.szInfoTitle = title[:63]
                self._nid.szInfo = body[:255]
                self._nid.dwInfoFlags = 1
                self._s32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(self._nid))
            except Exception:
                pass

        def _show_menu(self):
            menu = self._u32.CreatePopupMenu()
            hud_on = self.daemon.is_hud_running()
            cfg = self.daemon.cfg
            auto_on = cfg.get("hud_oto", "acik") != "kapali"
            always_on = cfg.get("hud_hep_acik", "kapali") == "acik"
            ram_mb = self.daemon.self_ram_mb()

            self._u32.AppendMenuW(menu, MF_STRING, self.CMD_DASHBOARD, "Meraklısına Veriler")
            self._u32.SetMenuDefaultItem(menu, self.CMD_DASHBOARD, 0)
            self._u32.AppendMenuW(menu, MF_STRING, self.CMD_WEB_KOMUTA, "🌐 Web Komuta Merkezi (serhatbahar.com/komuta)")
            self._u32.AppendMenuW(
                menu, MF_STRING, self.CMD_TOGGLE_HUD,
                "🎮 Canlı Oyun HUD'unu Kapat" if hud_on else "🎮 Canlı Oyun HUD'unu Aç",
            )
            self._u32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
            self._u32.AppendMenuW(
                menu, MF_STRING | (MF_CHECKED if auto_on else 0),
                self.CMD_TOGGLE_AUTO, "Oyun Açılınca HUD Otomatik Aç",
            )
            self._u32.AppendMenuW(
                menu, MF_STRING | (MF_CHECKED if always_on else 0),
                self.CMD_TOGGLE_ALWAYS, "HUD Hep Açık Kalsın",
            )
            self._u32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
            sync_lbl = "🔄 Şimdi Senkronize Et"
            if self.daemon.sync.last_ok:
                sync_lbl += f" (son {time.strftime('%H:%M', time.localtime(self.daemon.sync.last_ok))})"
            self._u32.AppendMenuW(menu, MF_STRING, self.CMD_SYNC, sync_lbl)
            self._u32.AppendMenuW(menu, MF_STRING, self.CMD_SETTINGS, f"⚙️ GitHub && Ayarlar…  ({ram_mb:.0f} MB)")
            self._u32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
            self._u32.AppendMenuW(menu, MF_STRING, self.CMD_EXIT, "❌ Çıkış")

            pt = POINT()
            self._u32.GetCursorPos(ctypes.byref(pt))
            self._u32.SetForegroundWindow(self.hwnd)
            self._u32.TrackPopupMenu(menu, TPM_RIGHTBUTTON | TPM_BOTTOMALIGN, pt.x, pt.y, 0, self.hwnd, None)
            self._u32.PostMessageW(self.hwnd, 0, 0, 0)
            self._u32.DestroyMenu(menu)

        def _proc(self, hwnd, msg, wp, lp):
            if msg == WM_TRAY:
                ev = lp & 0xFFFF
                if ev == WM_RBUTTONUP:
                    self._show_menu()
                elif ev == WM_LBUTTONDBLCLK:
                    self.daemon.launch_dashboard()
                return 0
            if msg == WM_COMMAND:
                cmd = wp & 0xFFFF
                if cmd == self.CMD_DASHBOARD:
                    self.daemon.launch_dashboard()
                elif cmd == self.CMD_WEB_KOMUTA:
                    import webbrowser
                    webbrowser.open("https://serhatbahar.com/komuta/")
                elif cmd == self.CMD_TOGGLE_HUD:
                    self.daemon.toggle_hud_manual()
                elif cmd == self.CMD_TOGGLE_AUTO:
                    cur = self.daemon.cfg.get("hud_oto", "acik") != "kapali"
                    nv = "kapali" if cur else "acik"
                    self.daemon.cfg["hud_oto"] = nv
                    save_machine_config({"hud_oto": nv})
                elif cmd == self.CMD_TOGGLE_ALWAYS:
                    cur = self.daemon.cfg.get("hud_hep_acik", "kapali") == "acik"
                    nv = "kapali" if cur else "acik"
                    self.daemon.cfg["hud_hep_acik"] = nv
                    save_machine_config({"hud_hep_acik": nv})
                    if nv == "acik":
                        self.daemon.ensure_hud_running(auto=False)
                    else:
                        self.daemon.stop_hud()
                elif cmd == self.CMD_SYNC:
                    self.daemon.sync.maybe_sync(force=True)
                    self.balloon("Oyun & PC İzleyici", "GitHub senkronizasyonu başlatıldı…")
                elif cmd == self.CMD_SETTINGS:
                    self.daemon.launch_dashboard(extra_args=["--sync"])
                elif cmd == self.CMD_EXIT:
                    self.daemon.request_stop()
                    self._u32.PostQuitMessage(0)
                return 0
            if msg in (WM_CLOSE, WM_DESTROY):
                self.daemon.request_stop()
                self._u32.PostQuitMessage(0)
                return 0
            return self._u32.DefWindowProcW(hwnd, msg, wp, lp)

        def pump_loop(self):
            msg = MSG()
            while not self.daemon.stop_requested:
                # 250 ms arayla Win32 mesajlarını işle (sıfır CPU)
                res = self._u32.MsgWaitForMultipleObjects(0, None, False, 250, 0x04FF)
                while self._u32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
                    if msg.message == 0x0012:  # WM_QUIT
                        self.daemon.request_stop()
                        break
                    self._u32.TranslateMessage(ctypes.byref(msg))
                    self._u32.DispatchMessageW(ctypes.byref(msg))
                self.daemon.step()
            self.cleanup()

        def cleanup(self):
            if self.hwnd:
                try:
                    self._s32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid))
                    self._u32.DestroyWindow(self.hwnd)
                except Exception:
                    pass
                self.hwnd = None


# =============================================================================
# ANA HAFİF ARKA PLAN SERVİSİ (PCActivityDaemon)
# =============================================================================
class PCActivityDaemon:
    """7/24 düşük RAM'li ana servis: aktivite + web + kurulum + kaynak + otomatik HUD."""

    def __init__(self, app_script=None):
        self.app_script = app_script or INSTALLED_BIN
        self.cfg = load_machine_config()
        self.store = ActivityStore(self.cfg["kimlik"])
        self.auditor = SystemAuditor(self.store)
        self.browser = BrowserDomainResolver()
        self.res_sampler = ResourceSampler()
        self.sync = DaemonSync(self.store, self.cfg)
        self.komuta = KomutaPusher(self.store, self.cfg)
        self.tray = WinTrayController(self) if IS_WIN else None
        self.stop_requested = False

        # OS kullanıcı adı (bir kez; timeline olayları için)
        try:
            self._os_user = getpass.getuser()
        except Exception:
            self._os_user = "bilinmiyor"

        # Film Şeridi: TimelineCollector ve saatlik prune zamanlayıcısı
        self._collector = TimelineCollector()
        self._next_prune_hour = time.time() + 3600.0

        # Bellek içi tampon (her 30 sn'de SQLite'a tek işlemde yazılır)
        self._buf_act = {}  # (day, kind, name) -> [cat, secs, visits, title, ts]
        self._last_app = None
        self._last_domain = None
        self._last_game_name = ""
        self._last_res_samples = None
        self._last_poll_ts = time.time()
        self._next_poll = 0.0
        self._next_res = time.time() + 8.0
        self._next_browser = time.time() + 4.0
        self._next_audit = time.time() + 6.0
        self._next_flush = time.time() + FLUSH_SEC
        self._next_hb = time.time() + 12.0
        self._next_ingest = time.time() + 25.0
        self._next_tip = time.time() + 5.0

        # Çocuk HUD & Dashboard süreç yönetimi
        self._hud_proc = None
        self._hud_auto_launched = False
        self._dash_proc = None
        self._last_game_seen_ts = 0.0
        self._current_label = "Hazır"

    def request_stop(self):
        self.stop_requested = True

    def self_ram_mb(self):
        if not psutil:
            return 0.0
        try:
            return psutil.Process(os.getpid()).memory_info().rss / (1024.0 * 1024.0)
        except Exception:
            return 0.0

    def _python_exe(self):
        py = sys.executable
        if IS_WIN and py.lower().endswith("python.exe"):
            pyw = py[:-10] + "pythonw.exe"
            if os.path.isfile(pyw):
                return pyw
        return py

    def is_hud_running(self):
        return self._hud_proc is not None and self._hud_proc.poll() is None

    def ensure_hud_running(self, auto=True):
        if self.is_hud_running():
            return
        try:
            if os.path.exists(HUD_STOP_FLAG):
                os.remove(HUD_STOP_FLAG)
        except OSError:
            pass
        args = [self._python_exe(), self.app_script, "--hud"]
        if auto:
            args.append("--auto-hud")
        try:
            self._hud_proc = subprocess.Popen(args, close_fds=True, **_hidden())
            self._hud_auto_launched = auto
        except Exception:
            self._hud_proc = None

    def stop_hud(self):
        if not self.is_hud_running():
            self._hud_proc = None
            return
        try:
            # Temiz kapanış bayrağı (oturumu kaydedip PresentMon'u kapatsın)
            with open(HUD_STOP_FLAG, "w") as f:
                f.write("1")
        except OSError:
            pass
        proc = self._hud_proc
        self._hud_proc = None
        self._hud_auto_launched = False

        def _reap(p):
            try:
                p.wait(timeout=6.0)
            except Exception:
                try:
                    p.terminate()
                except Exception:
                    pass
        threading.Thread(target=_reap, args=(proc,), daemon=True).start()

    def toggle_hud_manual(self):
        if self.is_hud_running():
            self.stop_hud()
        else:
            self.ensure_hud_running(auto=False)

    def launch_dashboard(self, extra_args=None):
        self.flush_now()
        if self._dash_proc is not None and self._dash_proc.poll() is None:
            # Zaten açıksa yeniden odaklamak için ikinci tetikleme göndermeyelim
            return
        args = [self._python_exe(), self.app_script, "--dashboard"] + (extra_args or [])
        try:
            self._dash_proc = subprocess.Popen(args, close_fds=True, **_hidden())
        except Exception:
            self._dash_proc = None

    def _add_buf(self, day, kind, name, cat, secs, visit_inc, title, ts):
        key = (day, kind, name)
        slot = self._buf_act.get(key)
        if slot is None:
            self._buf_act[key] = [cat, secs, visit_inc, title, ts]
        else:
            if cat != "Diğer":
                slot[0] = cat
            slot[1] += secs
            slot[2] += visit_inc
            if title:
                slot[3] = title
            slot[4] = ts

    def flush_now(self):
        if not self._buf_act:
            return
        items = [
            (day, kind, name, slot[0], slot[1], slot[2], slot[3], slot[4])
            for (day, kind, name), slot in self._buf_act.items()
        ]
        self._buf_act.clear()
        try:
            self.store.flush_activity(items)
        except Exception:
            pass

    def _poll_active(self, now):
        dt = max(0.5, min(10.0, now - self._last_poll_ts))
        self._last_poll_ts = now
        day = today_str(now)

        idle = get_idle_seconds()
        # Film Şeridi: her yoklamada kenarı izle; yalnız uzun beklemeden dönüşte yaz.
        try:
            idle_ev = self._collector.poll_idle(now, self._os_user, idle)
            if idle_ev:
                self.store.add_timeline_event(
                    now, self._os_user, idle_ev["kind"], idle_ev["name"], idle_ev["title"]
                )
        except Exception:
            pass
        if idle >= IDLE_THRESHOLD_SEC:
            self._add_buf(day, "idle", "Boşta", "Boşta", dt, 0, "", now)
            self._last_app = None
            self._last_domain = None
            self._current_label = "Boşta"
            return

        pid, exe_name, exe_path, title = get_foreground_info()
        if not exe_name:
            return
        app_name, cat, is_game, is_browser = classify_process(exe_name, exe_path, self.auditor.known_games)
        visit_inc = 1 if app_name != self._last_app else 0
        self._last_app = app_name
        self._add_buf(day, "app", app_name, cat, dt, visit_inc, (title or "")[:140], now)
        self._current_label = app_name

        dom = None
        url = ""
        if is_browser and title:
            _tu = getattr(self.browser, 'resolve_title_url', None)
            if _tu:
                _res = _tu(title)
                if _res:
                    dom, url = _res
            if not dom:
                dom = self.browser.resolve_title(title)
            if dom:
                d_vis = 1 if dom != self._last_domain else 0
                self._last_domain = dom
                # Repo aktivite özetine yalnız URL'siz, saf sekme başlığı yazılır.
                web_title = strip_browser_suffix(title)[:140]
                self._add_buf(day, "web", dom, "Web", dt, d_vis, web_title, now)
                self._current_label = f"{app_name} · {dom}"
        else:
            self._last_domain = None

        # Film Şeridi: yoklama başına tek birleşik besleme; domain varsa web önceliklidir.
        try:
            if dom:
                # URL yalnız LAN film şeridi feed'inde görünür; repo buffer'ına taşınmaz.
                combined = f"{web_title} · {url}"[:140] if url else web_title
                tl_args = ("web", dom, combined, dom)
            else:
                tl_args = ("oyun" if is_game else "app", app_name, (title or "")[:140], "")
            tl_ev = self._collector.feed(now, self._os_user, *tl_args[:3], domain=tl_args[3])
            if tl_ev:
                self.store.add_timeline_event(
                    tl_ev["ts"], tl_ev["os_user"], tl_ev["kind"], tl_ev["name"], tl_ev["title"]
                )
        except Exception:
            pass

        # İzleme listesi: film_seridi_izlenen desenleri film şeridi olayında aranır
        try:
            _wl = (self.cfg.get("film_seridi_izlenen") or "").strip()
            if _wl and tl_ev:
                _wl_name = (tl_ev.get("name") or "") + " " + (tl_ev.get("title") or "")
                _wl_low = _wl_name.lower()
                for _pat in _wl.split(","):
                    _pat = _pat.strip()
                    if _pat and _pat.lower() in _wl_low:
                        _ev_key = hashlib.sha1(
                            f"izlenen|{self.cfg.get('kimlik', '')}|{_pat}|{int(now) // 180}".encode("utf-8")
                        ).hexdigest()[:16]
                        self.store.add_event(
                            "izlenen",
                            f"[{_pat}] {tl_ev.get('name', '')}",
                            (web_title if dom else (tl_ev.get("title") or ""))[:300],
                            severity="important",
                            ts=now,
                            event_key=_ev_key,
                        )
                        break
        except Exception:
            pass

        # Oyun algılama → otomatik HUD (Windows süreç/klasör + Linux MangoHud canlı log)
        if is_game or (not IS_WIN and self._linux_mangohud_active()):
            self._last_game_seen_ts = now
            if is_game:
                self._last_game_name = app_name
            if self.cfg.get("hud_oto", "acik") != "kapali" and not self.is_hud_running():
                self.ensure_hud_running(auto=True)
        elif now - self._last_game_seen_ts > HUD_IDLE_GRACE_SEC:
            self._last_game_name = ""

    def _check_auto_hud(self, now):
        if self.cfg.get("hud_hep_acik", "kapali") == "acik":
            if not self.is_hud_running():
                self.ensure_hud_running(auto=False)
            return
        if self.is_hud_running() and self._hud_auto_launched:
            # Oyun arka planda hâlâ açık mı kontrol et (alt+tab yapan oyuncunun HUD'unu hemen kapatma)
            if now - self._last_game_seen_ts > HUD_IDLE_GRACE_SEC:
                if not self._any_game_process_running():
                    self.stop_hud()
                else:
                    self._last_game_seen_ts = now - HUD_IDLE_GRACE_SEC + 15.0

    def _linux_mangohud_active(self):
        if IS_WIN or not os.path.isdir(MANGOHUD_DIR):
            return False
        now = time.time()
        try:
            for p in glob.glob(os.path.join(MANGOHUD_DIR, "*.csv")):
                if now - os.path.getmtime(p) <= 6.0:
                    return True
        except OSError:
            pass
        return False

    def _any_game_process_running(self):
        if not IS_WIN and self._linux_mangohud_active():
            return True
        if not psutil:
            return False
        for proc in psutil.process_iter(["name"]):
            try:
                nm = proc.info["name"] or ""
                stem = os.path.splitext(nm)[0].lower()
                if stem in NON_GAME_EXES or stem in BROWSER_EXES or stem in KNOWN_APPS:
                    continue
                if stem in GAME_TITLE_OVERRIDES or stem in self.auditor.known_games:
                    return True
                try:
                    ep = (proc.exe() or "").lower()
                    if any(m in ep for m in GAME_PATH_MARKERS):
                        return True
                except (psutil.AccessDenied, psutil.NoSuchProcess):
                    pass
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return False

    def _check_pending_notify(self):
        """HUD (--hud) sürecinden gelen oyun sonu / sıcaklık bildirimlerini Win32 tepsisinden gösterir."""
        if not os.path.isfile(NOTIFY_FILE):
            return
        try:
            with open(NOTIFY_FILE, encoding="utf-8") as f:
                data = json.load(f)
            os.remove(NOTIFY_FILE)
            title = (data.get("title") or "Oyun İzleyici").strip()
            body = (data.get("body") or "").strip()
            if self.tray and body:
                self.tray.balloon(title, body)
            # Oyun bittiğinde güncel oturumu ve veriyi beklemeden senkronla
            self.sync.maybe_sync(force=True)
            # Film Şeridi: oyun bitişi anında da timeline olaylarını gönder
            try:
                wm = self.store.get_timeline_watermark()
                tl_events = self.store.fetch_timeline_since(wm, limit=500)
                self.komuta.send_ingest(timeline_events=tl_events)
            except Exception:
                self.komuta.send_ingest()
        except Exception:
            try:
                os.remove(NOTIFY_FILE)
            except OSError:
                pass

    def step(self):
        now = time.time()
        if now >= self._next_poll:
            self._next_poll = now + POLL_ACTIVE_SEC
            self._poll_active(now)
            self._check_auto_hud(now)
            self._check_pending_notify()

        if now >= self._next_browser:
            self._next_browser = now + POLL_BROWSER_SEC
            try:
                self.browser.refresh(self.store, days_back=7)
            except Exception:
                pass

        if now >= self._next_res:
            self._next_res = now + POLL_RESOURCE_SEC
            try:
                samples = self.res_sampler.sample(self.auditor.known_games)
                if samples:
                    self._last_res_samples = samples
                    self.store.flush_resources(today_str(now), samples)
            except Exception:
                pass

        if now >= self._next_audit:
            self._next_audit = now + POLL_AUDIT_SEC
            try:
                self.auditor.run_audit()
            except Exception:
                pass

        if now >= self._next_flush:
            self._next_flush = now + FLUSH_SEC
            self.flush_now()
            self.sync.apply_pending_imports()
            self.sync.maybe_sync(force=False)
            trim_memory()
            # Film Şeridi: saatte bir eski timeline olaylarını budа
            if now >= self._next_prune_hour:
                self._next_prune_hour = now + 3600.0
                try:
                    self.store.prune_timeline(keep_days=30)
                except Exception:
                    pass

        if now >= self._next_hb:
            self._next_hb = now + POLL_HEARTBEAT_SEC
            try:
                self.komuta.send_heartbeat(
                    active_app=self._current_label,
                    active_game=self._last_game_name,
                    agent_ram_mb=self.self_ram_mb(),
                    last_samples=self._last_res_samples,
                )
            except Exception:
                pass

        if now >= self._next_ingest:
            self._next_ingest = now + POLL_INGEST_SEC
            try:
                self.flush_now()
                # Film Şeridi: watermark'tan bu yana olayları gönder
                wm = self.store.get_timeline_watermark()
                tl_events = self.store.fetch_timeline_since(wm, limit=500)
                self.komuta.send_ingest(timeline_events=tl_events)
            except Exception:
                pass

        if self.tray and now >= self._next_tip:
            self._next_tip = now + 8.0
            self.tray.update_tip("Sistem hizmeti")

    def run(self):
        # İlk açılışta HUD hep açık seçiliyse başlat (tamamen sessiz, sıfır pop-up!)
        if self.cfg.get("hud_hep_acik", "kapali") == "acik":
            self.ensure_hud_running(auto=False)
        elif self.cfg.get("hud_oto", "acik") != "kapali" and self._any_game_process_running():
            self._last_game_seen_ts = time.time()
            self.ensure_hud_running(auto=True)

        if self.tray:
            self.tray.start()
            self.tray.pump_loop()
        else:
            try:
                while not self.stop_requested:
                    self.step()
                    time.sleep(0.5)
            except KeyboardInterrupt:
                pass
        self.shutdown()

    def shutdown(self):
        self.flush_now()
        self.stop_hud()
        self.res_sampler.close()
        self.store.close()


def acquire_daemon_lock(wait=0.0):
    if IS_WIN:
        lock_path = os.path.join(DATA_DIR, "pc-izleyici-daemon.lock")
        os.makedirs(DATA_DIR, exist_ok=True)
    else:
        lock_path = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "pc-izleyici-daemon.lock")
    deadline = time.time() + wait
    while True:
        fd = open(lock_path, "w")
        try:
            if IS_WIN:
                msvcrt.locking(fd.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except OSError:
            fd.close()
            if time.time() >= deadline:
                return None
            time.sleep(0.3)


def run_daemon(app_script=None):
    wait = 12.0 if os.environ.get("OI_RESTART") else 0.0
    lock = acquire_daemon_lock(wait)
    if lock is None:
        # Servis zaten çalışıyorsa kullanıcı ikona tekrar tıklamıştır → doğrudan Rapor Panelini aç
        if app_script and os.path.isfile(app_script):
            py = sys.executable
            if IS_WIN and py.lower().endswith("python.exe"):
                pyw = py[:-10] + "pythonw.exe"
                if os.path.isfile(pyw):
                    py = pyw
            subprocess.Popen([py, app_script, "--dashboard"], close_fds=True, **_hidden())
        return
    try:
        daemon = PCActivityDaemon(app_script=app_script)
        daemon.run()
    finally:
        try:
            lock.close()
        except Exception:
            pass


if __name__ == "__main__":
    if "--ozet" in sys.argv:
        cfg = load_machine_config()
        st = ActivityStore(cfg["kimlik"])
        days = 7
        for a in sys.argv:
            if a.startswith("--gun="):
                days = int(a.split("=", 1)[1])
        print(SummaryEngine.to_markdown(st, days=days))
        st.close()
    else:
        run_daemon(INSTALLED_BIN)
