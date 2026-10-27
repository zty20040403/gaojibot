"""Independent votes reuse the existing private, one-use OTP lifecycle."""


class ApprovalQuorum:
    @staticmethod
    def required_quorum(kind: str, requested: int = 1) -> int:
        if type(requested) is not int or requested not in {1, 2}:
            raise ValueError("Invalid approval quorum")
        return max(requested, 2 if kind in {"server_task", "fleet"} else 1)

    @staticmethod
    def _vote_payload(parent):
        return {"approval_id": parent["approval_id"], "generation": parent["code_generation"],
                "kind": parent["kind"], "payload_hash": parent["payload_hash"], "quorum": parent["quorum"]}

    def _vote_parent(self, db, vote):
        from .store import SecurityError, canonical, digest

        parent = db.execute("SELECT * FROM admin_approvals WHERE approval_id=?", (vote["parent_approval_id"],)).fetchone()
        if (not parent or parent["quorum"] != 2 or parent["parent_approval_id"] is not None
                or parent["status"] not in {"pending", "awaiting_quorum"}
                or parent["expires_at"] <= self.clock() or not self._current(db, dict(parent))
                or parent["account_id"] == vote["account_id"] or parent["qq_id"] == vote["qq_id"]
                or parent["bot_id"] != vote["bot_id"] or parent["code_generation"] != vote["parent_generation"]
                or digest(parent["payload_json"]) != parent["payload_hash"]
                or vote["payload_json"] != canonical(self._vote_payload(parent))):
            raise SecurityError("复核对象已变化、过期或不是另一位管理员的操作", 409)
        return dict(parent)

    def _vote_count(self, db, parent):
        from .store import canonical, digest

        if not self._current(db, parent) or digest(parent["payload_json"]) != parent["payload_hash"]:
            return 0
        if parent["status"] not in {"pending", "awaiting_quorum", "queued", "executing", "succeeded"}:
            return 0
        accounts, qqs = set(), set()
        if parent["consumed_at"] is not None:
            accounts.add(parent["account_id"])
            qqs.add(parent["qq_id"])
        for row in db.execute("SELECT * FROM admin_approvals WHERE parent_approval_id=? AND parent_generation=? AND status='succeeded'", (parent["approval_id"], parent["code_generation"])).fetchall():
            if (row["consumed_at"] is None or row["kind"] != "quorum_vote" or row["bot_id"] != parent["bot_id"]
                    or row["payload_json"] != canonical(self._vote_payload(parent))
                    or digest(row["payload_json"]) != row["payload_hash"] or not self._current(db, dict(row))
                    or row["account_id"] == parent["account_id"] or row["qq_id"] == parent["qq_id"]
                    or row["account_id"] in accounts or row["qq_id"] in qqs):
                continue
            accounts.add(row["account_id"])
            qqs.add(row["qq_id"])
        return len(accounts)

    def _quorum_valid(self, db, request):
        return (request["parent_approval_id"] is None
                and request["consumed_at"] is not None
                and request["quorum"] >= self.required_quorum(request["kind"])
                and self._vote_count(db, request) >= request["quorum"])

    def validate_execution_approval(self, approval_id, owner):
        from .store import SecurityError

        with self.transaction() as db:
            row = db.execute("SELECT * FROM admin_approvals WHERE approval_id=? AND execution_owner=? AND status='executing'", (approval_id, owner)).fetchone()
            if not row or not self._quorum_valid(db, dict(row)):
                raise SecurityError("审批或复核管理员权限已变化，未继续执行", 409)

    def _settle_vote(self, db, request):
        from .store import canonical

        parent_id = request["parent_approval_id"] or request["approval_id"]
        parent = dict(db.execute("SELECT * FROM admin_approvals WHERE approval_id=?", (parent_id,)).fetchone())
        count = self._vote_count(db, parent)
        if parent["status"] == "awaiting_quorum" and count >= parent["quorum"]:
            db.execute("UPDATE admin_approvals SET status='queued', updated_at=? WHERE approval_id=?", (int(self.clock()), parent_id))
            self._audit(db, request["account_id"], "approval.quorum_reached", parent_id, canonical({"quorum": parent["quorum"]}))
            parent["status"] = "queued"
        return {**parent, "approved_count": count}

    def request_review(self, approval_id, qq_id, bot_id):
        from .store import SecurityError

        account = self.account_for_qq(qq_id)
        if account is None:
            raise SecurityError("复核只允许已绑定 QQ 的有效管理员")
        with self.transaction() as db:
            row = db.execute("SELECT * FROM admin_approvals WHERE approval_id=? AND bot_id=?", (approval_id, bot_id)).fetchone()
            if not row:
                raise SecurityError("复核操作不存在", 404)
            parent = dict(row)
        # propose checks the parent again under the same lock that creates the vote.
        return self.propose(account, bot_id=bot_id, kind="quorum_vote", payload=self._vote_payload(parent),
            summary=parent["summary"],
            _parent=(approval_id, parent["code_generation"]))

    def revoke_review(self, approval_id, qq_id, bot_id):
        from .store import SecurityError

        with self.transaction() as db:
            root = db.execute("SELECT * FROM admin_approvals WHERE approval_id=? AND bot_id=?", (approval_id, bot_id)).fetchone()
            if not root or root["parent_approval_id"]:
                raise SecurityError("审批不存在", 404)
            vote = db.execute("SELECT * FROM admin_approvals WHERE parent_approval_id=? AND parent_generation=? AND qq_id=?", (approval_id, root["code_generation"], qq_id)).fetchone()
            if not vote or not self._current(db, dict(vote)):
                raise SecurityError("没有本人可撤销的复核")
            if root["status"] == "executing":
                raise SecurityError("操作已开始，不能撤回已经执行的命令；请核对实际状态", 409)
            if root["status"] in {"succeeded", "failed", "needs_attention"} and root["kind"] != "server_task":
                raise SecurityError("操作已有执行记录，撤票不能撤销服务器结果；请核对实际状态", 409)
            db.execute("UPDATE admin_approvals SET status='cancelled', code_hash='', updated_at=? WHERE approval_id IN (?,?)", (int(self.clock()), approval_id, vote["approval_id"]))
            self._audit(db, vote["account_id"], "approval.review_revoked", approval_id)

    def quorum_snapshot(self, db, row):
        root = row
        if row["parent_approval_id"]:
            root = dict(db.execute("SELECT * FROM admin_approvals WHERE approval_id=?", (row["parent_approval_id"],)).fetchone())
        return {"quorum": root["quorum"], "approved_count": self._vote_count(db, root),
                "parent_approval_id": row["parent_approval_id"], "parent_status": root["status"],
                "review_command": f"复核 {root['approval_id']}" if root["quorum"] > 1 else None}
