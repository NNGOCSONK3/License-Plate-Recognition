import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from PIL import Image, ImageTk, Image as PILImage
import cv2
import torch
from datetime import datetime
import os, csv, math, serial, threading, queue, time

# Quét cổng COM
try:
    import serial.tools.list_ports
except ImportError:
    print("Cảnh báo: cần cài pyserial:  pip install pyserial")

# ==== MOCK YOLO nếu thiếu module cục bộ ====
try:
    import function.utils_rotate as utils_rotate
    import function.helper as helper
except ImportError:
    print("Không có module function/, dùng mock YOLO-OCR để test.")
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
        def __call__(self, frame, size): return _MockPandasResult()
    class helper:
        @staticmethod
        def read_plate(model, img): return "80T-8888"
    class utils_rotate:
        @staticmethod
        def deskew(img, a, b): return img

# ==== Load YOLO (nếu có) ====
try:
    if 'yolo_LP_detect' not in globals():
        yolo_LP_detect = torch.hub.load('yolov5', 'custom', path='model/LP_detector_nano_61.pt', force_reload=True, source='local')
        yolo_license_plate = torch.hub.load('yolov5', 'custom', path='model/LP_ocr_nano_62.pt', force_reload=True, source='local')
        yolo_license_plate.conf = 0.60
except Exception as e:
    print(f"Không thể tải YOLO, dùng mock. Lỗi: {e}")
    yolo_LP_detect = MockYoloModel()
    yolo_license_plate = MockYoloModel()

# ==== Tham số chống spam / treo ====
UID_COOLDOWN_MS_IN  = 2500
UID_COOLDOWN_MS_OUT = 2500
SERIAL_SAME_LINE_COOLDOWN_MS = 800
DISPLAY_RESET_MS = 8000         # Giữ hình lâu hơn
OCR_TIMEOUT_SEC = 6             # Nếu OCR quá lâu → bỏ

def now_ms():
    return int(time.time()*1000)

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

        # Trạng thái bãi
        self.parking_spots = {'A1': None,'A2': None,'A3': None,'A4': None}
        self.spot_labels = {}

        # Camera
        self.default_source_in  = 0
        self.default_source_out = 1
        self.source_in = self.default_source_in
        self.source_out = self.default_source_out
        self.vid_in = None
        self.vid_out = None
        self.last_frame_in = None
        self.last_frame_out = None

        # Serial: chỉ 1 cổng MASTER
        self.master_serial_connection = None
        self.listener_thread = None
        self.stop_thread = threading.Event()

        # Hàng đợi và cờ chống spam
        self.rfid_queue_in  = queue.Queue()
        self.rfid_queue_out = queue.Queue()
        self.uid_last_time = {'in':{}, 'out':{}}
        self.last_serial_line = ""
        self.last_serial_time_ms = 0

        # Khóa & trạng thái xử lý
        self.entry_lock = threading.Lock()
        self.exit_lock  = threading.Lock()
        self.entry_busy = False
        self.exit_busy  = False

        self.init_capture_devices()
        self.create_menu()
        self.create_widgets()
        self.process_reservations()   # Giữ chỗ nếu có file
        self.update_spot_display()

        self.delay = 20
        self.update()
        self.window.protocol("WM_DELETE_WINDOW", self.on_closing)
        self.window.mainloop()

    # ---------- UI ----------
    def create_menu(self):
        menubar = tk.Menu(self.window); self.window.config(menu=menubar)
        m_file = tk.Menu(menubar, tearoff=0); menubar.add_cascade(label="Tệp", menu=m_file)
        m_file.add_command(label="Chọn nguồn tạm thời cho Camera Vào...", command=lambda: self.select_media_source('in'))
        m_file.add_command(label="Chọn nguồn tạm thời cho Camera Ra...",  command=lambda: self.select_media_source('out'))
        m_file.add_separator(); m_file.add_command(label="Thoát", command=self.on_closing)
        m_opt = tk.Menu(menubar, tearoff=0); menubar.add_cascade(label="Tùy chọn", menu=m_opt)
        m_opt.add_command(label="Cài đặt", command=self.open_settings_window)

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

        # >>> FIX: dùng column= thay vì col=
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
        nb.add(tab_res, text="Xe Đặt Chỗ"); nb.add(tab_log, text="Lịch Sử Xe")
        self._populate_reserved_tab(tab_res); self._populate_log_tab(tab_log)

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
        cols = ('ID','Tên','SĐT','Biển số')
        self.tree_reserved = ttk.Treeview(parent, columns=cols, show='headings')
        for c in cols:
            self.tree_reserved.heading(c, text=c)
            self.tree_reserved.column(c, width=100, anchor=tk.CENTER)
        self.tree_reserved.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb = ttk.Scrollbar(parent, orient="vertical", command=self.tree_reserved.yview); sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree_reserved.configure(yscrollcommand=sb.set)
        self.load_reserved_list_from_csv()

    def _populate_log_tab(self, parent):
        cols = ('Mã Thẻ','Biển số','Thời gian vào','Thời gian ra','Phí')
        self.tree_log = ttk.Treeview(parent, columns=cols, show='headings')
        for c in cols: self.tree_log.heading(c, text=c)
        self.tree_log.column('Mã Thẻ', width=100, anchor=tk.CENTER)
        self.tree_log.column('Biển số', width=100, anchor=tk.CENTER)
        self.tree_log.column('Thời gian vào', width=140, anchor=tk.CENTER)
        self.tree_log.column('Thời gian ra', width=140, anchor=tk.CENTER)
        self.tree_log.column('Phí', width=90, anchor=tk.E)
        self.tree_log.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb = ttk.Scrollbar(parent, orient="vertical", command=self.tree_log.yview); sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree_log.configure(yscrollcommand=sb.set)
        self.load_log_from_csv()

    # ---------- Loop ----------
    def update(self):
        self.clock_var.set(datetime.now().strftime("%A, %d/%m/%Y | %I:%M:%S %p"))
        self._process_rfid_queue('in')
        self._process_rfid_queue('out')

        fi = self._get_frame(self.vid_in)
        if fi is not None:
            self.last_frame_in = fi
            self._update_video_label(self.label_cam_in, fi)

        fo = self._get_frame(self.vid_out)
        if fo is not None:
            self.last_frame_out = fo
            self._update_video_label(self.label_cam_out, fo)

        self.window.after(self.delay, self.update)

    # ---------- Entry (IN) ----------
    def capture_in(self):
        self._process_vehicle_entry(self.last_frame_in, "MANUAL_ENTRY")

    def _process_vehicle_entry(self, frame, rfid_uid):
        if frame is None:
            messagebox.showerror("Lỗi", "Không có tín hiệu từ camera vào.")
            return
        with self.entry_lock:
            if self.entry_busy:
                print("[ENTRY] Busy, bỏ yêu cầu.")
                return
            self.entry_busy = True

        def worker():
            try:
                # Ngăn cùng UID spam
                if rfid_uid != "MANUAL_ENTRY":
                    for spot, data in self.parking_spots.items():
                        if data and data.get('rfid_uid') == rfid_uid:
                            self._ui(lambda: messagebox.showwarning("Cảnh báo", f"Thẻ {rfid_uid} đã dùng cho xe {data['plate_text']} ở {spot}."))
                            return

                spot_id = self._find_empty_spot()
                if not spot_id:
                    self._ui(lambda: messagebox.showwarning("Hết chỗ", "Bãi đã đầy."))
                    return

                plate_text, crop_img = self._ocr_plate_with_timeout(frame, OCR_TIMEOUT_SEC)
                if plate_text == "unknown":
                    self._ui(lambda: messagebox.showinfo("Thông tin", "Không nhận diện được biển số xe vào."))
                    return

                # chặn biển số đã có trong bãi (trường hợp loop video)
                found_spot, _ = self._find_vehicle_by_plate(plate_text)
                if found_spot:
                    self._ui(lambda: messagebox.showwarning("Cảnh báo", f"Biển số {plate_text} đã có ở {found_spot}."))
                    return

                # Gửi vị trí đến Arduino (đưa xe vào chỗ)
                target_num = self._spot_to_target(spot_id)
                if target_num:
                    self._send_master(f"{target_num},{plate_text}")  # MASTER sẽ quay và LCD1 hiển thị

                veh = {
                    'plate_text': plate_text,
                    'entry_time': datetime.now(),
                    'plate_image': self._pil_from_bgr(crop_img) if crop_img is not None else self._placeholder_pil(150, 75),
                    'vehicle_image': self._pil_from_bgr(frame),
                    'status': 'occupied',
                    'rfid_uid': rfid_uid
                }
                def apply():
                    self.parking_spots[spot_id] = veh
                    self.update_spot_display()
                    self._reset_exit_info()
                    self._set_img(self.label_img_in,  veh['vehicle_image'])
                    self._set_img(self.label_plate_in, veh['plate_image'])
                    self.plate_in_var.set(plate_text)
                    messagebox.showinfo("Thành công", f"Xe {plate_text} (Thẻ: {rfid_uid}) đã vào {spot_id}")
                    self._schedule_reset_display()
                    if isinstance(self.source_in, str):
                        self.source_in = self.default_source_in; self._reopen_cams()
                self._ui(apply)
            finally:
                self._ui(lambda: setattr(self, 'entry_busy', False))
        threading.Thread(target=worker, daemon=True).start()

    # ---------- Exit (OUT) ----------
    def capture_out(self):
        frame = self.last_frame_out
        if frame is None:
            messagebox.showerror("Lỗi", "Không có tín hiệu từ camera ra.")
            return
        with self.exit_lock:
            if self.exit_busy:
                print("[EXIT] Busy, bỏ yêu cầu.")
                return
            self.exit_busy = True

        def worker():
            try:
                plate_out, crop_out = self._ocr_plate_with_timeout(frame, OCR_TIMEOUT_SEC)
                if plate_out == "unknown":
                    self._ui(lambda: messagebox.showinfo("Thông tin", "Không nhận diện được biển số xe ra."))
                    return
                spot_id, veh_in = self._find_vehicle_by_plate(plate_out)
                if not spot_id:
                    self._ui(lambda: messagebox.showerror("Lỗi", f"Không tìm thấy xe {plate_out} trong bãi."))
                    return

                # LCD2 và quay về vị trí xe đó
                self._send_master(f"OUT,{plate_out}")
                target_num = self._spot_to_target(spot_id)
                if target_num:
                    # tách dòng rõ ràng
                    time.sleep(0.05)
                    self._send_master(f"{target_num}")

                self._ui(lambda: self._finalize_exit(spot_id, veh_in, frame, crop_out))
            finally:
                self._ui(lambda: setattr(self, 'exit_busy', False))
        threading.Thread(target=worker, daemon=True).start()

    def _process_vehicle_exit_by_rfid(self, rfid_uid):
        frame = self.last_frame_out
        if frame is None:
            messagebox.showerror("Lỗi", "Không có tín hiệu từ camera ra.")
            return
        with self.exit_lock:
            if self.exit_busy:
                print("[EXIT] Busy, bỏ UID mới.")
                return
            self.exit_busy = True

        def worker():
            try:
                spot_id, veh_in = self._find_vehicle_by_rfid(rfid_uid)
                if not spot_id:
                    self._ui(lambda: messagebox.showerror("Không tìm thấy", f"Không có xe dùng thẻ {rfid_uid}"))
                    return
                plate_out, crop_out = self._ocr_plate_with_timeout(frame, OCR_TIMEOUT_SEC)
                if plate_out == "unknown":
                    self._ui(lambda: messagebox.showinfo("Thông tin", "Không nhận diện được biển số xe ra."))
                    return

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
                        messagebox.showerror("Sai thông tin", f"Biển số ra ({plate_out}) != ({veh_in['plate_text']}) đã đăng ký!")
                    self._ui(mismatch); return

                # Đúng biển số → LCD2 + quay vị trí
                self._send_master(f"OUT,{plate_out}")
                target_num = self._spot_to_target(spot_id)
                if target_num:
                    time.sleep(0.05)
                    self._send_master(f"{target_num}")

                self._ui(lambda: self._finalize_exit(spot_id, veh_in, frame, crop_out))
            finally:
                self._ui(lambda: setattr(self, 'exit_busy', False))
        threading.Thread(target=worker, daemon=True).start()

    def _finalize_exit(self, spot_id, veh_in, frame_out, crop_img_out):
        plate = veh_in['plate_text']
        self._set_img(self.label_img_out,  self._pil_from_bgr(frame_out))
        if crop_img_out is not None:
            self._set_img(self.label_plate_out, self._pil_from_bgr(crop_img_out))
        self.plate_out_var.set(plate)
        self._set_img(self.label_img_in,  veh_in['vehicle_image'])
        self._set_img(self.label_plate_in, veh_in['plate_image'])
        self.plate_in_var.set(plate)
        if not self.match_status_var.get():
            self.match_status_var.set("✅ TRÙNG BIỂN SỐ ✅")

        exit_time = datetime.now()
        duration = exit_time - veh_in['entry_time']
        raw_fee = (duration.total_seconds()/3600) * 5000
        final_fee = math.ceil(raw_fee/1000)*1000
        fee_text = f"{final_fee:,}".replace(",", ".")

        secs = int(duration.total_seconds()); h, r = divmod(secs,3600); m, s = divmod(r,60)
        self.duration_var.set(f"Thời gian gửi: {h:02d}:{m:02d}:{s:02d}")
        self.fee_var.set(f"Phí gửi xe: {fee_text} VNĐ")

        row = {
            'ma_the': veh_in.get('rfid_uid','N/A'),
            'bien_so': plate,
            'thoi_gian_vao': veh_in['entry_time'].strftime("%Y-%m-%d %H:%M:%S"),
            'thoi_gian_ra' : exit_time.strftime("%Y-%m-%d %H:%M:%S"),
            'phi': f"{fee_text} VNĐ"
        }
        self._log_exit(row)
        messagebox.showinfo("Thành công", f"Xe {plate} rời {spot_id}\nThời gian: {h:02d}:{m:02d}:{s:02d}\nPhí: {fee_text} VNĐ")

        self.parking_spots[spot_id] = None
        self.update_spot_display()
        self._schedule_reset_display()

    # ---------- OCR ----------
    def _ocr_plate_with_timeout(self, frame, timeout_sec):
        start = time.time()
        plate = "unknown"; crop = None
        try:
            plates = yolo_LP_detect(frame, size=640)
            lst = plates.pandas().xyxy[0].values.tolist()
        except Exception as e:
            print("Detect lỗi:", e); return "unknown", None

        if not lst: return "unknown", None
        x,y,x2,y2 = map(int, lst[0][:4])
        x=max(0,x); y=max(0,y); x2=max(x+1,x2); y2=max(y+1,y2)
        crop = frame[y:y2,x:x2]

        # Quét một vài biến dạng, có timeout
        for cc in range(2):
            for ct in range(2):
                if time.time() - start > timeout_sec:
                    return plate, crop
                try:
                    lp = helper.read_plate(yolo_license_plate, utils_rotate.deskew(crop, cc, ct))
                    if lp != "unknown": return lp, crop
                except Exception as e:
                    print("OCR lỗi:", e)
        return plate, crop

    # ---------- Serial MASTER ----------
    def start_master_listener(self, com_port, baud=9600):
        if not com_port or "Không tìm thấy" in com_port:
            messagebox.showerror("Lỗi", "Chọn cổng COM hợp lệ.")
            return

        if self.listener_thread and self.listener_thread.is_alive():
            self.stop_thread.set()
            self.listener_thread.join(timeout=1)
            try:
                if self.master_serial_connection and self.master_serial_connection.is_open:
                    self.master_serial_connection.close()
            except: pass

        self.stop_thread.clear()
        self.listener_thread = threading.Thread(target=self._read_master_serial, args=(com_port, baud), daemon=True)
        self.listener_thread.start()
        messagebox.showinfo("Thành công", f"Đã kết nối MASTER trên {com_port}.")

    def _read_master_serial(self, com_port, baud):
        print(f"Kết nối MASTER: {com_port}")
        try:
            conn = serial.Serial(com_port, baud, timeout=1)
            self.master_serial_connection = conn
        except Exception as e:
            print("Mở COM lỗi:", e); return

        while not self.stop_thread.is_set():
            try:
                line = conn.readline().decode('utf-8', errors='ignore').strip()
                if not line: continue
                # chặn spam lặp dòng
                if line == self.last_serial_line and (now_ms()-self.last_serial_time_ms) < SERIAL_SAME_LINE_COOLDOWN_MS:
                    continue
                self.last_serial_line = line; self.last_serial_time_ms = now_ms()

                if line.startswith("RFID_IN:"):
                    uid = line.split("RFID_IN:",1)[1].strip()
                    if self._uid_ok('in', uid): self.rfid_queue_in.put(uid); print("[IN]", uid)

                elif line.startswith("RFID_OUT:"):
                    uid = line.split("RFID_OUT:",1)[1].strip()
                    if self._uid_ok('out', uid): self.rfid_queue_out.put(uid); print("[OUT]", uid)

                # Các event khác để debug
                elif line.startswith("STATION_PASS:") or line.startswith("ARRIVED:"):
                    print("[MASTER]", line)

            except Exception as e:
                print("Lỗi luồng MASTER:", e); break

        try:
            if self.master_serial_connection and self.master_serial_connection.is_open:
                self.master_serial_connection.close()
        except: pass
        print("Luồng MASTER đã dừng.")

    def _uid_ok(self, direction, uid):
        t = self.uid_last_time[direction].get(uid, 0)
        cd = UID_COOLDOWN_MS_IN if direction=='in' else UID_COOLDOWN_MS_OUT
        if now_ms()-t < cd: return False
        self.uid_last_time[direction][uid] = now_ms()
        return True

    def _process_rfid_queue(self, direction):
        q = self.rfid_queue_in if direction=='in' else self.rfid_queue_out
        try:
            uid = q.get_nowait()
        except queue.Empty:
            return
        if direction=='in':
            self._process_vehicle_entry(self.last_frame_in, uid)
        else:
            self._process_vehicle_exit_by_rfid(uid)

    def _send_master(self, text):
        try:
            c = self.master_serial_connection
            if c and c.is_open:
                c.write((text.strip()+"\n").encode('utf-8'))
                print("[PC→MASTER]", text.strip())
            else:
                print("[PC→MASTER] Chưa kết nối.")
        except Exception as e:
            print("Gửi lệnh lỗi:", e)

    # ---------- Cài đặt ----------
    def open_settings_window(self):
        w = tk.Toplevel(self.window); w.title("Cài đặt"); w.configure(bg='#e6f0ff'); w.resizable(False, False)
        cams = self._find_cams(); ports = self._find_coms()

        cf = self._lframe(w, "Chọn camera"); cf.pack(padx=20,pady=10,fill="x")
        tk.Label(cf, text="Camera vào:", bg='#dcdad5').grid(row=0,column=0,sticky="w",padx=5,pady=5)
        ttk.Combobox(cf, values=cams, state="readonly", width=20).grid(row=0,column=1,padx=5,pady=5)
        tk.Label(cf, text="Camera ra:", bg='#dcdad5').grid(row=1,column=0,sticky="w",padx=5,pady=5)
        ttk.Combobox(cf, values=cams, state="readonly", width=20).grid(row=1,column=1,padx=5,pady=5)
        ttk.Button(cf, text="Áp dụng").grid(row=0,rowspan=2,column=2,padx=10,pady=10)

        # CHỈ MỘT COM (MASTER + 2 RFID)
        sf = self._lframe(w, "Kết nối Arduino MASTER + RFID"); sf.pack(padx=20,pady=10,fill="x")
        com_var = tk.StringVar()
        tk.Label(sf, text="Cổng COM:", bg='#dcdad5').grid(row=0,column=0,sticky="w",padx=5,pady=5)
        cb = ttk.Combobox(sf, textvariable=com_var, values=ports, state="readonly", width=20); cb.grid(row=0,column=1,padx=5,pady=5)
        ttk.Button(sf, text="Kết nối", command=lambda: self.start_master_listener(com_var.get())).grid(row=0,column=2,padx=10,pady=5)

        ff = self._lframe(w, "Cài đặt phí (VNĐ/giờ)"); ff.pack(padx=20,pady=10,fill="x")
        fee_var = tk.StringVar(value="5000")
        e = ttk.Entry(ff, textvariable=fee_var, width=20); e.pack(side=tk.LEFT,padx=10,pady=10)
        def save_fee():
            try:
                v = int(fee_var.get())
                if v<0: raise ValueError
                self._fee = v
                messagebox.showinfo("OK", f"Đã cập nhật phí: {v:,} VNĐ/giờ".replace(",", "."))
            except:
                messagebox.showerror("Lỗi", "Nhập số nguyên dương.")
        ttk.Button(ff, text="Lưu Phí", command=save_fee).pack(side=tk.LEFT,padx=10,pady=10)

    # ---------- Helpers ----------
    def _lframe(self, parent, text): return ttk.LabelFrame(parent, text=text)
    def _placeholder_imgtk(self,w,h): return ImageTk.PhotoImage(PILImage.new('RGB',(w,h),'white'))
    def _placeholder_pil(self,w,h): return PILImage.new('RGB',(w,h),'white')

    def _find_cams(self):
        res=[]
        for i in range(10):
            cap = cv2.VideoCapture(i)
            if cap.isOpened(): res.append(f"Camera {i}"); cap.release()
        return res if res else ["Không tìm thấy camera"]

    def _find_coms(self):
        try:
            ports = serial.tools.list_ports.comports()
            return [p.device for p in ports] if ports else ["Không tìm thấy cổng COM"]
        except:
            return ["pyserial chưa được cài đặt"]

    def _reopen_cams(self):
        self.init_capture_devices()

    def init_capture_devices(self):
        if self.vid_in: self.vid_in.release()
        if self.vid_out: self.vid_out.release()
        self.vid_in  = cv2.VideoCapture(self.source_in)
        self.vid_out = cv2.VideoCapture(self.source_out)
        if not self.vid_in.isOpened():  print("Không mở được camera vào:", self.source_in)
        if not self.vid_out.isOpened(): print("Không mở được camera ra:",  self.source_out)

    def _get_frame(self, cap):
        if cap is None or not cap.isOpened(): return None
        ret, frame = cap.read()
        if not ret:
            # nếu là file video → loop
            cap.set(cv2.CAP_PROP_POS_FRAMES, 0); ret, frame = cap.read()
        return frame if ret else None

    def _update_video_label(self, label, frame):
        self._set_img(label, self._pil_from_bgr(frame))

    def _set_img(self, label, pil_img):
        # scale-in-center giữ nguyên UI
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

    def _find_empty_spot(self):
        for sid, v in self.parking_spots.items():
            if v is None: return sid
        return None

    def update_spot_display(self):
        self._update_spot_display()

    def _update_spot_display(self):
        for sid, v in self.parking_spots.items():
            lb = self.spot_labels[sid]
            if v:
                st = v.get('status','occupied')
                if st=='reserved':
                    lb.config(bg='#f39c12', fg='white', text=f"{sid}\n{v['plate_text']}\n(Đã đặt)")
                else:
                    lb.config(bg='#e74c3c', fg='white', text=f"{sid}\n{v['plate_text']}")
            else:
                lb.config(bg='#2ecc71', fg='white', text=sid)

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
        print("Đã auto reset hiển thị.")

    def _schedule_reset_display(self):
        self.window.after(DISPLAY_RESET_MS, self._reset_all)

    def _find_vehicle_by_plate(self, plate):
        for sid, v in self.parking_spots.items():
            if v and v['plate_text'] == plate: return sid, v
        return None, None

    def _find_vehicle_by_rfid(self, uid):
        for sid, v in self.parking_spots.items():
            if v and v.get('rfid_uid') == uid: return sid, v
        return None, None

    def _spot_to_target(self, sid):
        return {'A1':1,'A2':2,'A3':3,'A4':4}.get(sid, 0)

    def _log_exit(self, row, fn="lich_su_xe.csv"):
        exists = os.path.isfile(fn)
        try:
            with open(fn, 'a', newline='', encoding='utf-8') as f:
                w = csv.DictWriter(f, fieldnames=['ma_the','bien_so','thoi_gian_vao','thoi_gian_ra','phi'])
                if not exists: w.writeheader()
                w.writerow(row)
            self.load_log_from_csv()
            self.tree_log.update_idletasks()
        except Exception as e:
            print("Ghi CSV lỗi:", e)

    def load_reserved_list_from_csv(self, fn="dat_cho_truoc.csv"):
        self._load_csv_to_tree(fn, self.tree_reserved, expected_cols=4)

    def load_log_from_csv(self, fn="lich_su_xe.csv"):
        self._load_csv_to_tree(fn, self.tree_log, expected_cols=5)

    def _load_csv_to_tree(self, fn, tree, expected_cols):
        for it in tree.get_children(): tree.delete(it)
        try:
            with open(fn,'r',newline='',encoding='utf-8') as f:
                rd = csv.reader(f)
                try:
                    header = next(rd)
                except StopIteration:
                    return
                for row in rd:
                    if len(row) == expected_cols:
                        tree.insert("", 0, values=row)
                    elif len(row) == expected_cols-1 and 'ma_the' in tree['columns']:
                        tree.insert("", 0, values=['N/A']+row)
        except FileNotFoundError:
            print(f"Chưa có file {fn}")
        except Exception as e:
            print("Đọc CSV lỗi:", e)

    def process_reservations(self, fn="dat_cho_truoc.csv"):
        try:
            with open(fn,'r',newline='',encoding='utf-8') as f:
                rd = csv.reader(f); next(rd, None)
                for row in rd:
                    if len(row) < 4: continue
                    plate = row[3]; sid = self._find_empty_spot()
                    if not sid: print("Hết chỗ cho đặt trước."); break
                    self.parking_spots[sid] = {
                        'plate_text': plate,
                        'status': 'reserved',
                        'vehicle_image': self._placeholder_pil(500,375),
                        'plate_image'  : self._placeholder_pil(150,75),
                        'rfid_uid':'RESERVED'
                    }
        except FileNotFoundError:
            print("Không có dat_cho_truoc.csv (bỏ qua).")
        except Exception as e:
            print("Lỗi đọc đặt chỗ:", e)

    def select_media_source(self, channel):
        fp = filedialog.askopenfilename(title="Chọn ảnh/video", filetypes=[("All","*.*"),("Video","*.mp4 *.avi"),("Image","*.jpg *.png")])
        if not fp: return
        if channel=='in': self.source_in = fp
        else: self.source_out = fp
        self._reopen_cams()
        messagebox.showinfo("Thông báo", f"Đã cập nhật nguồn Camera {channel.upper()}.")

    def on_closing(self):
        if messagebox.askokcancel("Thoát", "Thoát chương trình?"):
            self.stop_thread.set()
            try:
                if self.master_serial_connection and self.master_serial_connection.is_open:
                    self.master_serial_connection.close()
            except: pass
            if self.listener_thread: self.listener_thread.join(timeout=1)
            if self.vid_in: self.vid_in.release()
            if self.vid_out: self.vid_out.release()
            self.window.destroy()

    def _ui(self, fn): self.window.after(0, fn)

# ---- RUN ----
if __name__ == "__main__":
    root = tk.Tk()
    root.state('zoomed')
    app = ParkingApp(root, "Hệ thống Quản lý Bãi giữ xe (1 COM: Arduino MASTER + RFID)")
