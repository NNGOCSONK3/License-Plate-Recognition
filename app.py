import tkinter as tk
from tkinter import ttk, font, messagebox, filedialog
from PIL import Image, ImageTk
import cv2
import torch
from datetime import datetime
import numpy as np
import os
import csv
import math

# Thêm thư viện để quét cổng COM
try:
    import serial.tools.list_ports
except ImportError:
    print("Cảnh báo: Thư viện 'pyserial' chưa được cài đặt. Chức năng tự động tìm cổng COM sẽ không hoạt động.")
    print("Vui lòng cài đặt bằng lệnh: pip install pyserial")

# --- GIẢ LẬP MODULE (ĐỂ TEST) ---
try:
    import function.utils_rotate as utils_rotate
    import function.helper as helper
except ImportError:
    print("Cảnh báo: Không tìm thấy module 'function'. Sử dụng module giả lập.")
    class MockYoloModel:
        def __init__(self): self.conf = 0.6
        def __call__(self, frame, size):
            class MockPandasResult:
                def __init__(self): self.xyxy = [{'values': [[100, 100, 300, 200, 0.95, 0]]}]
                def pandas(self): return self
            return MockPandasResult()
    class helper:
        @staticmethod
        def read_plate(model, img): return "80T-8888"
    class utils_rotate:
        @staticmethod
        def deskew(img, a, b): return img

# --- CÁC THIẾT LẬP BAN ĐẦU ---
try:
    if 'yolo_LP_detect' not in globals():
        yolo_LP_detect = torch.hub.load('yolov5', 'custom', path='model/LP_detector_nano_61.pt', force_reload=True, source='local')
        yolo_license_plate = torch.hub.load('yolov5', 'custom', path='model/LP_ocr_nano_62.pt', force_reload=True, source='local')
        yolo_license_plate.conf = 0.60
except Exception as e:
    print(f"Cảnh báo: Không thể tải model YOLO. Chương trình sẽ chạy với model giả lập. Lỗi: {e}")
    yolo_LP_detect = MockYoloModel()
    yolo_license_plate = MockYoloModel()

# --- LỚP GIAO DIỆN CHÍNH ---
class ParkingApp:
    def __init__(self, window, window_title):
        self.window = window
        self.window.title(window_title)
        self.window.configure(bg='#e6f0ff')

        style = ttk.Style(self.window)
        style.theme_use('clam')
        
        # SỬA Ở ĐÂY: Đặt borderwidth = 0 để xóa viền
        style.configure("TLabelFrame", borderwidth=0, background='#e6f0ff')
        
        style.configure("TLabelFrame.Label", foreground="blue", background='#e6f0ff', font=("Helvetica", 11, "bold"))
        style.configure("TButton", font=("Helvetica", 10))
        
        self.parking_spots = {'A1': None, 'A2': None, 'A3': None, 'A4': None}
        self.spot_labels = {}

        self.parking_fee_per_hour = 5000
        self.vid_in, self.vid_out = None, None
        
        self.default_source_in = 0
        self.default_source_out = 1 
        self.source_in = self.default_source_in
        self.source_out = self.default_source_out
        
        self.last_frame_in, self.last_frame_out = None, None

        self.init_capture_devices()
        self.create_menu()
        self.create_widgets()
        
        self.process_reservations()
        
        self.update_spot_display()

        self.delay = 15
        self.update()
        self.window.protocol("WM_DELETE_WINDOW", self.on_closing)
        self.window.mainloop()

    def create_menu(self):
        menubar = tk.Menu(self.window)
        self.window.config(menu=menubar)
        file_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Tệp", menu=file_menu)
        file_menu.add_command(label="Chọn nguồn tạm thời cho Camera Vào...", command=lambda: self.select_media_source('in'))
        file_menu.add_command(label="Chọn nguồn tạm thời cho Camera Ra...", command=lambda: self.select_media_source('out'))
        file_menu.add_separator()
        file_menu.add_command(label="Thoát", command=self.on_closing)
        options_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Tùy chọn", menu=options_menu)
        options_menu.add_command(label="Cài đặt", command=self.open_settings_window)

    def create_widgets(self):
        main_frame = tk.Frame(self.window, bg='#e6f0ff')
        main_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        left_pane = tk.Frame(main_frame, bg='#e6f0ff')
        left_pane.pack(side=tk.LEFT, fill=tk.Y, expand=False, padx=(0, 5))
        self.create_left_pane_widgets(left_pane)
        right_pane = tk.Frame(main_frame, bg='#e6f0ff')
        right_pane.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True, padx=(5, 0))
        self.create_right_pane_widgets(right_pane)

    def create_left_pane_widgets(self, parent):
        FIXED_WIDTH = 500; FIXED_HEIGHT = 375
        parent.columnconfigure(0, minsize=FIXED_WIDTH); parent.columnconfigure(1, minsize=FIXED_WIDTH)
        parent.rowconfigure(0, minsize=FIXED_HEIGHT); parent.rowconfigure(1, minsize=FIXED_HEIGHT)
        
        self.placeholder_video = self.create_placeholder_image(FIXED_WIDTH, FIXED_HEIGHT)
        
        frame_cam_in = self.create_labeled_frame(parent, "Camera ngõ vào"); frame_cam_in.grid(row=0, column=0, sticky="nsew", padx=5, pady=5); frame_cam_in.pack_propagate(False) 
        self.label_cam_in = tk.Label(frame_cam_in, image=self.placeholder_video, bg='white'); self.label_cam_in.pack(fill=tk.BOTH, expand=True)
        
        frame_img_in = self.create_labeled_frame(parent, "Ảnh xe vào"); frame_img_in.grid(row=0, column=1, sticky="nsew", padx=5, pady=5); frame_img_in.pack_propagate(False)
        self.label_img_in = tk.Label(frame_img_in, image=self.placeholder_video, bg='white'); self.label_img_in.pack(fill=tk.BOTH, expand=True)
        
        frame_cam_out = self.create_labeled_frame(parent, "Camera ngõ ra"); frame_cam_out.grid(row=1, column=0, sticky="nsew", padx=5, pady=5); frame_cam_out.pack_propagate(False)
        self.label_cam_out = tk.Label(frame_cam_out, image=self.placeholder_video, bg='white'); self.label_cam_out.pack(fill=tk.BOTH, expand=True)
        
        frame_img_out = self.create_labeled_frame(parent, "Ảnh xe ra"); frame_img_out.grid(row=1, column=1, sticky="nsew", padx=5, pady=5); frame_img_out.pack_propagate(False)
        self.label_img_out = tk.Label(frame_img_out, image=self.placeholder_video, bg='white'); self.label_img_out.pack(fill=tk.BOTH, expand=True)

    def create_right_pane_widgets(self, parent):
        plate_frame = self.create_labeled_frame(parent, "Thông tin biển số"); plate_frame.pack(fill=tk.X, pady=(0, 5), ipady=5); self.populate_plate_frame(plate_frame)
        time_cost_frame = self.create_labeled_frame(parent, "Thời gian & Chi phí"); time_cost_frame.pack(fill=tk.X, pady=5, ipady=5); self.populate_time_cost_frame(time_cost_frame)
        spots_frame = self.create_labeled_frame(parent, "Trạng thái bãi xe"); spots_frame.pack(fill=tk.X, pady=5, ipady=5); self.populate_spots_frame(spots_frame)
        notebook = ttk.Notebook(parent); notebook.pack(fill=tk.BOTH, expand=True, pady=(10, 0))
        tab_reserved = ttk.Frame(notebook); tab_log = ttk.Frame(notebook)
        notebook.add(tab_reserved, text="Xe Đặt Chỗ"); notebook.add(tab_log, text="Lịch Sử Xe")
        self.populate_reserved_tab(tab_reserved); self.populate_log_tab(tab_log)

    def populate_spots_frame(self, parent):
        parent.columnconfigure((0, 1, 2, 3), weight=1)
        spot_font = ("Helvetica", 14, "bold")
        for i, spot_id in enumerate(self.parking_spots.keys()):
            label = tk.Label(parent, text=spot_id, font=spot_font, relief=tk.RAISED, bd=2, width=5, height=2)
            label.grid(row=0, column=i, padx=5, pady=5, sticky="ew")
            self.spot_labels[spot_id] = label

    def populate_plate_frame(self, parent):
        parent.columnconfigure((0, 1, 2), weight=1)
        self.placeholder_plate = self.create_placeholder_image(150, 75)
        tk.Label(parent, text="Biển số xe vào", font=("Helvetica", 10, "bold"), bg='#dcdad5').grid(row=0, column=0, pady=(5,0))
        self.label_plate_in = tk.Label(parent, image=self.placeholder_plate, bg='white'); self.label_plate_in.grid(row=1, column=0)
        self.plate_in_var = tk.StringVar(value="---")
        tk.Label(parent, textvariable=self.plate_in_var, font=("Helvetica", 12, "bold"), bg='#dcdad5').grid(row=2, column=0, pady=(0,5))
        self.match_status_var = tk.StringVar(value="")
        tk.Label(parent, textvariable=self.match_status_var, font=("Helvetica", 12, "bold", "italic"), fg="green", bg='#dcdad5').grid(row=1, column=1)
        tk.Label(parent, text="Biển số xe ra", font=("Helvetica", 10, "bold"), bg='#dcdad5').grid(row=0, column=2, pady=(5,0))
        self.label_plate_out = tk.Label(parent, image=self.placeholder_plate, bg='white'); self.label_plate_out.grid(row=1, column=2)
        self.plate_out_var = tk.StringVar(value="---")
        tk.Label(parent, textvariable=self.plate_out_var, font=("Helvetica", 12, "bold"), bg='#dcdad5').grid(row=2, column=2, pady=(0,5))

    def populate_time_cost_frame(self, parent):
        parent.columnconfigure(0, weight=1)
        self.clock_var = tk.StringVar()
        tk.Label(parent, textvariable=self.clock_var, font=("Helvetica", 14, "bold"), bg='#dcdad5').pack()
        self.duration_var = tk.StringVar(value="Thời gian gửi: --:--:--")
        tk.Label(parent, textvariable=self.duration_var, font=("Helvetica", 11), bg='#dcdad5').pack()
        self.fee_var = tk.StringVar(value=f"Phí gửi xe: -- VNĐ")
        tk.Label(parent, textvariable=self.fee_var, font=("Helvetica", 11, "bold"), bg='#dcdad5').pack()
        
        button_frame = tk.Frame(parent, bg='#dcdad5'); button_frame.pack(pady=5)
        ttk.Button(button_frame, text="Xác nhận vào", command=self.capture_in).pack(side=tk.LEFT, padx=10)
        ttk.Button(button_frame, text="Xác nhận ra", command=self.capture_out).pack(side=tk.LEFT, padx=10)

    def populate_reserved_tab(self, parent):
        cols = ('ID', 'Tên', 'SĐT', 'Biển số'); self.tree_reserved = ttk.Treeview(parent, columns=cols, show='headings')
        for col in cols: self.tree_reserved.heading(col, text=col); self.tree_reserved.column(col, width=100, anchor=tk.CENTER)
        self.tree_reserved.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar = ttk.Scrollbar(parent, orient="vertical", command=self.tree_reserved.yview); scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree_reserved.configure(yscrollcommand=scrollbar.set); self.load_reserved_list_from_csv()
    
    def populate_log_tab(self, parent):
        cols = ('Biển số', 'Thời gian vào', 'Thời gian ra', 'Phí'); self.tree_log = ttk.Treeview(parent, columns=cols, show='headings')
        for col in cols: self.tree_log.heading(col, text=col); self.tree_log.column(col, width=110, anchor=tk.CENTER)
        self.tree_log.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar = ttk.Scrollbar(parent, orient="vertical", command=self.tree_log.yview); scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree_log.configure(yscrollcommand=scrollbar.set); self.load_log_from_csv()

    def update(self):
        self.update_clock()
        frame_in = self.get_frame_from_source(self.vid_in)
        if frame_in is not None: self.last_frame_in = frame_in; self.update_video_label(self.label_cam_in, self.last_frame_in)
        frame_out = self.get_frame_from_source(self.vid_out)
        if frame_out is not None: self.last_frame_out = frame_out; self.update_video_label(self.label_cam_out, self.last_frame_out)
        self.window.after(self.delay, self.update)

    def update_clock(self):
        self.clock_var.set(datetime.now().strftime("%A, %d/%m/%Y | %I:%M:%S %p"))
        
    def capture_in(self):
        spot_id = self.find_empty_spot()
        if not spot_id: messagebox.showwarning("Hết chỗ", "Bãi xe đã đầy."); return
        frame = self.last_frame_in
        if frame is None: messagebox.showerror("Lỗi", "Không có tín hiệu từ camera vào."); return
        plate_text, crop_img = self.process_frame_for_plate(frame)
        if plate_text == "unknown": messagebox.showinfo("Thông tin", "Không nhận diện được biển số xe vào."); return
        
        vehicle_data = {'plate_text': plate_text, 'entry_time': datetime.now(), 'plate_image': Image.fromarray(cv2.cvtColor(crop_img, cv2.COLOR_BGR2RGB)), 'vehicle_image': Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)), 'status': 'occupied'}
        self.parking_spots[spot_id] = vehicle_data; self.update_spot_display()
        self.reset_exit_info(); self.update_display_image(self.label_img_in, vehicle_data['vehicle_image'])
        self.update_display_image(self.label_plate_in, vehicle_data['plate_image']); self.plate_in_var.set(plate_text)
        messagebox.showinfo("Thành công", f"Xe {plate_text} đã vào vị trí {spot_id}")
        
        self.schedule_display_reset()

        if isinstance(self.source_in, str):
            print(f"Hoàn tất xử lý file, quay lại camera mặc định cho ngõ vào: {self.default_source_in}")
            self.source_in = self.default_source_in
            self.init_capture_devices()
        
    def capture_out(self):
        frame = self.last_frame_out
        if frame is None: messagebox.showerror("Lỗi", "Không có tín hiệu từ camera ra."); return
        plate_text_out, crop_img_out = self.process_frame_for_plate(frame)
        if plate_text_out == "unknown": messagebox.showinfo("Thông tin", "Không nhận diện được biển số xe ra."); return

        spot_id, vehicle_data_in = self.find_vehicle_by_plate(plate_text_out)
        if not spot_id: messagebox.showerror("Lỗi", f"Không tìm thấy xe có biển số {plate_text_out} trong bãi."); return
        
        self.update_display_image(self.label_img_out, Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)))
        self.update_display_image(self.label_plate_out, Image.fromarray(cv2.cvtColor(crop_img_out, cv2.COLOR_BGR2RGB)))
        self.plate_out_var.set(plate_text_out)
        self.update_display_image(self.label_img_in, vehicle_data_in['vehicle_image'])
        self.update_display_image(self.label_plate_in, vehicle_data_in['plate_image'])
        self.plate_in_var.set(vehicle_data_in['plate_text'])
        self.match_status_var.set("✅ TRÙNG BIỂN SỐ ✅")
        
        exit_time = datetime.now()
        duration = exit_time - vehicle_data_in['entry_time']
        
        raw_fee = (duration.total_seconds() / 3600) * self.parking_fee_per_hour
        final_fee = math.ceil(raw_fee / 1000) * 1000
        formatted_fee = f"{final_fee:,}".replace(',', '.')
        
        secs = int(duration.total_seconds()); h, rem = divmod(secs, 3600); m, s = divmod(rem, 60)
        self.duration_var.set(f"Thời gian gửi: {h:02d}:{m:02d}:{s:02d}")
        self.fee_var.set(f"Phí gửi xe: {formatted_fee} VNĐ")

        log_data = {
            'bien_so': plate_text_out, 
            'thoi_gian_vao': vehicle_data_in['entry_time'].strftime("%Y-%m-%d %H:%M:%S"), 
            'thoi_gian_ra': exit_time.strftime("%Y-%m-%d %H:%M:%S"), 
            'phi': f"{formatted_fee} VNĐ"
        }
        self.log_vehicle_exit_to_csv(log_data)
        messagebox.showinfo("Thành công", f"Xe {plate_text_out} từ vị trí {spot_id} đã ra.\nTổng thời gian: {str(duration).split('.')[0]}\nTổng phí: {formatted_fee} VNĐ")
        
        self.parking_spots[spot_id] = None; self.update_spot_display()

        self.schedule_display_reset()

        if isinstance(self.source_out, str):
            print(f"Hoàn tất xử lý file, quay lại camera mặc định cho ngõ ra: {self.default_source_out}")
            self.source_out = self.default_source_out
            self.init_capture_devices()

    def process_frame_for_plate(self, frame):
        plates = yolo_LP_detect(frame, size=640)
        list_plates = plates.pandas().xyxy[0].values.tolist()
        if list_plates:
            plate_info = list_plates[0]; x, y, x2, y2 = map(int, plate_info[:4]); crop_img = frame[y:y2, x:x2]
            for cc in range(0,2):
                for ct in range(0,2):
                    lp = helper.read_plate(yolo_license_plate, utils_rotate.deskew(crop_img, cc, ct))
                    if lp != "unknown": return lp, crop_img
        return "unknown", None

    def on_closing(self):
        if messagebox.askokcancel("Thoát", "Bạn có chắc muốn thoát chương trình?"):
            if self.vid_in: self.vid_in.release() 
            if self.vid_out: self.vid_out.release()
            self.window.destroy()

    def open_settings_window(self):
        settings_window = tk.Toplevel(self.window); settings_window.title("Cài đặt"); settings_window.configure(bg='#e6f0ff'); settings_window.resizable(False, False)
        available_cameras = self.find_available_cameras(); available_com_ports = self.find_available_com_ports()
        
        camera_frame = self.create_labeled_frame(settings_window, "Chọn camera"); camera_frame.pack(padx=20, pady=10, fill="x")
        tk.Label(camera_frame, text="Camera vào:", bg='#dcdad5').grid(row=0, column=0, padx=5, pady=5, sticky="w"); ttk.Combobox(camera_frame, values=available_cameras, state="readonly", width=20).grid(row=0, column=1, padx=5, pady=5)
        tk.Label(camera_frame, text="Camera ra:", bg='#dcdad5').grid(row=1, column=0, padx=5, pady=5, sticky="w"); ttk.Combobox(camera_frame, values=available_cameras, state="readonly", width=20).grid(row=1, column=1, padx=5, pady=5)
        ttk.Button(camera_frame, text="Áp dụng").grid(row=0, rowspan=2, column=2, padx=10, pady=10)

        com_frame = self.create_labeled_frame(settings_window, "Chọn cổng COM RFID"); com_frame.pack(padx=20, pady=10, fill="x")
        tk.Label(com_frame, text="RFID 1:", bg='#dcdad5').grid(row=0, column=0, padx=5, pady=5, sticky="w"); ttk.Combobox(com_frame, values=available_com_ports, state="readonly", width=20).grid(row=0, column=1, padx=5, pady=5)
        ttk.Button(com_frame, text="Kết nối 1").grid(row=0, column=2, padx=10, pady=5)
        tk.Label(com_frame, text="RFID 2:", bg='#dcdad5').grid(row=1, column=0, padx=5, pady=5, sticky="w"); ttk.Combobox(com_frame, values=available_com_ports, state="readonly", width=20).grid(row=1, column=1, padx=5, pady=5)
        ttk.Button(com_frame, text="Kết nối 2").grid(row=1, column=2, padx=10, pady=5)
        
        fee_frame = self.create_labeled_frame(settings_window, "Cài đặt phí gửi xe (VNĐ/giờ)"); fee_frame.pack(padx=20, pady=10, fill="x")
        fee_var = tk.StringVar(value=str(self.parking_fee_per_hour)); fee_entry = ttk.Entry(fee_frame, textvariable=fee_var, width=20); fee_entry.pack(side=tk.LEFT, padx=10, pady=10)
        def save_settings():
            try:
                new_fee = int(fee_var.get())
                if new_fee < 0: raise ValueError
                self.parking_fee_per_hour = new_fee
                formatted_new_fee = f"{new_fee:,}".replace(',', '.')
                messagebox.showinfo("Thành công", f"Đã cập nhật phí gửi xe thành {formatted_new_fee} VNĐ/giờ.")
            except ValueError: messagebox.showerror("Lỗi", "Vui lòng nhập một số nguyên dương hợp lệ.")
        ttk.Button(fee_frame, text="Lưu Phí", command=save_settings).pack(side=tk.LEFT, padx=10, pady=10)

    def find_available_cameras(self):
        cameras = [];
        for i in range(10):
            cap = cv2.VideoCapture(i)
            if cap.isOpened(): cameras.append(f"Camera {i}"); cap.release()
        return cameras if cameras else ["Không tìm thấy camera"]

    def find_available_com_ports(self):
        try:
            ports = serial.tools.list_ports.comports()
            return [port.device for port in ports] if ports else ["Không tìm thấy cổng COM"]
        except NameError: return ["pyserial chưa được cài đặt"]
    
    def find_empty_spot(self):
        for spot_id, vehicle in self.parking_spots.items():
            if vehicle is None: return spot_id
        return None
    
    def find_vehicle_by_plate(self, plate_text):
        for spot_id, vehicle in self.parking_spots.items():
            if vehicle and vehicle['plate_text'] == plate_text: return spot_id, vehicle
        return None, None

    def update_spot_display(self):
        for spot_id, vehicle in self.parking_spots.items():
            label = self.spot_labels[spot_id]
            if vehicle:
                status = vehicle.get('status', 'occupied') 
                if status == 'reserved':
                    label.config(bg='#f39c12', fg='white', text=f"{spot_id}\n{vehicle['plate_text']}\n(Đã đặt)")
                else: 
                    label.config(bg='#e74c3c', fg='white', text=f"{spot_id}\n{vehicle['plate_text']}")
            else: 
                label.config(bg='#2ecc71', fg='white', text=spot_id)

    def log_vehicle_exit_to_csv(self, data, filename="lich_su_xe.csv"):
        file_exists = os.path.isfile(filename)
        try:
            with open(filename, mode='a', newline='', encoding='utf-8') as f:
                writer = csv.DictWriter(f, fieldnames=['bien_so', 'thoi_gian_vao', 'thoi_gian_ra', 'phi'])
                if not file_exists: writer.writeheader()
                writer.writerow(data)
            self.load_log_from_csv()
            self.tree_log.update_idletasks()
        except Exception as e: print(f"Lỗi khi ghi file log CSV: {e}")

    def select_media_source(self, channel):
        file_path = filedialog.askopenfilename(title="Chọn file ảnh hoặc video", filetypes=[("All files", "*.*"), ("Video files", "*.mp4 *.avi"), ("Image files", "*.jpg *.png")])
        if not file_path: return
        if channel == 'in': self.source_in = file_path
        else: self.source_out = file_path
        self.init_capture_devices()
        messagebox.showinfo("Thông báo", f"Đã cập nhật nguồn cho Camera {channel.upper()}.")

    def load_csv_data(self, filename, treeview):
        for item in treeview.get_children():
            treeview.delete(item)
        try:
            with open(filename, mode='r', newline='', encoding='utf-8') as f:
                reader = csv.reader(f)
                try:
                    header = next(reader)
                except StopIteration:
                    return
                for row in reader:
                    treeview.insert("", 0, values=row)
        except FileNotFoundError:
            print(f"File '{filename}' không tồn tại. Sẽ được tự động tạo.")
        except Exception as e:
            print(f"Lỗi khi đọc file CSV '{filename}': {e}")
            
    def process_reservations(self, filename="dat_cho_truoc.csv"):
        try:
            with open(filename, mode='r', newline='', encoding='utf-8') as f:
                reader = csv.reader(f)
                try:
                    header = next(reader)
                except StopIteration:
                    return 
                
                for row in reader:
                    if len(row) < 4: continue 
                    
                    plate = row[3] 
                    spot_id = self.find_empty_spot()
                    
                    if spot_id:
                        self.parking_spots[spot_id] = {
                            'plate_text': plate,
                            'status': 'reserved',
                            'vehicle_image': self.placeholder_video_as_image(),
                            'plate_image': self.placeholder_plate_as_image()
                        }
                        print(f"Đã giữ chỗ {spot_id} cho xe {plate}")
                    else:
                        print(f"Cảnh báo: Hết chỗ, không thể giữ chỗ cho xe {plate}")
                        break 
        except FileNotFoundError:
            print(f"Không tìm thấy file đặt chỗ '{filename}'. Bỏ qua xử lý đặt chỗ.")
        except Exception as e:
            print(f"Lỗi khi xử lý file đặt chỗ '{filename}': {e}")
            
    def load_reserved_list_from_csv(self, filename="dat_cho_truoc.csv"):
        self.load_csv_data(filename, self.tree_reserved)
        
    def load_log_from_csv(self, filename="lich_su_xe.csv"):
        self.load_csv_data(filename, self.tree_log)

    def init_capture_devices(self):
        if self.vid_in: self.vid_in.release() 
        if self.vid_out: self.vid_out.release()
        self.vid_in = cv2.VideoCapture(self.source_in); self.vid_out = cv2.VideoCapture(self.source_out)
        if not self.vid_in.isOpened(): print(f"Lỗi: Không thể mở nguồn camera VÀO: {self.source_in}")
        if not self.vid_out.isOpened(): print(f"Lỗi: Không thể mở nguồn camera RA: {self.source_out}")

    def get_frame_from_source(self, cap):
        if cap is None or not cap.isOpened(): return None
        ret, frame = cap.read()
        is_video_file = isinstance(self.source_in, str) or isinstance(self.source_out, str)
        if isinstance(cap.get(cv2.CAP_PROP_FRAME_COUNT), float) and cap.get(cv2.CAP_PROP_FRAME_COUNT) == 1.0:
            return frame if ret else None
        if not ret and is_video_file:
             cap.set(cv2.CAP_PROP_POS_FRAMES, 0); ret, frame = cap.read()
        return frame if ret else None
    
    def reset_exit_info(self):
        self.label_img_out.configure(image=self.placeholder_video); self.label_img_out.image = self.placeholder_video
        self.label_plate_out.configure(image=self.placeholder_plate); self.label_plate_out.image = self.placeholder_plate
        self.plate_out_var.set("---"); self.match_status_var.set("")
        self.duration_var.set("Thời gian gửi: --:--:--")
        self.fee_var.set("Phí gửi xe: -- VNĐ")
        
    def reset_all_displays(self):
        self.label_img_in.configure(image=self.placeholder_video); self.label_img_in.image = self.placeholder_video
        self.label_plate_in.configure(image=self.placeholder_plate); self.label_plate_in.image = self.placeholder_plate
        self.plate_in_var.set("---")
        self.reset_exit_info()
        print("Đã tự động reset các ô hiển thị.")

    def schedule_display_reset(self):
        self.window.after(2000, self.reset_all_displays)

    def create_labeled_frame(self, parent, text): 
        return ttk.LabelFrame(parent, text=text)
    
    def create_placeholder_image(self, width, height): 
        return ImageTk.PhotoImage(Image.new('RGB', (width, height), 'white')) 
    
    def placeholder_video_as_image(self):
        return Image.new('RGB', (500, 375), 'white')
    
    def placeholder_plate_as_image(self):
        return Image.new('RGB', (150, 75), 'white')
    
    def update_display_image(self, label, pil_image):
        label_w, label_h = label.winfo_width(), label.winfo_height()
        if label_w < 2 or label_h < 2: 
            self.window.after(50, lambda: self.update_display_image(label, pil_image))
            return
        
        background = Image.new('RGB', (label_w, label_h), 'white')
        
        img_pil_to_show = pil_image.copy()
        img_pil_to_show.thumbnail((label_w, label_h), Image.Resampling.LANCZOS)
        
        paste_x, paste_y = (label_w - img_pil_to_show.width) // 2, (label_h - img_pil_to_show.height) // 2
        background.paste(img_pil_to_show, (paste_x, paste_y))
        
        img_tk = ImageTk.PhotoImage(image=background)
        label.configure(image=img_tk); label.image = img_tk
        
    def update_video_label(self, label, frame): 
        pil_img = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
        self.update_display_image(label, pil_img)

# --- CHẠY CHƯƠNG TRÌNH ---
if __name__ == "__main__":
    root = tk.Tk()
    root.state('zoomed') 
    app = ParkingApp(root, "Hệ thống Quản lý Bãi giữ xe")