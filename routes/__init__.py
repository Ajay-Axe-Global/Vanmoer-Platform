from clients import TASK_REGISTRY
from routes.admin_routes import bp as admin_bp
from routes.auth_routes import bp as auth_bp


def register_all(app):
    """The only place that 'connects the API in Main' — main.py never changes
    when a new client task is added, it just calls this. New tasks show up
    automatically because they're appended to clients.TASK_REGISTRY."""
    app.register_blueprint(auth_bp)
    app.register_blueprint(admin_bp)
    for blueprint in TASK_REGISTRY:
        app.register_blueprint(blueprint)
