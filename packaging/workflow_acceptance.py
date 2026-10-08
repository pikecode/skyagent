"""One-command synthetic coupon workflow acceptance; no network or user database."""

import argparse
import json
import secrets
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import requests  # noqa: E402

from skyagent_manager import __version__  # noqa: E402
from skyagent_manager.account_benefits import AccountBenefitSession  # noqa: E402
from skyagent_manager.accounts import Accounts, parse_account_text  # noqa: E402
from skyagent_manager.backup import BackupService  # noqa: E402
from skyagent_manager.coupon_sharing import (  # noqa: E402
    CouponShareOutputs,
    DirectCouponShareAdapter,
)
from skyagent_manager.db import StoreDatabase  # noqa: E402
from skyagent_manager.direct_benefits import (  # noqa: E402
    ORIGIN,
    DirectBenefitQueryAdapter,
)
from skyagent_manager.inventory import Inventory  # noqa: E402
from skyagent_manager.share_journal import ShareJournal  # noqa: E402
from skyagent_manager.share_safety import share_writes_blocked  # noqa: E402

TOKEN = "synthetic-workflow-authorized-token"
URL = "https://mobile.yaduo.com/share?code=synthetic-workflow-output"
GIFT_CODE = "synthetic-workflow-private-gift-code"


def require(condition):
    if not condition:
        raise RuntimeError("Synthetic workflow invariant failed")


def expect_blocked(action):
    try:
        action()
    except ValueError:
        return
    raise RuntimeError("Expected workflow guard did not block")


class Response:
    status_code = 200

    def __init__(self, payload):
        self.raw = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def iter_content(self, chunk_size):
        yield self.raw


class SyntheticTransport:
    """In-memory transport plugged into real adapters, not a provider server."""

    def __init__(self, calls, *, fail=False, before_share=None):
        self.cookies = requests.cookies.RequestsCookieJar()
        self.calls = calls
        self.fail = fail
        self.before_share = before_share
        self.closed = False

    def request(self, method, url, **options):
        require(not self.trust_env)
        require(options["verify"] and not options["allow_redirects"])
        require(options["stream"] and options["timeout"] == (10, 30))
        require(url.startswith(ORIGIN + "/"))
        path = url.removeprefix(ORIGIN)
        self.calls.append((method, path))
        if path == "/coupon/memberCouponListOfType":
            require(method == "POST" and options["json"]["token"] == TOKEN)
            require(options["json"]["stateCodes"] == ["AVAILABLE"])
            result = {
                "couponList": [
                    {
                        "code": "synthetic-" + options["json"]["titleCode"],
                        "share": 1,
                        "expiryStr": "2099-12-31",
                    }
                ]
            }
        elif path == "/propCard/classification/list":
            require(method == "GET")
            result = {}
        elif path == "/user/share/generateShareInfo":
            require(method == "POST" and options["json"]["token"] == TOKEN)
            require(options["json"]["shareBizType"] == "COUPON")
            require(callable(self.before_share))
            self.before_share()
            if self.fail:
                # Model a lost reply AFTER provider acceptance, not a known rejection.
                raise requests.Timeout("synthetic lost response")
            result = {"shareUrl": URL}
        else:
            raise RuntimeError("Unexpected synthetic protocol route")
        return Response({"retcode": 0, "result": result})

    def close(self):
        self.closed = True


class SyntheticGiftTransport(SyntheticTransport):
    """Order/gift form protocol substitute; never supplies validity evidence."""

    def request(self, method, url, **options):
        from skyagent_manager.direct_gifts import ORIGIN as gift_origin

        require(not self.trust_env and method == "POST")
        require(options["verify"] and not options["allow_redirects"])
        require(options["stream"] and options["timeout"] == (10, 30))
        require(url.startswith(gift_origin + "/") and "json" not in options)
        data = options["data"]
        require(data["token"] == TOKEN)
        path = url.removeprefix(gift_origin)
        self.calls.append((method, path))
        if path == "/order/getOrderList":
            require(data["pageNo"] == "1" and data["pageSize"] == "20")
            require(data["state"] == "0" and data["queryState"] == "1")
            require(data["appVer"] == "3.31.0" and data["channelId"] == "3000001")
            result = [
                {
                    "folioId": "synthetic-workflow-folio",
                    "chainId": "synthetic-workflow-chain",
                    "chainName": "虚构酒店",
                    "orderStateName": "资格未知的订单快照",
                }
            ]
        elif path == "/fission/lucky/createShare":
            require(data["folioId"] == "synthetic-workflow-folio")
            require(data["chainId"] == "synthetic-workflow-chain")
            require(data["appVer"] == "4.8.1" and data["channelId"] == "20001")
            require(data["platType"] == "2")
            require(callable(self.before_share))
            self.before_share()
            if self.fail:
                raise requests.Timeout("synthetic lost gift response")
            result = {"shareCode": GIFT_CODE}
        else:
            raise RuntimeError("Unexpected synthetic gift protocol route")
        return Response({"retcode": 0, "result": result})


def run(output):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    report = {
        "ok": False,
        "version": __version__,
        "scope": "synthetic-coupon-workflow-in-memory-transport",
        "network": "blocked",
        "production_verified": False,
        "gui_verified": False,
        "user_database_accessed": False,
        "steps": [],
    }
    calls, sessions = [], []

    def factory(*, fail=False, before_share=None):
        transport = SyntheticTransport(calls, fail=fail, before_share=before_share)
        sessions.append(transport)
        return transport

    db = None
    try:
        with patch(
            "requests.sessions.Session.request",
            side_effect=RuntimeError("Network forbidden"),
        ):
            with TemporaryDirectory(prefix="skyagent-workflow-") as temporary:
                root = Path(temporary)
                key = secrets.token_bytes(32)
                path = root / "data" / "manager.sqlite3"
                db = StoreDatabase(path, key=key)
                store = db.add_store("虚构验收门店")
                other = db.add_store("虚构隔离门店")
                imported = Accounts(db).import_rows(
                    store, parse_account_text(f"13900000000----{TOKEN}")
                )
                require(imported.inserted == 1 and imported.skipped == 0)
                account = Accounts(db).list(store)[0]["id"]
                require(not Accounts(db).list(other))
                report["steps"].append("account-text-import-and-store-isolation")

                before = path.read_bytes()
                query = AccountBenefitSession(
                    db, DirectBenefitQueryAdapter(session_factory=factory)
                )
                items = query.query(store, account)
                owner = query.owner
                require(len(items) == 3 and len(calls) == 4)
                require(path.read_bytes() == before)
                report["steps"].append("direct-query-read-only")

                item = items[0]
                journal = ShareJournal(db)
                operation = journal.reserve(store, account, owner, item)

                def pending_is_durable(target):
                    reopened = StoreDatabase(path, key=key)
                    try:
                        require(
                            ShareJournal(reopened).state(target)["state"] == "pending"
                        )
                    finally:
                        reopened.close()

                share = DirectCouponShareAdapter(
                    session_factory=lambda: factory(
                        before_share=lambda: pending_is_durable(item)
                    )
                )
                url = share.share(TOKEN, item)
                outputs = CouponShareOutputs(db)
                outputs.save(store, account, owner, item, operation, url)
                require(journal.state(item)["state"] == "confirmed")
                require(not Inventory(db).list(store))
                expect_blocked(lambda: journal.reserve(store, account, owner, item))
                report["steps"].append(
                    "durable-reservation-single-share-encrypted-save"
                )

                failed = items[1]
                failed_operation = journal.reserve(store, account, owner, failed)
                unreliable = DirectCouponShareAdapter(
                    session_factory=lambda: factory(
                        fail=True, before_share=lambda: pending_is_durable(failed)
                    )
                )
                expect_blocked(lambda: unreliable.share(TOKEN, failed))
                journal.finish(failed, failed_operation)
                require(journal.state(failed)["state"] == "unknown")
                report["steps"].append("lost-reply-unknown-not-success")

                encrypted = path.read_bytes()
                require(
                    all(
                        secret.encode() not in encrypted
                        for secret in (TOKEN, URL, "13900000000")
                    )
                )
                db.close()
                db = StoreDatabase(path, key=key)
                outputs = CouponShareOutputs(db)
                saved = outputs.list_saved(store, account)
                require(len(saved) == 1 and not outputs.list_saved(other, account))
                expect_blocked(
                    lambda: ShareJournal(db).reserve(store, account, owner, failed)
                )
                require(ShareJournal(db).state(failed)["state"] == "unknown")
                report["steps"].append(
                    "restart-success-history-and-unknown-replay-block"
                )

                outputs.import_operation(
                    store, account, owner, operation, expected=saved[0]
                )
                require(len(Inventory(db).list(store)) == 1)
                require(Inventory(db).list(store)[0]["value"] == URL)
                expect_blocked(
                    lambda: outputs.import_operation(store, account, owner, operation)
                )
                before = path.read_bytes()
                outputs.export_to(
                    output / "masked-history.csv",
                    store,
                    account,
                    owner,
                    outputs.list_saved(store, account),
                )
                require(path.read_bytes() == before)
                require(
                    all(
                        secret
                        not in (output / "masked-history.csv").read_text(
                            encoding="utf-8-sig"
                        )
                        for secret in (URL, TOKEN, "13900000000")
                    )
                )
                report["steps"].append(
                    "explicit-once-only-stock-import-and-masked-export"
                )

                backup = BackupService(db).create(root / "snapshot.skybackup")
                BackupService(db).restore(backup)
                require(share_writes_blocked(db))
                expect_blocked(
                    lambda: ShareJournal(db).reserve(store, account, owner, items[2])
                )
                require(len(CouponShareOutputs(db).list_saved(store, account)) == 1)
                report["steps"].append(
                    "encrypted-backup-restore-write-block-history-readable"
                )
                require(len(calls) == 6 and all(session.closed for session in sessions))
                report["protocol_requests"] = {
                    "query": 4,
                    "share_success": 1,
                    "share_lost_reply": 1,
                }
                report["ok"] = True
                db.close()
                db = None
    except Exception:
        report["error"] = (
            "Workflow acceptance failed; no sensitive diagnostic retained."
        )
    finally:
        if db is not None:
            db.close()
        (output / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    options = parser.parse_args()
    if options.output.exists():
        parser.error("Use a new output directory; existing evidence is preserved.")
    report = run(options.output)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
