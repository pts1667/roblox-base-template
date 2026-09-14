"""Free Model uploads with a durable journal. Default plan is offline; only main agent uses live API commands."""
from __future__ import annotations

import argparse
import base64
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import repo_config
import math
import mimetypes
import os
from pathlib import Path
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener
import uuid

from asset_inventory import ROOT, approved_ids, digest, gltf_dependencies, inventory

API = "https://apis.roblox.com/assets/v1/"
MAX_BYTES = 20 * 1024 * 1024
TYPES = {".gltf": "model/gltf+json", ".glb": "model/gltf-binary", ".fbx": "model/fbx"}


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(data, stream, indent=2, ensure_ascii=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


@contextmanager
def journal_lock(path):
    """An OS lock is released on process exit, including an interrupted uploader."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(path.suffix + ".lock").open("a+b") as stream:
        stream.seek(0, 2)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def creator(value):
    values = {key: str(value[key]) for key in ("userId", "groupId") if value.get(key)}
    if len(values) != 1 or not next(iter(values.values())).isdigit():
        raise ValueError("Destination requires exactly one positive userId or groupId")
    if int(next(iter(values.values()))) <= 0:
        raise ValueError("Creator ID must be positive")
    return values


def credential(path=ROOT / ".env"):
    # Never read at import, discovery, planning or test-fixture construction time.
    value = os.environ.get("ROBLOX_STUDIO_API_KEY", "")
    if not value:
        for line in Path(path).read_text(encoding="utf-8-sig").splitlines():
            key, sep, candidate = line.strip().partition("=")
            if sep and key == "ROBLOX_STUDIO_API_KEY":
                value = candidate.strip().strip("\"'")
                break
    if not value or any(c in value for c in "\r\n"):
        raise ValueError("ROBLOX_STUDIO_API_KEY is absent or invalid")
    return value


class ApiFailure(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward credentials, or replay a creation, to a redirect.


class AssetsAPI:
    def __init__(self, key):
        self.key = key
        self.opener = build_opener(NoRedirect())

    def request(self, path, data=None, content_type=None):
        if path != "permissions" and not re.fullmatch(r"(?:assets(?:/\d+)?|operations/[\w-]+)", path):
            raise ValueError("Unsupported API path")
        url = "https://apis.roblox.com/asset-permissions-api/v1/assets/permissions" if path == "permissions" else API + path
        return self._json_request(url, data, content_type, "PATCH" if path == "permissions" else None)

    def inventory_page(self, user_id, page_token=None):
        if not str(user_id).isdigit() or int(user_id) <= 0:
            raise ValueError('Inventory requires a positive user ID')
        query = {'maxPageSize': 100, 'filter': 'inventoryItemAssetTypes=MODEL'}
        if page_token:
            query['pageToken'] = page_token
        return self._json_request('https://apis.roblox.com/cloud/v2/users/' + str(user_id) + '/inventory-items?' + urlencode(query))

    def _json_request(self, url, data=None, content_type=None, method=None):
        headers = {"x-api-key": self.key, "Accept": "application/json"}
        if content_type:
            headers["Content-Type"] = content_type
        request = Request(url, data=data, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=30) as response:
                return json.load(response)
        except HTTPError as exc:
            # Do not log request headers, arbitrary response bodies or exception reprs.
            try:
                body = json.loads(exc.read(8192))
                message = str(body.get("message") or body.get("error") or "Request rejected")
            except (ValueError, AttributeError):
                message = "Request rejected"
            message = message.replace(self.key, "[REDACTED]")[:500]
            raise ApiFailure(f"HTTP {exc.code}: {message}", exc.code) from None
        except (URLError, TimeoutError, OSError, ValueError):
            raise ApiFailure("No valid API response; creation may have been accepted") from None

    def get_asset(self, asset_id):
        return self.request("assets/" + str(asset_id))

    def get_operation(self, path):
        return self.request(path)

    def grant_use(self, asset_id, universe_id):
        if not str(asset_id).isdigit() or not str(universe_id).isdigit():
            raise ValueError("Permission targets must be numeric asset/universe IDs")
        body = {"subjectType": "Universe", "subjectId": str(universe_id), "action": "Use",
                "requests": [{"assetId": int(asset_id), "grantToDependencies": True, "parentVersionNumber": 1}]}
        return self.request("permissions", json.dumps(body).encode(), "application/json")

    def create(self, entry, content, mime):
        metadata = {
            "assetType": "Model", "displayName": entry["name"],
            "description": "Source SHA-256: " + entry["fingerprint"],
            "creationContext": {"creator": entry["creator"], "expectedPrice": 0},
        }
        boundary = "roblox-" + uuid.uuid4().hex
        filename = "model" + Path(entry["source"]).suffix.lower()
        body = (f'--{boundary}\r\nContent-Disposition: form-data; name="request"\r\n'
                'Content-Type: application/json\r\n\r\n').encode() + json.dumps(metadata).encode()
        body += (f'\r\n--{boundary}\r\nContent-Disposition: form-data; name="fileContent"; '
                 f'filename="{filename}"\r\nContent-Type: {mime}\r\n\r\n').encode()
        body += content + f"\r\n--{boundary}--\r\n".encode()
        return self.request("assets", body, "multipart/form-data; boundary=" + boundary)


def safe_path(root, relative):
    result = (root / relative).resolve()
    if not result.is_relative_to((root / "unimported").resolve()) or not result.is_file():
        raise ValueError("Missing source or path outside unimported/")
    return result


def payload(asset, root=ROOT):
    """Embed all external glTF buffers/images; reject stale input and oversized files."""
    source = safe_path(root, asset["source"])
    license_path = safe_path(root, asset["evidence"])
    if asset.get("license") != "CC0-1.0" or digest(license_path) != asset.get("sha256"):
        raise ValueError("License evidence changed or is not approved CC0")
    suffix = source.suffix.lower()
    if suffix not in TYPES:
        raise ValueError("Unsupported automatic Model upload format")
    fingerprint = digest(source)
    content = source.read_bytes()
    if suffix == ".gltf":
        deps, failure = gltf_dependencies(source, root / "unimported" / asset["pack"])
        if failure:
            raise ValueError(failure)
        combined = hashlib.sha256(fingerprint.encode())
        for dep in deps:
            combined.update(dep.relative_to(root / "unimported").as_posix().encode())
            combined.update(digest(dep).encode())
        fingerprint = combined.hexdigest()
        data = json.loads(content.decode("utf-8-sig"))
        for entry in data.get("buffers", []) + data.get("images", []):
            uri = entry.get("uri")
            if uri and not uri.startswith("data:"):
                dep = (source.parent / unquote(uri)).resolve()
                # gltf_dependencies already validated and bounded each URI.
                mime = mimetypes.guess_type(dep)[0] or "application/octet-stream"
                entry["uri"] = "data:" + mime + ";base64," + base64.b64encode(dep.read_bytes()).decode()
        content = json.dumps(data, separators=(",", ":")).encode()
    if fingerprint != asset["fingerprint"]:
        raise ValueError("Source/dependency fingerprint changed; regenerate inventory and reconcile")
    if len(content) > MAX_BYTES:
        raise ValueError("Bundled Model exceeds the 20 MiB API limit")
    return content, TYPES[suffix]


def entry_for(asset, destination):
    return {**{key: asset[key] for key in ("key", "name", "source", "pack", "fingerprint",
                                          "license", "evidence", "sha256", "intended_use")},
            "creator": creator(destination["creator"]), "state": "planned", "expected_price": 0}


def validate_metadata(data, expected_creator, entry=None):
    if creator(data.get("creationContext", {}).get("creator", {})) != creator(expected_creator):
        raise ValueError("API asset creator differs from the verified destination")
    if data.get("assetType") not in ("Model", "ASSET_TYPE_MODEL"):
        raise ValueError("API asset is not a Model")
    if entry and (data.get("displayName") != entry["name"] or
                  entry["fingerprint"] not in data.get("description", "")):
        raise ValueError("Asset name/source marker does not match the journal; reconciliation required")


def metadata_result(entry, data, reconciliation=False):
    # An operation journaled directly from Create already binds the submitted bytes.
    # Roblox may filter the public description; only an unrelated-ID reconciliation
    # relies on the public source marker as additional identity evidence.
    validate_metadata(data, entry["creator"], entry if reconciliation else None)
    asset_id = str(data.get("assetId", ""))
    if not asset_id.isdigit() or int(asset_id) <= 0:
        raise ValueError("API returned no asset ID")
    if entry.get("asset_id") and entry["asset_id"] != asset_id:
        raise ValueError("API asset ID changed from journal")
    entry.update(asset_id=asset_id, state="verified" if entry.get("state") == "verified" else "uploaded", metadata_checked_at=now(),
                 moderation=data.get("moderationResult", {}).get("moderationState", "Unknown"),
                 api_display_name=data.get("displayName"),
                 public_source_marker_matches=entry["fingerprint"] in data.get("description", ""))
    entry.pop("error", None)
    if "rejected" in entry["moderation"].lower():
        entry.update(state="blocked", error="Asset rejected by moderation; never automatically re-upload")


def accept_operation(entry, response):
    path = response.get("path", "")
    if not re.fullmatch(r"operations/[\w-]+", path):
        raise ValueError("API response has no valid operation path")
    if entry.get("operation") and entry["operation"] != path:
        raise ValueError("Operation identity changed")
    entry.update(operation=path, state="pending")
    if response.get("done"):
        if response.get("error"):
            error = response["error"]
            entry.update(state="blocked", terminal_failure=True, error=f"Operation failed ({error.get('code')}): {str(error.get('message'))[:500]}")
        else:
            metadata_result(entry, response.get("response", {}))


def upload_one(entry, api, save, root=ROOT):
    if entry["state"] != "planned" or entry.get("operation") or entry.get("asset_id"):
        return False
    try:
        content, mime = payload(entry, root)
    except ValueError as exc:
        entry.update(state="blocked", error=str(exc))
        save()
        return False
    # A crash after this write is ambiguous, even if no response was journaled.
    entry.update(state="uploading", attempted_at=now(), payload_sha256=hashlib.sha256(content).hexdigest())
    save()
    try:
        result = api.create(entry, content, mime)
        # Persist the operation before interpreting metadata or doing another request.
        path = result.get("path", "")
        if not re.fullmatch(r"operations/[\w-]+", path):
            raise ValueError("Creation response has no operation; reconcile creator inventory")
        entry.update(operation=path, state="pending")
        save()
        accept_operation(entry, result)
    except ApiFailure as exc:
        uncertain = exc.status is None or exc.status >= 500 or 300 <= exc.status < 400
        entry.update(state="uncertain" if uncertain else "blocked", error=str(exc), http_status=exc.status)
    except ValueError as exc:
        entry.update(state="pending" if entry.get("operation") else "uncertain", error=str(exc))
    save()
    return True


def resume_one(entry, api, save, wait_seconds=0):
    if entry["state"] == "uploading":
        entry.update(state="uncertain", error="Interrupted create has no response; reconcile before any new upload")
        save()
    deadline = time.monotonic() + wait_seconds
    while entry.get("operation") and entry["state"] == "pending":
        try:
            accept_operation(entry, api.get_operation(entry["operation"]))
            entry.pop("poll_error", None)
        except (ApiFailure, ValueError) as exc:
            # Polling failure/timeout is not a terminal operation. Preserve its identity.
            entry["poll_error"] = str(exc)
            save()
            return
        save()
        if entry["state"] != "pending" or time.monotonic() >= deadline:
            return
        time.sleep(min(2, max(0, deadline - time.monotonic())))


def inspect_creator_inventory(api, destination, document):
    """List every inventory page; inspect undocumented IDs before any new create."""
    expected = creator(destination['creator'])
    if 'userId' not in expected:
        raise ValueError('Automatic pre-upload inventory reconciliation currently requires a user creator')
    hints = {}
    for (_, name), value in approved_ids(document).items():
        for asset_id in [value['asset_id'], *value['alternate_ids']]:
            hints.setdefault(asset_id, set()).add(name)
    ids, seen_tokens, page_token = set(), set(), None
    for page_number in range(1, 21):
        page = api.inventory_page(expected['userId'], page_token)
        for item in page.get('inventoryItems', []):
            details = item.get('assetDetails', {})
            asset_id = str(details.get('assetId', ''))
            if details.get('inventoryItemAssetType') != 'MODEL' or not asset_id.isdigit():
                raise ValueError('Unexpected inventory item; cannot establish complete Model inventory')
            ids.add(asset_id)
        page_token = page.get('nextPageToken')
        if not page_token:
            break
        if page_token in seen_tokens:
            raise ValueError('Repeated inventory cursor; cannot establish complete Model inventory')
        seen_tokens.add(page_token)
    else:
        raise ValueError('Inventory exceeded the 20-page bound; reconcile manually before uploading')
    models = []
    for asset_id in sorted(ids):
        if asset_id in hints:
            models.append({'asset_id': asset_id, 'names': sorted(hints[asset_id]), 'evidence': 'ROBLOX_ASSETS.md'})
        else:
            metadata = api.get_asset(asset_id)
            name = metadata.get('displayName')
            if metadata.get('assetType') != 'Model' or not isinstance(name, str) or not name.strip():
                raise ValueError('Inventory Model metadata is incomplete: ' + asset_id)
            models.append({'asset_id': asset_id, 'names': [name.strip()], 'evidence': 'Assets API',
                           'creator': metadata.get('creationContext', {}).get('creator'),
                           'moderation': metadata.get('moderationResult')})
    return {'creator': expected, 'checked_at': now(), 'complete': True, 'pages': page_number, 'models': models}


def existing_name_candidates(entry, audit):
    if not audit.get('complete') or audit.get('creator') != entry['creator']:
        raise ValueError('Complete inventory reconciliation for the destination creator is required')
    wanted = entry['name'].strip().casefold()
    return sorted(m['asset_id'] for m in audit['models'] if any(n.strip().casefold() == wanted for n in m['names']))


def record_inventory_audit(journal, audit):
    journal['creator_inventory'] = audit
    for entry in journal['entries'].values():
        if entry['creator'] == audit['creator']:
            entry['existing_asset_candidates'] = [asset_id for asset_id in existing_name_candidates(entry, audit)
                                                  if asset_id != entry.get('asset_id')]


def plan(assets, journal, destination):
    rows = []
    expected = creator(destination["creator"])
    for asset in assets:
        old = journal["entries"].get(asset["key"])
        row = {key: asset[key] for key in ("key", "name", "source", "fingerprint", "status", "reason")}
        row.update(creator=expected, expected_price=0, action="upload" if asset["status"] == "queued" else asset["status"])
        if asset.get("asset_id"):
            row["asset_id"] = asset["asset_id"]
        if old:
            if old["fingerprint"] != asset["fingerprint"] or old["creator"] != expected:
                row.update(action="blocked", reason="Journal identity differs; explicit reconciliation required")
            else:
                row.update(action=old["state"], reason=old.get("error", "Recorded in durable import journal"))
                for key in ("asset_id", "operation"):
                    if old.get(key):
                        row[key] = old[key]
        elif row["action"] == "upload":
            duplicate = next((v for v in journal["entries"].values()
                              if v["fingerprint"] == asset["fingerprint"] and v["creator"] == expected), None)
            if duplicate:
                row.update(action="blocked", reason="Same fingerprint already journaled under " + duplicate["key"])
        audit = journal.get('creator_inventory', {})
        if row['action'] in ('upload', 'planned') and audit.get('complete') and audit.get('creator') == expected:
            candidates = existing_name_candidates({'name': asset['name'], 'creator': expected}, audit)
            if candidates:
                row.update(action='blocked', reason='Existing inventory Models share this name; inspect/reuse before uploading',
                           existing_asset_candidates=candidates)
        rows.append(row)
    return rows


def record_verification(journal, reports, destination, journal_sha256, models_only=False):
    """Accept identity-matched runtime evidence without changing upload IDs or source data."""
    updates = []
    expected_creator = next(iter(creator(destination["creator"]).values()))
    for path, report in reports:
        correct_scope = ((report.get("mode") in ("Edit", "Server") and bool(report.get("journal_sha256"))) if models_only else
                         (report.get("journal_sha256") == journal_sha256 and report.get("mode") == "Server" and report.get("running") is True))
        if (not correct_scope or report.get("place_id") != destination["place_id"] or
                report.get("universe_id") != destination["universe_id"] or report.get("creator_id") != expected_creator):
            raise ValueError("Report does not identify this journal and a running intended experience")
        for row in report.get("results", []):
            entry = journal["entries"].get(row.get("key"))
            if (not entry or row.get("asset_id") != entry.get("asset_id") or
                    row.get("fingerprint") != entry["fingerprint"]):
                raise ValueError("Verification asset identity differs from journal")
            size = row.get("size", [])
            valid_size = (len(size) == 3 and all(isinstance(v, (int, float)) and math.isfinite(v) and v > 0 for v in size))
            model_ok = (row.get("model_passed", row.get("passed")) is True and row.get("scripts") == 0 and
                        row.get("parts", 0) > 0 and valid_size)
            passed = model_ok if models_only else (model_ok and row.get("passed") is True and
                row.get("content_failures") == [] and row.get("client_content", {}).get("passed") is True and
                entry.get("moderation", "").lower() in ("approved", "moderation_state_approved"))
            updates.append((entry, row, str(path), report["checked_at"], report["journal_sha256"], report["mode"], passed))
    for entry, row, path, checked_at, snapshot, mode, passed in updates:
        entry["model_verification" if models_only else "runtime_verification"] = {
            "report": path, "checked_at": checked_at, "passed": passed, "mode": mode, "journal_sha256": snapshot,
            **{k: row[k] for k in ("size", "pivot", "bounds", "mesh_ids", "content_ids", "parts", "scripts", "bones", "error") if k in row}}
        if passed and not models_only:
            entry["state"] = "verified"
        elif not passed and entry["state"] == "verified":
            entry["state"] = "uploaded"
    return len(updates)


def retry_rejected(entry):
    if (entry["state"] != "blocked" or entry.get("asset_id") or
            not (entry.get("terminal_failure") or
                 (not entry.get("operation") and entry.get("http_status") in (400, 401, 403, 413, 429)))):
        raise ValueError("Only a confirmed failed creation with no asset ID can be explicitly retried")
    fields = ("state", "operation", "error", "poll_error", "http_status", "terminal_failure", "attempted_at", "payload_sha256")
    entry.setdefault("attempts", []).append({k: entry[k] for k in fields if k in entry})
    for key in fields:
        entry.pop(key, None)
    entry["state"] = "planned"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("plan", "upload", "resume", "metadata", "reconcile", "inventory", "record-verification", "record-models", "retry-rejected", "grant-use"), nargs="?", default="plan")
    parser.add_argument("--destination", type=Path, help="Override config.json open_cloud with a destination JSON file")
    parser.add_argument("--journal", type=Path, default=ROOT / "docs/ASSET_IMPORT_RESULTS.json")
    parser.add_argument("--pack", help="Exact pack folder name")
    parser.add_argument("--key", help="Exact inventory key")
    parser.add_argument("--limit", type=int, default=5, help="Bound the batch (1-100)")
    parser.add_argument("--wait", type=int, default=20, help="Operation wait per asset (0-60 seconds)")
    parser.add_argument("--out", type=Path, help="Write the complete offline plan")
    parser.add_argument("--asset-id", help="Existing asset for explicit fingerprint/name/creator reconciliation")
    parser.add_argument("--operation", help="Known operations/... path for an interrupted create")
    parser.add_argument("--report", type=Path, action="append", default=[], help="Runtime report; repeat to record batches atomically")
    args = parser.parse_args()
    if not 1 <= args.limit <= 100 or not 0 <= args.wait <= 60:
        parser.error("limit must be 1-100 and wait 0-60")
    try:
        destination = repo_config.open_cloud_destination(args.destination)
        creator(destination["creator"])
    except (OSError, ValueError) as error:
        parser.error("Configure open_cloud in config.json (or --destination): %s" % error)
    discovered = inventory(ROOT / "unimported", ROOT / "docs/ROBLOX_ASSETS.md")
    assets = [a for a in discovered["asset_groups"] if (not args.pack or a["pack"] == args.pack)
              and (not args.key or a["key"] == args.key)]
    assets.sort(key=lambda a: (a["priority"], a["key"]))
    if not assets:
        parser.error("No matching asset groups")

    def read_journal():
        return read_json(args.journal) if args.journal.exists() else {"version": 1, "entries": {}}

    if args.command == "plan":
        rows = plan(assets, read_journal(), destination)
        result = {"destination": destination, "summary": dict(Counter(r["action"] for r in rows)), "assets": rows}
        if args.out:
            write_json(args.out, result)
            print(json.dumps({"summary": result["summary"], "plan": str(args.out)}))
        else:
            print(json.dumps(result, indent=2))
        return 0

    with journal_lock(args.journal):
        journal = read_journal()

        def save():
            journal["updated_at"] = now()
            write_json(args.journal, journal)

        if args.command in ("record-verification", "record-models"):
            if not args.report:
                parser.error("record-verification requires --report")
            count = record_verification(journal, [(p, read_json(p)) for p in args.report], destination, digest(args.journal),
                                        models_only=args.command == "record-models")
            save()
            print(json.dumps({"recorded": count, "summary": dict(Counter(e["state"] for e in journal["entries"].values()))}))
            return 0

        if args.command == "retry-rejected":
            if not args.key or args.key not in journal["entries"]:
                parser.error("retry-rejected requires a journaled --key")
            entry = journal["entries"][args.key]
            if entry["fingerprint"] != assets[0]["fingerprint"]:
                parser.error("Source identity changed; reconcile it before retrying")
            retry_rejected(entry)
            save()
            print(json.dumps({"key": args.key, "state": entry["state"], "upload_sent": False}))
            return 0

        api = AssetsAPI(credential())
        proof = api.get_asset(destination["verification_asset_id"])
        validate_metadata(proof, destination["creator"])
        journal["destination_verification"] = {"creator": creator(destination["creator"]),
                                               "asset_id": destination["verification_asset_id"], "checked_at": now()}
        save()
        if args.command == 'inventory' or (args.command == 'upload' and any(
            r['action'] in ('upload', 'planned') for r in plan(assets, journal, destination))):
            print(json.dumps({'checking': 'creator Model inventory before new uploads'}), flush=True)
            audit = inspect_creator_inventory(api, destination, (ROOT / 'docs/ROBLOX_ASSETS.md').read_text(encoding='utf-8'))
            record_inventory_audit(journal, audit)
            save()
            print(json.dumps({'inventory_models': len(audit['models']), 'pages': audit['pages'], 'complete': True}), flush=True)
            if args.command == 'inventory':
                return 0
        if args.command == "reconcile":
            if not args.key or bool(args.asset_id) == bool(args.operation):
                parser.error("reconcile requires --key and exactly one of --asset-id/--operation")
            entry = journal["entries"].get(args.key)
            if not entry:
                parser.error("No journal entry to reconcile")
            if args.operation:
                if entry.get("operation") and entry["operation"] != args.operation:
                    parser.error("Cannot replace a recorded operation")
                accept_operation(entry, api.get_operation(args.operation))
            else:
                if entry.get("asset_id") and entry["asset_id"] != args.asset_id:
                    parser.error("Cannot replace a recorded asset ID")
                metadata_result(entry, api.get_asset(args.asset_id), reconciliation=True)
            save()
            print(json.dumps({"key": args.key, "state": entry["state"]}))
            return 0

        processed = 0
        for asset in assets:
            row = plan([asset], journal, destination)[0]
            entry = journal["entries"].get(asset["key"])
            if args.command == 'upload' and row.get('existing_asset_candidates'):
                print(json.dumps({'key': asset['key'], 'state': 'blocked', 'reason': row['reason'],
                                  'existing_asset_candidates': row['existing_asset_candidates']}), flush=True)
                continue
            if args.command == "upload" and row["action"] == "upload":
                entry = entry_for(asset, destination)
                journal["entries"][asset["key"]] = entry
                save()
            if not entry or entry["fingerprint"] != asset["fingerprint"] or entry["creator"] != creator(destination["creator"]):
                continue
            eligible = ((args.command == "upload" and entry["state"] == "planned") or
                        (args.command == "resume" and entry["state"] in ("pending", "uploading")) or
                        (args.command in ("metadata", "grant-use") and entry.get("asset_id")))
            if not eligible:
                continue
            if args.command == "upload":
                candidates = existing_name_candidates(entry, journal['creator_inventory'])
                if candidates:
                    entry.update(state='blocked', existing_asset_candidates=candidates,
                                 error='Existing inventory Models share this name; inspect/reuse before creating another upload')
                    save()
                    print(json.dumps({'key': entry['key'], 'state': 'blocked', 'existing_asset_candidates': candidates}), flush=True)
                    continue
                upload_one(entry, api, save)
                resume_one(entry, api, save, args.wait)
            elif args.command == "resume":
                resume_one(entry, api, save, args.wait)
            elif args.command == "grant-use":
                entry["experience_permission"] = {"universe_id": destination["universe_id"], "requested_at": now(),
                                                   "state": "requested", "grant_to_dependencies": True}
                save()
                try:
                    result = api.grant_use(entry["asset_id"], destination["universe_id"])
                    entry["experience_permission"].update(state="response_received", response=result)
                except ApiFailure as exc:
                    entry["experience_permission"].update(state="failed", error=str(exc))
                save()
                print(json.dumps({"key": entry["key"], "experience_permission": entry["experience_permission"]}), flush=True)
            else:
                try:
                    metadata_result(entry, api.get_asset(entry["asset_id"]))
                except (ApiFailure, ValueError) as exc:
                    entry["metadata_error"] = str(exc)
                save()
            processed += 1
            print(json.dumps({k: entry[k] for k in ("key", "state", "asset_id", "operation", "error", "poll_error", "moderation") if k in entry}), flush=True)
            if processed >= args.limit or entry.get("http_status") in (401, 403, 429):
                break
            time.sleep(0.6)
        print(json.dumps({"processed": processed, "journal": str(args.journal)}))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ApiFailure, ValueError, OSError) as exc:
        print(json.dumps({"error": str(exc)}))
        raise SystemExit(1)
