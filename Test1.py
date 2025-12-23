# -*- coding: utf-8 -*-
"""
SMART PARKING - FULL (Tkinter + Serial + Live Camera + OCR + Spot UI + Log CSV)

✅ LOGIC ĐÃ CHỐT (KHÔNG ĐỔI):
- Mặc định motor đang ở vị trí 1 (Python current_pos = 1). Khi kết nối COM sẽ gửi SETPOS:1 để đồng bộ.
- Khi quét thẻ / bấm nút -> OCR NGAY thời điểm đó (không timeout vòng lặp).
- Cổng vào:
  RFID_IN / TOUCH_IN / nút thủ công -> OCR -> nếu OK -> chọn ô trống -> nếu target==1 và current_pos==1 thì KHÔNG quay
  nếu cần quay -> GO:n + chờ ARRIVED:n -> (beep 1 lần) -> OPEN_IN (Arduino tự beep 2 lần + auto close 3s)
  -> hiển thị trạng thái lên LCD1 (LCD IN)
- Cổng ra:
  RFID_OUT / TOUCH_OUT / nút thủ công -> OCR -> nếu OK -> tìm xe đang ở ô nào -> quay đến ô đó -> (beep 1 lần)
  -> OUT,<plate> (hiển thị trên LCD2) -> OPEN_OUT (Arduino tự beep 2 lần + auto close 3s)
  -> hiển thị trạng thái lên LCD2 (LCD OUT)

✅ BỔ SUNG (THEO YÊU CẦU): 1 TRANG WEB ĐẶT TRƯỚC Ô ĐỖ
- Form: tên, sdt, biển số, ô đỗ, thời gian đỗ (giờ dự kiến)
- Tính phí: từ lúc XÁC NHẬN ĐẶT (created_at) -> đến khi xe RỜI BÃI (exit_time)
  => phí = (exit_time - created_at) * fee_per_hour (làm tròn 1000đ)
- Khi xe vào mà biển số trùng reservation (status=reserved): sẽ ưu tiên ô đã đặt.
- Spot đã đặt trước sẽ không được cấp cho xe khác (trừ khi xe đó đúng biển số reservation).

"""

import os, csv, time, threading, queue, math, re
from datetime import datetime

import tkinter as tk
from tkinter import ttk, filedialog
from PIL import Image, ImageTk, Image as PILImage
import cv2

# Serial
try:
    import serial
    import serial.tools.list_ports
    SERIAL_OK = True
except Exception:
    serial = None
    SERIAL_OK = False

# ==== YOLO OCR (có thì dùng, không có thì mock) ====
try:
    import torch
    import function.utils_rotate as utils_rotate
    import function.helper as helper
    TORCH_OK = True
except Exception:
    TORCH_OK = False
    print("Không có torch/function -> dùng mock OCR để test.")

    class _MockValues:
        def __init__(self): self._vals = [[100, 100, 300, 200, 0.95, 0]]
        def tolist(self): return self._vals

    class _MockDF:
        def __init__(self): self.values = _MockValues()

    class _MockPandasResult:
        def __init__(self): self.xyxy = [_MockDF()]
        def pandas(self): return self

    class MockYoloModel:
        def __init__(self): self.conf = 0.6
        def __call__(self, frame, size=640): return _MockPandasResult()

    class helper:
        @staticmethod
        def read_plate(model, img): return "80T-8888"

    class utils_rotate:
        @staticmethod
        def deskew(img, a, b): return img

yolo_LP_detect = None
yolo_license_plate = None
if TORCH_OK:
    try:
        yolo_LP_detect = torch.hub.load(
            'yolov5', 'custom',
            path='model/LP_detector_nano_61.pt',
            force_reload=False, source='local'
        )
        yolo_license_plate = torch.hub.load(
            'yolov5', 'custom',
            path='model/LP_ocr_nano_62.pt',
            force_reload=False, source='local'
        )
        yolo_license_plate.conf = 0.60
    except Exception as e:
        print("Không tải được YOLO, chuyển sang mock. Lỗi:", e)
        yolo_LP_detect = MockYoloModel()
        yolo_license_plate = MockYoloModel()
else:
    yolo_LP_detect = MockYoloModel()
    yolo_license_plate = MockYoloModel()


# ===================== CONFIG =====================
CSV_LOG      = "lich_su_xe.csv"
CSV_SPOTS    = "vi_tri_do.csv"
CSV_SETTINGS = "settings.csv"
CSV_RESERVED = "dat_cho_truoc.csv"   # ✅ web reservation

DEFAULT_FEE_PER_HOUR = 5000

UID_COOLDOWN_MS_IN  = 2500
UID_COOLDOWN_MS_OUT = 2500
SERIAL_SAME_LINE_COOLDOWN_MS = 700

ARRIVED_TIMEOUT_SEC = 28
DISPLAY_RESET_MS = 8000

SPOT_TO_TARGET = {'A1': 1, 'A2': 2, 'A3': 3, 'A4': 4}
TARGET_TO_SPOT = {v: k for k, v in SPOT_TO_TARGET.items()}

PLATE_RE = re.compile(r"^[0-9A-Z]{2,3}[A-Z]{0,2}-?[0-9A-Z]{3,6}$")


def now_ms():
    return int(time.time() * 1000)

def vn_clock_str():
    dow = ["Thứ Hai","Thứ Ba","Thứ Tư","Thứ Năm","Thứ Sáu","Thứ Bảy","Chủ Nhật"]
    d = datetime.now()
    return f"{dow[d.weekday()]}, {d.strftime('%d/%m/%Y | %H:%M:%S')}"

def normalize_plate(p: str) -> str:
    p = (p or "").upper().strip()
    p = p.replace(" ", "").replace(".", "").replace("_", "-")
    return p

def is_valid_plate(p: str) -> bool:
    p = normalize_plate(p)
    if len(p) < 6 or len(p) > 12:
        return False
    if PLATE_RE.match(p):
        return True
    return all(ch.isalnum() or ch == '-' for ch in p)

def fmt_money(v: int) -> str:
    try:
        return f"{int(v):,}".replace(",", ".")
    except Exception:
        return str(v)

def ceil_1000(v: float) -> int:
    if v <= 0:
        return 0
    return int(math.ceil(v / 1000.0) * 1000)


# ===================== CSV =====================
def ensure_csv_log():
    if not os.path.isfile(CSV_LOG):
        with open(CSV_LOG, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["ma_the","bien_so","thoi_gian_vao","thoi_gian_ra","phi"])

def ensure_csv_spots():
    if not os.path.isfile(CSV_SPOTS):
        with open(CSV_SPOTS, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["spot","status","plate","rfid_uid","entry_time"])
            for s in SPOT_TO_TARGET.keys():
                w.writerow([s,"empty","","",""])

def ensure_csv_settings():
    if not os.path.isfile(CSV_SETTINGS):
        with open(CSV_SETTINGS, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["key","value"])
            w.writerow(["fee_per_hour", str(DEFAULT_FEE_PER_HOUR)])
            w.writerow(["cam_in", "0"])
            w.writerow(["cam_out","1"])
            w.writerow(["com_port",""])

def ensure_csv_reserved():
    if not os.path.isfile(CSV_RESERVED):
        with open(CSV_RESERVED, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            # status: reserved | in | done | cancel
            w.writerow(["id","ten","sdt","bien_so","spot","gio_du_kien","created_at","status","exit_time","final_fee"])

def read_settings():
    ensure_csv_settings()
    d = {"fee_per_hour": str(DEFAULT_FEE_PER_HOUR), "cam_in":"0", "cam_out":"1", "com_port":""}
    try:
        with open(CSV_SETTINGS, "r", newline="", encoding="utf-8") as f:
            rd = csv.reader(f)
            next(rd, None)
            for k, v in rd:
                d[k] = v
    except Exception:
        pass
    try:
        d["fee_per_hour"] = int(d.get("fee_per_hour", DEFAULT_FEE_PER_HOUR))
    except Exception:
        d["fee_per_hour"] = DEFAULT_FEE_PER_HOUR
    return d

def write_settings(d):
    rows = [["key","value"]]
    for k, v in d.items():
        rows.append([k, str(v)])
    with open(CSV_SETTINGS, "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows(rows)


# ===================== TOAST =====================
class Toast:
    def __init__(self, root):
        self.root = root
        self.win = None
        self._hide_job = None

    def show(self, msg, ms=1800):
        try:
            if self.win and self.win.winfo_exists():
                self.win.destroy()
        except Exception:
            pass

        self.win = tk.Toplevel(self.root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)

        lbl = tk.Label(self.win, text=msg, bg="#222", fg="white",
                       font=("Helvetica", 11, "bold"), padx=12, pady=8)
        lbl.pack()

        self.root.update_idletasks()
        x = self.root.winfo_rootx() + self.root.winfo_width() - 20 - self.win.winfo_reqwidth()
        y = self.root.winfo_rooty() + self.root.winfo_height() - 60 - self.win.winfo_reqheight()
        self.win.geometry(f"+{x}+{y}")

        if self._hide_job:
            self.root.after_cancel(self._hide_job)
        self._hide_job = self.root.after(ms, self.hide)

    def hide(self):
        try:
            if self.win and self.win.winfo_exists():
                self.win.destroy()
        except Exception:
            pass
        self.win = None
        self._hide_job = None


# ===================== APP =====================
class ParkingApp:
    def __init__(self, window, title):
        self.window = window
        self.window.title(title)
        self.window.configure(bg="#e6f0ff")

        style = ttk.Style(self.window)
        try: style.theme_use('clam')
        except Exception: pass
        style.configure("TLabelFrame", borderwidth=0, background="#e6f0ff")
        style.configure("TLabelFrame.Label", foreground="blue", background="#e6f0ff", font=("Helvetica", 11, "bold"))

        self.toast = Toast(self.window)

        # data
        self.parking_spots = {s: None for s in SPOT_TO_TARGET.keys()}
        self.spot_labels = {}

        # motor pos (mặc định 1)
        self.current_pos = 1

        # settings
        self.settings = read_settings()
        self.fee_per_hour = int(self.settings.get("fee_per_hour", DEFAULT_FEE_PER_HOUR))

        # camera
        self.source_in = self._parse_cam_source(self.settings.get("cam_in","0"))
        self.source_out = self._parse_cam_source(self.settings.get("cam_out","1"))
        self.vid_in = None
        self.vid_out = None
        self.last_frame_in = None
        self.last_frame_out = None

        # serial
        self.master_serial_connection = None
        self.listener_thread = None
        self.stop_thread = threading.Event()

        # queues
        self.rfid_queue_in  = queue.Queue()
        self.rfid_queue_out = queue.Queue()
        self.touch_queue_in = queue.Queue()
        self.touch_queue_out= queue.Queue()
        self.arrived_queue  = queue.Queue()

        self.uid_last_time = {'in':{}, 'out':{}}
        self.last_serial_line = ""
        self.last_serial_time_ms = 0

        # locks
        self.entry_lock = threading.Lock()
        self.exit_lock  = threading.Lock()
        self.entry_busy = False
        self.exit_busy  = False

        # init files
        ensure_csv_spots()
        ensure_csv_log()
        ensure_csv_settings()
        ensure_csv_reserved()

        self.load_spots_from_csv()

        # UI
        self._build_ui()

        # camera init
        self._open_cams()

        # ✅ start web server (đặt dưới cùng code, nhưng chạy ở đây)
        self._start_web_thread()

        # auto connect COM
        if self.settings.get("com_port",""):
            self.window.after(700, lambda: self.start_master_listener(self.settings.get("com_port",""), 9600))

        # loop
        self.delay = 35
        self.window.after(self.delay, self.update_loop)
        self.window.protocol("WM_DELETE_WINDOW", self.on_closing)

        # initial UI
        self.update_spot_display()

    # ---------- parse source ----------
    def _parse_cam_source(self, s):
        s = str(s).strip()
        if s.isdigit():
            return int(s)
        return s

    # ---------- UI ----------
    def _build_ui(self):
        self._create_menu()

        main = tk.Frame(self.window, bg="#e6f0ff")
        main.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        left = tk.Frame(main, bg="#e6f0ff")
        left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 6))

        right = tk.Frame(main, bg="#e6f0ff")
        right.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True, padx=(6, 0))

        # Left grid 2x2: cam in, img in, cam out, img out
        FIX_W, FIX_H = 520, 380
        left.columnconfigure(0, minsize=FIX_W)
        left.columnconfigure(1, minsize=FIX_W)
        left.rowconfigure(0, minsize=FIX_H)
        left.rowconfigure(1, minsize=FIX_H)

        self.placeholder_video = ImageTk.PhotoImage(PILImage.new("RGB", (FIX_W, FIX_H), "white"))

        f1 = ttk.LabelFrame(left, text="Camera ngõ vào")
        f1.grid(row=0, column=0, sticky="nsew", padx=6, pady=6)
        f1.pack_propagate(False)
        self.label_cam_in = tk.Label(f1, image=self.placeholder_video, bg="white")
        self.label_cam_in.pack(fill=tk.BOTH, expand=True)

        f2 = ttk.LabelFrame(left, text="Ảnh xe vào (snapshot)")
        f2.grid(row=0, column=1, sticky="nsew", padx=6, pady=6)
        f2.pack_propagate(False)
        self.label_img_in = tk.Label(f2, image=self.placeholder_video, bg="white")
        self.label_img_in.pack(fill=tk.BOTH, expand=True)

        f3 = ttk.LabelFrame(left, text="Camera ngõ ra")
        f3.grid(row=1, column=0, sticky="nsew", padx=6, pady=6)
        f3.pack_propagate(False)
        self.label_cam_out = tk.Label(f3, image=self.placeholder_video, bg="white")
        self.label_cam_out.pack(fill=tk.BOTH, expand=True)

        f4 = ttk.LabelFrame(left, text="Ảnh xe ra (snapshot)")
        f4.grid(row=1, column=1, sticky="nsew", padx=6, pady=6)
        f4.pack_propagate(False)
        self.label_img_out = tk.Label(f4, image=self.placeholder_video, bg="white")
        self.label_img_out.pack(fill=tk.BOTH, expand=True)

        # Right side: plate info + time cost + spots + log
        pf = ttk.LabelFrame(right, text="Thông tin biển số")
        pf.pack(fill=tk.X, padx=6, pady=(6, 6))
        pf.columnconfigure((0, 1, 2), weight=1)

        self.placeholder_plate = ImageTk.PhotoImage(PILImage.new("RGB", (160, 80), "white"))

        tk.Label(pf, text="Biển số vào", font=("Helvetica",10,"bold"), bg="#dcdad5").grid(row=0, column=0, pady=(6, 0))
        self.label_plate_in = tk.Label(pf, image=self.placeholder_plate, bg="white")
        self.label_plate_in.grid(row=1, column=0, padx=6, pady=6)
        self.plate_in_var = tk.StringVar(value="---")
        tk.Label(pf, textvariable=self.plate_in_var, font=("Helvetica",14,"bold"), bg="#dcdad5").grid(row=2, column=0, pady=(0, 6))

        self.match_status_var = tk.StringVar(value="")
        tk.Label(pf, textvariable=self.match_status_var, font=("Helvetica",14,"bold","italic"),
                 fg="green", bg="#dcdad5").grid(row=1, column=1, padx=10)

        tk.Label(pf, text="Biển số ra", font=("Helvetica",10,"bold"), bg="#dcdad5").grid(row=0, column=2, pady=(6, 0))
        self.label_plate_out = tk.Label(pf, image=self.placeholder_plate, bg="white")
        self.label_plate_out.grid(row=1, column=2, padx=6, pady=6)
        self.plate_out_var = tk.StringVar(value="---")
        tk.Label(pf, textvariable=self.plate_out_var, font=("Helvetica",14,"bold"), bg="#dcdad5").grid(row=2, column=2, pady=(0, 6))

        tf = ttk.LabelFrame(right, text="Thời gian & Chi phí")
        tf.pack(fill=tk.X, padx=6, pady=6)
        self.clock_var = tk.StringVar(value=vn_clock_str())
        self.pos_var = tk.StringVar(value="Vị trí hiện tại: 1")
        self.duration_var = tk.StringVar(value="Thời gian gửi: --:--:--")
        self.fee_var = tk.StringVar(value="Phí gửi xe: -- VNĐ")

        tk.Label(tf, textvariable=self.clock_var, font=("Helvetica",13,"bold"), bg="#dcdad5").pack(fill=tk.X, padx=8, pady=(8, 2))
        tk.Label(tf, textvariable=self.pos_var, font=("Helvetica",11,"bold"), bg="#dcdad5").pack(fill=tk.X, padx=8, pady=2)
        tk.Label(tf, textvariable=self.duration_var, font=("Helvetica",11), bg="#dcdad5").pack(fill=tk.X, padx=8, pady=2)
        tk.Label(tf, textvariable=self.fee_var, font=("Helvetica",11,"bold"), bg="#dcdad5").pack(fill=tk.X, padx=8, pady=(2, 8))

        bf = tk.Frame(tf, bg="#dcdad5")
        bf.pack(pady=(0, 10))
        ttk.Button(bf, text="Xác nhận vào (Thủ công)", command=self.capture_in).pack(side=tk.LEFT, padx=10)
        ttk.Button(bf, text="Xác nhận ra (Thủ công)",  command=self.capture_out).pack(side=tk.LEFT, padx=10)
        ttk.Button(bf, text="Cài đặt", command=self.open_settings_window).pack(side=tk.LEFT, padx=10)

        sf = ttk.LabelFrame(right, text="Trạng thái bãi xe")
        sf.pack(fill=tk.X, padx=6, pady=6)
        sf.columnconfigure((0,1,2,3), weight=1)
        fontL = ("Helvetica", 14, "bold")
        for i, spot in enumerate(self.parking_spots.keys()):
            lb = tk.Label(sf, text=spot, font=fontL, relief=tk.RAISED, bd=2, width=6, height=2)
            lb.grid(row=0, column=i, padx=6, pady=10, sticky="ew")
            self.spot_labels[spot] = lb

        nb = ttk.Notebook(right)
        nb.pack(fill=tk.BOTH, expand=True, padx=6, pady=(6, 6))
        tab_log = ttk.Frame(nb)
        nb.add(tab_log, text="Lịch sử xe")
        self._build_log_tab(tab_log)

        self.load_log_from_csv()

    def _create_menu(self):
        menubar = tk.Menu(self.window)
        self.window.config(menu=menubar)

        m_file = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Tệp", menu=m_file)
        m_file.add_command(label="Đổi nguồn Camera Vào (tạm thời)...", command=lambda: self.select_media_source('in'))
        m_file.add_command(label="Đổi nguồn Camera Ra (tạm thời)...",  command=lambda: self.select_media_source('out'))
        m_file.add_separator()
        m_file.add_command(label="Thoát", command=self.on_closing)

        m_tools = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Công cụ", menu=m_tools)
        m_tools.add_command(label="Cài đặt", command=self.open_settings_window)
        m_tools.add_command(label="Web đặt chỗ: http://127.0.0.1:5000", command=lambda: self.toast.show("Mở trình duyệt: http://127.0.0.1:5000", 2200))

    def _build_log_tab(self, parent):
        cols = ('Mã Thẻ','Biển số','Thời gian vào','Thời gian ra','Phí')
        self.tree_log = ttk.Treeview(parent, columns=cols, show='headings')
        for c in cols:
            self.tree_log.heading(c, text=c)
        self.tree_log.column('Mã Thẻ', width=110, anchor=tk.CENTER)
        self.tree_log.column('Biển số', width=120, anchor=tk.CENTER)
        self.tree_log.column('Thời gian vào', width=170, anchor=tk.CENTER)
        self.tree_log.column('Thời gian ra', width=170, anchor=tk.CENTER)
        self.tree_log.column('Phí', width=90, anchor=tk.E)

        self.tree_log.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb = ttk.Scrollbar(parent, orient="vertical", command=self.tree_log.yview)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree_log.configure(yscrollcommand=sb.set)

    # ---------- settings window ----------
    def open_settings_window(self):
        w = tk.Toplevel(self.window)
        w.title("Cài đặt")
        w.configure(bg="#e6f0ff")
        w.resizable(False, False)

        ports = self._find_coms()
        cams = self._find_cams()

        # COM
        sf = ttk.LabelFrame(w, text="Arduino MASTER (COM)")
        sf.pack(padx=16, pady=10, fill="x")
        tk.Label(sf, text="Cổng COM:", bg="#dcdad5").grid(row=0, column=0, padx=8, pady=10, sticky="w")
        com_var = tk.StringVar(value=self.settings.get("com_port",""))
        cb = ttk.Combobox(sf, textvariable=com_var, values=ports, state="readonly", width=22)
        cb.grid(row=0, column=1, padx=8, pady=10)

        def connect():
            self.settings["com_port"] = com_var.get()
            write_settings(self.settings)
            self.start_master_listener(com_var.get(), 9600)
            self.toast.show("Đang kết nối COM...", 1500)

        ttk.Button(sf, text="Kết nối", command=connect).grid(row=0, column=2, padx=10, pady=10)

        # Fee
        ff = ttk.LabelFrame(w, text="Phí gửi xe (VNĐ/giờ)")
        ff.pack(padx=16, pady=10, fill="x")
        fee_var = tk.StringVar(value=str(self.fee_per_hour))
        ttk.Entry(ff, textvariable=fee_var, width=18).pack(side=tk.LEFT, padx=10, pady=10)

        def save_fee():
            try:
                v = int(fee_var.get())
                if v < 0: raise ValueError
                self.fee_per_hour = v
                self.settings["fee_per_hour"] = str(v)
                write_settings(self.settings)
                self.toast.show(f"Đã lưu phí: {fmt_money(v)} VNĐ/giờ", 1800)
            except Exception:
                self.toast.show("Phí không hợp lệ.", 1800)

        ttk.Button(ff, text="Lưu", command=save_fee).pack(side=tk.LEFT, padx=10, pady=10)

        # Cameras
        cf = ttk.LabelFrame(w, text="Camera")
        cf.pack(padx=16, pady=10, fill="x")

        tk.Label(cf, text="Camera vào:", bg="#dcdad5").grid(row=0, column=0, padx=8, pady=8, sticky="w")
        cam_in_var = tk.StringVar(value=str(self.settings.get("cam_in","0")))
        cb_in = ttk.Combobox(cf, textvariable=cam_in_var, values=cams, state="readonly", width=22)
        cb_in.grid(row=0, column=1, padx=8, pady=8)

        tk.Label(cf, text="Camera ra:", bg="#dcdad5").grid(row=1, column=0, padx=8, pady=8, sticky="w")
        cam_out_var = tk.StringVar(value=str(self.settings.get("cam_out","1")))
        cb_out = ttk.Combobox(cf, textvariable=cam_out_var, values=cams, state="readonly", width=22)
        cb_out.grid(row=1, column=1, padx=8, pady=8)

        def parse_cam(x):
            x = str(x)
            if x.startswith("Camera "):
                try: return int(x.split("Camera ", 1)[1].strip())
                except Exception: return 0
            return x

        def apply_cams():
            self.settings["cam_in"] = str(parse_cam(cam_in_var.get()))
            self.settings["cam_out"] = str(parse_cam(cam_out_var.get()))
            write_settings(self.settings)
            self.source_in = self._parse_cam_source(self.settings["cam_in"])
            self.source_out = self._parse_cam_source(self.settings["cam_out"])
            self._open_cams()
            self.toast.show("Đã áp dụng camera.", 1600)

        ttk.Button(cf, text="Áp dụng", command=apply_cams).grid(row=0, rowspan=2, column=2, padx=10, pady=8)

    def _find_cams(self):
        res = []
        backend = cv2.CAP_DSHOW if os.name == "nt" else cv2.CAP_ANY
        for i in range(10):
            cap = cv2.VideoCapture(i, backend)
            if cap is not None and cap.isOpened():
                res.append(f"Camera {i}")
                cap.release()
        return res if res else ["Không tìm thấy camera"]

    def _find_coms(self):
        if not SERIAL_OK:
            return ["pyserial chưa được cài đặt"]
        try:
            ports = serial.tools.list_ports.comports()
            return [p.device for p in ports] if ports else ["Không tìm thấy cổng COM"]
        except Exception:
            return ["Không tìm thấy cổng COM"]

    # ---------- camera ----------
    def _open_cams(self):
        if self.vid_in:
            try: self.vid_in.release()
            except Exception: pass
        if self.vid_out:
            try: self.vid_out.release()
            except Exception: pass

        backend = cv2.CAP_DSHOW if os.name == "nt" else cv2.CAP_ANY
        self.vid_in  = cv2.VideoCapture(self.source_in, backend)
        self.vid_out = cv2.VideoCapture(self.source_out, backend)

        try:
            self.vid_in.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            self.vid_out.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        except Exception:
            pass

        if not self.vid_in.isOpened():
            print("Không mở được cam vào:", self.source_in)
        if not self.vid_out.isOpened():
            print("Không mở được cam ra:", self.source_out)

    def _get_frame(self, cap):
        if cap is None or not cap.isOpened():
            return None
        ret, frame = cap.read()
        if not ret:
            return None
        return frame

    def _snap_now(self, channel="in"):
        cap = self.vid_in if channel == "in" else self.vid_out
        if cap is None or not cap.isOpened():
            return None
        last = None
        for _ in range(2):
            ret, frame = cap.read()
            if ret:
                last = frame
        return last

    # ---------- image helpers ----------
    def _pil_from_bgr(self, frame):
        return PILImage.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))

    def _set_img_fit(self, label, pil_img):
        lw, lh = label.winfo_width(), label.winfo_height()
        if lw < 2 or lh < 2:
            self.window.after(50, lambda: self._set_img_fit(label, pil_img))
            return
        bg = PILImage.new("RGB", (lw, lh), "white")
        im = pil_img.copy()
        im.thumbnail((lw, lh), PILImage.Resampling.LANCZOS)
        x = (lw - im.width) // 2
        y = (lh - im.height) // 2
        bg.paste(im, (x, y))
        imgtk = ImageTk.PhotoImage(bg)
        label.configure(image=imgtk)
        label.image = imgtk

    def _reset_display(self):
        self.label_img_in.configure(image=self.placeholder_video); self.label_img_in.image = self.placeholder_video
        self.label_img_out.configure(image=self.placeholder_video); self.label_img_out.image = self.placeholder_video
        self.label_plate_in.configure(image=self.placeholder_plate); self.label_plate_in.image = self.placeholder_plate
        self.label_plate_out.configure(image=self.placeholder_plate); self.label_plate_out.image = self.placeholder_plate
        self.plate_in_var.set("---")
        self.plate_out_var.set("---")
        self.match_status_var.set("")
        self.duration_var.set("Thời gian gửi: --:--:--")
        self.fee_var.set("Phí gửi xe: -- VNĐ")

    def _schedule_reset(self):
        self.window.after(DISPLAY_RESET_MS, self._reset_display)

    # ---------- Arduino send ----------
    def _connected(self):
        c = self.master_serial_connection
        return bool(c and getattr(c, "is_open", False))

    def _send_master(self, text):
        text = (text or "").strip()
        if not text:
            return
        try:
            c = self.master_serial_connection
            if c and c.is_open:
                c.write((text + "\n").encode("utf-8"))
                print("[PC→MASTER]", text)
        except Exception as e:
            print("Gửi lệnh lỗi:", e)

    def _lcd_in(self, msg):
        msg = (msg or "")[:16]
        self._send_master(f"LCD1:{msg}")

    def _lcd_out(self, msg):
        msg = (msg or "")[:16]
        self._send_master(f"LCD2:{msg}")

    def _beep(self, n=1):
        n = max(1, min(5, int(n)))
        self._send_master(f"BEEP:{n}")

    # ---------- Serial read thread ----------
    def start_master_listener(self, com_port, baud=9600):
        com_port = (com_port or "").strip()
        if not SERIAL_OK:
            self.toast.show("Chưa cài pyserial.", 2000)
            return
        if not com_port or "Không tìm thấy" in com_port:
            self.toast.show("Chọn cổng COM hợp lệ.", 2000)
            return

        # stop old
        if self.listener_thread and self.listener_thread.is_alive():
            self.stop_thread.set()
            try: self.listener_thread.join(timeout=1)
            except Exception: pass
            try:
                if self.master_serial_connection and self.master_serial_connection.is_open:
                    self.master_serial_connection.close()
            except Exception:
                pass

        self.stop_thread.clear()
        self.listener_thread = threading.Thread(target=self._read_master_serial, args=(com_port, baud), daemon=True)
        self.listener_thread.start()

    def _read_master_serial(self, com_port, baud):
        print("Kết nối MASTER:", com_port)
        try:
            conn = serial.Serial(com_port, baud, timeout=1)
            self.master_serial_connection = conn
        except Exception as e:
            print("Mở COM lỗi:", e)
            self.window.after(0, lambda: self.toast.show("Mở COM lỗi.", 2200))
            return

        # đồng bộ vị trí motor = 1
        self.current_pos = 1
        self._send_master("SETPOS:1")
        self.window.after(0, lambda: self.toast.show(f"Đã kết nối {com_port} (SETPOS:1)", 1800))

        while not self.stop_thread.is_set():
            try:
                line = conn.readline().decode("utf-8", errors="ignore").strip()
                if not line:
                    continue

                if line == self.last_serial_line and (now_ms() - self.last_serial_time_ms) < SERIAL_SAME_LINE_COOLDOWN_MS:
                    continue
                self.last_serial_line = line
                self.last_serial_time_ms = now_ms()

                if line.startswith("RFID_IN:"):
                    uid = line.split("RFID_IN:", 1)[1].strip().upper()
                    if self._uid_ok("in", uid):
                        self.rfid_queue_in.put(uid)

                elif line.startswith("RFID_OUT:"):
                    uid = line.split("RFID_OUT:", 1)[1].strip().upper()
                    if self._uid_ok("out", uid):
                        self.rfid_queue_out.put(uid)

                elif "TOUCH_IN" in line:
                    self.touch_queue_in.put(True)

                elif "TOUCH_OUT" in line:
                    self.touch_queue_out.put(True)

                elif line.startswith("ARRIVED:"):
                    try:
                        n = int(line.split("ARRIVED:", 1)[1].strip())
                        self.arrived_queue.put(n)
                    except Exception:
                        pass

            except Exception as e:
                print("Lỗi luồng MASTER:", e)
                break

        try:
            if self.master_serial_connection and self.master_serial_connection.is_open:
                self.master_serial_connection.close()
        except Exception:
            pass
        print("Luồng MASTER đã dừng.")

    def _uid_ok(self, direction, uid):
        uid = (uid or "").strip().upper()
        if not uid:
            return False
        t = self.uid_last_time[direction].get(uid, 0)
        cd = UID_COOLDOWN_MS_IN if direction == "in" else UID_COOLDOWN_MS_OUT
        if now_ms() - t < cd:
            return False
        self.uid_last_time[direction][uid] = now_ms()
        return True

    # ---------- Reservation (WEB) helpers ----------
    def read_reservations(self):
        ensure_csv_reserved()
        rows = []
        try:
            with open(CSV_RESERVED, "r", newline="", encoding="utf-8") as f:
                rd = csv.DictReader(f)
                for r in rd:
                    rows.append(r)
        except Exception:
            pass
        return rows

    def _write_reservations(self, rows):
        ensure_csv_reserved()
        fn = ["id","ten","sdt","bien_so","spot","gio_du_kien","created_at","status","exit_time","final_fee"]
        with open(CSV_RESERVED, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fn)
            w.writeheader()
            for r in rows:
                w.writerow({k: r.get(k, "") for k in fn})

    def get_reserved_spots_active(self):
        # spot đang giữ chỗ nếu status=reserved hoặc status=in (đã vào nhưng chưa done)
        rows = self.read_reservations()
        locked = set()
        for r in rows:
            st = (r.get("status","") or "").strip()
            if st in ("reserved","in"):
                s = (r.get("spot","") or "").strip()
                if s:
                    locked.add(s)
        return locked

    def find_reservation_by_plate(self, plate):
        plate = normalize_plate(plate)
        rows = self.read_reservations()
        for r in rows:
            if normalize_plate(r.get("bien_so","")) == plate and (r.get("status","") == "reserved"):
                return r
        return None

    def mark_reservation_status(self, rid, status, exit_time="", final_fee=""):
        rows = self.read_reservations()
        for r in rows:
            if r.get("id","") == str(rid):
                r["status"] = status
                if exit_time:
                    r["exit_time"] = exit_time
                if final_fee != "":
                    r["final_fee"] = str(final_fee)
                break
        self._write_reservations(rows)

    def add_reservation(self, ten, sdt, bien_so, spot, gio_du_kien):
        ensure_csv_reserved()
        ten = (ten or "").strip()
        sdt = (sdt or "").strip()
        bien_so = normalize_plate(bien_so)
        spot = (spot or "").strip()
        try:
            gio = int(gio_du_kien)
            if gio < 1: gio = 1
        except Exception:
            gio = 1

        if not ten or not sdt or not bien_so or not spot:
            return False, "Thiếu thông tin."
        if spot not in SPOT_TO_TARGET:
            return False, "Ô đỗ không hợp lệ."
        if not is_valid_plate(bien_so):
            return False, "Biển số không hợp lệ."

        # spot phải đang trống trong bãi
        if self.parking_spots.get(spot) is not None:
            return False, "Ô đỗ đã có xe."

        # spot không được bị khóa bởi reservation khác
        locked = self.get_reserved_spots_active()
        if spot in locked:
            return False, "Ô đỗ đã được đặt trước."

        # biển số không được đặt trùng ở reservation đang hoạt động
        for r in self.read_reservations():
            if normalize_plate(r.get("bien_so","")) == bien_so and (r.get("status","") in ("reserved","in")):
                return False, "Biển số này đã có đặt chỗ."

        rid = str(int(time.time() * 1000))
        created = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(CSV_RESERVED, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow([rid, ten, sdt, bien_so, spot, str(gio), created, "reserved", "", ""])

        return True, "Đặt chỗ thành công!"

    def get_empty_spots_for_web(self):
        locked = self.get_reserved_spots_active()
        empty = []
        for sid, v in self.parking_spots.items():
            if v is None and sid not in locked:
                empty.append(sid)
        return empty

    # ---------- spots ----------
    def _find_empty_spot(self):
        # ✅ tránh cấp nhầm spot đã đặt trước
        locked = self.get_reserved_spots_active()
        for sid, v in self.parking_spots.items():
            if v is None and sid not in locked:
                return sid
        return None

    def _find_vehicle_by_plate(self, plate):
        plate = normalize_plate(plate)
        for sid, v in self.parking_spots.items():
            if v and normalize_plate(v.get("plate_text","")) == plate and v.get("status","occupied") == "occupied":
                return sid, v
        return None, None

    def _find_vehicle_by_uid(self, uid):
        uid = (uid or "").upper().strip()
        for sid, v in self.parking_spots.items():
            if v and (v.get("rfid_uid","").upper().strip() == uid) and v.get("status","occupied") == "occupied":
                return sid, v
        return None, None

    def update_spot_display(self):
        for sid, v in self.parking_spots.items():
            lb = self.spot_labels[sid]
            if v:
                lb.config(bg="#e74c3c", fg="white", text=f"{sid}\n{v.get('plate_text','')}")
            else:
                lb.config(bg="#2ecc71", fg="white", text=sid)

    # ---------- OCR NOW ----------
    def _ocr_plate_now(self, frame):
        if frame is None:
            return "unknown", None

        try:
            det = yolo_LP_detect(frame, size=640)
            lst = det.pandas().xyxy[0].values.tolist()
        except Exception as e:
            print("Detect lỗi:", e)
            return "unknown", None

        if not lst:
            return "unknown", None

        lst.sort(key=lambda x: float(x[4]) if len(x) > 4 else 0.0, reverse=True)
        x, y, x2, y2 = map(int, lst[0][:4])
        x = max(0, x); y = max(0, y); x2 = max(x + 1, x2); y2 = max(y + 1, y2)
        crop = frame[y:y2, x:x2]

        best = "unknown"
        for cc in range(2):
            for ct in range(2):
                try:
                    lp = helper.read_plate(yolo_license_plate, utils_rotate.deskew(crop, cc, ct))
                    lp = normalize_plate(lp)
                    if lp and lp != "unknown":
                        if is_valid_plate(lp):
                            return lp, crop
                        best = lp
                except Exception as e:
                    print("OCR lỗi:", e)

        if best != "unknown" and is_valid_plate(best):
            return best, crop
        return "unknown", crop

    # ---------- move + wait arrived ----------
    def _drain_arrived_queue(self):
        while True:
            try:
                self.arrived_queue.get_nowait()
            except queue.Empty:
                break

    def _move_to(self, target_num, channel="in"):
        target_num = int(target_num)
        if target_num < 1 or target_num > 4:
            return False

        if self.current_pos == target_num:
            return True

        self._drain_arrived_queue()

        if channel == "in":
            self._lcd_in(f"QUAY->{TARGET_TO_SPOT.get(target_num,'?')}")
        else:
            self._lcd_out(f"QUAY->{TARGET_TO_SPOT.get(target_num,'?')}")

        self._send_master(f"GO:{target_num}")

        t0 = time.time()
        while time.time() - t0 < ARRIVED_TIMEOUT_SEC:
            try:
                arrived = self.arrived_queue.get(timeout=0.25)
                if arrived == target_num:
                    self.current_pos = target_num
                    self._beep(1)  # đến vị trí -> beep 1
                    return True
            except queue.Empty:
                pass
        return False

    # ---------- ENTRY ----------
    def capture_in(self):
        self._handle_entry(rfid_uid="NO_CARD")

    def _handle_entry(self, rfid_uid):
        with self.entry_lock:
            if self.entry_busy:
                return
            self.entry_busy = True

        def worker():
            try:
                if not self._connected():
                    self.window.after(0, lambda: self.toast.show("Chưa kết nối Arduino MASTER.", 2200))
                    return

                self._lcd_in("DOC BIEN SO")
                frame = self._snap_now("in")
                if frame is None:
                    self._lcd_in("NO CAM IN")
                    self.window.after(0, lambda: self.toast.show("Không có tín hiệu camera vào.", 2000))
                    return

                plate, crop = self._ocr_plate_now(frame)
                if plate == "unknown":
                    self._lcd_in("OCR FAIL")
                    self.window.after(0, lambda: self.toast.show("Không nhận diện được biển số xe vào.", 2200))
                    return

                found_spot, _ = self._find_vehicle_by_plate(plate)
                if found_spot:
                    self._lcd_in("DA TON TAI")
                    self.window.after(0, lambda: self.toast.show(f"Xe {plate} đã ở {found_spot}.", 2200))
                    return

                # ✅ nếu có reservation đúng biển số -> ưu tiên spot đó
                res = self.find_reservation_by_plate(plate)
                spot_id = None
                charge_start = None
                reserved_id = None

                if res:
                    rs = (res.get("spot","") or "").strip()
                    if rs in self.parking_spots and self.parking_spots.get(rs) is None:
                        spot_id = rs
                        reserved_id = res.get("id","")
                        try:
                            charge_start = datetime.strptime(res.get("created_at",""), "%Y-%m-%d %H:%M:%S")
                        except Exception:
                            charge_start = datetime.now()
                        # mark in
                        self.mark_reservation_status(reserved_id, "in")
                    else:
                        # nếu spot đã không trống thì fallback như bình thường
                        spot_id = None

                if not spot_id:
                    spot_id = self._find_empty_spot()

                if not spot_id:
                    self._lcd_in("BAI DAY")
                    self.window.after(0, lambda: self.toast.show("Bãi đã đầy (hoặc ô trống đã bị đặt trước).", 2200))
                    return

                target = SPOT_TO_TARGET[spot_id]

                if target == 1 and self.current_pos == 1:
                    self._lcd_in("O A1 (SKIP)")
                else:
                    ok = self._move_to(target, channel="in")
                    if not ok:
                        self._lcd_in("ERR TIMEOUT")
                        self.window.after(0, lambda: self.toast.show("Quay vị trí thất bại (timeout).", 2400))
                        return

                snap_pil = self._pil_from_bgr(frame)
                crop_pil = self._pil_from_bgr(crop) if crop is not None else PILImage.new("RGB", (160, 80), "white")

                veh = {
                    "plate_text": plate,
                    "status": "occupied",
                    "rfid_uid": rfid_uid,
                    "entry_time": datetime.now(),
                    "vehicle_image": snap_pil,
                    "plate_image": crop_pil,

                    # ✅ charge_start_time: nếu có reservation -> tính từ created_at, nếu không -> tính từ entry_time (set ở exit)
                    "charge_start_time": charge_start,  # None nếu không đặt trước
                    "reserved_id": reserved_id
                }

                self.parking_spots[spot_id] = veh
                self.save_spots_to_csv()

                def ui_apply():
                    self._set_img_fit(self.label_img_in, snap_pil)
                    self._set_img_fit(self.label_plate_in, crop_pil)
                    self.plate_in_var.set(plate)
                    self.match_status_var.set("")
                    self.update_spot_display()
                    if reserved_id:
                        self.toast.show(f"Xe {plate} vào {spot_id} (đã đặt trước)", 2000)
                    else:
                        self.toast.show(f"Xe {plate} vào {spot_id}", 1800)
                    self._schedule_reset()

                self.window.after(0, ui_apply)

                self._lcd_in("OPEN IN")
                self._send_master("OPEN_IN")
                time.sleep(3.2)
                self._lcd_in("READY")

            finally:
                self.entry_busy = False

        threading.Thread(target=worker, daemon=True).start()

    # ---------- EXIT ----------
    def capture_out(self):
        self._handle_exit(rfid_uid="NO_CARD")

    def _handle_exit(self, rfid_uid):
        with self.exit_lock:
            if self.exit_busy:
                return
            self.exit_busy = True

        def worker():
            try:
                if not self._connected():
                    self.window.after(0, lambda: self.toast.show("Chưa kết nối Arduino MASTER.", 2200))
                    return

                self._lcd_out("DOC BIEN SO")
                frame = self._snap_now("out")
                if frame is None:
                    self._lcd_out("NO CAM OUT")
                    self.window.after(0, lambda: self.toast.show("Không có tín hiệu camera ra.", 2000))
                    return

                plate_out, crop_out = self._ocr_plate_now(frame)
                if plate_out == "unknown":
                    self._lcd_out("OCR FAIL")
                    self.window.after(0, lambda: self.toast.show("Không nhận diện được biển số xe ra.", 2200))
                    return

                spot_uid, veh_uid = (None, None)
                if rfid_uid and rfid_uid not in ("NO_CARD", "MANUAL"):
                    spot_uid, veh_uid = self._find_vehicle_by_uid(rfid_uid)

                spot_id, veh = (spot_uid, veh_uid) if veh_uid else self._find_vehicle_by_plate(plate_out)

                if not spot_id or not veh:
                    self._lcd_out("NOT FOUND")
                    self.window.after(0, lambda: self.toast.show(f"Không tìm thấy xe {plate_out} trong bãi.", 2400))
                    return

                plate_in = veh.get("plate_text","")
                if veh_uid and normalize_plate(plate_out) != normalize_plate(plate_in):
                    snap_pil = self._pil_from_bgr(frame)
                    crop_pil = self._pil_from_bgr(crop_out) if crop_out is not None else PILImage.new("RGB", (160, 80), "white")

                    def mismatch_ui():
                        self._set_img_fit(self.label_img_out, snap_pil)
                        self._set_img_fit(self.label_plate_out, crop_pil)
                        self.plate_out_var.set(plate_out)

                        if veh.get("vehicle_image"): self._set_img_fit(self.label_img_in, veh["vehicle_image"])
                        if veh.get("plate_image"): self._set_img_fit(self.label_plate_in, veh["plate_image"])
                        self.plate_in_var.set(plate_in)

                        self.match_status_var.set("❌ SAI BIỂN SỐ ❌")
                        self.toast.show("Sai biển số so với xe đã đăng ký!", 2600)
                        self._schedule_reset()

                    self.window.after(0, mismatch_ui)
                    self._lcd_out("SAI BIEN SO")
                    return

                target = SPOT_TO_TARGET[spot_id]
                ok = self._move_to(target, channel="out")
                if not ok:
                    self._lcd_out("ERR TIMEOUT")
                    self.window.after(0, lambda: self.toast.show("Quay vị trí xe ra thất bại (timeout).", 2400))
                    return

                snap_pil = self._pil_from_bgr(frame)
                crop_pil = self._pil_from_bgr(crop_out) if crop_out is not None else PILImage.new("RGB", (160, 80), "white")

                # ✅ tính phí:
                exit_time = datetime.now()
                entry_time = veh.get("entry_time", exit_time)

                # charge_start_time: nếu đặt trước -> created_at; nếu không -> entry_time
                charge_start = veh.get("charge_start_time")
                if not isinstance(charge_start, datetime):
                    charge_start = entry_time

                duration = exit_time - charge_start
                secs = int(duration.total_seconds())
                h, r = divmod(max(0, secs), 3600)
                m, s = divmod(r, 60)

                raw_fee = (duration.total_seconds() / 3600.0) * self.fee_per_hour
                fee = ceil_1000(raw_fee)

                def ui_apply():
                    self._set_img_fit(self.label_img_out, snap_pil)
                    self._set_img_fit(self.label_plate_out, crop_pil)
                    self.plate_out_var.set(plate_in)

                    if veh.get("vehicle_image"): self._set_img_fit(self.label_img_in, veh["vehicle_image"])
                    if veh.get("plate_image"): self._set_img_fit(self.label_plate_in, veh["plate_image"])
                    self.plate_in_var.set(plate_in)

                    self.match_status_var.set("✅ TRÙNG BIỂN SỐ ✅")
                    self.duration_var.set(f"Thời gian tính phí: {h:02d}:{m:02d}:{s:02d}")
                    self.fee_var.set(f"Phí gửi xe: {fmt_money(fee)} VNĐ")
                    self._schedule_reset()

                self.window.after(0, ui_apply)

                self._lcd_out("XAC NHAN OK")
                self._send_master(f"OUT,{plate_in}")
                time.sleep(0.06)

                self._lcd_out("OPEN OUT")
                self._send_master("OPEN_OUT")
                time.sleep(3.2)
                self._lcd_out("READY")

                # log
                self._log_exit_simple({
                    "ma_the": veh.get("rfid_uid", "N/A"),
                    "bien_so": plate_in,
                    "thoi_gian_vao": entry_time.strftime("%Y-%m-%d %H:%M:%S"),
                    "thoi_gian_ra": exit_time.strftime("%Y-%m-%d %H:%M:%S"),
                    "phi": str(fee)
                })

                # ✅ nếu là xe đặt trước -> mark reservation done + lưu final_fee
                rid = veh.get("reserved_id")
                if rid:
                    self.mark_reservation_status(
                        rid,
                        "done",
                        exit_time=exit_time.strftime("%Y-%m-%d %H:%M:%S"),
                        final_fee=str(fee)
                    )

                self.parking_spots[spot_id] = None
                self.save_spots_to_csv()
                self.window.after(0, self.update_spot_display)

                self.window.after(0, lambda: self.toast.show(f"Xe {plate_in} rời {spot_id} | Phí: {fmt_money(fee)}đ", 2600))

            finally:
                self.exit_busy = False

        threading.Thread(target=worker, daemon=True).start()

    # ---------- process events ----------
    def _process_events(self):
        try:
            uid = self.rfid_queue_in.get_nowait()
            self._handle_entry(uid)
        except queue.Empty:
            pass
        try:
            _ = self.touch_queue_in.get_nowait()
            self._handle_entry("NO_CARD")
        except queue.Empty:
            pass

        try:
            uid = self.rfid_queue_out.get_nowait()
            self._handle_exit(uid)
        except queue.Empty:
            pass
        try:
            _ = self.touch_queue_out.get_nowait()
            self._handle_exit("NO_CARD")
        except queue.Empty:
            pass

    # ---------- update loop ----------
    def update_loop(self):
        self.clock_var.set(vn_clock_str())
        self.pos_var.set(f"Vị trí hiện tại: {self.current_pos}")

        fi = self._get_frame(self.vid_in)
        if fi is not None:
            self.last_frame_in = fi
            self._set_img_fit(self.label_cam_in, self._pil_from_bgr(fi))

        fo = self._get_frame(self.vid_out)
        if fo is not None:
            self.last_frame_out = fo
            self._set_img_fit(self.label_cam_out, self._pil_from_bgr(fo))

        self._process_events()
        self.window.after(self.delay, self.update_loop)

    # ---------- CSV persistence ----------
    def save_spots_to_csv(self):
        ensure_csv_spots()
        try:
            with open(CSV_SPOTS, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["spot","status","plate","rfid_uid","entry_time"])
                for sid in SPOT_TO_TARGET.keys():
                    v = self.parking_spots.get(sid)
                    if v is None:
                        w.writerow([sid, "empty", "", "", ""])
                    else:
                        et = ""
                        if isinstance(v.get("entry_time"), datetime):
                            et = v["entry_time"].strftime("%Y-%m-%d %H:%M:%S")
                        w.writerow([
                            sid,
                            v.get("status","occupied"),
                            v.get("plate_text",""),
                            v.get("rfid_uid",""),
                            et
                        ])
        except Exception as e:
            print("Lưu vi_tri_do.csv lỗi:", e)

    def load_spots_from_csv(self):
        ensure_csv_spots()
        for sid in self.parking_spots:
            self.parking_spots[sid] = None
        try:
            with open(CSV_SPOTS, "r", newline="", encoding="utf-8") as f:
                rd = csv.DictReader(f)
                for r in rd:
                    sid = r.get("spot","")
                    st = r.get("status","empty")
                    if sid not in self.parking_spots:
                        continue
                    if st == "empty":
                        self.parking_spots[sid] = None
                    else:
                        plate = normalize_plate(r.get("plate",""))
                        uid = r.get("rfid_uid","")
                        et_str = r.get("entry_time","")
                        try:
                            et = datetime.strptime(et_str, "%Y-%m-%d %H:%M:%S") if et_str else datetime.now()
                        except Exception:
                            et = datetime.now()
                        self.parking_spots[sid] = {
                            "plate_text": plate,
                            "status": st,
                            "rfid_uid": uid,
                            "entry_time": et,
                            "vehicle_image": None,
                            "plate_image": None,
                            "charge_start_time": None,
                            "reserved_id": None
                        }
        except Exception as e:
            print("Load vi_tri_do.csv lỗi:", e)

    def _log_exit_simple(self, row):
        ensure_csv_log()
        try:
            with open(CSV_LOG, "a", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow([row["ma_the"], row["bien_so"], row["thoi_gian_vao"], row["thoi_gian_ra"], row["phi"]])
            self.window.after(0, self.load_log_from_csv)
        except Exception as e:
            print("Ghi log lỗi:", e)

    def load_log_from_csv(self):
        ensure_csv_log()
        for it in self.tree_log.get_children():
            self.tree_log.delete(it)
        try:
            with open(CSV_LOG, "r", newline="", encoding="utf-8") as f:
                rd = csv.DictReader(f)
                rows = list(rd)
            rows.reverse()
            for r in rows[:400]:
                fee = r.get("phi","0")
                try:
                    fee_txt = f"{fmt_money(int(fee))} VNĐ"
                except Exception:
                    fee_txt = fee
                self.tree_log.insert("", tk.END, values=(
                    r.get("ma_the",""),
                    r.get("bien_so",""),
                    r.get("thoi_gian_vao",""),
                    r.get("thoi_gian_ra",""),
                    fee_txt
                ))
        except Exception as e:
            print("Đọc CSV log lỗi:", e)

    # ---------- media source ----------
    def select_media_source(self, channel):
        fp = filedialog.askopenfilename(
            title="Chọn ảnh/video",
            filetypes=[("All","*.*"),("Video","*.mp4 *.avi"),("Image","*.jpg *.png")]
        )
        if not fp:
            return
        if channel == "in":
            self.source_in = fp
            self.settings["cam_in"] = fp
        else:
            self.source_out = fp
            self.settings["cam_out"] = fp
        write_settings(self.settings)
        self._open_cams()
        self.toast.show(f"Đã đổi nguồn Camera {channel.upper()}.", 1800)

    # ---------- Closing ----------
    def on_closing(self):
        self.stop_thread.set()
        try:
            if self.master_serial_connection and self.master_serial_connection.is_open:
                self.master_serial_connection.close()
        except Exception:
            pass
        try:
            if self.listener_thread:
                self.listener_thread.join(timeout=1)
        except Exception:
            pass
        try:
            if self.vid_in: self.vid_in.release()
            if self.vid_out: self.vid_out.release()
        except Exception:
            pass
        self.window.destroy()

    # ===================== WEB SERVER STARTER =====================
    def _start_web_thread(self):
        # import ở đây để đúng ý “đặt web dưới cùng trong code”
        from flask import Flask, request, redirect, render_template_string

        WEB_TEMPLATE = """
        <!doctype html>
        <html>
        <head>
          <meta charset="utf-8">
          <title>Đặt trước ô đỗ - Smart Parking</title>
          <style>
            body{font-family:Arial;background:#f4f7ff;margin:0}
            .top{background:#1f3c88;color:#fff;padding:14px 18px;display:flex;justify-content:space-between;align-items:center}
            .wrap{max-width:980px;margin:18px auto;padding:0 14px}
            .card{background:#fff;border-radius:14px;padding:16px;margin-bottom:14px;box-shadow:0 8px 24px rgba(0,0,0,.08)}
            .grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}
            label{font-weight:700;font-size:13px}
            input,select{width:100%;padding:10px;border-radius:10px;border:1px solid #cfd7ff}
            .btn{background:#1f3c88;color:#fff;border:0;border-radius:10px;padding:10px 14px;font-weight:700;cursor:pointer}
            table{width:100%;border-collapse:collapse}
            th,td{padding:10px;border-bottom:1px solid #eee;text-align:left}
            .badge{padding:4px 10px;border-radius:20px;font-weight:700;font-size:12px}
            .b-ok{background:#e8fff0;color:#18794e}
            .b-warn{background:#fff5e6;color:#9a6500}
            .b-bad{background:#ffecec;color:#a31313}
            .hint{color:#666;font-size:12px;margin-top:8px}
          </style>
        </head>
        <body>
          <div class="top">
            <div><b>Web đặt trước ô đỗ</b> – Smart Parking</div>
            <div style="opacity:.9;font-size:13px">Phí/giờ: <b>{{fee}}</b> VNĐ</div>
          </div>

          <div class="wrap">
            <div class="card">
              <h3>Đặt trước ô đỗ</h3>
              <form method="POST" action="/reserve">
                <div class="grid">
                  <div><label>Họ tên</label><input name="ten" required></div>
                  <div><label>Số điện thoại</label><input name="sdt" required></div>
                  <div><label>Biển số</label><input name="bien_so" required placeholder="VD: 80T-8888"></div>
                  <div>
                    <label>Chọn ô đỗ còn trống</label>
                    <select name="spot" required>
                      {% for s in empty_spots %}
                        <option value="{{s}}">{{s}}</option>
                      {% endfor %}
                    </select>
                  </div>
                  <div>
                    <label>Thời gian đỗ dự kiến (giờ)</label>
                    <input name="gio_du_kien" type="number" min="1" value="1" required>
                  </div>
                  <div>
                    <label>Ước tính phí (VNĐ)</label>
                    <input value="{{estimate}}" readonly>
                  </div>
                </div>
                <div style="margin-top:12px;display:flex;gap:10px;align-items:center">
                  <button class="btn" type="submit">Xác nhận đặt trước</button>
                  <span style="color:#444">{{msg}}</span>
                </div>
                <div class="hint">
                  Tính phí thực tế: từ lúc <b>xác nhận đặt</b> (created_at) → đến khi xe <b>rời bãi</b>.
                </div>
              </form>
            </div>

            <div class="card">
              <h3>Danh sách đặt trước</h3>
              <table>
                <thead>
                  <tr>
                    <th>ID</th><th>Tên</th><th>SĐT</th><th>Biển số</th><th>Ô</th><th>Giờ dự kiến</th>
                    <th>Created</th><th>Trạng thái</th><th>Exit</th><th>Phí cuối</th>
                  </tr>
                </thead>
                <tbody>
                  {% for r in reservations %}
                    <tr>
                      <td>{{r["id"]}}</td>
                      <td>{{r["ten"]}}</td>
                      <td>{{r["sdt"]}}</td>
                      <td><b>{{r["bien_so"]}}</b></td>
                      <td><b>{{r["spot"]}}</b></td>
                      <td>{{r["gio_du_kien"]}}</td>
                      <td>{{r["created_at"]}}</td>
                      <td>
                        {% if r["status"]=="reserved" %}
                          <span class="badge b-warn">Đã đặt</span>
                        {% elif r["status"]=="in" %}
                          <span class="badge b-ok">Xe đã vào</span>
                        {% elif r["status"]=="done" %}
                          <span class="badge b-ok">Hoàn tất</span>
                        {% else %}
                          <span class="badge b-bad">{{r["status"]}}</span>
                        {% endif %}
                      </td>
                      <td>{{r.get("exit_time","")}}</td>
                      <td>{{r.get("final_fee","")}}</td>
                    </tr>
                  {% endfor %}
                </tbody>
              </table>
            </div>
          </div>
        </body>
        </html>
        """

        app = Flask(__name__)
        app.secret_key = "smart-parking-secret"

        @app.get("/")
        def home():
            empty_spots = self.get_empty_spots_for_web()
            reservations = self.read_reservations()
            reservations = list(reversed(reservations))[:200]
            msg = request.args.get("msg","")

            # estimate: dùng fee_per_hour * 1 giờ mặc định (UI hiển thị)
            estimate = fmt_money(self.fee_per_hour * 1)

            return render_template_string(
                WEB_TEMPLATE,
                empty_spots=empty_spots,
                reservations=reservations,
                msg=msg,
                fee=fmt_money(self.fee_per_hour),
                estimate=estimate
            )

        @app.post("/reserve")
        def reserve():
            ten = request.form.get("ten","").strip()
            sdt = request.form.get("sdt","").strip()
            bien_so = request.form.get("bien_so","").strip().upper()
            spot = request.form.get("spot","").strip()
            gio = request.form.get("gio_du_kien","1").strip()

            ok, reason = self.add_reservation(ten, sdt, bien_so, spot, gio)
            if not ok:
                return redirect(f"/?msg={reason}")
            return redirect("/?msg=Đặt trước thành công!")

        def run_web():
            # chạy local
            app.run(host="127.0.0.1", port=5000, debug=False, use_reloader=False)

        self.web_thread = threading.Thread(target=run_web, daemon=True)
        self.web_thread.start()


# ===================== RUN =====================
if __name__ == "__main__":
    ensure_csv_spots()
    ensure_csv_log()
    ensure_csv_settings()
    ensure_csv_reserved()

    root = tk.Tk()
    root.state("zoomed")
    app = ParkingApp(root, "Hệ thống Quản lý Bãi giữ xe (FULL + Web đặt trước)")
    root.mainloop()
