# -*- coding: utf-8 -*-
"""
SMART PARKING - FULL (Tkinter + Serial + OCR + Web Reservation + History)

Arduino MASTER (đã OK theo bạn):
- Gửi lên PC: RFID_IN:<uid>, RFID_OUT:<uid>, TOUCH_IN, TOUCH_OUT, ARRIVED:<pos>, STATION_PASS:<pos>
- Nhận từ PC: "1|2|3|4" (GO), "OPEN_IN", "OPEN_OUT", "OUT,<plate>", "BEEP:n", ...

Yêu cầu đã áp:
- Mặc định bãi đang ở vị trí 1 (Python giữ self.master_pos = 1)
- OCR nhận diện ngay thời điểm đó (không timeout)
- Xe vào: thẻ hoặc nút -> OCR -> chọn ô trống -> quay (nếu cần) -> BEEP:1 -> OPEN_IN
- Xe ra : thẻ hoặc nút -> OCR -> tìm xe -> quay -> BEEP:1 -> OPEN_OUT
- Web:
  - /reservations : Đặt trước + danh sách đặt + KPI + trạng thái ô
  - /history      : Lịch sử xe
  - Admin settings đơn giản
  - Fix lỗi web in raw HTML: dùng {{ body|safe }}

CSV:
- dat_cho_truoc.csv : đặt trước
- vi_tri_do.csv     : trạng thái bãi
- lich_su_xe.csv    : lịch sử xe
- settings.csv      : cấu hình
"""

import os
import csv
import math
import time
import threading
import queue
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
from flask import Flask, request, redirect, session, render_template_string

# ===================== YOLO / OCR (giữ như bạn, nhưng OCR không timeout) =====================
try:
    import torch
    import function.utils_rotate as utils_rotate
    import function.helper as helper
    TORCH_OK = True
except Exception:
    TORCH_OK = False
    print("Không có module function/ hoặc torch, dùng mock YOLO-OCR để test.")

    class _MockValues:
        def __init__(self):
            self._vals = [[100, 100, 300, 200, 0.95, 0]]

        def tolist(self):
            return self._vals

    class _MockDF:
        def __init__(self):
            self.values = _MockValues()

    class _MockPandasResult:
        def __init__(self):
            self.xyxy = [_MockDF()]

        def pandas(self):
            return self

    class MockYoloModel:
        def __init__(self):
            self.conf = 0.6

        def __call__(self, frame, size=640):
            return _MockPandasResult()

    class helper:
        @staticmethod
        def read_plate(model, img):
            return "80T-8888"

    class utils_rotate:
        @staticmethod
        def deskew(img, a, b):
            return img

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
ARRIVED_TIMEOUT_SEC = 28

CSV_RESERVED = "dat_cho_truoc.csv"
CSV_LOG      = "lich_su_xe.csv"
CSV_SPOTS    = "vi_tri_do.csv"
CSV_SETTINGS = "settings.csv"

DEFAULT_FEE_PER_HOUR = 5000
ADMIN_USER = "Admin"
ADMIN_PASS = "123"

# A1..A4 -> 1..4
SPOT_TO_TARGET = {'A1': 1, 'A2': 2, 'A3': 3, 'A4': 4}
TARGET_TO_SPOT = {v: k for k, v in SPOT_TO_TARGET.items()}

def now_ms():
    return int(time.time() * 1000)

def vn_clock_str():
    dow = ["Thứ Hai","Thứ Ba","Thứ Tư","Thứ Năm","Thứ Sáu","Thứ Bảy","Chủ Nhật"]
    d = datetime.now()
    return f"{dow[d.weekday()]}, {d.strftime('%d/%m/%Y | %H:%M:%S')}"

# ===================== TOAST =====================
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
            for k, v in rd:
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
    for k, v in d.items():
        rows.append([k, str(v)])
    with open(CSV_SETTINGS, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerows(rows)

def ensure_csv_spots():
    if not os.path.isfile(CSV_SPOTS):
        with open(CSV_SPOTS, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["spot","status","plate","rfid_uid","entry_time","bill_start_time","reservation_id"])
            for s in SPOT_TO_TARGET.keys():
                w.writerow([s,"empty","","","","",""])

def ensure_csv_log():
    if not os.path.isfile(CSV_LOG):
        with open(CSV_LOG, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["ma_the","bien_so","thoi_gian_vao","thoi_gian_ra","phi","bill_start","note"])

def ensure_csv_reserved():
    """
    dat_cho_truoc.csv columns:
      id,ten,sdt,bien_so,spot,gio_du_kien,created_at,status,arrival_time,exit_time,final_fee
    """
    required = ["id","ten","sdt","bien_so","spot","gio_du_kien",
                "created_at","status","arrival_time","exit_time","final_fee"]

    if os.path.isfile(CSV_RESERVED):
        # detect header mismatch -> backup -> recreate
        try:
            with open(CSV_RESERVED, "r", newline="", encoding="utf-8") as f:
                first = next(csv.reader(f), [])
            if first != required:
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                backup = f"dat_cho_truoc_backup_{ts}.csv"
                try:
                    os.replace(CSV_RESERVED, backup)
                except Exception:
                    pass
                with open(CSV_RESERVED, "w", newline="", encoding="utf-8") as f:
                    w = csv.writer(f)
                    w.writerow(required)
        except Exception:
            pass
        return

    with open(CSV_RESERVED, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(required)

# ===================== WEB TEMPLATES (PRO STYLE) =====================
WEB_BASE = r"""
<!doctype html>
<html lang="vi">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>SMART PARKING</title>
  <style>
    :root{
      --bg:#0b1220;
      --card:#111a2e;
      --card2:#0f1830;
      --muted:#91a4c7;
      --text:#eaf0ff;
      --line:rgba(255,255,255,.08);
      --brand:#4f8cff;
      --good:#35d07f;
      --warn:#ffb020;
      --bad:#ff5f6d;
      --shadow: 0 14px 40px rgba(0,0,0,.35);
      --radius:16px;
    }
    *{box-sizing:border-box}
    body{
      margin:0;
      font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Arial;
      background: radial-gradient(1200px 500px at 20% -10%, rgba(79,140,255,.35), transparent),
                  radial-gradient(900px 450px at 90% 0%, rgba(53,208,127,.25), transparent),
                  var(--bg);
      color:var(--text);
    }
    .topbar{
      position:sticky; top:0; z-index:50;
      background: linear-gradient(180deg, rgba(15,24,48,.95), rgba(10,16,32,.92));
      border-bottom: 1px solid var(--line);
      padding: 14px 18px;
      display:flex; align-items:center; justify-content:space-between;
      backdrop-filter: blur(10px);
    }
    .brand{
      display:flex; gap:10px; align-items:center;
    }
    .dot{
      width:12px; height:12px; border-radius:50%;
      background: var(--brand);
      box-shadow: 0 0 0 4px rgba(79,140,255,.15);
    }
    .brand h1{font-size:18px; margin:0; letter-spacing:.3px}
    .brand .sub{font-size:12px; color:var(--muted); margin-top:2px}
    .nav{
      display:flex; gap:10px; align-items:center;
    }
    .tab{
      padding:10px 14px;
      border-radius: 14px;
      border: 1px solid var(--line);
      background: rgba(255,255,255,.04);
      color: var(--text);
      text-decoration:none;
      font-weight:700;
      transition:.15s;
    }
    .tab:hover{transform: translateY(-1px); border-color: rgba(79,140,255,.35)}
    .tab.active{
      background: rgba(79,140,255,.18);
      border-color: rgba(79,140,255,.45);
    }
    .wrap{
      max-width: 1200px;
      margin: 18px auto;
      padding: 0 14px 28px 14px;
    }
    .grid{
      display:grid;
      grid-template-columns: 1.25fr .75fr;
      gap: 14px;
    }
    @media(max-width: 980px){
      .grid{grid-template-columns:1fr}
    }
    .card{
      background: linear-gradient(180deg, rgba(255,255,255,.05), rgba(255,255,255,.02));
      border: 1px solid var(--line);
      border-radius: var(--radius);
      box-shadow: var(--shadow);
      padding: 14px;
    }
    .card h2{margin:0 0 10px 0; font-size:16px; letter-spacing:.2px}
    .muted{color:var(--muted); font-size:13px}
    .kpi{
      display:grid; grid-template-columns: repeat(3,1fr);
      gap: 12px; margin-top: 12px;
    }
    .box{
      padding: 12px;
      border: 1px solid var(--line);
      border-radius: 14px;
      background: rgba(255,255,255,.04);
    }
    .box .big{font-size:28px; font-weight:900; margin-top:6px}
    .row{display:flex; gap:12px; flex-wrap:wrap; align-items:center}
    .formgrid{
      display:grid;
      grid-template-columns: 1fr 1fr;
      gap: 12px;
      margin-top: 12px;
    }
    @media(max-width: 720px){
      .formgrid{grid-template-columns:1fr}
    }
    label{display:block; font-size:12px; font-weight:800; color: var(--muted); margin-bottom:6px}
    input, select{
      width:100%;
      padding: 11px 12px;
      border-radius: 14px;
      border: 1px solid rgba(255,255,255,.12);
      background: rgba(10,16,32,.55);
      color: var(--text);
      outline:none;
    }
    input:focus, select:focus{border-color: rgba(79,140,255,.7)}
    .btn{
      padding: 11px 14px;
      border-radius: 14px;
      border: 1px solid rgba(79,140,255,.45);
      background: rgba(79,140,255,.18);
      color: var(--text);
      font-weight: 900;
      cursor:pointer;
      transition:.15s;
    }
    .btn:hover{transform: translateY(-1px); border-color: rgba(79,140,255,.75)}
    .hint{
      margin-top:10px;
      padding: 10px 12px;
      border-radius: 14px;
      border: 1px solid var(--line);
      background: rgba(255,255,255,.04);
      color: var(--muted);
      font-size: 13px;
      line-height: 1.4;
    }
    table{
      width:100%;
      border-collapse: collapse;
      overflow:hidden;
      border-radius: 14px;
    }
    th, td{
      text-align:left;
      padding: 10px 10px;
      border-bottom: 1px solid rgba(255,255,255,.08);
      font-size: 13px;
    }
    th{
      font-size:12px;
      color: var(--muted);
      text-transform: uppercase;
      letter-spacing: .8px;
      background: rgba(255,255,255,.03);
    }
    .scroll{max-height: 380px; overflow:auto; border-radius: 14px; border:1px solid var(--line)}
    .badge{
      display:inline-flex; align-items:center; gap:8px;
      padding: 6px 10px;
      border-radius: 999px;
      font-weight: 900;
      font-size: 12px;
      border: 1px solid var(--line);
      background: rgba(255,255,255,.04);
    }
    .b-good{color: var(--good); border-color: rgba(53,208,127,.35); background: rgba(53,208,127,.12)}
    .b-warn{color: var(--warn); border-color: rgba(255,176,32,.35); background: rgba(255,176,32,.12)}
    .b-bad{color: var(--bad);  border-color: rgba(255,95,109,.35); background: rgba(255,95,109,.12)}
    .spots{
      display:grid; grid-template-columns: repeat(4, 1fr);
      gap: 10px; margin-top: 10px;
    }
    .spot{
      border: 1px solid var(--line);
      border-radius: 14px;
      padding: 10px;
      background: rgba(255,255,255,.04);
      text-align:center;
      font-weight: 900;
    }
    .spot .s{font-size:12px; color: var(--muted); font-weight:800; margin-top:6px}
    .spot.empty{border-color: rgba(53,208,127,.35); background: rgba(53,208,127,.10)}
    .spot.reserved{border-color: rgba(255,176,32,.35); background: rgba(255,176,32,.10)}
    .spot.occupied{border-color: rgba(255,95,109,.35); background: rgba(255,95,109,.10)}
    .foot{
      margin-top:14px;
      font-size:12px;
      color: var(--muted);
      opacity:.9;
    }
    .rightcol .card{position:sticky; top:86px}
    .msg{
      margin-left:10px;
      font-size:13px;
      color: var(--muted);
    }
  </style>
</head>
<body>
  <div class="topbar">
    <div class="brand">
      <div class="dot"></div>
      <div>
        <h1>SMART PARKING</h1>
        <div class="sub">Đặt trước &amp; Lịch sử xe</div>
      </div>
    </div>
    <div class="nav">
      <a class="tab {{ 'active' if active=='reservations' else '' }}" href="/reservations">Đặt trước</a>
      <a class="tab {{ 'active' if active=='history' else '' }}" href="/history">Lịch sử xe</a>
      <a class="tab" href="/admin">Admin</a>
    </div>
  </div>

  <div class="wrap">
    {{ body|safe }}
  </div>
</body>
</html>
"""

def fmt_badge(status: str) -> str:
    st = (status or "").strip().lower()
    if st in ("reserved", "paid"):
        return '<span class="badge b-warn">Đã đặt</span>'
    if st in ("in", "arrived", "occupied"):
        return '<span class="badge b-good">Đang gửi</span>'
    if st in ("done", "out", "exit"):
        return '<span class="badge b-good">Hoàn tất</span>'
    if st in ("cancel", "canceled"):
        return '<span class="badge b-bad">Hủy</span>'
    return f'<span class="badge b-bad">{status}</span>'

# ===================== WEB SERVER =====================
def create_web_server(parking_app_ref):
    app = Flask(__name__)
    app.secret_key = "smart-parking-secret"

    @app.get("/")
    def root():
        return redirect("/reservations")

    @app.get("/reservations")
    def reservations_page():
        msg = request.args.get("msg","").strip()
        fee = parking_app_ref.fee_per_hour

        # dropdown spot states
        spots = parking_app_ref.get_spot_states_for_web()
        reservations = parking_app_ref.read_reservations()

        # KPI
        empty_n, reserved_n, occupied_n = parking_app_ref.web_kpi_counts()

        # spots display
        spots_html = []
        for s in spots:
            cls = "empty" if s["state"]=="empty" else ("reserved" if s["state"]=="reserved" else "occupied")
            label = s["spot"]
            sub = s["desc"]
            spots_html.append(f'<div class="spot {cls}"><div>{label}</div><div class="s">{sub}</div></div>')
        spots_block = f'<div class="spots">{"".join(spots_html)}</div>'

        # option html
        opt_html = []
        for s in spots:
            dis = "" if s["selectable"] else "disabled"
            opt_html.append(f'<option value="{s["spot"]}" {dis}>{s["spot"]} — {s["desc"]}</option>')
        opt_html = "\n".join(opt_html)

        # table rows
        rows = []
        for r in reservations[:250]:
            bid = r.get("id","")
            ten = r.get("ten","")
            sdt = r.get("sdt","")
            bs  = (r.get("bien_so","") or "").upper()
            spot= r.get("spot","")
            gio = r.get("gio_du_kien","")
            created = r.get("created_at","")
            status  = r.get("status","")
            arr = r.get("arrival_time","")
            ex  = r.get("exit_time","")
            fee_final = r.get("final_fee","")
            rows.append(
                "<tr>"
                f"<td>{bid}</td>"
                f"<td>{ten}</td>"
                f"<td>{sdt}</td>"
                f"<td><b>{bs}</b></td>"
                f"<td><b>{spot}</b></td>"
                f"<td>{gio}</td>"
                f"<td>{created}</td>"
                f"<td>{fmt_badge(status)}</td>"
                f"<td>{arr}</td>"
                f"<td>{ex}</td>"
                f"<td>{fee_final}</td>"
                "</tr>"
            )
        table_html = (
            '<div class="scroll" style="margin-top:10px">'
            '<table>'
            '<thead><tr>'
            '<th>ID</th><th>Tên</th><th>SĐT</th><th>Biển số</th><th>Ô</th><th>Giờ DK</th>'
            '<th>Created</th><th>Status</th><th>Arrival</th><th>Exit</th><th>Phí</th>'
            '</tr></thead>'
            f'<tbody>{"".join(rows) if rows else "<tr><td colspan=11 class=muted>Chưa có đặt trước.</td></tr>"}</tbody>'
            '</table></div>'
        )

        body = f"""
        <div class="grid">
          <div class="card">
            <h2>Đặt trước ô đỗ</h2>
            <div class="muted">Phí/giờ hiện tại: <b>{fee:,} VNĐ</b></div>

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
                  <label>Ô đỗ (Trống / Đã đặt / Có xe)</label>
                  <select name="spot" required>
                    {opt_html}
                  </select>
                </div>
                <div>
                  <label>Thời gian đỗ dự kiến (giờ)</label>
                  <input name="gio_du_kien" type="number" min="1" value="1" required>
                </div>
              </div>

              <div class="row" style="margin-top:12px">
                <button class="btn" type="submit">Xác nhận đặt trước</button>
                <div class="msg">{msg}</div>
              </div>

              <div class="hint">
                <b>Cách tính phí đặt trước:</b> tính từ thời điểm <b>xác nhận đặt</b> (created_at)
                → tới khi xe <b>rời bãi</b> (exit_time).<br>
                Nếu xe không có đặt trước thì tính từ lúc xe <b>vào bãi</b>.
              </div>
            </form>

            <div class="kpi">
              <div class="box"><div class="muted">Ô trống</div><div class="big">{empty_n}</div></div>
              <div class="box"><div class="muted">Đã đặt</div><div class="big">{reserved_n}</div></div>
              <div class="box"><div class="muted">Có xe</div><div class="big">{occupied_n}</div></div>
            </div>

            {spots_block}

            <div class="foot">Trang sẽ phản ánh trạng thái theo dữ liệu Python (RAM + CSV).</div>
          </div>

          <div class="rightcol">
            <div class="card">
              <h2>Danh sách đặt trước</h2>
              <div class="muted">Trạng thái: reserved (đã đặt), in (đã đến), done (đã rời), cancel.</div>
              {table_html}
            </div>
          </div>
        </div>
        """

        return render_template_string(WEB_BASE, body=body, active="reservations")

    @app.post("/reserve")
    def reserve_post():
        ten = (request.form.get("ten","") or "").strip()
        sdt = (request.form.get("sdt","") or "").strip()
        bien = (request.form.get("bien_so","") or "").strip().upper()
        spot = (request.form.get("spot","") or "").strip().upper()
        gio  = (request.form.get("gio_du_kien","1") or "1").strip()

        try:
            gio_i = max(1, int(gio))
        except:
            gio_i = 1

        ok, reason = parking_app_ref.add_reservation(ten, sdt, bien, spot, gio_i)
        if not ok:
            return redirect(f"/reservations?msg={reason}")
        return redirect("/reservations?msg=Đặt trước thành công!")

    @app.get("/history")
    def history_page():
        logs = parking_app_ref.read_logs()
        rows = []
        for r in logs[:400]:
            rows.append(
                "<tr>"
                f"<td>{r.get('ma_the','')}</td>"
                f"<td><b>{(r.get('bien_so','') or '').upper()}</b></td>"
                f"<td>{r.get('thoi_gian_vao','')}</td>"
                f"<td>{r.get('thoi_gian_ra','')}</td>"
                f"<td>{r.get('phi','')}</td>"
                f"<td>{r.get('bill_start','')}</td>"
                f"<td>{r.get('note','')}</td>"
                "</tr>"
            )

        body = f"""
        <div class="card">
          <h2>Lịch sử xe</h2>
          <div class="muted">Dữ liệu lấy từ <b>{CSV_LOG}</b></div>
          <div class="scroll" style="margin-top:12px">
            <table>
              <thead>
                <tr>
                  <th>Mã thẻ</th><th>Biển số</th><th>Giờ vào</th><th>Giờ ra</th><th>Phí</th><th>Bill start</th><th>Ghi chú</th>
                </tr>
              </thead>
              <tbody>
                {"".join(rows) if rows else "<tr><td colspan=7 class=muted>Chưa có lịch sử.</td></tr>"}
              </tbody>
            </table>
          </div>
          <div class="foot">Nếu xe có đặt trước: bill_start = created_at (lúc đặt). Nếu không: bill_start = entry_time (lúc vào).</div>
        </div>
        """
        return render_template_string(WEB_BASE, body=body, active="history")

    # ---------------- Admin ----------------
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

        body = f"""
        <div class="card" style="max-width:520px;margin:20px auto">
          <h2>Admin Login</h2>
          <form method="POST" style="margin-top:10px">
            <div class="formgrid" style="grid-template-columns:1fr">
              <div><label>Tài khoản</label><input name="u" required></div>
              <div><label>Mật khẩu</label><input name="p" type="password" required></div>
            </div>
            <div class="row" style="margin-top:12px">
              <button class="btn" type="submit">Đăng nhập</button>
              <div class="msg">{msg}</div>
            </div>
          </form>
          <div class="foot"><a class="tab" href="/reservations">← Quay lại</a></div>
        </div>
        """
        return render_template_string(WEB_BASE, body=body, active="")

    @app.get("/admin/logout")
    def admin_logout():
        session.clear()
        return redirect("/reservations")

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
            try:
                fee_per_hour_i = max(0, int(fee_per_hour))
            except:
                fee_per_hour_i = DEFAULT_FEE_PER_HOUR

            st["fee_per_hour"] = fee_per_hour_i
            st["com_port"] = com_port
            st["cam_in"] = cam_in
            st["cam_out"] = cam_out
            write_settings(st)
            msg = "Đã lưu."

            parking_app_ref.on_settings_changed(st)

        st = read_settings()
        body = f"""
        <div class="card" style="max-width:860px;margin:20px auto">
          <h2>Admin Settings</h2>
          <div class="muted">Chỉnh phí, camera và COM. Python sẽ áp dụng ngay.</div>
          <form method="POST" style="margin-top:12px">
            <div class="formgrid">
              <div><label>Phí gửi xe (VNĐ/giờ)</label><input name="fee_per_hour" type="number" min="0" value="{st['fee_per_hour']}" required></div>
              <div><label>COM Arduino MASTER</label><input name="com_port" value="{st.get('com_port','')}" placeholder="VD: COM5"></div>
              <div><label>Camera vào</label><input name="cam_in" value="{st.get('cam_in','0')}" placeholder="VD: 0"></div>
              <div><label>Camera ra</label><input name="cam_out" value="{st.get('cam_out','1')}" placeholder="VD: 1"></div>
            </div>
            <div class="row" style="margin-top:12px">
              <button class="btn" type="submit">Lưu</button>
              <div class="msg">{msg}</div>
              <a class="tab" href="/admin/logout">Đăng xuất</a>
            </div>
          </form>
        </div>
        """
        return render_template_string(WEB_BASE, body=body, active="")

    return app

# ===================== MAIN APP =====================
class ParkingApp:
    def __init__(self, window, title):
        self.window = window
        self.window.title(title)
        self.window.configure(bg='#e6f0ff')

        style = ttk.Style(self.window)
        try:
            style.theme_use('clam')
        except:
            pass
        style.configure("TLabelFrame", borderwidth=0, background='#e6f0ff')
        style.configure("TLabelFrame.Label", foreground="blue", background='#e6f0ff', font=("Helvetica", 11, "bold"))
        style.configure("TButton", font=("Helvetica", 10))

        self.toast = Toast(self.window)

        # dữ liệu bãi (RAM)
        self.parking_spots = {s: None for s in SPOT_TO_TARGET.keys()}
        self.spot_labels = {}

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
        self._cam_fail_in = 0
        self._cam_fail_out = 0
        self.static_frame_in = None
        self.static_frame_out = None


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

        # trạng thái vị trí bãi (mặc định đang ở 1)
        self.master_pos = 1

        # locks
        self.data_lock  = threading.Lock()
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
        self.settings = read_settings()
        self.fee_per_hour = int(self.settings.get("fee_per_hour", DEFAULT_FEE_PER_HOUR))
        self.toast.show("Đã áp dụng settings mới từ Web.", 1800)

        self.source_in = self._parse_cam_source(self.settings.get("cam_in","0"))
        self.source_out = self._parse_cam_source(self.settings.get("cam_out","1"))
        self._reopen_cams()

        com = self.settings.get("com_port","")
        if com:
            self.start_master_listener(com, 9600)

    def _parse_cam_source(self, s):
        s = str(s).strip()
        if s.isdigit():
            return int(s)
        return s

    # ---------- UI ----------
    def create_menu(self):
        menubar = tk.Menu(self.window)
        self.window.config(menu=menubar)

        m_file = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Tệp", menu=m_file)
        m_file.add_command(label="Chọn nguồn tạm thời cho Camera Vào...", command=lambda: self.select_media_source('in'))
        m_file.add_command(label="Chọn nguồn tạm thời cho Camera Ra...",  command=lambda: self.select_media_source('out'))
        m_file.add_separator()
        m_file.add_command(label="Thoát", command=self.on_closing)

        m_opt = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Tùy chọn", menu=m_opt)
        m_opt.add_command(label="Cài đặt", command=self.open_settings_window)

        m_web = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Web", menu=m_web)
        m_web.add_command(label="Mở Web: http://127.0.0.1:5000/reservations",
                          command=lambda: self.toast.show("Mở trình duyệt: http://127.0.0.1:5000/reservations", 2200))
        m_web.add_command(label="Mở Lịch sử: http://127.0.0.1:5000/history",
                          command=lambda: self.toast.show("Mở trình duyệt: http://127.0.0.1:5000/history", 2200))

    def create_widgets(self):
        main = tk.Frame(self.window, bg='#e6f0ff')
        main.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        left = tk.Frame(main, bg='#e6f0ff')
        left.pack(side=tk.LEFT, fill=tk.Y, expand=False, padx=(0,5))

        right = tk.Frame(main, bg='#e6f0ff')
        right.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True, padx=(5,0))

        self.create_left_pane_widgets(left)
        self.create_right_pane_widgets(right)

    def create_left_pane_widgets(self, parent):
        FIX_W, FIX_H = 500, 375
        parent.columnconfigure(0, minsize=FIX_W)
        parent.columnconfigure(1, minsize=FIX_W)
        parent.rowconfigure(0, minsize=FIX_H)
        parent.rowconfigure(1, minsize=FIX_H)

        self.placeholder_video = self._placeholder_imgtk(FIX_W, FIX_H)

        f1 = self._lframe(parent, "Camera ngõ vào")
        f1.grid(row=0, column=0, sticky="nsew", padx=5, pady=5)
        f1.pack_propagate(False)
        self.label_cam_in = tk.Label(f1, image=self.placeholder_video, bg='white')
        self.label_cam_in.pack(fill=tk.BOTH, expand=True)

        f2 = self._lframe(parent, "Ảnh xe vào")
        f2.grid(row=0, column=1, sticky="nsew", padx=5, pady=5)
        f2.pack_propagate(False)
        self.label_img_in = tk.Label(f2, image=self.placeholder_video, bg='white')
        self.label_img_in.pack(fill=tk.BOTH, expand=True)

        f3 = self._lframe(parent, "Camera ngõ ra")
        f3.grid(row=1, column=0, sticky="nsew", padx=5, pady=5)
        f3.pack_propagate(False)
        self.label_cam_out = tk.Label(f3, image=self.placeholder_video, bg='white')
        self.label_cam_out.pack(fill=tk.BOTH, expand=True)

        f4 = self._lframe(parent, "Ảnh xe ra")
        f4.grid(row=1, column=1, sticky="nsew", padx=5, pady=5)
        f4.pack_propagate(False)
        self.label_img_out = tk.Label(f4, image=self.placeholder_video, bg='white')
        self.label_img_out.pack(fill=tk.BOTH, expand=True)

    def create_right_pane_widgets(self, parent):
        pf = self._lframe(parent, "Thông tin biển số")
        pf.pack(fill=tk.X, pady=(0,5), ipady=5)
        self._populate_plate_frame(pf)

        tf = self._lframe(parent, "Thời gian & Chi phí")
        tf.pack(fill=tk.X, pady=5, ipady=5)
        self._populate_time_cost_frame(tf)

        sf = self._lframe(parent, "Trạng thái bãi xe")
        sf.pack(fill=tk.X, pady=5, ipady=5)
        self._populate_spots_frame(sf)

        nb = ttk.Notebook(parent)
        nb.pack(fill=tk.BOTH, expand=True, pady=(10,0))
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
        self.label_plate_in = tk.Label(parent, image=self.placeholder_plate, bg='white')
        self.label_plate_in.grid(row=1, column=0)
        self.plate_in_var = tk.StringVar(value="---")
        tk.Label(parent, textvariable=self.plate_in_var, font=("Helvetica",12,"bold"), bg='#dcdad5').grid(row=2, column=0, pady=(0,5))

        self.match_status_var = tk.StringVar(value="")
        tk.Label(parent, textvariable=self.match_status_var, font=("Helvetica",12,"bold","italic"), fg="green", bg='#dcdad5').grid(row=1, column=1)

        tk.Label(parent, text="Biển số xe ra", font=("Helvetica",10,"bold"), bg='#dcdad5').grid(row=0, column=2, pady=(5,0))
        self.label_plate_out = tk.Label(parent, image=self.placeholder_plate, bg='white')
        self.label_plate_out.grid(row=1, column=2)
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

        bf = tk.Frame(parent, bg='#dcdad5')
        bf.pack(pady=5)
        ttk.Button(bf, text="Xác nhận vào (Thủ công)", command=self.capture_in).pack(side=tk.LEFT, padx=10)
        ttk.Button(bf, text="Xác nhận ra (Thủ công)",  command=self.capture_out).pack(side=tk.LEFT, padx=10)

    def _populate_reserved_tab(self, parent):
        cols = ('ID','Tên','SĐT','Biển số','Ô','Giờ DK','Created','Status','Arrival','Exit','Phí')
        self.tree_reserved = ttk.Treeview(parent, columns=cols, show='headings')
        for c in cols:
            self.tree_reserved.heading(c, text=c)
            self.tree_reserved.column(c, width=110, anchor=tk.CENTER)
        self.tree_reserved.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb = ttk.Scrollbar(parent, orient="vertical", command=self.tree_reserved.yview)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree_reserved.configure(yscrollcommand=sb.set)

    def _populate_log_tab(self, parent):
        cols = ('Mã Thẻ','Biển số','Giờ vào','Giờ ra','Phí','Bill start','Ghi chú')
        self.tree_log = ttk.Treeview(parent, columns=cols, show='headings')
        for c in cols:
            self.tree_log.heading(c, text=c)
        self.tree_log.column('Mã Thẻ', width=100, anchor=tk.CENTER)
        self.tree_log.column('Biển số', width=120, anchor=tk.CENTER)
        self.tree_log.column('Giờ vào', width=160, anchor=tk.CENTER)
        self.tree_log.column('Giờ ra', width=160, anchor=tk.CENTER)
        self.tree_log.column('Phí', width=120, anchor=tk.E)
        self.tree_log.column('Bill start', width=160, anchor=tk.CENTER)
        self.tree_log.column('Ghi chú', width=180, anchor=tk.W)
        self.tree_log.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb = ttk.Scrollbar(parent, orient="vertical", command=self.tree_log.yview)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree_log.configure(yscrollcommand=sb.set)

    # ---------- Main loop ----------
    def update_loop(self):
        self.clock_var.set(vn_clock_str())

        self._process_in_events()
        self._process_out_events()

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
        self._process_vehicle_entry(self.last_frame_in, rfid_uid="NO_CARD")

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
                # OCR ngay
                plate_text, crop_img = self._ocr_plate_now(frame)
                if plate_text == "unknown":
                    self._ui(lambda: self.toast.show("Không nhận diện được biển số xe vào.", 2000))
                    return

                # nếu biển đã có trong bãi -> không cho vào
                found_spot, _ = self._find_vehicle_by_plate(plate_text)
                if found_spot:
                    self._ui(lambda: self.toast.show(f"Biển số {plate_text} đã có ở {found_spot}.", 2200))
                    return

                # nếu có đặt trước đúng biển -> dùng đúng spot đó
                reserved = self._find_reservation_by_plate(plate_text, allow_status=("reserved",))
                if reserved:
                    spot_id = reserved["spot"]
                else:
                    spot_id = self._find_empty_spot()
                    if not spot_id:
                        self._ui(lambda: self.toast.show("Bãi đã đầy.", 2000))
                        return

                target_num = SPOT_TO_TARGET.get(spot_id, 0)
                if not target_num:
                    self._ui(lambda: self.toast.show("Lỗi mapping ô đỗ.", 2000))
                    return

                # quay tới target (chỉ quay nếu khác vị trí hiện tại)
                ok = self._move_and_wait_arrived(target_num)
                if not ok:
                    self._ui(lambda: self.toast.show("Quay vị trí thất bại (timeout).", 2200))
                    return

                # beep 1 lần khi quay xong
                self._send_master("BEEP:1")
                time.sleep(0.05)

                # mở cổng vào
                self._send_master("OPEN_IN")

                entry_time = datetime.now()

                # bill_start: nếu đặt trước -> created_at, else entry_time
                reservation_id = ""
                bill_start_time = entry_time
                note = "normal"
                if reserved:
                    reservation_id = reserved["id"]
                    bill_start_time = reserved["created_at_dt"]
                    note = "reserved"

                    # update reservation arrival/status
                    self._update_reservation_on_arrival(reservation_id, entry_time)

                veh = {
                    'plate_text': plate_text,
                    'entry_time': entry_time,
                    'bill_start_time': bill_start_time,
                    'plate_image': self._pil_from_bgr(crop_img) if crop_img is not None else self._placeholder_pil(150, 75),
                    'vehicle_image': self._pil_from_bgr(frame),
                    'status': 'occupied',
                    'rfid_uid': rfid_uid if rfid_uid else "NO_CARD",
                    'reservation_id': reservation_id,
                    'note': note,
                }

                def apply():
                    with self.data_lock:
                        self.parking_spots[spot_id] = veh
                        self.save_spots_to_csv()

                    self.apply_reservations_to_spots()  # refresh reserved overlay
                    self.update_spot_display()

                    self._reset_exit_info()

                    self._set_img(self.label_img_in,  veh['vehicle_image'])
                    self._set_img(self.label_plate_in, veh['plate_image'])
                    self.plate_in_var.set(plate_text)
                    self.match_status_var.set("")
                    self.toast.show(f"Xe {plate_text} đã vào {spot_id}", 1800)

                    self._schedule_reset_display()
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
                plate_out, crop_out = self._ocr_plate_now(frame)
                if plate_out == "unknown":
                    self._ui(lambda: self.toast.show("Không nhận diện được biển số xe ra.", 2000))
                    return

                spot_id, veh_in = self._find_vehicle_by_plate(plate_out)
                if not spot_id:
                    self._ui(lambda: self.toast.show(f"Không tìm thấy xe {plate_out} trong bãi.", 2200))
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
                spot_id, veh_in = self._find_vehicle_by_rfid(rfid_uid)
                if not spot_id:
                    self._ui(lambda: self.toast.show(f"Không có xe dùng thẻ {rfid_uid}", 2200))
                    return

                plate_out, crop_out = self._ocr_plate_now(frame)
                if plate_out == "unknown":
                    self._ui(lambda: self.toast.show("Không nhận diện được biển số xe ra.", 2000))
                    return

                # check mismatch
                if plate_out != veh_in['plate_text']:
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
                    self._ui(mismatch)
                    return

                self._ui(lambda: self._finalize_exit_flow(spot_id, veh_in, frame, crop_out, rfid_uid=rfid_uid))
            finally:
                self._ui(lambda: setattr(self, 'exit_busy', False))
        threading.Thread(target=worker, daemon=True).start()

    def _finalize_exit_flow(self, spot_id, veh_in, frame_out, crop_img_out, rfid_uid):
        plate = veh_in['plate_text']

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

        # LCD OUT: hiển thị biển số
        self._send_master(f"OUT,{plate}")
        time.sleep(0.05)

        ok = self._move_and_wait_arrived(target_num)
        if not ok:
            self.toast.show("Quay vị trí xe ra thất bại (timeout).", 2200)
            return

        # beep 1 lần khi quay xong
        self._send_master("BEEP:1")
        time.sleep(0.05)

        # mở servo OUT
        self._send_master("OPEN_OUT")

        # tính phí:
        # - nếu có đặt trước -> bill_start_time = created_at (lúc đặt)
        # - nếu không -> bill_start_time = entry_time (lúc vào)
        exit_time = datetime.now()
        bill_start = veh_in.get('bill_start_time', veh_in.get('entry_time', datetime.now()))
        duration = exit_time - bill_start

        raw_fee = (duration.total_seconds() / 3600.0) * self.fee_per_hour
        final_fee = int(math.ceil(raw_fee/1000)*1000) if raw_fee > 0 else 0

        secs = int(duration.total_seconds())
        h, r = divmod(secs, 3600)
        m, s = divmod(r, 60)
        self.duration_var.set(f"Thời gian tính phí: {h:02d}:{m:02d}:{s:02d}")
        self.fee_var.set(f"Phí gửi xe: {final_fee:,} VNĐ".replace(",", "."))

        # update reservation done if any
        reservation_id = veh_in.get("reservation_id","").strip()
        if reservation_id:
            self._update_reservation_on_exit(reservation_id, exit_time, final_fee)

        # log CSV
        self._log_exit({
            'ma_the': veh_in.get('rfid_uid','N/A'),
            'bien_so': plate,
            'thoi_gian_vao': veh_in.get('entry_time', datetime.now()).strftime("%Y-%m-%d %H:%M:%S"),
            'thoi_gian_ra': exit_time.strftime("%Y-%m-%d %H:%M:%S"),
            'phi': f"{final_fee:,} VNĐ".replace(",", "."),
            'bill_start': bill_start.strftime("%Y-%m-%d %H:%M:%S") if isinstance(bill_start, datetime) else str(bill_start),
            'note': "reserved" if reservation_id else "normal"
        })

        # clear spot
        with self.data_lock:
            self.parking_spots[spot_id] = None
            self.save_spots_to_csv()

        self.apply_reservations_to_spots()
        self.update_spot_display()
        self.load_log_from_csv()
        self.load_reserved_list_from_csv()

        self.toast.show(f"Xe {plate} rời {spot_id}. Phí: {final_fee:,}đ".replace(",", "."), 2400)
        self._schedule_reset_display()

    # ---------- MOVE + WAIT ARRIVED ----------
    def _drain_arrived_queue(self):
        while True:
            try:
                self.arrived_queue.get_nowait()
            except queue.Empty:
                break

    def _move_and_wait_arrived(self, target_num: int) -> bool:
        # nếu đang ở sẵn target -> không cần chờ ARRIVED
        if int(target_num) == int(self.master_pos):
            return True

        self._drain_arrived_queue()
        self._send_master(str(target_num))

        t0 = time.time()
        while time.time() - t0 < ARRIVED_TIMEOUT_SEC:
            try:
                arrived = self.arrived_queue.get(timeout=0.2)
                if int(arrived) == int(target_num):
                    self.master_pos = int(arrived)
                    return True
            except queue.Empty:
                pass
        return False

    # ---------- OCR (NO TIMEOUT) ----------
    def _ocr_plate_now(self, frame):
        """Nhận diện ngay 1 lần. Nếu fail -> unknown."""
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

        x, y, x2, y2 = map(int, lst[0][:4])
        x = max(0, x); y = max(0, y)
        x2 = max(x+1, x2); y2 = max(y+1, y2)
        crop = frame[y:y2, x:x2]

        # thử vài biến thể (nhanh) nhưng không timeout
        tries = [(0,0), (0,1), (1,0), (1,1)]
        for cc, ct in tries:
            try:
                lp = helper.read_plate(yolo_license_plate, utils_rotate.deskew(crop, cc, ct))
                if lp != "unknown" and lp:
                    plate = str(lp).strip().upper()
                    break
            except Exception:
                pass

        return plate, crop

    # ---------- Serial MASTER ----------
    def start_master_listener(self, com_port, baud=9600):
        com_port = (com_port or "").strip()
        if not com_port or "Không tìm thấy" in com_port:
            self.toast.show("Chọn cổng COM hợp lệ.", 2000)
            return

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

                if line == self.last_serial_line and (now_ms()-self.last_serial_time_ms) < SERIAL_SAME_LINE_COOLDOWN_MS:
                    continue
                self.last_serial_line = line
                self.last_serial_time_ms = now_ms()

                if line.startswith("RFID_IN:"):
                    uid = line.split("RFID_IN:",1)[1].strip()
                    if self._uid_ok('in', uid):
                        self.rfid_queue_in.put(uid)

                elif line.startswith("RFID_OUT:"):
                    uid = line.split("RFID_OUT:",1)[1].strip()
                    if self._uid_ok('out', uid):
                        self.rfid_queue_out.put(uid)

                elif "TOUCH_IN" in line:
                    self.touch_queue_in.put(True)

                elif "TOUCH_OUT" in line:
                    self.touch_queue_out.put(True)

                elif line.startswith("ARRIVED:"):
                    try:
                        n = int(line.split("ARRIVED:",1)[1].strip())
                        self.arrived_queue.put(n)
                        self.master_pos = n
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
                print("[PC→MASTER]", text)
            else:
                print("[PC→MASTER] Chưa kết nối.")
        except Exception as e:
            print("Gửi lệnh lỗi:", e)

    # ---------- Settings window (Tkinter) ----------
    def open_settings_window(self):
        w = tk.Toplevel(self.window)
        w.title("Cài đặt")
        w.configure(bg='#e6f0ff')
        w.resizable(False, False)

        cams = self._find_cams()
        ports = self._find_coms()

        cf = self._lframe(w, "Chọn camera")
        cf.pack(padx=20,pady=10,fill="x")
        tk.Label(cf, text="Camera vào:", bg='#dcdad5').grid(row=0,column=0,sticky="w",padx=5,pady=5)
        cam_in_var = tk.StringVar(value=str(self.settings.get("cam_in","0")))
        cb_in = ttk.Combobox(cf, textvariable=cam_in_var, values=cams, state="readonly", width=20)
        cb_in.grid(row=0,column=1,padx=5,pady=5)

        tk.Label(cf, text="Camera ra:", bg='#dcdad5').grid(row=1,column=0,sticky="w",padx=5,pady=5)
        cam_out_var = tk.StringVar(value=str(self.settings.get("cam_out","1")))
        cb_out = ttk.Combobox(cf, textvariable=cam_out_var, values=cams, state="readonly", width=20)
        cb_out.grid(row=1,column=1,padx=5,pady=5)

        def apply_cams():
            def parse_cam(x):
                x = str(x)
                if x.startswith("Camera "):
                    try:
                        return int(x.split("Camera ",1)[1].strip())
                    except:
                        return 0
                return x
            self.settings["cam_in"] = str(parse_cam(cam_in_var.get()))
            self.settings["cam_out"] = str(parse_cam(cam_out_var.get()))
            write_settings(self.settings)
            self.on_settings_changed(self.settings)
            self.toast.show("Đã áp dụng camera.", 1600)

        ttk.Button(cf, text="Áp dụng", command=apply_cams).grid(row=0,rowspan=2,column=2,padx=10,pady=10)

        sf = self._lframe(w, "Kết nối Arduino MASTER")
        sf.pack(padx=20,pady=10,fill="x")
        com_var = tk.StringVar(value=self.settings.get("com_port",""))
        tk.Label(sf, text="Cổng COM:", bg='#dcdad5').grid(row=0,column=0,sticky="w",padx=5,pady=5)
        cb = ttk.Combobox(sf, textvariable=com_var, values=ports, state="readonly", width=20)
        cb.grid(row=0,column=1,padx=5,pady=5)

        def connect():
            self.settings["com_port"] = com_var.get()
            write_settings(self.settings)
            self.start_master_listener(com_var.get(), 9600)

        ttk.Button(sf, text="Kết nối", command=connect).grid(row=0,column=2,padx=10,pady=5)

        ff = self._lframe(w, "Cài đặt phí (VNĐ/giờ)")
        ff.pack(padx=20,pady=10,fill="x")
        fee_var = tk.StringVar(value=str(self.fee_per_hour))
        e = ttk.Entry(ff, textvariable=fee_var, width=20)
        e.pack(side=tk.LEFT,padx=10,pady=10)

        def save_fee():
            try:
                v = int(fee_var.get())
                if v < 0:
                    raise ValueError
                self.fee_per_hour = v
                self.settings["fee_per_hour"] = str(v)
                write_settings(self.settings)
                self.toast.show(f"Đã cập nhật phí: {v:,} VNĐ/giờ".replace(",", "."), 2000)
            except:
                self.toast.show("Phí không hợp lệ.", 1800)

        ttk.Button(ff, text="Lưu Phí", command=save_fee).pack(side=tk.LEFT,padx=10,pady=10)

    # ---------- Camera ----------
    def init_capture_devices(self):
        if self.vid_in:
            try:
                self.vid_in.release()
            except:
                pass
        if self.vid_out:
            try:
                self.vid_out.release()
            except:
                pass

        backend = cv2.CAP_DSHOW if os.name == "nt" else cv2.CAP_ANY

        self.vid_in  = cv2.VideoCapture(self.source_in, backend)
        self.vid_out = cv2.VideoCapture(self.source_out, backend)

        try:
            self.vid_in.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            self.vid_out.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        except Exception:
            pass

        if not self.vid_in.isOpened():
            print("Không mở được camera vào:", self.source_in)
        if not self.vid_out.isOpened():
            print("Không mở được camera ra:", self.source_out)

    def _reopen_cams(self):
        self.init_capture_devices()

    def _get_frame(self, cap, channel="in"):
        if cap is None or not cap.isOpened():
            return None
        ret, frame = cap.read()
        if not ret:
            if channel == "in":
                self._cam_fail_in += 1
                if self._cam_fail_in > 30:
                    self._cam_fail_in = 0
                    self._reopen_cams()
            else:
                self._cam_fail_out += 1
                if self._cam_fail_out > 30:
                    self._cam_fail_out = 0
                    self._reopen_cams()
            return None

        if channel == "in":
            self._cam_fail_in = 0
        else:
            self._cam_fail_out = 0
        return frame

    def _update_video_label(self, label, frame):
        self._set_img(label, self._pil_from_bgr(frame))

    # ---------- Web helpers ----------
    def get_spot_states_for_web(self):
        """Dùng cho dropdown & hiển thị web."""
        items = []
        with self.data_lock:
            for sid, v in self.parking_spots.items():
                if v is None:
                    items.append({"spot": sid, "state":"empty", "desc":"Trống ✅", "selectable": True})
                else:
                    st = v.get("status","occupied")
                    plate = v.get("plate_text","")
                    if st == "reserved":
                        items.append({"spot": sid, "state":"reserved", "desc": f"Đã đặt ({plate})", "selectable": False})
                    else:
                        items.append({"spot": sid, "state":"occupied", "desc": f"Có xe ({plate})", "selectable": False})
        return items

    def web_kpi_counts(self):
        empty_n = reserved_n = occupied_n = 0
        with self.data_lock:
            for _, v in self.parking_spots.items():
                if v is None:
                    empty_n += 1
                else:
                    if v.get("status") == "reserved":
                        reserved_n += 1
                    else:
                        occupied_n += 1
        return empty_n, reserved_n, occupied_n

    def read_reservations(self):
        ensure_csv_reserved()
        res = []
        try:
            with open(CSV_RESERVED, "r", newline="", encoding="utf-8") as f:
                rd = csv.DictReader(f)
                for r in rd:
                    res.append(r)
        except Exception:
            pass
        res.reverse()
        return res

    def read_logs(self):
        ensure_csv_log()
        logs = []
        try:
            with open(CSV_LOG, "r", newline="", encoding="utf-8") as f:
                rd = csv.DictReader(f)
                for r in rd:
                    logs.append(r)
        except Exception:
            pass
        logs.reverse()
        return logs

    def add_reservation(self, ten, sdt, plate, spot, gio_du_kien):
        plate = (plate or "").strip().upper()
        spot = (spot or "").strip().upper()

        if not ten or not sdt or not plate:
            return False, "Thiếu thông tin."

        if spot not in self.parking_spots:
            return False, "Ô đỗ không hợp lệ."

        # spot phải trống (không reserved / không occupied)
        with self.data_lock:
            if self.parking_spots.get(spot) is not None:
                return False, "Ô đỗ không còn trống."

        # plate không được đang ở trong bãi
        found_spot, _ = self._find_vehicle_by_plate(plate)
        if found_spot:
            return False, f"Xe {plate} đang ở trong bãi ({found_spot})."

        # plate không được có reservation reserved khác
        rs = self.read_reservations()
        for r in rs:
            if (r.get("bien_so","") or "").upper() == plate and (r.get("status","") or "").strip().lower() in ("reserved","in"):
                return False, "Biển số đã có đặt trước/đang gửi."

        rid = str(int(time.time()*1000))
        created = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        status = "reserved"

        ensure_csv_reserved()
        with open(CSV_RESERVED, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow([rid, ten, sdt, plate, spot, str(gio_du_kien), created, status, "", "", ""])

        # apply vào RAM để tô màu reserved ngay
        self._ui(lambda: self.apply_reservations_to_spots())
        self._ui(lambda: self.load_reserved_list_from_csv())
        return True, "OK"

    def apply_reservations_to_spots(self):
        """Overlay reservation vào RAM: nếu spot trống -> đặt status reserved."""
        reservations = self.read_reservations()

        with self.data_lock:
            # clear reserved overlay cũ
            for sid, v in list(self.parking_spots.items()):
                if v and v.get("status") == "reserved":
                    self.parking_spots[sid] = None

            for r in reservations:
                st = (r.get("status","") or "").strip().lower()
                if st != "reserved":
                    continue
                spot = (r.get("spot","") or "").strip().upper()
                plate = (r.get("bien_so","") or "").strip().upper()
                if spot in self.parking_spots and self.parking_spots[spot] is None:
                    self.parking_spots[spot] = {
                        "plate_text": plate,
                        "status": "reserved",
                        "vehicle_image": self._placeholder_pil(500,375),
                        "plate_image": self._placeholder_pil(150,75),
                        "rfid_uid": "RESERVED",
                        "entry_time": datetime.now(),
                        "bill_start_time": datetime.now(),
                        "reservation_id": r.get("id",""),
                        "note": "reserved_overlay"
                    }

            self.update_spot_display()
            self.save_spots_to_csv()

    def _find_reservation_by_plate(self, plate_text, allow_status=("reserved",)):
        plate_text = (plate_text or "").strip().upper()
        rs = self.read_reservations()
        for r in rs:
            if (r.get("bien_so","") or "").strip().upper() != plate_text:
                continue
            st = (r.get("status","") or "").strip().lower()
            if st not in allow_status:
                continue
            # parse created_at
            created_s = r.get("created_at","")
            try:
                created_dt = datetime.strptime(created_s, "%Y-%m-%d %H:%M:%S")
            except:
                created_dt = datetime.now()
            return {
                "id": r.get("id",""),
                "spot": (r.get("spot","") or "").strip().upper(),
                "created_at_dt": created_dt
            }
        return None

    def _update_reservation_on_arrival(self, rid, arrival_time: datetime):
        ensure_csv_reserved()
        rows = []
        try:
            with open(CSV_RESERVED, "r", newline="", encoding="utf-8") as f:
                rd = csv.DictReader(f)
                for r in rd:
                    rows.append(r)
        except Exception:
            return

        for r in rows:
            if r.get("id","") == rid:
                r["status"] = "in"
                r["arrival_time"] = arrival_time.strftime("%Y-%m-%d %H:%M:%S")

        with open(CSV_RESERVED, "w", newline="", encoding="utf-8") as f:
            fn = ["id","ten","sdt","bien_so","spot","gio_du_kien","created_at","status","arrival_time","exit_time","final_fee"]
            w = csv.DictWriter(f, fieldnames=fn)
            w.writeheader()
            for r in rows:
                w.writerow({k: r.get(k,"") for k in fn})

    def _update_reservation_on_exit(self, rid, exit_time: datetime, final_fee: int):
        ensure_csv_reserved()
        rows = []
        try:
            with open(CSV_RESERVED, "r", newline="", encoding="utf-8") as f:
                rd = csv.DictReader(f)
                for r in rd:
                    rows.append(r)
        except Exception:
            return

        for r in rows:
            if r.get("id","") == rid:
                r["status"] = "done"
                r["exit_time"] = exit_time.strftime("%Y-%m-%d %H:%M:%S")
                r["final_fee"] = f"{final_fee:,} VNĐ".replace(",", ".")

        with open(CSV_RESERVED, "w", newline="", encoding="utf-8") as f:
            fn = ["id","ten","sdt","bien_so","spot","gio_du_kien","created_at","status","arrival_time","exit_time","final_fee"]
            w = csv.DictWriter(f, fieldnames=fn)
            w.writeheader()
            for r in rows:
                w.writerow({k: r.get(k,"") for k in fn})

    # ---------- CSV spots persistence ----------
    def save_spots_to_csv(self):
        ensure_csv_spots()
        try:
            with open(CSV_SPOTS, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["spot","status","plate","rfid_uid","entry_time","bill_start_time","reservation_id"])
                for sid in SPOT_TO_TARGET.keys():
                    v = self.parking_spots.get(sid)
                    if v is None:
                        w.writerow([sid,"empty","","","","",""])
                    else:
                        st = v.get("status","occupied")
                        plate = v.get("plate_text","")
                        uid = v.get("rfid_uid","")
                        et = ""
                        bs = ""
                        if isinstance(v.get("entry_time"), datetime):
                            et = v["entry_time"].strftime("%Y-%m-%d %H:%M:%S")
                        if isinstance(v.get("bill_start_time"), datetime):
                            bs = v["bill_start_time"].strftime("%Y-%m-%d %H:%M:%S")
                        rid = v.get("reservation_id","")
                        w.writerow([sid, st, plate, uid, et, bs, rid])
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
                    sid = (r.get("spot","") or "").strip().upper()
                    st  = (r.get("status","empty") or "empty").strip().lower()
                    if sid not in self.parking_spots:
                        continue

                    if st == "empty":
                        self.parking_spots[sid] = None
                    else:
                        plate = (r.get("plate","") or "").upper()
                        uid = (r.get("rfid_uid","") or "").strip()
                        et_str = (r.get("entry_time","") or "").strip()
                        bs_str = (r.get("bill_start_time","") or "").strip()
                        rid = (r.get("reservation_id","") or "").strip()

                        try:
                            et = datetime.strptime(et_str, "%Y-%m-%d %H:%M:%S") if et_str else datetime.now()
                        except:
                            et = datetime.now()
                        try:
                            bs = datetime.strptime(bs_str, "%Y-%m-%d %H:%M:%S") if bs_str else et
                        except:
                            bs = et

                        self.parking_spots[sid] = {
                            "plate_text": plate,
                            "status": st if st in ("reserved","occupied") else "occupied",
                            "vehicle_image": self._placeholder_pil(500,375),
                            "plate_image": self._placeholder_pil(150,75),
                            "rfid_uid": uid,
                            "entry_time": et,
                            "bill_start_time": bs,
                            "reservation_id": rid,
                            "note": "loaded"
                        }
        except Exception as e:
            print("Load vi_tri_do.csv lỗi:", e)

    # ---------- Reserved list & log list ----------
    def load_reserved_list_from_csv(self):
        ensure_csv_reserved()
        for it in self.tree_reserved.get_children():
            self.tree_reserved.delete(it)
        try:
            with open(CSV_RESERVED, "r", newline="", encoding="utf-8") as f:
                rd = csv.DictReader(f)
                for r in rd:
                    self.tree_reserved.insert("", 0, values=(
                        r.get("id",""),
                        r.get("ten",""),
                        r.get("sdt",""),
                        (r.get("bien_so","") or "").upper(),
                        r.get("spot",""),
                        r.get("gio_du_kien",""),
                        r.get("created_at",""),
                        r.get("status",""),
                        r.get("arrival_time",""),
                        r.get("exit_time",""),
                        r.get("final_fee",""),
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
                for r in rd:
                    self.tree_log.insert("", 0, values=(
                        r.get("ma_the",""),
                        (r.get("bien_so","") or "").upper(),
                        r.get("thoi_gian_vao",""),
                        r.get("thoi_gian_ra",""),
                        r.get("phi",""),
                        r.get("bill_start",""),
                        r.get("note",""),
                    ))
        except Exception as e:
            print("Đọc CSV log lỗi:", e)

    def _log_exit(self, row):
        ensure_csv_log()
        try:
            with open(CSV_LOG, 'a', newline='', encoding='utf-8') as f:
                fn = ['ma_the','bien_so','thoi_gian_vao','thoi_gian_ra','phi','bill_start','note']
                w = csv.DictWriter(f, fieldnames=fn)
                w.writerow({k: row.get(k,"") for k in fn})
        except Exception as e:
            print("Ghi CSV log lỗi:", e)

    # ---------- Spot finders ----------
    def _find_empty_spot(self):
        with self.data_lock:
            for sid, v in self.parking_spots.items():
                if v is None:
                    return sid
        return None

    def _find_vehicle_by_plate(self, plate):
        plate = (plate or "").upper().strip()
        with self.data_lock:
            for sid, v in self.parking_spots.items():
                if v and v.get("status") == "occupied" and (v.get('plate_text','').upper() == plate):
                    return sid, v
        return None, None

    def _find_vehicle_by_rfid(self, uid):
        uid = (uid or "").upper().strip()
        with self.data_lock:
            for sid, v in self.parking_spots.items():
                if v and v.get("status") == "occupied" and (v.get('rfid_uid','').upper() == uid):
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
        x = (lw - im.width)//2
        y = (lh - im.height)//2
        bg.paste(im, (x,y))
        imgtk = ImageTk.PhotoImage(bg)
        label.configure(image=imgtk)
        label.image = imgtk

    def _pil_from_bgr(self, frame):
        return PILImage.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))

    # ---------- reset display ----------
    def _reset_exit_info(self):
        self.label_img_out.configure(image=self.placeholder_video); self.label_img_out.image = self.placeholder_video
        self.label_plate_out.configure(image=self.placeholder_plate); self.label_plate_out.image = self.placeholder_plate
        self.plate_out_var.set("---"); self.match_status_var.set("")
        self.duration_var.set("Thời gian tính phí: --:--:--")
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
            filetypes=[("All","*.*"),("Video","*.mp4 *.avi"),("Image","*.jpg *.png")]
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
    app = ParkingApp(root, "Hệ thống Quản lý Bãi giữ xe (Arduino MASTER + OCR + Web đặt trước)")
    root.mainloop()
