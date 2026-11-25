"""
Base contract every client task implements.

To add a new client/task:
  1. Create clients/<client_slug>/<task_slug>/task.py
  2. Subclass BaseTask, implement process(files)
  3. Build a Flask Blueprint in the same file (see clients/sabic/outbound/task.py
     or clients/carpenter/inbound/task.py for the reference pattern)
  4. Register the blueprint in clients/__init__.py's TASK_REGISTRY
"""

import traceback
from abc import ABC, abstractmethod


def safe_validate(validate_fn, *args, **kwargs) -> list[str]:
    """Run a task's cross-document validate(): a check is a warning list for the user, so a bug inside one (an
    extraction shape it did not expect) must never stop the upload. On a crash the output is still built and the
    crash becomes one warning line (the traceback goes to the console for the developer)."""
    try:
        return list(validate_fn(*args, **kwargs) or [])
    except Exception as e:                                   # noqa: BLE001 — see above
        traceback.print_exc()
        return [f"[!]  The cross-document check could not finish ({type(e).__name__}: {e}) — the output was still "
                f"built; compare the references / totals with the documents by hand and report this to the developer."]


class BaseTask(ABC):
    client_slug: str = ""
    task_slug: str = ""
    label: str = ""

    # [{"key": "order_sheet", "label": "Order Sheet (Excel)", "accept": ".xlsx,.xls", "multiple": False}, ...]
    required_documents: list[dict] = []

    # For helpers/excel_writer.py: [{"header": str, "field_key": str, "width": int, "num_format": str?}, ...]
    column_config: list[dict] = []

    # Most tasks (e.g. Sabic) just return row dicts and let the blueprint route
    # write the Excel generically via helpers/excel_writer.write_excel. A task
    # with its own bespoke output format (e.g. Carpenter, which merges 3 document
    # types and writes a custom-styled report) sets this True and writes the file
    # itself inside process() instead of returning "rows".
    writes_own_output: bool = False

    @abstractmethod
    def process(self, files: dict, output_path: str | None = None) -> dict:
        """
        files: {doc_key: path} or {doc_key: [paths]} for documents marked "multiple".
        output_path: only passed (and required) when writes_own_output is True.
        Returns: {"rows": list[dict], "summary": dict}
        """
        raise NotImplementedError
