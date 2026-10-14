import io,unittest
from bonuspro.backup_stream import encrypt,decrypt,InvalidBackup,CHUNK
class BackupStreamTests(unittest.TestCase):
    def setUp(self):self.key=b"k"*32
    def encode(self,value,key=None):
        target=io.BytesIO();encrypt(io.BytesIO(value),target,key or self.key);return target.getvalue()
    def decode(self,value,key=None):
        target=io.BytesIO();decrypt(io.BytesIO(value),target,key or self.key);return target.getvalue()
    def test_empty_and_multichunk_round_trip(self):
        for value in (b"",b"secret fixture",(b"abc"*(CHUNK//3+1))*3):
            self.assertEqual(self.decode(self.encode(value)),value)
    def test_ciphertext_does_not_expose_plaintext_and_is_random_per_file(self):
        value=b"private-test-only"
        first=self.encode(value);second=self.encode(value)
        self.assertNotEqual(first,second);self.assertNotIn(value,first)
    def test_wrong_master_key_is_rejected(self):
        with self.assertRaises(InvalidBackup):self.decode(self.encode(b"value"),b"z"*32)
    def test_tampering_in_header_ciphertext_and_final_record_is_rejected(self):
        encoded=self.encode(b"a"*(CHUNK+1))
        for position in (8,40,55,len(encoded)-1):
            value=bytearray(encoded);value[position]^=1
            with self.assertRaises(InvalidBackup):self.decode(value)
    def test_tail_truncation_and_extra_bytes_are_detected(self):
        encoded=self.encode(b"secret")
        for value in (encoded[:-1],encoded[:-20],encoded+b"x",encoded[:48]):
            with self.assertRaises(InvalidBackup):self.decode(value)
    def test_record_reordering_and_unbounded_length_are_rejected(self):
        import struct
        value=self.encode(b"a"*(CHUNK+5))
        first_end=48+4+struct.unpack(">I",value[48:52])[0]
        second_end=first_end+4+struct.unpack(">I",value[first_end:first_end+4])[0]
        with self.assertRaises(InvalidBackup):
            self.decode(value[:48]+value[first_end:second_end]+value[48:first_end]+value[second_end:])
        encoded=bytearray(self.encode(b"secret"));encoded[48:52]=b"\xff"*4
        with self.assertRaises(InvalidBackup):self.decode(encoded)
    def test_reader_handles_short_reads(self):
        class Short(io.BytesIO):
            def read(inner,size=-1):return super().read(min(3,size) if size>=0 else 3)
        target=io.BytesIO();decrypt(Short(self.encode(b"test")),target,self.key)
        self.assertEqual(target.getvalue(),b"test")
if __name__=="__main__":unittest.main()
