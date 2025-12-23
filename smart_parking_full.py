# -*- coding: utf-8 -*-
"""
SMART PARKING - ALL IN ONE (Tkinter + Serial + OCR + Web Reservation + History + Admin Settings)

✅ Arduino MASTER protocol (as per your MASTER_FIXED.ino):
  - From MASTER -> PC:
      RFID_IN:<uid>
      RFID_OUT:<uid>
      TOUCH_IN
      TOUCH_OUT
      STATION_PASS:<pos>
      ARRIVED:<pos>
  - From PC -> MASTER:
      "1|2|3|4"   (move to position)
      "GO:n"      (optional)
      "OPEN_IN" / "OPEN_OUT"      (servo open, auto close handled by Arduino after 3s)
      "BEEP:n"                    (beep n times)
      "LCD1:text" / "LCD2:text"   (write LCD lines)
      "OUT,<plate>"               (LCD OUT UI helper for gate out)

✅ Logic you requested:
  - Default motor position = 1 (Python keeps master_position=1 and updates on ARRIVED).
  - When RFID_IN or TOUCH_IN -> capture frame NOW -> OCR NOW -> if OK:
        choose EMPTY spot (or reserved spot if plate has reservation) -> move if needed -> BEEP 1 -> OPEN_IN (Arduino beeps 2)
  - When RFID_OUT or TOUCH_OUT -> capture frame NOW -> OCR NOW -> if OK:
        find vehicle spot -> move if needed -> BEEP 1 -> OPEN_OUT (Arduino beeps 2)

✅ Web (Flask, in same file, bottom):
  - /           : Reservation dashboard (professional UI)
  - /reserve    : POST create reservation
  - /history    : View vehicle history (CSV log)
  - /admin      : Login
  - /admin/settings : Change fee/hour, COM, cam sources
  - Reservation fee rule per your request:
        If vehicle had reservation -> fee duration starts from reservation created_at to exit_time.
        Else -> fee duration starts from actual entry_time to exit_time.
"""

import os, csv, math, time, threading, queue
from datetime import datetime

import tkinter as tk
from tkinter import ttk, filedialog
from PIL import Image, ImageTk, Image as PILImage

import cv2

# Serial
import serial
try:
    import serial.tools.list_ports
except Exception:
    serial = None

# Web
from flask import Flask, request, redirect, session, render_template_string, Response

# ==== MOCK YOLO nếu thiếu module cục bộ ====
try:
    import torch
    import function.utils_rotate as utils_rotate
    import function.helper as helper
    TORCH_OK = True
except Exception:
    TORCH_OK = False
    print("Không có module function/ hoặc torch, dùng mock YOLO-OCR để test.")
    class _MockValues:
        def __init__(self): self._vals = [[100,100,300,200,0.95,0]]
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

# ==== Load YOLO (nếu có) ====
yolo_LP_detect = None
yolo_license_plate = None
if TORCH_OK:
    try:
        # giữ đúng như bạn (source='local' nếu bạn có thư mục yolov5 local)
        yolo_LP_detect = torch.hub.load('yolov5', 'custom', path='model/LP_detector_nano_61.pt',
                                        force_reload=False, source='local')
        yolo_license_plate = torch.hub.load('yolov5', 'custom', path='model/LP_ocr_nano_62.pt',
                                            force_reload=False, source='local')
        yolo_license_plate.conf = 0.60
    except Exception as e:
        print(f"Không thể tải YOLO, dùng mock. Lỗi: {e}")
        yolo_LP_detect = MockYoloModel()
        yolo_license_plate = MockYoloModel()
else:
    yolo_LP_detect = MockYoloModel()
    yolo_license_plate = MockYoloModel()

# ===================== CONFIG / CONSTANTS =====================
UID_COOLDOWN_MS_IN  = 2500
UID_COOLDOWN_MS_OUT = 2500
SERIAL_SAME_LINE_COOLDOWN_MS = 700

DISPLAY_RESET_MS = 8000

# Chờ Arduino đến vị trí
ARRIVED_TIMEOUT_SEC = 28

CSV_RESERVED = "dat_cho_truoc.csv"
CSV_LOG      = "lich_su_xe.csv"
CSV_SPOTS    = "vi_tri_do.csv"
CSV_SETTINGS = "settings.csv"

DEFAULT_FEE_PER_HOUR = 5000
ADMIN_USER = "Admin"
ADMIN_PASS = "123"

# Map spot -> target position (A1..A4 -> 1..4)
SPOT_TO_TARGET = {'A1':1,'A2':2,'A3':3,'A4':4}
TARGET_TO_SPOT = {v:k for k,v in SPOT_TO_TARGET.items()}
SPOT_ORDER = list(SPOT_TO_TARGET.keys())

def now_ms():
    return int(time.time()*1000)

def vn_clock_str():
    dow = ["Thứ Hai","Thứ Ba","Thứ Tư","Thứ Năm","Thứ Sáu","Thứ Bảy","Chủ Nhật"]
    d = datetime.now()
    return f"{dow[d.weekday()]}, {d.strftime('%d/%m/%Y | %H:%M:%S')}"

def fmt_money(v):
    try:
        v = int(v)
    except:
        v = 0
    return f"{v:,}".replace(",", ".")

def safe_upper_plate(s):
    s = (s or "").strip().upper()
    # normalize common separators
    s = s.replace(" ", "").replace("_", "-")
    return s

# ===================== TOAST (AUTO DISMISS) =====================
class Toast:
    def __init__(self, root):
        self.root = root
        self.win = None
        self._hide_job = None

    def show(self, msg, ms=2000):
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

# ===================== CSV HELPERS =====================
RES_FIELDS = ["id","ten","sdt","bien_so","spot","gio_du_kien","so_tien_nap","created_at","status","arrival_time","exit_time","fee_total","paid_from_prepaid","con_thieu"]
LOG_FIELDS = ["ma_the","bien_so","thoi_gian_vao","thoi_gian_ra","phi","paid_from_prepaid","con_thieu"]

def ensure_csv_reserved():
    if not os.path.isfile(CSV_RESERVED):
        with open(CSV_RESERVED, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(RES_FIELDS)

def ensure_csv_spots():
    if not os.path.isfile(CSV_SPOTS):
        with open(CSV_SPOTS, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["spot","status","plate","rfid_uid","entry_time","prepaid_balance","reserve_id","reserved_at"])
            for s in SPOT_TO_TARGET.keys():
                w.writerow([s,"empty","","","", "0","",""])

def ensure_csv_log():
    if not os.path.isfile(CSV_LOG):
        with open(CSV_LOG, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(LOG_FIELDS)

def ensure_csv_settings():
    if not os.path.isfile(CSV_SETTINGS):
        with open(CSV_SETTINGS, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["key","value"])
            w.writerow(["fee_per_hour", str(DEFAULT_FEE_PER_HOUR)])
            w.writerow(["cam_in", "0"])
            w.writerow(["cam_out","1"])
            w.writerow(["com_port",""])

def read_settings():
    ensure_csv_settings()
    d = {"fee_per_hour": str(DEFAULT_FEE_PER_HOUR), "cam_in":"0", "cam_out":"1", "com_port":""}
    try:
        with open(CSV_SETTINGS, "r", newline="", encoding="utf-8") as f:
            rd = csv.reader(f)
            next(rd, None)
            for row in rd:
                if len(row) >= 2:
                    k, v = row[0], row[1]
                    d[k] = v
    except Exception:
        pass
    try:
        d["fee_per_hour"] = int(d.get("fee_per_hour", DEFAULT_FEE_PER_HOUR))
    except:
        d["fee_per_hour"] = DEFAULT_FEE_PER_HOUR
    return d

def write_settings(d):
    rows = [["key","value"]]
    for k,v in d.items():
        rows.append([k, str(v)])
    with open(CSV_SETTINGS, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerows(rows)

# ===================== WEB (Flask) =====================
WEB_BASE = r"""
<!doctype html>
<html lang="vi">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{{title}}</title>
  <style>
    :root{
      --bg:#0b1220;
      --card:#0f1a2e;
      --card2:#101f3a;
      --text:#eaf0ff;
      --muted:#a9b7d6;
      --line:rgba(255,255,255,.08);
      --brand1:#1c4fd7;
      --brand2:#23c2ff;
      --ok:#21c55d;
      --warn:#f59e0b;
      --bad:#ef4444;
    }
    *{box-sizing:border-box}
    body{margin:0;font-family:Inter,Segoe UI,Arial;background:linear-gradient(180deg,#071024 0%, #050a14 100%);color:var(--text)}
    .top{
      position:sticky;top:0;z-index:20;
      background:linear-gradient(90deg,#0b1f4d 0%, #061b3f 55%, #051634 100%);
      border-bottom:1px solid var(--line);
      padding:14px 18px;
      display:flex;align-items:center;justify-content:space-between;
    }
    .brand{display:flex;gap:12px;align-items:center}
    .dot{width:10px;height:10px;border-radius:999px;background:var(--brand2);box-shadow:0 0 0 6px rgba(35,194,255,.12)}
    .brand h1{margin:0;font-size:18px;letter-spacing:.3px}
    .brand .sub{font-size:12px;color:var(--muted);margin-top:2px}
    .nav{display:flex;gap:10px;align-items:center}
    .pill{
      padding:10px 14px;border-radius:12px;text-decoration:none;
      color:var(--text);border:1px solid var(--line);
      background:rgba(255,255,255,.04);
      font-weight:700;font-size:14px;
    }
    .pill.active{background:linear-gradient(90deg,rgba(28,79,215,.35),rgba(35,194,255,.18));border-color:rgba(35,194,255,.35)}
    .wrap{max-width:1150px;margin:18px auto;padding:0 14px}
    .grid{display:grid;grid-template-columns:1.2fr .8fr;gap:14px}
    @media(max-width:980px){.grid{grid-template-columns:1fr}}
    .card{
      background:linear-gradient(180deg,rgba(255,255,255,.05),rgba(255,255,255,.03));
      border:1px solid var(--line);
      border-radius:18px;
      padding:16px;
      box-shadow:0 18px 40px rgba(0,0,0,.35);
    }
    .card h2{margin:0 0 8px;font-size:18px}
    .muted{color:var(--muted);font-size:13px}
    .formgrid{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:12px}
    @media(max-width:720px){.formgrid{grid-template-columns:1fr}}
    label{display:block;font-weight:800;font-size:12px;margin-bottom:6px;color:#dbe6ff}
    input,select{
      width:100%;
      padding:11px 12px;border-radius:12px;
      border:1px solid rgba(255,255,255,.14);
      background:rgba(7,12,24,.55);
      color:var(--text);
      outline:none;
    }
    input:focus,select:focus{border-color:rgba(35,194,255,.65);box-shadow:0 0 0 4px rgba(35,194,255,.12)}
    .btn{
      border:0;border-radius:12px;
      padding:11px 14px;
      font-weight:900;
      cursor:pointer;
      color:#071024;
      background:linear-gradient(90deg,var(--brand2),#7dd3fc);
    }
    .btn:hover{filter:brightness(1.05)}
    table{width:100%;border-collapse:collapse;margin-top:10px}
    th,td{padding:10px;border-bottom:1px solid var(--line);text-align:left;font-size:13px}
    th{color:#dbe6ff;font-weight:900}
    .scroll{max-height:360px;overflow:auto;border:1px solid var(--line);border-radius:14px}
    .badge{
      padding:4px 10px;border-radius:999px;font-weight:900;font-size:12px;display:inline-block
    }
    .b-ok{background:rgba(33,197,93,.18);color:#b8ffcf;border:1px solid rgba(33,197,93,.35)}
    .b-warn{background:rgba(245,158,11,.18);color:#ffe7b0;border:1px solid rgba(245,158,11,.35)}
    .b-bad{background:rgba(239,68,68,.18);color:#ffb8b8;border:1px solid rgba(239,68,68,.35)}
    .kpi{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-top:12px}
    .box{background:rgba(255,255,255,.04);border:1px solid var(--line);border-radius:16px;padding:12px}
    .big{font-size:26px;font-weight:1000;margin-top:6px}
    .hint{margin-top:10px;font-size:12px;color:var(--muted);line-height:1.5}
    .mono{font-family:ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace}
    a{color:#a5d8ff}
    .footer{margin:18px 0;color:rgba(255,255,255,.45);font-size:12px;text-align:center}
  </style>
</head>
<body>
  <div class="top">
    <div class="brand">
      <div class="dot"></div>
      <div>
        <h1>SMART PARKING</h1>
        <div class="sub">{{subtitle}}</div>
      </div>
    </div>
    <div class="nav">
      <a class="pill {{'active' if active=='reserve' else ''}}" href="/">Đặt trước</a>
      <a class="pill {{'active' if active=='history' else ''}}" href="/history">Lịch sử xe</a>
      <a class="pill" href="/admin">Admin</a>
    </div>
  </div>

  <div class="wrap">
    {{body | safe}}
    <div class="footer">Smart Parking • Local Web • http://127.0.0.1:5000</div>
  </div>
</body>
</html>
"""

WEB_BODY_RESERVE = r"""
<div class="grid">
  <div class="card">
    <h2>Đặt trước ô đỗ</h2>
    <div class="muted">Phí/giờ hiện tại: <b>{{fee}}</b> VNĐ</div>

    <form method="POST" action="/reserve" style="margin-top:12px">
      <div class="formgrid">
        <div>
          <label>Họ tên</label>
          <input name="ten" required>
        </div>
        <div>
          <label>Số điện thoại</label>
          <input name="sdt" required>
        </div>
        <div>
          <label>Biển số</label>
          <input name="bien_so" required placeholder="VD: 80T-8888">
        </div>
        <div>
          <label>Ô đỗ (chỉ chọn ô còn trống)</label>
          <select name="spot" required>
            {% for s in selectable_spots %}
              <option value="{{s}}">{{s}} — Trống ✅</option>
            {% endfor %}
          </select>
        </div>
        <div>
          <label>Thời gian đỗ dự kiến (giờ)</label>
          <input name="gio_du_kien" type="number" min="1" value="1" required>
        </div>
        <div>
          <label>Số tiền nạp trước (VNĐ)</label>
          <input name="so_tien_nap" type="number" min="0" value="0" required>
        </div>
      </div>

      <div style="margin-top:12px;display:flex;gap:10px;align-items:center;flex-wrap:wrap">
        <button class="btn" type="submit">Xác nhận đặt trước</button>
        <span class="muted">{{msg}}</span>
      </div>

      <div class="hint">
        <b>Cách tính phí đặt trước:</b> nếu xe vào bằng đúng biển số đã đặt,
        hệ thống tính từ thời điểm <b>xác nhận đặt</b> (created_at) đến khi xe <b>rời bãi</b> (exit_time).
        Nếu không có đặt trước, hệ thống tính từ <b>thời điểm xe vào</b>.
      </div>
    </form>
  </div>

  <div class="card">
    <h2>Trạng thái bãi xe</h2>
    <div class="muted">Ô trống / đã đặt / có xe</div>

    <div class="kpi">
      <div class="box">
        <div class="muted">Ô trống</div>
        <div class="big">{{kpi_empty}}</div>
      </div>
      <div class="box">
        <div class="muted">Đã đặt</div>
        <div class="big">{{kpi_reserved}}</div>
      </div>
      <div class="box">
        <div class="muted">Có xe</div>
        <div class="big">{{kpi_occupied}}</div>
      </div>
    </div>

    <div class="scroll" style="margin-top:12px">
      <table>
        <thead>
          <tr><th>Ô</th><th>Trạng thái</th><th>Biển số</th></tr>
        </thead>
        <tbody>
          {% for s in spots %}
            <tr>
              <td class="mono"><b>{{s.spot}}</b></td>
              <td>
                {% if s.status=='empty' %}
                  <span class="badge b-ok">empty</span>
                {% elif s.status=='reserved' %}
                  <span class="badge b-warn">reserved</span>
                {% else %}
                  <span class="badge b-bad">occupied</span>
                {% endif %}
              </td>
              <td><b>{{s.plate}}</b></td>
            </tr>
          {% endfor %}
        </tbody>
      </table>
    </div>

    <div class="hint">
      Chọn ô đỗ chỉ hiển thị ô <b>trống</b>. Ô <b>reserved</b> sẽ tự động được dùng khi xe vào đúng biển số đã đặt.
    </div>
  </div>
</div>

<div class="card" style="margin-top:14px">
  <h2>Danh sách đặt trước</h2>
  <div class="muted">Trạng thái: reserved (chưa đến), in (đã đến), done (đã rời), cancel.</div>

  <div class="scroll" style="margin-top:10px">
    <table>
      <thead>
        <tr>
          <th>ID</th><th>Tên</th><th>SĐT</th><th>Biển số</th><th>Ô</th><th>Giờ DK</th><th>Nạp</th>
          <th>Created</th><th>Status</th><th>Arrival</th><th>Exit</th><th>Phí</th>
        </tr>
      </thead>
      <tbody>
        {% for r in reservations %}
          <tr>
            <td class="mono">{{r.id}}</td>
            <td>{{r.ten}}</td>
            <td class="mono">{{r.sdt}}</td>
            <td><b>{{r.bien_so}}</b></td>
            <td class="mono"><b>{{r.spot}}</b></td>
            <td class="mono">{{r.gio_du_kien}}</td>
            <td class="mono">{{r.so_tien_nap}}</td>
            <td class="mono">{{r.created_at}}</td>
            <td>
              {% if r.status=='reserved' %}
                <span class="badge b-warn">reserved</span>
              {% elif r.status=='in' %}
                <span class="badge b-ok">in</span>
              {% elif r.status=='done' %}
                <span class="badge b-ok">done</span>
              {% else %}
                <span class="badge b-bad">{{r.status}}</span>
              {% endif %}
            </td>
            <td class="mono">{{r.arrival_time}}</td>
            <td class="mono">{{r.exit_time}}</td>
            <td class="mono"><b>{{r.fee_total}}</b></td>
          </tr>
        {% endfor %}
      </tbody>
    </table>
  </div>
</div>
"""

WEB_BODY_HISTORY = r"""
<div class="card">
  <h2>Lịch sử xe</h2>
  <div class="muted">Dữ liệu lấy từ <span class="mono">lich_su_xe.csv</span></div>

  <div class="scroll" style="margin-top:10px">
    <table>
      <thead>
        <tr>
          <th>Mã thẻ</th><th>Biển số</th><th>Thời gian vào</th><th>Thời gian ra</th>
          <th>Phí</th><th>Trừ từ nạp</th><th>Còn thiếu</th>
        </tr>
      </thead>
      <tbody>
        {% for r in logs %}
          <tr>
            <td class="mono">{{r.ma_the}}</td>
            <td><b>{{r.bien_so}}</b></td>
            <td class="mono">{{r.thoi_gian_vao}}</td>
            <td class="mono">{{r.thoi_gian_ra}}</td>
            <td class="mono"><b>{{r.phi}}</b></td>
            <td class="mono">{{r.paid_from_prepaid}}</td>
            <td class="mono">{{r.con_thieu}}</td>
          </tr>
        {% endfor %}
      </tbody>
    </table>
  </div>
</div>
"""

WEB_ADMIN_LOGIN = r"""
<div class="card" style="max-width:520px;margin:0 auto">
  <h2>Admin Login</h2>
  <div class="muted">Tài khoản mặc định: <span class="mono">Admin / 123</span></div>
  <form method="POST" style="margin-top:12px">
    <div class="formgrid" style="grid-template-columns:1fr">
      <div><label>Tài khoản</label><input name="u" required></div>
      <div><label>Mật khẩu</label><input name="p" type="password" required></div>
    </div>
    <div style="margin-top:12px;display:flex;gap:10px;align-items:center">
      <button class="btn" type="submit">Đăng nhập</button>
      <span class="muted">{{msg}}</span>
    </div>
  </form>
</div>
"""

WEB_ADMIN_SETTINGS = r"""
<div class="card">
  <h2>Admin Settings</h2>
  <div class="muted">Chỉnh phí/giờ, camera, COM — lưu vào <span class="mono">settings.csv</span></div>

  <form method="POST" action="/admin/settings" style="margin-top:12px">
    <div class="formgrid">
      <div>
        <label>Phí gửi xe (VNĐ/giờ)</label>
        <input name="fee_per_hour" type="number" min="0" value="{{fee_per_hour}}" required>
      </div>
      <div>
        <label>COM Arduino MASTER</label>
        <input name="com_port" value="{{com_port}}" placeholder="VD: COM5">
      </div>
      <div>
        <label>Camera vào</label>
        <input name="cam_in" value="{{cam_in}}" placeholder="VD: 0 hoặc đường dẫn video/ảnh">
      </div>
      <div>
        <label>Camera ra</label>
        <input name="cam_out" value="{{cam_out}}" placeholder="VD: 1 hoặc đường dẫn video/ảnh">
      </div>
    </div>

    <div style="margin-top:12px;display:flex;gap:10px;align-items:center;flex-wrap:wrap">
      <button class="btn" type="submit">Lưu</button>
      <a class="pill" style="padding:10px 14px" href="/admin/logout">Đăng xuất</a>
      <span class="muted">{{msg}}</span>
    </div>

    <div class="hint">
      Gợi ý Camera: nhập <b>0</b>, <b>1</b>... để dùng webcam theo index.
      Nếu nhập đường dẫn <b>.mp4</b> sẽ chạy video; nhập <b>.jpg</b> sẽ hiển thị ảnh tĩnh (demo OCR).
    </div>
  </form>
</div>
"""

def create_web_server(parking_app_ref):
    app = Flask(__name__)
    app.secret_key = "smart-parking-secret"

    def render_page(body_html, title="Smart Parking", subtitle="Đặt trước & Lịch sử xe", active="reserve", **ctx):
        html = render_template_string(WEB_BASE, title=title, subtitle=subtitle, active=active,
                                      body=render_template_string(body_html, **ctx))
        return Response(html, mimetype="text/html")

    @app.get("/")
    def home():
        st = read_settings()
        fee = int(st.get("fee_per_hour", DEFAULT_FEE_PER_HOUR))

        spots = parking_app_ref.get_spots_status_for_web()
        selectable_spots = [s["spot"] for s in spots if s["status"] == "empty"]
        reservations = parking_app_ref.read_reservations()

        kpi_empty = sum(1 for s in spots if s["status"] == "empty")
        kpi_reserved = sum(1 for s in spots if s["status"] == "reserved")
        kpi_occupied = sum(1 for s in spots if s["status"] == "occupied")

        msg = request.args.get("msg","")
        return render_page(
            WEB_BODY_RESERVE,
            active="reserve",
            fee=fmt_money(fee),
            msg=msg,
            selectable_spots=selectable_spots if selectable_spots else ["(Không còn ô trống)"],
            spots=spots,
            reservations=reservations,
            kpi_empty=kpi_empty, kpi_reserved=kpi_reserved, kpi_occupied=kpi_occupied
        )

    @app.post("/reserve")
    def reserve():
        ensure_csv_reserved()
        ten = (request.form.get("ten","") or "").strip()
        sdt = (request.form.get("sdt","") or "").strip()
        bien_so = safe_upper_plate(request.form.get("bien_so",""))
        spot = (request.form.get("spot","") or "").strip()
        gio = (request.form.get("gio_du_kien","1") or "1").strip()
        nap = (request.form.get("so_tien_nap","0") or "0").strip()

        try: gio_i = max(1, int(gio))
        except: gio_i = 1
        try: nap_i = max(0, int(nap))
        except: nap_i = 0

        ok, reason = parking_app_ref.add_reservation(ten, sdt, bien_so, spot, gio_i, nap_i)
        if not ok:
            return redirect(f"/?msg={reason}")
        return redirect("/?msg=Đặt trước thành công!")

    @app.get("/history")
    def history():
        logs = parking_app_ref.read_vehicle_logs()
        return render_page(WEB_BODY_HISTORY, active="history", logs=logs)

    @app.route("/admin", methods=["GET","POST"])
    def admin_login():
        msg = ""
        if request.method == "POST":
            u = request.form.get("u","")
            p = request.form.get("p","")
            if u == ADMIN_USER and p == ADMIN_PASS:
                session["admin"] = True
                return redirect("/admin/settings")
            msg = "Sai tài khoản hoặc mật khẩu."
        return render_page(WEB_ADMIN_LOGIN, active="", msg=msg, subtitle="Admin", title="Admin Login")

    @app.get("/admin/logout")
    def admin_logout():
        session.clear()
        return redirect("/")

    @app.route("/admin/settings", methods=["GET","POST"])
    def admin_settings():
        if not session.get("admin"):
            return redirect("/admin")

        st = read_settings()
        msg = ""
        if request.method == "POST":
            fee_per_hour = (request.form.get("fee_per_hour","5000") or "5000").strip()
            com_port = (request.form.get("com_port","") or "").strip()
            cam_in = (request.form.get("cam_in","0") or "0").strip()
            cam_out = (request.form.get("cam_out","1") or "1").strip()

            try: fee_per_hour_i = max(0, int(fee_per_hour))
            except: fee_per_hour_i = DEFAULT_FEE_PER_HOUR

            st["fee_per_hour"] = fee_per_hour_i
            st["com_port"] = com_port
            st["cam_in"] = cam_in
            st["cam_out"] = cam_out
            write_settings(st)
            msg = "Đã lưu."

            # báo Python reload
            parking_app_ref.on_settings_changed(st)

        st = read_settings()
        return render_page(
            WEB_ADMIN_SETTINGS,
            active="",
            subtitle="Admin Settings",
            title="Admin Settings",
            fee_per_hour=st["fee_per_hour"],
            com_port=st.get("com_port",""),
            cam_in=st.get("cam_in","0"),
            cam_out=st.get("cam_out","1"),
            msg=msg
        )

    return app

# ===================== MAIN APP =====================
class ParkingApp:
    def __init__(self, window, title):
        self.window = window
        self.window.title(title)
        self.window.configure(bg='#e6f0ff')

        style = ttk.Style(self.window)
        try: style.theme_use('clam')
        except: pass
        style.configure("TLabelFrame", borderwidth=0, background='#e6f0ff')
        style.configure("TLabelFrame.Label", foreground="blue", background='#e6f0ff', font=("Helvetica", 11, "bold"))
        style.configure("TButton", font=("Helvetica", 10))

        self.toast = Toast(self.window)

        # dữ liệu bãi (RAM)
        # mỗi spot: None hoặc dict {plate_text, entry_time, status, rfid_uid, prepaid_balance, reserve_id, reserved_at...}
        self.parking_spots = {s: None for s in SPOT_TO_TARGET.keys()}
        self.spot_labels = {}

        # settings
        self.settings = read_settings()
        self.fee_per_hour = int(self.settings.get("fee_per_hour", DEFAULT_FEE_PER_HOUR))

        # camera sources
        self.source_in = self._parse_cam_source(self.settings.get("cam_in","0"))
        self.source_out = self._parse_cam_source(self.settings.get("cam_out","1"))
        self.vid_in = None
        self.vid_out = None
        self.static_frame_in = None
        self.static_frame_out = None
        self.last_frame_in = None
        self.last_frame_out = None
        self._cam_fail_in = 0
        self._cam_fail_out = 0

        # serial MASTER
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

        # keep motor position (default pos 1)
        self.master_position = 1

        # locks
        self.entry_lock = threading.Lock()
        self.exit_lock  = threading.Lock()
        self.entry_busy = False
        self.exit_busy  = False

        # UI init
        self.init_capture_devices()
        self.create_menu()
        self.create_widgets()

        # load from CSV
        ensure_csv_spots()
        ensure_csv_reserved()
        ensure_csv_log()
        self.load_spots_from_csv()
        self.apply_reservations_to_spots()
        self.update_spot_display()
        self.load_reserved_list_from_csv()
        self.load_log_from_csv()

        # start web server
        self.web_app = create_web_server(self)
        self.web_thread = threading.Thread(
            target=lambda: self.web_app.run(host="127.0.0.1", port=5000, debug=False, use_reloader=False),
            daemon=True
        )
        self.web_thread.start()

        # auto connect COM if settings has com_port
        if self.settings.get("com_port",""):
            self.window.after(600, lambda: self.start_master_listener(self.settings.get("com_port",""), 9600))

        # loop
        self.delay = 20
        self.update_loop()
        self.window.protocol("WM_DELETE_WINDOW", self.on_closing)

    # ---------- settings change (from web) ----------
    def on_settings_changed(self, st):
        # cập nhật fee/cam/com
        self.settings = read_settings()
        self.fee_per_hour = int(self.settings.get("fee_per_hour", DEFAULT_FEE_PER_HOUR))
        self.toast.show("Đã áp dụng settings mới từ Web.", 1800)

        # reload camera
        self.source_in = self._parse_cam_source(self.settings.get("cam_in","0"))
        self.source_out = self._parse_cam_source(self.settings.get("cam_out","1"))
        self._reopen_cams()

        # auto reconnect COM if changed
        com = self.settings.get("com_port","")
        if com:
            self.start_master_listener(com, 9600)

    def _parse_cam_source(self, s):
        s = str(s).strip()
        if s.isdigit():
            return int(s)
        return s  # file path

    # ---------- UI ----------
    def create_menu(self):
        menubar = tk.Menu(self.window); self.window.config(menu=menubar)
        m_file = tk.Menu(menubar, tearoff=0); menubar.add_cascade(label="Tệp", menu=m_file)
        m_file.add_command(label="Chọn nguồn tạm thời cho Camera Vào...", command=lambda: self.select_media_source('in'))
        m_file.add_command(label="Chọn nguồn tạm thời cho Camera Ra...",  command=lambda: self.select_media_source('out'))
        m_file.add_separator(); m_file.add_command(label="Thoát", command=self.on_closing)
        m_opt = tk.Menu(menubar, tearoff=0); menubar.add_cascade(label="Tùy chọn", menu=m_opt)
        m_opt.add_command(label="Cài đặt", command=self.open_settings_window)
        m_web = tk.Menu(menubar, tearoff=0); menubar.add_cascade(label="Web", menu=m_web)
        m_web.add_command(label="Mở Web đặt chỗ: http://127.0.0.1:5000",
                          command=lambda: self.toast.show("Mở trình duyệt: http://127.0.0.1:5000", 2000))

    def create_widgets(self):
        main = tk.Frame(self.window, bg='#e6f0ff'); main.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        left = tk.Frame(main, bg='#e6f0ff'); left.pack(side=tk.LEFT, fill=tk.Y, expand=False, padx=(0,5))
        right = tk.Frame(main, bg='#e6f0ff'); right.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True, padx=(5,0))
        self.create_left_pane_widgets(left)
        self.create_right_pane_widgets(right)

    def create_left_pane_widgets(self, parent):
        FIX_W, FIX_H = 500, 375
        parent.columnconfigure(0, minsize=FIX_W); parent.columnconfigure(1, minsize=FIX_W)
        parent.rowconfigure(0, minsize=FIX_H);    parent.rowconfigure(1, minsize=FIX_H)
        self.placeholder_video = self._placeholder_imgtk(FIX_W, FIX_H)

        f1 = self._lframe(parent, "Camera ngõ vào"); f1.grid(row=0, column=0, sticky="nsew", padx=5, pady=5); f1.pack_propagate(False)
        self.label_cam_in = tk.Label(f1, image=self.placeholder_video, bg='white'); self.label_cam_in.pack(fill=tk.BOTH, expand=True)

        f2 = self._lframe(parent, "Ảnh xe vào"); f2.grid(row=0, column=1, sticky="nsew", padx=5, pady=5); f2.pack_propagate(False)
        self.label_img_in = tk.Label(f2, image=self.placeholder_video, bg='white'); self.label_img_in.pack(fill=tk.BOTH, expand=True)

        f3 = self._lframe(parent, "Camera ngõ ra"); f3.grid(row=1, column=0, sticky="nsew", padx=5, pady=5); f3.pack_propagate(False)
        self.label_cam_out = tk.Label(f3, image=self.placeholder_video, bg='white'); self.label_cam_out.pack(fill=tk.BOTH, expand=True)

        f4 = self._lframe(parent, "Ảnh xe ra"); f4.grid(row=1, column=1, sticky="nsew", padx=5, pady=5); f4.pack_propagate(False)
        self.label_img_out = tk.Label(f4, image=self.placeholder_video, bg='white'); self.label_img_out.pack(fill=tk.BOTH, expand=True)

    def create_right_pane_widgets(self, parent):
        pf = self._lframe(parent, "Thông tin biển số"); pf.pack(fill=tk.X, pady=(0,5), ipady=5)
        self._populate_plate_frame(pf)

        tf = self._lframe(parent, "Thời gian & Chi phí"); tf.pack(fill=tk.X, pady=5, ipady=5)
        self._populate_time_cost_frame(tf)

        sf = self._lframe(parent, "Trạng thái bãi xe"); sf.pack(fill=tk.X, pady=5, ipady=5)
        self._populate_spots_frame(sf)

        nb = ttk.Notebook(parent); nb.pack(fill=tk.BOTH, expand=True, pady=(10,0))
        tab_res, tab_log = ttk.Frame(nb), ttk.Frame(nb)
        nb.add(tab_res, text="Xe Đặt Chỗ")
        nb.add(tab_log, text="Lịch Sử Xe")
        self._populate_reserved_tab(tab_res)
        self._populate_log_tab(tab_log)

    def _populate_spots_frame(self, parent):
        parent.columnconfigure((0,1,2,3), weight=1)
        fontL = ("Helvetica", 14, "bold")
        for i, spot in enumerate(self.parking_spots.keys()):
            lb = tk.Label(parent, text=spot, font=fontL, relief=tk.RAISED, bd=2, width=5, height=2)
            lb.grid(row=0, column=i, padx=5, pady=5, sticky="ew")
            self.spot_labels[spot] = lb

    def _populate_plate_frame(self, parent):
        parent.columnconfigure((0,1,2), weight=1)
        self.placeholder_plate = self._placeholder_imgtk(150, 75)

        tk.Label(parent, text="Biển số xe vào", font=("Helvetica",10,"bold"), bg='#dcdad5').grid(row=0, column=0, pady=(5,0))
        self.label_plate_in = tk.Label(parent, image=self.placeholder_plate, bg='white'); self.label_plate_in.grid(row=1, column=0)
        self.plate_in_var = tk.StringVar(value="---")
        tk.Label(parent, textvariable=self.plate_in_var, font=("Helvetica",12,"bold"), bg='#dcdad5').grid(row=2, column=0, pady=(0,5))

        self.match_status_var = tk.StringVar(value="")
        tk.Label(parent, textvariable=self.match_status_var, font=("Helvetica",12,"bold","italic"), fg="green", bg='#dcdad5').grid(row=1, column=1)

        tk.Label(parent, text="Biển số xe ra", font=("Helvetica",10,"bold"), bg='#dcdad5').grid(row=0, column=2, pady=(5,0))
        self.label_plate_out = tk.Label(parent, image=self.placeholder_plate, bg='white'); self.label_plate_out.grid(row=1, column=2)
        self.plate_out_var = tk.StringVar(value="---")
        tk.Label(parent, textvariable=self.plate_out_var, font=("Helvetica",12,"bold"), bg='#dcdad5').grid(row=2, column=2, pady=(0,5))

    def _populate_time_cost_frame(self, parent):
        parent.columnconfigure(0, weight=1)
        self.clock_var = tk.StringVar()
        tk.Label(parent, textvariable=self.clock_var, font=("Helvetica",14,"bold"), bg='#dcdad5').pack()

        self.duration_var = tk.StringVar(value="Thời gian gửi: --:--:--")
        tk.Label(parent, textvariable=self.duration_var, font=("Helvetica",11), bg='#dcdad5').pack()

        self.fee_var = tk.StringVar(value="Phí gửi xe: -- VNĐ")
        tk.Label(parent, textvariable=self.fee_var, font=("Helvetica",11,"bold"), bg='#dcdad5').pack()

        bf = tk.Frame(parent, bg='#dcdad5'); bf.pack(pady=5)
        ttk.Button(bf, text="Xác nhận vào (Thủ công)", command=self.capture_in).pack(side=tk.LEFT, padx=10)
        ttk.Button(bf, text="Xác nhận ra (Thủ công)",  command=self.capture_out).pack(side=tk.LEFT, padx=10)

    def _populate_reserved_tab(self, parent):
        cols = ('ID','Tên','SĐT','Biển số','Ô','Giờ','Nạp','Trạng thái')
        self.tree_reserved = ttk.Treeview(parent, columns=cols, show='headings')
        for c in cols:
            self.tree_reserved.heading(c, text=c)
            self.tree_reserved.column(c, width=100, anchor=tk.CENTER)
        self.tree_reserved.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb = ttk.Scrollbar(parent, orient="vertical", command=self.tree_reserved.yview); sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree_reserved.configure(yscrollcommand=sb.set)

    def _populate_log_tab(self, parent):
        cols = ('Mã Thẻ','Biển số','Thời gian vào','Thời gian ra','Phí','Trả từ nạp','Thiếu')
        self.tree_log = ttk.Treeview(parent, columns=cols, show='headings')
        for c in cols:
            self.tree_log.heading(c, text=c)
        self.tree_log.column('Mã Thẻ', width=100, anchor=tk.CENTER)
        self.tree_log.column('Biển số', width=120, anchor=tk.CENTER)
        self.tree_log.column('Thời gian vào', width=160, anchor=tk.CENTER)
        self.tree_log.column('Thời gian ra', width=160, anchor=tk.CENTER)
        self.tree_log.column('Phí', width=90, anchor=tk.E)
        self.tree_log.column('Trả từ nạp', width=90, anchor=tk.E)
        self.tree_log.column('Thiếu', width=90, anchor=tk.E)
        self.tree_log.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb = ttk.Scrollbar(parent, orient="vertical", command=self.tree_log.yview); sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree_log.configure(yscrollcommand=sb.set)

    # ---------- Main loop ----------
    def update_loop(self):
        self.clock_var.set(vn_clock_str())

        # process queues
        self._process_in_events()
        self._process_out_events()

        # update camera frames
        fi = self._get_frame(self.vid_in, channel="in")
        if fi is not None:
            self.last_frame_in = fi
            self._update_video_label(self.label_cam_in, fi)

        fo = self._get_frame(self.vid_out, channel="out")
        if fo is not None:
            self.last_frame_out = fo
            self._update_video_label(self.label_cam_out, fo)

        self.window.after(self.delay, self.update_loop)

    # ---------- Entry (IN) ----------
    def capture_in(self):
        self._process_vehicle_entry(self.last_frame_in, rfid_uid="MANUAL_ENTRY")

    def _process_in_events(self):
        try:
            uid = self.rfid_queue_in.get_nowait()
            self._process_vehicle_entry(self.last_frame_in, rfid_uid=uid)
            return
        except queue.Empty:
            pass
        try:
            _ = self.touch_queue_in.get_nowait()
            self._process_vehicle_entry(self.last_frame_in, rfid_uid="NO_CARD")
            return
        except queue.Empty:
            pass

    def _process_vehicle_entry(self, frame, rfid_uid):
        if frame is None:
            self.toast.show("Không có tín hiệu camera vào.", 2000)
            return

        with self.entry_lock:
            if self.entry_busy:
                return
            self.entry_busy = True

        def worker():
            try:
                # Show LCD step
                self._send_master("LCD1:XE VAO")
                self._send_master("LCD1:SCAN PLATE")

                # OCR NOW
                plate_text, crop_img = self._ocr_plate_now(frame)
                if plate_text == "unknown":
                    self._ui(lambda: self.toast.show("Không nhận diện được biển số xe vào.", 2000))
                    self._send_master("LCD1:OCR FAIL")
                    return

                # plate normalize
                plate_text = safe_upper_plate(plate_text)

                # already inside?
                found_spot, _ = self._find_vehicle_by_plate(plate_text)
                if found_spot:
                    self._ui(lambda: self.toast.show(f"Biển số {plate_text} đã có ở {found_spot}.", 2200))
                    self._send_master("LCD1:DA CO TRONG BAI")
                    return

                # Choose spot: reservation match -> its spot; else empty
                spot_id, prepaid, reserved_at, reserve_id = self._take_reservation_if_match(plate_text)
                if not spot_id:
                    spot_id = self._find_empty_spot()
                    prepaid = 0
                    reserved_at = ""
                    reserve_id = ""

                if not spot_id:
                    self._ui(lambda: self.toast.show("Bãi đã đầy.", 2000))
                    self._send_master("LCD1:BAI DAY")
                    return

                target_num = SPOT_TO_TARGET.get(spot_id, 0)
                if not target_num:
                    self._ui(lambda: self.toast.show("Lỗi mapping ô đỗ.", 2000))
                    self._send_master("LCD1:MAP ERROR")
                    return

                # UI: show images + plate immediately
                def pre_ui():
                    self._set_img(self.label_img_in,  self._pil_from_bgr(frame))
                    if crop_img is not None:
                        self._set_img(self.label_plate_in, self._pil_from_bgr(crop_img))
                    self.plate_in_var.set(plate_text)
                    self.match_status_var.set("")
                self._ui(pre_ui)

                # Move to spot (skip if already at position)
                self._send_master(f"LCD1:SPOT {spot_id}")
                ok = self._move_and_wait_arrived(target_num)
                if not ok:
                    self._ui(lambda: self.toast.show("Quay vị trí thất bại (timeout).", 2200))
                    self._send_master("LCD1:MOVE TIMEOUT")
                    return

                # Beep 1 after arrived (as requested)
                self._send_master("BEEP:1")
                time.sleep(0.05)

                # open gate IN (Arduino beeps 2 and auto close 3s)
                self._send_master("LCD1:OPEN GATE")
                self._send_master("OPEN_IN")

                veh = {
                    'plate_text': plate_text,
                    'entry_time': datetime.now(),
                    'plate_image': self._pil_from_bgr(crop_img) if crop_img is not None else self._placeholder_pil(150, 75),
                    'vehicle_image': self._pil_from_bgr(frame),
                    'status': 'occupied',
                    'rfid_uid': rfid_uid,
                    'prepaid_balance': int(prepaid) if prepaid else 0,
                    'reserve_id': reserve_id,
                    'reserved_at': reserved_at
                }

                def apply():
                    self.parking_spots[spot_id] = veh
                    self.save_spots_to_csv()
                    self.update_spot_display()
                    self._reset_exit_info()
                    self.toast.show(f"Xe {plate_text} đã vào {spot_id}", 1800)
                    self._schedule_reset_display()
                    # refresh web view data
                    self.load_reserved_list_from_csv()
                self._ui(apply)

            finally:
                self._ui(lambda: setattr(self, 'entry_busy', False))

        threading.Thread(target=worker, daemon=True).start()

    # ---------- Exit (OUT) ----------
    def capture_out(self):
        self._process_vehicle_exit_manual()

    def _process_out_events(self):
        try:
            uid = self.rfid_queue_out.get_nowait()
            self._process_vehicle_exit_by_rfid(uid)
            return
        except queue.Empty:
            pass
        try:
            _ = self.touch_queue_out.get_nowait()
            self._process_vehicle_exit_manual()
            return
        except queue.Empty:
            pass

    def _process_vehicle_exit_manual(self):
        frame = self.last_frame_out
        if frame is None:
            self.toast.show("Không có tín hiệu camera ra.", 2000)
            return

        with self.exit_lock:
            if self.exit_busy:
                return
            self.exit_busy = True

        def worker():
            try:
                self._send_master("LCD2:XE RA")
                self._send_master("LCD2:SCAN PLATE")

                plate_out, crop_out = self._ocr_plate_now(frame)
                if plate_out == "unknown":
                    self._ui(lambda: self.toast.show("Không nhận diện được biển số xe ra.", 2000))
                    self._send_master("LCD2:OCR FAIL")
                    return
                plate_out = safe_upper_plate(plate_out)

                spot_id, veh_in = self._find_vehicle_by_plate(plate_out)
                if not spot_id:
                    self._ui(lambda: self.toast.show(f"Không tìm thấy xe {plate_out} trong bãi.", 2200))
                    self._send_master("LCD2:NOT FOUND")
                    return

                self._ui(lambda: self._finalize_exit_flow(spot_id, veh_in, frame, crop_out, rfid_uid=None))
            finally:
                self._ui(lambda: setattr(self, 'exit_busy', False))
        threading.Thread(target=worker, daemon=True).start()

    def _process_vehicle_exit_by_rfid(self, rfid_uid):
        frame = self.last_frame_out
        if frame is None:
            self.toast.show("Không có tín hiệu camera ra.", 2000)
            return

        with self.exit_lock:
            if self.exit_busy:
                return
            self.exit_busy = True

        def worker():
            try:
                self._send_master("LCD2:XE RA")
                self._send_master("LCD2:SCAN PLATE")

                spot_id, veh_in = self._find_vehicle_by_rfid(rfid_uid)
                if not spot_id:
                    self._ui(lambda: self.toast.show(f"Không có xe dùng thẻ {rfid_uid}", 2200))
                    self._send_master("LCD2:UID NOT FOUND")
                    return

                plate_out, crop_out = self._ocr_plate_now(frame)
                if plate_out == "unknown":
                    self._ui(lambda: self.toast.show("Không nhận diện được biển số xe ra.", 2000))
                    self._send_master("LCD2:OCR FAIL")
                    return

                plate_out = safe_upper_plate(plate_out)
                if plate_out != safe_upper_plate(veh_in['plate_text']):
                    def mismatch():
                        self.match_status_var.set("❌ SAI BIỂN SỐ ❌")
                        self._set_img(self.label_img_out, self._pil_from_bgr(frame))
                        if crop_out is not None:
                            self._set_img(self.label_plate_out, self._pil_from_bgr(crop_out))
                        self.plate_out_var.set(plate_out)

                        self._set_img(self.label_img_in,  veh_in['vehicle_image'])
                        self._set_img(self.label_plate_in, veh_in['plate_image'])
                        self.plate_in_var.set(veh_in['plate_text'])
                        self.toast.show("Sai biển số so với xe đã đăng ký!", 2200)
                        self._send_master("LCD2:PLATE MISMATCH")
                    self._ui(mismatch)
                    return

                self._ui(lambda: self._finalize_exit_flow(spot_id, veh_in, frame, crop_out, rfid_uid=rfid_uid))
            finally:
                self._ui(lambda: setattr(self, 'exit_busy', False))
        threading.Thread(target=worker, daemon=True).start()

    def _finalize_exit_flow(self, spot_id, veh_in, frame_out, crop_img_out, rfid_uid):
        plate = safe_upper_plate(veh_in['plate_text'])

        # UI: show images
        self._set_img(self.label_img_out,  self._pil_from_bgr(frame_out))
        if crop_img_out is not None:
            self._set_img(self.label_plate_out, self._pil_from_bgr(crop_img_out))
        self.plate_out_var.set(plate)

        self._set_img(self.label_img_in,  veh_in['vehicle_image'])
        self._set_img(self.label_plate_in, veh_in['plate_image'])
        self.plate_in_var.set(plate)
        self.match_status_var.set("✅ TRÙNG BIỂN SỐ ✅")

        target_num = SPOT_TO_TARGET.get(spot_id, 0)
        if not target_num:
            self.toast.show("Lỗi mapping ô đỗ.", 2000)
            return

        # LCD OUT UI on Arduino
        self._send_master(f"OUT,{plate}")
        time.sleep(0.05)

        self._send_master(f"LCD2:SPOT {spot_id}")
        ok = self._move_and_wait_arrived(target_num)
        if not ok:
            self.toast.show("Quay vị trí xe ra thất bại (timeout).", 2200)
            self._send_master("LCD2:MOVE TIMEOUT")
            return

        # Beep 1 after arrived
        self._send_master("BEEP:1")
        time.sleep(0.05)

        # open servo OUT
        self._send_master("LCD2:OPEN GATE")
        self._send_master("OPEN_OUT")

        exit_time = datetime.now()

        # Fee rule:
        # - If has reservation -> start time from reserved_at (created_at)
        # - Else -> start time from entry_time
        start_time = veh_in.get('entry_time', datetime.now())
        reserved_at_str = (veh_in.get("reserved_at") or "").strip()
        if reserved_at_str:
            try:
                start_time = datetime.strptime(reserved_at_str, "%Y-%m-%d %H:%M:%S")
            except:
                pass

        duration = exit_time - start_time
        raw_fee = (duration.total_seconds()/3600) * self.fee_per_hour
        final_fee = int(math.ceil(raw_fee/1000)*1000) if raw_fee > 0 else 0

        prepaid = int(veh_in.get("prepaid_balance", 0) or 0)
        paid_from_prepaid = min(prepaid, final_fee)
        thieu = max(0, final_fee - prepaid)
        remaining = max(0, prepaid - final_fee)

        secs = int(max(0, duration.total_seconds()))
        h, r = divmod(secs,3600); m, s = divmod(r,60)
        self.duration_var.set(f"Thời gian gửi: {h:02d}:{m:02d}:{s:02d}")
        self.fee_var.set(f"Phí gửi xe: {fmt_money(final_fee)} VNĐ")

        # log CSV
        self._log_exit({
            'ma_the': veh_in.get('rfid_uid','N/A'),
            'bien_so': plate,
            'thoi_gian_vao': (veh_in.get('entry_time', datetime.now())).strftime("%Y-%m-%d %H:%M:%S"),
            'thoi_gian_ra' : exit_time.strftime("%Y-%m-%d %H:%M:%S"),
            'phi': f"{fmt_money(final_fee)} VNĐ",
            'paid_from_prepaid': f"{fmt_money(paid_from_prepaid)} VNĐ",
            'con_thieu': f"{fmt_money(thieu)} VNĐ"
        })

        # update reservation if used
        reserve_id = (veh_in.get("reserve_id") or "").strip()
        if reserve_id:
            self._mark_reservation_done(reserve_id, exit_time, final_fee, paid_from_prepaid, thieu)

        # clear spot
        self.parking_spots[spot_id] = None
        self.save_spots_to_csv()
        self.update_spot_display()
        self.load_reserved_list_from_csv()
        self.load_log_from_csv()

        if prepaid > 0:
            self.toast.show(f"Xe {plate} rời {spot_id}. Trừ từ nạp: {fmt_money(paid_from_prepaid)}đ, thiếu: {fmt_money(thieu)}đ", 2600)
        else:
            self.toast.show(f"Xe {plate} rời {spot_id}. Phí: {fmt_money(final_fee)}đ", 2200)

        self._schedule_reset_display()

    # ---------- MOVE + WAIT ARRIVED ----------
    def _drain_arrived_queue(self):
        while True:
            try:
                self.arrived_queue.get_nowait()
            except queue.Empty:
                break

    def _move_and_wait_arrived(self, target_num):
        # skip if already there (Python tracks master_position)
        if int(target_num) == int(self.master_position):
            return True

        self._drain_arrived_queue()
        self._send_master(str(int(target_num)))

        t0 = time.time()
        while time.time() - t0 < ARRIVED_TIMEOUT_SEC:
            try:
                arrived = self.arrived_queue.get(timeout=0.2)
                if arrived == int(target_num):
                    self.master_position = int(arrived)
                    return True
            except queue.Empty:
                pass
        return False

    # ---------- OCR (NO TIMEOUT) ----------
    def _ocr_plate_now(self, frame):
        """
        OCR ngay thời điểm hiện tại (không timeout chờ).
        - Detect plate once
        - OCR attempts a few deskew combos (fast)
        """
        plate = "unknown"
        crop = None
        try:
            plates = yolo_LP_detect(frame, size=640)
            lst = plates.pandas().xyxy[0].values.tolist()
        except Exception as e:
            print("Detect lỗi:", e)
            return "unknown", None

        if not lst:
            return "unknown", None

        # Choose best by confidence if available
        try:
            lst.sort(key=lambda x: float(x[4]), reverse=True)
        except:
            pass

        x,y,x2,y2 = map(int, lst[0][:4])
        x=max(0,x); y=max(0,y); x2=max(x+1,x2); y2=max(y+1,y2)
        crop = frame[y:y2, x:x2]

        # quick OCR tries
        for cc in range(2):
            for ct in range(2):
                try:
                    lp = helper.read_plate(yolo_license_plate, utils_rotate.deskew(crop, cc, ct))
                    if lp and str(lp).strip().lower() != "unknown":
                        return safe_upper_plate(str(lp)), crop
                except Exception:
                    pass

        return plate, crop

    # ---------- Serial MASTER ----------
    def start_master_listener(self, com_port, baud=9600):
        com_port = (com_port or "").strip()
        if not com_port or "Không tìm thấy" in com_port:
            self.toast.show("Chọn cổng COM hợp lệ.", 2000)
            return

        # stop old
        if self.listener_thread and self.listener_thread.is_alive():
            self.stop_thread.set()
            try:
                self.listener_thread.join(timeout=1)
            except Exception:
                pass
            try:
                if self.master_serial_connection and self.master_serial_connection.is_open:
                    self.master_serial_connection.close()
            except Exception:
                pass

        self.stop_thread.clear()
        self.listener_thread = threading.Thread(target=self._read_master_serial, args=(com_port, baud), daemon=True)
        self.listener_thread.start()
        self.toast.show(f"Đang kết nối MASTER {com_port}...", 1500)

    def _read_master_serial(self, com_port, baud):
        print(f"Kết nối MASTER: {com_port}")
        try:
            conn = serial.Serial(com_port, baud, timeout=1)
            self.master_serial_connection = conn
        except Exception as e:
            print("Mở COM lỗi:", e)
            self._ui(lambda: self.toast.show("Mở COM lỗi.", 2200))
            return

        self._ui(lambda: self.toast.show(f"Đã kết nối {com_port}", 1500))

        while not self.stop_thread.is_set():
            try:
                line = conn.readline().decode('utf-8', errors='ignore').strip()
                if not line:
                    continue

                # chặn spam lặp dòng
                if line == self.last_serial_line and (now_ms()-self.last_serial_time_ms) < SERIAL_SAME_LINE_COOLDOWN_MS:
                    continue
                self.last_serial_line = line; self.last_serial_time_ms = now_ms()

                # print("[MASTER]", line)

                if line.startswith("RFID_IN:"):
                    uid = line.split("RFID_IN:",1)[1].strip().upper()
                    if self._uid_ok('in', uid):
                        self.rfid_queue_in.put(uid)

                elif line.startswith("RFID_OUT:"):
                    uid = line.split("RFID_OUT:",1)[1].strip().upper()
                    if self._uid_ok('out', uid):
                        self.rfid_queue_out.put(uid)

                elif "TOUCH_IN" in line:
                    self.touch_queue_in.put(True)

                elif "TOUCH_OUT" in line:
                    self.touch_queue_out.put(True)

                elif line.startswith("ARRIVED:"):
                    try:
                        n = int(line.split("ARRIVED:",1)[1].strip())
                        self.master_position = n
                        self.arrived_queue.put(n)
                    except Exception:
                        pass

                # else ignore

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
        cd = UID_COOLDOWN_MS_IN if direction=='in' else UID_COOLDOWN_MS_OUT
        if now_ms()-t < cd:
            return False
        self.uid_last_time[direction][uid] = now_ms()
        return True

    def _send_master(self, text):
        text = (text or "").strip()
        if not text:
            return
        try:
            c = self.master_serial_connection
            if c and c.is_open:
                c.write((text + "\n").encode('utf-8'))
                # print("[PC→MASTER]", text)
            else:
                # print("[PC→MASTER] Chưa kết nối.")
                pass
        except Exception as e:
            print("Gửi lệnh lỗi:", e)

    # ---------- Settings window (Tkinter) ----------
    def open_settings_window(self):
        w = tk.Toplevel(self.window); w.title("Cài đặt"); w.configure(bg='#e6f0ff'); w.resizable(False, False)

        cams = self._find_cams()
        ports = self._find_coms()

        cf = self._lframe(w, "Chọn camera"); cf.pack(padx=20,pady=10,fill="x")
        tk.Label(cf, text="Camera vào:", bg='#dcdad5').grid(row=0,column=0,sticky="w",padx=5,pady=5)
        cam_in_var = tk.StringVar(value=str(self.settings.get("cam_in","0")))
        cb_in = ttk.Combobox(cf, textvariable=cam_in_var, values=cams, state="readonly", width=30)
        cb_in.grid(row=0,column=1,padx=5,pady=5)

        tk.Label(cf, text="Camera ra:", bg='#dcdad5').grid(row=1,column=0,sticky="w",padx=5,pady=5)
        cam_out_var = tk.StringVar(value=str(self.settings.get("cam_out","1")))
        cb_out = ttk.Combobox(cf, textvariable=cam_out_var, values=cams, state="readonly", width=30)
        cb_out.grid(row=1,column=1,padx=5,pady=5)

        def apply_cams():
            def parse_cam(x):
                x = str(x)
                if x.startswith("Camera "):
                    try: return int(x.split("Camera ",1)[1].strip())
                    except: return 0
                return x
            self.settings["cam_in"] = str(parse_cam(cam_in_var.get()))
            self.settings["cam_out"] = str(parse_cam(cam_out_var.get()))
            write_settings(self.settings)
            self.on_settings_changed(self.settings)
            self.toast.show("Đã áp dụng camera.", 1600)

        ttk.Button(cf, text="Áp dụng", command=apply_cams).grid(row=0,rowspan=2,column=2,padx=10,pady=10)

        sf = self._lframe(w, "Kết nối Arduino MASTER + RFID"); sf.pack(padx=20,pady=10,fill="x")
        com_var = tk.StringVar(value=self.settings.get("com_port",""))
        tk.Label(sf, text="Cổng COM:", bg='#dcdad5').grid(row=0,column=0,sticky="w",padx=5,pady=5)
        cb = ttk.Combobox(sf, textvariable=com_var, values=ports, state="readonly", width=20); cb.grid(row=0,column=1,padx=5,pady=5)

        def connect():
            self.settings["com_port"] = com_var.get()
            write_settings(self.settings)
            self.start_master_listener(com_var.get(), 9600)

        ttk.Button(sf, text="Kết nối", command=connect).grid(row=0,column=2,padx=10,pady=5)

        ff = self._lframe(w, "Cài đặt phí (VNĐ/giờ)"); ff.pack(padx=20,pady=10,fill="x")
        fee_var = tk.StringVar(value=str(self.fee_per_hour))
        e = ttk.Entry(ff, textvariable=fee_var, width=20); e.pack(side=tk.LEFT,padx=10,pady=10)

        def save_fee():
            try:
                v = int(fee_var.get())
                if v<0: raise ValueError
                self.fee_per_hour = v
                self.settings["fee_per_hour"] = str(v)
                write_settings(self.settings)
                self.toast.show(f"Đã cập nhật phí: {fmt_money(v)} VNĐ/giờ", 2000)
            except:
                self.toast.show("Phí không hợp lệ.", 1800)

        ttk.Button(ff, text="Lưu Phí", command=save_fee).pack(side=tk.LEFT,padx=10,pady=10)

    # ---------- Camera (FIX: support camera index / video file / image file) ----------
    def init_capture_devices(self):
        # release old
        try:
            if self.vid_in: self.vid_in.release()
        except: pass
        try:
            if self.vid_out: self.vid_out.release()
        except: pass

        self.vid_in = None
        self.vid_out = None
        self.static_frame_in = None
        self.static_frame_out = None

        def open_source(src, which="in"):
            if isinstance(src, int):
                backend = cv2.CAP_DSHOW if os.name == "nt" else cv2.CAP_ANY
                cap = cv2.VideoCapture(src, backend)
                try:
                    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                except: pass
                return cap, None

            s = str(src).strip()
            low = s.lower()
            img_ext = (".jpg", ".jpeg", ".png", ".bmp", ".webp")
            if low.endswith(img_ext):
                frame = cv2.imread(s)
                if frame is None:
                    print(f"Không đọc được ảnh {which}: {s}")
                return None, frame

            # video file / stream
            backend = cv2.CAP_ANY
            cap = cv2.VideoCapture(s, backend)
            try:
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            except: pass
            return cap, None

        self.vid_in, self.static_frame_in = open_source(self.source_in, "in")
        self.vid_out, self.static_frame_out = open_source(self.source_out, "out")

        if self.vid_in is not None and not self.vid_in.isOpened():
            print("Không mở được camera/video vào:", self.source_in)
        if self.vid_out is not None and not self.vid_out.isOpened():
            print("Không mở được camera/video ra:", self.source_out)

    def _reopen_cams(self):
        self.init_capture_devices()

    def _get_frame(self, cap, channel="in"):
        # static image mode
        if cap is None:
            return self.static_frame_in if channel == "in" else self.static_frame_out

        if not cap.isOpened():
            return None

        ret, frame = cap.read()
        if not ret:
            # loop video for demo
            try:
                cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                ret2, frame2 = cap.read()
                if ret2:
                    return frame2
            except:
                pass
            return None

        return frame

    def _update_video_label(self, label, frame):
        self._set_img(label, self._pil_from_bgr(frame))

    # ---------- Web helpers ----------
    def get_spots_status_for_web(self):
        """
        return list of dicts: {spot,status,plate}
        status: empty/reserved/occupied
        """
        spots = []
        for sid in SPOT_ORDER:
            v = self.parking_spots.get(sid)
            if v is None:
                spots.append({"spot": sid, "status": "empty", "plate": ""})
            else:
                st = v.get("status","occupied")
                if st not in ("reserved","occupied"):
                    st = "occupied"
                spots.append({"spot": sid, "status": st, "plate": v.get("plate_text","")})
        return spots

    def read_reservations(self):
        ensure_csv_reserved()
        res = []
        try:
            with open(CSV_RESERVED, "r", newline="", encoding="utf-8") as f:
                rd = csv.DictReader(f)
                for r in rd:
                    # normalize missing fields
                    rr = {k:(r.get(k,"") or "") for k in RES_FIELDS}
                    # default status if empty
                    if not rr["status"]:
                        rr["status"] = "reserved"
                    res.append(rr)
        except Exception:
            pass
        # newest first
        res.reverse()

        # format for web (object-like)
        out = []
        for r in res[:300]:
            out.append(type("R",(object,),{
                "id": r["id"], "ten": r["ten"], "sdt": r["sdt"], "bien_so": r["bien_so"], "spot": r["spot"],
                "gio_du_kien": r["gio_du_kien"], "so_tien_nap": r["so_tien_nap"], "created_at": r["created_at"],
                "status": r["status"], "arrival_time": r["arrival_time"], "exit_time": r["exit_time"],
                "fee_total": r["fee_total"]
            })())
        return out

    def read_vehicle_logs(self):
        ensure_csv_log()
        logs = []
        try:
            with open(CSV_LOG, "r", newline="", encoding="utf-8") as f:
                rd = csv.DictReader(f)
                for r in rd:
                    logs.append(type("L",(object,),{
                        "ma_the": r.get("ma_the",""),
                        "bien_so": r.get("bien_so",""),
                        "thoi_gian_vao": r.get("thoi_gian_vao",""),
                        "thoi_gian_ra": r.get("thoi_gian_ra",""),
                        "phi": r.get("phi",""),
                        "paid_from_prepaid": r.get("paid_from_prepaid",""),
                        "con_thieu": r.get("con_thieu",""),
                    })())
        except Exception:
            pass
        logs.reverse()
        return logs[:400]

    def add_reservation(self, ten, sdt, plate, spot, gio_du_kien, so_tien_nap):
        plate = safe_upper_plate(plate)

        # validate spot exists
        if spot not in self.parking_spots:
            return False, "Ô đỗ không hợp lệ."

        # must be empty (not reserved, not occupied)
        if self.parking_spots.get(spot) is not None:
            return False, "Ô đỗ không còn trống."

        # check no active reservation on same spot
        rows = self._read_res_rows()
        for r in rows:
            if r.get("spot","") == spot and r.get("status","") in ("reserved","in"):
                return False, "Ô đỗ đã được đặt trước."

        rid = str(int(time.time()*1000))
        created = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        status = "reserved"

        row = {k:"" for k in RES_FIELDS}
        row.update({
            "id": rid, "ten": ten, "sdt": sdt, "bien_so": plate,
            "spot": spot, "gio_du_kien": str(gio_du_kien),
            "so_tien_nap": str(so_tien_nap),
            "created_at": created, "status": status,
            "arrival_time": "", "exit_time": "",
            "fee_total": "", "paid_from_prepaid": "", "con_thieu": ""
        })

        rows.append(row)
        self._write_res_rows(rows)

        # apply reserved into RAM for UI & web
        self._ui(lambda: self.apply_reservations_to_spots())
        return True, "OK"

    # ---------- Reservation internals ----------
    def _read_res_rows(self):
        ensure_csv_reserved()
        rows = []
        try:
            with open(CSV_RESERVED, "r", newline="", encoding="utf-8") as f:
                rd = csv.DictReader(f)
                for r in rd:
                    rr = {k:(r.get(k,"") or "") for k in RES_FIELDS}
                    if not rr["status"]:
                        rr["status"] = "reserved"
                    rows.append(rr)
        except Exception:
            pass
        return rows

    def _write_res_rows(self, rows):
        ensure_csv_reserved()
        with open(CSV_RESERVED, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=RES_FIELDS)
            w.writeheader()
            for r in rows:
                w.writerow({k:r.get(k,"") for k in RES_FIELDS})

    def apply_reservations_to_spots(self):
        """
        Represent reservation in RAM as status='reserved' (orange) only if spot is empty.
        """
        rows = self._read_res_rows()

        # clear old reserved markers in RAM
        for sid, v in list(self.parking_spots.items()):
            if v and v.get("status") == "reserved":
                self.parking_spots[sid] = None

        # apply latest reserved rows
        for r in rows:
            if r.get("status") != "reserved":
                continue
            spot = r.get("spot","").strip()
            plate = safe_upper_plate(r.get("bien_so",""))
            if spot in self.parking_spots and self.parking_spots[spot] is None:
                self.parking_spots[spot] = {
                    "plate_text": plate,
                    "status": "reserved",
                    "vehicle_image": self._placeholder_pil(500,375),
                    "plate_image": self._placeholder_pil(150,75),
                    "rfid_uid": "RESERVED",
                    "entry_time": datetime.now(),
                    "prepaid_balance": int(r.get("so_tien_nap","0") or 0),
                    "reserve_id": r.get("id",""),
                    "reserved_at": r.get("created_at","")
                }

        self.update_spot_display()
        self.load_reserved_list_from_csv()
        self.save_spots_to_csv()

    def _take_reservation_if_match(self, plate_text):
        """
        If reservation matches plate and is active (reserved):
          - mark status to 'in'
          - return its spot, prepaid, created_at, reserve_id
        """
        plate_text = safe_upper_plate(plate_text)
        rows = self._read_res_rows()

        hit = None
        for r in rows:
            if safe_upper_plate(r.get("bien_so","")) == plate_text and r.get("status","") == "reserved":
                hit = r
                break
        if not hit:
            return None, 0, "", ""

        spot = hit.get("spot","").strip()
        if spot not in self.parking_spots:
            return None, 0, "", ""

        # If that spot is currently occupied by a car, fallback to normal empty spot (keep reservation)
        if self.parking_spots.get(spot) is not None and self.parking_spots[spot].get("status") == "occupied":
            return None, 0, "", ""

        # mark IN + arrival_time
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for r in rows:
            if r.get("id") == hit.get("id"):
                r["status"] = "in"
                r["arrival_time"] = now_str
        self._write_res_rows(rows)

        # update RAM display
        self._ui(lambda: self.apply_reservations_to_spots())

        prepaid = int(hit.get("so_tien_nap","0") or 0)
        reserved_at = hit.get("created_at","")
        reserve_id = hit.get("id","")
        return spot, prepaid, reserved_at, reserve_id

    def _mark_reservation_done(self, reserve_id, exit_time, fee_total, paid_from_prepaid, con_thieu):
        reserve_id = str(reserve_id).strip()
        rows = self._read_res_rows()
        for r in rows:
            if str(r.get("id","")).strip() == reserve_id:
                r["status"] = "done"
                r["exit_time"] = exit_time.strftime("%Y-%m-%d %H:%M:%S")
                r["fee_total"] = f"{fmt_money(fee_total)}"
                r["paid_from_prepaid"] = f"{fmt_money(paid_from_prepaid)}"
                r["con_thieu"] = f"{fmt_money(con_thieu)}"
        self._write_res_rows(rows)

    # ---------- CSV spots persistence ----------
    def save_spots_to_csv(self):
        ensure_csv_spots()
        try:
            with open(CSV_SPOTS, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["spot","status","plate","rfid_uid","entry_time","prepaid_balance","reserve_id","reserved_at"])
                for sid in SPOT_ORDER:
                    v = self.parking_spots.get(sid)
                    if v is None:
                        w.writerow([sid,"empty","","","", "0","",""])
                    else:
                        st = v.get("status","occupied")
                        plate = v.get("plate_text","")
                        uid = v.get("rfid_uid","")
                        et = ""
                        if isinstance(v.get("entry_time"), datetime):
                            et = v["entry_time"].strftime("%Y-%m-%d %H:%M:%S")
                        prepaid = int(v.get("prepaid_balance",0) or 0)
                        reserve_id = v.get("reserve_id","")
                        reserved_at = v.get("reserved_at","")
                        w.writerow([sid, st, plate, uid, et, str(prepaid), reserve_id, reserved_at])
        except Exception as e:
            print("Lưu vi_tri_do.csv lỗi:", e)

    def load_spots_from_csv(self):
        ensure_csv_spots()
        try:
            with open(CSV_SPOTS, "r", newline="", encoding="utf-8") as f:
                rd = csv.DictReader(f)
                for sid in self.parking_spots:
                    self.parking_spots[sid] = None
                for r in rd:
                    sid = r.get("spot","")
                    st  = r.get("status","empty")
                    if sid not in self.parking_spots:
                        continue
                    if st == "empty":
                        self.parking_spots[sid] = None
                    else:
                        plate = safe_upper_plate(r.get("plate",""))
                        uid = r.get("rfid_uid","")
                        et_str = r.get("entry_time","")
                        try: et = datetime.strptime(et_str, "%Y-%m-%d %H:%M:%S") if et_str else datetime.now()
                        except: et = datetime.now()
                        prepaid = int(r.get("prepaid_balance","0") or 0)
                        reserve_id = r.get("reserve_id","")
                        reserved_at = r.get("reserved_at","")
                        self.parking_spots[sid] = {
                            "plate_text": plate,
                            "status": st,
                            "vehicle_image": self._placeholder_pil(500,375),
                            "plate_image": self._placeholder_pil(150,75),
                            "rfid_uid": uid,
                            "entry_time": et,
                            "prepaid_balance": prepaid,
                            "reserve_id": reserve_id,
                            "reserved_at": reserved_at
                        }
        except Exception as e:
            print("Load vi_tri_do.csv lỗi:", e)

    # ---------- Reserved list & log list ----------
    def load_reserved_list_from_csv(self):
        ensure_csv_reserved()
        for it in self.tree_reserved.get_children():
            self.tree_reserved.delete(it)
        try:
            rows = self._read_res_rows()
            for r in reversed(rows[-400:]):
                self.tree_reserved.insert("", 0, values=(
                    r.get("id",""),
                    r.get("ten",""),
                    r.get("sdt",""),
                    r.get("bien_so",""),
                    r.get("spot",""),
                    r.get("gio_du_kien",""),
                    r.get("so_tien_nap",""),
                    r.get("status",""),
                ))
        except Exception as e:
            print("Đọc CSV đặt chỗ lỗi:", e)

    def load_log_from_csv(self):
        ensure_csv_log()
        for it in self.tree_log.get_children():
            self.tree_log.delete(it)
        try:
            with open(CSV_LOG, "r", newline="", encoding="utf-8") as f:
                rd = csv.DictReader(f)
                rows = list(rd)
            for r in reversed(rows[-600:]):
                self.tree_log.insert("", 0, values=(
                    r.get("ma_the",""),
                    r.get("bien_so",""),
                    r.get("thoi_gian_vao",""),
                    r.get("thoi_gian_ra",""),
                    r.get("phi",""),
                    r.get("paid_from_prepaid",""),
                    r.get("con_thieu",""),
                ))
        except Exception as e:
            print("Đọc CSV log lỗi:", e)

    def _log_exit(self, row):
        ensure_csv_log()
        try:
            with open(CSV_LOG, 'a', newline='', encoding='utf-8') as f:
                w = csv.DictWriter(f, fieldnames=LOG_FIELDS)
                # ensure header exists
                if f.tell() == 0:
                    w.writeheader()
                w.writerow({k:row.get(k,"") for k in LOG_FIELDS})
            self.load_log_from_csv()
        except Exception as e:
            print("Ghi CSV log lỗi:", e)

    # ---------- Spot finders ----------
    def _find_empty_spot(self):
        for sid in SPOT_ORDER:
            v = self.parking_spots.get(sid)
            if v is None:
                return sid
        return None

    def _find_vehicle_by_plate(self, plate):
        plate = safe_upper_plate(plate)
        for sid, v in self.parking_spots.items():
            if v and v.get("status") == "occupied" and safe_upper_plate(v.get('plate_text','')) == plate:
                return sid, v
        return None, None

    def _find_vehicle_by_rfid(self, uid):
        uid = (uid or "").upper().strip()
        for sid, v in self.parking_spots.items():
            if v and v.get("status") == "occupied" and (str(v.get('rfid_uid','')).upper().strip() == uid):
                return sid, v
        return None, None

    # ---------- UI update spots ----------
    def update_spot_display(self):
        for sid, v in self.parking_spots.items():
            lb = self.spot_labels[sid]
            if v:
                st = v.get('status','occupied')
                if st == 'reserved':
                    lb.config(bg='#f39c12', fg='white', text=f"{sid}\n{v.get('plate_text','')}")
                else:
                    lb.config(bg='#e74c3c', fg='white', text=f"{sid}\n{v.get('plate_text','')}")
            else:
                lb.config(bg='#2ecc71', fg='white', text=sid)

    # ---------- Image helpers ----------
    def _lframe(self, parent, text): return ttk.LabelFrame(parent, text=text)
    def _placeholder_imgtk(self,w,h): return ImageTk.PhotoImage(PILImage.new('RGB',(w,h),'white'))
    def _placeholder_pil(self,w,h): return PILImage.new('RGB',(w,h),'white')

    def _set_img(self, label, pil_img):
        lw, lh = label.winfo_width(), label.winfo_height()
        if lw < 2 or lh < 2:
            self.window.after(50, lambda: self._set_img(label, pil_img))
            return
        bg = PILImage.new('RGB', (lw, lh), 'white')
        im = pil_img.copy()
        im.thumbnail((lw, lh), PILImage.Resampling.LANCZOS)
        x = (lw - im.width)//2; y = (lh - im.height)//2
        bg.paste(im, (x,y))
        imgtk = ImageTk.PhotoImage(bg)
        label.configure(image=imgtk); label.image = imgtk

    def _pil_from_bgr(self, frame):
        return PILImage.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))

    # ---------- reset display ----------
    def _reset_exit_info(self):
        self.label_img_out.configure(image=self.placeholder_video); self.label_img_out.image = self.placeholder_video
        self.label_plate_out.configure(image=self.placeholder_plate); self.label_plate_out.image = self.placeholder_plate
        self.plate_out_var.set("---"); self.match_status_var.set("")
        self.duration_var.set("Thời gian gửi: --:--:--")
        self.fee_var.set("Phí gửi xe: -- VNĐ")

    def _reset_all(self):
        self.label_img_in.configure(image=self.placeholder_video); self.label_img_in.image = self.placeholder_video
        self.label_plate_in.configure(image=self.placeholder_plate); self.label_plate_in.image = self.placeholder_plate
        self.plate_in_var.set("---")
        self._reset_exit_info()

    def _schedule_reset_display(self):
        self.window.after(DISPLAY_RESET_MS, self._reset_all)

    # ---------- select media source ----------
    def select_media_source(self, channel):
        fp = filedialog.askopenfilename(
            title="Chọn ảnh/video",
            filetypes=[("All","*.*"),("Video","*.mp4 *.avi *.mkv *.mov"),("Image","*.jpg *.jpeg *.png *.bmp *.webp")]
        )
        if not fp:
            return
        if channel == 'in':
            self.source_in = fp
            self.settings["cam_in"] = fp
        else:
            self.source_out = fp
            self.settings["cam_out"] = fp
        write_settings(self.settings)
        self._reopen_cams()
        self.toast.show(f"Đã đổi nguồn Camera {channel.upper()}.", 1800)

    # ---------- find cams/com ----------
    def _find_cams(self):
        res = []
        for i in range(10):
            backend = cv2.CAP_DSHOW if os.name == "nt" else cv2.CAP_ANY
            cap = cv2.VideoCapture(i, backend)
            if cap is not None and cap.isOpened():
                res.append(f"Camera {i}")
                cap.release()
        return res if res else ["Không tìm thấy camera"]

    def _find_coms(self):
        try:
            ports = serial.tools.list_ports.comports()
            return [p.device for p in ports] if ports else ["Không tìm thấy cổng COM"]
        except:
            return ["pyserial chưa được cài đặt"]

    # ---------- UI thread helper ----------
    def _ui(self, fn):
        self.window.after(0, fn)

    # ---------- Closing ----------
    def on_closing(self):
        self.stop_thread.set()
        try:
            if self.master_serial_connection and self.master_serial_connection.is_open:
                self.master_serial_connection.close()
        except:
            pass
        try:
            if self.listener_thread:
                self.listener_thread.join(timeout=1)
        except:
            pass
        try:
            if self.vid_in: self.vid_in.release()
            if self.vid_out: self.vid_out.release()
        except:
            pass
        self.window.destroy()


# ===================== RUN =====================
if __name__ == "__main__":
    ensure_csv_reserved()
    ensure_csv_spots()
    ensure_csv_log()
    ensure_csv_settings()

    root = tk.Tk()
    root.state('zoomed')
    app = ParkingApp(root, "Hệ thống Quản lý Bãi giữ xe (Arduino MASTER + RFID + OCR + Web)")
    root.mainloop()
