"""
app.py

Flask application factory. Keeps app creation separate from route
definitions (routes.py) so the app can be imported and tested without
side effects.
"""

from flask import Flask


def create_app() -> Flask:
    app = Flask(
        __name__,
        static_folder="../client/static",
        static_url_path="/static",
    )

    from .routes import register_routes
    register_routes(app)

    return app
