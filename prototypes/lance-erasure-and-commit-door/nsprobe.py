import os
import sys


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lance_namespace as ln
from lance.namespace import DescribeTableRequest, ListTablesRequest

from common import *


root = fresh("ns_probe")
os.makedirs(root)
ns = ln.connect("dir", {"root": root})
print(type(ns))
create(os.path.join(root, "t.lance"), tbl([1], ["a"]))
print(ns.list_tables(ListTablesRequest(id=[])))
print(ns.describe_table(DescribeTableRequest(id=["t"])))
