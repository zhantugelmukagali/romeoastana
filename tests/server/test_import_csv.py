import unittest
from uuid import uuid4
from bonuspro.importing import parse_csv,ImportInvalid

class CSVTests(unittest.TestCase):
    def test_canonical_manifest_ignores_order_and_separator(self):
        a,b=str(uuid4()),str(uuid4())
        one=parse_csv("counterparty_id,bonus_balance\n"+a+",15\n"+b+",0\n")
        two=parse_csv("\ufeffcounterparty_id;bonus_balance\n"+b+";0\n"+a+";15\n")
        self.assertEqual(one[:2],two[:2]);self.assertNotEqual(one[2],two[2])
    def test_reject_ambiguous_or_unsafe_financial_input(self):
        a=str(uuid4());header="counterparty_id,bonus_balance\n"
        for value in ("-1","1.1","1e3","01","90000000000001","=1+1",""):
            with self.subTest(value=value),self.assertRaises(ImportInvalid):parse_csv(header+a+","+value)
        for body in (header+a+",1\n"+a+",2",header+"bad,1",header+a+",1,2",header,
                     "name,balance\nTest,2",header+"\n".join(str(uuid4())+",1" for _ in range(101))):
            with self.assertRaises(ImportInvalid):parse_csv(body)

if __name__=="__main__":unittest.main()
