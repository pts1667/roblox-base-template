import base64
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from asset_import import (ApiFailure, AssetsAPI, accept_operation, entry_for, journal_lock,
                          existing_name_candidates, inspect_creator_inventory, metadata_result, payload, plan,
                          record_verification, resume_one, retry_rejected, upload_one, write_json)
from asset_inventory import inventory
import asset_import


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.pack = self.root / "unimported/Resources"
        self.pack.mkdir(parents=True)
        (self.pack / "License.txt").write_text("CC0", encoding="utf-8")
        (self.pack / "stone.bin").write_bytes(b"geometry")
        (self.pack / "stone.png").write_bytes(b"texture")
        self.source = self.pack / "stone.gltf"
        self.source.write_text(json.dumps({"asset": {"version": "2.0"},
            "buffers": [{"uri": "stone.bin"}], "images": [{"uri": "stone.png"}]}))
        document = self.root / "approved.md"
        document.write_text("")
        self.asset = inventory(self.root / "unimported", document)["asset_groups"][0]
        self.destination = {"creator": {"userId": "123"}}
        self.entry = entry_for(self.asset, self.destination)
        self.snapshots = []

    def save(self):
        self.snapshots.append(copy.deepcopy(self.entry))

    def metadata(self):
        return {"assetId": "456", "assetType": "Model", "displayName": "stone",
                "description": "SHA-256: " + self.entry["fingerprint"],
                "creationContext": {"creator": {"userId": 123}},
                "moderationResult": {"moderationState": "Approved"}}

    def operation(self):
        return {"path": "operations/abc", "done": True, "response": self.metadata()}

    def test_bundle_and_stale_source_or_license_rejected(self):
        content, mime = payload(self.entry, self.root)
        model = json.loads(content)
        self.assertEqual(mime, "model/gltf+json")
        self.assertEqual(base64.b64decode(model["buffers"][0]["uri"].split(",")[1]), b"geometry")
        self.assertTrue(model["images"][0]["uri"].startswith("data:image/png;base64,"))
        (self.pack / "stone.bin").write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "fingerprint changed"):
            payload(self.entry, self.root)
        (self.pack / "License.txt").write_text("Changed terms")
        with self.assertRaisesRegex(ValueError, "License evidence"):
            payload(self.entry, self.root)

    def test_inventory_follows_empty_pages_and_blocks_undocumented_name_matches(self):
        calls = []

        class API:
            def inventory_page(self, user_id, token):
                calls.append((user_id, token))
                if token is None:
                    return {'nextPageToken': 'second'}
                return {'inventoryItems': [
                    {'assetDetails': {'assetId': '456', 'inventoryItemAssetType': 'MODEL'}},
                    {'assetDetails': {'assetId': '789', 'inventoryItemAssetType': 'MODEL'}}]}

            def get_asset(self, asset_id):
                if asset_id != '789':
                    raise AssertionError('Documented identity should use its name hints')
                return {'assetType': 'Model', 'displayName': ' STONE ', 'creationContext': {'creator': {'userId': '123'}}}

        audit = inspect_creator_inventory(API(), self.destination, '## Pack\n| `different` | `456` |')
        self.assertEqual(calls, [('123', None), ('123', 'second')])
        self.assertEqual(existing_name_candidates(self.entry, audit), ['789'])
        self.assertEqual(existing_name_candidates({**self.entry, 'name': 'new'}, audit), [])
        with self.assertRaisesRegex(ValueError, 'Complete inventory'):
            existing_name_candidates(self.entry, {**audit, 'complete': False})
        with self.assertRaisesRegex(ValueError, 'Complete inventory'):
            existing_name_candidates(self.entry, {**audit, 'creator': {'userId': '999'}})
        row = plan([self.asset], {'entries': {}, 'creator_inventory': audit}, self.destination)[0]
        self.assertEqual((row['action'], row['existing_asset_candidates']), ('blocked', ['789']))

    def test_inventory_refuses_incomplete_or_repeated_pagination(self):
        class API:
            def inventory_page(self, user_id, token):
                return {'nextPageToken': 'same'}

        with self.assertRaisesRegex(ValueError, 'Repeated inventory cursor'):
            inspect_creator_inventory(API(), self.destination, '')
        with patch.object(API, 'inventory_page', return_value={'inventoryItems': [{'assetDetails': {'assetId': '12'}}]}):
            with self.assertRaisesRegex(ValueError, 'Unexpected inventory item'):
                inspect_creator_inventory(API(), self.destination, '')
        with patch.object(API, 'inventory_page', side_effect=ApiFailure('HTTP 403', 403)):
            with self.assertRaises(ApiFailure):
                inspect_creator_inventory(API(), self.destination, '')
        api = AssetsAPI('test-key')
        with patch.object(api, '_json_request', return_value={}) as request:
            api.inventory_page('123', 'token&other=true')
        self.assertIn('pageToken=token%26other%3Dtrue', request.call_args.args[0])
        with self.assertRaises(ValueError):
            api.inventory_page('../other')

    def test_upload_command_reconciles_before_any_create(self):
        docs = self.root / 'docs'
        docs.mkdir()
        (docs / 'ROBLOX_ASSETS.md').write_text('')
        destination = self.root / 'destination.json'
        write_json(destination, {**self.destination, 'verification_asset_id': '456'})
        journal = self.root / 'journal.json'
        api = Mock()
        api.get_asset.return_value = self.metadata()
        api.inventory_page.return_value = {'inventoryItems': [
            {'assetDetails': {'assetId': '456', 'inventoryItemAssetType': 'MODEL'}}]}
        args = ['asset_import.py', 'upload', '--destination', str(destination), '--journal', str(journal), '--wait', '0']
        with patch('sys.argv', args), patch.object(asset_import, 'ROOT', self.root), \
                patch.object(asset_import, 'credential', return_value='test-key'), \
                patch.object(asset_import, 'AssetsAPI', return_value=api), \
                patch.object(asset_import, 'inventory', return_value={'asset_groups': [self.asset]}), \
                patch('builtins.print'):
            self.assertEqual(asset_import.main(), 0)
        api.create.assert_not_called()
        saved = json.loads(journal.read_text())
        self.assertTrue(saved['creator_inventory']['complete'])
        self.assertEqual(plan([self.asset], saved, self.destination)[0]['action'], 'blocked')

    def test_crash_intent_and_pending_operation_are_saved_before_further_work(self):
        owner = self

        class API:
            def create(self, entry, content, mime):
                owner.assertEqual(owner.snapshots[-1]["state"], "uploading")
                return {"path": "operations/abc", "done": False}

            def get_operation(self, path):
                owner.assertEqual(owner.snapshots[-1]["operation"], path)
                return owner.operation()

        api = API()
        self.assertTrue(upload_one(self.entry, api, self.save, self.root))
        self.assertEqual(self.entry["state"], "pending")
        resume_one(self.entry, api, self.save)
        self.assertEqual((self.entry["state"], self.entry["asset_id"]), ("uploaded", "456"))
        self.assertFalse(upload_one(self.entry, api, self.save, self.root))
        self.entry = entry_for(self.asset, self.destination)
        self.entry["state"] = "uploading"
        resume_one(self.entry, api, self.save)
        self.assertEqual(self.entry["state"], "uncertain")
        self.assertFalse(upload_one(self.entry, api, self.save, self.root))

    def test_ambiguous_creation_never_retries_but_poll_failures_remain_pending(self):
        class API:
            def create(self, *args):
                raise ApiFailure("No response")

            def get_operation(self, *args):
                raise ApiFailure("HTTP 503", 503)

        api = API()
        upload_one(self.entry, api, self.save, self.root)
        self.assertEqual(self.entry["state"], "uncertain")
        self.assertFalse(upload_one(self.entry, api, self.save, self.root))
        self.entry.update(operation="operations/abc", state="pending")
        resume_one(self.entry, api, self.save)
        self.assertEqual((self.entry["state"], self.entry["operation"]), ("pending", "operations/abc"))
        self.assertIn("poll_error", self.entry)

    def test_permissions_moderation_and_wrong_creator_are_distinct(self):
        class API:
            def create(self, *args):
                raise ApiFailure("HTTP 403", 403)

        upload_one(self.entry, API(), self.save, self.root)
        self.assertEqual((self.entry["state"], self.entry["http_status"]), ("blocked", 403))
        response = self.operation()
        response["response"]["creationContext"]["creator"]["userId"] = 789
        with self.assertRaisesRegex(ValueError, "creator differs"):
            accept_operation(self.entry, response)
        response = self.operation()
        response["response"]["moderationResult"]["moderationState"] = "MODERATION_STATE_REJECTED"
        accept_operation(self.entry, response)
        self.assertEqual((self.entry["state"], self.entry["asset_id"]), ("blocked", "456"))
        self.assertIn("moderation", self.entry["error"])
        changed = self.metadata()
        changed["description"] = "unrelated source"
        with self.assertRaisesRegex(ValueError, "source marker"):
            metadata_result(self.entry, changed, reconciliation=True)

    def test_recorded_operation_accepts_filtered_description_without_losing_identity(self):
        response = self.operation()
        response["response"]["description"] = "#############"
        accept_operation(self.entry, response)
        self.assertEqual((self.entry["operation"], self.entry["asset_id"]), ("operations/abc", "456"))
        self.assertFalse(self.entry["public_source_marker_matches"])
        self.assertEqual(self.entry["state"], "uploaded")
        with self.assertRaisesRegex(ValueError, "source marker"):
            metadata_result(self.entry, response["response"], reconciliation=True)

    def test_explicit_retry_preserves_confirmed_failure_and_refuses_ambiguous_or_completed(self):
        self.entry.update(state="uncertain")
        with self.assertRaises(ValueError):
            retry_rejected(self.entry)
        self.entry.update(state="blocked", terminal_failure=True, operation="operations/failed", error="Invalid model")
        retry_rejected(self.entry)
        self.assertEqual(self.entry["state"], "planned")
        self.assertNotIn("operation", self.entry)
        self.assertEqual(self.entry["attempts"][0]["operation"], "operations/failed")
        self.entry.update(state="blocked", http_status=403, asset_id="456")
        with self.assertRaises(ValueError):
            retry_rejected(self.entry)

    def test_plan_preserves_known_results_and_rejects_identity_changes(self):
        journal = {"entries": {self.entry["key"]: self.entry}}
        metadata_result(self.entry, self.metadata())
        row = plan([self.asset], journal, self.destination)[0]
        self.assertEqual((row["action"], row["asset_id"]), ("uploaded", "456"))
        changed = {**self.asset, "fingerprint": "changed"}
        self.assertEqual(plan([changed], journal, self.destination)[0]["action"], "blocked")
        alias = {**self.asset, "key": "alias"}
        self.assertIn("Same fingerprint", plan([alias], journal, self.destination)[0]["reason"])
        reused = {**self.asset, "status": "reused", "asset_id": "456"}
        self.assertEqual(plan([reused], {"entries": {}}, self.destination)[0]["action"], "reused")

    def test_creation_only_allowed_endpoint_and_maximum_zero_fee(self):
        api = AssetsAPI("test-key")
        with patch.object(api, "request", return_value={}) as request:
            api.create(self.entry, b"bytes", "model/gltf+json")
        path, body, content_type = request.call_args.args
        self.assertEqual(path, "assets")
        self.assertIn(b'"expectedPrice": 0', body)
        self.assertIn(b'"userId": "123"', body)
        self.assertNotIn(b"test-key", body)
        self.assertIn("multipart/form-data", content_type)
        with self.assertRaisesRegex(ValueError, "Unsupported API path"):
            api.request("../purchases")

    def test_permission_grant_is_limited_to_use_in_one_experience(self):
        api = AssetsAPI("test-key")
        with patch.object(api, "request", return_value={}) as request:
            api.grant_use("456", "789")
        path, body, mime = request.call_args.args
        payload = json.loads(body)
        self.assertEqual((path, mime), ("permissions", "application/json"))
        self.assertEqual((payload["subjectType"], payload["subjectId"], payload["action"]), ("Universe", "789", "Use"))
        self.assertEqual(payload["requests"], [{"assetId": 456, "grantToDependencies": True, "parentVersionNumber": 1}])

    def test_atomic_journal_and_exclusive_lock_survive_reopen(self):
        path = self.root / "results.json"
        with journal_lock(path):
            write_json(path, {"state": "pending", "operation": "operations/abc"})
            with self.assertRaises(OSError):
                with journal_lock(path):
                    self.fail("Second writer acquired journal")
        with journal_lock(path):
            self.assertEqual(json.loads(path.read_text())["operation"], "operations/abc")

    def test_runtime_evidence_requires_correct_identity_and_loaded_safe_geometry(self):
        metadata_result(self.entry, self.metadata())
        journal = {"entries": {self.entry["key"]: self.entry}}
        destination = {**self.destination, "place_id": "11", "universe_id": "22"}
        report = {"journal_sha256": "snapshot", "mode": "Server", "running": True,
                  "place_id": "11", "universe_id": "22", "creator_id": "123", "checked_at": "today",
                  "results": [{"key": self.entry["key"], "asset_id": "456", "fingerprint": self.entry["fingerprint"],
                               "passed": True, "model_passed": True, "client_content": {"passed": True},
                               "parts": 1, "scripts": 0, "size": [1, 2, 3], "content_failures": []}]}
        wrong = {**report, "mode": "Edit"}
        with self.assertRaisesRegex(ValueError, "running intended experience"):
            record_verification(journal, [("report", wrong)], destination, "snapshot")
        self.assertEqual(self.entry["state"], "uploaded")
        record_verification(journal, [("edit-report", wrong)], destination, "later-metadata-snapshot", models_only=True)
        self.assertEqual(self.entry["state"], "uploaded", "Edit evidence must not certify client content")
        self.assertTrue(self.entry["model_verification"]["passed"])
        self.assertEqual(record_verification(journal, [("report", report)], destination, "snapshot"), 1)
        self.assertEqual(self.entry["state"], "verified")
        report["results"][0]["scripts"] = 1
        record_verification(journal, [("report", report)], destination, "snapshot")
        self.assertEqual(self.entry["state"], "uploaded")


if __name__ == "__main__":
    unittest.main()
