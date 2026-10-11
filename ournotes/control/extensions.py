"""Capabilities shared by source checkouts and installed clients."""
from ournotes.proxy.ui.web import Extension as Clash
from ournotes.rebind.web import Extension as EmailRebind


class Extension:
    def __init__(self, root):
        self.rebind = EmailRebind(root)
        self.clash = Clash(root)
        self.extensions = (self.rebind, self.clash)

    def decorate(self, html):
        for extension in self.extensions:
            html = extension.decorate(html)
        return html

    def mount(self, app):
        for extension in self.extensions:
            extension.mount(app)

    def status(self):
        return {'email_rebind': self.rebind.service.status(), 'clash': self.clash.status()}

    def close(self):
        for extension in reversed(self.extensions):
            extension.close()
