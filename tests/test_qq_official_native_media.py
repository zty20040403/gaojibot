"""Pure schema/byte-boundary tests. No network, credentials, real target or PG."""
from copy import deepcopy
from dataclasses import replace
import base64
import hashlib
import unittest
from urllib.parse import unquote

from src.plugins.ai_chat.adapters.qq_official.native_media import (
    NativeLimits, NativeProtocolError, HASH_PREFIX_BYTES, fingerprint,
    build_small_upload, build_prepare, parse_prepare, build_part_finish,
    confirm_part_finish, build_complete, parse_upload_receipt, build_media_payload,
    validate_part_url,
)
from src.plugins.ai_chat.platform_types import AccountKey, EndpointKey


class NativeContractTests(unittest.TestCase):
    def setUp(self):
        self.account = AccountKey("qq-official", "TEST_NATIVE_APP", "test")
        self.group = EndpointKey(self.account, "group", "TEST_TARGET")
        self.private = EndpointKey(self.account, "private", "TEST_TARGET")
        self.content = b"TEST_PDF_BYTES_1234567"
        self.artifact = fingerprint([self.content[:7], self.content[7:]], filename="test.pdf", file_type=4)
        self.hosts = frozenset({"fixture.cos.example"})

    def prepared_body(self):
        size, block = len(self.content), 10
        return {"upload_id": "TEST_UPLOAD", "block_size": str(block), "parts": [
            {"index": index, "block_size": str(min(block, size - index * block)),
             "presigned_url": f"https://fixture.cos.example/part/{index}?signature=TEST_SIGNED_VALUE"}
            for index in range((size + block - 1) // block)
        ], "upload_config": {"concurrency": 9, "retry_timeout": 300, "retry_delay": 1}}

    def prepared(self, body=None):
        return parse_prepare(200, self.prepared_body() if body is None else body,
            endpoint=self.group, artifact=self.artifact, allowed_hosts=self.hosts)

    def uploaded(self, **updates):
        body = {"file_info": "TEST_OPAQUE_FILE_INFO", "file_uuid": "TEST_UUID", "ttl": 300, **updates}
        return parse_upload_receipt(200, body, endpoint=self.group, artifact=self.artifact, now=1000)

    def test_stream_hashes_match_bytes_and_exact_tencent_prefix(self):
        content = b"a" * HASH_PREFIX_BYTES + b"different suffix"
        artifact = fingerprint([content[:500], content[500:]], filename="test.zip", file_type=4)
        self.assertEqual(artifact.size, len(content))
        self.assertEqual(artifact.sha256, hashlib.sha256(content).hexdigest())
        self.assertEqual(artifact.md5_10m, hashlib.md5(content[:HASH_PREFIX_BYTES]).hexdigest())
        self.assertNotEqual(artifact.md5, artifact.md5_10m)

    def test_small_upload_always_names_and_never_sends(self):
        for endpoint, route in ((self.group, "groups"), (self.private, "users")):
            for kind in range(1, 5):
                artifact = replace(self.artifact, file_type=kind)
                request = build_small_upload(endpoint, artifact, self.content)
                self.assertEqual(request.path, f"/v2/{route}/TEST_TARGET/files")
                self.assertEqual(request.stage, "upload_small")
                self.assertIs(request.body["srv_send_msg"], False)
                self.assertEqual(request.body["file_name"], "test.pdf")
                self.assertEqual(base64.b64decode(request.body["file_data"]), self.content)

    def test_changed_immutable_bytes_are_rejected(self):
        with self.assertRaisesRegex(NativeProtocolError, "artifact_changed"):
            build_small_upload(self.group, self.artifact, b"x" * len(self.content))

    def test_direct_limit_is_local_and_bounded(self):
        limit = NativeLimits(small_bytes=10)
        with self.assertRaisesRegex(NativeProtocolError, "chunked_upload_required"):
            build_small_upload(self.group, self.artifact, self.content, limits=limit)
        for value in (True, 0, -1, 5_000_001, float("inf")):
            with self.subTest(value=value), self.assertRaises(NativeProtocolError):
                NativeLimits(small_bytes=value)

    def test_raw_and_encoded_small_limits_both_apply_at_exact_boundary(self):
        self.assertEqual(NativeLimits().small_bytes, 3_500_000)
        raw = b"x" * 3_500_000
        artifact = fingerprint([raw], filename="boundary.pdf", file_type=4)
        self.assertLessEqual(len(build_small_upload(self.group, artifact, raw).body["file_data"]), 5_000_000)
        raw += b"x"
        artifact = fingerprint([raw], filename="boundary.pdf", file_type=4)
        with self.assertRaisesRegex(NativeProtocolError, "chunked_upload_required"):
            build_small_upload(self.group, artifact, raw)
        # Even a trusted larger raw threshold cannot bypass encoded size.
        limits = NativeLimits(small_bytes=5_000_000)
        raw = b"x" * 3_750_000
        artifact = fingerprint([raw], filename="boundary.pdf", file_type=4)
        self.assertEqual(len(build_small_upload(self.group, artifact, raw, limits=limits).body["file_data"]), 5_000_000)
        raw += b"x"
        artifact = fingerprint([raw], filename="boundary.pdf", file_type=4)
        with self.assertRaisesRegex(NativeProtocolError, "encoded_upload_exceeds_local_limit"):
            build_small_upload(self.group, artifact, raw, limits=limits)

    def test_opaque_native_ids_use_one_encoded_segment_without_normalization(self):
        for native in ("测试:群成员", "TEST:opaque", " TEST spaced ", "TEST?query#fragment", "TEST%2Fencoded", "é", "e\u0301"):
            with self.subTest(native=native):
                endpoint = EndpointKey(self.account, "group", native)
                request = build_prepare(endpoint, self.artifact)
                segments = request.path.split("/")
                self.assertEqual(len(segments), 5)
                self.assertEqual(unquote(segments[3]), native)
                self.assertNotIn("?", request.path)
                self.assertNotIn("#", request.path)
                self.assertTrue(request.path.isascii())
        left = build_prepare(EndpointKey(self.account, "group", "é"), self.artifact)
        right = build_prepare(EndpointKey(self.account, "group", "e\u0301"), self.artifact)
        self.assertNotEqual(left.path, right.path)
        literal_percent = build_prepare(EndpointKey(self.account, "group", "%2F"), self.artifact)
        self.assertIn("/%252F/", literal_percent.path)

    def test_route_separators_traversal_controls_and_utf8_byte_overflow_fail(self):
        for native in ("/", "TEST/target", "../target", "TEST\\target", ".", "..", "TEST\nID", "TEST\u202eID", "x\ud800", "界" * 86):
            with self.subTest(native=native), self.assertRaises(NativeProtocolError):
                build_prepare(EndpointKey(self.account, "group", native), self.artifact)
        self.assertIn("upload_prepare", build_prepare(EndpointKey(self.account, "group", "界" * 85), self.artifact).path)

    def test_file_names_and_types_are_strict_without_silent_path_rewrite(self):
        for value in ("", ".", "..", "../test.pdf", "a\\b.pdf", " test.pdf", "test.pdf.", "a\n.pdf", "a\u202e.pdf", "x" * 256):
            with self.subTest(value=value), self.assertRaises(NativeProtocolError):
                fingerprint([b"x"], filename=value, file_type=4)
        for kind in (0, 5, True, "4"):
            with self.subTest(kind=kind), self.assertRaises(NativeProtocolError):
                fingerprint([b"x"], filename="test.pdf", file_type=kind)

    def test_prepare_uses_current_string_sizes_and_wire_hashes(self):
        request = build_prepare(self.private, self.artifact)
        self.assertEqual(request.path, "/v2/users/TEST_TARGET/upload_prepare")
        self.assertEqual(request.body["file_size"], str(len(self.content)))
        self.assertEqual(request.body["md5"], self.artifact.md5)
        self.assertNotIn("sha256", request.body)
        self.assertNotIn("file_data", request.body)

    def test_prepare_zero_based_parts_cover_source_exactly(self):
        body = self.prepared_body()
        body["parts"].reverse()
        prepared = self.prepared(body)
        self.assertEqual([p.index for p in prepared.parts], [0, 1, 2])
        self.assertEqual([p.offset for p in prepared.parts], [0, 10, 20])
        self.assertEqual(sum(p.length for p in prepared.parts), len(self.content))
        self.assertEqual(prepared.concurrency, 4)

    def test_sdk_one_based_numeric_or_top_level_schema_cannot_mix(self):
        for mutate in (
            lambda b: b.update(block_size=10),
            lambda b: b.update(concurrency=3),
            lambda b: [p.update(index=p["index"] + 1) for p in b["parts"]],
            lambda b: b["parts"][0].update(block_size=10),
        ):
            body = self.prepared_body(); mutate(body)
            with self.assertRaises(NativeProtocolError):
                self.prepared(body)

    def test_part_coverage_duplicate_missing_and_false_indexes_fail(self):
        for mutate in (
            lambda b: b["parts"].pop(),
            lambda b: b["parts"][1].update(index=0),
            lambda b: b["parts"][0].update(index=False),
            lambda b: b["parts"][-1].update(block_size="10"),
            lambda b: b.update(block_size="-1"),
            lambda b: b.update(parts="not-a-list"),
        ):
            body = self.prepared_body(); mutate(body)
            with self.assertRaises(NativeProtocolError):
                self.prepared(body)

    def test_presigned_urls_require_exact_hosts_and_safe_transport_shape(self):
        urls = ("http://fixture.cos.example/x", "https://evil.example/x", "https://fixture.cos.example.evil/x",
                "https://user:secret@fixture.cos.example/x", "https://fixture.cos.example:8443/x",
                "https://fixture.cos.example/x#fragment", "https://127.0.0.1/x", "https://localhost/x",
                "https://fixture.cos.example\\@evil.example/x", "https://[broken/x")
        for url in urls:
            with self.subTest(url=url), self.assertRaises(NativeProtocolError):
                validate_part_url(url, allowed_hosts=self.hosts | {"127.0.0.1", "localhost"})
        with self.assertRaises(NativeProtocolError):
            validate_part_url("https://fixture.cos.example/x", allowed_hosts=frozenset())

    def test_part_finish_and_complete_do_not_create_a_message_or_fake_route(self):
        prepared = self.prepared()
        requests = [build_part_finish(prepared, part.index, self.content[part.offset:part.offset + part.length]) for part in prepared.parts]
        self.assertEqual([r.body["part_index"] for r in requests], [0, 1, 2])
        self.assertTrue(all(isinstance(r.body["block_size"], str) for r in requests))
        self.assertEqual(requests[0].body["md5"], hashlib.md5(self.content[:10]).hexdigest())
        self.assertTrue(confirm_part_finish(200, {}))
        complete = build_complete(prepared)
        self.assertEqual(complete.path, "/v2/groups/TEST_TARGET/files")
        self.assertEqual(complete.body, {"upload_id": "TEST_UPLOAD", "file_type": 4, "file_name": "test.pdf", "srv_send_msg": False})

    def test_http_async_or_body_errors_are_not_completed_uploads(self):
        good = {"file_info": "TEST_INFO", "file_uuid": "TEST_UUID", "ttl": 300}
        for status, body in ((201, good), (202, good), (500, good), (True, good), (200, []), (200, {**good, "err_code": 50015014}), (200, {**good, "code": False})):
            with self.subTest(status=status), self.assertRaises(NativeProtocolError):
                parse_upload_receipt(status, body, endpoint=self.group, artifact=self.artifact, now=1000)

    def test_upload_message_id_is_a_contract_alarm_not_permission_to_send_twice(self):
        for value in ("TEST_ALREADY_SENT", {}, 7, False):
            with self.subTest(value=value), self.assertRaises(NativeProtocolError) as caught:
                self.uploaded(id=value)
            self.assertTrue(caught.exception.message_evidence)
            self.assertNotIn("TEST_ALREADY_SENT", str(caught.exception))

    def test_receipt_ttl_zero_is_locally_bounded_and_short_ttl_is_unusable(self):
        self.assertEqual(self.uploaded(ttl=0).reusable_until, 1000 + 86400 - 5)
        self.assertEqual(self.uploaded(ttl=300).reusable_until, 1295)
        self.assertEqual(self.uploaded(ttl=1).reusable_until, 1000)
        for value in (None, True, -1, "300", float("inf")):
            with self.subTest(value=value), self.assertRaises(NativeProtocolError):
                self.uploaded(ttl=value)

    def test_file_info_is_opaque_bounded_hidden_and_raw_url_discarded(self):
        media = self.uploaded(raw_url="https://secret.example/?signed=TEST_SECRET")
        self.assertNotIn("TEST_OPAQUE_FILE_INFO", repr(media))
        self.assertNotIn("TEST_SIGNED_VALUE", repr(self.prepared()))
        self.assertFalse(hasattr(media, "raw_url"))
        for value in ("", "a\nsecret", "x" * 65537, {}, "a\ud800"):
            with self.subTest(length=len(value)), self.assertRaises(NativeProtocolError):
                self.uploaded(file_info=value)

    def test_cache_identity_separates_account_environment_endpoint_type_and_filename(self):
        media = self.uploaded()
        variants = [media, replace(media, endpoint=self.private),
            replace(media, endpoint=EndpointKey(replace(self.account, app_id="TEST_OTHER"), "group", "TEST_TARGET")),
            replace(media, endpoint=EndpointKey(replace(self.account, environment="sandbox"), "group", "TEST_TARGET")),
            replace(media, artifact=replace(self.artifact, filename="renamed.pdf")),
            replace(media, artifact=replace(self.artifact, file_type=1))]
        self.assertEqual(len({item.cache_key for item in variants}), len(variants))

    def test_media_message_uses_only_m3_supplied_sequence_and_credential(self):
        media = self.uploaded()
        payload = build_media_payload(media, endpoint=self.group, now=1001, msg_id="TEST_SOURCE", msg_seq=65535, content="说明")
        self.assertEqual(payload, {"msg_type": 7, "media": {"file_info": "TEST_OPAQUE_FILE_INFO"}, "msg_id": "TEST_SOURCE", "msg_seq": 65535, "content": "说明"})
        self.assertNotIn("msg_seq", build_media_payload(media, endpoint=self.group, now=1001))
        self.assertNotIn("content", build_media_payload(media, endpoint=self.group, now=1001))
        for seq in (True, -1, 65536, None):
            with self.subTest(seq=seq), self.assertRaises(NativeProtocolError):
                build_media_payload(media, endpoint=self.group, now=1001, msg_id="TEST_SOURCE", msg_seq=seq)

    def test_media_target_expiry_conflicting_credentials_and_unsafe_caption_rejected(self):
        media = self.uploaded()
        for values in ({"endpoint": self.private}, {"now": 1295}, {"msg_id": "A", "event_id": "B", "msg_seq": 1},
                       {"content": "<qqbot-at-user id='TEST'>"}, {"content": "x" * 2001}, {"content": "x\ud800"}):
            with self.subTest(values=values), self.assertRaises(NativeProtocolError):
                build_media_payload(media, **{"endpoint": self.group, "now": 1001, **deepcopy(values)})

    def test_media_send_clock_is_strict_integer_before_expiry_check(self):
        media = self.uploaded()
        for now in (float("nan"), float("inf"), float("-inf"), 1001.0, True, False, -1, 2**63, "1001", None):
            with self.subTest(now=now), self.assertRaises(NativeProtocolError) as error:
                build_media_payload(media, endpoint=self.group, now=now)
            self.assertEqual(error.exception.stage, "send")


if __name__ == "__main__":
    unittest.main()
