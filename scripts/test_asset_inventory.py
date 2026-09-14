import json
import struct
from pathlib import Path
import tempfile
import unittest

from asset_inventory import approved_ids, gltf_dependencies, group_key, inventory


class InventoryTests(unittest.TestCase):
    def test_published_animation_is_reused_as_animation_without_assuming_model_upload(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pack = root / 'unimported/animations'
            pack.mkdir(parents=True)
            (pack / 'Blocking.rbxmx').write_text('<roblox/>')
            doc = root / 'assets.md'
            doc.write_text('## animations\n| `Blocking` | `123` |')
            asset = inventory(root / 'unimported', doc)['asset_groups'][0]
            self.assertEqual((asset['status'], asset['asset_type'], asset['asset_id']), ('reused', 'Animation', '123'))

    def test_static_queue_excludes_skinned_and_animation_glb(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pack = root / 'unimported' / 'Characters'
            pack.mkdir(parents=True)
            (pack / 'License.txt').write_text('CC0')
            (pack / 'weapon.gltf').write_text('{"meshes":[{}],"nodes":[{}]}')
            data = json.dumps({'meshes': [{}], 'nodes': [{'name': 'hips'}],
                               'skins': [{'joints': [0]}], 'animations': [{}]}).encode()
            data += b' ' * (-len(data) % 4)
            (pack / 'rig.glb').write_bytes(struct.pack('<4sIIII', b'glTF', 2, len(data) + 20, len(data), 0x4e4f534a) + data)
            (pack / 'broken.glb').write_bytes(b'glTF')
            doc = root / 'assets.md'
            doc.write_text('## Empty')
            assets = {a['name']: a for a in inventory(root / 'unimported', doc)['asset_groups']}
            self.assertEqual(assets['weapon']['status'], 'queued')
            self.assertEqual(assets['rig']['status'], 'blocked')
            self.assertEqual(assets['rig']['structure']['bones'], ['hips'])
            self.assertEqual(assets['broken']['status'], 'blocked')

    def test_document_ids_are_pack_scoped_and_preserve_alternates(self):
        ids = approved_ids('## One\n| `axe` | `123` (alt: `456`) |\n## Two\n| `axe` | `789` |')
        self.assertEqual(ids['One', 'axe'], {'asset_id': '123', 'alternate_ids': ['456']})
        self.assertEqual(ids['Two', 'axe']['asset_id'], '789')

    def test_export_folders_group_but_independent_assets_do_not(self):
        expected = 'Pack/Assets/axe'
        for folder in ('fbx', 'fbx(unity)', 'gltf', 'obj'):
            self.assertEqual(group_key(Path(f'Pack/Assets/{folder}/axe.fbx')), expected)
        self.assertNotEqual(group_key(Path('Pack/Animation/axe.fbx')), expected)

    def test_discovery_reuses_approved_and_fingerprints_dependencies(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'unimported'
            pack = source / 'Weapons'
            gltf = pack / 'Assets/gltf/axe_A.gltf'
            gltf.parent.mkdir(parents=True)
            gltf.write_text(json.dumps({'buffers': [{'uri': 'axe_A.bin'}], 'nodes': [{'name': 'axe_A_handle'}]}))
            dependency = gltf.with_suffix('.bin')
            dependency.write_bytes(b'first geometry')
            (pack / 'License.txt').write_text('License: CC0')
            fbx = pack / 'Assets/fbx(unity)/axe_A.fbx'
            fbx.parent.mkdir(parents=True)
            fbx.write_bytes(b'alternative export')
            doc = root / 'assets.md'
            doc.write_text('## Weapons\n| `axe_A` | `12345` |')
            first = inventory(source, doc)
            self.assertEqual(first, inventory(source, doc), 'discovery must be deterministic')
            self.assertEqual(first['summary']['model_groups'], 1)
            asset = first['asset_groups'][0]
            self.assertEqual((asset['status'], asset['asset_id']), ('reused', '12345'))
            self.assertTrue(any(f['reason'].startswith('Alternate export') for f in first['files']))
            dependency.write_bytes(b'changed geometry')
            second = inventory(source, doc)
            self.assertNotEqual(asset['fingerprint'], second['asset_groups'][0]['fingerprint'])
            self.assertEqual(second['asset_groups'][0]['asset_id'], '12345')
            obj = pack / 'Assets/obj/axe_A_handle.obj'
            obj.parent.mkdir(parents=True)
            obj.write_text('o axe_A_handle')
            third = inventory(source, doc)
            component = next(a for a in third['asset_groups'] if a['name'] == 'axe_A_handle')
            self.assertEqual((component['status'], component['asset_id'], component['parent_model']), ('excluded', '12345', 'axe_A'))

    def test_dependency_paths_cannot_escape_or_fetch_remotes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pack = root / 'pack'
            pack.mkdir()
            model = pack / 'model.gltf'
            (root / 'secret.bin').write_bytes(b'private')
            for uri in ('../secret.bin', '..%2Fsecret.bin', 'https://example.com/file.bin', 'missing.bin'):
                model.write_text(json.dumps({'buffers': [{'uri': uri}]}))
                dependencies, error = gltf_dependencies(model, pack)
                self.assertEqual(dependencies, [])
                self.assertIsNotNone(error)

    def test_unknown_license_is_blocked_and_every_source_has_status(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'unimported'
            pack = source / 'UnknownPack'
            pack.mkdir(parents=True)
            (pack / 'model.fbx').write_bytes(b'model')
            (pack / 'picture.png').write_bytes(b'picture')
            (pack / 'readme.txt').write_text('No license')
            doc = root / 'assets.md'
            doc.write_text('## Empty')
            result = inventory(source, doc)
            self.assertEqual(result['summary']['file_count'], 3)
            self.assertEqual(result['asset_groups'][0]['status'], 'blocked')
            self.assertTrue(all(f['status'] and f['reason'] and f['sha256'] for f in result['files']))


if __name__ == '__main__':
    unittest.main()
