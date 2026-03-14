import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
from datetime import datetime, date, timedelta
import tkinter as tk
import tkinter.filedialog as filedialog
import tkinter.messagebox as messagebox
import customtkinter as ctk
from PIL import Image
from PIL import ImageTk

try:
    import requests
except ImportError:
    requests = None

# Türkiye TAC (ilk 3 hane) fallback listesi
IMEI_TR_TAC_PREFIXES = ("352", "354", "355", "356", "357", "358", "860", "861", "862", "863", "864", "865", "866", "867", "868", "869")

# PyInstaller: exe yanındaki klasör = çalışma dizini; paket verileri _MEIPASS'ta
if getattr(sys, "frozen", False):
    SCRIPT_DIR = os.path.dirname(sys.executable)
    _RESOURCE_DIR = getattr(sys, "_MEIPASS", SCRIPT_DIR)
else:
    SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
    _RESOURCE_DIR = SCRIPT_DIR

DB_NAME = os.path.join(SCRIPT_DIR, "servis.db")
CONFIG_FILE = os.path.join(SCRIPT_DIR, "tsp_config.json")
LOGO_PATH = os.path.join(_RESOURCE_DIR, "logo.png")
YEDEKLER_DIR = os.path.join(SCRIPT_DIR, "yedekler")


def _ensure_bundled_config():
    """Exe ilk çalıştırmada paketteki tsp_config.json'ı exe yanına kopyala."""
    if getattr(sys, "frozen", False) and not os.path.exists(CONFIG_FILE):
        bundled = os.path.join(_RESOURCE_DIR, "tsp_config.json")
        if os.path.exists(bundled):
            try:
                shutil.copy2(bundled, CONFIG_FILE)
            except Exception:
                pass
YEDEK_ONCEKI = "cagın_iletisim_yedek_"


def yedek_klasoru_hazirla():
    try:
        os.makedirs(YEDEKLER_DIR, exist_ok=True)
    except Exception:
        pass


def _yedek_dosya_yolu():
    tarih = date.today().strftime("%Y-%m-%d")
    return f"{YEDEK_ONCEKI}{tarih}.db"


def otomatik_yedek_al():
    """Uygulama açılışında sessizce günlük yedek al. yedekler + drive (varsa)."""
    try:
        yedek_klasoru_hazirla()
        if not os.path.exists(DB_NAME):
            return
        ad = _yedek_dosya_yolu()
        hedef_yerel = os.path.join(YEDEKLER_DIR, ad)
        shutil.copy2(DB_NAME, hedef_yerel)
        drive = load_drive_folder()
        if drive and os.path.isdir(drive):
            hedef_drive = os.path.join(drive, ad)
            try:
                shutil.copy2(DB_NAME, hedef_drive)
            except Exception:
                pass
    except Exception:
        pass


def manuel_yedek_al():
    """Manuel yedek al; yedekler + drive (varsa). Oluşturulan dosya adını döndür."""
    try:
        yedek_klasoru_hazirla()
        if not os.path.exists(DB_NAME):
            return None
        ad = _yedek_dosya_yolu()
        hedef_yerel = os.path.join(YEDEKLER_DIR, ad)
        shutil.copy2(DB_NAME, hedef_yerel)
        drive = load_drive_folder()
        if drive and os.path.isdir(drive):
            try:
                shutil.copy2(DB_NAME, os.path.join(drive, ad))
            except Exception:
                pass
        return ad
    except Exception:
        return None


def yedek_listesi():
    """Yedekler klasöründeki .db dosyalarını en yeni en üstte döndür (dosya adı tarihine göre)."""
    try:
        if not os.path.isdir(YEDEKLER_DIR):
            return []
        dosyalar = [
            (f, os.path.getmtime(os.path.join(YEDEKLER_DIR, f)))
            for f in os.listdir(YEDEKLER_DIR)
            if f.endswith(".db") and os.path.isfile(os.path.join(YEDEKLER_DIR, f))
        ]
        dosyalar.sort(key=lambda x: x[1], reverse=True)
        return [x[0] for x in dosyalar]
    except Exception:
        return []

TR_AYLAR = ("Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran",
            "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık")


def _tarih_baslik(tarih_str):
    """created_at string'inden grup başlığı: Bugün — 8 Mart 2026, Dün — ..., veya 5 Mart 2026."""
    try:
        parts = tarih_str.strip().split()
        ymd = parts[0] if parts else ""
        if not ymd or len(ymd) < 10:
            return ymd or tarih_str
        y, m, d = int(ymd[:4]), int(ymd[5:7]), int(ymd[8:10])
        today = date.today()
        gun = date(y, m, d)
        ay_ad = TR_AYLAR[m - 1] if 1 <= m <= 12 else str(m)
        tarih_metin = f"{d} {ay_ad} {y}"
        if gun == today:
            return f"Bugün — {tarih_metin}"
        if gun == today - timedelta(days=1):
            return f"Dün — {tarih_metin}"
        return tarih_metin
    except Exception:
        return tarih_str


def _ariza_kisa(ariza, max_satir=2, max_uzunluk=120):
    """Arıza metnini en fazla max_satir satır yap, taşarsa '...' ekle. Boşsa None."""
    if not ariza or not ariza.strip():
        return None
    satirlar = ariza.strip().splitlines()
    al = []
    for s in satirlar[:max_satir]:
        s = s.strip()
        if len(s) > max_uzunluk:
            s = s[: max_uzunluk - 3].rstrip() + "..."
        al.append(s)
    return "\n".join(al) if al else None


def _imei_turkey_check(imei_str):
    """15 haneli IMEI için Türkiye cihazı mı kontrol et. API dener, olmazsa TAC fallback. True=TR, False=Yurt dışı."""
    if not imei_str or len(imei_str) != 15 or not imei_str.isdigit():
        return None
    tac8 = imei_str[:8]
    tac3 = imei_str[:3]
    if requests:
        try:
            r = requests.get(f"https://www.imeicheck.com/api/check?imei={imei_str}&token=free", timeout=5)
            if r.ok:
                data = r.json()
                if isinstance(data, dict):
                    country = (data.get("country") or data.get("Country") or "").upper()
                    if "TURKEY" in country or "TÜRKİYE" in country.upper() or "TURK" in country:
                        return True
                    return False
        except Exception:
            pass
    return tac3 in IMEI_TR_TAC_PREFIXES

THEMES = {
    "dark": {
        "bg": "#0f0f0f",
        "frame_bg": "#1a1a1a",
        "card_bg": "#242424",
        "border_color": "#333333",
        "text_dim": "#999999",
        "accent": "#1a73e8",
        "accent_dark": "#0d47a1",
        "list_bg": "#1a1a1a",
        "list_card_bg": "#242424",
        "list_card_hover": "#2e2e2e",
        "list_card_border": "#333333",
        "list_title_color": "#ffffff",
        "list_sub_color": "#999999",
        "input_bg": "#2a2a2a",
        "input_text": "#e0e0e0",
        "placeholder": "#666666",
        "separator": "#2e2e2e",
        "kar_card_bg": "#242424",
        "kar_card_border": "#333333",
        "kar_title": "#ffffff",
        "kar_maliyet": "#ff6b6b",
        "kar_satis": "#64b5f6",
        "kar_kar": "#00c853",
        "kar_header_bg": "transparent",
        "kar_total_bg": "#1e1e1e",
        "kar_total_text": "#ffffff",
    },
    "light": {
        "bg": "#f0f2f5",
        "frame_bg": "#ffffff",
        "card_bg": "#e5e7eb",
        "border_color": "#d1d5db",
        "text_dim": "#6b7280",
        "accent": "#1a73e8",
        "accent_dark": "#0d47a1",
        "list_bg": "#e8e8e8",
        "list_card_bg": "#f5f5f5",
        "list_card_hover": "#eeeeee",
        "list_card_border": "#d0d0d0",
        "list_title_color": "#1f1f1f",
        "list_sub_color": "#555555",
        "kar_card_bg": "#ffffff",
        "kar_card_border": "#e0e0e0",
        "kar_title": "#1a1a1a",
        "kar_maliyet": "#e53935",
        "kar_satis": "#1565c0",
        "kar_kar": "#2e7d32",
        "kar_header_bg": "#f5f5f5",
        "kar_total_bg": "#e8f5e9",
        "kar_total_text": "#1b5e20",
    },
}


def load_theme_preference():
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data.get("theme", "dark")
    except Exception:
        pass
    return "dark"


def save_theme_preference(theme: str):
    try:
        data = {}
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        data["theme"] = theme
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def load_drive_folder():
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data.get("drive_folder", "").strip() or None
    except Exception:
        pass
    return None


def save_drive_folder(path: str):
    try:
        data = {}
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        data["drive_folder"] = path.strip() if path else ""
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _son_guncelleme_metin(tarih_str):
    """2026-03-14 15:32 -> 14 Mart 2026, 15:32"""
    if not tarih_str or not tarih_str.strip():
        return None
    try:
        parts = tarih_str.strip().split()
        ymd = parts[0] if parts else ""
        if len(ymd) < 10:
            return tarih_str
        y, m, d = int(ymd[:4]), int(ymd[5:7]), int(ymd[8:10])
        ay_ad = TR_AYLAR[m - 1] if 1 <= m <= 12 else str(m)
        saat = parts[1] if len(parts) > 1 else ""
        return f"{d} {ay_ad} {y}, {saat}" if saat else f"{d} {ay_ad} {y}"
    except Exception:
        return tarih_str


def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS cihazlar (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            musteri_adi TEXT NOT NULL,
            telefon_modeli TEXT NOT NULL,
            imei TEXT NOT NULL,
            ariza TEXT NOT NULL,
            tahmini_maliyet REAL NOT NULL,
            musteriye_soylenen_fiyat REAL,
            durum TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    try:
        cursor.execute(
            "ALTER TABLE cihazlar ADD COLUMN musteriye_soylenen_fiyat REAL"
        )
    except sqlite3.OperationalError:
        pass
    try:
        cursor.execute(
            "ALTER TABLE cihazlar ADD COLUMN son_guncelleme TEXT"
        )
    except sqlite3.OperationalError:
        pass
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS degisiklik_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cihaz_id INTEGER NOT NULL,
            tarih TEXT NOT NULL,
            mesaj TEXT,
            FOREIGN KEY (cihaz_id) REFERENCES cihazlar(id)
        )
        """
    )
    conn.commit()
    conn.close()


class LogoImageHolder:
    """PIL ile yüklenen logo; apply_theme ile arka plan güncellenebilir."""

    def __init__(self, parent, size, bg="#1a1a2e"):
        self._bg = bg
        self._size = size
        self.frame = tk.Frame(parent, bg=bg, width=size, height=size)
        self.frame.pack_propagate(False)
        self._lbl = None
        self._photo = None
        self._load()

    def _load(self):
        try:
            resample = getattr(Image, "Resampling", Image).LANCZOS
            img = Image.open(LOGO_PATH).convert("RGB").resize((self._size, self._size), resample)
            self._photo = ImageTk.PhotoImage(img)
            self._lbl = tk.Label(self.frame, image=self._photo, bg=self._bg)
            self._lbl.image = self._photo
            self._lbl.place(relx=0.5, rely=0.5, anchor="center")
        except Exception:
            pass

    def update_bg(self, bg):
        self._bg = bg
        self.frame.configure(bg=bg)
        if self._lbl:
            self._lbl.configure(bg=bg)


class ServisApp(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("Çağın İletişim TSP")
        self.geometry("1100x640")
        self.minsize(1000, 580)

        self.theme = load_theme_preference()
        ctk.set_appearance_mode("dark" if self.theme == "dark" else "light")
        ctk.set_default_color_theme("blue")

        self.grid_columnconfigure(0, weight=1)
        self.grid_columnconfigure(1, weight=2)
        self.grid_rowconfigure(1, weight=1)

        self.selected_id = None
        self._theme_btn = None
        self._header_logo = None

        self._build_header()
        self._build_form()
        self._build_tabs()
        self._build_footer()
        self.apply_theme()
        self.refresh_list()
        self.refresh_kar_analizi()

    def _build_form(self):
        self.form_frame = ctk.CTkFrame(
            self, corner_radius=16, border_width=1
        )
        self.form_frame.grid(row=1, column=0, padx=20, pady=(0, 8), sticky="nsew")

        title_label = ctk.CTkLabel(
            self.form_frame,
            text="Yeni Teknik Servis Kaydı",
            font=ctk.CTkFont(size=18, weight="bold"),
        )
        title_label.grid(row=0, column=0, pady=(16, 24), padx=14, sticky="w")
        self.son_guncelleme_label = ctk.CTkLabel(
            self.form_frame,
            text="",
            font=ctk.CTkFont(size=11),
            text_color=("#6b7280", "#9ca3af"),
        )
        self.son_guncelleme_label.grid(row=0, column=1, pady=(16, 24), padx=14, sticky="e")

        self.form_frame.grid_columnconfigure(0, minsize=160)
        self.form_frame.grid_columnconfigure(1, weight=1)

        row_pad = 14
        musteri_label = ctk.CTkLabel(self.form_frame, text="Müşteri Adı")
        musteri_label.grid(row=1, column=0, padx=14, pady=(0, row_pad), sticky="w")
        self.musteri_entry = ctk.CTkEntry(self.form_frame, placeholder_text="Ad Soyad", height=36)
        self.musteri_entry.grid(row=1, column=1, padx=14, pady=(0, row_pad), sticky="ew")

        model_label = ctk.CTkLabel(self.form_frame, text="Telefon Marka / Model")
        model_label.grid(row=2, column=0, padx=14, pady=(0, row_pad), sticky="w")
        self.model_entry = ctk.CTkEntry(
            self.form_frame, placeholder_text="Örn: Samsung S21, Redmi Note 12", height=36
        )
        self.model_entry.grid(row=2, column=1, padx=14, pady=(0, row_pad), sticky="ew")

        imei_label = ctk.CTkLabel(self.form_frame, text="IMEI")
        imei_label.grid(row=3, column=0, padx=14, pady=(0, row_pad), sticky="w")
        imei_frame = ctk.CTkFrame(self.form_frame, fg_color="transparent")
        imei_frame.grid(row=3, column=1, padx=14, pady=(0, row_pad), sticky="ew")
        imei_frame.grid_columnconfigure(0, weight=1)
        self.imei_entry = ctk.CTkEntry(imei_frame, placeholder_text="15 haneli IMEI", height=36)
        self.imei_entry.grid(row=0, column=0, sticky="ew")
        self.imei_status_label = ctk.CTkLabel(imei_frame, text="", font=ctk.CTkFont(size=11), height=0)
        self.imei_status_label.grid(row=1, column=0, sticky="w", pady=(4, 0))
        self.imei_entry.bind("<KeyRelease>", self._on_imei_changed)

        ariza_label = ctk.CTkLabel(self.form_frame, text="Arıza / Problem Açıklaması")
        ariza_label.grid(row=4, column=0, padx=14, pady=(0, row_pad), sticky="nw")
        self.ariza_text = ctk.CTkTextbox(self.form_frame, height=100, corner_radius=8)
        self.ariza_text.grid(row=4, column=1, padx=14, pady=(0, row_pad), sticky="nsew")

        maliyet_label = ctk.CTkLabel(self.form_frame, text="Tahmini Maliyet (₺)")
        maliyet_label.grid(row=5, column=0, padx=14, pady=(0, row_pad), sticky="w")
        self.maliyet_entry = ctk.CTkEntry(self.form_frame, placeholder_text="Örn: 750", height=36)
        self.maliyet_entry.grid(row=5, column=1, padx=14, pady=(0, row_pad), sticky="ew")

        satis_label = ctk.CTkLabel(self.form_frame, text="Müşteriye Söylenen Fiyat (₺)")
        satis_label.grid(row=6, column=0, padx=14, pady=(0, row_pad), sticky="w")
        self.satis_entry = ctk.CTkEntry(self.form_frame, placeholder_text="Örn: 900", height=36)
        self.satis_entry.grid(row=6, column=1, padx=14, pady=(0, row_pad), sticky="ew")

        durum_label = ctk.CTkLabel(self.form_frame, text="Cihaz Durumu")
        durum_label.grid(row=7, column=0, padx=14, pady=(0, row_pad), sticky="w")

        self.durum_var = ctk.StringVar(value="Bekliyor")
        self.durum_menu = ctk.CTkOptionMenu(
            self.form_frame,
            values=["Bekliyor", "Tamirde", "Teslim Edildi"],
            variable=self.durum_var,
            height=36,
        )
        self.durum_menu.grid(row=7, column=1, padx=14, pady=(0, row_pad), sticky="ew")

        button_frame = ctk.CTkFrame(self.form_frame, fg_color="transparent")
        button_frame.grid(row=8, column=0, columnspan=2, pady=(24, 14), padx=14, sticky="e")

        self.clear_button = ctk.CTkButton(
            button_frame,
            text="Yeni Kayıt",
            command=self.clear_form,
            fg_color="#3d3d3d",
            hover_color="#4a4a4a",
            height=36,
            corner_radius=8,
        )
        self.clear_button.pack(side="left", padx=6)

        self.delete_button = ctk.CTkButton(
            button_frame,
            text="Sil",
            command=self.delete_record,
            fg_color="#b3261e",
            hover_color="#7f1d1a",
            height=36,
            corner_radius=8,
        )
        self.delete_button.pack(side="left", padx=6)

        self.save_button = ctk.CTkButton(
            button_frame,
            text="Kaydet",
            command=self.save_record,
            height=36,
            corner_radius=8,
        )
        self.save_button.pack(side="left", padx=6)

        self.form_frame.grid_rowconfigure(4, weight=1)

        # Tab sırası ve focus highlight
        self._inputs_for_tab = [
            self.musteri_entry,
            self.model_entry,
            self.imei_entry,
            self.ariza_text,
            self.maliyet_entry,
            self.satis_entry,
            self.durum_menu,
            self.save_button,
        ]
        for w in self._inputs_for_tab:
            w.bind("<Tab>", self._on_tab_next)

        self._focus_highlight_widgets = [
            self.musteri_entry,
            self.model_entry,
            self.imei_entry,
            self.ariza_text,
            self.maliyet_entry,
            self.satis_entry,
            self.durum_menu,
        ]
        for w in self._focus_highlight_widgets:
            self._bind_focus_highlight(w)

    def _build_tabs(self):
        self.tabview = ctk.CTkTabview(self, corner_radius=16)
        self.tabview.grid(row=1, column=1, padx=(0, 20), pady=(0, 8), sticky="nsew")
        self.tabview.add("Kayıtlar")
        self.tabview.add("Kar Analizi")
        self.list_frame = self.tabview.tab("Kayıtlar")
        header_frame = ctk.CTkFrame(self.list_frame, fg_color="transparent")
        header_frame.pack(fill="x", padx=14, pady=(14, 4))
        ctk.CTkLabel(
            header_frame,
            text="Servis Kayıtları",
            font=ctk.CTkFont(size=18, weight="bold"),
        ).pack(side="left")
        self.list_info_label = ctk.CTkLabel(header_frame, text="", font=ctk.CTkFont(size=12))
        self.list_info_label.pack(side="right")

        # Arama kutusu (KeyRelease ile anlık; müşteri, model, arıza alanlarında büyük/küçük harf duyarsız)
        self.search_var = tk.StringVar()
        self.search_entry = ctk.CTkEntry(
            self.list_frame,
            placeholder_text="Müşteri adı, model veya problem ara...",
            height=32,
            textvariable=self.search_var,
        )
        self.search_entry.pack(fill="x", padx=14, pady=(0, 6))
        self.search_entry.bind("<KeyRelease>", lambda e: self.refresh_list())

        self.durum_filter = "Tümü"
        self.filter_frame = ctk.CTkFrame(self.list_frame, fg_color="transparent")
        self.filter_frame.pack(fill="x", padx=14, pady=(0, 8))
        self._filter_buttons = {}
        for key, label in [("Tümü", "Tümü"), ("Bekleyenler", "Bekleyenler"), ("Tamirde", "Tamirde"), ("Teslim Edildi", "Teslim Edildi")]:
            btn = ctk.CTkButton(
                self.filter_frame,
                text=label,
                width=100,
                height=28,
                corner_radius=6,
                fg_color="transparent",
                command=lambda k=key: self._set_durum_filter(k),
            )
            btn.pack(side="left", padx=(0, 6))
            self._filter_buttons[key] = btn

        self.scroll_frame = ctk.CTkScrollableFrame(self.list_frame)
        self.scroll_frame.pack(fill="both", expand=True, padx=14, pady=(0, 14))

        self.kar_frame = self.tabview.tab("Kar Analizi")
        kar_header = ctk.CTkFrame(self.kar_frame, fg_color="transparent")
        kar_header.pack(fill="x", padx=14, pady=(14, 8))
        ctk.CTkLabel(
            kar_header,
            text="Teslim Edilen İşler — Kar Özeti",
            font=ctk.CTkFont(size=18, weight="bold"),
        ).pack(side="left")
        self.kar_scroll = ctk.CTkScrollableFrame(self.kar_frame, fg_color="transparent")
        self.kar_scroll.pack(fill="both", expand=True, padx=14, pady=(0, 8))
        self.kar_summary_frame = ctk.CTkFrame(self.kar_frame, fg_color="transparent")
        self.kar_summary_frame.pack(fill="x", padx=14, pady=(8, 14))
        self.kar_summary_label = ctk.CTkLabel(
            self.kar_summary_frame,
            text="Toplam Kar: 0,00 ₺",
            font=ctk.CTkFont(size=16, weight="bold"),
        )
        self.kar_summary_label.pack(anchor="e")

    def _toggle_theme(self):
        self.theme = "light" if self.theme == "dark" else "dark"
        save_theme_preference(self.theme)
        ctk.set_appearance_mode("dark" if self.theme == "dark" else "light")
        self._theme_btn.configure(text="☀" if self.theme == "dark" else "🌙")
        self.apply_theme()

    def apply_theme(self):
        t = THEMES[self.theme]
        self.configure(fg_color=t["bg"])
        self.form_frame.configure(fg_color=t["frame_bg"], border_color=t["border_color"])
        try:
            self.tabview.configure(fg_color=t["frame_bg"])
        except Exception:
            pass
        try:
            self.tabview.tab("Kayıtlar").configure(fg_color=t["list_bg"])
            self.scroll_frame.configure(fg_color=t["list_bg"])
        except Exception:
            pass
        for name in ["Kar Analizi"]:
            tab = self.tabview.tab(name)
            try:
                tab.configure(fg_color=t["frame_bg"])
            except Exception:
                pass
        if self._header_logo:
            self._header_logo.update_bg(t["bg"])

        # Form ve arama alanları için renkler
        if self.theme == "dark":
            ibg = t.get("input_bg", "#2a2a2a")
            itext = t.get("input_text", "#e0e0e0")
            ph = t.get("placeholder", "#666666")
            b = t.get("border_color", "#333333")
            for e in [
                self.musteri_entry,
                self.model_entry,
                self.imei_entry,
                self.maliyet_entry,
                self.satis_entry,
            ]:
                e.configure(fg_color=ibg, text_color=itext, placeholder_text_color=ph, border_color=b)
            self.ariza_text.configure(fg_color=ibg, text_color=itext, border_color=b)
            if hasattr(self, "search_entry"):
                self.search_entry.configure(fg_color=ibg, text_color=itext, placeholder_text_color=ph, border_color=b)
        else:
            b = t.get("border_color", "#d1d5db")
            for e in [
                self.musteri_entry,
                self.model_entry,
                self.imei_entry,
                self.maliyet_entry,
                self.satis_entry,
            ]:
                e.configure(border_color=b)
            self.ariza_text.configure(border_color=b)
            if hasattr(self, "search_entry"):
                self.search_entry.configure(border_color=b)

        self.refresh_list()
        self.refresh_kar_analizi()

    def _bind_focus_highlight(self, widget):
        def on_focus_in(_event):
            t = THEMES.get(self.theme, THEMES["dark"])
            widget.configure(border_color=t.get("accent", "#1a73e8"))

        def on_focus_out(_event):
            t = THEMES.get(self.theme, THEMES["dark"])
            widget.configure(border_color=t.get("border_color", "#333333"))

        widget.bind("<FocusIn>", on_focus_in)
        widget.bind("<FocusOut>", on_focus_out)

    def _on_tab_next(self, event):
        if not hasattr(self, "_inputs_for_tab"):
            return
        try:
            idx = self._inputs_for_tab.index(event.widget)
        except ValueError:
            return
        next_idx = (idx + 1) % len(self._inputs_for_tab)
        next_widget = self._inputs_for_tab[next_idx]
        next_widget.focus_set()
        return "break"

    def _build_footer(self):
        self.footer_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.footer_frame.grid(row=2, column=0, columnspan=2, sticky="se", padx=20, pady=(0, 12))
        self.footer_frame.grid_columnconfigure(0, weight=1)
        credit = ctk.CTkLabel(
            self.footer_frame,
            text="© 2025 M. Emir Öz. tarafından geliştirilmiştir.",
            font=ctk.CTkFont(size=11),
            text_color=("#6b7280", "#9ca3af"),
        )
        credit.grid(row=0, column=0, sticky="e")

    def _drive_klasoru_sec(self):
        path = filedialog.askdirectory(title="Google Drive / Yedek klasörü seçin", initialdir=os.path.expanduser("~"))
        if path:
            save_drive_folder(path)
            messagebox.showinfo("Kaydedildi", f"Drive klasörü kaydedildi.\n{path}")

    def _build_header(self):
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, columnspan=2, sticky="ew", padx=20, pady=(16, 8))
        header.grid_columnconfigure(1, weight=1)

        content = ctk.CTkFrame(header, fg_color="transparent")
        content.grid(row=0, column=0, sticky="w")

        self._header_logo = LogoImageHolder(
            content, size=60, bg=THEMES.get(self.theme, THEMES["dark"])["bg"]
        )
        self._header_logo.frame.pack(side="left", padx=(0, 14))

        text_block = ctk.CTkFrame(content, fg_color="transparent")
        text_block.pack(side="left")
        title_main = ctk.CTkLabel(
            text_block,
            text="Çağın İletişim TSP",
            font=ctk.CTkFont(size=22, weight="bold"),
        )
        title_main.pack(anchor="w")
        subtitle = ctk.CTkLabel(
            text_block,
            text="Teknik Servis Programı",
            font=ctk.CTkFont(size=12),
            text_color=("#6b7280", "#9ca3af"),
        )
        subtitle.pack(anchor="w", pady=(2, 0))

        # Yedekleme ve tema
        right_f = ctk.CTkFrame(header, fg_color="transparent")
        right_f.grid(row=0, column=2, padx=(12, 0), sticky="e")
        ctk.CTkButton(
            right_f,
            text="💾 Yedekle",
            width=90,
            height=32,
            corner_radius=8,
            command=self._manuel_yedekle,
        ).pack(side="left", padx=(0, 6))
        ctk.CTkButton(
            right_f,
            text="Yedekten Geri Yükle",
            width=140,
            height=32,
            corner_radius=8,
            fg_color="transparent",
            command=self._yedekten_geri_yukle_dialog,
        ).pack(side="left", padx=(0, 6))
        ctk.CTkButton(
            right_f,
            text="📁 Klasörü Aç",
            width=100,
            height=32,
            corner_radius=8,
            fg_color="transparent",
            command=self._yedek_klasoru_ac,
        ).pack(side="left", padx=(0, 6))
        ctk.CTkButton(
            right_f,
            text="☁ Drive Klasörü Seç",
            width=130,
            height=32,
            corner_radius=8,
            fg_color="transparent",
            command=self._drive_klasoru_sec,
        ).pack(side="left", padx=(0, 6))
        self._theme_btn = ctk.CTkButton(
            right_f,
            text="☀" if self.theme == "dark" else "🌙",
            width=44,
            height=36,
            corner_radius=8,
            command=self._toggle_theme,
        )
        self._theme_btn.pack(side="left")

    def _show_yedek_toast(self, message):
        """Sağ alt köşede 3 saniyelik bildirim."""
        t = THEMES.get(self.theme, THEMES["dark"])
        toast = ctk.CTkToplevel(self)
        toast.overrideredirect(True)
        toast.attributes("-topmost", True)
        f = ctk.CTkFrame(toast, corner_radius=8, border_width=1, fg_color=t.get("frame_bg", "#1a1a1a"), border_color=t.get("border_color"))
        f.pack(padx=8, pady=8)
        ctk.CTkLabel(f, text=message, font=ctk.CTkFont(size=12), text_color=t.get("list_title_color", "#ffffff")).pack(padx=16, pady=12)
        toast.update_idletasks()
        w, h = toast.winfo_reqwidth(), toast.winfo_reqheight()
        x = self.winfo_x() + self.winfo_width() - w - 24
        y = self.winfo_y() + self.winfo_height() - h - 24
        toast.geometry(f"+{x}+{y}")
        toast.after(3000, toast.destroy)

    def _manuel_yedekle(self):
        ad = manuel_yedek_al()
        if ad:
            self._show_yedek_toast(f"✓ Yedek alındı: {ad}")
        else:
            messagebox.showerror("Hata", "Yedek alınamadı.")

    def _yedek_klasoru_ac(self):
        try:
            yedek_klasoru_hazirla()
            if sys.platform == "win32":
                os.startfile(YEDEKLER_DIR)
            else:
                import subprocess
                subprocess.Popen(["xdg-open", YEDEKLER_DIR])
        except Exception as e:
            messagebox.showerror("Hata", f"Klasör açılamadı: {e}")

    def _yedekten_geri_yukle_dialog(self):
        liste = yedek_listesi()
        if not liste:
            messagebox.showinfo("Yedek Yok", "Yedekler klasöründe .db dosyası bulunamadı.")
            return
        t = THEMES.get(self.theme, THEMES["dark"])
        pop = ctk.CTkToplevel(self)
        pop.title("Yedekten Geri Yükle")
        pop.minsize(420, 360)
        pop.transient(self)
        pop.grab_set()
        f = ctk.CTkFrame(pop, fg_color="transparent")
        f.pack(fill="both", expand=True, padx=20, pady=20)
        ctk.CTkLabel(f, text="Yedek dosyası seçin (en yeni en üstte):", font=ctk.CTkFont(weight="bold")).pack(anchor="w", pady=(0, 8))
        listbox_frame = ctk.CTkFrame(f, fg_color="transparent")
        listbox_frame.pack(fill="both", expand=True, pady=(0, 16))
        lb = tk.Listbox(listbox_frame, font=("Segoe UI", 11), height=12, selectmode="single")
        lb.pack(side="left", fill="both", expand=True)
        for dosya in liste:
            lb.insert(tk.END, dosya)
        if liste:
            lb.selection_set(0)
        btn_f = ctk.CTkFrame(f, fg_color="transparent")
        btn_f.pack(fill="x")

        def geri_yukle():
            sel = lb.curselection()
            if not sel:
                messagebox.showwarning("Seçim", "Bir yedek seçin.")
                return
            dosya = liste[sel[0]]
            if not messagebox.askyesno("Onay", "Bu yedekten geri yüklenecek, mevcut veriler silinecek. Emin misiniz?"):
                return
            try:
                kaynak = os.path.join(YEDEKLER_DIR, dosya)
                shutil.copy2(kaynak, DB_NAME)
                pop.destroy()
                messagebox.showinfo("Tamam", "Geri yükleme tamamlandı. Uygulama yeniden başlatılıyor.")
                self.after(100, self._yeniden_baslat)
            except Exception as e:
                messagebox.showerror("Hata", f"Geri yükleme başarısız: {e}")

        def iptal():
            pop.destroy()

        ctk.CTkButton(btn_f, text="İptal", width=90, command=iptal, fg_color=t.get("text_dim", "#666")).pack(side="left", padx=(0, 10))
        ctk.CTkButton(btn_f, text="Geri Yükle", width=100, command=geri_yukle).pack(side="left")
        pop.geometry("420x360")
        pop.update_idletasks()
        wx, hx = pop.winfo_screenwidth(), pop.winfo_screenheight()
        pop.geometry(f"420x360+{(wx-420)//2}+{(hx-360)//2}")

    def _yeniden_baslat(self):
        try:
            subprocess.Popen([sys.executable] + sys.argv, cwd=os.getcwd())
        except Exception:
            pass
        self.destroy()

    def _sil_kayit(self, record_id: int):
        if not messagebox.askyesno("Onay", "Bu kayıt kalıcı olarak silinecek. Emin misiniz?"):
            return
        conn = sqlite3.connect(DB_NAME)
        conn.execute("DELETE FROM cihazlar WHERE id = ?", (record_id,))
        conn.execute("DELETE FROM degisiklik_log WHERE cihaz_id = ?", (record_id,))
        conn.commit()
        conn.close()
        if self.selected_id == record_id:
            self.clear_form()
        messagebox.showinfo("Silindi", "Kayıt silindi.")
        self.refresh_list()
        self.refresh_kar_analizi()

    def _on_imei_changed(self, event=None):
        val = self.imei_entry.get().strip()
        if not val or len(val) != 15 or not val.isdigit():
            self.imei_status_label.configure(text="")
            return

        def do_check():
            result = _imei_turkey_check(val)
            def update():
                try:
                    if result is True:
                        self.imei_status_label.configure(text="✓ Türkiye Cihazı", text_color="#22c55e")
                    elif result is False:
                        self.imei_status_label.configure(text="⚠ Yurt Dışı Cihazı", text_color="#f97316")
                    else:
                        self.imei_status_label.configure(text="")
                except Exception:
                    pass
            self.after(0, update)

        threading.Thread(target=do_check, daemon=True).start()

    def _set_durum_filter(self, key):
        self.durum_filter = key
        self._update_filter_buttons()
        self.refresh_list()

    def _update_filter_buttons(self):
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute("SELECT durum, COUNT(*) FROM cihazlar GROUP BY durum")
        counts = dict(cursor.fetchall())
        conn.close()
        toplam = sum(counts.values())
        bekleyen = counts.get("Bekliyor", 0)
        tamirde = counts.get("Tamirde", 0)
        teslim = counts.get("Teslim Edildi", 0)
        t = THEMES.get(self.theme, THEMES["dark"])
        accent = t.get("accent", "#1a73e8")
        dim = t.get("text_dim", "#9ca3af")
        for key, btn in self._filter_buttons.items():
            if key == "Tümü":
                n = toplam
            elif key == "Bekleyenler":
                n = bekleyen
            elif key == "Tamirde":
                n = tamirde
            else:
                n = teslim
            btn.configure(text=f"{key} ({n})")
            if key == self.durum_filter:
                btn.configure(fg_color=accent, text_color="white")
            else:
                btn.configure(fg_color="transparent", text_color=dim)

    def refresh_list(self):
        for child in self.scroll_frame.winfo_children():
            child.destroy()

        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        where = ""
        params = []
        if self.durum_filter == "Bekleyenler":
            where = " WHERE durum = 'Bekliyor'"
        elif self.durum_filter == "Tamirde":
            where = " WHERE durum = 'Tamirde'"
        elif self.durum_filter == "Teslim Edildi":
            where = " WHERE durum = 'Teslim Edildi'"
        cursor.execute(
            f"""
            SELECT id, musteri_adi, telefon_modeli, imei, ariza, durum, tahmini_maliyet, created_at, son_guncelleme
            FROM cihazlar
            {where}
            ORDER BY created_at DESC, id DESC
            """
        )
        rows = cursor.fetchall()
        conn.close()

        # Arama filtresi: müşteri adı, telefon modeli, arıza açıklamasında büyük/küçük harf duyarsız ara
        query = ""
        if hasattr(self, "search_entry"):
            query = (self.search_entry.get() or "").strip().lower()
        elif hasattr(self, "search_var"):
            query = (self.search_var.get() or "").strip().lower()
        if query:
            filtered = []
            for row in rows:
                _, musteri_adi, telefon_modeli, _, ariza, _, _, _, _ = row
                text = " ".join(
                    [
                        musteri_adi or "",
                        telefon_modeli or "",
                        ariza or "",
                    ]
                ).lower()
                if query in text:
                    filtered.append(row)
            rows = filtered

        self._update_filter_buttons()
        self.list_info_label.configure(text=f"{len(rows)} kayıt")

        if not rows:
            t = THEMES.get(self.theme, THEMES["dark"])
            query = (getattr(self, "search_entry", None) and self.search_entry.get() or getattr(self, "search_var", None) and self.search_var.get() or "").strip()
            if query:
                msg = "Sonuç bulunamadı."
            elif self.durum_filter != "Tümü":
                msg = "Bu filtrede kayıt yok."
            else:
                msg = "Henüz kayıt yok. Soldan yeni kayıt ekleyin."
            empty_label = ctk.CTkLabel(
                self.scroll_frame,
                text=msg,
                font=ctk.CTkFont(size=13, slant="italic"),
                text_color=t.get("text_dim", "#9ca3af"),
            )
            empty_label.pack(pady=20)
            return

        t = THEMES.get(self.theme, THEMES["dark"])
        list_bg = t.get("list_bg", "#1a1a1a")
        card_bg = t.get("list_card_bg", "#2a2a2a")
        card_hover = t.get("list_card_hover", "#333333")
        card_border = t.get("list_card_border", "#3a3a3a")
        title_color = t.get("list_title_color", "#ffffff")
        sub_color = t.get("list_sub_color", "#aaaaaa")
        header_color = t.get("list_title_color", "#ffffff")
        header_dim = t.get("text_dim", "#9ca3af")

        from collections import OrderedDict
        gruplar = OrderedDict()
        for row in rows:
            created_at = row[7] or ""
            tarih_key = (created_at.split() or [""])[0]
            if tarih_key not in gruplar:
                gruplar[tarih_key] = []
            gruplar[tarih_key].append(row)

        for tarih_key, grup_rows in gruplar.items():
            baslik_metin = _tarih_baslik(tarih_key if tarih_key else "—")
            header_frame = ctk.CTkFrame(self.scroll_frame, fg_color="transparent")
            header_frame.pack(fill="x", padx=4, pady=(14, 6))
            ctk.CTkLabel(
                header_frame,
                text=baslik_metin,
                font=ctk.CTkFont(size=13, weight="bold"),
                text_color=header_color,
            ).pack(anchor="w")
            sep = ctk.CTkFrame(self.scroll_frame, height=1, fg_color=card_border)
            sep.pack(fill="x", padx=4, pady=(0, 4))

            for row in grup_rows:
                (
                    record_id,
                    musteri_adi,
                    telefon_modeli,
                    imei,
                    ariza,
                    durum,
                    tahmini_maliyet,
                    created_at,
                    son_guncelleme,
                ) = row

                musteri_adi = musteri_adi or "—"
                telefon_modeli = telefon_modeli or "—"
                imei = imei or "—"
                ariza_kisa = _ariza_kisa(ariza or "")
                tahmini_maliyet = tahmini_maliyet if tahmini_maliyet is not None else 0.0
                durum = durum or "Bekliyor"
                created_at = created_at or "—"

                card = ctk.CTkFrame(
                    self.scroll_frame,
                    corner_radius=12,
                    fg_color=card_bg,
                    border_width=1,
                    border_color=card_border,
                )
                card.pack(fill="x", padx=4, pady=8)

                def _on_enter(e, c=card, h=card_hover):
                    c.configure(fg_color=h)

                def _on_leave(e, c=card, b=card_bg):
                    c.configure(fg_color=b)

                card.bind("<Enter>", _on_enter)
                card.bind("<Leave>", _on_leave)

                top_frame = ctk.CTkFrame(card, fg_color="transparent")
                top_frame.pack(fill="x", padx=16, pady=(12, 4))
                top_frame.bind("<Enter>", _on_enter)
                top_frame.bind("<Leave>", _on_leave)

                title = ctk.CTkLabel(
                    top_frame,
                    text=f"{musteri_adi} — {telefon_modeli}",
                    font=ctk.CTkFont(size=14, weight="bold"),
                    text_color=title_color,
                )
                title.pack(side="left")
                title.bind("<Enter>", _on_enter)
                title.bind("<Leave>", _on_leave)

                durum_badge = ctk.CTkLabel(
                    top_frame,
                    text=durum,
                    font=ctk.CTkFont(size=12, weight="bold"),
                    fg_color=self._status_color(durum),
                    corner_radius=8,
                    padx=10,
                    pady=3,
                )
                durum_badge.pack(side="right")
                durum_badge.bind("<Enter>", _on_enter)
                durum_badge.bind("<Leave>", _on_leave)

                if ariza_kisa:
                    ariza_lbl = ctk.CTkLabel(
                        card,
                        text=ariza_kisa,
                        font=ctk.CTkFont(size=11),
                        text_color=sub_color,
                        anchor="w",
                        justify="left",
                    )
                    ariza_lbl.pack(fill="x", padx=16, pady=(0, 4))
                    ariza_lbl.bind("<Enter>", _on_enter)
                    ariza_lbl.bind("<Leave>", _on_leave)

                alt_metin = f"IMEI: {imei}   ·   Tahmini: {tahmini_maliyet:.2f} ₺   ·   Kayıt: {created_at}"
                if son_guncelleme:
                    sg = _son_guncelleme_metin(son_guncelleme)
                    if sg:
                        alt_metin += f"   ·   Son güncelleme: {sg}"
                subtitle = ctk.CTkLabel(
                    card,
                    text=alt_metin,
                    font=ctk.CTkFont(size=11),
                    text_color=sub_color,
                )
                subtitle.pack(fill="x", padx=16, pady=(0, 8))
                subtitle.bind("<Enter>", _on_enter)
                subtitle.bind("<Leave>", _on_leave)

                btn_f = ctk.CTkFrame(card, fg_color="transparent")
                btn_f.pack(anchor="e", padx=16, pady=(0, 12))
                action_button = ctk.CTkButton(
                    btn_f,
                    text="Detay / Düzenle",
                    width=120,
                    height=32,
                    corner_radius=8,
                    command=lambda rid=record_id: self.load_record(rid),
                )
                action_button.pack(side="left", padx=(0, 8))
                action_button.bind("<Enter>", _on_enter)
                action_button.bind("<Leave>", _on_leave)
                sil_btn = ctk.CTkButton(
                    btn_f,
                    text="🗑 Sil",
                    width=60,
                    height=32,
                    corner_radius=8,
                    fg_color="#b3261e",
                    hover_color="#7f1d1a",
                    command=lambda rid=record_id: self._sil_kayit(rid),
                )
                sil_btn.pack(side="left")
                sil_btn.bind("<Enter>", _on_enter)
                sil_btn.bind("<Leave>", _on_leave)

    def _kar_duzenle_popup(self, record_id, musteri_model, maliyet, satis, ariza=""):
        t = THEMES.get(self.theme, THEMES["dark"])
        pop = ctk.CTkToplevel(self)
        pop.title("Kar Düzenle")
        pop.minsize(420, 420)
        pop.transient(self)
        pop.grab_set()
        f = ctk.CTkFrame(pop, fg_color="transparent")
        f.pack(fill="both", expand=True, padx=24, pady=24)
        ctk.CTkLabel(f, text=musteri_model, font=ctk.CTkFont(size=14, weight="bold")).pack(anchor="w")
        ctk.CTkLabel(f, text="Tahmini Maliyet (₺)").pack(anchor="w", pady=(16, 4))
        m_e = ctk.CTkEntry(f, height=36)
        m_e.pack(fill="x", pady=(0, 4))
        m_e.insert(0, str(maliyet))
        ctk.CTkLabel(f, text="Müşteriye Söylenen Fiyat (₺)").pack(anchor="w", pady=(12, 4))
        s_e = ctk.CTkEntry(f, height=36)
        s_e.pack(fill="x", pady=(0, 8))
        s_e.insert(0, str(satis))
        ctk.CTkLabel(f, text="Arıza / Problem Açıklaması").pack(anchor="w", pady=(12, 4))
        ariza_tb = ctk.CTkTextbox(f, height=80)
        ariza_tb.pack(fill="x", pady=(0, 16))
        ariza_tb.insert("1.0", ariza or "")
        btn_f = ctk.CTkFrame(f, fg_color="transparent")
        btn_f.pack(fill="x")

        def kaydet():
            try:
                m = float(m_e.get().strip().replace(",", ".") or 0)
                s = float(s_e.get().strip().replace(",", ".") or 0)
            except ValueError:
                messagebox.showwarning("Hata", "Geçerli sayı girin.")
                return
            ariza_yeni = (ariza_tb.get("1.0", "end") or "").strip()
            conn = sqlite3.connect(DB_NAME)
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
            conn.execute(
                "UPDATE cihazlar SET tahmini_maliyet = ?, musteriye_soylenen_fiyat = ?, ariza = ?, son_guncelleme = ? WHERE id = ?",
                (m, s, ariza_yeni, now_str, record_id),
            )
            degisiklikler = []
            if m != (maliyet if isinstance(maliyet, (int, float)) else float(maliyet or 0)):
                degisiklikler.append("maliyet")
            if s != (satis if isinstance(satis, (int, float)) else float(satis or 0)):
                degisiklikler.append("satış fiyatı")
            if (ariza_yeni or "") != (ariza or ""):
                degisiklikler.append("açıklama")
            if degisiklikler:
                conn.execute(
                    "INSERT INTO degisiklik_log (cihaz_id, tarih, mesaj) VALUES (?, ?, ?)",
                    (record_id, now_str, "Güncellendi: " + ", ".join(degisiklikler)),
                )
            conn.commit()
            conn.close()
            messagebox.showinfo("Güncellendi", "Kayıt güncellendi.")
            pop.destroy()
            self.refresh_kar_analizi()
            self.refresh_list()

        def iptal():
            pop.destroy()

        ctk.CTkButton(btn_f, text="İptal", width=90, height=36, command=iptal, fg_color=t.get("text_dim", "#666")).pack(side="left", padx=(0, 10))
        ctk.CTkButton(btn_f, text="Kaydet", width=90, height=36, command=kaydet).pack(side="left")
        pop.geometry("420x420")
        pop.update_idletasks()
        w, h = max(420, pop.winfo_reqwidth()), max(420, pop.winfo_reqheight())
        x = (pop.winfo_screenwidth() - w) // 2
        y = (pop.winfo_screenheight() - h) // 2
        pop.geometry(f"{w}x{h}+{x}+{y}")

    def refresh_kar_analizi(self):
        for child in self.kar_scroll.winfo_children():
            child.destroy()

        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, musteri_adi, telefon_modeli, tahmini_maliyet, musteriye_soylenen_fiyat, created_at, ariza
            FROM cihazlar
            WHERE durum = 'Teslim Edildi'
            ORDER BY created_at DESC, id DESC
            """
        )
        rows = cursor.fetchall()
        conn.close()

        t = THEMES.get(self.theme, THEMES["dark"])
        toplam_kar = 0.0
        card_bg = t.get("kar_card_bg", t.get("list_card_bg", t["card_bg"]))
        card_border = t.get("kar_card_border", t.get("list_card_border", t["border_color"]))
        title_c = t.get("kar_title", t.get("list_title_color", "#ffffff"))
        maliyet_c = t.get("kar_maliyet", "#ff6b6b")
        satis_c = t.get("kar_satis", "#64b5f6")
        kar_c = t.get("kar_kar", "#00c853")
        header_bg = t.get("kar_header_bg", "transparent")
        total_bg = t.get("kar_total_bg", "#1e1e1e")
        total_text = t.get("kar_total_text", "#ffffff")
        sep_c = t.get("separator", card_border)
        dim_c = t.get("text_dim", "#999999")

        if not rows:
            ctk.CTkLabel(
                self.kar_scroll,
                text="Teslim edilmiş kayıt yok.",
                font=ctk.CTkFont(size=13, slant="italic"),
                text_color=dim_c,
            ).pack(pady=20)
            self.kar_summary_label.configure(text="Toplam Kar: 0,00 ₺")
            return

        from collections import OrderedDict
        gruplar = OrderedDict()
        for row in rows:
            created_at = row[5] if len(row) > 5 else ""
            tarih_key = (created_at.split() or [""])[0]
            if tarih_key not in gruplar:
                gruplar[tarih_key] = []
            gruplar[tarih_key].append(row)

        for tarih_key, grup_rows in gruplar.items():
            baslik_metin = _tarih_baslik(tarih_key if tarih_key else "—")
            header_f = ctk.CTkFrame(self.kar_scroll, fg_color=header_bg if header_bg != "transparent" else "transparent")
            header_f.pack(fill="x", padx=4, pady=(16, 8))
            ctk.CTkLabel(header_f, text=baslik_metin, font=ctk.CTkFont(size=13, weight="bold"), text_color=title_c).pack(anchor="w", padx=8, pady=6)
            sep = ctk.CTkFrame(self.kar_scroll, height=1, fg_color=sep_c)
            sep.pack(fill="x", padx=4, pady=(0, 8))

            gun_maliyet = 0.0
            gun_satis = 0.0
            gun_kar = 0.0

            for row in grup_rows:
                record_id, musteri_adi, telefon_modeli, maliyet, satis, _ = row[:6]
                ariza_metin = row[6] if len(row) > 6 else ""
                maliyet = maliyet or 0.0
                satis = satis or 0.0
                kar = satis - maliyet
                marj = (kar / satis * 100) if satis else 0.0
                gun_maliyet += maliyet
                gun_satis += satis
                gun_kar += kar
                toplam_kar += kar
                musteri_adi = musteri_adi or "—"
                telefon_modeli = telefon_modeli or "—"
                musteri_model = f"{musteri_adi} — {telefon_modeli}"

                card = ctk.CTkFrame(self.kar_scroll, corner_radius=12, fg_color=card_bg, border_width=1, border_color=card_border)
                card.pack(fill="x", padx=4, pady=10)

                row_f = ctk.CTkFrame(card, fg_color="transparent")
                row_f.pack(fill="x", padx=16, pady=(14, 6))
                ctk.CTkLabel(row_f, text=musteri_model, font=ctk.CTkFont(size=14, weight="bold"), text_color=title_c).pack(side="left")
                ctk.CTkButton(
                    row_f,
                    text="Düzenle",
                    width=70,
                    height=28,
                    corner_radius=6,
                    command=lambda rid=record_id, mm=musteri_model, ml=maliyet, st=satis, ar=ariza_metin: self._kar_duzenle_popup(rid, mm, ml, st, ar),
                ).pack(side="right", padx=(0, 6))
                ctk.CTkButton(
                    row_f,
                    text="🗑 Sil",
                    width=50,
                    height=28,
                    corner_radius=6,
                    fg_color="#b3261e",
                    hover_color="#7f1d1a",
                    command=lambda rid=record_id: self._sil_kayit(rid),
                ).pack(side="right")
                detay_f = ctk.CTkFrame(card, fg_color="transparent")
                detay_f.pack(fill="x", padx=16, pady=(0, 14))
                ctk.CTkLabel(detay_f, text=f"🔧 Maliyet: {maliyet:,.2f} ₺", font=ctk.CTkFont(size=12), text_color=maliyet_c).pack(side="left", padx=(0, 16))
                ctk.CTkLabel(detay_f, text=f"💰 Satış: {satis:,.2f} ₺", font=ctk.CTkFont(size=12), text_color=satis_c).pack(side="left", padx=(0, 16))
                ctk.CTkLabel(detay_f, text=f"📈 Kar: {kar:,.2f} ₺ ({marj:.1f}%)", font=ctk.CTkFont(size=12, weight="bold"), text_color=kar_c).pack(side="left")

            # Gün alt toplamı — görsel olarak ayrı (üst çizgi + farklı arka plan)
            total_sep = ctk.CTkFrame(self.kar_scroll, height=1, fg_color=sep_c)
            total_sep.pack(fill="x", padx=4, pady=(8, 0))
            sub_f = ctk.CTkFrame(self.kar_scroll, fg_color=total_bg, corner_radius=8)
            sub_f.pack(fill="x", padx=4, pady=(6, 14))
            ctk.CTkLabel(
                sub_f,
                text=f"Toplam Maliyet: {gun_maliyet:,.2f} ₺   ·   Toplam Satış: {gun_satis:,.2f} ₺   ·   Toplam Kar: {gun_kar:,.2f} ₺",
                font=ctk.CTkFont(size=14, weight="bold"),
                text_color=total_text,
            ).pack(anchor="e", padx=12, pady=10)

        self.kar_summary_label.configure(text=f"Toplam Kar: {toplam_kar:,.2f} ₺")

    @staticmethod
    def _status_color(durum: str) -> str:
        if durum == "Bekliyor":
            return "#4b5563"
        if durum == "Tamirde":
            return "#1d4ed8"
        if durum == "Teslim Edildi":
            return "#15803d"
        return "#4b5563"

    def clear_form(self):
        self.selected_id = None
        self.son_guncelleme_label.configure(text="")
        self.musteri_entry.delete(0, "end")
        self.model_entry.delete(0, "end")
        self.imei_entry.delete(0, "end")
        self.imei_status_label.configure(text="")
        self.ariza_text.delete("1.0", "end")
        self.maliyet_entry.delete(0, "end")
        self.satis_entry.delete(0, "end")
        self.durum_var.set("Bekliyor")
        self.save_button.configure(text="Kaydet")

    def save_record(self):
        musteri = self.musteri_entry.get().strip() or ""
        model = self.model_entry.get().strip() or ""
        imei = self.imei_entry.get().strip() or ""
        ariza = self.ariza_text.get("1.0", "end").strip() or ""
        maliyet_str = self.maliyet_entry.get().strip().replace(",", ".") or "0"
        satis_str = self.satis_entry.get().strip().replace(",", ".") or "0"
        durum = self.durum_var.get().strip() or "Bekliyor"

        try:
            maliyet = float(maliyet_str) if maliyet_str else 0.0
        except ValueError:
            maliyet = 0.0
        try:
            satis = float(satis_str) if satis_str else 0.0
        except ValueError:
            satis = 0.0

        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()

        if self.selected_id is None:
            cursor.execute(
                """
                INSERT INTO cihazlar
                (musteri_adi, telefon_modeli, imei, ariza, tahmini_maliyet, musteriye_soylenen_fiyat, durum, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    musteri,
                    model,
                    imei,
                    ariza,
                    maliyet,
                    satis,
                    durum,
                    datetime.now().strftime("%Y-%m-%d %H:%M"),
                ),
            )
            messagebox.showinfo("Başarılı", "Yeni servis kaydı oluşturuldu.")
        else:
            cursor.execute(
                "SELECT tahmini_maliyet, musteriye_soylenen_fiyat, ariza, durum FROM cihazlar WHERE id = ?",
                (self.selected_id,),
            )
            eski = cursor.fetchone()
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
            degisiklikler = []
            if eski:
                e_mal, e_sat, e_ari, e_dur = eski
                if (maliyet or 0) != (e_mal or 0):
                    degisiklikler.append("maliyet")
                if (satis or 0) != (e_sat or 0):
                    degisiklikler.append("satış fiyatı")
                if (ariza or "") != (e_ari or ""):
                    degisiklikler.append("açıklama")
                if (durum or "") != (e_dur or ""):
                    degisiklikler.append("durum")
            cursor.execute(
                """
                UPDATE cihazlar
                SET musteri_adi = ?, telefon_modeli = ?, imei = ?, ariza = ?,
                    tahmini_maliyet = ?, musteriye_soylenen_fiyat = ?, durum = ?, son_guncelleme = ?
                WHERE id = ?
                """,
                (
                    musteri,
                    model,
                    imei,
                    ariza,
                    maliyet,
                    satis,
                    durum,
                    now_str,
                    self.selected_id,
                ),
            )
            if degisiklikler:
                cursor.execute(
                    "INSERT INTO degisiklik_log (cihaz_id, tarih, mesaj) VALUES (?, ?, ?)",
                    (self.selected_id, now_str, "Güncellendi: " + ", ".join(degisiklikler)),
                )
            messagebox.showinfo("Güncellendi", "Servis kaydı güncellendi.")

        conn.commit()
        conn.close()

        self.clear_form()
        self.refresh_list()
        self.refresh_kar_analizi()

    def load_record(self, record_id: int):
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, musteri_adi, telefon_modeli, imei, ariza,
                   tahmini_maliyet, musteriye_soylenen_fiyat, durum, son_guncelleme
            FROM cihazlar
            WHERE id = ?
            """,
            (record_id,),
        )
        row = cursor.fetchone()
        conn.close()

        if not row:
            messagebox.showerror("Hata", "Kayıt bulunamadı.")
            return

        self.selected_id, musteri_adi, telefon_modeli, imei, ariza, tahmini_maliyet, musteriye_soylenen_fiyat, durum = row[:8]
        son_guncelleme = row[8] if len(row) > 8 else None

        if son_guncelleme:
            self.son_guncelleme_label.configure(text="Son Güncelleme: " + _son_guncelleme_metin(son_guncelleme))
        else:
            self.son_guncelleme_label.configure(text="")

        self.musteri_entry.delete(0, "end")
        self.musteri_entry.insert(0, musteri_adi or "")

        self.model_entry.delete(0, "end")
        self.model_entry.insert(0, telefon_modeli or "")

        self.imei_entry.delete(0, "end")
        self.imei_entry.insert(0, imei or "")
        self._on_imei_changed()

        self.ariza_text.delete("1.0", "end")
        self.ariza_text.insert("1.0", ariza or "")

        self.maliyet_entry.delete(0, "end")
        self.maliyet_entry.insert(0, str(tahmini_maliyet) if tahmini_maliyet is not None else "")

        self.satis_entry.delete(0, "end")
        self.satis_entry.insert(0, str(musteriye_soylenen_fiyat) if musteriye_soylenen_fiyat is not None else "")

        self.durum_var.set(durum or "Bekliyor")
        self.save_button.configure(text="Güncelle")

    def delete_record(self):
        if self.selected_id is None:
            messagebox.showwarning(
                "Seçim yok",
                "Silmek için listeden bir kayıt seçin.",
            )
            return

        cevap = messagebox.askyesno(
            "Silme onayı", "Bu kaydı silmek istediğinize emin misiniz?"
        )
        if not cevap:
            return

        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM cihazlar WHERE id = ?", (self.selected_id,))
        conn.commit()
        conn.close()

        messagebox.showinfo("Silindi", "Kayıt başarıyla silindi.")
        self.clear_form()
        self.refresh_list()
        self.refresh_kar_analizi()


def main():
    _ensure_bundled_config()
    init_db()
    try:
        yedek_klasoru_hazirla()
        otomatik_yedek_al()
    except Exception:
        pass
    ctk.set_appearance_mode("dark" if load_theme_preference() == "dark" else "light")
    ctk.set_default_color_theme("blue")
    app = ServisApp()
    app.mainloop()


if __name__ == "__main__":
    main()

