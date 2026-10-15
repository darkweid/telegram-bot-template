from pydantic import BaseModel

NOTIFICATION_TEMPLATE = "notification.html"


class NotificationEmail(BaseModel):
    """The context of `notification.html`. `message` is plain text: the
    template escapes it and turns line breaks into <br>. An empty `logo_url`
    leaves the logo row out."""

    title: str
    message: str
    logo_url: str = ""
