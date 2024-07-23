#!/usr/bin/env python3.9
# -*- coding: utf-8 -*-
"""
@Author: Zheng.Xu
@Date: 2024/7/15
@Python：python 3.9
"""

from flask import Flask
from .callback import call_back_bp


def create_app():
    app = Flask(__name__)

    # 注册蓝图
    app.register_blueprint(call_back_bp)

    return app