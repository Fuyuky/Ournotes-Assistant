"""Refresh the runner's BDON snapshot without modifying the account website assets."""
import argparse
import json

from ournotes.catalogs.runtime import sync_catalog


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--workers', type=int, default=3)
    args = parser.parse_args()
    folder = sync_catalog(workers=args.workers, max_age=0)
    cards = json.loads((folder / 'catalog.json').read_text(encoding='utf-8'))
    pools = json.loads((folder / 'pools.json').read_text(encoding='utf-8'))
    print(json.dumps({'snapshot': str(folder), 'cards': len(cards['cards']),
                      'pools': len(pools['pools'])}, ensure_ascii=False))


if __name__ == '__main__':
    main()
