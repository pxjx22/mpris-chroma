import unittest
from dataclasses import FrozenInstanceError

from mpris_chroma.state import PlayerState

class PlayerStateTest(unittest.TestCase):
    def test_instantiation(self):
        """Verify that PlayerState is correctly instantiated with all fields."""
        state = PlayerState(status="Playing", art_url="http://example.com/art.jpg", seq=1)

        self.assertEqual(state.status, "Playing")
        self.assertEqual(state.art_url, "http://example.com/art.jpg")
        self.assertEqual(state.seq, 1)

    def test_frozen(self):
        """Verify that PlayerState is immutable (frozen)."""
        state = PlayerState(status="Paused", art_url="", seq=2)

        with self.assertRaises(FrozenInstanceError):
            state.status = "Playing"

    def test_slots(self):
        """Verify that PlayerState uses slots (no __dict__)."""
        state = PlayerState(status="Stopped", art_url="", seq=3)

        # A dataclass with slots=True does not have a __dict__ attribute
        self.assertFalse(hasattr(state, "__dict__"))
