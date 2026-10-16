"""Tests for the player registry and the access code."""

from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest

from antilag.registry import (
    ClientEntry,
    Registry,
    RegistryError,
    make_access_code,
    parse_access_code,
)


def registry(**kw) -> Registry:
    return Registry(endpoint=kw.get("endpoint", "203.0.113.10:51888"))


class TestAdd(unittest.TestCase):
    def test_first_player_gets_dot_two(self):
        """The .1 belongs to the VPS inside the tunnel."""
        reg = registry()
        entry = reg.add("principal")
        self.assertEqual((entry.id, entry.address), (1, "10.66.0.2"))
        self.assertEqual(len(entry.secret), 64)

    def test_next_free_id_and_ip(self):
        reg = registry()
        reg.add("principal")
        reg.add("amigo1")
        reg.remove("principal")
        third = reg.add("amigo2")
        # reuses whatever was freed
        self.assertEqual((third.id, third.address), (1, "10.66.0.2"))

    def test_duplicate_name_rejected(self):
        reg = registry()
        reg.add("amigo")
        with self.assertRaises(RegistryError):
            reg.add("AMIGO")

    def test_keeps_existing_secret(self):
        """Migration: today's secret becomes player 1's."""
        entry = registry().add("principal", secret="secreto-viejo")
        self.assertEqual(entry.secret, "secreto-viejo")

    def test_network_full(self):
        reg = Registry(endpoint="x:1", network="10.66.0.0/30")  # .1 VPS, .2 the only player
        reg.add("a")
        with self.assertRaises(RegistryError):
            reg.add("b")

    def test_remove_unknown(self):
        with self.assertRaises(RegistryError):
            registry().remove("nadie")


class TestValidate(unittest.TestCase):
    def bad(self, *clients):
        reg = registry()
        reg.clients = list(clients)
        with self.assertRaises(RegistryError):
            reg.validate()

    def test_duplicate_ids(self):
        self.bad(ClientEntry(1, "a", "s", "10.66.0.2"), ClientEntry(1, "b", "s", "10.66.0.3"))

    def test_duplicate_ips(self):
        self.bad(ClientEntry(1, "a", "s", "10.66.0.2"), ClientEntry(2, "b", "s", "10.66.0.2"))

    def test_ip_outside_network(self):
        self.bad(ClientEntry(1, "a", "s", "10.67.0.2"))

    def test_server_ip_is_not_a_player(self):
        self.bad(ClientEntry(1, "a", "s", "10.66.0.1"))

    def test_empty_secret(self):
        self.bad(ClientEntry(1, "a", "", "10.66.0.2"))


class TestFile(unittest.TestCase):
    def test_round_trip_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "antilag", "clients.json")
            reg = registry()
            reg.add("principal")
            reg.save(path)
            if os.name == "posix":  # the registry lives on the VPS; Windows has no such permissions
                self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            loaded = Registry.load(path)
            self.assertEqual(loaded.clients, reg.clients)
            self.assertEqual(loaded.endpoint, reg.endpoint)

    def test_missing_file_explains(self):
        with self.assertRaises(RegistryError) as ctx:
            Registry.load("/no/existe/clients.json")
        self.assertIn("clients init", str(ctx.exception))

    def test_invalid_file_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "clients.json")
            with open(path, "w") as handle:
                json.dump({"clients": [{"id": 1, "name": "a"}]}, handle)
            with self.assertRaises(RegistryError):
                Registry.load(path)


class TestAccessCode(unittest.TestCase):
    def test_round_trip(self):
        reg = registry()
        reg.add("principal")
        entry = reg.add("amigo1")
        access = parse_access_code(make_access_code(reg, entry))
        self.assertEqual(access.endpoint, "203.0.113.10:51888")
        self.assertEqual((access.client_id, access.name), (2, "amigo1"))
        self.assertEqual(access.address, "10.66.0.3/24")
        self.assertEqual(access.secret, entry.secret)

    def test_survives_line_breaks_when_pasted(self):
        reg = registry()
        entry = reg.add("a")
        code = make_access_code(reg, entry)
        self.assertEqual(parse_access_code(code[:20] + "\n " + code[20:]).client_id, 1)

    def test_needs_endpoint(self):
        reg = Registry()
        entry = reg.add("a")
        with self.assertRaises(RegistryError):
            make_access_code(reg, entry)

    def test_garbage_rejected(self):
        for code in ("hola", "antilag1:@@@", "antilag1:e30"):  # e30 = "{}"
            with self.assertRaises(RegistryError):
                parse_access_code(code)

    def test_truncated_rejected(self):
        reg = registry()
        code = make_access_code(reg, reg.add("a"))
        with self.assertRaises(RegistryError):
            parse_access_code(code[:-10])


if __name__ == "__main__":
    unittest.main()
