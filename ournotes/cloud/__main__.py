"""Standalone cloud process. Run exactly one worker against a server-local database."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from ournotes.cloud.api import create_app
from ournotes.cloud.mail import GmailBackend, MailRule, SecretVault
from ournotes.cloud.process import process_lease
from ournotes.cloud.publication import Publisher
from ournotes.cloud.store import Store


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--backup", type=Path)
    args = parser.parse_args()
    config = json.loads(args.config.read_text("utf-8"))
    base = args.config.resolve().parent

    def path(key):
        value = Path(config[key])
        return value if value.is_absolute() else base / value

    if args.backup:
        store = Store(path("database"), config["mailbox"])
        store.backup(args.backup)
        return
    with process_lease(path("database")):
        store = Store(path("database"), config["mailbox"])
        serve(config, path, store)


def serve(config, path, store):
    administrator = path("admin_token_file").read_text("utf-8").strip()
    backend, rule = None, None
    if config.get("gmail_proxy"):
        backend = GmailBackend(SecretVault(path("gmail_credentials")), config["gmail_proxy"],
                               config["mailbox"], store.clock)
        if config.get("mail_rule"):
            value = json.loads(path("mail_rule").read_text("utf-8"))
            rule = MailRule(tuple(value["senders"]), value["subject_pattern"], value["code_pattern"])
    publisher = Publisher(store, path("publication_root"), path("catalog"))
    portal_credential = (json.loads(path('portal_credential_file').read_text('utf-8'))
                         if config.get('portal_credential_file') else None)
    app = create_app(store, administrator, backend=backend, rule=rule, publisher=publisher,
                     catalog=path("catalog"), rebind_nodes=config.get("rebind_nodes", []),
                     portal_credential=portal_credential,
                     activity_catalog=path('activity_catalog') if config.get('activity_catalog') else None)
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=config.get("port", 8770), workers=1, access_log=False)


if __name__ == "__main__":
    main()
