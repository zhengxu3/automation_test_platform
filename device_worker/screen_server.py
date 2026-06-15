"""实时推流 + 任务消息推送 WebSocket"""
import asyncio
import base64
import json
import time
import websockets
from collections import defaultdict


class ScreenServer:
    def __init__(self, config):
        self.fps_interval = config["device"]["screen_fps"]
        self.device_viewers = defaultdict(set)
        self.task_viewers = defaultdict(set)
        self.tap_markers = {}
        self.device_locked = set()
        self.device_strategy = {}

    def set_stream_strategy(self, device_id, strategy):
        self.device_strategy[device_id] = strategy
        if strategy == "pause":
            self.device_locked.add(device_id)
        else:
            self.device_locked.discard(device_id)

    def clear_stream_strategy(self, device_id):
        self.device_strategy.pop(device_id, None)
        self.device_locked.discard(device_id)

    def lock_device(self, device_id):
        self.device_locked.add(device_id)

    def unlock_device(self, device_id):
        self.device_locked.discard(device_id)

    async def handler(self, websocket):
        try:
            path = websocket.request.path
        except AttributeError:
            path = websocket.path
        parts = path.strip("/").split("/")
        if len(parts) < 2:
            return

        channel_type = parts[0]
        channel_id = parts[1]

        if channel_type == "screen":
            await self._handle_device_viewer(websocket, channel_id)
        elif channel_type == "task":
            await self._handle_task_viewer(websocket, channel_id)

    async def _handle_device_viewer(self, ws, device_id):
        self.device_viewers[device_id].add(ws)
        try:
            if len(self.device_viewers[device_id]) == 1:
                asyncio.create_task(self._stream_device(device_id))
            async for _ in ws:
                pass
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            self.device_viewers[device_id].discard(ws)

    async def _handle_task_viewer(self, ws, task_id):
        self.task_viewers[task_id].add(ws)
        try:
            async for msg in ws:
                try:
                    data = json.loads(msg)
                    if data.get("action") == "resume":
                        self._resume_queue.append(data)
                except:
                    pass
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            self.task_viewers[task_id].discard(ws)

    # ==================== 推送接口 ====================

    async def push(self, task_id, data):
        if not self.task_viewers[task_id]:
            return
        payload = json.dumps(data, ensure_ascii=False)
        dead = set()
        for ws in self.task_viewers[task_id]:
            try:
                await ws.send(payload)
            except:
                dead.add(ws)
        self.task_viewers[task_id] -= dead

    async def push_frame(self, task_id, device_id, frame_b64):
        await self.push(task_id, {"type": "frame", "device_id": device_id, "image": frame_b64, "ts": time.time()})

    def report_tap_sync(self, device_id, x, y):
        self.tap_markers[device_id] = {"x": x, "y": y, "ts": time.time()}

    def report_tap(self, device_id, x, y):
        self.tap_markers[device_id] = {"x": x, "y": y, "ts": time.time()}

    # ==================== 设备画面推流 ====================

    async def _stream_device(self, device_id):
        fail_count = 0
        while self.device_viewers[device_id]:
            if device_id in self.device_locked:
                await asyncio.sleep(self.fps_interval * 2)
                continue
            frame = await self._capture(device_id)
            if frame:
                fail_count = 0
                payload = json.dumps({"type": "frame", "device_id": device_id, "image": frame, "ts": time.time()})
                dead = set()
                for ws in self.device_viewers[device_id]:
                    try:
                        await ws.send(payload)
                    except:
                        dead.add(ws)
                self.device_viewers[device_id] -= dead
            else:
                fail_count += 1
                if fail_count >= 5:
                    for ws in list(self.device_viewers[device_id]):
                        try:
                            await ws.send(json.dumps({"type": "offline", "device_id": device_id}))
                        except:
                            pass
                    await asyncio.sleep(5)
                    fail_count = 0
                    continue
            active_streams = sum(1 for v in self.device_viewers.values() if v)
            strategy = self.device_strategy.get(device_id, "normal")
            if strategy == "low_fps":
                interval = self.fps_interval * 3
            elif strategy == "low_quality":
                interval = self.fps_interval * 2
            else:
                interval = self.fps_interval * (1.5 if active_streams > 2 else 1)
            await asyncio.sleep(interval)

    async def _capture(self, device_id):
        try:
            proc = await asyncio.create_subprocess_shell(
                f"/opt/homebrew/Caskroom/android-platform-tools/37.0.0/platform-tools/adb -s {device_id} exec-out screencap -p",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=3)
            if not stdout:
                return None
            marker = self.tap_markers.get(device_id)
            if marker and time.time() - marker["ts"] < 2:
                stdout = self._draw_marker(stdout, marker["x"], marker["y"])
            return base64.b64encode(stdout).decode()
        except:
            return None

    def _draw_marker(self, png_bytes, x, y):
        try:
            from PIL import Image, ImageDraw
            import io
            img = Image.open(io.BytesIO(png_bytes))
            draw = ImageDraw.Draw(img)
            r = 25
            draw.ellipse([x-r, y-r, x+r, y+r], outline="red", width=4)
            draw.line([x-r, y, x+r, y], fill="red", width=2)
            draw.line([x, y-r, x, y+r], fill="red", width=2)
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return buf.getvalue()
        except:
            return png_bytes

    # ==================== 启动 ====================

    async def start(self):
        self._resume_queue = []
        port = 5008
        await websockets.serve(self.handler, "0.0.0.0", port)
        print(f"📺 WS 服务: 0.0.0.0:{port} (/screen/{{device_id}} | /task/{{task_id}})")


# 全局实例（由 main.py 启动时赋值）
screen_server_instance = None
