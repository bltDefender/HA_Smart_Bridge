class ActionError(Exception):
    """Aktion fehlgeschlagen (Backend-Fehler, Tool-Fehler, Timeout)."""


class UnsupportedActionError(ActionError):
    """Backend kann die Aktion nicht ausführen; nächstes Backend versuchen."""
