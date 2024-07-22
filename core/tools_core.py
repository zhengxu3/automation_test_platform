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


def make_response(data=None, message="Success", status=200):
    response = {
        "status": status,
        "message": message,
        "data": data
    }
    return jsonify(response), status


def setup_logging(log_folder='logs'):
    """
    配置全局 logging
    """

    log_folder_path = os.path.join(os.path.dirname(__file__), log_folder)  # 在当前文件夹下创建日志文件夹
    if not os.path.exists(log_folder_path):
        os.makedirs(log_folder_path)

    # 获取当前日期
    current_date = datetime.now().strftime('%Y-%m-%d')

    # 设置日志文件路径
    log_file_path = os.path.join(log_folder, f'app_{current_date}.log')

    # 配置 logging
    logging.basicConfig(
        level=logging.INFO,  # 可以设置为 DEBUG, INFO, WARNING, ERROR, CRITICAL
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.StreamHandler(),  # 将日志输出到标准输出
            logging.FileHandler(log_file_path)  # 将日志输出到文件
        ]
    )
    logging.info("Logging is set up.")
