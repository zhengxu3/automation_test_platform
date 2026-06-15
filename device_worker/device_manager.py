"""设备管理器 — 发现/锁定/释放/准备"""
import subprocess
import time


class DeviceManager:
    STATUS_IDLE = "idle"
    STATUS_BUSY = "busy"
    STATUS_OFFLINE = "offline"

    def __init__(self, db, config):
        self.db = db
        self.config = config
        self.col = db[config["collections"]["device_pool"]]

    # ==================== 设备发现 ====================

    def discover(self):
        output = self._adb("devices")
        connected = set()
        for line in output.strip().split("\n")[1:]:
            parts = line.split("\t")
            if len(parts) == 2:
                device_id, state = parts[0].strip(), parts[1].strip()
                connected.add(device_id)
                if state == "device":
                    self._register_or_update(device_id)
                else:
                    self._mark_offline(device_id)
        for dev in self.col.find({"status": {"$ne": self.STATUS_OFFLINE}}):
            if dev["device_id"] not in connected:
                self._mark_offline(dev["device_id"])

    def _register_or_update(self, device_id):
        existing = self.col.find_one({"device_id": device_id})
        now = int(time.time())
        if existing:
            update = {"last_heartbeat": now}
            if existing["status"] == self.STATUS_OFFLINE:
                update["status"] = self.STATUS_IDLE
            self.col.update_one({"device_id": device_id}, {"$set": update})
        else:
            self._init_device(device_id)
            model = self._adb(f"-s {device_id} shell getprop ro.product.model").strip()
            resolution = self._adb(f"-s {device_id} shell wm size").replace("Physical size: ", "").strip()
            self.col.insert_one({
                "device_id": device_id,
                "type": "emulator" if "emulator" in device_id else "physical",
                "model": model or "unknown",
                "resolution": resolution,
                "status": self.STATUS_IDLE,
                "locked_by": "",
                "locked_at": 0,
                "last_heartbeat": now,
                "initialized": True,
            })

    def _init_device(self, device_id):
        cmds = [
            "shell settings put system screen_off_timeout 1800000",
            "shell svc power stayon usb",
            "shell settings put global install_non_market_apps 1",
            "shell settings put secure lock_screen_lock_after_timeout 0",
            "shell settings put global stay_on_while_plugged_in 3",
        ]
        for cmd in cmds:
            self._adb(f"-s {device_id} {cmd}")
        print(f"🔧 设备初始化完成: {device_id}")

    def _mark_offline(self, device_id):
        self.col.update_one({"device_id": device_id}, {"$set": {"status": self.STATUS_OFFLINE}})

    # ==================== 锁定/释放 ====================

    def acquire(self, task_id, requirements=None):
        query = {"status": self.STATUS_IDLE}
        if requirements and requirements.get("type"):
            query["type"] = requirements["type"]
        result = self.col.find_one_and_update(
            query,
            {"$set": {"status": self.STATUS_BUSY, "locked_by": task_id, "locked_at": int(time.time())}},
        )
        return result["device_id"] if result else None

    def acquire_multi(self, task_id, requirements_list):
        acquired = []
        for req in requirements_list:
            device_id = self.acquire(task_id, req)
            if not device_id:
                for d in acquired:
                    self.release(d)
                return None
            acquired.append(device_id)
        return acquired

    def release(self, device_id):
        self.col.update_one(
            {"device_id": device_id},
            {"$set": {"status": self.STATUS_IDLE, "locked_by": "", "locked_at": 0}}
        )

    def release_expired(self):
        threshold = int(time.time()) - self.config["device"]["lock_timeout"]
        self.col.update_many(
            {"status": self.STATUS_BUSY, "locked_at": {"$lt": threshold, "$gt": 0}},
            {"$set": {"status": self.STATUS_IDLE, "locked_by": "", "locked_at": 0}}
        )

    # ==================== 设备准备 ====================

    def prepare(self, device_id, package=None, apk_path=None):
        self._adb(f"-s {device_id} shell input keyevent KEYCODE_WAKEUP")
        time.sleep(1)
        self._adb(f"-s {device_id} shell input swipe 540 2000 540 1000 300")
        time.sleep(0.5)
        pw = self.config["device"].get("unlock_password", "")
        if pw:
            self._adb(f"-s {device_id} shell input text {pw}")
            self._adb(f"-s {device_id} shell input keyevent KEYCODE_ENTER")
            time.sleep(1)

        if not self._is_home(device_id):
            self._adb(f"-s {device_id} shell input keyevent KEYCODE_HOME")
            time.sleep(1)
            if not self._is_home(device_id):
                return False, "无法到达桌面"

        if apk_path:
            r = self._adb(f"-s {device_id} install -r -g {apk_path}")
            if "Success" not in r:
                return False, f"APK安装失败: {r[:100]}"

        if package:
            self._adb(f"-s {device_id} shell monkey -p {package} -c android.intent.category.LAUNCHER 1")
            time.sleep(3)
            if not self._is_foreground(device_id, package):
                return False, f"{package} 启动失败"

        return True, ""

    # ==================== 查询 ====================

    def get_list(self):
        return list(self.col.find({}, {"_id": 0}))

    def get_capacity(self):
        devices = self.get_list()
        return {
            "total": len(devices),
            "idle": sum(1 for d in devices if d["status"] == self.STATUS_IDLE),
            "busy": sum(1 for d in devices if d["status"] == self.STATUS_BUSY),
            "offline": sum(1 for d in devices if d["status"] == self.STATUS_OFFLINE),
        }

    # ==================== 工具 ====================

    def _is_home(self, device_id):
        a = self._adb(f"-s {device_id} shell dumpsys activity top | grep ACTIVITY | tail -1")
        return "Launcher" in a or "Home" in a

    def _is_foreground(self, device_id, package):
        return bool(self._adb(f"-s {device_id} shell pidof {package}").strip())

    @staticmethod
    def _adb(cmd):
        adb_bin = "/opt/homebrew/Caskroom/android-platform-tools/37.0.0/platform-tools/adb"
        r = subprocess.run(f"{adb_bin} {cmd}", shell=True, capture_output=True, text=True, timeout=30)
        return r.stdout
