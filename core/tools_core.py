#!/usr/bin/env python3.9
# -*- coding: utf-8 -*-
"""
@Author: Zheng.Xu
@Date: 2024/7/19
@Python：python 3.9
"""
import os
from datetime import datetime

from flask import jsonify
import logging
import requests


def make_response(data=None, message="Success", status=200):
    response = {
        "status": status,
        "message": message,
        "data": data
    }
    return jsonify(response), status

def send_request(url,data):
    try:
        r = response = requests.post(url, data=data)
    except Exception as e:
        return ''

def setup_logging(log_folder='logs'):
    # 确保日志目录存在
    os.makedirs(log_folder, exist_ok=True)

    # 创建日志文件名，使用当前日期
    log_filename = os.path.join(log_folder, 'app_%Y-%m-%d.log')

    # 创建日志器
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)

    # 创建文件处理器
    file_handler = logging.FileHandler(log_filename)
    file_handler.setLevel(logging.INFO)

    # 创建格式化器并添加到处理器
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    file_handler.setFormatter(formatter)

    # 添加处理器到日志器
    logger.addHandler(file_handler)

    # 创建控制台处理器（可选）
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)