import sys
import types
import unittest

# The workflow installs these runtime dependencies. Destination resolution is
# pure, so keep its unit tests runnable in a dependency-free checkout.
sys.modules.setdefault("requests", types.ModuleType("requests"))
sys.modules.setdefault("yaml", types.ModuleType("yaml"))

from scripts.summarize import resolve_destinations


class ResolveDestinationsTests(unittest.TestCase):
    def test_resolves_topic_and_regular_group_destinations(self):
        project = {
            "destinations": [
                {"chat_id": -1003947505610, "thread_id": 62},
                {"chat_id": -1001546860015, "topics": False},
            ]
        }

        self.assertEqual(
            resolve_destinations(project),
            [(-1003947505610, 62), (-1001546860015, None)],
        )

    def test_skips_group_with_no_topic_mode_or_thread_id(self):
        project = {"destinations": [{"chat_id": -1001546860015}]}

        self.assertEqual(resolve_destinations(project), [])

    def test_channel_remains_a_supported_threadless_alias(self):
        project = {"chat_id": -1002627184483, "channel": True}

        self.assertEqual(resolve_destinations(project), [(-1002627184483, None)])


if __name__ == "__main__":
    unittest.main()
