from flask import Flask


def create_app():
    app = Flask(__name__)

    from app.routes.breakdown import breakdown_bp
    app.register_blueprint(breakdown_bp)

    return app
