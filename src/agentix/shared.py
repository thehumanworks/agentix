import modal

_app: modal.App | None = None


def get_app():
    global _app
    if _app is None:
        _app = modal.App.lookup(name="agentix", create_if_missing=True)
    return _app
