import json
import os
import re
import subprocess
import threading
import time
from collections import deque
from datetime import datetime
from tkinter import filedialog

try:
    import customtkinter as ctk
    USE_CTK = True
except ImportError:
    import tkinter as tk
    USE_CTK = False

CREATE_NO_WINDOW = 0x08000000
APP_NAME = "Automatic Internet Tester"
APP_VERSION = "v1.5"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(SCRIPT_DIR, "AITconfig.json")

DEFAULT_CONFIG = {
    "gateway": "",
    "targets": [
        {"name": "Cloudflare DNS", "host": "1.1.1.1"},
        {"name": "Google", "host": "google.com"}
    ]
}


class AutomaticInternetTester:
    def __init__(self):
        self.running = True
        self.session_start = datetime.now()
        self.targets = {}  # key -> target state dict
        self.ui_cards = {}
        self.outage_events = []  # closed outage records
        self.raw_logs = []
        self.lock = threading.Lock()

        self.config = self._load_config()

        if USE_CTK:
            ctk.set_appearance_mode("dark")
            ctk.set_default_color_theme("blue")
            self.root = ctk.CTk()
        else:
            self.root = tk.Tk()
            self.root.configure(bg="#121212")

        self.root.title(f"{APP_NAME} {APP_VERSION}")
        self.root.geometry("680x740")
        self.root.minsize(560, 560)
        self.root.resizable(True, True)

        self._build_ui()

        # Register Primary Gateway (starts inactive if unconfigured)
        gw_host = self.config.get("gateway", "").strip()
        self._register_target("Gateway", gw_host, is_gw=True, save=False)

        # Register Saved Targets from AITconfig.json
        for item in self.config.get("targets", []):
            name = item.get("name", "").strip()
            host = item.get("host", "").strip()
            if host:
                self._register_target(name or host, host, is_gw=False, save=False)

        self._log_event(f"{APP_NAME} {APP_VERSION} initialized. Config loaded from {CONFIG_FILE}.")

    # --- Configuration Management ---
    def _load_config(self):
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        return {
                            "gateway": data.get("gateway", ""),
                            "targets": data.get("targets", DEFAULT_CONFIG["targets"])
                        }
            except Exception:
                pass
        self._write_config(DEFAULT_CONFIG)
        return dict(DEFAULT_CONFIG)

    def _write_config(self, data):
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=4)
        except Exception as e:
            print(f"Failed to save config: {e}")

    def _save_current_config(self):
        with self.lock:
            gw = self.targets["GATEWAY"]["host"] if "GATEWAY" in self.targets else ""
            extras = [
                {"name": d["name"], "host": d["host"]}
                for k, d in self.targets.items()
                if not d["is_gw"] and d["active"]
            ]
        self.config = {"gateway": gw, "targets": extras}
        self._write_config(self.config)

    # --- Responsive UI Construction ---
    def _build_ui(self):
        self.root.grid_columnconfigure(0, weight=1)
        self.root.grid_rowconfigure(2, weight=3)  # Cards list scales
        self.root.grid_rowconfigure(4, weight=2)  # Log box scales

        if USE_CTK:
            # --- Row 0: Header & Status Banner ---
            header_frame = ctk.CTkFrame(self.root, corner_radius=10, fg_color="#1e293b")
            header_frame.grid(row=0, column=0, sticky="ew", padx=16, pady=(14, 6))
            header_frame.grid_columnconfigure(0, weight=1)

            title_lbl = ctk.CTkLabel(
                header_frame,
                text=f"{APP_NAME}  {APP_VERSION}",
                font=("Segoe UI", 16, "bold"),
                text_color="#f8fafc"
            )
            title_lbl.grid(row=0, column=0, sticky="w", padx=14, pady=(8, 2))

            self.diag_lbl = ctk.CTkLabel(
                header_frame,
                text="Initializing probes...",
                font=("Segoe UI", 12, "bold"),
                text_color="#38bdf8"
            )
            self.diag_lbl.grid(row=1, column=0, sticky="w", padx=14, pady=(0, 8))

            # --- Row 1: Configuration Controls (Gateway + Add Target) ---
            ctrl_container = ctk.CTkFrame(self.root, corner_radius=10)
            ctrl_container.grid(row=1, column=0, sticky="ew", padx=16, pady=6)
            ctrl_container.grid_columnconfigure(1, weight=1)

            # Gateway Row
            ctk.CTkLabel(ctrl_container, text="Gateway IP:", font=("Segoe UI", 12, "bold")).grid(
                row=0, column=0, sticky="w", padx=(12, 8), pady=(10, 6)
            )
            self.gw_entry = ctk.CTkEntry(
                ctrl_container,
                placeholder_text="e.g. 192.168.1.1 or 100.64.65.1",
                font=("Consolas", 12)
            )
            if self.config.get("gateway"):
                self.gw_entry.insert(0, self.config["gateway"])
            self.gw_entry.grid(row=0, column=1, columnspan=2, sticky="ew", padx=4, pady=(10, 6))

            ctk.CTkButton(
                ctrl_container, text="Set Gateway", width=115, command=self._update_gateway
            ).grid(row=0, column=3, padx=(6, 12), pady=(10, 6))

            # Add Target Row
            ctk.CTkLabel(ctrl_container, text="New Target:", font=("Segoe UI", 12, "bold")).grid(
                row=1, column=0, sticky="w", padx=(12, 8), pady=(4, 10)
            )
            self.new_name_entry = ctk.CTkEntry(
                ctrl_container, placeholder_text="Label (e.g. Riot EUW)", font=("Segoe UI", 12)
            )
            self.new_name_entry.grid(row=1, column=1, sticky="ew", padx=4, pady=(4, 10))

            self.new_host_entry = ctk.CTkEntry(
                ctrl_container, placeholder_text="Host/IP (e.g. 8.8.8.8)", font=("Consolas", 12)
            )
            self.new_host_entry.grid(row=1, column=2, sticky="ew", padx=4, pady=(4, 10))
            ctrl_container.grid_columnconfigure(2, weight=1)

            ctk.CTkButton(
                ctrl_container,
                text="+ Add Target",
                width=115,
                fg_color="#16a34a",
                hover_color="#15803d",
                command=self._add_custom_target
            ).grid(row=1, column=3, padx=(6, 12), pady=(4, 10))

            # --- Row 2: Scrollable Active Targets List ---
            self.cards_container = ctk.CTkScrollableFrame(
                self.root, corner_radius=10, label_text="Active Ping Targets", label_font=("Segoe UI", 12, "bold")
            )
            self.cards_container.grid(row=2, column=0, sticky="nsew", padx=16, pady=6)
            self.cards_container.grid_columnconfigure(0, weight=1)

            # --- Row 3: Action Bar ---
            btn_frame = ctk.CTkFrame(self.root, fg_color="transparent")
            btn_frame.grid(row=3, column=0, sticky="ew", padx=16, pady=4)
            btn_frame.grid_columnconfigure((0, 1, 2), weight=1)

            self.btn = ctk.CTkButton(btn_frame, text="Pause All", height=32, command=self._toggle)
            self.btn.grid(row=0, column=0, sticky="ew", padx=(0, 6))

            self.export_btn = ctk.CTkButton(
                btn_frame,
                text="Export Log (.txt)",
                height=32,
                fg_color="#0284c7",
                hover_color="#0369a1",
                command=self._export_logs
            )
            self.export_btn.grid(row=0, column=1, sticky="ew", padx=6)

            self.clear_btn = ctk.CTkButton(
                btn_frame,
                text="Clear Log",
                height=32,
                fg_color="#374151",
                hover_color="#4b5563",
                command=self._clear_log
            )
            self.clear_btn.grid(row=0, column=2, sticky="ew", padx=(6, 0))

            # --- Row 4: Event & Outage Log ---
            log_frame = ctk.CTkFrame(self.root, corner_radius=10)
            log_frame.grid(row=4, column=0, sticky="nsew", padx=16, pady=(6, 14))
            log_frame.grid_columnconfigure(0, weight=1)
            log_frame.grid_rowconfigure(1, weight=1)

            ctk.CTkLabel(
                log_frame, text="Outage, Scope & High-Spike Log", font=("Segoe UI", 12, "bold")
            ).grid(row=0, column=0, sticky="w", padx=12, pady=(8, 4))

            self.log_box = ctk.CTkTextbox(log_frame, font=("Consolas", 11), activate_scrollbars=True)
            self.log_box.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 10))
            self.log_box.configure(state="disabled")

        else:
            # Standard Tkinter fallback with responsive grid
            header_frame = tk.Frame(self.root, bg="#1e293b", padx=12, pady=8)
            header_frame.grid(row=0, column=0, sticky="ew", padx=14, pady=(12, 6))
            tk.Label(header_frame, text=f"{APP_NAME} {APP_VERSION}", font=("Segoe UI", 13, "bold"), fg="white", bg="#1e293b").pack(anchor="w")
            self.diag_lbl = tk.Label(header_frame, text="Initializing probes...", font=("Segoe UI", 10, "bold"), fg="#38bdf8", bg="#1e293b")
            self.diag_lbl.pack(anchor="w")

            ctrl_container = tk.Frame(self.root, bg="#262626", padx=10, pady=8)
            ctrl_container.grid(row=1, column=0, sticky="ew", padx=14, pady=4)
            ctrl_container.grid_columnconfigure(1, weight=1)
            ctrl_container.grid_columnconfigure(2, weight=1)

            tk.Label(ctrl_container, text="Gateway IP:", font=("Segoe UI", 9, "bold"), fg="white", bg="#262626").grid(row=0, column=0, sticky="w")
            self.gw_entry = tk.Entry(ctrl_container, font=("Consolas", 10), bg="#111111", fg="white", insertbackground="white")
            self.gw_entry.insert(0, self.config.get("gateway", ""))
            self.gw_entry.grid(row=0, column=1, columnspan=2, sticky="ew", padx=6, pady=3)
            tk.Button(ctrl_container, text="Set Gateway", command=self._update_gateway, bg="#2563eb", fg="white", relief="flat").grid(row=0, column=3, sticky="ew")

            tk.Label(ctrl_container, text="New Target:", font=("Segoe UI", 9, "bold"), fg="white", bg="#262626").grid(row=1, column=0, sticky="w")
            self.new_name_entry = tk.Entry(ctrl_container, font=("Segoe UI", 9), bg="#111111", fg="white", insertbackground="white")
            self.new_name_entry.grid(row=1, column=1, sticky="ew", padx=6, pady=3)
            self.new_host_entry = tk.Entry(ctrl_container, font=("Consolas", 9), bg="#111111", fg="white", insertbackground="white")
            self.new_host_entry.grid(row=1, column=2, sticky="ew", padx=6, pady=3)
            tk.Button(ctrl_container, text="+ Add Target", command=self._add_custom_target, bg="#16a34a", fg="white", relief="flat").grid(row=1, column=3, sticky="ew")

            self.cards_container = tk.Frame(self.root, bg="#121212")
            self.cards_container.grid(row=2, column=0, sticky="nsew", padx=14, pady=6)
            self.cards_container.grid_columnconfigure(0, weight=1)

            btn_frame = tk.Frame(self.root, bg="#121212")
            btn_frame.grid(row=3, column=0, sticky="ew", padx=14, pady=4)
            btn_frame.grid_columnconfigure((0, 1, 2), weight=1)
            self.btn = tk.Button(btn_frame, text="Pause All", command=self._toggle, bg="#2563eb", fg="white", relief="flat")
            self.btn.grid(row=0, column=0, sticky="ew", padx=2)
            self.export_btn = tk.Button(btn_frame, text="Export Log (.txt)", command=self._export_logs, bg="#0284c7", fg="white", relief="flat")
            self.export_btn.grid(row=0, column=1, sticky="ew", padx=2)
            self.clear_btn = tk.Button(btn_frame, text="Clear Log", command=self._clear_log, bg="#374151", fg="white", relief="flat")
            self.clear_btn.grid(row=0, column=2, sticky="ew", padx=2)

            self.log_box = tk.Text(self.root, height=8, font=("Consolas", 9), bg="#111111", fg="#e5e5e5", state="disabled")
            self.log_box.grid(row=4, column=0, sticky="nsew", padx=14, pady=(4, 12))

    # --- Target Registration & Card UI ---
    def _register_target(self, name, host, is_gw=False, save=True):
        key = "GATEWAY" if is_gw else f"{name}_{host}_{time.time()}"
        with self.lock:
            self.targets[key] = {
                "name": name,
                "host": host,
                "is_gw": is_gw,
                "active": True,
                "sent": 0,
                "lost": 0,
                "ms": "---",
                "online": True,
                "was_online": True,
                "outage_start": None,
                "history": deque(maxlen=60),
                "total_ms_sum": 0,
                "total_ms_count": 0,
                "avg": 0.0
            }

        self._create_card_ui(key, name, host, is_gw)
        if save:
            self._save_current_config()

        t = threading.Thread(target=self._ping_worker, args=(key,), daemon=True)
        t.start()

    def _create_card_ui(self, key, name, host, is_gw):
        disp_host = host if host else "Not Set — Enter IP above"
        badge = "[GATEWAY] " if is_gw else ""

        if USE_CTK:
            card = ctk.CTkFrame(self.cards_container, corner_radius=8, fg_color="#1f2937")
            card.pack(fill="x", padx=6, pady=4)
            card.grid_columnconfigure(0, weight=1)

            top_row = ctk.CTkFrame(card, fg_color="transparent")
            top_row.grid(row=0, column=0, sticky="ew", padx=12, pady=(8, 2))
            top_row.grid_columnconfigure(0, weight=1)

            title_lbl = ctk.CTkLabel(
                top_row, text=f"{badge}{name} ({disp_host})", font=("Segoe UI", 13, "bold"), anchor="w"
            )
            title_lbl.grid(row=0, column=0, sticky="w")

            if not is_gw:
                del_btn = ctk.CTkButton(
                    top_row,
                    text="✕",
                    width=26,
                    height=24,
                    fg_color="#dc2626",
                    hover_color="#b91c1c",
                    command=lambda k=key: self._remove_target(k)
                )
                del_btn.grid(row=0, column=1, sticky="e")

            stat_lbl = ctk.CTkLabel(
                card,
                text="Awaiting gateway IP..." if (is_gw and not host) else "Probing...",
                font=("Consolas", 11),
                text_color="#9ca3af",
                anchor="w"
            )
            stat_lbl.grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 8))
        else:
            card = tk.Frame(self.cards_container, bg="#1f2937", padx=10, pady=6)
            card.pack(fill="x", pady=3)
            top_row = tk.Frame(card, bg="#1f2937")
            top_row.pack(fill="x")
            title_lbl = tk.Label(top_row, text=f"{badge}{name} ({disp_host})", font=("Segoe UI", 9, "bold"), fg="white", bg="#1f2937")
            title_lbl.pack(side="left")
            if not is_gw:
                del_btn = tk.Button(top_row, text="X", command=lambda k=key: self._remove_target(k), bg="#dc2626", fg="white", relief="flat", padx=4)
                del_btn.pack(side="right")
            stat_lbl = tk.Label(card, text="Probing...", font=("Consolas", 9), fg="#9ca3af", bg="#1f2937")
            stat_lbl.pack(anchor="w")

        self.ui_cards[key] = {"frame": card, "title": title_lbl, "stat": stat_lbl}

    # --- User Actions ---
    def _update_gateway(self):
        new_gw = self.gw_entry.get().strip()
        with self.lock:
            if "GATEWAY" in self.targets:
                d = self.targets["GATEWAY"]
                d["host"] = new_gw
                d["sent"] = 0
                d["lost"] = 0
                d["history"].clear()
                d["total_ms_sum"] = 0
                d["total_ms_count"] = 0
                d["avg"] = 0.0
                d["online"] = True
                d["was_online"] = True
                d["outage_start"] = None

        disp = new_gw if new_gw else "Not Set — Enter IP above"
        if "GATEWAY" in self.ui_cards:
            self._set_label(self.ui_cards["GATEWAY"]["title"], f"[GATEWAY] Gateway ({disp})", "#ffffff")
            if not new_gw:
                self._set_label(self.ui_cards["GATEWAY"]["stat"], "Awaiting gateway IP...", "#9ca3af")

        self._save_current_config()
        self._log_event(f"Gateway set to: {new_gw if new_gw else 'None (Disabled)'}")

    def _add_custom_target(self):
        host = self.new_host_entry.get().strip()
        name = self.new_name_entry.get().strip() or host
        if not host:
            return
        self._register_target(name, host, is_gw=False, save=True)
        self._log_event(f"Added target: {name} ({host})")
        self.new_name_entry.delete(0, "end")
        self.new_host_entry.delete(0, "end")

    def _remove_target(self, key):
        with self.lock:
            if key in self.targets:
                name = self.targets[key]["name"]
                host = self.targets[key]["host"]
                self.targets[key]["active"] = False
                del self.targets[key]
                self._log_event(f"Removed target: {name} ({host})")
        if key in self.ui_cards:
            self.ui_cards[key]["frame"].destroy()
            del self.ui_cards[key]
        self._save_current_config()

    # --- Logging & Export ---
    def _log_event(self, message):
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        short_now = datetime.now().strftime("%H:%M:%S")
        self.raw_logs.append(f"[{now}] {message}")
        self.root.after(0, self._append_log, f"[{short_now}] {message}\n")

    def _append_log(self, entry):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", entry)
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _clear_log(self):
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")
        self.raw_logs.clear()
        self.outage_events.clear()
        self._log_event("Session log cleared.")

    def _classify_outage_scope(self, failed_key):
        """Determines whether an outage is Local Gateway, Full WAN/ISP, or Single Host."""
        gw = self.targets.get("GATEWAY")
        gw_configured = bool(gw and gw["host"])
        gw_online = gw["online"] if gw_configured else True

        wan_targets = [d for k, d in self.targets.items() if not d["is_gw"] and d["host"]]
        any_wan_online = any(d["online"] for d in wan_targets) if wan_targets else True

        if gw_configured and not gw_online:
            return "Local Gateway / Router Drop"
        if not any_wan_online:
            return "ISP / Complete WAN Outage (Gateway OK)"
        return "Single Target / External Route Drop"

    def _export_logs(self):
        default_name = f"Internet_Test_Log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        filepath = filedialog.asksaveasfilename(
            defaultextension=".txt",
            initialfile=default_name,
            filetypes=[("Text Files", "*.txt"), ("All Files", "*.*")],
            title="Export Network Diagnostic Log"
        )
        if not filepath:
            return

        now_dt = datetime.now()
        duration = now_dt - self.session_start
        duration_str = str(duration).split(".")[0]

        lines = [
            "=" * 72,
            f"{APP_NAME} {APP_VERSION} — DIAGNOSTIC REPORT",
            "=" * 72,
            f"Session Start : {self.session_start.strftime('%Y-%m-%d %H:%M:%S')}",
            f"Export Time   : {now_dt.strftime('%Y-%m-%d %H:%M:%S')}",
            f"Total Runtime : {duration_str}",
            "",
            "--- 1. TARGET SUMMARY (UPTIME % & AVERAGE PING) ---",
            f"{'Target Name':<22} | {'Host / IP':<18} | {'Avg Ping':<10} | {'Uptime %':<10} | {'Packets (Lost/Sent)'}",
            "-" * 72
        ]

        with self.lock:
            for key, d in self.targets.items():
                if not d["host"]:
                    lines.append(f"{d['name']:<22} | {'Not Configured':<18} | {'N/A':<10} | {'N/A':<10} | 0/0")
                    continue
                sent = d["sent"]
                lost = d["lost"]
                uptime = ((sent - lost) / sent * 100.0) if sent > 0 else 100.0
                avg_ping = f"{(d['total_ms_sum'] / d['total_ms_count']):.1f} ms" if d["total_ms_count"] > 0 else "N/A"
                lines.append(
                    f"{d['name'][:22]:<22} | {d['host'][:18]:<18} | {avg_ping:<10} | {uptime:>8.2f}% | {lost}/{sent}"
                )

            lines.extend([
                "",
                "--- 2. OUTAGE TIMING & SCOPE LOG ---"
            ])

            # Combine completed outages + any ongoing outages
            all_outages = list(self.outage_events)
            for key, d in self.targets.items():
                if not d["online"] and d["outage_start"] is not None:
                    dur = (now_dt - d["outage_start"]).total_seconds()
                    all_outages.append({
                        "target": f"{d['name']} ({d['host']})",
                        "start": d["outage_start"].strftime("%Y-%m-%d %H:%M:%S"),
                        "end": "ONGOING",
                        "duration": f"{dur:.1f}s+",
                        "scope": self._classify_outage_scope(key)
                    })

        if not all_outages:
            lines.append("No outages recorded during this session.")
        else:
            lines.append(f"{'Start Time':<20} | {'End Time':<20} | {'Duration':<10} | {'Target':<24} | {'Scope'}")
            lines.append("-" * 105)
            for ev in all_outages:
                lines.append(
                    f"{ev['start']:<20} | {ev['end']:<20} | {ev['duration']:<10} | {ev['target'][:24]:<24} | {ev['scope']}"
                )

        lines.extend([
            "",
            "--- 3. CHRONOLOGICAL EVENT LOG ---"
        ])
        lines.extend(self.raw_logs)
        lines.append("=" * 72)

        try:
            with open(filepath, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
            self._log_event(f"Log exported to {os.path.basename(filepath)}")
        except Exception as e:
            self._log_event(f"Export failed: {e}")

    def _toggle(self):
        self.running = not self.running
        btn_text = "Pause All" if self.running else "Resume All"
        if USE_CTK:
            self.btn.configure(text=btn_text, fg_color="#1f6aa5" if self.running else "#4b5563")
        else:
            self.btn.configure(text=btn_text)
        self._log_event("Monitoring resumed." if self.running else "Monitoring paused.")

    def _set_label(self, widget, text, color):
        if USE_CTK:
            widget.configure(text=text, text_color=color)
        else:
            widget.configure(text=text, fg=color)

    # --- Worker Thread ---
    def _ping_worker(self, key):
        while True:
            with self.lock:
                if key not in self.targets or not self.targets[key]["active"]:
                    break
                d = self.targets[key]
                host = d["host"].strip()
                name = d["name"]

            if self.running and host:
                try:
                    res = subprocess.run(
                        ["ping", "-n", "1", "-w", "1000", host],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        creationflags=CREATE_NO_WINDOW
                    )
                    out = res.stdout
                    success = res.returncode == 0 and ("TTL=" in out or "ttl=" in out)
                except Exception:
                    success = False
                    out = ""

                now_dt = datetime.now()
                with self.lock:
                    if key not in self.targets or not self.targets[key]["active"]:
                        break
                    d = self.targets[key]
                    # Verify host didn't change mid-ping
                    if d["host"].strip() != host:
                        continue

                    d["sent"] += 1

                    if success:
                        match = re.search(r"(?:time|zeit)[=<](\d+)\s*ms", out, re.IGNORECASE)
                        ms_val = int(match.group(1)) if match else 1
                        d["ms"] = f"{ms_val}ms"
                        d["online"] = True

                        if not d["was_online"]:
                            outage_start = d["outage_start"] or now_dt
                            dur = (now_dt - outage_start).total_seconds()
                            scope = self._classify_outage_scope(key)
                            self.outage_events.append({
                                "target": f"{name} ({host})",
                                "start": outage_start.strftime("%Y-%m-%d %H:%M:%S"),
                                "end": now_dt.strftime("%Y-%m-%d %H:%M:%S"),
                                "duration": f"{dur:.1f}s",
                                "scope": scope
                            })
                            self._log_event(f"RECOVERED: {name} ({host}) back online in {ms_val}ms (Down for {dur:.1f}s)")
                            d["was_online"] = True
                            d["outage_start"] = None

                        if len(d["history"]) >= 5:
                            avg = d["avg"]
                            threshold = max(avg * 2.0, avg + 15.0)
                            if ms_val > threshold:
                                self._log_event(f"SPIKE: {name} ({host}) hit {ms_val}ms (Avg: {avg:.1f}ms)")

                        d["history"].append(ms_val)
                        d["total_ms_sum"] += ms_val
                        d["total_ms_count"] += 1
                        d["avg"] = d["total_ms_sum"] / d["total_ms_count"]
                    else:
                        d["lost"] += 1
                        d["ms"] = "TIMEOUT"
                        d["online"] = False
                        if d["was_online"]:
                            d["outage_start"] = now_dt
                            scope = self._classify_outage_scope(key)
                            self._log_event(f"OUTAGE: {name} ({host}) timed out! [Scope: {scope}]")
                            d["was_online"] = False

                self.root.after(0, self._refresh_ui)
            time.sleep(1)

    def _refresh_ui(self):
        if not self.running:
            return
        with self.lock:
            gw_configured = False
            gw_ok = True
            wan_list = []

            for key, d in self.targets.items():
                if key not in self.ui_cards:
                    continue
                if not d["host"]:
                    continue

                sent = d["sent"]
                lost = d["lost"]
                uptime_pct = ((sent - lost) / sent * 100.0) if sent > 0 else 100.0
                status = "ONLINE" if d["online"] else "DROP"
                color = "#22c55e" if d["online"] else "#ef4444"
                avg_str = f"{d['avg']:.1f}ms" if d["total_ms_count"] > 0 else "---"

                text = (
                    f"[{status:<6}] Ping: {d['ms']:<8} | Avg: {avg_str:<8} | "
                    f"Uptime: {uptime_pct:>6.2f}% ({lost}/{sent} lost)"
                )
                self._set_label(self.ui_cards[key]["stat"], text, color)

                if d["is_gw"]:
                    gw_configured = True
                    gw_ok = d["online"]
                else:
                    wan_list.append(d["online"])

        any_wan_ok = any(wan_list) if wan_list else True
        all_wan_ok = all(wan_list) if wan_list else True
        now = datetime.now().strftime("%H:%M:%S")

        if gw_configured and not gw_ok:
            self._set_label(self.diag_lbl, f"[{now}] ALERT: Local Gateway / Router Drop!", "#ef4444")
        elif all_wan_ok:
            gw_note = "Gateway & " if gw_configured else ""
            self._set_label(self.diag_lbl, f"[{now}] Healthy — {gw_note}All Targets Active", "#22c55e")
        elif any_wan_ok:
            self._set_label(self.diag_lbl, f"[{now}] Partial Drop — Specific External Route Failing", "#f59e0b")
        else:
            self._set_label(self.diag_lbl, f"[{now}] ISP / WAN Outage (Local Gateway OK)", "#f59e0b")

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    AutomaticInternetTester().run()
