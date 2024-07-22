#!/usr/bin/env python3.9
# -*- coding: utf-8 -*-
"""
@Author: Zheng.Xu
@Date: 2024/7/19
@Python：python 3.9
"""
from flask import jsonify


def make_response(data=None, message="Success", status=200):
    response = {
        "status": status,
        "message": message,
        "data": data
    }
    return jsonify(response), status