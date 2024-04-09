# This file is part of beets.
# Copyright 2021, Graham R. Cobb.

"""Tests for the 'bareasc' plugin."""

import unittest

from beets import logging
from beets.test.helper import TestHelper, capture_stdout


class BareascPluginTest(unittest.TestCase, TestHelper):
    """Test bare ASCII query matching."""

    def setUp(self):
        """Set up test environment for bare ASCII query matching."""
        self.setup_beets()
        self.log = logging.getLogger("beets.web")
        self.config["bareasc"]["prefix"] = "#"
        self.load_plugins("bareasc")

        # Add library elements. Note that self.lib.add overrides any "id=<n>"
        # and assigns the next free id number.
        self.add_item(title="with accents", album_id=2, artist="Antonín Dvořák")
        self.add_item(title="without accents", artist="Antonín Dvorak")
        self.add_item(title="with umlaut", album_id=2, artist="Brüggen")
        self.add_item(title="without umlaut or e", artist="Bruggen")
        self.add_item(title="without umlaut with e", artist="Brueggen")

    def test_bareasc_search(self):
        test_cases = [
            ("dvorak", ["without accents"]),  # Normal search, no accents, not using bare-ASCII match.
            ("dvořák", ["with accents"]), # Normal search, with accents, not using bare-ASCII match.
            ("#dvorak", ["without accents", "with accents"]), # Bare-ASCII search, no accents.
            ("#dvořák", ["without accents", "with accents"]), # Bare-ASCII search, with accents.
            ("#dvořäk", ["without accents", "with accents"]), # Bare-ASCII search, with incorrect accent.
            ("#Bruggen", ["without umlaut or e", "with umlaut"]), # Bare-ASCII search, with no umlaut.
            ("#Brüggen", ["without umlaut or e", "with umlaut"]), # Bare-ASCII search, with umlaut.
        ]

        for query, expected_titles in test_cases:
            with self.subTest(query=query, expected_titles=expected_titles):
                items = self.lib.items(query)
                titles = [item.title for item in items]
                self.assertEqual(len(items), len(expected_titles))
                self.assertEqual(titles, expected_titles)

    def test_bareasc_list_output(self):
        """Bare-ASCII version of list command - check output."""
        with capture_stdout() as output:
            self.run_command("bareasc", "with accents")

        self.assertIn("Antonin Dvorak", output.getvalue())

    def test_bareasc_format_output(self):
        """Bare-ASCII version of list -f command - check output."""
        with capture_stdout() as output:
            self.run_command(
                "bareasc", "with accents", "-f", "$artist:: $title"
            )

        self.assertEqual("Antonin Dvorak:: with accents\n", output.getvalue())


def suite():
    """loader."""
    return unittest.TestLoader().loadTestsFromName(__name__)


if __name__ == "__main__":
    unittest.main(defaultTest="suite")
