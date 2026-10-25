class UserError(Exception):
    """Something the user can fix. The message is shown to them as-is."""


class NotFound(UserError):
    pass
