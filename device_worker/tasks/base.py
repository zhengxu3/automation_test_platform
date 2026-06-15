"""设备任务基类 — 支持多设备、挂起确认、通知分级"""
import asyncio
import base64
import os
import subprocess
import time
import uuid
import xml.etree.ElementTree as ET
import re

PRIORITY_CRITICAL = "critical"
PRIORITY_NORMAL = "normal"
PRIORITY_LOW = "low"

NOTIFY_FULL = "full"
NOTIFY_WS = "ws"
NOTIFY_SILENT = "silent"


class TaskAborted(Exception):
    pass


class SuspendTimeout(Exception):
    pass


class BaseDeviceTask:
    notify_level = NOTIFY_WS
    default_on_error = "continue"
    suspend_timeout = 60

    def __init__(self, task_id, payload, devices, device_mgr, ws_push, db=None):
        self.task_id = task_id
        self.payload = payload
        if isinstance(devices, str):
            devices = [{"device_id": devices, "role": "default"}]
        self.devices = devices
        self.device_mgr = device_mgr
        self.ws = ws_push
        self.db = db
        self._suspend_event = None
        self._suspend_response = None

    @property
    def device_id(self):
        return self.devices[0]["device_id"] if self.devices else ""

    def device(self, role=None, index=0):
        if role:
            d = next((x for x in self.devices if x.get("role") == role), None)
        else:
            d = self.devices[index] if index < len(self.devices) else None
        if not d:
            raise ValueError(f"设备不存在: role={role}, index={index}")
        return DeviceOps(d["device_id"], self)

    async def run(self):
        raise NotImplementedError

    # ==================== 挂起确认机制 ====================

    async def suspend(self, reason, options=None, default="continue", timeout=None, priority=PRIORITY_NORMAL, screenshot=None):
        timeout = timeout or self.suspend_timeout
        if options is None:
            options = ["继续", "中断"]

        suspend_id = uuid.uuid4().hex[:8]
        self._suspend_event = asyncio.Event()
        self._suspend_response = None

        screenshot_b64 = None
        if screenshot:
            screenshot_b64 = base64.b64encode(screenshot).decode() if isinstance(screenshot, bytes) else screenshot

        msg = {
            "type": "suspend", "task_id": self.task_id, "suspend_id": suspend_id,
            "reason": reason, "options": options, "default": default,
            "priority": priority, "timeout": timeout, "remaining": timeout,
            "screenshot": screenshot_b64, "suspended_at": int(time.time()),
        }
        await self._push(msg)
        await self.log(f"⏸️ 挂起: {reason} (等待 {timeout}s)", level="warning")

        if self.db:
            self.db["device_suspend_queue"].insert_one({
                "suspend_id": suspend_id, "task_id": self.task_id,
                "reason": reason, "options": options, "default": default,
                "priority": priority, "timeout": timeout,
                "status": "pending", "choice": "", "created_at": int(time.time()),
            })

        try:
            await asyncio.wait_for(self._suspend_event.wait(), timeout=timeout)
            choice = self._suspend_response or default
        except asyncio.TimeoutError:
            choice = default
            if self.db:
                self.db["device_suspend_queue"].update_one(
                    {"suspend_id": suspend_id}, {"$set": {"status": "timeout", "choice": default}}
                )
            await self._push({"type": "suspend_timeout", "task_id": self.task_id, "suspend_id": suspend_id, "choice": default})
            await self.log(f"⏰ 超时，自动选择: {default}", level="warning")
            if priority == PRIORITY_CRITICAL:
                raise TaskAborted(f"挂起超时(critical): {reason}")

        self._suspend_event = None
        return choice

    def resolve_suspend(self, choice):
        self._suspend_response = choice
        if self._suspend_event:
            self._suspend_event.set()

    # ==================== 日志推送 ====================

    async def log(self, msg, device_id=None, level="info"):
        await self._push({"type": "log", "task_id": self.task_id, "device_id": device_id or self.device_id, "level": level, "msg": msg})

    async def step(self, action, target, device_id=None, screenshot=None):
        screenshot_b64 = base64.b64encode(screenshot).decode() if screenshot else None
        await self._push({"type": "step", "task_id": self.task_id, "device_id": device_id or self.device_id, "action": action, "target": target, "screenshot": screenshot_b64})

    async def _push(self, data):
        if self.notify_level == NOTIFY_SILENT and data.get("type") not in ("suspend", "status"):
            return
        if self.ws:
            await self.ws.push(self.task_id, data)

    # ==================== 设备存活检查 ====================

    def is_device_alive(self, device_id=None):
        did = device_id or self.device_id
        try:
            r = subprocess.run(f"adb -s {did} get-state", shell=True, capture_output=True, text=True, timeout=5)
            return "device" in r.stdout
        except:
            return False

    # ==================== ADB 工具 ====================

    RETRY_COUNT = 3
    RETRY_INTERVAL = 1
    IMPLICIT_WAIT = 8

    POPUP_DISMISS = [
        {"text": "允许", "desc": ""},
        {"text": "始终允许", "desc": ""},
        {"text": "ALLOW", "desc": ""},
        {"text": "WHILE USING", "desc": ""},
        {"text": "稍后", "desc": ""},
        {"text": "取消", "desc": "update"},
        {"desc": "Close"},
    ]

    def adb(self, cmd, device_id=None, retry=True):
        did = device_id or self.device_id
        for attempt in range(self.RETRY_COUNT if retry else 1):
            try:
                r = subprocess.run(f"adb -s {did} {cmd}", shell=True, capture_output=True, text=True, timeout=30)
                if "error: device" in r.stderr or "device not found" in r.stderr:
                    subprocess.run(f"adb reconnect {did}", shell=True, timeout=10)
                    time.sleep(2)
                    continue
                return r.stdout.strip()
            except subprocess.TimeoutExpired:
                if attempt < self.RETRY_COUNT - 1:
                    time.sleep(self.RETRY_INTERVAL)
                    continue
                return ""
        return ""

    def tap(self, x, y, device_id=None):
        did = device_id or self.device_id
        self.adb(f"shell input tap {x} {y}", did)
        if self.ws:
            self.ws.report_tap_sync(did, x, y)
        time.sleep(1)

    def swipe(self, x1, y1, x2, y2, duration=300, device_id=None):
        self.adb(f"shell input swipe {x1} {y1} {x2} {y2} {duration}", device_id)
        time.sleep(0.5)

    def input_text(self, text, device_id=None):
        self.adb(f"shell input text '{text}'", device_id)

    def keyevent(self, key, device_id=None):
        self.adb(f"shell input keyevent {key}", device_id)

    def dump_ui(self, device_id=None):
        did = device_id or self.device_id
        self.adb("shell uiautomator dump /sdcard/ui.xml", did)
        xml_str = self.adb("shell cat /sdcard/ui.xml", did)
        try:
            return ET.fromstring(xml_str)
        except:
            return None

    def find_element(self, tree=None, text=None, desc=None, resource_id=None, device_id=None, wait=True):
        timeout = self.IMPLICIT_WAIT if wait else 0
        start = time.time()
        while True:
            if tree is None:
                tree = self.dump_ui(device_id)
            if tree is not None:
                result = self._find_in_tree(tree, text, desc, resource_id)
                if result:
                    return result
            if time.time() - start >= timeout:
                return None
            time.sleep(1)
            tree = None

    def _find_in_tree(self, tree, text=None, desc=None, resource_id=None):
        for elem in tree.iter("node"):
            e_text = elem.get("text", "")
            e_desc = elem.get("content-desc", "")
            e_rid = elem.get("resource-id", "")
            bounds = elem.get("bounds", "")
            match = False
            if text and text.lower() in e_text.lower():
                match = True
            if desc and desc.lower() in e_desc.lower():
                match = True
            if resource_id and resource_id in e_rid:
                match = True
            if match and bounds:
                nums = re.findall(r'\d+', bounds)
                if len(nums) == 4:
                    return (int(nums[0]) + int(nums[2])) // 2, (int(nums[1]) + int(nums[3])) // 2
        return None

    def tap_element(self, text=None, desc=None, resource_id=None, device_id=None):
        pos = self.find_element(text=text, desc=desc, resource_id=resource_id, device_id=device_id)
        if pos:
            self.tap(pos[0], pos[1], device_id)
            return True
        self._save_failure_screenshot(device_id, f"tap_failed_{text or desc or resource_id}")
        return False

    # ==================== 弹窗自动处理 ====================

    def dismiss_popups(self, device_id=None):
        tree = self.dump_ui(device_id)
        if tree is None:
            return False
        for popup in self.POPUP_DISMISS:
            pos = self._find_in_tree(tree, text=popup.get("text"), desc=popup.get("desc"))
            if pos:
                self.tap(pos[0], pos[1], device_id)
                time.sleep(0.5)
                return True
        return False

    def ensure_no_popup(self, device_id=None, max_attempts=3):
        for _ in range(max_attempts):
            if not self.dismiss_popups(device_id):
                return
            time.sleep(0.5)

    # ==================== 设备恢复 ====================

    def recover_to_home(self, device_id=None):
        did = device_id or self.device_id
        self.keyevent("KEYCODE_HOME", did)
        time.sleep(1)
        self.keyevent("KEYCODE_HOME", did)
        time.sleep(0.5)

    def recover_device(self, device_id=None):
        did = device_id or self.device_id
        self.adb("shell input keyevent KEYCODE_WAKEUP", did)
        time.sleep(1)
        self.adb("shell input swipe 540 2000 540 1000 300", did)
        time.sleep(0.5)
        self.ensure_no_popup(did)
        self.recover_to_home(did)

    # ==================== 失败截图（保存到 /tmp） ====================

    def _save_failure_screenshot(self, device_id=None, tag="failure"):
        try:
            screenshot = self.screenshot_bytes(device_id)
            if screenshot:
                out_dir = f"/tmp/device_outputs/{self.task_id}"
                os.makedirs(out_dir, exist_ok=True)
                path = os.path.join(out_dir, f"{tag}_{uuid.uuid4().hex[:8]}.png")
                with open(path, 'wb') as f:
                    f.write(screenshot)
        except Exception:
            pass

    # ==================== 查询工具 ====================

    def get_activity(self, device_id=None):
        return self.adb("shell dumpsys activity top | grep ACTIVITY | tail -1", device_id)

    def is_app_running(self, package, device_id=None):
        return bool(self.adb(f"shell pidof {package}", device_id).strip())

    def screenshot_bytes(self, device_id=None):
        did = device_id or self.device_id
        r = subprocess.run(f"adb -s {did} exec-out screencap -p", shell=True, capture_output=True, timeout=10)
        return r.stdout


class DeviceOps:
    """单设备操作器"""

    def __init__(self, device_id, task):
        self.device_id = device_id
        self._task = task

    def adb(self, cmd):
        return self._task.adb(cmd, self.device_id)

    def tap(self, x, y):
        self._task.tap(x, y, self.device_id)

    def tap_element(self, text=None, desc=None, resource_id=None):
        return self._task.tap_element(text=text, desc=desc, resource_id=resource_id, device_id=self.device_id)

    def swipe(self, x1, y1, x2, y2, duration=300):
        self._task.swipe(x1, y1, x2, y2, duration, self.device_id)

    def input_text(self, text):
        self._task.input_text(text, self.device_id)

    def keyevent(self, key):
        self._task.keyevent(key, self.device_id)

    def dump_ui(self):
        return self._task.dump_ui(self.device_id)

    def find_element(self, text=None, desc=None, resource_id=None, wait=True):
        return self._task.find_element(text=text, desc=desc, resource_id=resource_id, device_id=self.device_id, wait=wait)

    def screenshot_bytes(self):
        return self._task.screenshot_bytes(self.device_id)

    def get_activity(self):
        return self._task.get_activity(self.device_id)

    def is_app_running(self, package):
        return self._task.is_app_running(package, self.device_id)

    def is_alive(self):
        return self._task.is_device_alive(self.device_id)

    def dismiss_popups(self):
        return self._task.dismiss_popups(self.device_id)

    def ensure_no_popup(self):
        self._task.ensure_no_popup(self.device_id)

    def recover(self):
        self._task.recover_device(self.device_id)
