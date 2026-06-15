"""统一响应格式"""
from flask import jsonify


def ok(data=None, message="success"):
    return jsonify({"result_code": 200, "data": data or {}, "message": message})


def err(message, code=400):
    return jsonify({"result_code": code, "data": {}, "message": message})
