"""Protocol tests: round trip, deduplication, replay and limits."""

from __future__ import annotations

import os
import unittest

from antilag.proto import (
    DEFAULT_WINDOW,
    DIR_DOWN,
    DIR_UP,
    KIND_DATA,
    KIND_PING,
    OVERHEAD,
    Codec,
    ProtocolError,
    ReplayWindow,
    HEADER_LEN,
    derive_key,
    generate_secret,
    new_epoch,
    peek_client_id,
)


def _pair(window: int = DEFAULT_WINDOW) -> tuple[Codec, Codec]:
    """A client codec and a server codec that share a key."""
    key = derive_key("frase-de-prueba")
    return Codec(key, DIR_UP, window), Codec(key, DIR_DOWN, window)


class TestKeyDerivation(unittest.TestCase):
    def test_deterministic(self):
        self.assertEqual(derive_key("abc"), derive_key("abc"))

    def test_distinct_secrets_differ(self):
        self.assertNotEqual(derive_key("abc"), derive_key("abd"))

    def test_salt_changes_key(self):
        self.assertNotEqual(derive_key("abc"), derive_key("abc", salt=b"x"))

    def test_length_is_32(self):
        self.assertEqual(len(derive_key("abc")), 32)

    def test_empty_secret_rejected(self):
        with self.assertRaises(ValueError):
            derive_key("")

    def test_generated_secret_usable(self):
        self.assertEqual(len(derive_key(generate_secret())), 32)

    def test_bad_key_length_rejected(self):
        with self.assertRaises(ValueError):
            Codec(b"demasiado-corta", DIR_UP)


class TestRoundTrip(unittest.TestCase):
    def test_payload_survives(self):
        up, down = _pair()
        payload = b"\x45\x00\x00\x28paquete-ip-simulado"
        _, wire = up.seal(payload)
        frame = down.open(wire)
        self.assertIsNotNone(frame)
        self.assertEqual(frame.payload, payload)
        self.assertEqual(frame.kind, KIND_DATA)

    def test_kind_is_carried(self):
        up, down = _pair()
        _, wire = up.seal(b"", kind=KIND_PING)
        self.assertEqual(down.open(wire).kind, KIND_PING)

    def test_empty_payload(self):
        up, down = _pair()
        _, wire = up.seal(b"")
        self.assertEqual(down.open(wire).payload, b"")

    def test_overhead_is_exact(self):
        up, _ = _pair()
        payload = os.urandom(1340)
        _, wire = up.seal(payload)
        self.assertEqual(len(wire) - len(payload), OVERHEAD)

    def test_sequence_increments(self):
        up, _ = _pair()
        seqs = [up.seal(b"x")[0] for _ in range(5)]
        self.assertEqual(seqs, [0, 1, 2, 3, 4])

    def test_mtu_sized_payload(self):
        up, down = _pair()
        payload = os.urandom(1340)
        _, wire = up.seal(payload)
        self.assertEqual(down.open(wire).payload, payload)


class TestAuthentication(unittest.TestCase):
    def test_tampered_payload_rejected(self):
        up, down = _pair()
        _, wire = up.seal(b"carga-util")
        corrupted = bytearray(wire)
        corrupted[-1] ^= 0x01
        with self.assertRaises(ProtocolError):
            down.open(bytes(corrupted))

    def test_tampered_header_rejected(self):
        """The header travels in clear but authenticated as AAD."""
        up, down = _pair()
        _, wire = up.seal(b"carga-util")
        corrupted = bytearray(wire)
        corrupted[1] ^= 0xFF  # changes 'kind'
        with self.assertRaises(ProtocolError):
            down.open(bytes(corrupted))

    def test_wrong_key_rejected(self):
        up = Codec(derive_key("clave-a"), DIR_UP)
        down = Codec(derive_key("clave-b"), DIR_DOWN)
        _, wire = up.seal(b"carga-util")
        with self.assertRaises(ProtocolError):
            down.open(wire)

    def test_reflected_own_frame_rejected(self):
        """An own frame reflected back uses the opposite direction."""
        up_a, _ = _pair()
        up_b, _ = _pair()  # same role, same key
        _, wire = up_a.seal(b"carga-util")
        with self.assertRaises(ProtocolError):
            up_b.open(wire)

    def test_truncated_rejected(self):
        up, down = _pair()
        _, wire = up.seal(b"carga-util")
        with self.assertRaises(ProtocolError):
            down.open(wire[:10])

    def test_bad_version_rejected(self):
        up, down = _pair()
        _, wire = up.seal(b"carga-util")
        corrupted = bytearray(wire)
        corrupted[0] = 99
        with self.assertRaises(ProtocolError):
            down.open(bytes(corrupted))

    def test_invalid_counter_advances(self):
        up, down = _pair()
        _, wire = up.seal(b"carga-util")
        with self.assertRaises(ProtocolError):
            down.open(wire[:10])
        self.assertEqual(down.stats()["invalid"], 1)


class TestDeduplication(unittest.TestCase):
    def test_duplicate_copy_dropped(self):
        """The core case: the same frame over two links is delivered once."""
        up, down = _pair()
        _, wire = up.seal(b"carga-util")
        self.assertIsNotNone(down.open(wire))  # arrived over Ethernet
        self.assertIsNone(down.open(wire))     # same copy over Wi-Fi
        self.assertEqual(down.stats()["accepted"], 1)
        self.assertEqual(down.stats()["duplicates"], 1)

    def test_three_links_deliver_once(self):
        up, down = _pair()
        _, wire = up.seal(b"carga-util")
        results = [down.open(wire) for _ in range(3)]
        self.assertEqual(sum(r is not None for r in results), 1)

    def test_out_of_order_accepted(self):
        up, down = _pair()
        wires = [up.seal(bytes([i]))[0:2] for i in range(5)]
        frames = [w for _, w in wires]
        # Out-of-order arrival, as when one link is slower than the other
        for index in (3, 0, 4, 1, 2):
            self.assertIsNotNone(down.open(frames[index]), f"seq {index} rechazada")
        self.assertEqual(down.stats()["accepted"], 5)

    def test_dedup_survives_reordering(self):
        up, down = _pair()
        frames = [up.seal(bytes([i]))[1] for i in range(4)]
        for index in (2, 0, 2, 3, 0, 1, 3):
            down.open(frames[index])
        self.assertEqual(down.stats()["accepted"], 4)
        self.assertEqual(down.stats()["duplicates"], 3)


class TestEpochs(unittest.TestCase):
    """Restarts: without epochs, seq would go back to 0 with the same key."""

    KEY = derive_key("frase-de-prueba")

    def test_restart_does_not_reuse_keystream(self):
        """Two startups sealing seq 0 with the same plaintext do not match."""
        first = Codec(self.KEY, DIR_UP).seal(b"misma-carga")[1]
        second = Codec(self.KEY, DIR_UP).seal(b"misma-carga")[1]
        self.assertNotEqual(first[HEADER_LEN:], second[HEADER_LEN:])

    def test_sender_restart_is_accepted(self):
        down = Codec(self.KEY, DIR_DOWN, clock=lambda: 1_005.0)
        old = Codec(self.KEY, DIR_UP, epoch=new_epoch(1_000))
        for _ in range(3):
            down.open(old.seal(b"x")[1])
        restarted = Codec(self.KEY, DIR_UP, epoch=new_epoch(1_005))
        frame = down.open(restarted.seal(b"tras-reinicio")[1])
        self.assertIsNotNone(frame)
        self.assertEqual(frame.seq, 0)
        self.assertEqual(down.stats()["peer_restarts"], 1)

    def test_old_epoch_rejected_after_restart(self):
        """Frames captured from the previous startup cannot be replayed."""
        down = Codec(self.KEY, DIR_DOWN, clock=lambda: 1_000.0)
        old = Codec(self.KEY, DIR_UP, epoch=new_epoch(1_000))
        captured = old.seal(b"vieja")[1]
        down.open(captured)
        down.open(Codec(self.KEY, DIR_UP, epoch=new_epoch(1_000)).seal(b"nueva")[1])
        with self.assertRaises(ProtocolError):
            down.open(captured)
        self.assertEqual(down.stats()["stale"], 1)

    def test_older_epoch_rejected(self):
        down = Codec(self.KEY, DIR_DOWN, clock=lambda: 1_000.0)
        down.open(Codec(self.KEY, DIR_UP, epoch=new_epoch(1_000)).seal(b"x")[1])
        older = Codec(self.KEY, DIR_UP, epoch=new_epoch(990))
        with self.assertRaises(ProtocolError):
            down.open(older.seal(b"x")[1])

    def test_fresh_receiver_rejects_skewed_epoch(self):
        down = Codec(self.KEY, DIR_DOWN, clock=lambda: 10_000.0, max_skew_s=60)
        stale = Codec(self.KEY, DIR_UP, epoch=new_epoch(5_000))
        with self.assertRaises(ProtocolError):
            down.open(stale.seal(b"x")[1])

    def test_future_epoch_rejected(self):
        down = Codec(self.KEY, DIR_DOWN, clock=lambda: 1_000.0, max_skew_s=60)
        down.open(Codec(self.KEY, DIR_UP, epoch=new_epoch(1_000)).seal(b"x")[1])
        future = Codec(self.KEY, DIR_UP, epoch=new_epoch(9_999))
        with self.assertRaises(ProtocolError):
            down.open(future.seal(b"x")[1])

    def test_forged_new_epoch_does_not_switch(self):
        """A new epoch is only adopted if the frame authenticates."""
        down = Codec(self.KEY, DIR_DOWN, clock=lambda: 1_000.0)
        legit = Codec(self.KEY, DIR_UP, epoch=new_epoch(1_000))
        down.open(legit.seal(b"x")[1])
        forger = Codec(derive_key("otra"), DIR_UP, epoch=new_epoch(1_000))
        with self.assertRaises(ProtocolError):
            down.open(forger.seal(b"x")[1])
        self.assertEqual(down.peer_epoch, legit.epoch)
        self.assertIsNotNone(down.open(legit.seal(b"y")[1]))

    def test_bad_epoch_length_rejected(self):
        with self.assertRaises(ValueError):
            Codec(self.KEY, DIR_UP, epoch=b"corto")


class TestClientId(unittest.TestCase):
    """Several players on one VPS: the id picks the session and separates the keys."""

    KEY = derive_key("frase-de-prueba")

    def test_peek_without_decrypting(self):
        wire = Codec(self.KEY, DIR_UP, client_id=7).seal(b"x")[1]
        self.assertEqual(peek_client_id(wire), 7)

    def test_round_trip_with_id(self):
        up = Codec(self.KEY, DIR_UP, client_id=3)
        down = Codec(self.KEY, DIR_DOWN, client_id=3)
        self.assertEqual(down.open(up.seal(b"hola")[1]).payload, b"hola")
        self.assertEqual(up.open(down.seal(b"chao")[1]).payload, b"chao")

    def test_other_client_id_rejected(self):
        """Not even with the same secret: the id goes into the keys and the header."""
        wire = Codec(self.KEY, DIR_UP, client_id=2).seal(b"x")[1]
        with self.assertRaises(ProtocolError):
            Codec(self.KEY, DIR_DOWN, client_id=3).open(wire)

    def test_tampered_id_does_not_authenticate(self):
        wire = bytearray(Codec(self.KEY, DIR_UP, client_id=2).seal(b"x")[1])
        wire[5] = 3  # client_id 2 -> 3
        with self.assertRaises(ProtocolError):
            Codec(self.KEY, DIR_DOWN, client_id=3).open(bytes(wire))

    def test_old_version_says_update(self):
        wire = bytearray(Codec(self.KEY, DIR_UP).seal(b"x")[1])
        wire[0] = 2
        with self.assertRaises(ProtocolError) as ctx:
            peek_client_id(bytes(wire))
        self.assertIn("actualiza", str(ctx.exception))

    def test_id_out_of_range(self):
        with self.assertRaises(ValueError):
            Codec(self.KEY, DIR_UP, client_id=70000)


class TestReplayWindow(unittest.TestCase):
    def test_first_sequence_accepted(self):
        self.assertTrue(ReplayWindow().check_and_set(0))

    def test_nonzero_start_accepted(self):
        self.assertTrue(ReplayWindow().check_and_set(500))

    def test_duplicate_rejected(self):
        window = ReplayWindow()
        window.check_and_set(7)
        self.assertFalse(window.check_and_set(7))

    def test_in_window_backfill_accepted(self):
        window = ReplayWindow(window=64)
        window.check_and_set(40)
        self.assertTrue(window.check_and_set(20))
        self.assertFalse(window.check_and_set(20))

    def test_too_old_rejected(self):
        window = ReplayWindow(window=16)
        window.check_and_set(100)
        self.assertFalse(window.check_and_set(50))

    def test_edge_of_window(self):
        window = ReplayWindow(window=16)
        window.check_and_set(20)
        self.assertTrue(window.check_and_set(5))    # offset 15, inside
        self.assertFalse(window.check_and_set(4))   # offset 16, outside

    def test_large_jump_resets_window(self):
        window = ReplayWindow(window=16)
        window.check_and_set(1)
        self.assertTrue(window.check_and_set(10_000))
        self.assertFalse(window.check_and_set(10_000))
        self.assertFalse(window.check_and_set(1))

    def test_negative_rejected(self):
        self.assertFalse(ReplayWindow().check_and_set(-1))

    def test_beyond_uint64_rejected(self):
        self.assertFalse(ReplayWindow().check_and_set(1 << 64))

    def test_bitmap_stays_bounded(self):
        """The bitmap must not grow without bound after many packets."""
        window = ReplayWindow(window=128)
        for seq in range(5000):
            window.check_and_set(seq)
        self.assertLessEqual(window.bitmap.bit_length(), 128)

    def test_full_window_sweep(self):
        window = ReplayWindow(window=64)
        for seq in range(64):
            self.assertTrue(window.check_and_set(seq))
        for seq in range(64):
            self.assertFalse(window.check_and_set(seq))

    def test_invalid_window_size(self):
        with self.assertRaises(ValueError):
            ReplayWindow(window=0)


if __name__ == "__main__":
    unittest.main()
