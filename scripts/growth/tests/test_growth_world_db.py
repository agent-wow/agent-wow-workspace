from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from _support import ROOT  # noqa: F401

from scripts.growth.world_db import SQLDumpError, iter_table, parse_insert


class InsertParserTests(unittest.TestCase):
    def test_escaped_strings_commas_parentheses_null_and_numbers(self):
        statement = (
            "INSERT INTO `sample` VALUES\n"
            r"(1,'a,(b)',NULL,-2,3.5,'it\'s','doubled ''quote''',"
            r"'line\nnext','slash\\'),"
            "\n(2,'',NULL,0,1e2,'x','y','z','w');"
        )
        rows = parse_insert(statement, 9)
        self.assertEqual(rows[0][:5], [1, "a,(b)", None, -2, 3.5])
        self.assertEqual(rows[0][5], "it's")
        self.assertEqual(rows[0][6], "doubled 'quote'")
        self.assertEqual(rows[0][7], "line\nnext")
        self.assertEqual(rows[0][8], "slash\\")
        self.assertEqual(rows[1][4], 100.0)

    def test_truncated_or_malformed_values_fail_closed(self):
        invalid = (
            "INSERT INTO `x` VALUES (1,'unterminated);",
            "INSERT INTO `x` VALUES (1,2)",
            "INSERT INTO `x` VALUES (1,);",
            "INSERT INTO `x` VALUES (1,2); trailing",
        )
        for statement in invalid:
            with self.subTest(statement=statement):
                with self.assertRaises(SQLDumpError):
                    parse_insert(statement, 2)


class DumpStreamTests(unittest.TestCase):
    def dump(self, body: str, footer: bool = True) -> str:
        return (
            "CREATE TABLE `x` (\n"
            "  `id` int NOT NULL,\n"
            "  `name` text,\n"
            "  PRIMARY KEY (`id`)\n"
            ") ENGINE=InnoDB;\n"
            "LOCK TABLES `x` WRITE;\n"
            + body + ("\n-- Dump completed on 2026-06-01 00:00:00\n" if footer else "")
        )

    def read(self, text: str):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.sql"
            path.write_text(text, encoding="utf-8")
            return list(iter_table(path))

    def test_streams_multiple_insert_statements(self):
        rows = self.read(self.dump(
            "INSERT INTO `x` VALUES\n(1,'one');\n"
            "UNLOCK TABLES;\nINSERT INTO `x` VALUES\n(2,'two');"
        ))
        self.assertEqual(rows, [{"id": 1, "name": "one"},
                                {"id": 2, "name": "two"}])

    def test_missing_footer_and_truncated_statement_are_rejected(self):
        for body, footer in (("INSERT INTO `x` VALUES (1,'ok');", False),
                             ("INSERT INTO `x` VALUES (1,'bad')", True)):
            with self.subTest(footer=footer):
                with self.assertRaises(SQLDumpError):
                    self.read(self.dump(body, footer))


if __name__ == "__main__":
    unittest.main()
