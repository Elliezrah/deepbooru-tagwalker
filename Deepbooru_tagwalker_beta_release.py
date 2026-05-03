import sys
import os
import tkinter as tk
from tkinter import messagebox, filedialog
from PIL import Image, ImageTk
import glob
import datetime
import threading
import sys
import os

def resource_path(relative):
    base = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, relative)

class TagProofCheckerApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Deepbooru TagWalker Beta")
        self.root.iconbitmap(sys.executable)
        self.root.configure(bg='#1e1e1e')
        self.root.after(0, lambda: self.root.iconbitmap(resource_path('icon.ico')))

        # Data Structures
        self.image_directory = None
        self.master_tag_list = []
        self.tag_status = {}
        self.tag_counts = {}
        self.current_tag_index = -1
        self.image_queue = []        # kept for compat; no longer drives ordering
        self.skip_backlog = set()
        self.undo_history = []
        self.action_log = []
        self.current_image_path = None
        self.tk_image = None
        self.current_pil_image = None
        self.image_files = []
        self.expected_tags = []
        self.total_images_for_tag = 0
        self.current_image_seq = 0
        self.all_images_for_tag = []
        # --- NEW: position-based queue model ---
        # current_position: index in all_images_for_tag of the image currently on screen
        self.current_position = -1
        # session_processed: images handled (yes/no) this tag session; removed by Back.
        self.session_processed = set()
        # session_decision: per-image decision for color coding. Reverted by Back.
        self.session_decision = {}   # path -> 'yes' | 'no' | 'skip'
        # [FIX: Performance] Queue uses Listbox — O(1) highlight via itemconfig
        self.queue_path_list = []    # ordered list of paths matching listbox indices
        self.queue_path_index = {}   # {path: listbox_index} for O(1) lookup
        self._prev_queue_img = None  # Track previous image to revert highlight efficiently

        # State Flags
        self.pre_action_result = None
        self._is_resizing_sidebar = False  # [FIX: Sidebar lag] Track resize state
        self._is_minimized = False         # [FIX: Restore lag] Track minimize state
        self._resize_debounce_timer = None
        self._updating_sidebar = False     # Guard against recursive sidebar select events

        # Zoom Overlay State
        self.zoom_overlay = None
        self.overlay_zoom_level = 1.0

        # Initialize status_var safely before setup_ui completes
        self.status_var = tk.StringVar(value="⏳ Ready")
        self.scan_complete_var = tk.BooleanVar(value=False)

        self.setup_ui()
        # [FIX: Bug 7] Safe maximize with fallback for incompatible window managers
        try:
            self.root.after(150, lambda: self.root.state('zoomed'))
        except Exception:
            pass

    def setup_ui(self):
        # Resizable Paned Layout
        self.main_pane = tk.PanedWindow(self.root, orient=tk.HORIZONTAL, bg='#1e1e1e', sashwidth=4, sashrelief=tk.RIDGE, handlepad=10)
        self.main_pane.pack(fill='both', expand=True)

        # --- LEFT SIDEBAR (Tags) ---
        self.left_frame = tk.Frame(self.main_pane, bg='#2c2c2c', width=220)
        self.main_pane.add(self.left_frame, minsize=150)
        # [FIX: Tag title positioning] Locked to top, won't shrink or hide
        tk.Label(self.left_frame, text="🏷 Tags", bg='#2c2c2c', fg='#dcdcdc', font=('Arial', 14, 'bold')).pack(side='top', fill='x', padx=5, pady=5, anchor='w', expand=False)
        self.sidebar_listbox = tk.Listbox(self.left_frame, bg='#2c2c2c', fg='#dcdcdc', selectbackground='#3a3a5c', selectforeground='#dcdcdc', font=('Arial', 10))
        self.sidebar_scrollbar = tk.Scrollbar(self.left_frame, orient='vertical', command=self.sidebar_listbox.yview)
        self.sidebar_listbox.config(yscrollcommand=self.sidebar_scrollbar.set)
        self.sidebar_scrollbar.pack(side='right', fill='y')
        self.sidebar_listbox.pack(side='left', fill='both', expand=True, padx=5, pady=5)
        self.sidebar_listbox.bind('<<ListboxSelect>>', self.on_sidebar_select)

        # --- CENTER FRAME (Workspace) ---
        self.center_frame = tk.Frame(self.main_pane, bg='#1e1e1e')
        self.main_pane.add(self.center_frame)

        self.top_bar_frame = tk.Frame(self.center_frame, bg='#1e1e1e')
        self.top_bar_frame.pack(fill='x')
        self.btn_delete_tag = tk.Button(self.top_bar_frame, text="☢ Delete Tag", bg='#7b1a1a', fg='white', font=('Arial', 9, 'bold'), command=self.handle_delete_tag)
        self.btn_delete_tag.pack(side='right')

        self.dir_frame = tk.Frame(self.center_frame, bg='#1e1e1e')
        self.dir_frame.pack(pady=(10, 5), fill='x')
        tk.Label(self.dir_frame, text="📁 Image Directory:", bg='#1e1e1e', fg='#dcdcdc', font=('Arial', 10, 'bold')).pack(side='left', padx=5)
        self.dir_path_label = tk.Label(self.dir_frame, text="Not selected", bg='#1e1e1e', fg='#aaaaaa', font=('Arial', 10))
        self.dir_path_label.pack(side='left', padx=5)
        self.btn_select_dir = tk.Button(self.dir_frame, text="Browse...", bg='#0078d7', fg='white', font=('Arial', 10, 'bold'), command=self.select_directory)
        self.btn_select_dir.pack(side='left', padx=5)
        # [FIX: Refresh relocation & functionality]
        self.btn_full_reset = tk.Button(self.dir_frame, text="🔄 Full Reset", bg='#1a7b7b', fg='white', font=('Arial', 10, 'bold'), command=self.handle_full_reset)
        self.btn_full_reset.pack(side='left', padx=5)

        self.tag_label = tk.Label(self.center_frame, text="", bg='#1e1e1e', fg='#dcdcdc', font=('Arial', 18, 'bold'))
        self.tag_label.pack(pady=(10, 0))
        self.presence_label = tk.Label(self.center_frame, text="", bg='#1e1e1e', fg='#dcdcdc', font=('Arial', 12))
        self.presence_label.pack(pady=5)

        self.progress1_label = tk.Label(self.center_frame, text="", bg='#1e1e1e', fg='#dcdcdc', font=('Arial', 10))
        self.progress1_label.pack(pady=2)
        self.progress2_label = tk.Label(self.center_frame, text="", bg='#1e1e1e', fg='#dcdcdc', font=('Arial', 10))
        self.progress2_label.pack(pady=2)
        self.backlog_label = tk.Label(self.center_frame, text="", bg='#1e1e1e', fg='#dcdcdc', font=('Arial', 10))
        self.backlog_label.pack(pady=2)

        # Centered Image Zone
        self.image_zone_frame = tk.Frame(self.center_frame, bg='#1e1e1e', relief=tk.SUNKEN, bd=3)
        self.image_zone_frame.pack(fill='both', expand=True, padx=20, pady=15)

        self.img_canvas = tk.Canvas(self.image_zone_frame, bg='#1e1e1e', highlightthickness=0)
        self.img_canvas.pack(fill='both', expand=True)

        self.image_label = tk.Label(self.img_canvas, bg='#1e1e1e')
        self.image_label.bind("<Button-1>", lambda e: self.open_zoom_overlay())

        # Action Buttons
        btn_frame1 = tk.Frame(self.center_frame, bg='#1e1e1e')
        btn_frame1.pack(pady=10)
        self.btn_yes = tk.Button(btn_frame1, text="✅ YES", bg='#28a745', fg='white', font=('Arial', 14, 'bold'), width=16, command=self.handle_yes)
        self.btn_yes.pack(side='left', padx=30)
        self.btn_no = tk.Button(btn_frame1, text="❌ NO", bg='#dc3545', fg='white', font=('Arial', 14, 'bold'), width=16, command=self.handle_no)
        self.btn_no.pack(side='left', padx=30)

        btn_frame2 = tk.Frame(self.center_frame, bg='#1e1e1e')
        btn_frame2.pack(pady=5)
        self.btn_skip = tk.Button(btn_frame2, text="⏭ Skip", bg='#e07b00', fg='white', font=('Arial', 11), width=10, command=self.handle_skip)
        self.btn_skip.pack(side='left', padx=5)
        self.btn_back = tk.Button(btn_frame2, text="↩ Back", bg='#555555', fg='white', font=('Arial', 11), width=10, command=self.handle_back)
        self.btn_back.pack(side='left', padx=5)
        self.btn_skip_tag = tk.Button(btn_frame2, text="⏭ Skip Tag", bg='#1a6faf', fg='white', font=('Arial', 11), width=10, command=self.handle_skip_tag)
        self.btn_skip_tag.pack(side='left', padx=5)
        # [FIX: Removed redundant center refresh button]

        tk.Label(self.center_frame, textvariable=self.status_var, bg='#1e1e1e', fg='#dcdcdc').pack(pady=5)

        # File State & Action Log (Bottom Center)
        self.bottom_info_frame = tk.Frame(self.center_frame, bg='#1e1e1e')
        self.bottom_info_frame.pack(fill='both', expand=False, pady=10)
        
        fs_frame = tk.Frame(self.bottom_info_frame, bg='#1e1e1e')
        fs_frame.pack(side='left', fill='both', expand=True, padx=5)
        tk.Label(fs_frame, text="📄 File State", bg='#1e1e1e', fg='#dcdcdc', font=('Arial', 12, 'bold')).pack(anchor='w')
        # [FIX: Removed redundant bottom refresh button]
        self.file_state_text = tk.Text(fs_frame, height=6, bg='#1a1a2e', fg='#dcdcdc', font=('Courier', 10), state='disabled', wrap='word')
        self.file_state_text.pack(fill='both', expand=True, padx=5, pady=5)

        al_frame = tk.Frame(self.bottom_info_frame, bg='#1e1e1e')
        al_frame.pack(side='right', fill='both', expand=True, padx=5)
        tk.Label(al_frame, text="📜 Action Log", bg='#1e1e1e', fg='#dcdcdc', font=('Arial', 12, 'bold')).pack(anchor='w')
        al_scroll = tk.Scrollbar(al_frame, orient='vertical')
        al_scroll.pack(side='right', fill='y')
        self.action_log_text = tk.Text(al_frame, height=6, bg='#1a1a2e', fg='#dcdcdc', font=('Courier', 10), state='disabled', wrap='word')
        self.action_log_text.pack(fill='both', expand=True, padx=5, pady=5)
        al_scroll.config(command=self.action_log_text.yview)
        self.action_log_text.config(yscrollcommand=al_scroll.set)

        self.action_log_text.tag_configure('added', foreground='#28a745')
        self.action_log_text.tag_configure('removed', foreground='#dc3545')
        self.action_log_text.tag_configure('confirmed', foreground='#0078d7')
        self.action_log_text.tag_configure('skipped', foreground='#e07b00')
        self.action_log_text.tag_configure('undone', foreground='#888888')
        self.action_log_text.tag_configure('deleted', foreground='#7b1a1a')
        self.action_log_text.tag_configure('warning', foreground='#ffff00')

        # --- RIGHT SIDEBAR (Queue) ---
        # [FIX: Performance] Use Listbox instead of individual Label widgets.
        # Creating 1000 Label widgets was the primary source of lag on tag selection.
        # Listbox handles thousands of items natively and uses itemconfig for O(1) color updates.
        self.right_frame = tk.Frame(self.main_pane, bg='#2c2c2c', width=220)
        self.main_pane.add(self.right_frame, minsize=150)
        tk.Label(self.right_frame, text="🖼 Image Queue", bg='#2c2c2c', fg='#dcdcdc', font=('Arial', 14, 'bold')).pack(pady=10)

        self.queue_vsb = tk.Scrollbar(self.right_frame, orient='vertical')
        self.queue_vsb.pack(side='right', fill='y')
        self.queue_listbox = tk.Listbox(
            self.right_frame, bg='#2c2c2c', fg='white',
            font=('Arial', 9), selectmode='single', activestyle='none',
            highlightthickness=0, bd=0,
            yscrollcommand=self.queue_vsb.set
        )
        self.queue_vsb.config(command=self.queue_listbox.yview)
        self.queue_listbox.pack(side='left', fill='both', expand=True, padx=5, pady=5)
        self.queue_listbox.bind('<<ListboxSelect>>', self._on_queue_listbox_select)

        # [FIX: Sidebar resize lag] Throttled configuration updates
        self.main_pane.bind('<<PaneConfigure>>', self._on_pane_configure)

        # [FIX: Minimize/Restore lag]
        self.root.bind('<Unmap>', self._on_unmap)
        self.root.bind('<Map>', self._on_map)

    def _on_pane_configure(self, event):
        # Throttle to prevent real-time lag during drag
        if self._resize_debounce_timer:
            self.root.after_cancel(self._resize_debounce_timer)
        self._resize_debounce_timer = self.root.after(300, self._commit_resize_update)

    def _commit_resize_update(self):
        self._resize_debounce_timer = None
        if not self._is_minimized:
            self.update_queue_panel_colors()

    def _on_unmap(self, event):
        self._is_minimized = True

    def _on_map(self, event):
        self._is_minimized = False
        # Defer heavy redraw until Tkinter idle state to prevent restore lag spike
        self.root.after_idle(self._defer_resize_update)

    def _defer_resize_update(self):
        if not self._is_resizing_sidebar:
            self._is_resizing_sidebar = True
            self.root.after(100, self._commit_resize_update)

    def select_directory(self):
        if self.image_directory and not messagebox.askyesno("Change Directory", "Loading a new directory will reset your current session. Any unsaved progress will be lost. Continue?"):
            return
        dir_path = filedialog.askdirectory(title="Select Image Directory")
        if not dir_path:
            return
        self.image_directory = dir_path
        self.dir_path_label.config(text=os.path.basename(dir_path), fg='#dcdcdc')
        self.status_var.set(f"🔍 Scanning {os.path.basename(dir_path)}...")
        self.root.update_idletasks()
        self.scan_complete_var.set(False)
        if self.startup_scan():
            self.root.wait_variable(self.scan_complete_var)
            if self.master_tag_list:
                self.load_next_tag()
        else:
            self.status_var.set("❌ Scan failed. Please select a valid directory.")

    def startup_scan(self):
        if not self.image_directory or not os.path.isdir(self.image_directory):
            messagebox.showerror("Error", "No valid directory selected.")
            return False

        def _background_scan():
            if not self.root.winfo_exists():
                return

            exts = ['*.png', '*.jpg', '*.jpeg', '*.bmp', '*.webp']
            image_files = []
            for ext in exts:
                image_files.extend(glob.glob(os.path.join(self.image_directory, ext)))
                image_files.extend(glob.glob(os.path.join(self.image_directory, ext.upper())))
            image_files = sorted(list(set(image_files)))

            if not image_files:
                self.root.after(0, lambda: messagebox.showerror("Error", "No image files found in the selected directory."))
                self.root.after(0, lambda: self._reset_session_state())
                self.root.after(0, lambda: self.scan_complete_var.set(True))
                return

            txt_files = glob.glob(os.path.join(self.image_directory, '*.txt'))
            tag_set = set()
            for txt in txt_files:
                try:
                    with open(txt, 'r', encoding='utf-8') as f:
                        content = f.read().strip()
                        if content:
                            tags = [t.strip() for t in content.split(',') if t.strip()]
                            tag_set.update(tags)
                except Exception:
                    pass

            master_tag_list = sorted(list(tag_set))
            tag_status = {tag: 'pending' for tag in master_tag_list}
            tag_counts = {tag: 0 for tag in master_tag_list}
            for txt in txt_files:
                tags = self.read_txt_file(txt)
                for t in tags:
                    if t in tag_counts:
                        tag_counts[t] += 1

            self.root.after(0, lambda: self._apply_scan_results(image_files, master_tag_list, tag_status, tag_counts))
            self.root.after(0, lambda: self.scan_complete_var.set(True))

        threading.Thread(target=_background_scan, daemon=True).start()
        return True

    def _apply_scan_results(self, image_files, master_tag_list, tag_status, tag_counts):
        self.image_files = image_files
        self.master_tag_list = master_tag_list
        self.tag_status = tag_status
        self.tag_counts = tag_counts
        self.current_tag_index = -1
        self.image_queue = []
        self.skip_backlog = set()
        self.undo_history = []
        self.action_log = []
        self.current_image_path = None
        self.tk_image = None
        if self.current_pil_image:
            self.current_pil_image.close()
            self.current_pil_image = None
        self.status_var.set("✅ Scan complete.")
        self.all_images_for_tag = []
        self.total_images_for_tag = 0
        self.current_image_seq = 0
        self.current_position = -1
        self.session_processed = set()
        self.session_decision = {}
        self.queue_path_list = []
        self.queue_path_index = {}
        self._prev_queue_img = None
        self.update_sidebar()
        self.update_queue_panel()

    def _reset_session_state(self):
        self.image_files = []
        self.master_tag_list = []
        self.tag_status = {}
        self.tag_counts = {}
        self.current_tag_index = -1
        self.image_queue = []
        self.skip_backlog = set()
        self.undo_history = []
        self.action_log = []
        self.current_image_path = None
        self.tk_image = None
        self.current_pil_image = None
        self.all_images_for_tag = []
        self.total_images_for_tag = 0
        self.current_image_seq = 0
        self.current_position = -1
        self.session_processed = set()
        self.session_decision = {}
        self.queue_path_list = []
        self.queue_path_index = {}
        self._prev_queue_img = None
        self.status_var.set("❌ Scan failed.")

    # [FIX: Full Reset functionality]
    def handle_full_reset(self):
        if not messagebox.askyesno("Full Reset", "This will clear all session data and re-scan the current directory. Continue?"):
            return
        self._reset_session_state()
        self.sidebar_listbox.delete(0, tk.END)
        self.queue_listbox.delete(0, tk.END)
        self.queue_path_list = []
        self.queue_path_index = {}
        self._prev_queue_img = None
        self.tag_label.config(text="")
        self.presence_label.config(text="", fg='#dcdcdc')
        self.progress1_label.config(text="")
        self.progress2_label.config(text="")
        self.backlog_label.config(text="")
        self.file_state_text.config(state='normal')
        self.file_state_text.delete('1.0', tk.END)
        self.file_state_text.config(state='disabled')
        self.action_log_text.config(state='normal')
        self.action_log_text.delete('1.0', tk.END)
        self.action_log_text.config(state='disabled')
        if self.image_directory:
            self.status_var.set("🔄 Resetting & Re-scanning...")
            self.root.update_idletasks()
            self.scan_complete_var.set(False)
            self.startup_scan()
        else:
            self.status_var.set("🔄 Session Reset. Select a directory.")

    def read_txt_file(self, path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                content = f.read().strip()
                return [t.strip() for t in content.split(',') if t.strip()] if content else []
        except Exception:
            return []

    def write_txt_file(self, path, tags):
        try:
            with open(path, 'w', encoding='utf-8') as f:
                f.write(', '.join(tags) if tags else "")
        except Exception as e:
            messagebox.showerror("File Write Error", f"Failed to write to {path}\n{str(e)}")
            raise

    def pre_action_verify(self, expected_tags, file_path):
        self.pre_action_result = None
        
        current_tags = self.read_txt_file(file_path)
        if sorted(current_tags) == sorted(expected_tags):
            return 'proceed'

        dialog = tk.Toplevel(self.root)
        dialog.title("State Mismatch")
        dialog.geometry("400x200")
        dialog.configure(bg='#2c2c2c')
        dialog.transient(self.root)
        dialog.grab_set()

        tk.Label(dialog, text="File state changed since loading.\nProceed with action?", bg='#2c2c2c', fg='#dcdcdc').pack(pady=20)
        btn_frame = tk.Frame(dialog, bg='#2c2c2c')
        btn_frame.pack(pady=10)

        def on_proceed():
            self.pre_action_result = 'proceed'
            dialog.destroy()
        def on_reload():
            self.pre_action_result = 'reload'
            dialog.destroy()
        def on_cancel():
            self.pre_action_result = 'cancel'
            dialog.destroy()

        tk.Button(btn_frame, text="Proceed Anyway", bg='#28a745', fg='white', command=on_proceed).pack(side='left', padx=5)
        tk.Button(btn_frame, text="Reload and Reconsider", bg='#e07b00', fg='white', command=on_reload).pack(side='left', padx=5)
        tk.Button(btn_frame, text="Cancel", bg='#555555', fg='white', command=on_cancel).pack(side='left', padx=5)

        dialog.protocol("WM_DELETE_WINDOW", on_cancel)
        self.root.wait_window(dialog)
        return self.pre_action_result

    def log_action(self, icon_action, image_path):
        timestamp = datetime.datetime.now().strftime("%H:%M:%S")
        filename = os.path.basename(image_path) if image_path else ""
        entry = f"{timestamp} | {icon_action} → {filename}" if image_path else f"{timestamp} | {icon_action}"
        self.action_log.append(entry)
        if len(self.action_log) > 50:
            self.action_log.pop(0)
        self.update_action_log_display()

    def update_action_log_display(self):
        self.action_log_text.config(state='normal')
        self.action_log_text.delete('1.0', tk.END)
        for line in self.action_log[-20:]:
            if '✅ Added' in line: tag = 'added'
            elif '❌ Removed' in line: tag = 'removed'
            elif '✔ Confirmed' in line or '✖ Confirmed' in line: tag = 'confirmed'
            elif '⏭ Skipped' in line or '⏭ Skip Tag' in line: tag = 'skipped'
            elif '↩ Undid' in line: tag = 'undone'
            elif '🗑 Deleted' in line: tag = 'deleted'
            elif '⚠️' in line: tag = 'warning'
            else: tag = None

            tags_to_use = (tag,) if tag else ()
            self.action_log_text.insert(tk.END, line + "\n", tags_to_use)
        self.action_log_text.config(state='disabled')
        self.action_log_text.see(tk.END)

    def update_sidebar(self):
        # [FIX: Performance] Use flag guard instead of unbind/rebind to prevent
        # recursive events while still avoiding the cost of re-registering the binding.
        self._updating_sidebar = True
        self.sidebar_listbox.delete(0, tk.END)
        total = len(self.image_files)
        for tag in self.master_tag_list:
            status = self.tag_status.get(tag, 'pending')
            icons = {'pending': '⏳', 'completed': '✅', 'skipped': '⏭', 'deleted': '🗑'}
            icon = icons.get(status, '⏳')
            count = self.tag_counts.get(tag, 0)
            self.sidebar_listbox.insert(tk.END, f"{icon} {tag} ({count}/{total})")
        if self.current_tag_index >= 0:
            self.sidebar_listbox.selection_set(self.current_tag_index)
            self.sidebar_listbox.see(self.current_tag_index)
        self._updating_sidebar = False

    def _update_sidebar_item(self, index):
        """Update a single sidebar item without full rebuild — O(1) instead of O(n)."""
        if index < 0 or index >= len(self.master_tag_list):
            return
        tag = self.master_tag_list[index]
        status = self.tag_status.get(tag, 'pending')
        icons = {'pending': '⏳', 'completed': '✅', 'skipped': '⏭', 'deleted': '🗑'}
        icon = icons.get(status, '⏳')
        count = self.tag_counts.get(tag, 0)
        total = len(self.image_files)
        self._updating_sidebar = True
        self.sidebar_listbox.delete(index)
        self.sidebar_listbox.insert(index, f"{icon} {tag} ({count}/{total})")
        self._updating_sidebar = False

    def _set_sidebar_selection(self, index):
        """Update selection highlight only — no list rebuild."""
        self._updating_sidebar = True
        self.sidebar_listbox.selection_clear(0, tk.END)
        if 0 <= index < self.sidebar_listbox.size():
            self.sidebar_listbox.selection_set(index)
            self.sidebar_listbox.see(index)
        self._updating_sidebar = False

    def update_center(self):
        if self.current_tag_index < 0 or self.current_tag_index >= len(self.master_tag_list):
            return
        current_tag = self.master_tag_list[self.current_tag_index]
        self.tag_label.config(text=current_tag)
        txt_path = os.path.splitext(self.current_image_path)[0] + '.txt' if self.current_image_path else ""
        current_tags = self.read_txt_file(txt_path) if self.current_image_path else []

        if current_tag in current_tags:
            self.presence_label.config(text="✅ TAG PRESENT IN FILE", fg='#28a745')
        else:
            self.presence_label.config(text="❌ TAG NOT IN FILE", fg='#dc3545')

        done = len(self.session_processed)
        remaining = self.total_images_for_tag - done
        self.progress1_label.config(text=f"🖼 Image {done + 1} of {self.total_images_for_tag} — {remaining} remaining")
        self.progress2_label.config(text=f"🏷 Tag {self.current_tag_index + 1} of {len(self.master_tag_list)}")
        self.backlog_label.config(text=f"⏳ Skip backlog: {len(self.skip_backlog)} images")

    def update_right(self):
        if not self.current_image_path:
            return
        txt_path = os.path.splitext(self.current_image_path)[0] + '.txt'
        content = ""
        try:
            with open(txt_path, 'r', encoding='utf-8') as f:
                content = f.read()
        except Exception:
            content = "[File read error]"
        self.file_state_text.config(state='normal')
        self.file_state_text.delete('1.0', tk.END)
        self.file_state_text.insert(tk.END, content)
        self.file_state_text.config(state='disabled')

    def load_image_display(self, pil_img=None):
        self.image_label.config(image='')
        self.tk_image = None
        self.update_center()
        self.update_right()

        if not self.current_image_path:
            if self.current_pil_image:
                self.current_pil_image.close()
            self.current_pil_image = None
            return

        try:
            if self.current_pil_image and self.current_pil_image is not pil_img:
                self.current_pil_image.close()
            self.current_pil_image = pil_img if pil_img else Image.open(self.current_image_path)
            self._fit_image_to_zone(self.current_pil_image)
        except Exception as e:
            if self.current_pil_image:
                self.current_pil_image.close()
            self.current_pil_image = None
            self.log_action(f"⚠️ Image load failed: {os.path.basename(self.current_image_path)}", self.current_image_path)
            self.image_label.config(text=f"Error loading image:\n{str(e)}", image="", fg='#dc3545')
            messagebox.showwarning("Image Load Error", f"Skipping image due to error:\n{str(e)}")
            self.root.after(0, self.load_next_image)

    def _fit_image_to_zone(self, pil_img):
        try:
            self.root.update_idletasks()
            target_w = self.img_canvas.winfo_width()
            target_h = self.img_canvas.winfo_height()
            if target_w <= 1 or target_h <= 1:
                target_w, target_h = 512, 512
                
            orig_w, orig_h = pil_img.size
            
            scale = min(target_w / orig_w, target_h / orig_h)
            new_w = int(orig_w * scale)
            new_h = int(orig_h * scale)

            self.img_canvas.delete("all")
            self.image_label.config(image='')

            resized_img = pil_img.resize((new_w, new_h), Image.LANCZOS)
            self.tk_image = ImageTk.PhotoImage(resized_img)
            self.image_label.config(image=self.tk_image)
            
            resized_img.close()
            
            center_x = target_w // 2
            center_y = target_h // 2
            self.img_canvas.create_window((center_x, center_y), window=self.image_label, anchor='center')
        except Exception:
            raise

    def load_next_image(self):
        """Advance to the next image using position-based model.
        Always moves forward from current_position in all_images_for_tag order.
        Skips session_processed images; recycles skip_backlog images only when
        no non-skipped unprocessed images remain.
        """
        total = len(self.all_images_for_tag)
        if total == 0:
            if 0 <= self.current_tag_index < len(self.master_tag_list):
                self.tag_status[self.master_tag_list[self.current_tag_index]] = 'completed'
            self.update_sidebar()
            self.load_next_tag()
            return

        # All definitively processed (yes/no) — tag complete
        if len(self.session_processed) >= total:
            if 0 <= self.current_tag_index < len(self.master_tag_list):
                self.tag_status[self.master_tag_list[self.current_tag_index]] = 'completed'
            self.update_sidebar()
            self.load_next_tag()
            return

        start = (self.current_position + 1) % total

        # Pass 1: find next image that is unprocessed AND not in skip backlog
        for i in range(total):
            idx = (start + i) % total
            p = self.all_images_for_tag[idx]
            if p not in self.session_processed and p not in self.skip_backlog:
                self.current_position = idx
                self.current_image_path = p
                self.load_image_display()
                self.root.after(0, self.update_queue_panel_colors)
                return

        # Pass 2: only skipped images remain — cycle through them
        for i in range(total):
            idx = (start + i) % total
            p = self.all_images_for_tag[idx]
            if p not in self.session_processed:
                self.current_position = idx
                self.current_image_path = p
                self.load_image_display()
                self.root.after(0, self.update_queue_panel_colors)
                return

        # Should not reach here — all processed
        if 0 <= self.current_tag_index < len(self.master_tag_list):
            self.tag_status[self.master_tag_list[self.current_tag_index]] = 'completed'
        self.update_sidebar()
        self.load_next_tag()

    def load_next_tag(self):
        if not self.master_tag_list:
            self.check_all_complete()
            return
        self.current_tag_index = -1
        for i, tag in enumerate(self.master_tag_list):
            if self.tag_status[tag] == 'pending':
                self.current_tag_index = i
                break
        if self.current_tag_index == -1:
            self.check_all_complete()
            return
        self.undo_history = []
        self.image_queue = []
        self.skip_backlog = set()
        self.session_processed = set()
        self.session_decision = {}
        self.all_images_for_tag = list(self.image_files)
        self.total_images_for_tag = len(self.all_images_for_tag)
        self.current_image_seq = 0
        self.current_position = -1
        self.update_sidebar()
        self.update_queue_panel()
        self.load_next_image()

    def check_all_complete(self):
        pending = any(status == 'pending' for status in self.tag_status.values())
        if not pending:
            self.tag_label.config(text="✅ All Tags Processed")
            self.presence_label.config(text="", fg='#dcdcdc')
            self.image_label.config(text="", image="")
            self.tk_image = None
            if self.current_pil_image:
                self.current_pil_image.close()
            self.current_pil_image = None
            self.progress1_label.config(text="")
            self.progress2_label.config(text="")
            self.backlog_label.config(text="")
            self.status_var.set("🎉 Session Complete")
            messagebox.showinfo("Completion", "All tags have been processed, skipped, or deleted.")

    def handle_yes(self):
        if not self.current_image_path: return
        self._process_decision('yes')

    def handle_no(self):
        if not self.current_image_path: return
        self._process_decision('no')

    def _process_decision(self, decision):
        txt_path = os.path.splitext(self.current_image_path)[0] + '.txt'
        current_tags = self.read_txt_file(txt_path)
        self.expected_tags = list(current_tags)
        new_tags = list(current_tags)
        current_tag = self.master_tag_list[self.current_tag_index]

        has_tag = current_tag in current_tags
        needs_write = False
        action_desc = ""

        if decision == 'yes':
            action_desc = f"✔ Confirmed present {current_tag}" if has_tag else f"✅ Added {current_tag}"
            needs_write = not has_tag
        else:
            action_desc = f"✖ Confirmed absent {current_tag}" if not has_tag else f"❌ Removed {current_tag}"
            needs_write = has_tag

        if needs_write:
            result = self.pre_action_verify(self.expected_tags, txt_path)
            if result == 'cancel':
                return
            if result == 'reload':
                self.handle_refresh()
                return

            current_tags = self.read_txt_file(txt_path)
            new_tags = list(current_tags)
            
            has_tag = current_tag in current_tags
            if decision == 'yes':
                needs_write = not has_tag
            else:
                needs_write = has_tag

            if decision == 'yes' and current_tag not in current_tags:
                new_tags.append(current_tag)
            elif decision == 'no' and current_tag in current_tags:
                new_tags = [t for t in new_tags if t != current_tag]

            try:
                self.write_txt_file(txt_path, new_tags)
            except Exception:
                return

            if decision == 'yes' and not has_tag:
                self.tag_counts[current_tag] += 1
            elif decision == 'no' and has_tag:
                # [FIX: Safety] Direct dict access with safe fallback
                self.tag_counts[current_tag] = max(0, self.tag_counts.get(current_tag, 0) - 1)

        self.log_action(action_desc, self.current_image_path)
        self.undo_history.append({
            'image_path': self.current_image_path,
            'action': decision,
            'tag': current_tag,
            'tags_before': list(current_tags),
            'tags_after': list(new_tags)
        })
        if len(self.undo_history) > 100:
            self.undo_history.pop(0)

        # Mark this image as definitively processed for this session
        self.session_processed.add(self.current_image_path)
        self.session_decision[self.current_image_path] = decision
        self.skip_backlog.discard(self.current_image_path)
        # Advance current_position to match so load_next_image starts correctly
        try:
            self.current_position = self.all_images_for_tag.index(self.current_image_path)
        except ValueError:
            pass
        self.root.after(0, self.update_queue_panel_colors)
        self.load_next_image()

    def handle_skip(self):
        if not self.current_image_path: return
        # Add to skip_backlog (will be recycled) and record decision for color coding
        self.skip_backlog.add(self.current_image_path)
        self.session_decision[self.current_image_path] = 'skip'
        # Advance position so load_next_image starts after this image
        try:
            self.current_position = self.all_images_for_tag.index(self.current_image_path)
        except ValueError:
            pass
        # Check if all remaining are stuck in skip backlog
        unprocessed = [p for p in self.all_images_for_tag if p not in self.session_processed]
        if unprocessed and all(p in self.skip_backlog for p in unprocessed):
            self.status_var.set("⚠️ Warning: All remaining images are in skip backlog. Press Skip Tag or Back to proceed.")
        else:
            self.status_var.set(f"⏳ Skip backlog: {len(self.skip_backlog)}")
        self.log_action("⏭ Skipped", self.current_image_path)
        self.load_next_image()

    def handle_back(self):
        if not self.undo_history:
            self.status_var.set("↩ Undo history is empty.")
            return

        entry = self.undo_history.pop()
        txt_path = os.path.splitext(entry['image_path'])[0] + '.txt'

        try:
            self.write_txt_file(txt_path, entry['tags_before'])
        except Exception as e:
            messagebox.showerror("Undo Failed", str(e))
            return

        # Revert tag count changes
        if 0 <= self.current_tag_index < len(self.master_tag_list):
            current_tag = entry['tag']
            tags_before = entry['tags_before']
            tags_after  = entry['tags_after']
            tag_was_added   = (current_tag in tags_after)  and (current_tag not in tags_before)
            tag_was_removed = (current_tag in tags_before) and (current_tag not in tags_after)
            if tag_was_added:
                self.tag_counts[current_tag] = max(0, self.tag_counts.get(current_tag, 0) - 1)
            elif tag_was_removed:
                self.tag_counts[current_tag] += 1
                self._update_sidebar_item(self.current_tag_index)

        # Revert session state for the restored image
        restored_path = entry['image_path']
        self.session_processed.discard(restored_path)
        self.session_decision.pop(restored_path, None)
        self.skip_backlog.discard(restored_path)

        # Set position to the restored image — load_next_image will start AFTER it
        try:
            self.current_position = self.all_images_for_tag.index(restored_path)
        except ValueError:
            self.current_position = 0

        self.current_image_path = restored_path
        self.load_image_display()
        self.log_action("↩ Undid action", self.current_image_path)
        self.status_var.set(f"🔄 Restored {os.path.basename(restored_path)}")
        self.root.after(0, self.update_queue_panel_colors)

    def handle_skip_tag(self):
        if self.current_tag_index < 0: return
        self.tag_status[self.master_tag_list[self.current_tag_index]] = 'skipped'
        self.image_queue = []
        self.skip_backlog = set()
        self.undo_history = []
        self.log_action(f"⏭ Skip Tag used on {self.master_tag_list[self.current_tag_index]}", self.current_image_path)
        self.update_sidebar()
        # Queue panel doesn't need rebuild
        self.load_next_tag()

    def handle_delete_tag(self):
        if self.current_tag_index < 0 or not self.image_directory: return
        current_tag = self.master_tag_list[self.current_tag_index]

        dialog = tk.Toplevel(self.root)
        dialog.title("Confirm Delete")
        dialog.geometry("500x250")
        dialog.configure(bg='#2c2c2c')
        dialog.transient(self.root)
        dialog.grab_set()

        tk.Label(dialog, text="☢ DESTRUCTIVE ACTION\n\nDelete tag from ALL images in this set?\n\nThis cannot be undone.",
                 bg='#2c2c2c', fg='#ff4444', font=('Arial', 12, 'bold'), justify='center').pack(pady=30)
        btn_frame = tk.Frame(dialog, bg='#2c2c2c')
        btn_frame.pack(pady=20)

        def confirm():
            dialog.destroy()
            self._execute_delete_tag(current_tag)
        def cancel():
            dialog.destroy()

        tk.Button(btn_frame, text="DELETE FROM ALL FILES", bg='#7b1a1a', fg='white', font=('Arial', 11, 'bold'), command=confirm).pack(side='left', padx=20)
        tk.Button(btn_frame, text="Cancel", bg='#555555', fg='white', font=('Arial', 11), command=cancel).pack(side='left', padx=20)

        dialog.protocol("WM_DELETE_WINDOW", cancel)

    def _execute_delete_tag(self, current_tag):
        modified_count = 0
        txt_files = glob.glob(os.path.join(self.image_directory, '*.txt'))
        for txt in txt_files:
            tags = self.read_txt_file(txt)
            if current_tag in tags:
                new_tags = [t for t in tags if t != current_tag]
                try:
                    self.write_txt_file(txt, new_tags)
                    modified_count += 1
                except Exception:
                    pass
        self.tag_status[current_tag] = 'deleted'
        self.tag_counts[current_tag] = 0

        self.image_queue = []
        self.skip_backlog = set()
        self.undo_history = []
        self.log_action(f"🗑 Deleted tag: {current_tag} from {modified_count} files", None)
        self.update_sidebar()
        # Queue panel doesn't need rebuild
        self.load_next_tag()

    def handle_refresh(self):
        if not self.current_image_path: return
        self.load_image_display()
        self.status_var.set("🔄 Refreshed file state")

    def on_sidebar_select(self, event):
        if self._updating_sidebar:
            return
        sel = self.sidebar_listbox.curselection()
        if not sel: return
        tag = self.master_tag_list[sel[0]]
        self.jump_to_tag(tag)

    def jump_to_tag(self, tag):
        if tag not in self.master_tag_list:
            return
        if self.tag_status.get(tag) not in ['completed', 'deleted']:
            self.tag_status[tag] = 'pending'
            
        self.current_tag_index = self.master_tag_list.index(tag)
        self.image_queue = []
        self.skip_backlog = set()
        self.session_processed = set()
        self.session_decision = {}
        self.undo_history = []
        self.all_images_for_tag = list(self.image_files)
        self.total_images_for_tag = len(self.all_images_for_tag)
        self.current_image_seq = 0
        self.current_position = -1
        # [FIX: Performance] Only update the single affected sidebar item + selection,
        # instead of deleting and reinserting ALL tags (O(1) vs O(n)).
        self._update_sidebar_item(self.current_tag_index)
        self._set_sidebar_selection(self.current_tag_index)
        # [FIX: Queue Sidebar] Rebuild queue panel when manually jumping to a tag
        self.update_queue_panel()
        self.load_next_image()

    def update_queue_panel(self):
        # [FIX: Performance] Listbox.insert is orders of magnitude faster than
        # creating individual tk.Label widgets. 1000 labels = ~1000 widget allocations;
        # 1000 Listbox.insert calls = a single C-level batch operation.
        self.queue_listbox.delete(0, tk.END)
        self.queue_path_list = []
        self.queue_path_index = {}
        self._prev_queue_img = None
        if not self.all_images_for_tag:
            return
        names = []
        for i, img_path in enumerate(self.all_images_for_tag):
            names.append(os.path.splitext(os.path.basename(img_path))[0])
            self.queue_path_list.append(img_path)
            self.queue_path_index[img_path] = i
        # Batch-insert all names in one call for maximum speed
        self.queue_listbox.insert(tk.END, *names)

    # Decision color map — used by update_queue_panel_colors
    _DECISION_COLORS = {
        'yes':  ('#1a5c2a', 'white'),   # green
        'no':   ('#7b1a1a', 'white'),   # red
        'skip': ('#7a4a00', 'white'),   # orange
    }
    _CURRENT_BG = '#0078d7'            # blue for currently displayed image

    def update_queue_panel_colors(self):
        """Repaint every queue item to reflect current session_decision + current image.
        Called after every action (yes/no/skip/back/jump). Uses itemconfig — O(n) Tcl
        calls but each is very cheap (no widget creation).
        """
        if not self.queue_path_index:
            return
        for path, idx in self.queue_path_index.items():
            try:
                if path == self.current_image_path:
                    self.queue_listbox.itemconfig(idx, bg=self._CURRENT_BG, fg='white')
                else:
                    decision = self.session_decision.get(path)
                    bg, fg = self._DECISION_COLORS.get(decision, ('#2c2c2c', 'white'))
                    self.queue_listbox.itemconfig(idx, bg=bg, fg=fg)
            except tk.TclError:
                pass
        # Auto-scroll to current image
        if self.current_image_path and self.current_image_path in self.queue_path_index:
            try:
                self.queue_listbox.see(self.queue_path_index[self.current_image_path])
            except tk.TclError:
                pass
        self._prev_queue_img = self.current_image_path

    def _on_queue_listbox_select(self, event):
        """Handle click on queue Listbox — bridge to jump_to_queue_image."""
        sel = self.queue_listbox.curselection()
        if not sel:
            return
        idx = sel[0]
        if idx < len(self.queue_path_list):
            self.jump_to_queue_image(self.queue_path_list[idx])

    def jump_to_queue_image(self, target_path):
        """Jump directly to any image in the queue (already processed or not).
        Sets current_position so load_next_image continues from immediately after.
        Does NOT mark the target as processed — the user must press Yes/No/Skip.
        """
        if target_path not in self.all_images_for_tag:
            return
        try:
            self.current_position = self.all_images_for_tag.index(target_path)
        except ValueError:
            return
        self.current_image_path = target_path
        self.load_image_display()
        self.root.after(0, self.update_queue_panel_colors)

    # --- ZOOM OVERLAY SYSTEM ---
    def open_zoom_overlay(self):
        if not self.current_pil_image: return
        if self.zoom_overlay and self.zoom_overlay.winfo_exists():
            self.zoom_overlay.lift()
            return

        self.zoom_overlay = tk.Toplevel(self.root)
        self.zoom_overlay.title("🔍 Zoom Viewer")
        self.zoom_overlay.geometry(f"{self.root.winfo_width()}x{self.root.winfo_height()}")
        self.zoom_overlay.configure(bg='#1a1a2e')
        self.zoom_overlay.transient(self.root)
        self.zoom_overlay.grab_set()
        self.zoom_overlay.resizable(False, False)

        self.overlay_canvas = tk.Canvas(self.zoom_overlay, bg='#1a1a2e', highlightthickness=0)
        self.overlay_canvas.pack(fill='both', expand=True, padx=40, pady=40)
        
        self.overlay_label = tk.Label(self.overlay_canvas, bg='#1a1a2e')
        self._load_overlay_image()

        self.zoom_overlay.bind("<Escape>", lambda e: self.close_zoom_overlay())

    def close_zoom_overlay(self):
        if self.zoom_overlay and self.zoom_overlay.winfo_exists():
            self.zoom_overlay.destroy()
            self.zoom_overlay = None

    def _load_overlay_image(self):
        if not self.current_pil_image or not self.overlay_canvas.winfo_exists(): return
        try:
            work_img = self.current_pil_image
            orig_w, orig_h = work_img.size
            
            target_w, target_h = 1024, 1024
            scale = min(target_w / orig_w, target_h / orig_h)
            new_w = int(orig_w * scale)
            new_h = int(orig_h * scale)

            self.overlay_canvas.config(scrollregion=(0, 0, target_w, target_h))
            self.overlay_canvas.delete("all")
            self.overlay_label.config(image='')

            resized_img = work_img.resize((new_w, new_h), Image.LANCZOS)
            overlay_tk_img = ImageTk.PhotoImage(resized_img)
            self.overlay_label.image = overlay_tk_img
            self.overlay_label.config(image=overlay_tk_img)
            
            resized_img.close()
            
            self.overlay_canvas.create_window((target_w//2, target_h//2), window=self.overlay_label, anchor='center')
        except Exception:
            pass

if __name__ == '__main__':
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('DeepbooruTagWalker')
    except Exception:
        pass
    root = tk.Tk()
    app = TagProofCheckerApp(root)
    root.mainloop()