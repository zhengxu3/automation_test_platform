from flask import Flask
from .index_page import index_page_bp


def create_app():
    app = Flask(__name__)

    # 注册蓝图
    app.register_blueprint(index_page_bp)

    return app
