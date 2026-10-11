"""Publish a validated runner catalog to the website's offline asset directory."""
import json
import os
import shutil

from ournotes.catalogs.cards import sha256
from ournotes.catalogs.runtime import current_catalog
from ournotes.common.files import atomic_json
from ournotes.paths import DATA_ROOT as ROOT


def main():
    source, target = current_catalog(), ROOT / 'data/catalog/site'
    data = json.loads((source / 'catalog.json').read_text('utf-8'))
    for card in data['cards']:
        path = (source / card['image']).resolve()
        destination = (target / card['image']).resolve()
        if (not path.is_relative_to(source.resolve()) or not destination.is_relative_to(target.resolve())
                or sha256(path) != card['sha256']):
            raise ValueError('Invalid catalog artwork')
        if not destination.exists() or sha256(destination) != card['sha256']:
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_suffix(destination.suffix + '.tmp')
            temporary.unlink(missing_ok=True)
            try:
                os.link(path, temporary)
            except OSError:
                shutil.copy2(path, temporary)
            temporary.replace(destination)
    for name in ('MasterMemberCard', 'MasterSupportCard', 'MasterCharacter', 'MasterText'):
        atomic_json(target / 'raw' / (name + '.json'), json.loads((source / 'raw' / (name + '.json')).read_text('utf-8')))
    atomic_json(target / 'catalog.json', data)
    print(json.dumps({'cards': len(data['cards']), 'SSR': sum(c['rarity'] == 'SSR' for c in data['cards'])}))


if __name__ == '__main__':
    main()
