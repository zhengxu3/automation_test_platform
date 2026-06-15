"""启动入口"""
from gateway.app import create_app

app = create_app()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5010, debug=True)
