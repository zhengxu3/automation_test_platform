"""外部脚本执行任务 — 拉脚本 → 注入环境 → 执行 → 定时截图 → 收集结果"""
import asyncio
import subprocess
import os
import json
import time
import shutil
import uuid
from device_worker.tasks.base import BaseDeviceTask, NOTIFY_SILENT

HANDLER_META = {
    "key": "external_script",
    "label": "外部脚本执行",
    "description": "托管执行外部自动化脚本，管理设备生命周期，统一收集结果",
}

SCRIPT_OUTPUT_ROOT = "/tmp/device_tasks"
SCRIPT_INSTALL_DIR = os.path.expanduser("~/device_scripts")


class ExternalScriptTask(BaseDeviceTask):
    notify_level = NOTIFY_SILENT
    default_on_error = "continue"

    async def run(self):
        script_id = self.payload.get("script_id", "")
        script_source = self.payload.get("script", "")
        params = self.payload.get("params", {})
        timeout = self.payload.get("timeout", 600)
        monitor_interval = self.payload.get("monitor_interval", 5)
        if self.payload.get("entry_command"):
            monitor_interval = 15

        output_dir = os.path.join(SCRIPT_OUTPUT_ROOT, self.task_id, "output")
        os.makedirs(output_dir, exist_ok=True)

        entry_command = self.payload.get("entry_command", "")
        if entry_command:
            script_path = None
        else:
            script_path = self._resolve_script(script_id, script_source)
            if not script_path:
                await self._save_result("fail", "脚本不存在或准备失败")
                return

        self.adb("shell settings put system pointer_location 1")

        env = os.environ.copy()
        adb_dir = "/opt/homebrew/Caskroom/android-platform-tools/37.0.0/platform-tools"
        env["PATH"] = f"{adb_dir}:{env.get('PATH', '')}"
        env["DEVICE_ID"] = self.device_id
        env["DEVICE_TASK_ID"] = self.task_id
        env["DEVICE_TASK_OUTPUT_DIR"] = output_dir
        env.update({f"PARAM_{k.upper()}": str(v) for k, v in params.items()})

        inputs = self.payload.get("inputs", {})
        work_dir = self.payload.get("work_dir", "") or (os.path.dirname(script_path) if script_path else "")
        work_dir = os.path.expanduser(work_dir)
        env_file_path = os.path.join(work_dir, ".env") if work_dir else ""
        if env_file_path:
            self._generate_env_file(env_file_path, inputs, output_dir)

        # 推流策略
        adb_profile = self.payload.get("adb_profile", {})
        stream_strategy = adb_profile.get("stream_strategy", "normal")
        try:
            from device_worker.screen_server import screen_server_instance
            if screen_server_instance:
                screen_server_instance.set_stream_strategy(self.device_id, stream_strategy)
        except Exception:
            pass

        # conda 环境
        conda_env = self.payload.get("conda_env", "")
        if conda_env:
            check = await asyncio.create_subprocess_shell(
                f"conda env list | grep -q {conda_env}",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            await check.communicate()
            if check.returncode != 0:
                await self.log(f"📦 创建环境 {conda_env}...")
                create = await asyncio.create_subprocess_shell(
                    f"conda create -n {conda_env} python=3.11 -y",
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
                )
                await create.communicate()
                if script_path:
                    req_txt = os.path.join(os.path.dirname(script_path), "requirements.txt")
                    if os.path.isfile(req_txt):
                        pip = await asyncio.create_subprocess_shell(
                            f"conda run -n {conda_env} pip install -r {req_txt}",
                            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
                        )
                        await pip.communicate()

        await self.log("▶️ 开始执行...")
        try:
            if entry_command:
                adb_dir = "/opt/homebrew/Caskroom/android-platform-tools/37.0.0/platform-tools"
                if conda_env:
                    conda_bin = os.path.expanduser("~/miniconda3/bin/conda")
                    cmd = f"export PATH={adb_dir}:$PATH && cd {work_dir} && rm -rf trajectories/* 2>/dev/null; {conda_bin} run --no-capture-output -n {conda_env} {entry_command}"
                else:
                    cmd = f"export PATH={adb_dir}:$PATH && {entry_command}"
                proc = await asyncio.create_subprocess_shell(
                    cmd, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                    cwd=work_dir or None
                )
            elif conda_env:
                conda_bin = os.path.expanduser("~/miniconda3/bin/conda")
                proc = await asyncio.create_subprocess_shell(
                    f"{conda_bin} run -n {conda_env} python -u {script_path}",
                    env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                    cwd=os.path.dirname(script_path)
                )
            else:
                proc = await asyncio.create_subprocess_exec(
                    "python3", "-u", script_path,
                    env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                    cwd=os.path.dirname(script_path)
                )

            monitor_task = asyncio.create_task(self._monitor_loop(proc, output_dir, monitor_interval))
            stdout_lines = []
            stdout_task = asyncio.create_task(self._stream_stdout(proc, stdout_lines))
            try:
                await asyncio.wait_for(proc.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                proc.kill()
                await self.log("⏰ 脚本执行超时", level="error")
            monitor_task.cancel()
            stdout_task.cancel()
            exit_code = proc.returncode if proc.returncode is not None else -1
            stdout = "\n".join(stdout_lines).encode()
            stderr = (await proc.stderr.read()) if proc.stderr else b""
        except Exception as e:
            exit_code = -1
            stdout, stderr = b"", str(e).encode()
            await self.log(f"⏰ 脚本异常: {e}", level="error")

        self.adb("shell settings put system pointer_location 0")

        try:
            from device_worker.screen_server import screen_server_instance
            if screen_server_instance:
                screen_server_instance.clear_stream_strategy(self.device_id)
        except Exception:
            pass

        result = await self._collect_output(output_dir, exit_code, stdout, stderr)
        await self._save_result(result["status"], result.get("error", ""), result)
        await self.log(f"{'✅' if result['status'] == 'pass' else '❌'} 脚本完成: {result.get('summary', '')}")

    def _generate_env_file(self, env_path, inputs, output_dir):
        platform_keys = {"AVAILABLE_DEVICES", "TASK_ID", "REQ_ID", "DEVICE_WORKER_URL", "DEVICE_TASK_OUTPUT_DIR", "APK_PATH"}
        existing_lines = []
        if os.path.exists(env_path):
            with open(env_path, "r") as f:
                for line in f:
                    key = line.split("=")[0].strip()
                    if key in platform_keys or "平台自动注入" in line:
                        continue
                    existing_lines.append(line.rstrip())

        platform_lines = [
            "# === 平台自动注入 ===",
            f"AVAILABLE_DEVICES={self.device_id}",
            f"TASK_ID={self.task_id}",
            f"REQ_ID={self.payload.get('req_id', '')}",
            f"DEVICE_WORKER_URL=http://127.0.0.1:5007",
            f"DEVICE_TASK_OUTPUT_DIR={output_dir}",
        ]
        apk_path = inputs.pop("apk_path", "") or self.payload.get("apk_path", "")
        if apk_path:
            platform_lines.append(f"APK_PATH={apk_path}")

        with open(env_path, "w") as f:
            for line in existing_lines:
                if line.strip():
                    f.write(line + "\n")
            f.write("\n" + "\n".join(platform_lines) + "\n")

    async def _stream_stdout(self, proc, lines_buf):
        try:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                text = line.decode(errors="replace").rstrip()
                lines_buf.append(text)
                await self.log(text)
        except asyncio.CancelledError:
            pass

    async def _monitor_loop(self, proc, output_dir, interval):
        monitor_dir = os.path.join(output_dir, "_monitor")
        os.makedirs(monitor_dir, exist_ok=True)
        idx = 0
        try:
            while proc.returncode is None:
                await asyncio.sleep(interval)
                if proc.returncode is not None:
                    break
                # 检查任务是否被取消
                task_doc = self.device_mgr.db[self.device_mgr.config["collections"]["task_queue"]].find_one(
                    {"task_id": self.task_id}, {"status": 1})
                if task_doc and task_doc.get("status") in (4, 5):
                    await self.log("🛑 任务已被取消", level="warning")
                    proc.kill()
                    break
                # 截图
                try:
                    screenshot = self.screenshot_bytes()
                    if screenshot:
                        path = os.path.join(monitor_dir, f"monitor_{idx:04d}.png")
                        with open(path, "wb") as f:
                            f.write(screenshot)
                        idx += 1
                except Exception:
                    pass
        except asyncio.CancelledError:
            pass

    async def _collect_output(self, output_dir, exit_code, stdout, stderr):
        result = {"status": "pass" if exit_code == 0 else "fail", "files": [], "summary": "", "exit_code": exit_code}

        json_path = os.path.join(output_dir, "result.json")
        if os.path.exists(json_path):
            try:
                with open(json_path) as f:
                    user_result = json.load(f)
                result["status"] = user_result.get("status", result["status"])
                result["summary"] = user_result.get("summary", "")
                result["cases"] = user_result.get("cases", [])
            except Exception:
                pass

        if not result["summary"]:
            result["summary"] = f"退出码: {exit_code}" + (f" | {stderr.decode()[:100]}" if stderr else "")

        if stdout:
            lines = stdout.decode(errors='replace').strip().split('\n')
            result["detail"] = '\n'.join(lines[-50:])

        for root, dirs, files in os.walk(output_dir):
            if "_monitor" in root:
                continue
            for fname in files:
                if fname == "result.json":
                    continue
                fpath = os.path.join(root, fname)
                rel_path = os.path.relpath(fpath, output_dir)
                ext = os.path.splitext(fname)[1].lower()
                if ext in ('.png', '.jpg', '.jpeg', '.gif', '.webp', '.html'):
                    local_url = self._upload_to_server(fpath, ext)
                    file_type = "image" if ext != '.html' else "report"
                    result["files"].append({"name": rel_path, "url": local_url, "type": file_type})
                else:
                    result["files"].append({"name": rel_path, "type": "file"})

        if stdout:
            result["stdout"] = stdout.decode(errors="replace")[:3000]
        if stderr:
            result["error"] = stderr.decode(errors="replace")[:1000]

        return result

    def _upload_to_server(self, file_path, ext):
        """保存文件到 /tmp/device_outputs/ 并返回本地路径"""
        dest_dir = "/tmp/device_outputs"
        os.makedirs(dest_dir, exist_ok=True)
        name = f"{uuid.uuid4().hex[:12]}{ext}"
        dest = os.path.join(dest_dir, name)
        shutil.copy2(file_path, dest)
        return dest

    async def _save_result(self, status, error="", result=None):
        db = self.device_mgr.db
        col = db[self.device_mgr.config["collections"]["task_results"]]
        doc = {
            "task_id": self.task_id, "device_id": self.device_id,
            "status": status, "summary": (result or {}).get("summary", ""),
            "detail": (result or {}).get("detail", ""), "error": error,
            "files": (result or {}).get("files", []),
            "created_at": int(time.time()),
        }
        col.replace_one({"task_id": self.task_id}, doc, upsert=True)

    def _resolve_script(self, script_id, source):
        if script_id:
            installed_dir = os.path.join(SCRIPT_INSTALL_DIR, script_id)
            meta_path = os.path.join(installed_dir, "script_meta.json")
            if os.path.exists(meta_path):
                with open(meta_path) as f:
                    meta = json.load(f)
                return os.path.join(installed_dir, meta.get("entry", "main.py"))
        if source and os.path.isfile(source):
            return source
        if source and (source.startswith("git@") or source.startswith("http")):
            parts = source.split("#", 1)
            git_url, script_file = parts[0], parts[1] if len(parts) > 1 else "main.py"
            clone_dir = f"/tmp/device_scripts/{self.task_id}"
            r = subprocess.run(f"git clone --depth 1 {git_url} {clone_dir}", shell=True, capture_output=True, timeout=60)
            if r.returncode == 0:
                return os.path.join(clone_dir, script_file)
        return None
