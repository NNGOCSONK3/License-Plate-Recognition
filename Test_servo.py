import tkinter as tk
from tkinter import ttk, messagebox
import serial
import serial.tools.list_ports

class ServoTesterApp:
    def __init__(self, window):
        self.window = window
        self.window.title("Servo Tester")
        self.window.geometry("350x200")
        self.window.resizable(False, False)

        self.serial_connection = None
        
        # --- UI Elements ---
        main_frame = ttk.Frame(self.window, padding="20")
        main_frame.pack(fill=tk.BOTH, expand=True)

        # COM Port Selection
        ttk.Label(main_frame, text="Chọn Cổng COM:").grid(row=0, column=0, padx=5, pady=5, sticky="w")
        
        self.com_port_var = tk.StringVar()
        self.com_ports = [port.device for port in serial.tools.list_ports.comports()]
        if not self.com_ports:
            self.com_ports = ["Không tìm thấy cổng COM"]
            
        self.com_combobox = ttk.Combobox(main_frame, textvariable=self.com_port_var, values=self.com_ports, state="readonly")
        self.com_combobox.grid(row=0, column=1, padx=5, pady=5, sticky="ew")
        if self.com_ports:
            self.com_combobox.set(self.com_ports[0])

        # Connect Button
        self.connect_button = ttk.Button(main_frame, text="Kết nối", command=self.toggle_connection)
        self.connect_button.grid(row=1, column=0, columnspan=2, pady=10)

        # Test Button
        self.test_button = ttk.Button(main_frame, text="Kiểm tra Servo", command=self.send_servo_command, state="disabled")
        self.test_button.grid(row=2, column=0, columnspan=2, pady=5)
        
        # Status Label
        self.status_var = tk.StringVar(value="Chưa kết nối")
        ttk.Label(main_frame, textvariable=self.status_var, foreground="red").grid(row=3, column=0, columnspan=2, pady=10)

        main_frame.columnconfigure(1, weight=1)
        self.window.protocol("WM_DELETE_WINDOW", self.on_closing)

    def toggle_connection(self):
        if self.serial_connection and self.serial_connection.is_open:
            # --- Disconnect ---
            self.serial_connection.close()
            self.status_var.set("Đã ngắt kết nối")
            self.connect_button.config(text="Kết nối")
            self.test_button.config(state="disabled")
            print("Đã ngắt kết nối.")
        else:
            # --- Connect ---
            port = self.com_port_var.get()
            if not port or "Không tìm thấy" in port:
                messagebox.showerror("Lỗi", "Vui lòng chọn cổng COM hợp lệ.")
                return
            try:
                self.serial_connection = serial.Serial(port, 9600, timeout=1)
                self.status_var.set(f"Đã kết nối tới {port}")
                self.connect_button.config(text="Ngắt kết nối")
                self.test_button.config(state="normal")
                print(f"Đã kết nối tới {port}.")
            except serial.SerialException as e:
                messagebox.showerror("Lỗi kết nối", f"Không thể mở cổng {port}.\nLỗi: {e}")
                self.status_var.set("Kết nối thất bại")

    def send_servo_command(self):
        if self.serial_connection and self.serial_connection.is_open:
            try:
                print("Gửi lệnh 'OPEN_SERVO'...")
                self.serial_connection.write(b'OPEN_SERVO\n')
                messagebox.showinfo("Thành công", "Đã gửi lệnh kiểm tra đến servo.")
            except Exception as e:
                messagebox.showerror("Lỗi", f"Không thể gửi lệnh.\nLỗi: {e}")
        else:
            messagebox.showwarning("Cảnh báo", "Chưa kết nối với cổng COM nào.")

    def on_closing(self):
        if self.serial_connection and self.serial_connection.is_open:
            self.serial_connection.close()
        self.window.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    app = ServoTesterApp(root)
    root.mainloop()
