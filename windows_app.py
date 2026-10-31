"""Windows desktop installer and scheduled runner for the DMIT monitor."""
from __future__ import annotations

import base64
import contextlib
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading

TASK_NAME = 'DMIT-Premium-Stock-Monitor'
DATA_DIR = Path(os.getenv('DMIT_DATA_DIR', str(Path(os.getenv('LOCALAPPDATA', '.')) / 'DMITPremiumMonitor')))
APP_DIR = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent
NO_WINDOW = 0x08000000


class Blob(ctypes.Structure):
    _fields_ = [('length', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]


def protect(data: bytes, decrypt=False):
    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    output = Blob()
    api = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    function = api.CryptUnprotectData if decrypt else api.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output)):
        raise OSError(ctypes.get_last_error(), 'Windows 无法加密或解密设备密钥。请使用原 Windows 账号。')
    try:
        return ctypes.string_at(output.data, output.length)
    finally:
        kernel.LocalFree(ctypes.cast(output.data, ctypes.c_void_p))


def core_module():
    os.environ['STATE_FILE'] = str(DATA_DIR / 'state.json')
    os.environ['BROWSER_PROFILE'] = str(DATA_DIR / 'browser-profile')
    os.environ['BROWSER_CHANNEL'] = 'msedge'
    os.environ['BROWSER_MINIMIZE'] = '1'
    os.environ['HEADLESS'] = '0'
    import monitor
    return monitor


def save_key(value, core):
    os.environ['BARK_KEY'] = value.strip()
    key, _ = core.validate_push_config()
    encrypted = base64.b64encode(protect(key.encode('utf-8'))).decode('ascii')
    core.save(DATA_DIR / 'credential.json', {'protected_key': encrypted})
    os.environ['BARK_KEY'] = key


def load_key():
    saved = json.loads((DATA_DIR / 'credential.json').read_text(encoding='utf-8'))
    return protect(base64.b64decode(saved['protected_key']), decrypt=True).decode('utf-8')


@contextlib.contextmanager
def check_lock():
    import msvcrt
    with (DATA_DIR / 'check.lock').open('a+b') as lock:
        lock.seek(0, 2)
        if lock.tell() == 0:
            lock.write(b'0')
            lock.flush()
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as error:
            raise RuntimeError('另一次检查正在进行，请稍后再试。') from error
        try:
            yield
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def ps_literal(value):
    return "'" + str(value).replace("'", "''") + "'"


def powershell(script, timeout=30):
    encoded = base64.b64encode(script.encode('utf-16-le')).decode('ascii')
    result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive',
                             '-EncodedCommand', encoded], capture_output=True, timeout=timeout,
                            creationflags=NO_WINDOW)
    if result.returncode:
        raise RuntimeError('Windows 定时任务设置失败。请用同一 Windows 账号右键“以管理员身份运行”后重试。')
    return result.stdout.decode('utf-8', errors='replace').strip()


def task_script(exe_path):
    return f"""
$ErrorActionPreference = 'Stop'
$taskUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$taskAction = New-ScheduledTaskAction -Execute {ps_literal(exe_path)} -Argument '--scheduled' -WorkingDirectory {ps_literal(Path(exe_path).parent)}
$repeatTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(30) -RepetitionInterval (New-TimeSpan -Minutes 30)
$logonTrigger = New-ScheduledTaskTrigger -AtLogOn -User $taskUser
$taskPrincipal = New-ScheduledTaskPrincipal -UserId $taskUser -LogonType Interactive -RunLevel Limited
$taskSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 10) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName '{TASK_NAME}' -Action $taskAction -Trigger @($repeatTrigger, $logonTrigger) -Principal $taskPrincipal -Settings $taskSettings -Description 'DMIT 美国西部 Premium 每 30 分钟检查；Bark 手机提醒' -Force | Out-Null
if (-not (Get-ScheduledTask -TaskName '{TASK_NAME}' -ErrorAction Stop)) {{ throw 'Task not found' }}
"""


def enable_task():
    if not getattr(sys, 'frozen', False):
        raise RuntimeError('请使用打包好的 DMITMonitor.exe 启用定时任务。')
    powershell(task_script(sys.executable))


def disable_task():
    powershell(f"""
$ErrorActionPreference = 'Stop'
Stop-ScheduledTask -TaskName '{TASK_NAME}' -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName '{TASK_NAME}' -Confirm:$false -ErrorAction SilentlyContinue
""")


def task_enabled():
    try:
        text = powershell(f"$t = Get-ScheduledTask -TaskName '{TASK_NAME}' -ErrorAction SilentlyContinue; if ($t -and $t.State -ne 'Disabled') {{ 'ENABLED' }}")
        return 'ENABLED' in text
    except Exception:
        return False


def verify_site(progress, core):
    from playwright.sync_api import sync_playwright
    progress('正在打开专用 Edge 浏览器。若出现网站验证，请在浏览器中手动完成。')
    with sync_playwright() as p:
        try:
            context = p.chromium.launch_persistent_context(str(DATA_DIR / 'browser-profile'),
                                                          channel='msedge', headless=False, locale='en-US')
        except Exception as error:
            raise RuntimeError('无法启动 Microsoft Edge。请确认这台服务器装有 Edge，并关闭本程序其他正在检查的窗口。') from error
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(core.CART, wait_until='domcontentloaded', timeout=60000)
            page.locator('.cart-products-box[pid] .cart-products-title').first.wait_for(state='attached', timeout=180000)
            names = page.locator('.cart-products-title').all_text_contents()
            if not any(core.in_scope(name.strip()) for name in names):
                raise RuntimeError('页面中未找到美国西部 Premium 套餐，未启用监控。')
        finally:
            context.close()


def launch_gui(core, smoke=False):
    import tkinter as tk
    from tkinter import messagebox, ttk
    root = tk.Tk()
    root.title('DMIT Premium 补货提醒')
    root.geometry('650x525')
    root.resizable(False, False)
    root.configure(background='#f3f5f8')
    style = ttk.Style(root)
    style.theme_use('clam')
    style.configure('TFrame', background='#f3f5f8')
    style.configure('TLabel', background='#f3f5f8', font=('Microsoft YaHei UI', 10))
    style.configure('Title.TLabel', font=('Microsoft YaHei UI', 19, 'bold'), foreground='#163353')
    style.configure('TButton', font=('Microsoft YaHei UI', 10), padding=8)
    outer = ttk.Frame(root, padding=25)
    outer.pack(fill='both', expand=True)
    ttk.Label(outer, text='DMIT Premium 补货提醒', style='Title.TLabel').pack(anchor='w')
    ttk.Label(outer, text='美国西部  ·  月付 < $20 或年付 < $100  ·  每 30 分钟', foreground='#4b6077').pack(anchor='w', pady=(6, 15))
    ttk.Label(outer, text='Bark 个人推送链接或设备密钥').pack(anchor='w')
    key_var = tk.StringVar()
    key_entry = ttk.Entry(outer, textvariable=key_var, show='•', font=('Segoe UI', 11))
    key_entry.pack(fill='x', pady=(5, 6), ipady=4)
    saved = (DATA_DIR / 'credential.json').exists()
    hint = '已保存加密密钥。留空可继续使用；粘贴新链接可替换。' if saved else '从 iPhone Bark 首页复制 https://api.day.app/ 开头的链接并粘贴。'
    ttk.Label(outer, text=hint, foreground='#687687', wraplength=590).pack(anchor='w')
    status_var = tk.StringVar(value='预览模式：不会联网或启用定时任务。' if smoke else '正在读取状态…')
    status_label = ttk.Label(outer, textvariable=status_var, wraplength=580, foreground='#143f6b')
    status_label.pack(fill='x', pady=(20, 12))
    busy = False
    events = queue.Queue()
    controls = []

    def progress(text):
        events.put(('status', text))

    def resolve_key(value):
        if value.strip():
            save_key(value, core)
        else:
            try:
                os.environ['BARK_KEY'] = load_key()
            except Exception as error:
                raise RuntimeError('请先粘贴 Bark 的个人推送链接。') from error
        core.validate_push_config()

    def action(kind):
        nonlocal busy
        if smoke or busy:
            return
        value = key_var.get()
        busy = True
        for button in controls:
            button.state(['disabled'])

        def work():
            try:
                if kind == 'stop':
                    disable_task()
                    events.put(('done', '已停用定时监控，密钥和历史记录已保留。'))
                    return
                resolve_key(value)
                if kind == 'test':
                    progress('正在发送测试铃声，请查看 iPhone。')
                    core.send_push('DMIT提醒测试（并非补货）', '这是一条来自 Windows 私有服务器的测试铃声。请确认锁屏、静音和专注模式下能听见。')
                    events.put(('done', '测试推送已被 Bark 接受；请在 iPhone 确认是否实际响铃。'))
                    return
                with check_lock():
                    if kind == 'enable':
                        progress('第 1/3 步：测试 iPhone 响铃。')
                        core.send_push('DMIT提醒测试（并非补货）', '正在配置 Windows 服务器监控。此条为测试铃声。')
                        verify_site(progress, core)
                        progress('第 2/3 步：核实商品库存与实际计费选项。')
                        if core.run(check_only=True) != 0:
                            raise RuntimeError('网页或计费选项检查未通过，尚未启用定时监控。请打开运行记录。')
                        progress('第 3/3 步：注册每 30 分钟检查任务。')
                        enable_task()
                        # Check now as well; future checks are owned by Task Scheduler.
                        result = core.run(check_only=False)
                        if result != 0:
                            raise RuntimeError('定时任务已注册，但首次正式检查失败。请查看记录；勿把当前状态视为监控正常。')
                        events.put(('done', '已启用：每 30 分钟检查，登录本机后也会检查。可以关闭此窗口。'))
                    else:
                        progress('正在检查网页。若失败，可点击“保存并启用”重新完成网站验证。')
                        if core.run(check_only=False) != 0:
                            raise RuntimeError('本次检查失败；未把访问失败当成补货。请打开运行记录。')
                        events.put(('done', '检查完成。符合条件且新出现的有货套餐会发送手机响铃。'))
            except Exception as error:
                # Display known application errors, not request data or credentials.
                message = str(error) if isinstance(error, (RuntimeError, core.CheckError)) else type(error).__name__
                print(core.utcnow(), message)
                events.put(('error', message))
            finally:
                events.put(('idle', ''))
        threading.Thread(target=work, daemon=True).start()

    first_row = ttk.Frame(outer)
    first_row.pack(fill='x', pady=(0, 9))
    enable = ttk.Button(first_row, text='保存并启用监控（会测试响铃）', command=lambda: action('enable'))
    enable.pack(side='left', fill='x', expand=True, padx=(0, 8))
    test = ttk.Button(first_row, text='发送测试铃声', command=lambda: action('test'))
    test.pack(side='left')
    second_row = ttk.Frame(outer)
    second_row.pack(fill='x')
    check = ttk.Button(second_row, text='立即检查', command=lambda: action('check'))
    check.pack(side='left', padx=(0, 8))
    stop = ttk.Button(second_row, text='停用定时监控', command=lambda: action('stop'))
    stop.pack(side='left', padx=(0, 8))
    logs = ttk.Button(second_row, text='打开运行记录', command=lambda: os.startfile(str(DATA_DIR / 'monitor.log')))
    logs.pack(side='left')
    controls.extend([enable, test, check, stop])
    ttk.Separator(outer).pack(fill='x', pady=(21, 13))
    ttk.Label(outer, text='仅监控 Premium，排除 Eyeball 和 Tier 1。\n服务器需开机联网，Windows 账号需保持登录（可以锁屏）。\n若网站要求人机验证，需由你手动完成。不会自动下单。',
              foreground='#687687', wraplength=590, justify='left').pack(anchor='w')

    def poll():
        nonlocal busy
        try:
            while True:
                kind, text = events.get_nowait()
                if kind in {'status', 'done', 'error'}:
                    status_var.set(text)
                if kind == 'error':
                    messagebox.showerror('需要处理', text, parent=root)
                if kind == 'idle':
                    busy = False
                    for button in controls:
                        button.state(['!disabled'])
        except queue.Empty:
            pass
        root.after(150, poll)

    def initial_state():
        enabled = task_enabled()
        progress('定时任务已存在。点击“立即检查”可验证当前网站访问。' if enabled else '尚未启用。粘贴 Bark 链接后点击“保存并启用监控”。')

    def close():
        if busy:
            messagebox.showinfo('正在处理', '请等待当前配置或检查完成后再关闭窗口。', parent=root)
        else:
            root.destroy()

    root.protocol('WM_DELETE_WINDOW', close)
    root.after(150, poll)
    if not smoke:
        threading.Thread(target=initial_state, daemon=True).start()
    root.mainloop()


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    logfile = DATA_DIR / 'monitor.log'
    if logfile.exists() and logfile.stat().st_size > 2_000_000:
        logfile.replace(DATA_DIR / 'monitor.previous.log')
    log = logfile.open('a', encoding='utf-8', buffering=1)
    sys.stdout = log
    sys.stderr = log
    core = core_module()
    if '--self-test' in sys.argv:
        import playwright
        sample = b'dummy-test-key-not-a-real-credential'
        assert protect(protect(sample), decrypt=True) == sample
        assert core.in_scope('LAX.AS3.Pro.TINY')
        assert not core.in_scope('LAX.AS3.EB.TINY')
        assert not core.qualifying_prices({'monthly': '20', 'annually': '100'})
        assert (Path(playwright.__file__).parent / 'driver' / 'node.exe').exists()
        assert (Path(core.__file__).parent / 'extract_catalog.js').exists()
        result = {'result': 'passed', 'checks': ['DPAPI roundtrip', 'Premium scope', 'strict prices', 'bundled driver', 'DOM extractor']}
        Path(sys.argv[sys.argv.index('--self-test') + 1]).write_text(json.dumps(result), encoding='utf-8')
        return 0
    if '--scheduled' in sys.argv:
        try:
            os.environ['BARK_KEY'] = load_key()
            core.validate_push_config()
            with check_lock():
                return core.run(check_only=False)
        except Exception as error:
            print(core.utcnow(), type(error).__name__)
            return 1
    launch_gui(core, smoke='--preview' in sys.argv)
    return 0


if __name__ == '__main__':
    sys.exit(main())
