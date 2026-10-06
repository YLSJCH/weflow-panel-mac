# -*- coding: utf-8 -*-
"""
微信聊天记录导出面板 macOS 版（WeFlow CLI 图形界面封装）
功能：账号列表（头像+昵称）、密钥提取（弹系统授权框）、一键导出全部会话。
导出结果保存在「桌面/微信导出记录/」下。
"""
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import messagebox, scrolledtext, simpledialog, ttk

try:
    from PIL import Image, ImageTk
    HAS_PIL = True
except Exception:  # noqa: BLE001
    HAS_PIL = False

# ---------------- 路径与常量 ----------------
HOME = os.path.expanduser("~")
if getattr(sys, "frozen", False):
    BUNDLED_WEFLOW = os.path.join(sys._MEIPASS, "weflow_bin", "weflow")
else:
    BUNDLED_WEFLOW = None

DATA_DIR = os.path.join(HOME, "Library", "Application Support", "WeFlowPanel")
LOG_DIR = os.path.join(DATA_DIR, "logs")
PANEL_CFG = os.path.join(DATA_DIR, "panel_config.json")
EXPORT_ROOT = os.path.join(HOME, "Desktop", "微信导出记录")

os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs(EXPORT_ROOT, exist_ok=True)

PROFILE_KEYS = ["db_path", "wxid", "decrypt_key", "image_xor_key", "image_aes_key",
                "cache_path", "log_enabled", "http_api_token", "http_api_host",
                "http_api_port", "ai_model_api_base_url", "ai_model_api_key",
                "ai_model_api_model", "ai_model_api_max_tokens", "ai_insight_enabled",
                "extra"]


def ensure_weflow():
    """返回 weflow 可执行文件路径；打包版先把内置文件释放到本地。"""
    if not getattr(sys, "frozen", False):
        for p in (os.path.join(os.path.dirname(os.path.abspath(__file__)), "weflow"),
                  os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "weflow_bin", "weflow")):
            if os.path.exists(p):
                return p
        return None
    dst = os.path.join(DATA_DIR, "bin", "weflow")
    if os.path.exists(dst) and os.path.getsize(dst) == os.path.getsize(BUNDLED_WEFLOW):
        return dst
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copyfile(BUNDLED_WEFLOW, dst)
    os.chmod(dst, 0o755)
    return dst


WEFLOW = ensure_weflow()


def run_cmd(args, timeout=300):
    try:
        p = subprocess.run(args, capture_output=True, timeout=timeout)
        return (p.returncode,
                p.stdout.decode("utf-8", errors="replace"),
                p.stderr.decode("utf-8", errors="replace"))
    except subprocess.TimeoutExpired:
        return -1, "", "操作超时"
    except Exception as e:  # noqa: BLE001
        return -1, "", str(e)


def birthtime(path):
    try:
        st = os.stat(path)
        return getattr(st, "st_birthtime", None) or st.st_ctime
    except OSError:
        return 0.0


# ---------------- weflow config.json 读写（多账号档案） ----------------
_CFG_PATH = None


def weflow_cfg_path():
    """向 weflow 询问配置文件位置，不猜。"""
    global _CFG_PATH
    if _CFG_PATH:
        return _CFG_PATH
    code, out, _ = run_cmd([WEFLOW, "config", "path"])
    m = re.search(r"(/[^\n]*?config\.json)", out or "")
    if m:
        _CFG_PATH = m.group(1).strip()
        return _CFG_PATH
    for cand in (os.path.join(HOME, ".config", "weflow", "config.json"),
                 os.path.join(HOME, "Library", "Application Support",
                              "weflow", "config.json")):
        if os.path.exists(cand):
            _CFG_PATH = cand
            return cand
    _CFG_PATH = os.path.join(HOME, ".config", "weflow", "config.json")
    return _CFG_PATH


def load_weflow_cfg():
    try:
        return json.load(open(weflow_cfg_path(), encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {"current_profile": "default", "lang": None,
                "profiles": {"default": {}}, "extra": {}}


def save_weflow_cfg(cfg):
    path = weflow_cfg_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    bak = path + ".bak"
    if os.path.exists(path):
        shutil.copyfile(path, bak)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    code, out, err = run_cmd([WEFLOW, "config", "get", "--json"])
    if code != 0 or "failed to parse" in (out + err):
        if os.path.exists(bak):
            shutil.copyfile(bak, path)
        raise RuntimeError("配置写入后工具无法解析，已自动回滚。请重试。")


def profile_of(cfg, name):
    prof = cfg.setdefault("profiles", {}).setdefault(name, {})
    for k in PROFILE_KEYS:
        if k in ("extra", "log_enabled"):
            continue
        prof.setdefault(k, None)
    if not isinstance(prof.get("log_enabled"), bool):
        prof["log_enabled"] = False
    if not isinstance(prof.get("extra"), dict):
        prof["extra"] = {}
    return prof


def panel_cfg_load():
    try:
        return json.load(open(PANEL_CFG, encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def panel_cfg_save(d):
    json.dump(d, open(PANEL_CFG, "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)


# ---------------- 主界面 ----------------
class Panel(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("微信聊天记录导出面板")
        self.geometry("820x700")
        self.minsize(760, 620)

        self.log_q = queue.Queue()
        self.accounts = []            # [{"wxid","nickname","remark","avatar"}]
        self.avatar_imgs = {}         # wxid -> PhotoImage（防回收）
        self.selected_wxid = tk.StringVar(value="（未选择）")
        self.busy = False

        self._build_ui()
        self.after(100, self._poll_log)
        self.after(300, self.refresh_accounts)

    # ---------- 界面 ----------
    def _build_ui(self):
        pad = {"padx": 10, "pady": 4}

        f1 = ttk.LabelFrame(self, text="第 1 步：选择账号（双击名称可改名）")
        f1.pack(fill="x", **pad)
        cols = ("name", "wxid", "state")
        self.tree = ttk.Treeview(f1, columns=cols, show="tree headings", height=6,
                                 selectmode="browse")
        self.tree.heading("#0", text="头像")
        self.tree.heading("name", text="微信名称")
        self.tree.heading("wxid", text="微信号")
        self.tree.heading("state", text="密钥状态")
        self.tree.column("#0", width=60, minwidth=50, anchor="center")
        self.tree.column("name", width=220)
        self.tree.column("wxid", width=220)
        self.tree.column("state", width=90, anchor="center")
        sb = ttk.Scrollbar(f1, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=6)
        sb.pack(side="left", fill="y", pady=6, padx=(0, 4))
        self.tree.bind("<Double-1>", self.rename_account)
        right = ttk.Frame(f1)
        right.pack(side="left", fill="y", padx=8)
        ttk.Button(right, text="🔄 刷新", command=self.refresh_accounts).pack(pady=4)
        ttk.Button(right, text="✅ 用这个账号", command=self.pick_account).pack(pady=4)
        ttk.Label(f1, text="当前账号：").pack(side="bottom", anchor="w", padx=8)
        ttk.Label(f1, textvariable=self.selected_wxid,
                  foreground="#0078d4").pack(side="bottom", anchor="w", padx=8,
                                             pady=(0, 6))

        f2 = ttk.LabelFrame(self, text="第 2 步：提取密钥（每个账号只需做一次）")
        f2.pack(fill="x", **pad)
        ttk.Button(f2, text="🔑 ① 提取数据库密钥（会弹系统密码框，按提示退出重登微信）",
                   command=self.extract_db_key).pack(fill="x", padx=8, pady=4)
        ttk.Button(f2, text="🖼️ ② 提取图片密钥（点一下就好）",
                   command=self.extract_image_key).pack(fill="x", padx=8, pady=4)
        ttk.Button(f2, text="🔌 测试连接",
                   command=self.test_connection).pack(fill="x", padx=8, pady=4)

        f3 = ttk.LabelFrame(self, text="第 3 步：导出当前账号的全部聊天记录"
                            "（保存在 桌面/微信导出记录）")
        f3.pack(fill="x", **pad)
        row = ttk.Frame(f3)
        row.pack(fill="x", padx=8, pady=4)
        ttk.Label(row, text="导出格式：").pack(side="left")
        self.fmt = ttk.Combobox(row, values=["json", "html", "excel", "txt"],
                                state="readonly", width=8)
        self.fmt.set("json")
        self.fmt.pack(side="left", padx=4)
        ttk.Label(row, text="（json 用于汇总到总查看器；html 可直接双击看）",
                  foreground="gray").pack(side="left", padx=4)
        self.with_media = tk.BooleanVar(value=True)
        ttk.Checkbutton(row, text="包含图片/语音/视频（较慢，体积大）",
                        variable=self.with_media).pack(side="left", padx=12)
        ttk.Button(f3, text="📂 开始导出全部会话",
                   command=self.export_all).pack(fill="x", padx=8, pady=4)
        ttk.Button(f3, text="📁 打开导出文件夹",
                   command=lambda: subprocess.Popen(
                       ["open", EXPORT_ROOT])).pack(fill="x", padx=8, pady=(0, 6))

        fl = ttk.LabelFrame(self, text="运行日志")
        fl.pack(fill="both", expand=True, **pad)
        self.log = scrolledtext.ScrolledText(fl, height=11, state="disabled",
                                             font=("Menlo", 11))
        self.log.pack(fill="both", expand=True, padx=6, pady=6)

        self.log_line("面板已启动。正在扫描账号与头像……")

    # ---------- 日志 ----------
    def log_line(self, text):
        self.log_q.put(str(text))

    def _poll_log(self):
        try:
            while True:
                line = self.log_q.get_nowait()
                self.log.config(state="normal")
                self.log.insert("end", line + "\n")
                self.log.see("end")
                self.log.config(state="disabled")
        except queue.Empty:
            pass
        self.after(100, self._poll_log)

    def _task(self, fn):
        if self.busy:
            messagebox.showinfo("请稍候", "上一个操作还在进行中。")
            return
        self.busy = True

        def wrap():
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                self.log_line(f"❌ 出错：{e}")
            finally:
                self.busy = False

        threading.Thread(target=wrap, daemon=True).start()

    # ---------- 账号扫描（含头像、昵称缓存） ----------
    def _find_db_path(self):
        cfg = load_weflow_cfg()
        for prof in cfg.get("profiles", {}).values():
            if prof.get("db_path") and os.path.isdir(prof["db_path"]):
                return prof["db_path"]
        code, out, _ = run_cmd([WEFLOW, "db", "detect", "--json"])
        try:
            data = json.loads(out).get("data", {})
            p = data.get("db_path") or (data if isinstance(data, str) else None)
            if p and os.path.isdir(p):
                return p
        except Exception:  # noqa: BLE001
            m = re.search(r"db_path[:：]\s*(\S.+)", out)
            if m and os.path.isdir(m.group(1).strip()):
                return m.group(1).strip()
        return None

    def _avatar_map(self, db_path):
        """用“创建时间”把 login/<wxid> 和 head_imgs/<编号> 配对。"""
        login_dir = os.path.join(db_path, "all_users", "login")
        head_dir = os.path.join(db_path, "all_users", "head_imgs")
        mapping = {}
        if not (os.path.isdir(login_dir) and os.path.isdir(head_dir)):
            return mapping
        heads = []
        for num in os.listdir(head_dir):
            if not num.isdigit() or num == "0":
                continue
            p = os.path.join(head_dir, num)
            files = [f for f in os.listdir(p)
                     if os.path.isfile(os.path.join(p, f))]
            if files:
                heads.append((birthtime(p), os.path.join(p, files[0])))
        for wxid in os.listdir(login_dir):
            p = os.path.join(login_dir, wxid)
            if not os.path.isdir(p):
                continue
            bt = birthtime(p)
            best, best_dt = None, 999
            for hbt, img in heads:
                dt = abs(hbt - bt)
                if dt < best_dt:
                    best, best_dt = img, dt
            if best and best_dt < 1.0:
                mapping[wxid] = best
        return mapping

    def refresh_accounts(self):
        def work():
            code, out, err = run_cmd([WEFLOW, "db", "wxid", "--json"])
            wxids = []  # [(wxid, path)]
            try:
                data = json.loads(out)
                items = data.get("data", data) if isinstance(data, dict) else data
                if isinstance(items, dict):
                    items = (items.get("accounts") or items.get("sessions")
                             or items.get("list") or [])
                for it in items or []:
                    if isinstance(it, dict):
                        w = it.get("wxid") or it.get("id") or ""
                        p = it.get("path") or ""
                        if w:
                            wxids.append((w, p))
                    elif isinstance(it, str):
                        wxids.append((it, ""))
            except Exception:  # noqa: BLE001
                wxids = [(m.group(1), m.group(2))
                         for m in re.finditer(r"wxid[:：]\s*(\S+)\s+(\S+)", out)]
            if not wxids:
                self.log_line(f"❌ 未发现账号：{err or out}")
                self.log_line("提示：本机需要安装微信 4.0 以上版本，"
                              "并且至少登录过一次。")
                return

            db_path = self._find_db_path()
            avatars = self._avatar_map(db_path) if db_path else {}
            cache = panel_cfg_load()

            wcfg = load_weflow_cfg()
            keyed = {name for name, prof in wcfg.get("profiles", {}).items()
                     if prof.get("decrypt_key")}

            accs = []
            for w, wpath in wxids:
                c = cache.get(w, {})
                accs.append({
                    "wxid": w,
                    "path": wpath,
                    "nickname": c.get("nickname", ""),
                    "remark": c.get("remark", ""),
                    "avatar": avatars.get(w, ""),
                    "keyed": w in keyed,
                })
            self.accounts = accs

            thumbs = {}
            if HAS_PIL:
                for a in accs:
                    if a["avatar"] and os.path.exists(a["avatar"]):
                        try:
                            im = Image.open(a["avatar"])
                            im.thumbnail((44, 44))
                            thumbs[a["wxid"]] = ImageTk.PhotoImage(im)
                        except Exception:  # noqa: BLE001
                            pass

            def fill():
                self.avatar_imgs = thumbs
                self.tree.delete(*self.tree.get_children())
                for a in accs:
                    disp = a["remark"] or a["nickname"] or "（双击可改名）"
                    img = thumbs.get(a["wxid"], "")
                    self.tree.insert("", "end", iid=a["wxid"], image=img,
                                     values=(disp, a["wxid"],
                                             "✅ 已配置" if a["keyed"] else "—"))
            self.after(0, fill)
            self.log_line(f"已发现 {len(accs)} 个账号，"
                          f"{len(thumbs)} 个头像加载成功。")
            if not db_path:
                self.log_line("⚠️ 未找到微信数据目录，头像无法加载。")
        self._task(work)

    def rename_account(self, event):
        iid = self.tree.identify_row(event.y)
        if not iid:
            return
        acc = next((a for a in self.accounts if a["wxid"] == iid), None)
        if not acc:
            return
        cur = acc["remark"] or acc["nickname"] or ""
        name = simpledialog.askstring("修改名称", f"给 {iid} 起个好认的名字：",
                                      initialvalue=cur, parent=self)
        if name is None:
            return
        cache = panel_cfg_load()
        cache.setdefault(iid, {})["remark"] = name.strip()
        panel_cfg_save(cache)
        acc["remark"] = name.strip()
        disp = acc["remark"] or acc["nickname"] or "（未命名）"
        self.tree.item(iid, values=(disp, iid,
                                    "✅ 已配置" if acc["keyed"] else "—"))

    # ---------- 选择账号 ----------
    def pick_account(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("提示", "请先在列表里点选一个账号。")
            return
        wxid = sel[0]

        def work():
            try:
                cfg = load_weflow_cfg()
                prof = profile_of(cfg, wxid)
                db_path = self._find_db_path()
                if db_path:
                    prof["db_path"] = db_path
                prof["wxid"] = wxid
                cfg["current_profile"] = wxid
                save_weflow_cfg(cfg)
                self.after(0, lambda: self.selected_wxid.set(wxid))
                has_key = "✅（密钥已配置，可直接导出）" if prof.get("decrypt_key") \
                    else "（尚未配置密钥，请执行第 2 步）"
                self.log_line(f"✅ 已切换到账号：{wxid} {has_key}")
            except Exception as e:  # noqa: BLE001
                self.log_line(f"❌ 切换失败：{e}")
        self._task(work)

    # ---------- 提取密钥 ----------
    def extract_db_key(self):
        wxid = self.selected_wxid.get()
        if "（" in wxid:
            messagebox.showinfo("提示", "请先在第 1 步选择账号。")
            return
        ok = messagebox.askokcancel(
            "提取数据库密钥",
            f"当前账号：{wxid}\n\n接下来会：\n"
            "1. 自动打开一个【终端窗口】，并弹出系统密码框\n"
            "   → 输入你的 Mac 开机密码\n"
            "2. 按终端窗口里的提示：\n"
            "   按 Command+Q 彻底退出微信 → 重新打开微信\n"
            f"   并登录 {wxid} 这个账号\n"
            "3. 登录成功后自动抓取密钥，本面板会实时显示结果\n\n"
            "准备好了吗？")
        if not ok:
            return

        def work():
            import time
            keylog = os.path.join(LOG_DIR, "keylog.txt")
            sh = os.path.join(LOG_DIR, "提取密钥.sh")
            if os.path.exists(keylog):
                os.remove(keylog)
            lines = [
                "#!/bin/bash",
                "clear",
                "echo '================================================'",
                "echo '   微信密钥提取向导（请不要关闭本窗口）'",
                "echo '================================================'",
                "echo ''",
                "echo '第 1 步：马上会弹出系统密码框，请输入 Mac 开机密码'",
                "echo '第 2 步：彻底退出微信（按 Command+Q，或在屏幕'",
                "echo '         左上角点「微信」菜单 → 退出微信）'",
                f"echo '第 3 步：重新打开微信，登录账号：{wxid}'",
                "echo '第 4 步：登录成功后稍等片刻，会自动抓取密钥'",
                "echo ''",
                "echo '================================================'",
                "echo ''",
                f"WEFLOW_BIN=\"{WEFLOW}\"",
                f"KEYLOG=\"{keylog}\"",
                "rm -f \"$KEYLOG\"",
                "CMD=\"\\\"$WEFLOW_BIN\\\" key db --timeout 600"
                " > \\\"$KEYLOG\\\" 2>&1\"",
                "osascript -e 'on run argv' \\",
                " -e 'with timeout of 900 seconds' \\",
                " -e 'do shell script (item 1 of argv)"
                " with administrator privileges' \\",
                " -e 'end timeout' \\",
                " -e 'end run' -- \"$CMD\"",
                "echo ''",
                "echo '================ 运行结果 ================'",
                "cat \"$KEYLOG\" 2>/dev/null",
                "echo ''",
                "echo '（看到 decrypt_key 字样就是成功了，可以关闭本窗口）'",
            ]
            with open(sh, "w", encoding="utf-8", newline="\n") as f:
                f.write("\n".join(lines) + "\n")
            os.chmod(sh, 0o755)
            subprocess.Popen(["open", "-a", "Terminal", sh])
            self.log_line("✅ 引导窗口已打开。请先在密码框输入开机密码，"
                          "然后按提示退出并重登微信……")
            key = None
            fail_hint = None
            for _ in range(120):
                time.sleep(5)
                if not os.path.exists(keylog):
                    continue
                try:
                    text = open(keylog, encoding="utf-8",
                                errors="replace").read()
                except Exception:  # noqa: BLE001
                    continue
                m = re.search(r"decrypt_key[:：]\s*([0-9a-fA-F]{32,128})", text)
                if m:
                    key = m.group(1)
                    break
                for pat in ("SCAN_FAILED", "HOOK_FAILED", "task_for_pid",
                            "patch_breakpoint", "thread_get_state"):
                    if pat in text:
                        fail_hint = pat
                        break
                if fail_hint:
                    break
            if key:
                cfg = load_weflow_cfg()
                prof = profile_of(cfg, wxid)
                prof["decrypt_key"] = key
                prof["wxid"] = wxid
                db_path = self._find_db_path()
                if db_path:
                    prof["db_path"] = db_path
                cfg["current_profile"] = wxid
                save_weflow_cfg(cfg)
                self.log_line("✅ 数据库密钥已保存到这个账号的档案！")
                self.log_line("再点「② 提取图片密钥」即可完成配置。")
                self.after(0, lambda: self.tree.item(
                    wxid, values=(self.tree.set(wxid, "name"), wxid,
                                  "✅ 已配置")))
                self._fetch_nickname(wxid)
            elif fail_hint:
                self.log_line(f"❌ 提取失败（{fail_hint}）。")
                self.log_line("这是 Mac 上最常见的问题，通常按这个顺序能解决：")
                self.log_line("1. 把微信降级到 4.1.7 或 4.1.8.100 版本；")
                self.log_line("2. Command+Q 彻底退出微信，然后【重启 Mac】；")
                self.log_line("3. 重启后打开微信（先别登录），回到本面板"
                              "再点一次「提取数据库密钥」，然后在微信里登录。")
                self.log_line("提示：不要连续反复点提取，容易触发微信保护。")
            else:
                self.log_line("❌ 等了 10 分钟没拿到密钥。如果你取消了密码框，"
                              "请再试一次并输入密码；否则把终端窗口截图发我。")
        self._task(work)

    def _fetch_nickname(self, wxid):
        """密钥配好后，尝试读取该账号的真实昵称。"""
        try:
            code, out, _ = run_cmd([WEFLOW, "chat", "contact", wxid, "--json"],
                                   timeout=60)
            if code != 0:
                return
            data = json.loads(out)
            d = data.get("data", data)
            nick = ""
            if isinstance(d, dict):
                nick = (d.get("nickname") or d.get("nick_name")
                        or d.get("name") or "")
            if nick:
                cache = panel_cfg_load()
                cache.setdefault(wxid, {})["nickname"] = nick
                panel_cfg_save(cache)
                acc = next((a for a in self.accounts if a["wxid"] == wxid), None)
                if acc:
                    acc["nickname"] = nick
                self.log_line(f"✅ 读到微信昵称：{nick}")

                def upd():
                    if self.tree.exists(wxid):
                        a = next((x for x in self.accounts
                                  if x["wxid"] == wxid), {})
                        disp = a.get("remark") or nick
                        self.tree.item(wxid, values=(disp, wxid, "✅ 已配置"))
                self.after(0, upd)
        except Exception:  # noqa: BLE001
            pass

    def extract_image_key(self):
        wxid = self.selected_wxid.get()
        if "（" in wxid:
            messagebox.showinfo("提示", "请先在第 1 步选择账号。")
            return

        def work():
            self.log_line("⏳ 正在推导图片密钥……")
            code, out, err = run_cmd([WEFLOW, "key", "image"], timeout=180)
            xor_m = re.search(r"image_xor_key[:：]\s*(\S+)", out)
            aes_m = re.search(r"image_aes_key[:：]\s*(\S+)", out)
            xor_v, aes_v = None, None
            if xor_m or aes_m:
                if xor_m:
                    s = xor_m.group(1)
                    xor_v = int(s) if s.isdigit() else s  # 工具要求数值类型
                if aes_m:
                    aes_v = aes_m.group(1)
            else:
                self.log_line("⚠️ 缓存推导不成功，改从微信数据扫描……")
                acc = next((a for a in self.accounts if a["wxid"] == wxid), {})
                udir = acc.get("path") or ""
                if not udir:
                    db = self._find_db_path()
                    if db:
                        cands = [d for d in os.listdir(db)
                                 if d.startswith(wxid + "_")]
                        if cands:
                            udir = os.path.join(db, cands[0])
                if udir:
                    code2, out2, _ = run_cmd(
                        [WEFLOW, "key", "scan-image", udir, "--json"],
                        timeout=300)
                    try:
                        d2 = json.loads(out2).get("data", {})
                        result = d2.get("result")
                        result = json.loads(result) if isinstance(result, str) \
                            else (result or {})
                        found = None
                        any_code = None
                        for a in result.get("accounts", []):
                            for k in a.get("keys", []):
                                if k.get("code"):
                                    any_code = k["code"]
                                if a.get("wxid") == wxid and not found:
                                    found = k
                        if found:
                            xor_v = found.get("xorKey")
                            aes_v = found.get("aesKey")
                        elif any_code:
                            # 密钥推导公式（来自源码）：xorKey=code&0xFF，
                            # aesKey=md5(str(code)+wxid)[:16]，code 全账号通用
                            import hashlib
                            xor_v = any_code & 0xFF
                            aes_v = hashlib.md5(
                                (str(any_code) + wxid).encode()
                            ).hexdigest()[:16]
                            self.log_line("🧮 已用密钥码直接计算出该账号的图片密钥。")
                    except Exception:  # noqa: BLE001
                        pass
            if xor_v is not None or aes_v:
                cfg = load_weflow_cfg()
                prof = profile_of(cfg, wxid)
                if xor_v is not None:
                    prof["image_xor_key"] = xor_v
                if aes_v:
                    prof["image_aes_key"] = aes_v
                save_weflow_cfg(cfg)
                self.log_line("✅ 图片密钥已保存。配置完成，可以导出了！")
            else:
                self.log_line("❌ 仍拿不到图片密钥。请确认：微信正登录该账号，"
                              "并在微信里点开几张聊天图片后再试一次。")
                self.log_line("（文字记录不受影响，可以直接导出；"
                              "图片密钥只影响图片/视频的解密。）")
        self._task(work)

    def test_connection(self):
        def work():
            self.log_line("⏳ 正在测试连接（读取会话列表）……")
            code, out, err = run_cmd([WEFLOW, "chat", "sessions"])
            if code == 0 and out.strip():
                lines = [l for l in out.splitlines() if l.strip()]
                self.log_line(f"✅ 连接成功！读到 {max(0, len(lines) - 1)} 行会话数据：")
                for line in lines[:6]:
                    self.log_line("   " + line)
                wxid = self.selected_wxid.get()
                if "（" not in wxid:
                    self._fetch_nickname(wxid)
            else:
                self.log_line(f"❌ 连接失败：{(err.strip() or out.strip())[:300]}")
                self.log_line("请确认：① 已提取密钥 ② 微信登录账号与所选账号一致。")
        self._task(work)

    # ---------- 导出 ----------
    def _get_sessions(self):
        code, out, err = run_cmd([WEFLOW, "chat", "sessions", "--json"])
        sessions = []
        try:
            data = json.loads(out)
            items = data.get("data", data) if isinstance(data, dict) else data
            if isinstance(items, dict):
                items = items.get("sessions") or items.get("list") or []
            for it in items or []:
                if isinstance(it, dict):
                    sid = (it.get("username") or it.get("session_id")
                           or it.get("id") or it.get("talker") or "")
                    name = (it.get("displayName") or it.get("name")
                            or it.get("nickname") or it.get("remark")
                            or it.get("display_name") or sid)
                    if sid:
                        sessions.append((sid, name))
        except Exception:  # noqa: BLE001
            pass
        return sessions, out, err

    def export_all(self):
        wxid = self.selected_wxid.get()
        if "（" in wxid:
            messagebox.showinfo("提示", "请先在第 1 步选择账号。")
            return
        cfg = load_weflow_cfg()
        prof = cfg.get("profiles", {}).get(wxid, {})
        if not prof.get("decrypt_key"):
            messagebox.showinfo("提示", "这个账号还没有提取密钥，请先完成第 2 步。")
            return
        fmt = self.fmt.get()
        media = self.with_media.get()

        def work():
            self.log_line("⏳ 正在读取会话列表……")
            sessions, raw, err = self._get_sessions()
            if not sessions:
                self.log_line("❌ 读不到会话列表，请先「测试连接」确认配置。")
                self.log_line("原始返回：" + (err.strip() or raw.strip())[:300])
                return
            out_dir = os.path.join(EXPORT_ROOT, wxid)
            os.makedirs(out_dir, exist_ok=True)
            ext = {"html": "html", "excel": "xlsx", "txt": "txt",
                   "json": "json"}[fmt]
            total = len(sessions)
            self.log_line(f"共 {total} 个会话，导出到：{out_dir}")
            ok_cnt, fail_cnt = 0, 0
            for i, (sid, name) in enumerate(sessions, 1):
                safe = re.sub(r'[\\/:*?"<>|]', "_", f"{name}_{sid}")[:80]
                out_file = os.path.join(out_dir, f"{safe}.{ext}")
                if os.path.exists(out_file) and os.path.getsize(out_file) > 100:
                    ok_cnt += 1
                    continue
                args = [WEFLOW, "export", "messages", sid,
                        "--format", fmt, "--out", out_file]
                if media:
                    args += ["--media", "all"]
                code, o, e = run_cmd(args, timeout=1800)
                if code == 0 and os.path.exists(out_file):
                    ok_cnt += 1
                    self.log_line(f"[{i}/{total}] ✅ {name}")
                else:
                    fail_cnt += 1
                    self.log_line(f"[{i}/{total}] ⚠️ {name} 失败："
                                  f"{(e or o).strip()[:120]}")
            self.log_line(f"🎉 导出完成：成功 {ok_cnt}，失败 {fail_cnt}。")
            self.log_line(f"位置：{out_dir}")
            self.log_line("把这个文件夹整个拷给你的主力电脑，"
                          "就能合并进总查看器了。")
        self._task(work)


if __name__ == "__main__":
    if not WEFLOW or not os.path.exists(WEFLOW):
        tk.Tk().withdraw()
        messagebox.showerror("缺少文件", "找不到 weflow 核心组件。")
        sys.exit(1)
    Panel().mainloop()
