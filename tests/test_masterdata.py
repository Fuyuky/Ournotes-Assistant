import hashlib
import io
import json
import zipfile

import pytest

from ournotes.catalogs.masterdata import read_archive


def archive_bytes(*, region='hk-tw-mo', digest=None, rows=None):
    manifest = b'{"version":"hk-version"}'
    version = {'regions': {'hk-tw-mo': {
        'data_path': region, 'snapshot_path': region, 'version': 'hk-version',
        'resource_version': '1.0.0.400', 'client_version': '1.0.3',
        'manifest_sha256': digest or hashlib.sha256(manifest).hexdigest()}}}
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as archive:
        archive.comment = b'a' * 40
        prefix = 'moenotes-masterdata-main/'
        archive.writestr(prefix + 'current_version.json', json.dumps(version))
        archive.writestr(prefix + 'hk-tw-mo/MasterManifest.json', manifest)
        archive.writestr(prefix + 'hk-tw-mo/MasterGacha.json', json.dumps(
            [{'_id': 12, '_nameTextId': 'HK'}] if rows is None else rows))
        archive.writestr(prefix + 'jp/MasterGacha.json', '[{"id":12,"nameTextId":"JP"}]')
    return stream.getvalue()


def test_archive_pins_commit_and_region_even_with_other_servers_present():
    source, tables = read_archive(archive_bytes(), ['MasterGacha'])
    assert source['region'] == 'hk-tw-mo'
    assert source['commit'] == 'a' * 40
    assert source['resource_version'] == '1.0.0.400'
    assert tables['MasterGacha'][0]['_nameTextId'] == 'HK'


@pytest.mark.parametrize('options', [
    {'region': 'jp'}, {'digest': '0' * 64}, {'rows': []},
    {'rows': [{'id': 12}, {'id': 12}]}, {'rows': [{'name': 'missing identity'}]},
])
def test_invalid_or_mixed_masterdata_is_rejected(options):
    with pytest.raises(ValueError):
        read_archive(archive_bytes(**options), ['MasterGacha'])
