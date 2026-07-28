from __future__ import annotations

import unittest

from packwiz_parser import PackwizOutputParser, visible_menu_items


class PackwizOutputParserTest(unittest.TestCase):
    def test_extracts_only_explicit_project_ids_from_menu_labels(self) -> None:
        parser = PackwizOutputParser()
        events = parser.feed(
            b"1) Root - Project ID: 12345\n"
            b"2) Similar Name (67890)\n"
            b"0) Cancel\nChoose a number: ",
        )
        result = next(event for event in events if event.kind == "search_results")
        items = {item.index: item for item in result.items}
        self.assertEqual(items[1].canonical_project_id, "12345")
        self.assertIsNone(items[2].canonical_project_id)

    def test_parses_split_ansi_menu(self) -> None:
        parser = PackwizOutputParser()
        payload = (
            b"\x1b[32mSearching Modrinth...\x1b[0m\r\n"
            b"0) Cancel\r\n"
            b"1) *Create\r\n"
            b"2) Create Deco\r\n"
            b"Choose a number:"
        )

        events = []
        for boundary in (17, 43, len(payload)):
            chunk, payload = payload[:boundary], payload[boundary:]
            events.extend(parser.feed(chunk))
            if not payload:
                break

        result_event = next(event for event in events if event.kind == "search_results")
        items = visible_menu_items(result_event.items)
        self.assertEqual([item.index for item in items], [1, 2])
        self.assertEqual([item.label for item in items], ["Create", "Create Deco"])
        self.assertTrue(items[0].is_default)

    def test_deduplicates_yes_no_prompt_after_input_echo(self) -> None:
        parser = PackwizOutputParser()
        events = parser.feed(b"Would you like to add them? (Y/n)")
        events += parser.feed(b"y\r\n")
        confirmations = [event for event in events if event.kind == "confirmation"]
        self.assertEqual(len(confirmations), 1)

    def test_carriage_return_replaces_progress_line(self) -> None:
        parser = PackwizOutputParser()
        parser.feed(b"Refreshing index... 10%\rRefreshing index... 90%\r\n", final=True)
        self.assertIn("Refreshing index... 90%", parser.normalized_lines)
        self.assertNotIn("Refreshing index... 10%", parser.normalized_lines)


if __name__ == "__main__":
    unittest.main()
