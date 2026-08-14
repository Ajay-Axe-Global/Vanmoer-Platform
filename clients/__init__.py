"""
TASK_REGISTRY — single source of truth for every client task blueprint.

To add a new client/task:
  1. Create clients/<client_slug>/<task_slug>/task.py (BaseTask subclass + Blueprint `bp`)
  2. Import its `bp` here and append it to TASK_REGISTRY
That's it — main.py and routes/__init__.py never need to change.
"""
from clients.sabic.inbound.task import bp as sabic_inbound_bp
from clients.carpenter.inbound.task import bp as carpenter_inbound_bp
from clients.carpenter.outbound.task import bp as carpenter_outbound_bp
from clients.sabic.outbound.task import bp as sabic_outbound_bp

TASK_REGISTRY = [
    sabic_outbound_bp,
    sabic_inbound_bp,
    carpenter_inbound_bp,
    carpenter_outbound_bp,
]
