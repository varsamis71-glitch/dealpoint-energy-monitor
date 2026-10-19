import tempfile
import unittest
from pathlib import Path
from monitor import change_kind, csv_safe, digest, excerpts, relevant, safe_url, write_csv


class MonitorTests(unittest.TestCase):
    def test_do_not_invent_single_price(self):
        text = 'Πρώτες 200 kWh\n0,09 €/kWh\nΥπόλοιπη κατανάλωση\n0,18 €/kWh\nΜε έκπτωση συνέπειας'
        evidence = excerpts(text, 'charges')
        self.assertIn('0,09', evidence)
        self.assertIn('0,18', evidence)
        self.assertIn('Πρώτες 200', evidence)

    def test_no_out_of_domain_discovery(self):
        self.assertIsNone(safe_url('https://a.gr/', 'https://a.gr.bad.com/gas', 'a.gr'))
        self.assertIsNone(safe_url('https://a.gr/', 'javascript:alert(1)', 'a.gr'))
        self.assertEqual(safe_url('https://a.gr/', '/gas?utm=x#top', 'a.gr'), 'https://a.gr/gas')

    def test_csv_formula_safety(self):
        self.assertEqual(csv_safe(' =HYPERLINK("bad")')[0], "'")
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'out.csv'
            write_csv(path, [{'Όνομα': '=1+1'}], ['Όνομα'])
            self.assertIn("'=1+1", path.read_text(encoding='utf-8-sig'))

    def test_change_is_content_change_only(self):
        h = digest('same')
        self.assertEqual(change_kind({'hash': h}, h), 'Χωρίς αλλαγή')
        self.assertIn('Αλλαγή περιεχομένου', change_kind({'hash': h}, digest('new')))
        self.assertEqual(change_kind(None, h), 'Πρώτη καταγραφή')

    def test_do_not_discover_known_archives(self):
        self.assertFalse(relevant('https://a.gr/archive/gas'))
        self.assertTrue(relevant('https://a.gr/home/gas-blue'))


if __name__ == '__main__':
    unittest.main()
