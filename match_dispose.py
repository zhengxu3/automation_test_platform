#!/usr/bin/env python3.9
# -*- coding: utf-8 -*-
"""
@Author: Zheng.Xu
@Date: 2024/7/18
@Python：python 3.9
"""
import random
import time
import threading
from queue import Queue
from concurrent.futures import ThreadPoolExecutor, as_completed
from mongoengine import Document, BooleanField, StringField, EmailField, DateTimeField
from app import create_app
from models.task_m import TaskM
# 创建 Flask 应用
app = create_app()

# 模拟任务接口的队列
task_queue = Queue()


# 添加任务到队列中（模拟接口返回的任务）
def add_tasks_to_queue():
    while True:
        # 模拟每隔10秒检查一次接口
        time.sleep(10)
        # 模拟接口返回新任务
        for i in range(3):  # 假设每次检查接口返回3个任务
            task_queue.put(f"Task-{i + 1}")


# Token处理函数
def process_token(token):
    print(f"Processing token {token}")
    time.sleep(2)  # 模拟处理时间
    return f"Result from {token}"


# 主任务线程函数
def process_task(task):
    print(f"Starting {task}")
    # 假设每个任务有多个token
    tokens = [f"{task}-token-{i + 1}" for i in range(5)]

    # 创建token线程池
    token_results = []
    with ThreadPoolExecutor(max_workers=len(tokens)) as token_executor:
        futures = [token_executor.submit(process_token, token) for token in tokens]
        for future in as_completed(futures):
            token_results.append(future.result())

    print(f"{task} completed with results: {token_results}")


# 主函数
def main():
    # 启动任务添加线程
    task_adder_thread = threading.Thread(target=add_tasks_to_queue)
    task_adder_thread.start()

    # 创建一个管理主任务线程的线程池，最多10个线程
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = []

        while True:
            # 检查任务队列是否有新任务
            if not task_queue.empty():
                while len(futures) < 10 and not task_queue.empty():
                    task = task_queue.get()
                    futures.append(executor.submit(process_task, task))

            # 移除已完成的任务
            futures = [f for f in futures if not f.done()]

            # 短暂休眠，避免CPU占用过高
            time.sleep(0.1)


if __name__ == "__main__":
    # 查询所有 status 为 'Pending' 的任务
    pending_tasks = TaskM.get_pending_tasks()
    print(f'Pending tasks:')
    for task in pending_tasks:
        print(f'Task ID: {task.id}, Task Name: {task.task_name}, Status: {task.status}')